"""Use the installed skill's real store with isolated application projects."""
import copy
import json
import uuid

import pytest
import app
from paperpilot.services import agent_conversations, scholar_workspace
from paperpilot.services import agent_tools
from paperpilot.services.project_errors import ProjectServiceError
from tests.test_req022_assets import project

pytestmark = pytest.mark.skipif(not scholar_workspace.public_skill()['available'], reason='scholar-apprentice is not installed')


def graph():
    return {'version': '0.1', 'purpose': '研究方案', 'directions': [{'id': 'd', 'title': '待核验的机制方向'}],
            'sources': [], 'units': [{'id': 'u', 'kind': 'concept', 'title': '研究对象与边界', 'content': '先界定研究对象，再核查适用条件。',
                'scope': {'object': 'isolated synthetic study'}, 'direction_ids': ['d'], 'aliases': [],
                'lifecycle': 'candidate', 'epistemic_status': 'unassessed', 'evidence': []}],
            'edges': [], 'tasks': [], 'bindings': [],
            'stage_draft': {'research_question': '待检验的研究问题', 'contribution': '待核验的机制候选', 'scope': '固定对象与评价边界'}}


def context(conn, pid, cid=''):
    return {'conn': conn, 'project_id': pid, 'conversation_id': cid, 'stage_id': 'STAGE-01',
            'run_id': 'test-run', 'state': {'research_profile': 'scholar_apprentice'}}


def test_native_knowledge_store_replays_and_versions_without_changing_plan(client):
    pid = project(client)
    before = client.get(f'/api/projects/{pid}/workflow/startup').json()
    conn = app.db()
    try:
        payload = {'action': 'knowledge', 'expected_version': 0, 'graph': graph()}
        created = scholar_workspace.handle(payload, context(conn, pid))
        assert created['revision'] == 1 and created['graph']['units'][0]['id'] == 'u'
        replay = scholar_workspace.handle(payload, context(conn, pid))
        assert replay['revision'] == 1 and replay['native_result']['already_committed']
        changed = copy.deepcopy(payload)
        changed['graph']['units'][0]['content'] += ' 保留新条件。'
        with pytest.raises(ProjectServiceError):
            scholar_workspace.handle(changed, context(conn, pid))
        after = client.get(f'/api/projects/{pid}/workflow/startup').json()
        assert after['workflow_version'] == before['workflow_version']
        assert after['plan']['stages'][0]['fields']['research_question'] == ''
        assert (scholar_workspace.workspace(pid) / 'db/structure.db').is_file()
    finally:
        conn.close()


def test_native_research_routes_and_project_isolation(client):
    pid = project(client)
    other = project(client)
    nodes = [{'id': 'g', 'kind': 'goal', 'title': '研究目标', 'acceptance': '有边界的判别方案'},
             {'id': 'r', 'kind': 'route', 'parent_id': 'g', 'title': '竞争机制', 'rationale': '分辨原因'},
             {'id': 'p', 'kind': 'plan', 'parent_id': 'r', 'title': '对照设计', 'rationale': '固定条件'},
             {'id': 't', 'kind': 'task', 'parent_id': 'p', 'title': '证据核查',
              'spec': {'question': '哪些证据改变判断', 'method': '核查原始来源', 'acceptance': '记录支持与反证',
                       'conditions': {'scope': 'synthetic'}, 'inputs': []}, 'depends_on': [], 'knowledge': []}]
    conn = app.db()
    try:
        result = scholar_workspace.handle({'action': 'plan', 'nodes': nodes}, context(conn, pid))
        assert len(result['routes']) == 4
        assert scholar_workspace.snapshot(other)['routes'] == []
        assert not scholar_workspace.workspace(other).exists()
    finally:
        conn.close()


def test_fabricated_sources_and_automatic_review_status_are_rejected(client):
    pid = project(client)
    conn = app.db()
    try:
        value = graph()
        value['sources'] = [{'id': 'fabricated', 'title': '虚构来源'}]
        with pytest.raises(ProjectServiceError, match='来源须引用'):
            scholar_workspace.handle({'action': 'knowledge', 'expected_version': 0, 'graph': value}, context(conn, pid))
        value = graph()
        value['units'][0]['lifecycle'] = 'reviewed'
        with pytest.raises(ProjectServiceError, match='候选'):
            scholar_workspace.handle({'action': 'knowledge', 'expected_version': 0, 'graph': value}, context(conn, pid))
        assert scholar_workspace.snapshot(pid)['revision'] == 0
    finally:
        conn.close()


def test_research_profile_changes_authority_and_is_stage_bound(client):
    assert [item['function']['name'] for item in agent_tools.tool_schemas('scholar', max_tools=2)] == ['scholar_research', 'search_academic']
    pid = project(client)
    cid = client.post(f'/api/projects/{pid}/agent/conversations', json={'title': '领域研究'}).json()['id']
    body = {'conversation_id': cid, 'instruction': '梳理研究方向'}
    normal = client.post(f'/api/projects/{pid}/agent/preview', json=body).json()
    research = client.post(f'/api/projects/{pid}/agent/preview', json=body | {'research_profile': 'scholar_apprentice', 'stage_id': 'STAGE-01'}).json()
    assert normal['authorization_fingerprint'] != research['authorization_fingerprint']
    assert research['context_manifest']['research_profile'] == 'scholar_apprentice'
    assert research['research_context']['workspace']['skill']['available']
    rejected = client.post(f'/api/projects/{pid}/agent/preview', json=body | {'research_profile': 'scholar_apprentice', 'stage_id': 'STAGE-02'})
    assert rejected.status_code == 422


