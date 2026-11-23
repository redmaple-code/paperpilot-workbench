"""Specific regressions introduced by the Traceify host integration."""
import copy
import json
import time
import uuid

import app
from paperpilot.services import structure_workspaces as store


def create_workspace(client, mode="paper_import", content="研究问题与假设。\n\n方法及验证计划。"):
    pid = client.post('/api/projects', json={'name': 'Traceify synthetic test'}).json()['project']['id']
    response = client.post(f'/api/projects/{pid}/structure-workspaces', json={
        'request_id': uuid.uuid4().hex, 'entry_mode': mode, 'content': content})
    assert response.status_code == 200, response.text
    return pid, response.json()


def save(client, pid, current, workspace, request_id=None):
    return client.put(f"/api/projects/{pid}/structure-workspaces/{workspace['id']}", json={
        'request_id': request_id or uuid.uuid4().hex, 'expected_workspace_revision': current['version'],
        'source_hash': current['workspace']['source']['content_hash'],
        'commands': [{'op': 'replace_graph', 'workspace': workspace}]})


def generated(client, pid, current, run_id='isolated-model-result'):
    workspace = current['workspace']
    ids = [s['id'] for s in workspace['source']['segments']]
    data = {'request_id': run_id, 'expected_draft_revision': 0, 'result': {
        'nodes': [{'id': 'topic', 'label': '研究问题', 'kind': 'claim', 'summary': '', 'content': '', 'confidence': None, 'segment_ids': ids[:1]},
                  {'id': 'method', 'label': '方法', 'kind': 'section', 'summary': '验证假设', 'content': '', 'confidence': .4, 'segment_ids': ids[-1:]}],
        'edges': [{'id': 'relation', 'from': 'topic', 'to': 'method', 'relation': '拟验证', 'relation_basis': '构思计划，待验证', 'segment_ids': ids, 'confidence': None}],
        'groups': [{'id': 'g-topic', 'label': '研究主线', 'parent_id': None, 'node_ids': ['topic', 'method'], 'color': '#5267d9', 'provenance': 'source_structure', 'basis': '当前来源'}],
        'paper_outline': [{'section_id': 'introduction', 'parent_id': None, 'position': 0, 'title': '引言', 'purpose': '说明问题与贡献', 'node_ids': ['topic']},
                          {'section_id': 'methods', 'parent_id': None, 'position': 1, 'title': '方法', 'purpose': '解释验证方法', 'node_ids': ['method']}],
        'covered_segment_ids': ids, 'complete': True}}
    with app.db() as conn:
        result = store.record_result(conn, pid, workspace['id'], run_id, data)
        replay = store.record_result(conn, pid, workspace['id'], run_id, data)
        assert result == replay
    return result


def test_dual_entries_identity_and_authoritative_paragraphs(client):
    pid, current = create_workspace(client)
    wid = current['workspace']['id']
    assert len(current['workspace']['source']['segments']) == 2
    other = client.post('/api/projects', json={'name': 'Other'}).json()['project']['id']
    assert client.get(f'/api/projects/{other}/structure-workspaces/{wid}').status_code == 404
    response = client.post(f'/api/projects/{pid}/structure-workspaces', json={'request_id': 'idea-1', 'entry_mode': 'idea_from_stage01'})
    assert response.status_code == 200, response.text
    assert response.json()['host']['entry_mode'] == 'idea_from_stage01'


def test_full_result_is_saved_as_draft_with_multistep_evidence(client):
    pid, current = create_workspace(client)
    result = generated(client, pid, current)
    wid = current['workspace']['id']
    draft = client.get(f"/api/projects/{pid}/structure-workspaces/{wid}/drafts/{result['draft_id']}").json()
    assert draft['state'] == 'ready'
    assert len(draft['workspace']['graph']['nodes']) == 2
    assert draft['workspace']['evidence_chains'][0]['status'] == 'complete'
    assert client.get(f'/api/projects/{pid}/structure-workspaces/{wid}').json()['workspace']['graph']['nodes'] == []
    accepted = client.post(f"/api/projects/{pid}/structure-workspaces/{wid}/drafts/{result['draft_id']}/accept", json={
        'request_id': 'accept-1', 'expected_workspace_revision': 1, 'source_hash': current['workspace']['source']['content_hash']})
    assert accepted.status_code == 200, accepted.text
    assert all(n['review_state'] == 'pending' for n in accepted.json()['workspace']['graph']['nodes'])


