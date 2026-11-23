"""Budget, recovery and provenance invariants, without external model claims."""
import json
import time
import uuid
import pytest
import app
from paperpilot.services import agent_execution as execution,agent_runtime,agent_tools,agent_memory,agent_graph,agent_transport
from paperpilot.services import agent_conversations as store
from tests.test_agent_conversations import project,conversation,payload,wait_run


def queued(client,monkeypatch,**changes):
    monkeypatch.setattr(agent_runtime,'schedule',lambda *a:None)
    pid=project(client);cid=conversation(client,pid)
    response=client.post(f'/api/projects/{pid}/agent/runs',json=payload(client,pid,cid,**changes))
    assert response.status_code==200,response.text
    return pid,cid,response.json()['id']


def test_durable_call_replay_and_input_fence(client,monkeypatch):
    pid,cid,rid=queued(client,monkeypatch);conn=app.db()
    try:
        token=execution.claim(conn,rid);ledger=execution.Ledger(conn,rid,token);calls=[]
        value=ledger.invoke('once','model','chat',{'input':'hello'},lambda _:calls.append(1) or {'ok':True,'content':'known','usage':{'total_tokens':20}},output_tokens=20)
        assert ledger.invoke('once','model','chat',{'input':'hello'},lambda _:pytest.fail('replayed model call'))==value
        assert len(calls)==1 and ledger.usage()['model_calls']==1
        with pytest.raises(execution.ExecutionStopped,match='输入已变化'):
            ledger.invoke('once','model','chat',{'input':'changed'},lambda _: {})
    finally:conn.close()


def test_final_payload_tokens_and_call_count_are_enforced_before_send(client,monkeypatch):
    pid,cid,rid=queued(client,monkeypatch,limits={'model_calls':1,'tool_calls':1,'tokens':512,'seconds':300})
    conn=app.db()
    try:
        ledger=execution.Ledger(conn,rid,execution.claim(conn,rid))
        with pytest.raises(execution.ExecutionStopped):ledger.invoke('large','model','chat',{'messages':['汉'*500],'tools':[{'schema':'x'*100}]},lambda _:pytest.fail('over budget send'),output_tokens=50)
        assert ledger.usage()['model_calls']==0
        ledger.invoke('small','model','chat',{'input':'x'},lambda _:{'ok':True,'usage':{'total_tokens':10}},output_tokens=30)
        with pytest.raises(execution.ExecutionStopped):ledger.invoke('second','model','chat',{},lambda _:pytest.fail('extra call'))
    finally:conn.close()


def test_late_tool_result_is_recorded_but_not_accepted(client,monkeypatch):
    pid,cid,rid=queued(client,monkeypatch);conn=app.db()
    try:
        ledger=execution.Ledger(conn,rid,execution.claim(conn,rid))
        def slow(_):time.sleep(.03);return {'ok':True,'data':'late'}
        with pytest.raises(execution.ExecutionStopped) as exc:ledger.invoke('slow','tool','slow',{},slow,timeout=.01)
        assert exc.value.code=='tool_timeout'
        assert conn.execute("SELECT status FROM agent_call_ledger WHERE run_id=?",(rid,)).fetchone()[0]=='succeeded'
    finally:conn.close()


def test_orphan_recovery_preserves_unknown_call_and_does_not_dispatch(client,monkeypatch):
    pid,cid,rid=queued(client,monkeypatch);conn=app.db()
    try:
        with conn:
            conn.execute("UPDATE agent_runs SET status='running',owner_pid=0 WHERE id=?",(rid,))
            conn.execute("INSERT INTO agent_call_ledger(id,run_id,call_key,kind,name,input_hash,status,started_at) VALUES(?,?,?,'model','chat','hash','pending',?)",(uuid.uuid4().hex,rid,'unknown',store.stamp()))
        execution.recover_orphans(conn)
        assert conn.execute('SELECT status FROM agent_runs WHERE id=?',(rid,)).fetchone()[0]=='interrupted'
        assert conn.execute('SELECT status FROM agent_call_ledger WHERE run_id=?',(rid,)).fetchone()[0]=='unknown'
        assert client.post(f'/api/projects/{pid}/agent/runs/{rid}/resume/preview',json={}).status_code==409
    finally:conn.close()