def test_revoked_sources_are_quarantined_from_agent_context(client):
    pid = project(client)
    response = client.post(f'/api/projects/{pid}/corpus', json={'title': '来源权限夹具', 'content': '仅用于隔离验证的材料。'})
    assert response.status_code == 200
    conn = app.db()
    try:
        sources = scholar_workspace.available_sources(conn, pid, '')
        identity = next(key for key in sources if key.startswith('corpus:'))
        value = graph()
        value['sources'] = [{'id': identity}]
        value['units'][0]['evidence'] = [{'source_id': identity, 'locator': '原始材料', 'role': 'context'}]
        scholar_workspace.handle({'action': 'knowledge', 'expected_version': 0, 'graph': value}, context(conn, pid))
        assert value['sources'] == [{'id': identity}]
        with conn:
            conn.execute("UPDATE corpus_source_versions SET permission_state='revoked'")
        safe, _ = scholar_workspace.agent_snapshot(conn, pid, '')
        assert safe['revision'] == 1 and safe['graph'] == {} and safe['omitted']
        with pytest.raises(ProjectServiceError, match='来源须引用'):
            scholar_workspace.handle({'action': 'knowledge', 'expected_version': 1, 'graph': value}, context(conn, pid))
    finally:
        conn.close()


def test_native_contract_keeps_actual_author_origin_and_replays(client):
    pid = project(client)
    conn = app.db()
    try:
        cid = agent_conversations.create(conn, pid, '领域研究')['id']
        with conn:
            agent_conversations.append_message(conn, pid, cid, 'user', '我的真实研究目标是区分两个机制。')
        contract = {'objective': '区分两个机制', 'deliverable': 'plan', 'exclusions': ['实际数值实验'],
            'requirements': [{'id': phase, 'phase': phase, 'question': '本环节的待核查问题', 'criterion': '可追溯证据与判别动作',
                              'allowed_outcomes': ['supported', 'refuted', 'bounded_inconclusive']} for phase in
                             ['framing', 'knowledge', 'challenge', 'verification', 'synthesis']]}
        result = scholar_workspace.handle({'action': 'contract', 'contract': contract}, context(conn, pid, cid))
        identity = result['native_result']['id']
        saved = json.loads((scholar_workspace.workspace(pid) / '研究档案/研究合同' / identity / 'contract.json').read_text(encoding='utf8'))
        origin = saved['origin'][0]
        assert '真实研究目标' in (scholar_workspace.workspace(pid) / origin['path']).read_text(encoding='utf8')
        replay = scholar_workspace.handle({'action': 'contract', 'contract': contract}, context(conn, pid, cid))
        assert replay['native_result']['already_committed']
        state = {'contract_sha256': result['native_result']['contract_sha256'], 'producer_context': 'isolated-agent',
            'results': [{'requirement_id': item['id'], 'status': 'open'} for item in contract['requirements']],
            'actions': [{'id': item['id'] + '-next', 'requirement_id': item['id'], 'description': '执行该义务的核查', 'status': 'pending'}
                        for item in contract['requirements']], 'gaps': []}
        checked = scholar_workspace.handle({'action': 'check_contract', 'contract_id': identity, 'contract_state': state}, context(conn, pid, cid))
        assert checked['native_result']['status'] == 'continue'
        assert not checked['native_result']['contract_satisfied']
    finally:
        conn.close()


def test_persistent_agent_calls_the_native_scholar_tool(client, monkeypatch):
    import time
    calls = []
    def model(*args, **kwargs):
        calls.append(kwargs['messages'])
        if len(calls) == 1:
            return {'ok': True, 'content': '', 'tool_calls': [{'id': 'native-graph', 'type': 'function',
                'function': {'name': 'scholar_research', 'arguments': json.dumps({'action': 'knowledge', 'expected_version': 0, 'graph': graph()})}}],
                'usage': {'total_tokens': 100}}
        return {'ok': True, 'content': '已形成候选知识结构，下一步核查来源与竞争解释。', 'usage': {'total_tokens': 100}}
    monkeypatch.setattr(app, 'call_ai', model)
    pid = project(client)
    cid = client.post(f'/api/projects/{pid}/agent/conversations', json={'title': '研究对话'}).json()['id']
    body = {'conversation_id': cid, 'instruction': '建立领域知识地图', 'research_profile': 'scholar_apprentice', 'stage_id': 'STAGE-01'}
    preview = client.post(f'/api/projects/{pid}/agent/preview', json=body).json()
    response = client.post(f'/api/projects/{pid}/agent/runs', json=body | {'request_id': uuid.uuid4().hex,
        'preview_fingerprint': preview['preview_fingerprint'], 'consent_to_send': True, 'possible_cost_confirmed': True})
    assert response.status_code == 200, response.text
    deadline = time.monotonic() + 25
    run = response.json()
    while run['status'] in ('queued', 'running') and time.monotonic() < deadline:
        time.sleep(.05)
        run = client.get(f'/api/projects/{pid}/agent/runs/{run["id"]}').json()
    assert run['status'] == 'succeeded', run
    assert any('scholar-apprentice' in message['content'] for message in calls[0] if message['role'] == 'system')
    assert client.get(f'/api/projects/{pid}/agent/research').json()['revision'] == 1
    assert len(calls) == 2