def test_bad_confidence_and_source_excerpt_are_refused(client):
    pid, current = create_workspace(client)
    value = copy.deepcopy(current['workspace'])
    value['graph']['nodes'] = [{'id': 'n-1', 'kind': 'claim', 'label': '测试', 'confidence': True}]
    assert save(client, pid, current, value).status_code == 422
    value['graph']['nodes'][0]['confidence'] = None
    value['graph']['nodes'][0]['source_refs'] = [{'source_id': value['source']['id'], 'locator': value['source']['segments'][0]['id'], 'excerpt': '伪造文本'}]
    assert save(client, pid, current, value).status_code == 422


def test_layout_is_idempotent_and_does_not_change_semantics(client):
    pid, current = create_workspace(client)
    value = current['workspace']
    value['graph']['nodes'] = [{'id': 'n-1', 'kind': 'concept', 'label': '节点', 'confidence': None}]
    current = save(client, pid, current, value).json()
    path = f"/api/projects/{pid}/structure-workspaces/{value['id']}/layout"
    payload = {'request_id': 'move-1', 'expected_layout_revision': 0, 'positions': {'n-1': {'x': 80, 'y': 100}}}
    first = client.put(path, json=payload)
    assert first.status_code == 200, first.text
    assert client.put(path, json=payload).json() == first.json()
    assert client.get(path[:-7]).json()['version'] == current['version']
    payload['request_id'] = 'move-2'
    assert client.put(path, json=payload).status_code == 409


def test_empty_explicit_clear_is_locked_and_history_restore_adds_version(client):
    pid, current = create_workspace(client)
    value = current['workspace']; value['graph']['nodes'] = [{'id': 'n-1', 'kind': 'concept', 'label': '节点', 'summary': '', 'confidence': None}]
    current = save(client, pid, current, value).json()
    path = f"/api/projects/{pid}/structure-workspaces/{value['id']}"
    cleared = client.put(path, json={'request_id': 'clear', 'expected_workspace_revision': current['version'], 'source_hash': value['source']['content_hash'],
        'commands': [{'op': 'edit', 'kind': 'node', 'id': 'n-1', 'fields': {}, 'explicit_clear_fields': ['summary']}]}).json()
    assert 'summary' in cleared['workspace']['graph']['nodes'][0]['locked_fields']
    restored = client.post(path + '/restore', json={'request_id': 'restore', 'expected_workspace_revision': cleared['version'], 'source_hash': value['source']['content_hash'], 'revision': 1})
    assert restored.status_code == 200, restored.text
    assert restored.json()['version'] == cleared['version'] + 1
    assert restored.json()['workspace']['graph']['nodes'] == []


def test_outline_apply_updates_workflow_and_workspace_atomically(client, monkeypatch):
    pid, current = create_workspace(client)
    result = generated(client, pid, current)
    wid = current['workspace']['id']
    card = client.post(f'/api/projects/{pid}/structure-workspaces/{wid}/outline-proposal', json={'request_id': 'proposal', 'draft_id': result['draft_id']})
    assert card.status_code == 200, card.text
    card = card.json()
    path = f"/api/projects/{pid}/agent/actions/{card['id']}"
    preview = client.post(path + '/preview', json={'selected_indices': [0, 1]})
    assert preview.status_code == 200, preview.text
    apply_payload = {'request_id': 'apply-1', 'expected_version': card['base_version'], 'action_fingerprint': preview.json()['action_fingerprint'], 'accepted': True}
    from paperpilot.services import structure_integration
    original = structure_integration.workflow.append_snapshot
    def broken(*args, **kwargs):
        raise RuntimeError('injected workflow failure')
    monkeypatch.setattr(structure_integration.workflow, 'append_snapshot', broken)
    try:
        client.post(path + '/apply', json=apply_payload)
    except RuntimeError:
        pass
    assert client.get(f'/api/projects/{pid}/outline/structured').json()['version'] == 0
    monkeypatch.setattr(structure_integration.workflow, 'append_snapshot', original)
    applied = client.post(path + '/apply', json=apply_payload)
    assert applied.status_code == 200, applied.text
    assert applied.json()['event']['kind'] == 'outline'
    saved = client.get(f'/api/projects/{pid}/structure-workspaces/{wid}').json()['workspace']
    assert len(saved['outline_bindings']) == 2
    assert saved['domain_versions']['outline'] == 1
    assert len(saved['graph']['groups']) == 1
    assert client.post(path + '/apply', json=apply_payload).json()['idempotent_replay']
    with app.db() as conn:
        from paperpilot.services import workflow
        plan = workflow.current_snapshot(conn, pid)['stage_outputs']['startup_plan']
        assert plan['stages'][1]['status'] == 'draft'
        assert '方法' in plan['stages'][1]['fields']['outline']


