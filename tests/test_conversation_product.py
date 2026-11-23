"""Regression coverage for the managed Red Maple workspace and author decisions."""
import base64
import json
import threading
import time
import uuid

import app
from tests.test_agent_conversations import payload, wait_run
from tests.test_req022_assets import project
from paperpilot.services.plan_state import STAGES


def workspace(client):
    pid = project(client)
    result = client.post(f'/api/projects/{pid}/agent/workspace',json={}).json()
    return pid,result['root']['id']


def proposal(client,pid,cid):
    version=client.get(f'/api/projects/{pid}/workflow/startup').json()['workflow_version']
    response=client.post(f'/api/projects/{pid}/agent/actions',json={
        'request_id':uuid.uuid4().hex,'conversation_id':cid,'operation':'planning','base_version':version,
        'payload':{'stage_id':'STAGE-01','mode':'confirm','fields':{key:'经讨论的'+label for key,label in STAGES[0]['fields'].items()}}})
    assert response.status_code==200,response.text
    return response.json()


def review(client,pid,cid,card):
    preview=client.post(f'/api/projects/{pid}/agent/actions/{card["id"]}/preview',json={}).json()
    response=client.post(f'/api/projects/{pid}/agent/actions/{card["id"]}/displayed',json={
        'conversation_id':cid,'action_fingerprint':preview['action_fingerprint']})
    assert response.status_code==200,response.text
    return preview


def decide(client,pid,cid,instruction='确认采用',**extra):
    return client.post(f'/api/projects/{pid}/agent/author-decisions',json={
        'conversation_id':cid,'instruction':instruction,'request_id':uuid.uuid4().hex,**extra})


def test_directory_covers_all_requirements_and_old_names(client):
    pid,cid=workspace(client)
    items=client.get(f'/api/projects/{pid}/agent/tools').json()['items']
    assert len(items)==37 and len({i['id'] for i in items})==37
    assert {r for item in items for r in item['requirement_ids']}=={f'REQ-{i:03}' for i in range(1,28) if i!=12}
    assert [item['route'] for item in items if item['id']=='innovations'] == ['innovations']
    results=client.get(f'/api/projects/{pid}/agent/tools',params={'query':'结果数据'}).json()['items']
    assert [item['route'] for item in results]==['visual/results']
    own=client.get(f'/api/projects/{pid}/agent/tools',params={'query':'已有论文导入'}).json()['items']
    assert [item['route'] for item in own]==['manuscript-import']
    assert client.get(f'/api/projects/{pid}/agent/tools',params={'query':'图片绘制'}).json()['items'][0]['id']=='draw'
    reused=client.get(f'/api/projects/{pid}/agent/tools',params={'query':'复制语料'}).json()['items'][0]
    assert reused['id']=='domain' and reused['route']=='knowledge/shared'
    history=client.get(f'/api/projects/{pid}/agent/tools',params={'query':'历史'}).json()['items']
    assert {'versions','tasks'} <= {item['id'] for item in history}


def test_author_decision_requires_display_and_replays_atomically(client):
    pid,cid=workspace(client);card=proposal(client,pid,cid)
    response=decide(client,pid,cid).json()
    assert response['status']=='review_required'
    review(client,pid,cid,card)
    request={'conversation_id':cid,'request_id':uuid.uuid4().hex,'instruction':'就按这个'}
    first=client.post(f'/api/projects/{pid}/agent/author-decisions',json=request)
    assert first.status_code==200,first.text
    assert first.json()['status']=='applied'
    replay=client.post(f'/api/projects/{pid}/agent/author-decisions',json=request).json()
    assert replay['idempotent_replay'] and replay['action_id']==card['id']
    assert client.get(f'/api/projects/{pid}/workflow/startup').json()['plan']['stages'][0]['status']=='confirmed'


