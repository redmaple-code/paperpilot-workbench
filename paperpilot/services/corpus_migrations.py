"""Explicit source association and per-curation migration; original bindings never drift."""
import hashlib
import json
import uuid

from paperpilot.services import corpus_lifecycle as corpus
from paperpilot.services.project_errors import ProjectServiceError


def initialize_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS corpus_curation_migrations(
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, corpus_id TEXT NOT NULL,
        curation_id TEXT NOT NULL, from_source_version INTEGER NOT NULL,
        target_source_version INTEGER NOT NULL, proposed_content TEXT NOT NULL,
        notes TEXT NOT NULL, status TEXT NOT NULL, fingerprint TEXT NOT NULL,
        request_id TEXT NOT NULL, decision_request_id TEXT, decision_fingerprint TEXT,
        result_curation_id TEXT, created_at TEXT NOT NULL, decided_at TEXT,
        UNIQUE(project_id,request_id))''')
    conn.execute('''CREATE TABLE IF NOT EXISTS corpus_source_links(
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, corpus_id TEXT NOT NULL,
        source_version INTEGER NOT NULL, reference_id TEXT NOT NULL,
        reference_version INTEGER NOT NULL, cite_key TEXT NOT NULL,
        request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, created_at TEXT NOT NULL,
        UNIQUE(project_id,request_id), UNIQUE(corpus_id,source_version,reference_id,reference_version))''')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def authorized(conn, corpus_id, source_version):
    row = conn.execute('SELECT * FROM corpus_source_versions WHERE corpus_id=? AND version=?', (corpus_id, source_version)).fetchone()
    if not row or row['permission_state'] != 'authorized':
        raise ProjectServiceError(409, 'corpus_source_unavailable', '原文来源版本缺失或授权不可用；保留历史绑定，不能迁移或回传受限内容。')
    return row


def create(conn, project_id, corpus_id, payload):
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    corpus._corpus(conn, project_id, corpus_id)
    fp = digest({'corpus_id': corpus_id, **payload})
    previous = conn.execute('SELECT * FROM corpus_curation_migrations WHERE project_id=? AND request_id=?', (project_id, payload['request_id'])).fetchone()
    if previous:
        if previous['fingerprint'] != fp:
            raise ProjectServiceError(409, 'idempotency_key_reused', '请求编号已用于不同的校正迁移。')
        return public(conn, previous) | {'idempotent_replay': True}
    old = conn.execute("SELECT * FROM corpus_curation_versions WHERE id=? AND corpus_id=? AND status='active'", (payload['curation_id'], corpus_id)).fetchone()
    if not old:
        raise ProjectServiceError(404, 'curation_missing', '人工校正版不存在。')
    current = corpus._current_source(conn, corpus_id)
    target = payload['target_source_version']
    if target != current['version'] or target <= old['source_version']:
        raise ProjectServiceError(409, 'migration_target_conflict', '请选择比旧绑定更新的当前原文版本，先刷新再预览。')
    authorized(conn, corpus_id, old['source_version'])
    authorized(conn, corpus_id, target)
    mid = uuid.uuid4().hex
    conn.execute('''INSERT INTO corpus_curation_migrations
        (id,project_id,corpus_id,curation_id,from_source_version,target_source_version,proposed_content,notes,status,fingerprint,request_id,created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''', (mid, project_id, corpus_id, old['id'], old['source_version'], target,
        payload['content'], payload.get('notes',''), 'pending', fp, payload['request_id'], corpus._now()))
    return public(conn, conn.execute('SELECT * FROM corpus_curation_migrations WHERE id=?', (mid,)).fetchone())


def public(conn, row):
    item = dict(row)
    item['stale'] = corpus._current_source(conn, item['corpus_id'])['version'] != item['target_source_version']
    try:
        authorized(conn, item['corpus_id'], item['from_source_version'])
        authorized(conn, item['corpus_id'], item['target_source_version'])
        item['permission_available'] = True
    except ProjectServiceError:
        item['proposed_content'] = ''
        item['notes'] = ''
        item['permission_available'] = False
    return item


def decide(conn, project_id, corpus_id, migration_id, payload):
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    row = conn.execute('SELECT * FROM corpus_curation_migrations WHERE id=? AND corpus_id=? AND project_id=?', (migration_id, corpus_id, project_id)).fetchone()
    if not row:
        raise ProjectServiceError(404, 'migration_missing', '迁移候选不属于当前论文或语料。')
    fp = digest(payload)
    if row['status'] != 'pending':
        if row['decision_fingerprint'] != fp:
            raise ProjectServiceError(409, 'migration_already_decided', '迁移候选已有其他决定，请查看原结果。')
        return public(conn, row) | {'idempotent_replay': True}
    result_id = None
    if payload['decision'] == 'accept':
        authorized(conn, corpus_id, row['from_source_version'])
        authorized(conn, corpus_id, row['target_source_version'])
        if corpus._current_source(conn, corpus_id)['version'] != row['target_source_version']:
            raise ProjectServiceError(409, 'migration_stale', '原文又有新版本；旧候选保留，需基于当前版本重新预览。')
        old = conn.execute("SELECT * FROM corpus_curation_versions WHERE id=? AND corpus_id=? AND status='active'", (row['curation_id'], corpus_id)).fetchone()
        if not old or old['source_version'] != row['from_source_version']:
            raise ProjectServiceError(409, 'curation_changed', '旧校正版身份已变化，未迁移。')
        result = corpus.create_curation(conn, project_id, corpus_id, row['target_source_version'], row['proposed_content'], row['notes'], old['kind'])
        result_id = result['id']
    conn.execute('''UPDATE corpus_curation_migrations SET status=?,decision_request_id=?,decision_fingerprint=?,result_curation_id=?,decided_at=? WHERE id=?''',
        ('accepted' if payload['decision']=='accept' else 'rejected', payload['request_id'], fp, result_id, corpus._now(), migration_id))
    return public(conn, conn.execute('SELECT * FROM corpus_curation_migrations WHERE id=?', (migration_id,)).fetchone())


def reference_candidates(conn, project_id, corpus_id):
    resource = corpus._corpus(conn, project_id, corpus_id)
    rows = [dict(r) for r in conn.execute('SELECT reference_id AS id,reference_version AS version,cite_key,citation_snapshot_json FROM project_reference_links WHERE project_id=? AND link_status IN (\'linked\',\'update_available\')', (project_id,))]
    for row in rows:
        meta = json.loads(row.pop('citation_snapshot_json') or '{}')
        row['title'] = meta.get('title', '')
    rows.extend(dict(r) for r in conn.execute('SELECT id,1 AS version,cite_key,title FROM refs WHERE project_id=?', (project_id,)))
    for row in rows:
        exact = bool(row['title'] and row['title'].strip().casefold() == resource['title'].strip().casefold())
        row['match_reason'] = '题名一致，仍需确认来源身份' if exact else '当前论文的参考文献，需人工确认来源身份'
        row['exact_title'] = exact
    return sorted(rows, key=lambda r: (not r['exact_title'], r['title'], r['id']))


def link(conn, project_id, corpus_id, payload):
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    resource = corpus._corpus(conn, project_id, corpus_id)
    fp = digest({'corpus_id': corpus_id, **payload})
    previous = conn.execute('SELECT * FROM corpus_source_links WHERE project_id=? AND request_id=?', (project_id, payload['request_id'])).fetchone()
    if previous:
        if previous['fingerprint'] != fp:
            raise ProjectServiceError(409, 'idempotency_key_reused', '请求编号已用于不同的来源关联。')
        return dict(previous) | {'idempotent_replay': True}
    source = corpus._current_source(conn, corpus_id)
    if source['version'] != payload['source_version']:
        raise ProjectServiceError(409, 'source_version_conflict', '原文已更新，请重新核对关联。')
    authorized(conn, corpus_id, source['version'])
    matches = [r for r in reference_candidates(conn, project_id, corpus_id) if r['id']==payload['reference_id'] and r['version']==payload['reference_version']]
    if not matches:
        raise ProjectServiceError(409, 'reference_scope_conflict', '文献不在当前论文引用快照中，或版本已变化；没有建立关联。')
    matching = conn.execute('SELECT * FROM corpus_source_links WHERE corpus_id=? AND source_version=? AND reference_id=? AND reference_version=?', (corpus_id, source['version'], payload['reference_id'], payload['reference_version'])).fetchone()
    if matching:
        return dict(matching) | {'idempotent_replay': True}
    lid = uuid.uuid4().hex
    conn.execute('INSERT INTO corpus_source_links VALUES(?,?,?,?,?,?,?,?,?,?)', (lid, project_id, corpus_id, source['version'], payload['reference_id'], payload['reference_version'], matches[0]['cite_key'], payload['request_id'], fp, corpus._now()))
    return dict(conn.execute('SELECT * FROM corpus_source_links WHERE id=?', (lid,)).fetchone())


def augment_lineage(conn, project_id, corpus_id, result):
    current = result['current_source_version']
    sources = {r['version']: r for r in result['source_versions']}
    for row in result['source_versions']:
        if row['permission_state'] != 'authorized': row['content'] = ''
    if current and current['permission_state'] != 'authorized':
        result['resource']['content'] = ''
    for row in result['refresh_candidates']:
        if row['permission_state'] != 'authorized' or (current and current['permission_state'] != 'authorized'):
            row['proposed_content'] = ''
    for row in result['curation_versions']:
        row['source_stale'] = row['source_version'] != current['version']
        row['permission_available'] = sources.get(row['source_version'], {}).get('permission_state') == 'authorized'
        if not row['permission_available']: row['content'] = ''; row['notes'] = ''
    result['migration_candidates'] = [public(conn, r) for r in conn.execute('SELECT * FROM corpus_curation_migrations WHERE project_id=? AND corpus_id=? ORDER BY created_at DESC', (project_id, corpus_id))]
    result['source_links'] = [dict(r) for r in conn.execute('SELECT * FROM corpus_source_links WHERE project_id=? AND corpus_id=? ORDER BY created_at', (project_id, corpus_id))]
    result['reference_candidates'] = reference_candidates(conn, project_id, corpus_id)
    return result
