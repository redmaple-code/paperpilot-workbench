"""Versioned shared-resource editing and deterministic, explicitly refreshed suggestions."""
import hashlib
import json
import re
import uuid

from paperpilot.services import domain_workspace
from paperpilot.services.project_errors import ProjectServiceError


def initialize_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS domain_resource_versions(
        resource_id TEXT NOT NULL, domain_id TEXT NOT NULL, version INTEGER NOT NULL,
        title TEXT NOT NULL, content TEXT NOT NULL, request_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
        PRIMARY KEY(resource_id,version), UNIQUE(domain_id,request_id))''')
    conn.execute('''INSERT OR IGNORE INTO domain_resource_versions
        SELECT id,domain_id,version,title,content,'legacy-'||id,'',updated_at FROM domain_resources''')


def get(conn, domain_id, resource_id):
    row = conn.execute('SELECT * FROM domain_resources WHERE id=? AND domain_id=?', (resource_id, domain_id)).fetchone()
    if not row:
        raise ProjectServiceError(404, 'domain_resource_missing', '该领域中没有此共享资料。')
    return dict(row)


def history(conn, domain_id, resource_id):
    get(conn, domain_id, resource_id)
    return [dict(r) for r in conn.execute('SELECT * FROM domain_resource_versions WHERE resource_id=? AND domain_id=? ORDER BY version DESC', (resource_id, domain_id))]


def edit(conn, domain_id, resource_id, payload):
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    original = get(conn, domain_id, resource_id)
    fp = hashlib.sha256(json.dumps({'resource_id': resource_id, **payload}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    replay = conn.execute('SELECT * FROM domain_resource_versions WHERE domain_id=? AND request_id=?', (domain_id, payload['request_id'])).fetchone()
    if replay:
        if replay['fingerprint'] != fp:
            raise ProjectServiceError(409, 'idempotency_key_reused', '此请求编号已用于不同的共享资料修改。')
        return dict(replay) | {'idempotent_replay': True}
    if original['version'] != payload['expected_version']:
        raise ProjectServiceError(409, 'domain_resource_conflict', '共享资料已更新；请保留草稿，刷新后比较或合并再提交。')
    # Term consumers keep their own immutable domain snapshot. Never edit it in place.
    if original['resource_type'] == 'term':
        version = domain_workspace._current_domain_version(conn, domain_id)
        terms = conn.execute("SELECT * FROM domain_terms WHERE domain_id=? AND version=? AND status='active'", (domain_id, version)).fetchall()
        matched = [r for r in terms if domain_workspace._term_key(r['term']) == domain_workspace._term_key(original['title'])]
        if len(matched) != 1 or matched[0]['definition'] != original['content']:
            raise ProjectServiceError(409, 'domain_term_binding_conflict', '共享条目与当前领域术语已不同，请先核对术语版本，未覆盖任何值。')
        if any(domain_workspace._term_key(r['term']) == domain_workspace._term_key(payload['title']) and r['id'] != matched[0]['id'] for r in terms):
            raise ProjectServiceError(409, 'domain_term_conflict', '目标名称与另一领域术语冲突，未覆盖。')
        stamp = domain_workspace._now()
        conn.execute("UPDATE domain_versions SET status='archived' WHERE domain_id=? AND version=?", (domain_id, version))
        conn.execute("INSERT INTO domain_versions VALUES(?,?,?,?,?)", (domain_id, version+1, 'active', payload['request_id'], stamp))
        for row in terms:
            replacement = row['id'] == matched[0]['id']
            conn.execute('INSERT INTO domain_terms(id,domain_id,version,term,definition,aliases,status,source_project_id,source_term_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (uuid.uuid4().hex, domain_id, version+1, payload['title'] if replacement else row['term'],
                 payload['content'] if replacement else row['definition'], row['aliases'], 'active', row['source_project_id'], row['source_term_id'], stamp))
        conn.execute('UPDATE domains SET version=?,updated_at=? WHERE id=?', (version+1, stamp, domain_id))
    return _record_revision(conn, original, payload['title'], payload['content'], payload['request_id'], fp)


def _record_revision(conn, original, title, content, request_id, fingerprint):
    resource_id, domain_id = original['id'], original['domain_id']
    now = domain_workspace._now()
    version = original['version'] + 1
    # Snapshot the previous value even for a resource created after startup.
    conn.execute('INSERT OR IGNORE INTO domain_resource_versions VALUES(?,?,?,?,?,?,?,?)',
                 (resource_id, domain_id, original['version'], original['title'], original['content'], 'initial-'+resource_id, '', original['updated_at']))
    conn.execute('INSERT INTO domain_resource_versions VALUES(?,?,?,?,?,?,?,?)',
                 (resource_id, domain_id, version, title, content, request_id, fingerprint, now))
    conn.execute('UPDATE domain_resources SET title=?,content=?,version=?,updated_at=? WHERE id=? AND domain_id=?',
                 (title, content, version, now, resource_id, domain_id))
    return get(conn, domain_id, resource_id) | {'idempotent_replay': False}


def sync_term_resource(conn, domain_id, original, changes):
    if (original['term'], original['definition']) == (changes['term'], changes['definition']):
        return
    expected = domain_workspace._term_key(original['term'])
    for resource in domain_workspace.list_domain_resources(conn, domain_id):
        if resource['resource_type'] == 'term' and domain_workspace._term_key(resource['title']) == expected:
            if resource['content'] == original['definition']:
                _record_revision(conn, resource, changes['term'], changes['definition'], 'term-edit-' + uuid.uuid4().hex, '')


def recommendations(conn, project_id):
    project = conn.execute('SELECT domain_id FROM projects WHERE id=?', (project_id,)).fetchone()
    if not project:
        raise ProjectServiceError(404, 'project_missing', '论文不存在。')
    query = ' '.join([r['term'] for r in conn.execute("SELECT term FROM terms WHERE project_id=? AND status='active'", (project_id,))])
    query += ' ' + ' '.join(r['title'] for r in conn.execute('SELECT title FROM refs WHERE project_id=?', (project_id,)))
    query += ' ' + ' '.join(r['content'][:100000] for r in conn.execute('SELECT content FROM files WHERE project_id=? ORDER BY path', (project_id,)))
    words = {t.casefold() for t in re.findall(r'[A-Za-z][A-Za-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}', query)}
    result = []
    for row in domain_workspace.list_domain_resources(conn, project['domain_id']):
        text = (row['title']+' '+row['content']).casefold()
        matched = sorted(t for t in words if t in text)
        if matched:
            result.append({**row, 'score': len(matched), 'match_reasons': ['论文术语/正文/引用命中：'+t for t in matched[:10]]})
    result.sort(key=lambda r: (-r['score'], r['title'], r['id']))
    return {'domain_id': project['domain_id'], 'rule_version': 'domain-keywords-v1',
            'results': result[:20], 'omitted': max(0, len(result)-20), 'automatic_write': False}
