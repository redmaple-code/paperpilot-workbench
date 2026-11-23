import json
import uuid

import app


def workspace(client):
    domain = client.post('/api/domains', json={'name': '资源测试-'+uuid.uuid4().hex[:8]}).json()
    project = client.post('/api/projects', json={'name': '资源论文', 'domain_id': domain['id']}).json()
    return domain, project['project']['id']


def term(client, pid, name='实验术语', definition='论文定义'):
    return client.post(f'/api/projects/{pid}/terms', json={'term': name, 'definition': definition}).json()


def test_selected_promotion_conflict_is_explicit_and_preserves_old_domain_snapshot(client):
    domain, pid = workspace(client)
    client.post(f"/api/domains/{domain['id']}/resources", json={'resource_type': 'term', 'title': '实验术语', 'content': '领域原值'})
    incoming = term(client, pid)
    other = term(client, pid, '不要沉淀', '留在论文')
    paper = client.get(f'/api/projects/{pid}').json()
    preview = client.post(f'/api/projects/{pid}/terms/promote/preview', json={
        'domain_id': domain['id'], 'request_id': uuid.uuid4().hex,
        'source_project_revision': paper['project']['project_revision'], 'term_ids': [incoming['id'], other['id']]})
    assert preview.status_code == 200, preview.text
    body = {'preview_id': preview.json()['id'], 'request_id': uuid.uuid4().hex,
            'selected_term_ids': [incoming['id']], 'conflict_decisions': {incoming['id']: 'use_paper'}}
    result = client.post(f'/api/projects/{pid}/terms/promote/commit', json=body)
    assert result.status_code == 200, result.text
    assert result.json()['domain_version'] == 2
    assert [t['definition'] for t in client.get(f"/api/domains/{domain['id']}/terms?version=1").json()] == ['领域原值']
    assert [t['definition'] for t in client.get(f"/api/domains/{domain['id']}/terms").json()] == ['论文定义']
    assert client.post(f'/api/projects/{pid}/terms/promote/commit', json=body).json()['idempotent_replay']
    assert client.post(f'/api/projects/{pid}/terms/promote/commit', json={**body, 'selected_term_ids': []}).status_code == 409


def test_classifier_failure_and_low_confidence_enter_manual_inbox_without_publishing(client, monkeypatch):
    domain, pid = workspace(client)
    value = term(client, pid)
    calls = []
    monkeypatch.setattr(app, 'call_ai', lambda *a, **k: calls.append(a) or {'ok': False, 'error_code': 'timeout'})
    preview = client.post(f'/api/projects/{pid}/terminology/classification-preview', json={'request_id': uuid.uuid4().hex, 'term_id': value['id']})
    assert preview.status_code == 200, preview.text
    assert preview.json()['generation_allowed']
    body = {'request_id': uuid.uuid4().hex, 'term_id': value['id'], 'preview_fingerprint': preview.json()['preview_fingerprint'], 'consent_to_send': True}
    result = client.post(f'/api/projects/{pid}/terminology/classify', json=body)
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'pending_manual'
    assert 'timeout' in result.json()['classifier_version']
    assert client.get(f"/api/domains/{domain['id']}/terms").json() == []
    assert client.post(f'/api/projects/{pid}/terminology/classify', json=body).json()['idempotent_replay']
    assert len(calls) == 1
    monkeypatch.setattr(app, 'call_ai', lambda *a, **k: {'ok': True, 'content': json.dumps({
        'suggested_term': '实验术语', 'suggested_definition': '低置信建议', 'suggested_aliases': '', 'confidence': .2, 'basis': '信息不足'})})
    second = client.post(f'/api/projects/{pid}/terminology/classify', json={**body, 'request_id': uuid.uuid4().hex})
    assert second.status_code == 200, second.text
    assert second.json()['confidence'] == .2 and second.json()['status'] == 'pending_manual'
    assert client.get(f"/api/domains/{domain['id']}/terms").json() == []