def test_multi_candidate_partial_and_cross_conversation_guard(client):
    pid,cid=workspace(client);a=proposal(client,pid,cid);b=proposal(client,pid,cid)
    review(client,pid,cid,a);preview=review(client,pid,cid,b)
    assert decide(client,pid,cid).json()['status']=='choose_target'
    response=decide(client,pid,cid,'只接受前两点',action_id=b['id']).json()
    assert response['status']=='review_required' and not response['preview']['diff']['confirmation']
    assert len(response['preview']['diff']['fields'])==2
    assert client.post(f'/api/projects/{pid}/agent/actions/{a["id"]}/reject',json={}).status_code==200
    assert decide(client,pid,cid).json()['status']=='review_required'
    updated=response['preview']
    client.post(f'/api/projects/{pid}/agent/actions/{b["id"]}/displayed',json={'conversation_id':cid,'action_fingerprint':updated['action_fingerprint']})
    assert decide(client,pid,cid).json()['status']=='applied'
    assert client.get(f'/api/projects/{pid}/workflow/startup').json()['plan']['stages'][0]['status']=='draft'
    other=client.post(f'/api/projects/{pid}/agent/conversations',json={'title':'其他讨论'}).json()['id']
    wrong=client.post(f'/api/projects/{pid}/agent/actions/{a["id"]}/displayed',json={'conversation_id':other,'action_fingerprint':preview['action_fingerprint']})
    assert wrong.status_code==409


def test_stale_natural_language_acceptance_preserves_newer_content(client):
    pid,cid=workspace(client);a=proposal(client,pid,cid);b=proposal(client,pid,cid)
    review(client,pid,cid,a);review(client,pid,cid,b)
    assert decide(client,pid,cid,action_id=b['id']).status_code==200
    response=decide(client,pid,cid,action_id=a['id'])
    assert response.status_code==409,response.text
    assert response.json()['detail']['code'] in {'stale_version','workflow_version_conflict','workflow_conflict'}


def test_partial_acceptance_uses_display_order(client):
    pid,cid=workspace(client);card=proposal(client,pid,cid);review(client,pid,cid,card)
    response=decide(client,pid,cid,'只接受前一点').json()
    assert response['preview']['diff']['fields'][0]['key']==next(iter(STAGES[0]['fields']))
    assert len(response['preview']['diff']['fields'])==1


def test_real_redmaple_delegation_shares_parent_budget_and_persists_topic(client,monkeypatch):
    calls=[]
    def model(*args,**kwargs):
        messages=kwargs['messages'];calls.append(messages)
        if '你是论文总 Agent 下的「章节大纲」' in messages[0]['content']:
            return {'ok':True,'content':'结构助理已核对章节目标，建议先说明研究边界。','usage':{'total_tokens':60}}
        if not any(m['role']=='tool' and m.get('name')=='delegate_agent' for m in messages):
            return {'ok':True,'content':'安排结构助理核对。','tool_calls':[{'id':'delegate-outline','type':'function',
                    'function':{'name':'delegate_agent','arguments':json.dumps({'module_key':'outline','instruction':'核对章节目标与边界'})}}], 'usage':{'total_tokens':60}}
        return {'ok':True,'content':'综合结构助理结果，先说明研究边界，再组织方法章节。','usage':{'total_tokens':60}}
    monkeypatch.setattr(app,'call_ai',model)
    pid,cid=workspace(client)
    response=client.post(f'/api/projects/{pid}/agent/runs',json=payload(client,pid,cid,'请安排结构助理核对章节目标'))
    done=wait_run(client,pid,response.json()['id'])
    assert done['status']=='succeeded',done
    assert done['budget']['model_calls']==3 and done['budget']['tool_calls']==1
    assert all(sum(m['role']=='system' for m in messages)==1 for messages in calls)
    assert any(e['kind']=='runtime_selected' for e in done['events'])
    tasks=client.get(f'/api/projects/{pid}/agent/subtasks',params={'run_id':done['id']}).json()['tasks']
    assert len(tasks)==1 and tasks[0]['status']=='succeeded'
    messages=client.get(f'/api/projects/{pid}/agent/conversations/{tasks[0]["conversation_id"]}/messages').json()['messages']
    assert '结构助理已核对' in messages[-1]['text']


