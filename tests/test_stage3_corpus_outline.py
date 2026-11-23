import json
import uuid

import app
from tests.test_stage3_resources import workspace


def corpus_setup(client, pid):
    resource = client.post(f'/api/projects/{pid}/corpus', json={'title': '来源论文', 'content': 'v1原始片段'}).json()
    url = f"/api/projects/{pid}/corpus/{resource['id']}"
    corrected = client.post(url+'/curations', json={'base_source_version': 1, 'content': 'C1人工校正', 'notes': '保留限定条件', 'kind': 'manual'}).json()
    refresh = client.post(url+'/refresh', json={'request_id': uuid.uuid4().hex, 'base_source_version': 1, 'content': 'v2原始片段', 'permission_state': 'authorized'}).json()
    accepted = client.post(url+f"/refresh/{refresh['id']}/decision", json={'decision': 'accept', 'request_id': uuid.uuid4().hex})
    assert accepted.status_code == 200, accepted.text
    return resource, corrected, url


def test_curation_migration_creates_new_binding_only_on_explicit_accept(client):
    _, pid = workspace(client)
    resource, old, url = corpus_setup(client, pid)
    before = client.get(url+'/lineage').json()
    assert before['curation_versions'][0]['source_version'] == 1
    assert before['curation_versions'][0]['source_stale']
    body = {'request_id': uuid.uuid4().hex, 'curation_id': old['id'], 'target_source_version': 2, 'content': 'C2核对新原文后的校正'}
    preview = client.post(url+'/migrations', json=body)
    assert preview.status_code == 200, preview.text
    assert len(client.get(url+'/lineage').json()['curation_versions']) == 1
    decision = {'request_id': uuid.uuid4().hex, 'decision': 'accept'}
    result = client.post(url+f"/migrations/{preview.json()['id']}/decision", json=decision)
    assert result.status_code == 200, result.text
    assert result.json()['status'] == 'accepted'
    assert client.post(url+f"/migrations/{preview.json()['id']}/decision", json=decision).json()['idempotent_replay']
    versions = client.get(url+'/lineage').json()['curation_versions']
    assert [(v['source_version'], v['content']) for v in versions] == [(2, 'C2核对新原文后的校正'), (1, 'C1人工校正')]


def test_migration_staleness_permission_and_rejection_do_not_drift_old_curation(client):
    _, pid = workspace(client)
    resource, old, url = corpus_setup(client, pid)
    body = {'request_id': uuid.uuid4().hex, 'curation_id': old['id'], 'target_source_version': 2, 'content': '待迁移校正'}
    preview = client.post(url+'/migrations', json=body).json()
    conn = app.db()
    with conn:
        conn.execute("UPDATE corpus_source_versions SET permission_state='revoked' WHERE corpus_id=? AND version=1", (resource['id'],))
    conn.close()
    lineage = client.get(url+'/lineage').json()
    assert lineage['curation_versions'][0]['content'] == ''
    assert lineage['migration_candidates'][0]['proposed_content'] == ''
    assert client.post(url+f"/migrations/{preview['id']}/decision", json={'request_id': uuid.uuid4().hex, 'decision': 'accept'}).status_code == 409
    rejected = client.post(url+f"/migrations/{preview['id']}/decision", json={'request_id': uuid.uuid4().hex, 'decision': 'reject'})
    assert rejected.status_code == 200 and rejected.json()['status'] == 'rejected'
    assert len(client.get(url+'/lineage').json()['curation_versions']) == 1


def test_source_association_needs_current_project_reference_and_explicit_confirmation(client):
    _, pid = workspace(client)
    resource, _, url = corpus_setup(client, pid)
    reference = client.post(f'/api/projects/{pid}/refs', json={'cite_key': 'source', 'title': '来源论文'}).json()
    lineage = client.get(url+'/lineage').json()
    assert lineage['source_links'] == [] and lineage['reference_candidates'][0]['exact_title']
    body = {'request_id': uuid.uuid4().hex, 'source_version': 2, 'reference_id': reference['id'], 'reference_version': 1}
    linked = client.post(url+'/source-links', json=body)
    assert linked.status_code == 200, linked.text
    assert client.post(url+'/source-links', json=body).json()['idempotent_replay']
    _, foreign = workspace(client)
    wrong = client.post(f'/api/projects/{foreign}/refs', json={'cite_key': 'other', 'title': '另一论文'}).json()
    assert client.post(url+'/source-links', json={**body, 'request_id': uuid.uuid4().hex, 'reference_id': wrong['id']}).status_code == 409


def test_structured_ai_candidate_preview_partial_apply_and_empty_selection(client, monkeypatch):
    _, pid = workspace(client)
    initial = client.post(f'/api/projects/{pid}/outline/nodes', json={'request_id': uuid.uuid4().hex, 'base_version': 0, 'command': 'create', 'node': {'id': 'intro', 'title': '原引言'}})
    assert initial.status_code == 200, initial.text
    calls = []
    operations = [{'op': 'update_node', 'id': 'intro', 'changes': {'title': '改进引言'}}, {'op': 'add_node', 'node': {'id': 'methods', 'title': '方法'}}]
    monkeypatch.setattr(app, 'call_ai', lambda *a, **kw: calls.append(a) or {'ok': True, 'content': json.dumps({'operations': operations})})
    body = {'request_id': uuid.uuid4().hex, 'instruction': '改进引言并补方法章节'}
    preview = client.post(f'/api/projects/{pid}/outline/suggestion-preview', json=body).json()
    submitted = {**body, 'consent_to_send': True, 'preview_fingerprint': preview['preview_fingerprint']}
    candidate = client.post(f'/api/projects/{pid}/outline/suggestions', json=submitted)
    assert candidate.status_code == 200, candidate.text
    assert client.post(f'/api/projects/{pid}/outline/suggestions', json=submitted).json()['idempotent_replay']
    assert len(calls) == 1
    url = f"/api/projects/{pid}/outline/candidates/{candidate.json()['id']}/decision"
    decision = {'request_id': uuid.uuid4().hex, 'decision': 'partial_apply', 'base_version': 1, 'selected_indices': []}
    assert client.post(url, json=decision).status_code == 422
    accepted = client.post(url, json={**decision, 'request_id': uuid.uuid4().hex, 'selected_indices': [0]})
    assert accepted.status_code == 200, accepted.text
    assert [n['title'] for n in client.get(f'/api/projects/{pid}/outline/structured').json()['nodes']] == ['改进引言']