def test_engine_resumes_next_node_without_repeating_completed_node():
    seen=[]
    def first(s,c):seen.append('first');return {'a':1},'second'
    def second(s,c):seen.append('second');return {'b':2,'_halt':'final'},None
    graph=agent_graph.Graph('recover',2,agent_graph.StateSchema({}),{'first':agent_graph.Node('first',first),'second':agent_graph.Node('second',second)},[], 'first')
    checkpoint=[];context=agent_graph.RunContext(checkpoint=lambda state,step,next_node:checkpoint.append((dict(state),next_node)))
    result=agent_graph.Engine(graph,context).run({},max_supersteps=1)
    assert checkpoint[-1][1]=='second'
    done=agent_graph.Engine(graph,context).run(result,max_supersteps=5)
    assert done['b']==2 and seen==['first','second']
    graph.version=3
    with pytest.raises(ValueError,match='incompatible'):agent_graph.Engine(graph,context).run(done)


def test_memory_requires_real_owned_completed_source_and_explicit_approval(client):
    a=project(client,'A');b=project(client,'B');conn=app.db()
    try:
        candidate={'kind':'semantic','title':'来源可核对的结论','content':{'idea':'材料约束需要核对'},'source_kind':'agent_run','source_id':'source'}
        with conn:
            assert not agent_memory.write(conn,a,candidate)['ok']
            conn.execute("INSERT INTO agent_runs(id,project_id,request_id,status,created_at,updated_at) VALUES('source',?,'source','succeeded',?,?)",(a,store.stamp(),store.stamp()))
            assert not agent_memory.write(conn,b,candidate)['ok']
            result=agent_memory.write(conn,a,candidate);assert result['ok']
        assert agent_memory.recall(conn,a,'材料')['items']==[]
        with conn:assert agent_memory.decide(conn,a,result['id'],'approve')['ok']
        assert agent_memory.recall(conn,a,'材料')['items']
        with conn:conn.execute("UPDATE agent_runs SET status='failed' WHERE id='source'")
        assert agent_memory.recall(conn,a,'材料')['items']==[]
    finally:conn.close()


def test_tool_schema_and_duplicate_registry_prevent_handler_execution():
    called=[];tool=agent_tools.Tool('schema_test','test',{'type':'object','properties':{'n':{'type':'integer','enum':[1,2]}},'required':['n'],'additionalProperties':False},lambda a,c:called.append(a) or {'ok':True})
    agent_tools.register(tool)
    try:
        for args in [{'n':True},{'n':3},{'extra':1}]:assert not agent_tools.call_tool({},tool.name,args)['ok']
        assert not called
        with pytest.raises(ValueError,match='duplicate'):agent_tools.register(tool)
    finally:agent_tools.REGISTRY.pop(tool.name,None)


def test_consent_is_reused_only_within_same_scope(client,monkeypatch):
    monkeypatch.setattr(app,'call_ai',lambda *a,**k:{'ok':True,'content':'已回答','usage':{'total_tokens':20}})
    pid=project(client);cid=conversation(client,pid)
    r=client.post(f'/api/projects/{pid}/agent/runs',json=payload(client,pid,cid));wait_run(client,pid,r.json()['id'])
    preview=client.post(f'/api/projects/{pid}/agent/preview',json={'conversation_id':cid,'instruction':'第二轮'}).json()
    assert not preview['authorization_required']
    changed=client.post(f'/api/projects/{pid}/agent/preview',json={'conversation_id':cid,'context_snapshot':{'scope':'full'}}).json()
    assert changed['authorization_required']


