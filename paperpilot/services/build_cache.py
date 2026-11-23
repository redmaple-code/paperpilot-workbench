"""Reference-aware retention of disposable build artifacts."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import shutil

from paperpilot.services.project_errors import ProjectServiceError


def initialize_schema(conn):
    columns = {row['name'] for row in conn.execute('PRAGMA table_info(build_jobs)')}
    if 'cache_pruned' not in columns:
        conn.execute('ALTER TABLE build_jobs ADD COLUMN cache_pruned INTEGER NOT NULL DEFAULT 0')
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS build_read_leases (
          project_id TEXT NOT NULL, job_id TEXT NOT NULL, lease_id TEXT NOT NULL,
          expires_at INTEGER NOT NULL,
          PRIMARY KEY(project_id,job_id,lease_id),
          FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
          FOREIGN KEY(job_id) REFERENCES build_jobs(id) ON DELETE CASCADE
        );
        CREATE INDEX IF NOT EXISTS idx_build_read_lease_expiry ON build_read_leases(expires_at);
    ''')


PERSISTENT_EXPIRY = 9223372036854775807


def hold(conn, project_id, job_id, lease_id, *, persistent=False):
    """Acquire/renew under the same write lock a future cleanup must use."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', lease_id):
        raise ProjectServiceError(422, 'build_read_lease_invalid', '阅读保护编号无效；请刷新阅读器。')
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    job = conn.execute('SELECT status,artifact_path,artifact_sha256,cache_pruned FROM build_jobs WHERE project_id=? AND id=?',
                       (project_id, job_id)).fetchone()
    if job is None:
        raise ProjectServiceError(404, 'build_job_missing', '此论文内没有该构建。')
    if job['status'] != 'succeeded' or job['cache_pruned'] or not job['artifact_path'] or not job['artifact_sha256']:
        raise ProjectServiceError(409, 'build_artifact_unavailable', '此构建没有可保护的成功PDF；请查看任务或重新编译。')
    # Legacy callers get bounded leases; active readers and streams explicitly
    # use persistent pins, released on close/completion instead of by a clock.
    conn.execute('DELETE FROM build_read_leases WHERE expires_at <= CAST(strftime(\'%s\',\'now\') AS INTEGER)')
    conn.execute('''INSERT INTO build_read_leases(project_id,job_id,lease_id,expires_at)
        VALUES(?,?,?,CASE WHEN ? THEN 9223372036854775807 ELSE CAST(strftime('%s','now') AS INTEGER)+86400 END)
        ON CONFLICT(project_id,job_id,lease_id) DO UPDATE SET expires_at=excluded.expires_at''',
        (project_id, job_id, lease_id, int(persistent)))
    return {'job_id': job_id, 'lease_id': lease_id, 'expires_in_seconds': None if persistent else 86400}


def release(conn, project_id, job_id, lease_id):
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    return conn.execute('DELETE FROM build_read_leases WHERE project_id=? AND job_id=? AND lease_id=?',
                        (project_id, job_id, lease_id)).rowcount


def clear_pins(conn, project_id, job_id):
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    if not conn.execute('SELECT 1 FROM build_jobs WHERE project_id=? AND id=?', (project_id, job_id)).fetchone():
        raise ProjectServiceError(404, 'build_job_missing', '此论文内没有该构建。')
    return conn.execute('DELETE FROM build_read_leases WHERE project_id=? AND job_id=? AND expires_at=?',
                        (project_id, job_id, PERSISTENT_EXPIRY)).rowcount