def test_saved_source_profile_is_in_preview_without_model_call(client):
    pid, current = create_workspace(client)
    response = client.post(f"/api/projects/{pid}/structure-workspaces/{current['workspace']['id']}/generation/preview", json={
        'instruction': '请搭建结构', 'context_snapshot': {'project_id': pid}})
    assert response.status_code == 200, response.text
    manifest = response.json()['context_manifest']['traceify']
    assert manifest['source_hash'] == current['workspace']['source']['content_hash']
    assert manifest['skill_hash'] and manifest['contract_hash']


def test_agent_executes_skill_and_persists_full_result(client, monkeypatch):
    pid, current = create_workspace(client)
    wid = current['workspace']['id']
    segments = [s['id'] for s in current['workspace']['source']['segments']]
    calls = []
    def model(*args, **kwargs):
        calls.append(kwargs)
        assert 'Traceify workflow' in json.dumps(kwargs['messages'], ensure_ascii=False)
        offered = {tool['function']['name'] for tool in kwargs.get('tools', [])}
        assert 'traceify_record_result' in offered
        if len(calls) == 1:
            name, data = 'traceify_context', {'workspace_id': wid}
        elif len(calls) == 2:
            name, data = 'traceify_record_result', {'workspace_id': wid, 'batch_key': 'first', 'expected_draft_revision': 0, 'result': {
                'nodes': [{'id': 'topic', 'label': '问题', 'kind': 'claim', 'summary': '待验证研究问题', 'content': '', 'segment_ids': segments, 'confidence': None}],
                'edges': [], 'groups': [], 'paper_outline': [{'section_id': 's1', 'parent_id': None, 'position': 0, 'title': '引言', 'purpose': '解释研究问题', 'node_ids': ['topic']}],
                'covered_segment_ids': segments, 'complete': True}}
        else:
            return {'ok': True, 'content': '结构草稿已保存，可查看来源并审阅。', 'usage': {'total_tokens': 70}}
        return {'ok': True, 'content': '', 'tool_calls': [{'id': f'call-{len(calls)}', 'type': 'function', 'function': {'name': name, 'arguments': json.dumps(data)}}], 'usage': {'total_tokens': 90}}
    monkeypatch.setattr(app, 'call_ai', model)
    cid = client.post(f'/api/projects/{pid}/agent/conversations', json={'title': '结构测试'}).json()['id']
    payload = {'conversation_id': cid, 'instruction': '请搭建论文结构', 'structure_workspace_id': wid, 'context_snapshot': {'project_id': pid}}
    base = f'/api/projects/{pid}/structure-workspaces/{wid}'
    preview = client.post(base + '/generation/preview', json=payload)
    assert preview.status_code == 200, preview.text
    response = client.post(base + '/generations', json={**payload, 'request_id': 'generate-1', 'preview_fingerprint': preview.json()['preview_fingerprint'], 'consent_to_send': True, 'possible_cost_confirmed': True})
    assert response.status_code == 200, response.text
    run_id = response.json()['id']
    end = time.monotonic() + 12
    while time.monotonic() < end:
        status = client.get(base + '/generations/' + run_id).json()
        if status['run']['status'] not in {'queued', 'running'}:
            break
        time.sleep(.03)
    assert status['run']['status'] == 'succeeded', status
    assert status['drafts'], status
    assert client.get(base + '/drafts/' + status['drafts'][0]['id']).json()['state'] == 'ready'
    assert len(calls) <= 3