def test_history_tail_pagination_and_monotonic_events(client,monkeypatch):
    pid,cid,rid=queued(client,monkeypatch);conn=app.db()
    try:
        with conn:
            for i in range(15):store.append_message(conn,pid,cid,'user',f'history {i}')
        tail=store.messages(conn,pid,cid,limit=5,tail=True)
        assert len(tail['messages'])==5 and tail['has_previous']
        older=store.messages(conn,pid,cid,limit=5,before_seq=tail['messages'][0]['seq'])
        assert max(m['seq'] for m in older['messages'])<min(m['seq'] for m in tail['messages'])
        agent_runtime._event(conn,rid,'one',{});agent_runtime._event(conn,rid,'two',{})
        first=agent_runtime.get_run(conn,pid,rid);next_page=agent_runtime.get_run(conn,pid,rid,first['next_seq'])
        assert next_page['events']==[]
    finally:conn.close()


def test_additive_migration_is_idempotent_and_preserves_old_run(client):
    pid=project(client);conn=app.db()
    try:
        with conn:conn.execute("INSERT INTO agent_runs(id,project_id,request_id,status,created_at,updated_at) VALUES('legacy',?,'legacy','succeeded',?,?)",(pid,store.stamp(),store.stamp()))
        agent_runtime.initialize_schema(conn);agent_runtime.initialize_schema(conn);agent_memory.initialize_schema(conn)
        assert conn.execute("SELECT status FROM agent_runs WHERE id='legacy'").fetchone()[0]=='succeeded'
        assert len([r for r in conn.execute('PRAGMA table_info(agent_runs)') if r['name']=='conversation_id'])==1
    finally:conn.close()


def test_manual_resume_retries_only_known_failure_and_request_is_idempotent(client,monkeypatch):
    sent=[]
    def model(*a,**kw):
        sent.append(1)
        return {'ok':False,'error_code':'provider_connect_failed','error':'known unsent'} if len(sent)==1 else {'ok':True,'content':'恢复后的回答','usage':{'total_tokens':40}}
    monkeypatch.setattr(app,'call_ai',model)
    pid=project(client);cid=conversation(client,pid)
    first=client.post(f'/api/projects/{pid}/agent/runs',json=payload(client,pid,cid));rid=first.json()['id']
    assert wait_run(client,pid,rid)['status']=='failed'
    for _ in range(100):
        conn=app.db();lease=conn.execute('SELECT lease_token FROM agent_runs WHERE id=?',(rid,)).fetchone()[0];conn.close()
        if not lease:break
        time.sleep(.01)
    preview=client.post(f'/api/projects/{pid}/agent/runs/{rid}/resume/preview',json={});assert preview.status_code==200,preview.text
    body={'request_id':'resume-once','preview_fingerprint':preview.json()['preview_fingerprint'],'consent_to_send':True,'possible_cost_confirmed':True}
    result=client.post(f'/api/projects/{pid}/agent/runs/{rid}/resume',json=body);assert result.status_code==200,result.text
    assert wait_run(client,pid,rid)['status']=='succeeded'
    replay=client.post(f'/api/projects/{pid}/agent/runs/{rid}/resume',json=body)
    assert replay.status_code==200 and len(sent)==2


def test_owner_probe_never_terminates_live_process():
    import subprocess,sys
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
    try:
        assert execution.owner_alive(child.pid)
        assert child.poll() is None
    finally:
        child.terminate();child.wait(timeout=5)
    assert not execution.owner_alive(child.pid)


def test_extension_tool_card_reaches_persistent_conversation(client,monkeypatch):
    name='test_plugin_card'
    class Provider:
        def get_tools(self):
            return [agent_tools.Tool(name,'Plugin read',{'type':'object','properties':{},'additionalProperties':False},
                lambda args,ctx:{'ok':True,'cards':[{'id':'stable-plugin-card','kind':'plugin_fixture','title':'真实工具结果'}],'complete':True},intents=('research',))]
    agent_tools.register_provider(Provider())
    def model(*args,**kwargs):
        schemas=kwargs.get('tools') or []
        assert name in [v['function']['name'] for v in schemas]
        return {'ok':True,'content':'','tool_calls':[{'id':'p1','type':'function','function':{'name':name,'arguments':'{}'}}],'usage':{'total_tokens':25}}
    monkeypatch.setattr(app,'call_ai',model)
    try:
        pid=project(client);cid=conversation(client,pid)
        response=client.post(f'/api/projects/{pid}/agent/runs',json=payload(client,pid,cid))
        run=wait_run(client,pid,response.json()['id']);assert run['status']=='succeeded',run
        messages=client.get(f'/api/projects/{pid}/agent/conversations/{cid}/messages').json()['messages']
        card=messages[-1]['cards'][0]
        assert card['id']=='stable-plugin-card' and card['kind']=='plugin_fixture' and card['project_id']==pid
        assert set(('status','title','summary','source_refs','target','base_version','limitations','actions'))<=set(card)
        assert run['result']['cards'][0]==card
    finally:agent_tools.REGISTRY.pop(name,None)


