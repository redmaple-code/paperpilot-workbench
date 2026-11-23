import uuid

import app
from paperpilot.services import continuation
from tests.test_req008_continuation import _project, _revision, _batch_request


def test_subgoals_are_distinct_persisted_and_outline_changes_make_candidates_read_only(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app, 'call_ai', lambda *args, **kwargs: calls.append(args[1]) or {'ok': True, 'content': '候选内容'})
    pid = _project(client); _revision(client, pid)
    client.post(f'/api/projects/{pid}/outline/nodes', json={'command': 'create', 'request_id': uuid.uuid4().hex, 'base_version': 0, 'node': {'id': 'first', 'title': '引言'}})
    client.post(f'/api/projects/{pid}/outline/nodes', json={'command': 'create', 'request_id': uuid.uuid4().hex, 'base_version': 1, 'node': {'id': 'second', 'title': '方法'}})
    goals = [{'goal': '补研究动机', 'outline_node_id': 'first', 'anchor_start': None, 'anchor_end': None}, {'goal': '补实验方法', 'outline_node_id': 'second', 'anchor_start': None, 'anchor_end': None}]
    paper = client.get(f'/api/projects/{pid}').json(); file = next(f for f in paper['files'] if f['path']=='main.tex')
    body = {'request_id': uuid.uuid4().hex, 'target_kind': 'file', 'target_path': 'main.tex', 'base_version': file['version'], 'anchor_start': 0, 'anchor_end': 0, 'goal': '完成两节', 'prompt': '', 'segment_count': 2, 'outline_node_id': 'first', 'outline_version': 2, 'subgoals': goals}
    preview = client.post(f'/api/projects/{pid}/ai-context', json={'action': 'continue', 'target_kind': 'file', 'target_path': 'main.tex', 'content': file['content'], 'selection': '', 'prompt': continuation.request_prompt(body)})
    assert preview.status_code == 200, preview.text
    response = client.post(f'/api/projects/{pid}/continuations', json={**body, 'context_version': preview.json()['version']})
    assert response.status_code == 202, response.text
    batch = client.get(f"/api/projects/{pid}/continuations/{response.json()['batch']['id']}").json()
    assert [s['goal'] for s in batch['segments']] == ['补研究动机','补实验方法']
    assert len(calls)==2 and calls[0].endswith('本段目标：补研究动机') and calls[1].endswith('本段目标：补实验方法')
    client.post(f'/api/projects/{pid}/outline/nodes', json={'command': 'update', 'request_id': uuid.uuid4().hex, 'base_version': 2, 'node_id': 'first', 'node': {'title':'新引言'}})
    stale = client.get(f"/api/projects/{pid}/continuations/{batch['id']}").json()
    assert stale['outline_stale_reasons'] and all(s['candidate']['stale'] for s in stale['segments'])


def test_actual_cumulative_usage_pauses_before_second_send_without_losing_first_candidate(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app, 'call_ai', lambda *args, **kwargs: calls.append(1) or {'ok': True, 'content': '第一段候选', 'usage': {'prompt_tokens': 10990, 'completion_tokens': 10, 'total_tokens': 11000}})
    pid = _project(client); _revision(client, pid)
    batch = _batch_request(client, pid, request_id=uuid.uuid4().hex, hard_budget_tokens=12000)
    saved = client.get(f"/api/projects/{pid}/continuations/{batch['id']}").json()
    assert len(calls) == 1
    assert saved['status'] == 'paused' and saved['pause_reason'] == 'budget_exceeded'
    assert saved['segments'][0]['candidate']
    assert saved['budget']['usage']['tokens'] == 11000


def test_unknown_pricing_cannot_execute_a_money_hard_limit(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app, 'call_ai', lambda *a, **kw: calls.append(1))
    pid = _project(client); _revision(client, pid)
    batch = _batch_request(client, pid, request_id=uuid.uuid4().hex, hard_budget_amount=.1)
    assert batch['status'] == 'paused' and batch['budget']['cost_status'] == 'unknown'
    assert not calls
