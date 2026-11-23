"""Deterministic conversation/race tests. They are not real-provider evidence."""
import json
import sqlite3
import threading
import time
import uuid
import pytest
import app
from paperpilot.services import agent_runtime,agent_execution,agent_memory,agent_tools
from paperpilot.services import agent_conversations as store
from tests.test_req022_assets import project


def conversation(client,pid):
    r=client.post(f'/api/projects/{pid}/agent/conversations',json={'title':'论文讨论'})
    assert r.status_code==200,r.text
    return r.json()['id']


def payload(client,pid,cid,instruction='请解释这个研究问题',**changes):
    body={'conversation_id':cid,'instruction':instruction}|changes
    r=client.post(f'/api/projects/{pid}/agent/preview',json=body)
    assert r.status_code==200,r.text
    return body|{'request_id':uuid.uuid4().hex,'preview_fingerprint':r.json()['preview_fingerprint'],
                 'consent_to_send':True,'possible_cost_confirmed':True}


def wait_run(client,pid,rid):
    end=time.monotonic()+60
    while time.monotonic()<end:
        r=client.get(f'/api/projects/{pid}/agent/runs/{rid}');assert r.status_code==200,r.text
        if r.json()['status'] not in ('queued','running'):return r.json()
        time.sleep(.02)
    raise AssertionError('run did not settle')


def test_two_turns_persist_visible_history_and_plain_text(client,monkeypatch):
    calls=[]
    def model(*a,**kw):
        calls.append(kw['messages'])
        return {'ok':True,'content':'第一轮的可见结论：保留实验边界。' if len(calls)==1 else '第二轮沿用实验边界。','usage':{'total_tokens':80}}
    monkeypatch.setattr(app,'call_ai',model)
    pid=project(client);cid=conversation(client,pid)
    for question in ['解释实验边界','沿用刚才结论再简化']:
        r=client.post(f'/api/projects/{pid}/agent/runs',json=payload(client,pid,cid,question));assert r.status_code==200,r.text
        done=wait_run(client,pid,r.json()['id']);assert done['status']=='succeeded',done
    assert '第一轮的可见结论' in json.dumps(calls[1],ensure_ascii=False)
    history=client.get(f'/api/projects/{pid}/agent/conversations/{cid}/messages').json()
    assert len(history['messages'])==4 and history['active_run'] is None
    assert all('思考' not in e['payload'] for e in done['events'])


def test_paper_workspace_adopts_history_and_initializes_idempotently(client):
    pid=project(client)
    cid=conversation(client,pid)
    conn=app.db()
    with conn:
        store.append_message(conn,pid,cid,'user','已有论文目标')
    conn.close()
    first=client.post(f'/api/projects/{pid}/agent/workspace',json={}).json()
    second=client.post(f'/api/projects/{pid}/agent/workspace',json={}).json()
    assert first==second
    assert first['root']['id']==cid
    assert {child['module_key'] for child in first['children']}==set(store.MODULES)
    assert all(child['parent_id']==cid for child in first['children'])
    assert client.get(f'/api/projects/{pid}/agent/conversations/{cid}/messages').json()['messages'][0]['text']=='已有论文目标'


def test_old_schema_migration_retains_conversation_and_messages():
    conn=sqlite3.connect(':memory:');conn.row_factory=sqlite3.Row
    conn.execute("CREATE TABLE agent_conversations(id TEXT PRIMARY KEY,project_id TEXT NOT NULL,title TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0,consent_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,updated_at TEXT NOT NULL)")
    conn.execute("INSERT INTO agent_conversations VALUES('old','paper','旧讨论',0,'{}','2026','2026')")
    conn.commit()
    agent_runtime.initialize_schema(conn)
    with conn:store.append_message(conn,'paper','old','user','旧版作者原始目标')
    agent_runtime.initialize_schema(conn)
    first=store.workspace(conn,'paper',ensure=True)
    assert first['root']['id']=='old'
    assert store.messages(conn,'paper','old')['messages'][0]['text']=='旧版作者原始目标'
    assert store.workspace(conn,'paper',ensure=True)==first
    conn.close()