def test_real_http_transport_respects_request_deadline():
    from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
    import threading
    from paperpilot.adapters.openai_compat import call_chat_completion
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            self.rfile.read(int(self.headers.get('Content-Length',0)))
            time.sleep(.3)
            try:
                self.send_response(200);self.end_headers();self.wfile.write(b'{}')
            except (BrokenPipeError,ConnectionResetError,ConnectionAbortedError):pass
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:
        started=time.monotonic()
        with agent_transport.deadline(.05):
            result=call_chat_completion('write','','deadline','','fixture',f'http://127.0.0.1:{server.server_port}/v1',environ={'PWA_AI_TIMEOUT':'5'})
        assert not result['ok'] and result['error_code']=='provider_result_unknown'
        assert time.monotonic()-started<.8
        assert not agent_transport.expired()  # context reset does not affect unrelated requests
    finally:server.shutdown();server.server_close();worker.join(2)


def test_simultaneous_identical_admission_has_one_run(client,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    monkeypatch.setattr(agent_runtime,'schedule',lambda *a:None)
    pid=project(client);cid=conversation(client,pid);body=payload(client,pid,cid)
    original=agent_runtime.preview;barrier=threading.Barrier(2)
    def both_preview(conn,pid,inputs):
        value=original(conn,pid,inputs);barrier.wait(timeout=5);return value
    monkeypatch.setattr(agent_runtime,'preview',both_preview)
    with ThreadPoolExecutor(2) as pool:
        futures=[pool.submit(client.post,f'/api/projects/{pid}/agent/runs',json=body) for _ in range(2)]
        responses=[f.result(timeout=15) for f in futures]
    assert all(r.status_code==200 for r in responses),[r.text for r in responses]
    assert responses[0].json()['id']==responses[1].json()['id']
    conn=app.db()
    try:assert conn.execute('SELECT COUNT(*) FROM agent_call_ledger').fetchone()[0]==0
    finally:conn.close()


def test_request_lookup_recovers_committed_turn_and_is_project_owned(client,monkeypatch):
    pid,cid,rid=queued(client,monkeypatch)
    conn=app.db();request=conn.execute('SELECT request_id FROM agent_turns WHERE run_id=?',(rid,)).fetchone()[0];conn.close()
    endpoint=f'/api/projects/{pid}/agent/conversations/{cid}/requests/{request}'
    assert client.get(endpoint).json()['run']['id']==rid
    assert client.get(endpoint+'missing').json()['run'] is None
    other=project(client,'other')
    assert client.get(endpoint.replace(pid,other)).status_code==404
    assert client.post(f'/api/projects/{pid}/agent/runs/{rid}/cancel',json={}).status_code==200
    messages=client.get(f'/api/projects/{pid}/agent/conversations/{cid}/messages').json()['messages']
    assert messages[-1]['role']=='assistant' and '停止' in messages[-1]['text']


def test_exhausted_budget_cannot_be_resumed(client,monkeypatch):
    pid,cid,rid=queued(client,monkeypatch);conn=app.db()
    try:
        with conn:conn.execute("UPDATE agent_runs SET status='blocked',budget_json=? WHERE id=?",(json.dumps({'model_calls':8}),rid))
        result=client.post(f'/api/projects/{pid}/agent/runs/{rid}/resume/preview',json={})
        assert result.status_code==409 and result.json()['detail']['code']=='budget_exhausted'
    finally:conn.close()