def test_import_file_preview_preserves_utf8_text(client):
    pid, _ = create_workspace(client)
    response = client.post(f'/api/projects/{pid}/structure-workspaces/import-file/preview?filename=paper.md', content='# 标题\n\n原文段落。'.encode(), headers={'Content-Type': 'text/markdown'})
    assert response.status_code == 200, response.text
    assert response.json()['content'].endswith('原文段落。')


def test_failed_source_can_be_retained_without_fabricated_text(client):
    pid, _ = create_workspace(client)
    response = client.post(f'/api/projects/{pid}/structure-workspaces', json={'request_id': 'unresolved', 'entry_mode': 'paper_import', 'locator': 'https://example.org/paper', 'source_status': 'failed', 'source_warnings': ['无法读取公开网页']})
    assert response.status_code == 200, response.text
    assert response.json()['workspace']['source']['status'] == 'failed'
    assert response.json()['workspace']['source']['segments'] == []


def test_partial_batch_retains_valid_siblings_and_reports_bad_dependencies(client):
    pid, current = create_workspace(client)
    wid = current['workspace']['id']
    segments = [s['id'] for s in current['workspace']['source']['segments']]
    with app.db() as conn:
        result = store.record_result(conn, pid, wid, 'partial-run', {
            'request_id': 'partial-batch', 'result': {
                'nodes': [{'id': 'good', 'label': '有效节点', 'confidence': None},
                          {'id': 'bad', 'label': '错误节点', 'confidence': 2}],
                'edges': [{'id': 'dangling', 'from': 'good', 'to': 'bad', 'relation': '无效依赖'}],
                'groups': [{'id': 'child', 'label': '子模块', 'parent_id': 'parent', 'node_ids': ['good']},
                           {'id': 'parent', 'label': '父模块', 'node_ids': ['good']},
                           {'id': 'invalid', 'label': '错误模块', 'node_ids': ['bad']}],
                'paper_outline': [{'section_id': 'good-section', 'title': '有效章节', 'node_ids': ['good']},
                                  {'section_id': 'bad-section', 'title': '错误章节', 'node_ids': ['bad']}],
                'covered_segment_ids': segments, 'complete': True}})
        value = store.read_draft(conn, pid, wid, result['draft_id'])
    assert value['state'] == 'partial'
    assert len(value['errors']) == 4
    assert [n['label'] for n in value['workspace']['graph']['nodes']] == ['有效节点']
    assert len(value['workspace']['graph']['groups']) == 2
    assert len(value['workspace']['paper_outline']) == 1


def test_model_changes_to_existing_content_become_suggestions(client):
    pid, current = create_workspace(client)
    value = current['workspace']
    value['graph']['nodes'] = [{'id': 'author', 'label': '作者问题', 'summary': '已有内容', 'content': '',
                              'locked_fields': ['content'], 'status': 'confirmed', 'confidence': None}]
    current = save(client, pid, current, value).json()
    with app.db() as conn:
        result = store.record_result(conn, pid, value['id'], 'model-change', {'request_id': 'change', 'result': {
            'nodes': [{'id': 'author', 'label': '模型问题', 'summary': '模型改写', 'content': '锁定空值改写', 'confidence': None}],
            'covered_segment_ids': [], 'complete': False}})
        value = store.read_draft(conn, pid, value['id'], result['draft_id'])['workspace']
    assert value['graph']['nodes'][0]['summary'] == '已有内容'
    assert value['graph']['nodes'][0]['content'] == ''
    assert value['graph']['nodes'][0]['status'] == 'confirmed'
    assert value['pending_suggestions'][0]['proposed_fields']['summary'] == '模型改写'


def test_action_application_notes_do_not_invalidate_idea_source(client):
    from paperpilot.services import agent_conversations
    pid, current = create_workspace(client, 'idea_from_stage01')
    with app.db() as conn:
        conversation = agent_conversations.create(conn, pid, '动作通知')
        agent_conversations.append_message(conn, pid, conversation['id'], 'assistant', '已应用章节提案', context={'action_id': 'test-action'})
        assert not store.read(conn, pid, current['workspace']['id'])['host']['context_changed']
        agent_conversations.append_message(conn, pid, conversation['id'], 'user', '更新研究问题', context={'stage_id': 'STAGE-01'})
        assert store.read(conn, pid, current['workspace']['id'])['host']['context_changed']