def test_running_author_steering_reaches_next_model_checkpoint(client,monkeypatch):
    entered=threading.Event();release=threading.Event();calls=[]
    def model(*args,**kwargs):
        calls.append(kwargs['messages'])
        if len(calls)==1:
            entered.set();assert release.wait(20)
            return {'ok':True,'content':'','tool_calls':[{'id':'stage','type':'function','function':{'name':'read_stage','arguments':'{}'}}],'usage':{'total_tokens':50}}
        return {'ok':True,'content':'已接收新的篇幅要求。','usage':{'total_tokens':50}}
    monkeypatch.setattr(app,'call_ai',model)
    pid,cid=workspace(client)
    response=client.post(f'/api/projects/{pid}/agent/runs',json=payload(client,pid,cid,'先核对当前方案'))
    rid=response.json()['id'];assert entered.wait(25)
    try:
        instruction={'request_id':'steer-once','text':'补充要求，最终控制在两句话。'}
        first=client.post(f'/api/projects/{pid}/agent/runs/{rid}/steering',json=instruction)
        assert first.status_code==200,first.text
        replay=client.post(f'/api/projects/{pid}/agent/runs/{rid}/steering',json=instruction)
        assert replay.json()['id']==first.json()['id']
    finally:
        release.set()
    done=wait_run(client,pid,rid)
    assert done['status']=='succeeded',done
    assert '最终控制在两句话' in json.dumps(calls[-1],ensure_ascii=False)
    assert any(e['kind']=='steering_received' for e in done['events'])


def test_attachment_preview_is_conversation_owned_and_revocable(client):
    pid,cid=workspace(client)
    request={'request_id':'notes','filename':'研究笔记.md','content_base64':base64.b64encode('有来源的作者资料与待核验假设'.encode()).decode()}
    response=client.post(f'/api/projects/{pid}/agent/conversations/{cid}/attachments',json=request)
    assert response.status_code==200,response.text
    assert client.post(f'/api/projects/{pid}/agent/conversations/{cid}/attachments',json=request).json()['id']==response.json()['id']
    preview=client.post(f'/api/projects/{pid}/agent/preview',json={'conversation_id':cid,'instruction':'阅读当前资料'}).json()
    assert preview['context_manifest']['attachments'][0]['filename']=='研究笔记.md'
    other=client.post(f'/api/projects/{pid}/agent/conversations',json={'title':'另一会话'}).json()['id']
    assert not client.get(f'/api/projects/{pid}/agent/conversations/{other}/attachments').json()['files']
    attachment_id=response.json()['id']
    inspected=client.get(f'/api/projects/{pid}/agent/conversations/{cid}/attachments/{attachment_id}')
    assert inspected.status_code==200 and inspected.json()['content']=='有来源的作者资料与待核验假设'
    assert client.get(f'/api/projects/{pid}/agent/conversations/{other}/attachments/{attachment_id}').status_code==404
    assert client.delete(f'/api/projects/{pid}/agent/conversations/{other}/attachments/{attachment_id}').status_code==404
    assert client.delete(f'/api/projects/{pid}/agent/conversations/{cid}/attachments/{attachment_id}').status_code==200
    assert not client.get(f'/api/projects/{pid}/agent/conversations/{cid}/attachments').json()['files']


def test_redmaple_recovers_known_model_result_without_resending(client,monkeypatch):
    from paperpilot.services import agent_runtime
    calls=[]
    monkeypatch.setattr(app,'call_ai',lambda *a,**k:calls.append(k) or {'ok':True,'content':'已知的可恢复结果。','usage':{'total_tokens':40}})
    original=agent_runtime._checkpoint
    failed=[]
    def checkpoint(conn,rid,state,step,next_node):
        known=any(call.get('status')=='succeeded' and call.get('kind')=='model' for call in (state.get('redmaple') or {}).get('calls',{}).values())
        if known and not failed:
            failed.append(True)
            raise OSError('injected checkpoint failure after a known model result')
        return original(conn,rid,state,step,next_node)
    monkeypatch.setattr(agent_runtime,'_checkpoint',checkpoint)
    pid,cid=workspace(client)
    response=client.post(f'/api/projects/{pid}/agent/runs',json=payload(client,pid,cid,'说明当前论文边界'))
    rid=response.json()['id'];assert wait_run(client,pid,rid)['status']=='failed'
    monkeypatch.setattr(agent_runtime,'_checkpoint',original)
    preview=client.post(f'/api/projects/{pid}/agent/runs/{rid}/resume/preview',json={})
    assert preview.status_code==200,preview.text
    resumed=client.post(f'/api/projects/{pid}/agent/runs/{rid}/resume',json={
        'request_id':uuid.uuid4().hex,'preview_fingerprint':preview.json()['preview_fingerprint'],
        'consent_to_send':True,'possible_cost_confirmed':True})
    assert resumed.status_code==200,resumed.text
    done=wait_run(client,pid,rid)
    assert done['status']=='succeeded',done
    assert len(calls)==1 and done['result']['text']=='已知的可恢复结果。'