def inventory(conn, project_id):
    if not conn.execute('SELECT 1 FROM projects WHERE id=?', (project_id,)).fetchone():
        raise ProjectServiceError(404, 'project_missing', '论文不存在。')
    successes = conn.execute(
        "SELECT id,artifact_path,artifact_sha256,promoted,cache_pruned FROM build_jobs "
        "WHERE project_id=? AND status='succeeded' ORDER BY COALESCE(finished_at,created_at) DESC, rowid DESC",
        (project_id,),
    ).fetchall()
    reasons = {row['id']: [] for row in successes}
    for row in successes[:10]:
        reasons[row['id']].append('recent_success')
    # Mirror /pdf selection, including the draft fallback, even when newer
    # candidate previews push that selected build out of the last-ten window.
    current = conn.execute(
        "SELECT id FROM build_jobs WHERE project_id=? AND status='succeeded' "
        "AND (purpose='draft_preview' OR promoted=1) "
        "ORDER BY CASE WHEN purpose IN ('submitted','release') THEN 0 ELSE 1 END,created_at DESC LIMIT 1",
        (project_id,),
    ).fetchone()
    if current and current['id'] in reasons:
        reasons[current['id']].append('current_pdf')
    for row in conn.execute('SELECT DISTINCT job_id FROM build_restore_events WHERE project_id=?', (project_id,)):
        if row['job_id'] in reasons:
            reasons[row['job_id']].append('restore_reference')
    for row in conn.execute("SELECT DISTINCT job_id FROM build_read_leases WHERE project_id=? AND expires_at > CAST(strftime('%s','now') AS INTEGER)", (project_id,)):
        if row['job_id'] in reasons:
            reasons[row['job_id']].append('active_reader')
    unknown = False
    for row in conn.execute('SELECT result_json FROM project_release_checks WHERE project_id=?', (project_id,)):
        try:
            result = json.loads(row['result_json'])
            if not isinstance(result, dict) or not isinstance(result.get('compile'), dict):
                raise ValueError('invalid release check')
            compile_result = result['compile']
            job_id = compile_result.get('build_job_id')
            if job_id is not None and not isinstance(job_id, str):
                raise ValueError('invalid build reference')
            if not job_id and compile_result.get('ok') is not False:
                raise ValueError('unresolved legacy build reference')
            if job_id in reasons and 'release_reference' not in reasons[job_id]:
                reasons[job_id].append('release_reference')
        except (TypeError, ValueError):
            unknown = True
    rows = [{'job_id': row['id'], 'protected_by': reasons[row['id']], 'cache_pruned': bool(row['cache_pruned']),
             'artifact_recorded': bool(row['artifact_path'] and row['artifact_sha256'])} for row in successes]
    failures = conn.execute("SELECT id,cache_pruned FROM build_jobs WHERE project_id=? AND status IN ('failed','cancelled','interrupted') ORDER BY COALESCE(finished_at,created_at) DESC,rowid DESC",(project_id,)).fetchall()
    failure_rows=[]
    for n,row in enumerate(failures):
        protected=[]
        if n<3:protected.append('recent_failure_diagnostics')
        if conn.execute('SELECT 1 FROM build_global_slots WHERE project_id=? AND job_id=?',(project_id,row['id'])).fetchone():protected.append('runtime_slot')
        if conn.execute("SELECT 1 FROM build_read_leases WHERE project_id=? AND job_id=? AND expires_at>CAST(strftime('%s','now') AS INTEGER)",(project_id,row['id'])).fetchone():protected.append('active_reader')
        if conn.execute('SELECT 1 FROM build_restore_events WHERE project_id=? AND job_id=?',(project_id,row['id'])).fetchone():protected.append('restore_reference')
        failure_rows.append({'job_id':row['id'],'protected_by':protected,'cache_pruned':bool(row['cache_pruned'])})
    return {'recent_limit': 10, 'success_count': len(rows), 'items': rows,
            'failed_recent_limit':3,'failed_items':failure_rows,
            'failed_cleanup_candidates':[r['job_id'] for r in failure_rows if not r['protected_by'] and not r['cache_pruned']],
            'persistent_pin_counts': {row['job_id']: row['n'] for row in conn.execute('SELECT job_id,COUNT(*) AS n FROM build_read_leases WHERE project_id=? AND expires_at=? GROUP BY job_id', (project_id, PERSISTENT_EXPIRY))},
            'potential_old_unreferenced': [r['job_id'] for r in rows if not r['protected_by'] and not r['cache_pruned']],
            'unknown_release_references': unknown,
            'deletion_enabled': True,
            'message': '成功保留最近10次，失败/取消/中断目录保留最近3次诊断；无运行或记录引用的旧目录可清理，源码快照、任务、诊断及发布/恢复记录保留。'}


def _unsafe_link(path):
    """Reject symlinks and Windows junctions/reparse points."""
    stat = path.lstat()
    return path.is_symlink() or bool(getattr(stat, 'st_file_attributes', 0) & 0x400)


def _locations(storage_root, project_id, job_id):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', project_id) or not re.fullmatch(r'[0-9a-f]{32}', job_id):
        raise ValueError('unsafe identity')
    root = Path(storage_root).resolve()
    project, runs = root / project_id, root / project_id / 'build-runs'
    if not project.is_dir() or not runs.is_dir() or _unsafe_link(project) or _unsafe_link(runs):
        raise ValueError('unsafe build parent')
    if project.resolve().parent != root or runs.resolve().parent != project.resolve():
        raise ValueError('build directory escapes project')
    return runs / job_id, runs / ('.prune-' + job_id)


def _check_tree(path):
    if _unsafe_link(path) or not path.is_dir():
        raise ValueError('unsafe build tree')
    for base, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            if _unsafe_link(Path(base) / name):
                raise ValueError('link in build tree')