def test_old_idea_branches_and_nonidea_messages_are_not_reintroduced(client):
    from paperpilot.services import agent_conversations
    pid, current = create_workspace(client, 'idea_from_stage01')
    with app.db() as conn:
        conversation = agent_conversations.create(conn, pid, '历史构思')
        agent_conversations.append_message(conn, pid, conversation['id'], 'user', '旧分支临床方向', context={'stage_id': 'STAGE-01', 'branch_id': 'old-branch'})
        agent_conversations.append_message(conn, pid, conversation['id'], 'user', '无关章节修订', context={'stage_id': 'STAGE-02'})
        context, manifest = store.idea_context(conn, pid)
    assert not context
    assert manifest['message_ids'] == []


def test_partial_draft_continuation_preserves_ids_and_coverage(client):
    pid, current = create_workspace(client)
    first = generated(client, pid, current)
    with app.db() as conn:
        original = store.read_draft(conn, pid, current['workspace']['id'], first['draft_id'])
        node = original['workspace']['graph']['nodes'][0]
        continued = store.record_result(conn, pid, current['workspace']['id'], 'continued-run', {
            'request_id': 'continuation', 'result': {'nodes': [{'id': 'topic', 'label': node['label'], 'kind': 'claim', 'summary': '', 'confidence': None}],
                'covered_segment_ids': [], 'complete': True}}, seed_draft_id=first['draft_id'])
        result = store.read_draft(conn, pid, current['workspace']['id'], continued['draft_id'])
    assert result['state'] == 'ready'
    assert {n['id'] for n in result['workspace']['graph']['nodes']} == {n['id'] for n in original['workspace']['graph']['nodes']}
    assert result['workspace']['coverage'] == original['workspace']['coverage']


def test_legacy_layout_and_logic_snapshot_migrate_without_changing_domains(client):
    from paperpilot.services import outline, logic_graph
    pid, _ = create_workspace(client)
    with app.db() as conn:
        outline._write_version(conn, pid, 0, 'legacy-outline', [{'id': 'old', 'parent_id': None, 'position': 3,
            'title': '旧章节', 'purpose': '原目标', 'canvas_x': 220, 'canvas_y': 180}], [], source='test')
        logic_graph.save_graph(conn, pid, base_version=0, request_id='legacy-logic', nodes=[
            {'id': 'claim', 'kind': 'claim', 'label': '论点', 'details': '旧论点', 'source_status': 'inaccessible'},
            {'id': 'evidence', 'kind': 'evidence', 'label': '证据', 'details': '旧证据'}], edges=[
            {'id': 'contradiction', 'from_node_id': 'claim', 'to_node_id': 'evidence', 'edge_type': 'contradicts'}])
        before = outline.read_outline(conn, pid)
        request = {'request_id': 'migration', 'entry_mode': 'paper_import', 'content': '迁移验证来源。'}
        result = store.create(conn, pid, request)
        assert store.create(conn, pid, request) == result
        assert outline.read_outline(conn, pid) == before
    value = result['workspace']
    assert result['version'] == 1 and result['host']['layout_revision'] == 1
    assert value['layout']['positions']['n-old'] == {'x': 220, 'y': 180}
    assert value['domain_snapshots']['outline']['nodes'][0]['position'] == 3
    assert value['domain_snapshots']['logic']['conflicts'][0]['status'] == 'unresolved'
    assert any(g['kind'] == 'source_unavailable' for g in value['domain_snapshots']['logic']['gaps'])


def test_manual_groups_survive_save_and_reload(client):
    pid, current = create_workspace(client)
    value = current['workspace']
    value['graph']['nodes'] = [{'id': 'manual', 'label': '手工模块成员', 'kind': 'concept'}]
    value['graph']['groups'] = [{'id': 'manual-group', 'label': '作者分组', 'node_ids': ['manual'], 'parent_id': None}]
    value['layout'].update(grouping_mode='manual', module_layout_version=1)
    result = save(client, pid, current, value)
    assert result.status_code == 200, result.text
    reloaded = client.get(f"/api/projects/{pid}/structure-workspaces/{value['id']}").json()['workspace']
    assert reloaded['layout']['grouping_mode'] == 'manual'
    assert reloaded['graph']['groups'][0]['label'] == '作者分组'
