"""Integrated 01/03 boundaries and additive-schema upgrade preservation."""
import json
import sqlite3
import uuid
import app
from paperpilot.services import agent_runtime
from tests.test_agent_conversations import project, conversation, payload, wait_run


def test_native_run_binds_business_proposal_and_persists_application_receipt(client,monkeypatch):
    pid=project(client);cid=conversation(client,pid)
    revision=client.get(f'/api/projects/{pid}').json()['project']['project_revision']
    turns=[]
    def model(*args,**kwargs):
        turns.append(1)
        if len(turns)==1:
            return {'ok':True,'content':'','tool_calls':[{'id':'business-one','type':'function','function':{'name':'propose_business','arguments':json.dumps({'operation':'term','base_version':revision,'payload':{'term':'Native term','definition':'Explicitly reviewed'}})}}],'usage':{'total_tokens':80}}
        return {'ok':True,'content':'请审阅术语提案。','usage':{'total_tokens':30}}
    monkeypatch.setattr(app,'call_ai',model)
    request=payload(client,pid,cid,'请提出研究术语定义，等待我明确应用')
    response=client.post(f'/api/projects/{pid}/agent/runs',json=request)
    assert response.status_code==200,response.text
    rid=response.json()['id'];done=wait_run(client,pid,rid)
    assert done['status']=='succeeded',done
    cards=client.get(f'/api/projects/{pid}/agent/actions',params={'conversation_id':cid}).json()['cards']
    assert len(cards)==1 and cards[0]['run_id']==rid and cards[0]['conversation_id']==cid
    assert not client.get(f'/api/projects/{pid}').json()['terms']
    action=cards[0]['id'];base=f'/api/projects/{pid}/agent/actions/{action}'
    preview=client.post(base+'/preview',json={}).json()
    applied=client.post(base+'/apply',json={'request_id':uuid.uuid4().hex,'accepted':True,'expected_version':preview['base_version'],'action_fingerprint':preview['action_fingerprint']})
    assert applied.status_code==200,applied.text
    messages=client.get(f'/api/projects/{pid}/agent/conversations/{cid}/messages').json()['messages']
    assert '应用回执' in json.dumps(messages,ensure_ascii=False)
    other=conversation(client,pid)
    assert client.get(f'/api/projects/{pid}/agent/actions',params={'conversation_id':other}).json()['cards']==[]


def test_upgrade_copied_legacy_store_twice_preserves_semantic_rows(client,tmp_path,monkeypatch):
    pid=project(client)
    old=app.db()
    # The previous release has no action receipts or conversation ledger tables.
    drop=['agent_action_receipts','agent_action_events','agent_actions','research_design_applications','agent_call_ledger','agent_run_attempts','agent_messages','agent_conversations']
    with old:
        old.execute('PRAGMA foreign_keys=OFF')
        for table in drop:old.execute('DROP TABLE IF EXISTS '+table)
    preserved=['projects','files','outline_versions','logic_graph_versions','corpus','terms','refs']
    names={r[0] for r in old.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    preserved=[name for name in preserved if name in names]
    def rows(conn):return {name:[tuple(r) for r in conn.execute('SELECT * FROM '+name+' ORDER BY rowid')] for name in preserved}
    before=rows(old)
    copy=tmp_path/'upgrade-copy.sqlite3';target=sqlite3.connect(copy);old.backup(target);target.close();old.close()
    monkeypatch.setattr(app,'DB_PATH',copy)
    app.init_db();app.init_db()
    upgraded=app.db()
    try:
        assert rows(upgraded)==before
        assert upgraded.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert upgraded.execute("SELECT 1 FROM sqlite_master WHERE name='agent_actions'").fetchone()
    finally:upgraded.close()


def test_same_project_run_cannot_attribute_proposal_to_another_conversation(client,monkeypatch):
    monkeypatch.setattr(agent_runtime,'schedule',lambda *args:None)
    pid=project(client);first=conversation(client,pid);other=conversation(client,pid)
    run=client.post(f'/api/projects/{pid}/agent/runs',json=payload(client,pid,first))
    assert run.status_code==200,run.text
    revision=client.get(f'/api/projects/{pid}').json()['project']['project_revision']
    response=client.post(f'/api/projects/{pid}/agent/actions',json={
        'request_id':uuid.uuid4().hex,'run_id':run.json()['id'],'conversation_id':other,
        'operation':'term','base_version':revision,'payload':{'term':'Wrong conversation','definition':'Must not be stored'}})
    assert response.status_code==409 and response.json()['detail']['code']=='target_changed'
    assert client.get(f'/api/projects/{pid}/agent/actions',params={'conversation_id':other}).json()['cards']==[]