def test_subagent_identity_and_related_context_reach_real_runtime(client,monkeypatch):
    calls=[]
    monkeypatch.setattr(app,'call_ai',lambda *a,**k:calls.append(k['messages']) or {'ok':True,'content':'模块产出候选。','usage':{'total_tokens':40}})
    pid=project(client)
    workspace=client.post(f'/api/projects/{pid}/agent/workspace',json={}).json()
    root=workspace['root']['id']
    child=next(c for c in workspace['children'] if c['module_key']=='outline')
    conn=app.db()
    with conn:store.append_message(conn,pid,root,'user','总目标约束：只使用作者已有材料')
    conn.close()
    body=payload(client,pid,child['id'],'整理章节目标',stage_id='STAGE-05',research_profile='scholar_apprentice')
    run=client.post(f'/api/projects/{pid}/agent/runs',json=body)
    assert run.status_code==200,run.text
    assert wait_run(client,pid,run.json()['id'])['status']=='succeeded'
    system=calls[0][0]['content']
    assert '章节大纲' in system and '子 Agent' in system and '当前是 scholar-apprentice' not in system
    assert '总目标约束' in json.dumps(calls[0],ensure_ascii=False)
    assert len(client.get(f'/api/projects/{pid}/agent/conversations/{root}/messages').json()['messages'])==1
    preview=client.post(f'/api/projects/{pid}/agent/preview',json={'conversation_id':root}).json()
    assert any(c['id']==child['id'] and c['revision']==2 for c in preview['context_manifest']['related_conversations'])
    conn=app.db()
    state=json.loads(conn.execute('SELECT state_json FROM agent_runs WHERE id=?',(run.json()['id'],)).fetchone()[0])
    assert state['stage_id']=='STAGE-02' and state['research_profile']==''
    conn.close()


def test_workspace_parent_project_isolation_and_stale_progress(client):
    a=project(client,'A');b=project(client,'B')
    workspace=client.post(f'/api/projects/{a}/agent/workspace',json={}).json()
    root=workspace['root']['id'];child=workspace['children'][0]['id']
    assert client.post(f'/api/projects/{b}/agent/conversations',json={'parent_id':root,'module_key':'outline'}).status_code==404
    assert client.post(f'/api/projects/{a}/agent/conversations',json={'parent_id':child,'module_key':'outline'}).status_code==422
    original=client.post(f'/api/projects/{a}/agent/preview',json={'conversation_id':root}).json()
    conn=app.db()
    with conn:store.append_message(conn,a,child,'assistant','一个新模块进展')
    conn.close()
    changed=client.post(f'/api/projects/{a}/agent/preview',json={'conversation_id':root}).json()
    assert original['preview_fingerprint']!=changed['preview_fingerprint']
    assert original['authorization_fingerprint']==changed['authorization_fingerprint']


def test_legacy_turn_replay_survives_paper_root_adoption(client,monkeypatch):
    calls=[]
    monkeypatch.setattr(app,'call_ai',lambda *a,**k:calls.append(k) or {'ok':True,'content':'旧对话回复','usage':{'total_tokens':20}})
    pid=project(client);cid=conversation(client,pid)
    body=payload(client,pid,cid)
    first=client.post(f'/api/projects/{pid}/agent/runs',json=body)
    assert wait_run(client,pid,first.json()['id'])['status']=='succeeded'
    assert client.post(f'/api/projects/{pid}/agent/workspace',json={}).json()['root']['id']==cid
    replay=client.post(f'/api/projects/{pid}/agent/runs',json=body)
    assert replay.status_code==200,replay.text
    assert replay.json()['id']==first.json()['id'] and len(calls)==1


def test_same_request_replays_without_model_send_and_rejects_different_input(client,monkeypatch):
    sent=[];monkeypatch.setattr(app,'call_ai',lambda *a,**k:sent.append(1) or {'ok':True,'content':'结果','usage':{'total_tokens':20}})
    pid=project(client);cid=conversation(client,pid);body=payload(client,pid,cid)
    first=client.post(f'/api/projects/{pid}/agent/runs',json=body);assert first.status_code==200,first.text
    wait_run(client,pid,first.json()['id'])
    replay=client.post(f'/api/projects/{pid}/agent/runs',json=body)
    assert replay.status_code==200 and replay.json()['id']==first.json()['id'] and len(sent)==1
    changed=client.post(f'/api/projects/{pid}/agent/runs',json=body|{'instruction':'different'})
    assert changed.status_code==409 and len(sent)==1