def test_custom_topic_stays_under_paper_and_retains_identity(client):
    pid,cid=workspace(client)
    response=client.post(f'/api/projects/{pid}/agent/conversations',json={'parent_id':cid,'title':'引言的论证结构'})
    assert response.status_code==200,response.text
    topic=response.json()
    assert topic['parent_id']==cid and topic['module_key']==''
    current=client.get(f'/api/projects/{pid}/agent/workspace').json()
    assert any(item['id']==topic['id'] for item in current['children'])
    preview=client.post(f'/api/projects/{pid}/agent/preview',json={'conversation_id':topic['id'],'instruction':'讨论章节目标'}).json()
    assert preview['context_manifest']['agent_identity']['module_key']=='topic'
    other,other_root=workspace(client)
    assert client.post(f'/api/projects/{other}/agent/conversations',json={'parent_id':cid,'title':'错误归属'}).status_code==404


def test_single_tool_provider_uses_validated_dispatch(client,monkeypatch):
    from paperpilot.services import agent_runtime
    original=agent_runtime.preview
    def preview(*args,**kwargs):
        value=original(*args,**kwargs);value['_capability']={'max_tools':1};return value
    monkeypatch.setattr(agent_runtime,'preview',preview)
    def model(*args,**kwargs):
        assert len(kwargs['tools'])==1
        if any(message['role']=='tool' for message in kwargs['messages']):
            return {'ok':True,'content':'已通过单工具入口核对阶段。','usage':{'total_tokens':40}}
        return {'ok':True,'content':'','tool_calls':[{'id':'single-read','type':'function',
            'function':{'name':'paper_tool','arguments':json.dumps({'name':'read_stage','arguments':{}})}}],'usage':{'total_tokens':40}}
    monkeypatch.setattr(app,'call_ai',model)
    pid,cid=workspace(client)
    response=client.post(f'/api/projects/{pid}/agent/runs',json=payload(client,pid,cid,'核对当前阶段'))
    done=wait_run(client,pid,response.json()['id'])
    assert done['status']=='succeeded',done
    assert done['budget']['model_calls']==2 and done['budget']['tool_calls']==1


def test_cancelling_parent_stops_child_and_preserves_known_call(client,monkeypatch):
    entered=threading.Event();release=threading.Event();calls=[]
    def model(*args,**kwargs):
        calls.append(kwargs['messages'])
        if '你是论文总 Agent 下的「章节大纲」' in kwargs['messages'][0]['content']:
            entered.set();assert release.wait(30)
            return {'ok':True,'content':'迟到结果保留执行记录。','usage':{'total_tokens':40}}
        return {'ok':True,'content':'','tool_calls':[{'id':'cancel-child','type':'function',
            'function':{'name':'delegate_agent','arguments':json.dumps({'module_key':'outline','instruction':'核对章节'})}}],'usage':{'total_tokens':40}}
    monkeypatch.setattr(app,'call_ai',model)
    pid,cid=workspace(client)
    response=client.post(f'/api/projects/{pid}/agent/runs',json=payload(client,pid,cid,'安排结构助理核对章节'))
    rid=response.json()['id'];assert entered.wait(45)
    try:
        assert client.post(f'/api/projects/{pid}/agent/runs/{rid}/cancel',json={}).status_code==200
    finally:
        release.set()
    deadline=time.monotonic()+30
    while time.monotonic()<deadline:
        tasks=client.get(f'/api/projects/{pid}/agent/subtasks',params={'run_id':rid}).json()['tasks']
        conn=app.db();lease=conn.execute('SELECT lease_token FROM agent_runs WHERE id=?',(rid,)).fetchone()[0];conn.close()
        if tasks and tasks[0]['status']!='running' and not lease:
            break
        time.sleep(.05)
    assert tasks[0]['status']=='cancelled' and len(calls)==2
