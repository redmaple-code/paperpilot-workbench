"""Regression coverage for automatic author saves and independent recovery drafts."""
import app


def setup_file(client):
    project = client.post('/api/projects', json={'name': '自动保存验证', 'domain': '计算方法'}).json()
    pid = project['project']['id']
    file = next(item for item in project['files'] if item['path'] == 'main.tex')
    return pid, file


def test_author_save_preserves_separate_revision_draft_without_mode_gate(client):
    pid, file = setup_file(client)
    conn = app.db()
    try:
        with conn:
            conn.execute("INSERT INTO human_revision_modes(id,project_id,status,workflow_version,state,entry_source,created_at,updated_at) VALUES('old-mode',?,'exited',1,'draft','author',?,?)",
                         (pid, '2026-11-09T00:00:00+00:00', '2026-11-09T00:00:00+00:00'))
            conn.execute("INSERT INTO revision_drafts(project_id,target_kind,target_path,content,base_version,mode_id,updated_at) VALUES(?,'file','main.tex',?,?,'old-mode',?)",
                         (pid, '另一份可恢复修订草稿', file['version'], '2026-11-09T00:00:00+00:00'))
        body = {'path': 'main.tex', 'content': file['content'] + '\n% author change',
                'base_version': file['version'], 'request_id': 'automatic-author-save'}
        saved = client.put(f'/api/projects/{pid}/editor/save', json=body)
        assert saved.status_code == 200, saved.text
        assert conn.execute('SELECT content FROM revision_drafts WHERE project_id=?', (pid,)).fetchone()[0] == '另一份可恢复修订草稿'
        assert client.get(f'/api/projects/{pid}').json()['files'][0]['content'] == body['content']
        assert not conn.execute("SELECT 1 FROM human_revision_modes WHERE project_id=? AND status='active'", (pid,)).fetchone()
    finally:
        conn.close()


def test_replayed_save_clears_only_its_matching_retry_draft(client):
    pid, file = setup_file(client)
    body = {'path': 'main.tex', 'content': file['content'] + '\n% saved once',
            'base_version': file['version'], 'request_id': 'lost-response'}
    first = client.put(f'/api/projects/{pid}/editor/save', json=body)
    assert first.status_code == 200
    draft = {'path': 'main.tex', 'base_version': file['version'],
             'base_content': file['content'], 'content': body['content']}
    assert client.put(f'/api/projects/{pid}/editor/draft', json=draft).status_code == 200
    replay = client.put(f'/api/projects/{pid}/editor/save', json=body)
    assert replay.status_code == 200 and replay.json()['version'] == first.json()['version']
    assert client.get(f'/api/projects/{pid}/editor/draft?path=main.tex').json()['exists'] is False
    other = dict(draft, content='更新的输入必须保留')
    assert client.put(f'/api/projects/{pid}/editor/draft', json=other).status_code == 200
    assert client.put(f'/api/projects/{pid}/editor/save', json=body).status_code == 200
    assert client.get(f'/api/projects/{pid}/editor/draft?path=main.tex').json()['content'] == other['content']


def test_regular_author_endpoints_preserve_internal_drafts_and_versions(client):
    pid, file = setup_file(client)
    project = client.get(f'/api/projects/{pid}').json()
    conn = app.db()
    try:
        with conn:
            conn.execute("INSERT INTO human_revision_modes(id,project_id,status,workflow_version,state,entry_source,created_at,updated_at) VALUES('old-mode',?,'exited',1,'draft','author',?,?)",
                         (pid, '2026-11-09T00:00:00+00:00', '2026-11-09T00:00:00+00:00'))
            for kind, path, version in [('file', file['path'], file['version']), ('outline', '', project['outline_version'])]:
                conn.execute("INSERT INTO revision_drafts(project_id,target_kind,target_path,content,base_version,mode_id,updated_at) VALUES(?,?,?,?,?,'old-mode',?)",
                             (pid, kind, path, 'recoverable internal draft', version, '2026-11-09T00:00:00+00:00'))
        for endpoint, body in [('files', {'path': file['path'], 'base_version': file['version'], 'content': file['content'] + '\n% regular author save'}),
                               ('outline', {'base_version': project['outline_version'], 'content': 'Author outline'})]:
            saved = client.put(f'/api/projects/{pid}/{endpoint}', json=body)
            assert saved.status_code == 200, saved.text
            conflict = client.put(f'/api/projects/{pid}/{endpoint}', json={**body, 'content': 'stale concurrent edit'})
            assert conflict.status_code == 409, conflict.text
        assert conn.execute('SELECT COUNT(*) FROM revision_drafts WHERE project_id=?', (pid,)).fetchone()[0] == 2
    finally:
        conn.close()


def test_conversion_apply_preserves_internal_recovery_draft(client):
    pid, file = setup_file(client)
    preview = client.post(f'/api/projects/{pid}/editor/conversions/preview', json={
        'request_id': 'author-conversion', 'source_path': file['path'], 'source_version': file['version'],
        'target_path': 'main.md', 'direction': 'latex_to_markdown'})
    assert preview.status_code == 200, preview.text
    conn = app.db()
    try:
        with conn:
            conn.execute("INSERT INTO human_revision_modes(id,project_id,status,workflow_version,state,entry_source,created_at,updated_at) VALUES('internal-mode',?,'active',1,'draft','author',?,?)",
                         (pid, '2026-11-09T00:00:00+00:00', '2026-11-09T00:00:00+00:00'))
            conn.execute("INSERT INTO revision_drafts(project_id,target_kind,target_path,content,base_version,mode_id,updated_at) VALUES(?,'file','main.md','recoverable draft',0,'internal-mode',?)",
                         (pid, '2026-11-09T00:00:00+00:00'))
        applied = client.post(f"/api/projects/{pid}/editor/conversions/{preview.json()['id']}/apply", json={'request_id': 'author-apply'})
        assert applied.status_code == 200, applied.text
        assert conn.execute("SELECT content FROM revision_drafts WHERE project_id=?", (pid,)).fetchone()[0] == 'recoverable draft'
    finally:
        conn.close()