def test_project_conversation_and_target_isolation(client):
    a=project(client,'A');b=project(client,'B');cid=conversation(client,a)
    assert client.get(f'/api/projects/{b}/agent/conversations/{cid}/messages').status_code==404
    r=client.post(f'/api/projects/{a}/agent/preview',json={'conversation_id':cid,'context_snapshot':{'project_id':b}})
    assert r.status_code==409


def test_cancel_fences_late_success_and_releases_conversation(client,monkeypatch):
    entered=threading.Event();release=threading.Event()
    def model(*a,**kw):
        entered.set();assert release.wait(6)
        return {'ok':True,'content':'late result','usage':{'total_tokens':40}}
    monkeypatch.setattr(app,'call_ai',model)
    pid=project(client);cid=conversation(client,pid)
    r=client.post(f'/api/projects/{pid}/agent/runs',json=payload(client,pid,cid));assert r.status_code==200,r.text
    assert entered.wait(3)
    try:
        busy=client.post(f'/api/projects/{pid}/agent/runs',json=payload(client,pid,cid,'another turn'))
        assert busy.status_code==409
        assert client.post(f'/api/projects/{pid}/agent/runs/{r.json()["id"]}/cancel',json={}).json()['status']=='cancelled'
    finally:release.set()
    end=time.monotonic()+3
    while time.monotonic()<end:
        conn=app.db();row=conn.execute('SELECT lease_token FROM agent_runs WHERE id=?',(r.json()['id'],)).fetchone();conn.close()
        if not row['lease_token']:break
        time.sleep(.02)
    done=wait_run(client,pid,r.json()['id']);assert done['status']=='cancelled'


def test_unknown_model_result_blocks_automatic_resume(client,monkeypatch):
    sent=[];monkeypatch.setattr(app,'call_ai',lambda *a,**k:sent.append(1) or {'ok':False,'error_code':'provider_result_unknown','error':'unknown'})
    pid=project(client);cid=conversation(client,pid)
    r=client.post(f'/api/projects/{pid}/agent/runs',json=payload(client,pid,cid));assert r.status_code==200,r.text
    done=wait_run(client,pid,r.json()['id']);assert done['status']=='blocked',done
    assert client.post(f'/api/projects/{pid}/agent/runs/{r.json()["id"]}/resume/preview',json={}).status_code==409
    assert len(sent)==1


def test_no_authorization_no_send_and_preview_changed(client,monkeypatch):
    sent=[];monkeypatch.setattr(app,'call_ai',lambda *a,**k:sent.append(1) or {})
    pid=project(client);cid=conversation(client,pid);body=payload(client,pid,cid)
    r=client.post(f'/api/projects/{pid}/agent/runs',json=body|{'consent_to_send':False})
    assert r.status_code==409 and not sent
    r=client.post(f'/api/projects/{pid}/agent/runs',json=body|{'instruction':'changed'})
    assert r.status_code==409 and not sent


def test_nested_draft_calls_share_model_budget(client,monkeypatch):
    sent=[]
    def model(*a,**kw):
        sent.append(kw)
        return {'ok':True,'content':'','tool_calls':[{'id':'draft-call','type':'function','function':{'name':'write_draft','arguments':json.dumps({'instruction':'起草引言','content':'真实研究问题和作者已有材料。'})}}],'usage':{'total_tokens':40}}
    monkeypatch.setattr(app,'call_ai',model)
    pid=project(client);cid=conversation(client,pid)
    body=payload(client,pid,cid,'起草引言',limits={'model_calls':1,'tool_calls':20,'tokens':40000,'seconds':300})
    r=client.post(f'/api/projects/{pid}/agent/runs',json=body);assert r.status_code==200,r.text
    done=wait_run(client,pid,r.json()['id']);assert done['status']=='blocked',done
    assert len(sent)==1 and done['error_code']=='budget_exhausted'


def test_outline_tool_proposes_without_writing(client):
    pid=project(client);conn=app.db()
    try:
        result=agent_tools.call_tool({},'edit_outline_nodes',{'command':'create','base_version':0,'node':{'title':'新章节'}},context={'conn':conn,'project_id':pid})
        assert result['ok'],result
        assert result['data']['proposal']['kind']=='outline_proposal'
        assert conn.execute('SELECT COUNT(*) FROM outline_versions WHERE project_id=?',(pid,)).fetchone()[0]==0
    finally:conn.close()
