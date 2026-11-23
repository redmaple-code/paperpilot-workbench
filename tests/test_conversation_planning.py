"""Conversation acceptance delegates to the existing workflow transaction ledger."""
import uuid
import app
from tests.test_req022_assets import project
from paperpilot.services import startup_workflow
from paperpilot.services.plan_state import STAGES


def propose(client, pid, stage, mode='confirm', **extra):
    version=client.get(f'/api/projects/{pid}/workflow/startup').json()['workflow_version']
    payload={'stage_id':stage['id'],'fields':{key:'作者已讨论的'+label for key,label in stage['fields'].items()},'mode':mode,**extra}
    response=client.post(f'/api/projects/{pid}/agent/actions',json={'request_id':uuid.uuid4().hex,'operation':'planning','base_version':version,'payload':payload})
    assert response.status_code==200,response.text
    card=response.json()
    preview=client.post(f'/api/projects/{pid}/agent/actions/{card["id"]}/preview',json={})
    assert preview.status_code==200,preview.text
    body={'request_id':uuid.uuid4().hex,'expected_version':version,'action_fingerprint':preview.json()['action_fingerprint'],'accepted':True}
    return card,body


def test_all_five_plans_can_be_accepted_in_conversation(client):
    pid=project(client)
    for index,stage in enumerate(STAGES):
        before=client.get(f'/api/projects/{pid}/workflow/startup').json()
        card,body=propose(client,pid,stage)
        assert client.get(f'/api/projects/{pid}/workflow/startup').json()['workflow_version']==before['workflow_version']
        applied=client.post(f'/api/projects/{pid}/agent/actions/{card["id"]}/apply',json=body)
        assert applied.status_code==200,applied.text
        plan=client.get(f'/api/projects/{pid}/workflow/startup').json()['plan']
        assert plan['stages'][index]['status']=='confirmed'
        replay=client.post(f'/api/projects/{pid}/agent/actions/{card["id"]}/apply',json=body)
        assert replay.json()['idempotent_replay']
        saved=next(item for item in client.get(f'/api/projects/{pid}/agent/actions').json()['cards'] if item['id']==card['id'])
        assert saved['title']=='已确认的论文方案' and saved['reviewed_plan']['fields']
    assert plan['status']=='complete'


def test_failed_confirmation_rolls_back_the_whole_acceptance(client,monkeypatch):
    pid=project(client);card,body=propose(client,pid,STAGES[0])
    before=client.get(f'/api/projects/{pid}/workflow/startup').json()
    original=startup_workflow.command
    from paperpilot.services.project_errors import ProjectServiceError
    def fail(conn,pid,payload):
        if payload['command']=='confirm':raise ProjectServiceError(409,'fixture_failed','隔离测试的确认失败')
        return original(conn,pid,payload)
    monkeypatch.setattr(startup_workflow,'command',fail)
    response=client.post(f'/api/projects/{pid}/agent/actions/{card["id"]}/apply',json=body)
    assert response.status_code==409
    after=client.get(f'/api/projects/{pid}/workflow/startup').json()
    assert before['workflow_version']==after['workflow_version'] and before['plan']==after['plan']


def test_draft_stale_conflict_and_explicit_revision(client):
    pid=project(client)
    card,body=propose(client,pid,STAGES[0],mode='draft')
    assert client.post(f'/api/projects/{pid}/agent/actions/{card["id"]}/apply',json=body).status_code==200
    assert client.get(f'/api/projects/{pid}/workflow/startup').json()['plan']['stages'][0]['status']=='draft'
    stale,stale_body=propose(client,pid,STAGES[0])
    current,body=propose(client,pid,STAGES[0])
    assert client.post(f'/api/projects/{pid}/agent/actions/{current["id"]}/apply',json=body).status_code==200
    assert client.post(f'/api/projects/{pid}/agent/actions/{stale["id"]}/apply',json=stale_body).status_code==409
    revise,body=propose(client,pid,STAGES[0],revise=True)
    assert client.post(f'/api/projects/{pid}/agent/actions/{revise["id"]}/apply',json=body).status_code==200


def test_main_conversation_research_and_writing_modes(client):
    pid=project(client)
    workspace=client.post(f'/api/projects/{pid}/agent/workspace',json={}).json()
    from paperpilot.services import agent_conversations,scholar_workspace
    conn=app.db()
    try:
        cid=workspace['root']['id']
        assert agent_conversations.bind_request(conn,pid,{'conversation_id':cid,'instruction':'研究这个问题'})['research_profile']=='scholar_apprentice'
        assert agent_conversations.bind_request(conn,pid,{'conversation_id':cid,'instruction':'起草一段引言'})['research_profile']==''
        from paperpilot.services import agent_runtime
        research=agent_runtime.preview(conn,pid,{'conversation_id':cid,'instruction':'研究这个问题'})
        writing=agent_runtime.preview(conn,pid,{'conversation_id':cid,'instruction':'起草一段引言'})
        assert research['authorization_fingerprint']==writing['authorization_fingerprint']
        child=workspace['children'][0]['id']
        with conn:agent_conversations.append_message(conn,pid,child,'user','子对话中的作者材料')
        assert any(item['title']=='子对话中的作者材料' for item in scholar_workspace.available_sources(conn,pid,cid).values())
        legacy=agent_conversations.create(conn,pid,'独立旧讨论')['id']
        with conn:agent_conversations.append_message(conn,pid,legacy,'user','未关联旧会话内容')
        assert not any(item['title']=='未关联旧会话内容' for item in scholar_workspace.available_sources(conn,pid,cid).values())
    finally:conn.close()


def test_react_rounds_keep_a_single_system_message(client,monkeypatch):
    from tests.test_agent_conversations import conversation,payload,wait_run
    calls=[]
    def model(*args,**kwargs):
        calls.append(kwargs['messages'])
        if len(calls)<3:
            return {'ok':True,'content':'','tool_calls':[{'id':'read-'+str(len(calls)),'type':'function','function':{'name':'read_stage','arguments':'{}'}}],'usage':{'total_tokens':50}}
        return {'ok':True,'content':'已核对论文上下文。','usage':{'total_tokens':50}}
    monkeypatch.setattr(app,'call_ai',model)
    pid=project(client);cid=conversation(client,pid)
    run=client.post(f'/api/projects/{pid}/agent/runs',json=payload(client,pid,cid)).json()
    assert wait_run(client,pid,run['id'])['status']=='succeeded'
    assert len(calls)==3 and all(sum(message['role']=='system' for message in messages)==1 for messages in calls)


def test_paper_request_replay_survives_profile_binding_upgrade(client,monkeypatch):
    from tests.test_agent_conversations import payload,wait_run
    from paperpilot.services import agent_conversations
    pid=project(client)
    cid=client.post(f'/api/projects/{pid}/agent/workspace',json={}).json()['root']['id']
    calls=[]
    monkeypatch.setattr(app,'call_ai',lambda *a,**k:calls.append(k) or {'ok':True,'content':'已保存的旧版论文回复','usage':{'total_tokens':30}})
    current=agent_conversations.bind_request
    monkeypatch.setattr(agent_conversations,'bind_request',lambda conn,pid,body:{**body,'research_profile':''})
    body=payload(client,pid,cid)
    first=client.post(f'/api/projects/{pid}/agent/runs',json=body)
    assert wait_run(client,pid,first.json()['id'])['status']=='succeeded'
    monkeypatch.setattr(agent_conversations,'bind_request',current)
    replay=client.post(f'/api/projects/{pid}/agent/runs',json=body)
    assert replay.status_code==200,replay.text
    assert replay.json()['id']==first.json()['id'] and len(calls)==1
