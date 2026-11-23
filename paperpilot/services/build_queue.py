"""Bounded, persisted per-paper scheduling; automatic input never erases explicit work."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import uuid

from paperpilot.services.project_errors import ProjectServiceError

GLOBAL_BUILD_LIMIT = 2

def initialize_schema(conn):
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(build_jobs)')}
    for name, declaration in [('trigger_kind', "TEXT NOT NULL DEFAULT 'explicit'"), ('queue_seq', 'INTEGER NOT NULL DEFAULT 0'),
        ('snapshot_identity', "TEXT NOT NULL DEFAULT ''"), ('checks_json', "TEXT NOT NULL DEFAULT '{}'"), ('request_fingerprint', "TEXT NOT NULL DEFAULT ''")]:
        if name not in columns:
            conn.execute(f'ALTER TABLE build_jobs ADD COLUMN {name} {declaration}')
    conn.execute('UPDATE build_jobs SET queue_seq=rowid WHERE queue_seq=0')
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS build_request_aliases(
          project_id TEXT NOT NULL,request_id TEXT NOT NULL,request_fingerprint TEXT NOT NULL,
          job_id TEXT NOT NULL REFERENCES build_jobs(id),created_at TEXT NOT NULL,
          PRIMARY KEY(project_id,request_id));
        CREATE INDEX IF NOT EXISTS idx_build_schedule ON build_jobs(project_id,status,trigger_kind,queue_seq);
        CREATE INDEX IF NOT EXISTS idx_build_identity ON build_jobs(project_id,snapshot_identity,purpose,status);
        CREATE TABLE IF NOT EXISTS project_build_profile_overrides(
          project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
          profile_json TEXT NOT NULL,version INTEGER NOT NULL,updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS project_build_profile_events(
          id TEXT PRIMARY KEY,project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          request_id TEXT NOT NULL,action TEXT NOT NULL,profile_json TEXT NOT NULL,
          version INTEGER NOT NULL,created_at TEXT NOT NULL,base_revision INTEGER NOT NULL,
          result_json TEXT NOT NULL,UNIQUE(project_id,request_id));
        CREATE TABLE IF NOT EXISTS build_global_slots(
          job_id TEXT PRIMARY KEY REFERENCES build_jobs(id) ON DELETE CASCADE,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          owner_pid INTEGER NOT NULL,owner_token TEXT NOT NULL,acquired_at TEXT NOT NULL,
          cleanup_pending INTEGER NOT NULL DEFAULT 0);
        INSERT OR IGNORE INTO build_global_slots(job_id,project_id,owner_pid,owner_token,acquired_at)
          SELECT id,project_id,owner_pid,owner_token,COALESCE(started_at,created_at)
          FROM build_jobs WHERE status='running';
    ''')
    slot_columns={row['name'] for row in conn.execute('PRAGMA table_info(build_global_slots)')}
    if 'cleanup_pending' not in slot_columns:
        conn.execute('ALTER TABLE build_global_slots ADD COLUMN cleanup_pending INTEGER NOT NULL DEFAULT 0')
    event_columns={row['name'] for row in conn.execute('PRAGMA table_info(project_build_profile_events)')}
    if 'base_revision' not in event_columns:
        conn.execute('ALTER TABLE project_build_profile_events ADD COLUMN base_revision INTEGER NOT NULL DEFAULT -1')
    if 'result_json' not in event_columns:
        conn.execute("ALTER TABLE project_build_profile_events ADD COLUMN result_json TEXT NOT NULL DEFAULT '{}'")


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def replay(conn, project_id, request_id, request_fingerprint):
    from paperpilot.services import build_snapshots
    alias = conn.execute('SELECT * FROM build_request_aliases WHERE project_id=? AND request_id=?', (project_id, request_id)).fetchone()
    if alias is None:
        return None
    if alias['request_fingerprint'] != request_fingerprint:
        raise ProjectServiceError(409, 'build_request_conflict', '此请求编号已用于不同快照、用途或触发参数；请核对原任务后以新编号提交，不会覆盖原任务。')
    return build_snapshots.get_job(conn, project_id, alias['job_id']) | {'idempotent_replay': True, 'alias_request_id': request_id}


def reusable(row, conn, snapshot, asset_storage_root):
    if row['status'] != 'succeeded':
        return True
    try:
        from paperpilot.services import visual_assets
        for asset in snapshot['manifest'].get('assets', []):
            if asset_storage_root is None:
                return False
            visual_assets.checked_file(conn, asset_storage_root, snapshot['project_id'], asset['version_id'])
        with Path(row['artifact_path']).open('rb') as stream:
            return hashlib.file_digest(stream, 'sha256').hexdigest() == row['artifact_sha256']
    except (OSError, ProjectServiceError):
        return False


def enqueue(conn, project_id, snapshot_id, request_id, purpose, *, trigger='explicit', force_rebuild=False, max_active=8, checks=None, request_fingerprint=None, asset_storage_root=None):
    from paperpilot.services import build_snapshots, tex_runtime
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    if trigger not in {'explicit', 'automatic'} or (trigger == 'automatic' and purpose != 'draft_preview'):
        raise ProjectServiceError(422, 'invalid_build_trigger', '只有草稿预览可以自动合并；候选预检和提交/发布构建必须明确发起。')
    if force_rebuild and trigger != 'explicit':
        raise ProjectServiceError(422, 'invalid_build_force', '自动预览不能强制重建；请在构建历史明确选择重新编译。')
    request_fingerprint = request_fingerprint or fingerprint({'snapshot_id': snapshot_id, 'purpose': purpose, 'trigger': trigger, **({'force_rebuild': True} if force_rebuild else {})})
    previous = replay(conn, project_id, request_id, request_fingerprint)
    if previous:
        return previous
    snapshot = build_snapshots.get(conn, project_id, snapshot_id)
    if snapshot['purpose'] != purpose:
        raise ProjectServiceError(409, 'build_purpose_mismatch', '快照用途不能改写；请选取对应用途的快照。')
    legacy = conn.execute('SELECT * FROM build_jobs WHERE project_id=? AND request_id=?', (project_id, request_id)).fetchone()
    if legacy:
        if legacy['snapshot_id'] != snapshot_id or legacy['purpose'] != purpose or force_rebuild:
            raise ProjectServiceError(409, 'build_request_conflict', '已有同编号任务使用另一快照或用途；请以新编号提交。')
        found = legacy
    else:
        found = next((row for row in conn.execute("SELECT * FROM build_jobs WHERE project_id=? AND snapshot_identity=? AND purpose=? AND status IN ('queued','running','succeeded') ORDER BY queue_seq DESC",
            (project_id, snapshot['manifest']['identity'], purpose)) if (not force_rebuild or row['status'] != 'succeeded') and reusable(row, conn, snapshot, asset_storage_root)), None)
    stamp = build_snapshots._now()
    replaced = []
    if trigger == 'automatic':
        replaced = [row['id'] for row in conn.execute("SELECT id FROM build_jobs WHERE project_id=? AND status='queued' AND trigger_kind='automatic' AND snapshot_identity!=?", (project_id, snapshot['manifest']['identity']))]
        for job_id in replaced:
            conn.execute("UPDATE build_jobs SET status='superseded',diagnostics_json=?,finished_at=?,updated_at=? WHERE id=? AND status='queued' AND trigger_kind='automatic'",
                (json.dumps([{'code': 'auto_preview_replaced', 'message': '后续输入已形成更新自动预览；此项未执行，显式任务和运行中任务保留。', 'replacement_snapshot_id': snapshot_id}], ensure_ascii=False), stamp, stamp, job_id))
    seq = conn.execute('SELECT COALESCE(MAX(queue_seq),0)+1 FROM build_jobs WHERE project_id=?', (project_id,)).fetchone()[0]
    if found:
        job_id = found['id']
        if trigger == 'explicit' and found['trigger_kind'] == 'automatic' and found['status'] == 'queued':
            # Explicit FIFO begins when the user explicitly requested this item.
            conn.execute("UPDATE build_jobs SET trigger_kind='explicit',queue_seq=?,updated_at=? WHERE id=? AND status='queued'", (seq, stamp, job_id))
    else:
        try:
            limit = max(1, min(64, int(os.environ.get('PWA_BUILD_QUEUE_LIMIT', str(max_active)))))
        except ValueError as exc:
            raise ProjectServiceError(422, 'invalid_build_queue_limit', '本机构建队列上限配置须为整数；请修复PWA_BUILD_QUEUE_LIMIT后重试。') from exc
        count = conn.execute("SELECT COUNT(*) FROM build_jobs WHERE project_id=? AND status IN ('queued','running')", (project_id,)).fetchone()[0]
        if count >= limit:
            raise ProjectServiceError(429, 'build_queue_full', f'本论文构建队列已满（含运行项最多{limit}项）。请等待或明确取消一项后重试；原任务未被删除。')
        job_id = uuid.uuid4().hex
        owner = tex_runtime.current_owner()
        conn.execute('INSERT INTO build_jobs(id,project_id,snapshot_id,request_id,purpose,status,created_at,updated_at,owner_pid,owner_token,trigger_kind,queue_seq,snapshot_identity,checks_json,request_fingerprint) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (job_id, project_id, snapshot_id, request_id, purpose, 'queued', stamp, stamp, owner['owner_pid'], owner['owner_token'], trigger, seq, snapshot['manifest']['identity'],
                json.dumps(checks or {}, ensure_ascii=False), request_fingerprint))
    conn.execute('INSERT INTO build_request_aliases VALUES(?,?,?,?,?)', (project_id, request_id, request_fingerprint, job_id, stamp))
    result = build_snapshots.job_payload(conn.execute('SELECT * FROM build_jobs WHERE id=?', (job_id,)).fetchone())
    return result | {'deduplicated': found is not None, 'superseded_job_ids': replaced, 'alias_request_id': request_id}


def claim_next(conn, project_id, storage_root=None):
    from paperpilot.services import tex_runtime, build_snapshots
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    for running in conn.execute("SELECT * FROM build_jobs WHERE project_id=? AND status='running'", (project_id,)).fetchall():
        if tex_runtime.owner_state(dict(running)) != 'dead':
            return None
        conn.execute("UPDATE build_jobs SET status='interrupted',diagnostics_json=?,finished_at=?,updated_at=? WHERE id=? AND status='running'", (
            json.dumps([{'code': 'worker_restart', 'message': '原工作进程已退出，运行资源已先行对账。此任务仅标记中断，不会自动重跑。'}], ensure_ascii=False), build_snapshots._now(), build_snapshots._now(), running['id']))
    project = conn.execute('SELECT archived FROM projects WHERE id=?', (project_id,)).fetchone()
    if not project or project['archived']:
        return None
    queued = conn.execute("SELECT * FROM build_jobs WHERE project_id=? AND status='queued' ORDER BY CASE WHEN trigger_kind='explicit' THEN 0 ELSE 1 END,queue_seq LIMIT 1", (project_id,)).fetchone()
    if queued is None:
        return None
    # A dead service's request is an interruption, never an automatic replay.
    owner_state = tex_runtime.owner_state(dict(queued))
    if owner_state == 'unknown':
        return None
    if owner_state == 'dead':
        conn.execute("UPDATE build_jobs SET status='interrupted',diagnostics_json=?,finished_at=?,updated_at=? WHERE id=? AND status='queued'", (
            json.dumps([{'code': 'worker_restart', 'message': '原工作进程已退出，请核对固定快照后明确重试；不会自动运行。'}], ensure_ascii=False), build_snapshots._now(), build_snapshots._now(), queued['id']))
        return claim_next(conn, project_id, storage_root)
    if conn.execute('SELECT COUNT(*) FROM build_global_slots').fetchone()[0] >= GLOBAL_BUILD_LIMIT:
        return None
    owner = tex_runtime.current_owner()
    from pathlib import Path
    from paperpilot.services import host_budget
    root = Path(storage_root) if storage_root else Path(conn.execute('PRAGMA database_list').fetchone()['file']).parent/'projects'
    snapshot = build_snapshots.get(conn, project_id, queued['snapshot_id'])
    if not host_budget.acquire(root/project_id, queued['id'], snapshot['manifest'].get('build_profile')):
        return None
    stamp = build_snapshots._now()
    conn.execute('INSERT INTO build_global_slots(job_id,project_id,owner_pid,owner_token,acquired_at) VALUES(?,?,?,?,?)',
                 (queued['id'],project_id,owner['owner_pid'],owner['owner_token'],stamp))
    conn.execute("UPDATE build_jobs SET status='running',owner_pid=?,owner_token=?,started_at=?,updated_at=? WHERE id=? AND status='queued'", (owner['owner_pid'], owner['owner_token'], stamp, stamp, queued['id']))
    return dict(conn.execute('SELECT * FROM build_jobs WHERE id=?', (queued['id'],)).fetchone())


def release_slot(conn, project_id, job_id, owner, storage_root=None):
    """Called only after this worker has verified its execution resources are gone."""
    from paperpilot.services import host_budget
    # Resolve the exact host identity; only the already verified local slot owner can release it.
    from pathlib import Path
    root = Path(storage_root) if storage_root else Path(conn.execute('PRAGMA database_list').fetchone()['file']).parent/'projects'
    deleted = conn.execute('DELETE FROM build_global_slots WHERE project_id=? AND job_id=? AND owner_pid=? AND owner_token=?',
                        (project_id,job_id,owner['owner_pid'],owner['owner_token'])).rowcount
    if deleted:
        host_budget.release(root/project_id, job_id, owner)
    return deleted


def recover_dead_slots(conn, storage_root):
    """Never free a dead worker's capacity until its container cleanup is verified."""
    from paperpilot.services import build_snapshots, tex_runtime
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    released, pending, queued_interrupted = [], [], []
    for row in conn.execute('SELECT * FROM build_global_slots').fetchall():
        state=tex_runtime.owner_state(dict(row))
        if state == 'alive' and not row['cleanup_pending']:
            continue
        if state != 'dead' and not row['cleanup_pending']:
            pending.append(row['job_id']);continue
        try:
            cleanup = build_snapshots.recover_project_runtime(conn, row['project_id'], storage_root, job_ids={row['job_id']}, force=True, build_only=True)
        except (ProjectServiceError, OSError, ValueError):
            pending.append(row['job_id']);continue
        if any(not item.get('cleanup_verified') for item in cleanup):
            pending.append(row['job_id']);continue
        stamp=build_snapshots._now()
        conn.execute("UPDATE build_jobs SET status='interrupted',diagnostics_json=?,finished_at=?,updated_at=? WHERE id=? AND project_id=? AND status='running'",
                     (json.dumps([{'code':'worker_restart','message':'原工作进程已退出，资源清理核实后任务标记中断；请检查固定快照并明确重试。'}],ensure_ascii=False),stamp,stamp,row['job_id'],row['project_id']))
        conn.execute('DELETE FROM build_global_slots WHERE job_id=? AND project_id=?', (row['job_id'],row['project_id']))
        from paperpilot.services import host_budget
        host_budget.release(__import__('pathlib').Path(storage_root)/row['project_id'],row['job_id'],dict(row))
        released.append(row['job_id'])
    for row in conn.execute("SELECT * FROM build_jobs WHERE status='queued'").fetchall():
        if tex_runtime.owner_state(dict(row)) != 'dead':
            continue
        stamp=build_snapshots._now()
        updated=conn.execute("UPDATE build_jobs SET status='interrupted',diagnostics_json=?,finished_at=?,updated_at=? WHERE id=? AND project_id=? AND status='queued'",
            (json.dumps([{'code':'worker_restart','message':'排队任务的原工作进程已退出；请核对固定快照后明确重试，系统不会自动重跑。'}],ensure_ascii=False),stamp,stamp,row['id'],row['project_id']))
        if updated.rowcount:
            queued_interrupted.append(row['id'])
    return {'released':released,'pending':pending,'queued_interrupted':queued_interrupted}
