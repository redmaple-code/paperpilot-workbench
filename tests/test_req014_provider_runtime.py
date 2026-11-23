"""Actual protected store and local HTTP transport; not a cloud quality claim."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import base64
from io import BytesIO
from PIL import Image
import os
import threading
import uuid
import pytest
import requests
import app
from paperpilot.adapters.openai_compat import call_chat_completion
from paperpilot.services import credential_vault as vault, provider_config as providers
from paperpilot.services.project_errors import ProjectServiceError
from test_req001 import enter_revision


@contextmanager
def local_provider(*,support_stream=False):
    calls=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_GET(self):
            payload=json.dumps({'data':[{'id':'test-model'},{'id':'unregistered-discovery'}]}).encode()
            self.send_response(200);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
        def do_POST(self):
            raw=self.rfile.read(int(self.headers.get('Content-Length','0')))
            body=json.loads(raw) if self.headers.get('Content-Type','').startswith('application/json') else {'multipart_received':True}
            calls.append({'path':self.path,'auth':self.headers.get('Authorization'),'body':body})
            if self.path.startswith('/redirect'):
                self.send_response(307);self.send_header('Location','/other/chat/completions');self.end_headers();return
            if self.path.startswith('/unauthorized'):
                self.send_response(401);self.end_headers();self.wfile.write(b'private-provider-body-never-return');return
            stream=support_stream and body.get('stream')
            if '/images/' in self.path:
                buffer=BytesIO();Image.new('RGB',(8,8),'blue').save(buffer,format='PNG')
                response={'data':[{'b64_json':base64.b64encode(buffer.getvalue()).decode()}],'usage':{'images_generated':1}}
            elif isinstance((body.get('messages') or [{}])[0].get('content'),list):
                response={'choices':[{'message':{'content':json.dumps({'findings':[{'check':'readability','status':'unknown','message':'Local protocol fixture only.','location':{},'limitations':'Not a model quality assessment.'}]})}}]}
            else:response={'choices':[{'message':{'content':'Local protocol response.'}}],'usage':{'prompt_tokens':10,'completion_tokens':5}}
            payload=('data: '+json.dumps({'choices':[{'delta':{'content':'OK'}}]})+'\n\ndata: [DONE]\n\n').encode() if stream else json.dumps(response).encode()
            self.send_response(200);self.send_header('Content-Type','text/event-stream' if stream else 'application/json');self.send_header('Content-Length',str(len(payload)));self.end_headers();self.wfile.write(payload)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    try:yield f'http://127.0.0.1:{server.server_port}',calls
    finally:server.shutdown();server.server_close();thread.join(3)


def profile(client,base,auth='none',secret=''):
    response=client.post('/api/ai/profiles',json={'name':'runtime provider','base_url':base,'auth_mode':auth,'api_key':secret,'models':['test-model'],'request_id':uuid.uuid4().hex})
    assert response.status_code==200,response.text
    return response.json()


@pytest.mark.skipif(os.name!='nt',reason='actual Windows current-user DPAPI check')
def test_actual_dpapi_store_resolves_only_owner_and_never_returns_plaintext(client):
    secret='fixture-secret-not-a-user-key'
    p=profile(client,'https://provider.invalid/v1','api_key',secret)
    other=profile(client,'https://other.invalid/v1','api_key','another-fixture-key')
    assert secret not in json.dumps(p) and secret not in client.get('/api/ai/profiles').text
    conn=app.db()
    row=conn.execute('SELECT * FROM ai_provider_credentials WHERE profile_id=?',(p['id'],)).fetchone()
    assert secret.encode() not in bytes(row['protected_value'])
    assert vault.load(conn,p['id'],row['id'])==secret
    with pytest.raises(ProjectServiceError):vault.load(conn,other['id'],row['id'])
    runtime=providers.resolve_credentials(conn,p['id'],1)
    assert runtime.key==secret and secret not in repr(runtime)
    assert all(secret not in r[0] for r in conn.execute('SELECT config_json FROM ai_provider_profile_versions'))
    with vault.scoped(runtime):assert vault.current(runtime.base_url).key==secret
    assert vault.current(runtime.base_url) is None
    conn.close()


def test_protection_failure_does_not_commit_profile_or_plaintext(client,monkeypatch):
    def unavailable(secret):raise ProjectServiceError(503,'credential_protection_unavailable','Protection unavailable; not saved.')
    monkeypatch.setattr(vault,'protect',unavailable)
    result=client.post('/api/ai/profiles',json={'name':'blocked','base_url':'https://protected.invalid/v1','auth_mode':'api_key','api_key':'temporary-fixture-secret','request_id':'blocked','models':['x']})
    assert result.status_code==503 and 'temporary-fixture-secret' not in result.text
    conn=app.db();assert conn.execute("SELECT COUNT(*) FROM ai_provider_profiles WHERE request_id='blocked'").fetchone()[0]==0;assert conn.execute('SELECT COUNT(*) FROM ai_provider_credentials').fetchone()[0]==0;conn.close()


def test_strict_provider_urls_and_remote_no_auth_rejected_without_network(client):
    for url in ['http://remote.invalid/v1','https://user:pass@host.invalid/v1','https://host.invalid/v1?key=value','https://host.invalid/v1#key','http://[broken','https://host.invalid:bad/v1','https://@host.invalid/v1']:
        result=client.post('/api/ai/profiles',json={'name':'invalid','base_url':url,'auth_mode':'none','models':['x'],'request_id':uuid.uuid4().hex})
        assert result.status_code==422,(url,result.text)
    result=client.post('/api/ai/profiles',json={'name':'invalid','base_url':'https://remote.invalid/v1','auth_mode':'none','models':['x'],'request_id':'no-auth-remote'})
    assert result.status_code==422 and result.json()['detail']['code']=='local_auth_required'
    result=client.post('/api/ai/profiles',json={'name':'unsupported','base_url':'http://127.0.0.1/v1','auth_mode':'none','models':['m'],'request_id':'headers','headers':{'x-api-key':'never-echo-extra-secret'}})
    assert result.status_code==422 and 'never-echo-extra-secret' not in result.text


def test_actual_local_transport_never_receives_environment_cloud_key(client,monkeypatch):
    monkeypatch.setenv('PWA_AI_API_KEY','cloud-fixture-secret')
    monkeypatch.setenv('PWA_AI_BASE_URL','https://cloud.invalid/v1')
    with local_provider() as (url,calls):
        p=profile(client,url+'/v1')
        conn=app.db();runtime=providers.resolve_credentials(conn,p['id']);conn.close()
        with vault.scoped(runtime):result=call_chat_completion('write','','test','','test-model',runtime.base_url)
        assert result['ok'] and len(calls)==1 and calls[0]['auth'] is None
        # Even the legacy adapter must not attach an unrelated global key locally.
        assert call_chat_completion('write','','test','','test-model',url+'/v1')['ok']
        assert calls[1]['auth'] is None


@pytest.mark.skipif(os.name!='nt',reason='actual Windows protected credential integration')
def test_actual_local_bearer_transport_uses_selected_key_and_rejects_redirect(client):
    with local_provider() as (url,calls):
        p=profile(client,url+'/v1','api_key','selected-fixture-key')
        conn=app.db();runtime=providers.resolve_credentials(conn,p['id']);conn.close()
        with vault.scoped(runtime):result=call_chat_completion('write','','test','','test-model',url+'/v1')
        assert result['ok'] and calls[-1]['auth']=='Bearer selected-fixture-key'
        p=profile(client,url+'/redirect','api_key','redirect-fixture-key')
        conn=app.db();runtime=providers.resolve_credentials(conn,p['id']);conn.close()
        with vault.scoped(runtime):result=call_chat_completion('write','','test','','test-model',url+'/redirect')
        assert result['error_code']=='provider_redirect_rejected'
        assert len(calls)==2


def test_endpoint_mismatch_no_send_and_unknown_result_no_automatic_retry(client,monkeypatch):
    calls=[]
    result=call_chat_completion('write','','test','','model','https://other.invalid/v1',environ={'PWA_AI_BASE_URL':'https://one.invalid/v1','PWA_AI_API_KEY':'one-key'},request_post=lambda *a,**k:calls.append(1))
    assert result['error_code']=='credential_endpoint_mismatch' and not calls
    def timeout(*args,**kwargs):calls.append(1);raise requests.ReadTimeout('private error body must not escape')
    result=call_chat_completion('write','','test','','model','http://127.0.0.1/v1',environ={},request_post=timeout)
    assert result['error_code']=='provider_result_unknown' and len(calls)==1
    assert 'private error body' not in json.dumps(result)


@pytest.mark.skipif(os.name!='nt',reason='actual DPAPI rotation check')
def test_rotation_keeps_immutable_key_reference_and_requires_new_endpoint_key(client):
    p=profile(client,'https://selected.invalid/v1','api_key','first-fixture-key')
    changed=client.put(f"/api/ai/profiles/{p['id']}",json={'base_version':1,'changes':{'api_key':'second-fixture-key'}})
    assert changed.status_code==200,changed.text
    conn=app.db()
    assert providers.resolve_credentials(conn,p['id'],1).key=='first-fixture-key'
    assert providers.resolve_credentials(conn,p['id'],2).key=='second-fixture-key'
    conn.close()
    result=client.put(f"/api/ai/profiles/{p['id']}",json={'base_version':2,'changes':{'base_url':'https://different.invalid/v1'}})
    assert result.status_code==422
    assert result.json()['detail']['code']=='api_key_required'


def test_explicit_real_generation_test_is_required_and_idempotent(client):
    with local_provider() as (url,calls):
        p=profile(client,url+'/v1')
        assert client.post(f"/api/ai/profiles/{p['id']}/test").json()['status']=='configuration_valid_only'
        assert not calls
        assert client.put(f"/api/ai/profiles/{p['id']}",json={'base_version':1,'changes':{'status':'enabled'}}).status_code==409
        payload={'request_id':'actual-generation','expected_version':1,'model_id':'test-model','mode':'chat','consent_to_send':True,'possible_cost_confirmed':False}
        assert client.post(f"/api/ai/profiles/{p['id']}/test",json=payload).status_code==422
        assert not calls
        payload['possible_cost_confirmed']=True
        response=client.post(f"/api/ai/profiles/{p['id']}/test",json=payload)
        assert response.status_code==200,response.text
        tests=client.get(f"/api/ai/profiles/{p['id']}/tests").json()
        assert tests[0]['status']=='ready',tests
        assert len(calls)==1 and 'No manuscript' in calls[0]['body']['messages'][1]['content']
        assert client.post(f"/api/ai/profiles/{p['id']}/test",json=payload).json()['idempotent_replay']
        assert len(calls)==1
        enabled=client.put(f"/api/ai/profiles/{p['id']}",json={'base_version':1,'changes':{'status':'enabled'}})
        assert enabled.status_code==200,enabled.text
        assert enabled.json()['status']=='enabled'


def test_stream_not_inferred_from_chat_and_temporary_test_key_is_not_persisted(client):
    with local_provider() as (url,calls):
        p=profile(client,url+'/v1','temporary')
        response=client.post(f"/api/ai/profiles/{p['id']}/test",json={'request_id':'temporary-stream','expected_version':1,'model_id':'test-model','mode':'stream','consent_to_send':True,'possible_cost_confirmed':True,'temporary_key':'one-use-fixture-key'})
        assert response.status_code==200,response.text
        state=client.get(f"/api/ai/profiles/{p['id']}/tests").json()[0]
        assert state['status']=='failed' and state['error_code']=='provider_stream_unsupported'
        assert calls[0]['auth']=='Bearer one-use-fixture-key'
        conn=app.db()
        assert not conn.execute('SELECT 1 FROM ai_provider_credentials WHERE profile_id=?',(p['id'],)).fetchone()
        assert 'one-use-fixture-key' not in ' '.join(str(tuple(r)) for r in conn.execute('SELECT * FROM ai_provider_tests'))
        with pytest.raises(ProjectServiceError):providers.resolve_credentials(conn,p['id'])
        conn.close()


def enable_real_provider(client,url,*,bounds=True):
    p=profile(client,url)
    if bounds:
        changed=client.put(f"/api/ai/profiles/{p['id']}",json={'base_version':1,'changes':{'model_limits':{'test-model':{'input_tokens':100000,'output_tokens':10000,'source':'local HTTP protocol test capacity fixture'}}}})
        assert changed.status_code==200,changed.text
        p=changed.json()
    response=client.post(f"/api/ai/profiles/{p['id']}/test",json={'request_id':uuid.uuid4().hex,'expected_version':p['current_version'],'model_id':'test-model','mode':'chat','consent_to_send':True,'possible_cost_confirmed':True})
    assert response.status_code==200,response.text
    assert client.get(f"/api/ai/profiles/{p['id']}/tests").json()[0]['status']=='ready'
    enabled=client.put(f"/api/ai/profiles/{p['id']}",json={'base_version':p['current_version'],'changes':{'status':'enabled'}})
    assert enabled.status_code==200,enabled.text
    return enabled.json()


def bind(client,pid,p):
    revision=client.get(f'/api/projects/{pid}').json()['project']['project_revision']
    response=client.put(f'/api/projects/{pid}/ai-binding',json={'profile_id':p['id'],'model_id':'test-model','base_project_revision':revision})
    assert response.status_code==200,response.text


def queue_authoring(client,pid,**extra):
    file=client.get(f'/api/projects/{pid}').json()['files'][0]
    preview=client.post(f'/api/projects/{pid}/ai-context',json={'action':'edit','target_kind':'file','target_path':file['path'],'content':file['content'],'selection':'Introduction','prompt':'Edit this heading.'})
    assert preview.status_code==200,preview.text
    start=file['content'].index('Introduction')
    return client.post(f'/api/projects/{pid}/ai-jobs',json={'action':'edit','request_id':uuid.uuid4().hex,'prompt':'Edit this heading.','selection':'Introduction','scope':{'targets':[{'kind':'file','path':file['path'],'start':start,'end':start+len('Introduction')}]},'base_file_versions':{file['path']:file['version']},'context_version':preview.json()['version'],**extra})


def test_queued_real_request_keeps_recipient_when_paper_switches(client,monkeypatch):
    with local_provider() as (first_url,first_calls),local_provider() as (second_url,second_calls):
        provider=enable_real_provider(client,first_url+'/v1')
        pid=client.post('/api/projects',json={'name':'immutable-recipient'}).json()['project']['id'];enter_revision(client,pid);bind(client,pid,provider)
        worker=app.process_ai_job;monkeypatch.setattr(app,'process_ai_job',lambda *args:None)
        queued=queue_authoring(client,pid)
        assert queued.status_code==202,queued.text
        jobid=queued.json()['job']['id']
        changed=client.put(f"/api/ai/profiles/{provider['id']}",json={'base_version':provider['current_version'],'changes':{'base_url':second_url+'/v1'}})
        assert changed.status_code==200 and changed.json()['status']=='draft'
        old=client.get(f'/api/projects/{pid}').json()['ai_binding']
        assert old['base_url']==first_url+'/v1' and old['status']=='enabled'
        tested=client.post(f"/api/ai/profiles/{provider['id']}/test",json={'request_id':'new-recipient-test','expected_version':changed.json()['current_version'],'model_id':'test-model','mode':'chat','consent_to_send':True,'possible_cost_confirmed':True})
        assert tested.status_code==200,tested.text
        latest=client.put(f"/api/ai/profiles/{provider['id']}",json={'base_version':changed.json()['current_version'],'changes':{'status':'enabled'}})
        assert latest.status_code==200,latest.text
        bind(client,pid,latest.json())
        worker(pid,jobid)
        assert len(first_calls)==2 and len(second_calls)==1
        assert first_calls[-1]['auth'] is None
        conn=app.db();scope=json.loads(conn.execute('SELECT scope_json FROM ai_jobs WHERE id=?',(jobid,)).fetchone()[0]);conn.close()
        assert scope['provider_snapshot']['base_url']==first_url+'/v1'
        assert scope['provider_snapshot']['config_revision']==provider['current_version']


def test_disable_blocks_unsent_and_requires_fresh_generation_test(client,monkeypatch):
    with local_provider() as (url,calls):
        p=enable_real_provider(client,url+'/v1')
        pid=client.post('/api/projects',json={'name':'disable-before-send'}).json()['project']['id'];enter_revision(client,pid);bind(client,pid,p)
        worker=app.process_ai_job;monkeypatch.setattr(app,'process_ai_job',lambda *args:None)
        queued=queue_authoring(client,pid);assert queued.status_code==202,queued.text
        disabled=client.put(f"/api/ai/profiles/{p['id']}",json={'base_version':p['current_version'],'changes':{'status':'disabled'}})
        assert disabled.status_code==200
        worker(pid,queued.json()['job']['id'])
        assert len(calls)==1
        job=client.get(f"/api/projects/{pid}/ai-jobs/{queued.json()['job']['id']}").json()
        assert job['job']['error_code']=='provider_not_enabled'
        assert client.put(f"/api/ai/profiles/{p['id']}",json={'base_version':disabled.json()['current_version'],'changes':{'status':'enabled'}}).status_code==409


def test_unknown_and_small_capacity_block_business_before_egress(client):
    with local_provider() as (url,calls):
        p=enable_real_provider(client,url+'/v1',bounds=False)
        pid=client.post('/api/projects',json={'name':'capacity-required'}).json()['project']['id'];enter_revision(client,pid);bind(client,pid,p)
        result=queue_authoring(client,pid)
        assert result.status_code==409 and result.json()['detail']['code']=='model_capacity_unknown'
        changed=client.put(f"/api/ai/profiles/{p['id']}",json={'base_version':p['current_version'],'changes':{'model_limits':{'test-model':{'input_tokens':300,'output_tokens':20,'source':'deliberately small local test capacity'}}}})
        assert changed.status_code==200 and changed.json()['status']=='enabled'
        bind(client,pid,changed.json())
        result=queue_authoring(client,pid)
        assert result.status_code==422 and result.json()['detail']['code']=='model_capacity_exceeded'
        assert len(calls)==1


def test_secret_validation_errors_never_echo_submitted_key(client):
    secret='sensitive-invalid-fixture-'*30
    result=client.post('/api/ai/profiles',json={'name':'bad-key','base_url':'https://provider.invalid/v1','auth_mode':'api_key','api_key':secret,'request_id':'long-key','models':['m']})
    assert result.status_code==422 and secret not in result.text
    result=client.post('/api/ai/profiles/nonexistent/test',json={'expected_version':1,'request_id':'invalid','model_id':'m','temporary_key':secret})
    assert result.status_code==422 and secret not in result.text


def test_real_local_all_generation_modes_and_discovery_are_separate(client):
    with local_provider(support_stream=True) as (url,calls):
        result=client.post('/api/ai/profiles',json={'name':'multi-mode protocol fixture','base_url':url+'/v1','auth_mode':'none','models':['test-model'],
            'capabilities':{'test-model':{'image_generation':True,'image_edit':True,'vision_analysis':True}},'request_id':'all-modes'})
        assert result.status_code==200,result.text
        p=result.json()
        discovered=client.post(f"/api/ai/profiles/{p['id']}/discover",json={'expected_version':1,'confirm_network':True})
        assert discovered.status_code==200 and 'unregistered-discovery' in discovered.json()['candidates']
        assert 'unregistered-discovery' not in next(v for v in client.get('/api/ai/profiles').json() if v['id']==p['id'])['models']
        assert not calls
        for mode in ['chat','stream','image_generation','image_edit','vision_analysis']:
            response=client.post(f"/api/ai/profiles/{p['id']}/test",json={'request_id':'mode-'+mode,'expected_version':1,'model_id':'test-model','mode':mode,'consent_to_send':True,'possible_cost_confirmed':True})
            assert response.status_code==200,response.text
            state=client.get(f"/api/ai/profiles/{p['id']}/tests").json()[0]
            assert state['status']=='ready',(mode,state)
        assert len(calls)==5
        assert calls[3]['body']['multipart_received']
        assert all(c['auth'] is None for c in calls)


def test_revoked_corpus_is_omitted_and_queued_request_cannot_send_it(client,monkeypatch):
    with local_provider() as (url,calls):
        p=enable_real_provider(client,url+'/v1')
        pid=client.post('/api/projects',json={'name':'source permission guard'}).json()['project']['id'];enter_revision(client,pid);bind(client,pid,p)
        corpus=client.post(f'/api/projects/{pid}/corpus',json={'title':'研究语料','content':'Restricted-source-material-v1.','source':'author-owned'}).json()
        assert 'Restricted-source-material-v1' in client.get(f'/api/projects/{pid}/ai-context').json()['preview']
        worker=app.process_ai_job;monkeypatch.setattr(app,'process_ai_job',lambda *args:None)
        queued=queue_authoring(client,pid);assert queued.status_code==202,queued.text
        conn=app.db()
        with conn:conn.execute("UPDATE corpus_source_versions SET permission_state='revoked' WHERE corpus_id=?",(corpus['id'],))
        conn.close()
        assert 'Restricted-source-material-v1' not in client.get(f'/api/projects/{pid}/ai-context').json()['preview']
        worker(pid,queued.json()['job']['id'])
        state=client.get(f"/api/projects/{pid}/ai-jobs/{queued.json()['job']['id']}").json()
        assert state['job']['error_code']=='context_permission_stale' and len(calls)==1
        retry=client.post(f"/api/projects/{pid}/ai-jobs/{queued.json()['job']['id']}/retry",json={'request_id':'permission-retry'})
        assert retry.status_code==409 and len(calls)==1


def test_temporary_business_key_never_persists_and_restart_requires_reentry(client,monkeypatch):
    with local_provider() as (url,calls):
        p=client.post('/api/ai/profiles',json={'name':'temporary business','base_url':url+'/v1','auth_mode':'temporary','models':['test-model'],
            'model_limits':{'test-model':{'input_tokens':100000,'output_tokens':10000,'source':'temporary local fixture'}},'request_id':'temp-business'}).json()
        secret='operation-only-business-fixture'
        assert client.post(f"/api/ai/profiles/{p['id']}/test",json={'expected_version':1,'request_id':'temp-business-test','model_id':'test-model','mode':'chat','consent_to_send':True,'possible_cost_confirmed':True,'temporary_key':secret}).status_code==200
        enabled=client.put(f"/api/ai/profiles/{p['id']}",json={'base_version':1,'changes':{'status':'enabled'}}).json()
        pid=client.post('/api/projects',json={'name':'temporary paper'}).json()['project']['id'];enter_revision(client,pid);bind(client,pid,enabled)
        assert queue_authoring(client,pid).status_code==409
        success=queue_authoring(client,pid,temporary_key=secret);assert success.status_code==202,success.text
        assert client.get(f"/api/projects/{pid}/ai-jobs/{success.json()['job']['id']}").json()['job']['status']=='succeeded'
        assert len(calls)==2 and calls[-1]['auth']=='Bearer '+secret
        assert vault.current(url+'/v1') is None
        monkeypatch.setattr(app,'process_ai_job',lambda *args:None)
        queued=queue_authoring(client,pid,temporary_key=secret);assert queued.status_code==202
        conn=app.db()
        with conn:conn.execute("UPDATE ai_jobs SET owner_token='old-process-birth' WHERE id=?",(queued.json()['job']['id'],))
        conn.close()
        app.init_db()
        jid=queued.json()['job']['id']
        assert client.post(f'/api/projects/{pid}/ai-jobs/{jid}/retry',json={'request_id':'temp-restart-no-key'}).status_code==409
        conn=app.db();assert secret not in '\n'.join(conn.iterdump());conn.close()
        assert len(calls)==2


def test_shared_revision_application_is_atomic_and_default_only_affects_new_papers(client):
    with local_provider() as (url,calls):
        p=enable_real_provider(client,url+'/v1')
        papers=[client.post('/api/projects',json={'name':name}).json()['project']['id'] for name in ['A','B']]
        for pid in papers:bind(client,pid,p)
        newer=client.put(f"/api/ai/profiles/{p['id']}",json={'base_version':p['current_version'],'changes':{'name':'new display name'}}).json()
        old_revisions={pid:client.get(f'/api/projects/{pid}').json()['project']['project_revision'] for pid in papers}
        client.put(f'/api/projects/{papers[1]}',json={'name':'B changed'})
        target={'profile_id':p['id'],'config_revision':newer['current_version'],'model_id':'test-model'}
        failure=client.post(f"/api/ai/profiles/{p['id']}/apply",json={**target,'project_revisions':old_revisions})
        assert failure.status_code==409,failure.text
        assert all(client.get(f'/api/projects/{pid}').json()['ai_binding']['ai_config_revision']==p['current_version'] for pid in papers)
        applied=client.post(f"/api/ai/profiles/{p['id']}/apply",json={**target,'project_revisions':{papers[0]:old_revisions[papers[0]]}})
        assert applied.status_code==200,applied.text
        assert client.get(f'/api/projects/{papers[1]}').json()['ai_binding']['ai_config_revision']==p['current_version']
        default=client.get('/api/ai/default').json()
        assert client.put('/api/ai/default',json={**target,'expected_version':default['version']}).status_code==200
        fresh=client.post('/api/projects',json={'name':'new default target'}).json()
        assert fresh['ai_binding']['ai_config_revision']==newer['current_version']
        assert client.get(f'/api/projects/{papers[1]}').json()['ai_binding']['ai_config_revision']==p['current_version']
        assert len(calls)==1


def test_audit_is_minimal_and_deleted_independently_of_manuscript(client,monkeypatch):
    pid=client.post('/api/projects',json={'name':'audit privacy'}).json()['project']['id'];enter_revision(client,pid)
    monkeypatch.setattr(app,'call_ai',lambda *a,**k:{'ok':True,'content':'Candidate.','usage':{'prompt_tokens':10,'secret_field':'never-audit-this'}})
    queued=queue_authoring(client,pid);assert queued.status_code==202,queued.text
    records=client.get(f'/api/projects/{pid}/ai-audit').json()
    assert len(records)==1 and records[0]['status']=='succeeded' and records[0]['pricing_status']=='unknown'
    serialized=json.dumps(records)
    assert 'never-audit-this' not in serialized and 'Edit this heading' not in serialized and 'Candidate.' not in serialized
    original=client.get(f'/api/projects/{pid}').json()['files']
    assert client.delete(f'/api/projects/{pid}/ai-audit').status_code==200
    assert client.get(f'/api/projects/{pid}/ai-audit').json()==[]
    assert client.get(f'/api/projects/{pid}').json()['files']==original


def test_real_auth_failure_is_classified_and_cannot_enable_or_fallback(client):
    with local_provider() as (url,calls):
        p=profile(client,url+'/unauthorized')
        response=client.post(f"/api/ai/profiles/{p['id']}/test",json={'expected_version':1,'request_id':'auth-failure','model_id':'test-model','mode':'chat','consent_to_send':True,'possible_cost_confirmed':True})
        assert response.status_code==200,response.text
        state=client.get(f"/api/ai/profiles/{p['id']}/tests").json()[0]
        assert state['status']=='failed' and state['error_code']=='provider_auth_failed'
        assert 'private-provider-body' not in json.dumps(state)
        assert client.put(f"/api/ai/profiles/{p['id']}",json={'base_version':1,'changes':{'status':'enabled'}}).status_code==409
        assert len(calls)==1


@pytest.mark.skipif(os.name!='nt',reason='actual protected key deletion')
def test_revocation_erases_protected_keys_and_keeps_nonsecret_version_history(client):
    p=profile(client,'https://protected.invalid/v1','api_key','erased-fixture-key')
    removed=client.put(f"/api/ai/profiles/{p['id']}",json={'base_version':1,'changes':{'status':'revoked'}})
    assert removed.status_code==200 and removed.json()['credential_state']=='revoked'
    conn=app.db()
    assert not conn.execute('SELECT 1 FROM ai_provider_credentials WHERE profile_id=?',(p['id'],)).fetchone()
    assert conn.execute('SELECT COUNT(*) FROM ai_provider_profile_versions WHERE profile_id=?',(p['id'],)).fetchone()[0]==2
    with pytest.raises(ProjectServiceError):providers.resolve_credentials(conn,p['id'],1)
    conn.close()
    assert client.put(f"/api/ai/profiles/{p['id']}",json={'base_version':2,'changes':{'status':'draft'}}).status_code==422
    renewed=client.put(f"/api/ai/profiles/{p['id']}",json={'base_version':2,'changes':{'status':'draft','api_key':'fresh-fixture-key'}})
    assert renewed.status_code==200 and renewed.json()['status']=='draft'
    assert client.put(f"/api/ai/profiles/{p['id']}",json={'base_version':3,'changes':{'status':'enabled'}}).status_code==409