def test_classification_preview_scope_changes_block_before_send_and_manual_mode_never_sends(client, monkeypatch):
    _, pid = workspace(client)
    value = term(client, pid)
    calls = []
    monkeypatch.setattr(app, 'call_ai', lambda *a, **k: calls.append(1))
    preview = client.post(f'/api/projects/{pid}/terminology/classification-preview', json={'request_id': uuid.uuid4().hex, 'term_id': value['id']}).json()
    body = {'request_id': uuid.uuid4().hex, 'term_id': value['id'], 'preview_fingerprint': preview['preview_fingerprint']}
    manual = client.post(f'/api/projects/{pid}/terminology/classify', json=body)
    assert manual.status_code == 200, manual.text
    assert not calls
    term(client, pid, '新增术语')
    assert client.post(f'/api/projects/{pid}/terminology/classify', json={**body, 'request_id': uuid.uuid4().hex, 'consent_to_send': True}).status_code == 409
    assert not calls


def test_shared_resource_versions_are_visible_to_both_papers_and_stale_edits_preserve_new_value(client):
    domain, pid = workspace(client)
    other = client.post('/api/projects', json={'name': '同域论文B', 'domain_id': domain['id']}).json()['project']['id']
    resource = client.post(f"/api/domains/{domain['id']}/resources", json={'resource_type': 'corpus', 'title': '实验术语资料', 'content': 'v1原文'}).json()
    body = {'request_id': uuid.uuid4().hex, 'expected_version': 1, 'title': resource['title'], 'content': 'v2新原文'}
    url = f"/api/domains/{domain['id']}/resources/{resource['id']}"
    saved = client.put(url, json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()['version'] == 2
    assert client.put(url, json=body).json()['idempotent_replay']
    assert client.put(url, json={**body, 'request_id': uuid.uuid4().hex, 'content': '旧稿不许覆盖'}).status_code == 409
    assert [r['content'] for r in client.get(url+'/versions').json()] == ['v2新原文', 'v1原文']
    term(client, other)
    recommended = client.get(f'/api/projects/{other}/domain-recommendations').json()
    assert recommended['results'][0]['version'] == 2
    foreign, _ = workspace(client)
    assert client.get(f"/api/domains/{foreign['id']}/resources/{resource['id']}/versions").status_code == 404


def test_editor_draft_save_as_preserves_current_file_and_rejects_changed_draft(client):
    _, pid = workspace(client)
    workflow = client.get(f'/api/projects/{pid}/workflow').json()
    started = client.post(f'/api/projects/{pid}/workflow/commands', json={'command': 'start', 'request_id': uuid.uuid4().hex, 'base_version': workflow['version']}).json()
    entered = client.post(f'/api/projects/{pid}/human-revision', json={'workflow_version': started['version']})
    assert entered.status_code == 200, entered.text
    file = next(f for f in client.get(f'/api/projects/{pid}').json()['files'] if f['path'] == 'main.tex')
    client.put(f'/api/projects/{pid}/editor/draft', json={'path': 'main.tex', 'base_version': file['version'], 'base_content': file['content'], 'content': '恢复副本内容'})
    draft = client.get(f'/api/projects/{pid}/editor/draft?path=main.tex').json()
    body = {'request_id': uuid.uuid4().hex, 'source_path': 'main.tex', 'target_path': 'recovered.tex', 'draft_updated_at': draft['updated_at']}
    copied = client.post(f'/api/projects/{pid}/editor/draft-copy', json=body)
    assert copied.status_code == 200, copied.text
    files = {f['path']: f['content'] for f in client.get(f'/api/projects/{pid}').json()['files']}
    assert files['main.tex'] == file['content'] and files['recovered.tex'] == '恢复副本内容'
    assert client.post(f'/api/projects/{pid}/editor/draft-copy', json=body).json()['idempotent_replay']
    assert client.post(f'/api/projects/{pid}/editor/draft-copy', json={**body, 'request_id': uuid.uuid4().hex, 'target_path': 'main.tex'}).status_code == 409