def recover_staged(conn, storage_root, project_id):
    """Recover only deterministic, database-owned quarantine directories."""
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    restored, removed, pending = [], [], []
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,120}', project_id):
        return {'restored': restored, 'removed': removed, 'pending': ['invalid_project_identity']}
    runs = Path(storage_root).resolve() / project_id / 'build-runs'
    for row in conn.execute("SELECT id,status,cache_pruned,artifact_path FROM build_jobs WHERE project_id=? AND status IN ('succeeded','failed','cancelled','interrupted')", (project_id,)):
        if not re.fullmatch(r'[0-9a-f]{32}', row['id']) or not (runs / ('.prune-' + row['id'])).exists():
            continue
        try:
            run, staged = _locations(storage_root, project_id, row['id'])
            if not staged.exists():
                continue
            if row['status']=='succeeded' and Path(row['artifact_path']).resolve() != (run / 'main.pdf').resolve():
                raise ValueError('artifact identity mismatch')
            _check_tree(staged)
            if row['cache_pruned']:
                shutil.rmtree(staged)
                removed.append(row['id'])
            else:
                if run.exists():
                    raise ValueError('both copies exist')
                staged.rename(run)
                restored.append(row['id'])
        except (OSError, ValueError):
            pending.append(row['id'])
    return {'restored': restored, 'removed': removed, 'pending': pending}


def prune_one(conn, storage_root, project_id, job_id):
    """Stage one eligible run while holding the reference write lock.

    Each call owns its transaction. A later recovery restores an uncommitted
    rename, or finishes deletion after a committed tombstone.
    """
    if conn.in_transaction:
        raise ValueError('cleanup requires its own transaction')
    staged = run = None
    try:
        conn.execute('BEGIN IMMEDIATE')
        plan = inventory(conn, project_id)
        if plan['unknown_release_references'] or job_id not in plan['potential_old_unreferenced']+plan['failed_cleanup_candidates']:
            conn.rollback()
            return 'protected'
        row = conn.execute('SELECT status,artifact_path FROM build_jobs WHERE id=? AND project_id=?', (job_id, project_id)).fetchone()
        run, staged = _locations(storage_root, project_id, job_id)
        if staged.exists() or (row['status']=='succeeded' and (Path(row['artifact_path']).resolve() != (run / 'main.pdf').resolve() or not (run / 'main.pdf').is_file())):
            raise ValueError('artifact path unavailable or staging conflict')
        from paperpilot.services import tex_runtime, host_budget
        if row['status']!='succeeded':
            runtime=tex_runtime.recover(__import__('pathlib').Path(storage_root)/project_id,job_ids={job_id})
            host=host_budget.connection()
            try:reserved=host.execute('SELECT 1 FROM claims WHERE identity=?',(host_budget.identity(__import__('pathlib').Path(storage_root)/project_id,job_id),)).fetchone()
            finally:host.close()
            if reserved or any(not x.get('cleanup_verified') for x in runtime):
                conn.rollback();return 'protected'
            if not run.exists():
                conn.execute('UPDATE build_jobs SET cache_pruned=1 WHERE id=? AND project_id=?',(job_id,project_id));conn.commit();return 'pruned'
        _check_tree(run)
        run.rename(staged)
        conn.execute('UPDATE build_jobs SET cache_pruned=1 WHERE id=? AND project_id=? AND cache_pruned=0', (job_id, project_id))
        conn.commit()
    except Exception:
        conn.rollback()
        if staged is not None and staged.exists() and run is not None and not run.exists():
            try:
                staged.rename(run)
            except OSError:
                pass  # startup recovery retries the uncommitted rename
        raise
    try:
        _check_tree(staged)
        shutil.rmtree(staged)
        return 'pruned'
    except (OSError, ValueError):
        return 'cleanup_pending'


def prune_project(conn, storage_root, project_id, limit=20):
    """Bound work and recheck references separately for each candidate."""
    if conn.in_transaction:
        raise ValueError('cleanup requires its own transaction')
    with conn:
        recovery = recover_staged(conn, storage_root, project_id)
    plan = inventory(conn, project_id)
    if plan['unknown_release_references'] or recovery['pending']:
        return {'pruned': [], 'pending': recovery['pending'], 'blocked': True, 'recovery': recovery}
    pruned, pending = [], []
    for job_id in (plan['potential_old_unreferenced']+plan['failed_cleanup_candidates'])[:max(1, min(int(limit), 100))]:
        try:
            result = prune_one(conn, storage_root, project_id, job_id)
            if result == 'pruned':
                pruned.append(job_id)
            elif result == 'cleanup_pending':
                pending.append(job_id)
        except (OSError, ValueError):
            pending.append(job_id)
    return {'pruned': pruned, 'pending': pending, 'blocked': False, 'recovery': recovery}
