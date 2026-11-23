"""Recovery never guesses ownership or duplicates an AI request."""
import app
from test_req001 import enter_revision
from test_req014_provider_runtime import queue_authoring


def test_unknown_legacy_owner_requires_explicit_recovery_and_duplicate_confirmation(client,monkeypatch):
    pid=client.post('/api/projects',json={'name':'身份不明的旧任务'}).json()['project']['id'];enter_revision(client,pid)
    monkeypatch.setattr(app,'process_ai_job',lambda *args:None)
    queued=queue_authoring(client,pid);assert queued.status_code==202,queued.text
    job=queued.json()['job'];jid=job['id']
    denied=client.post(f'/api/projects/{pid}/ai-jobs/{jid}/recover',json={'confirmed_stopped':True})
    assert denied.status_code==409 and denied.json()['detail']['code']=='worker_still_alive'
    conn=app.db()
    with conn:conn.execute("UPDATE ai_jobs SET owner_pid=0,owner_token='' WHERE id=?",(jid,))
    conn.close();app.init_db()
    view=client.get(f'/api/projects/{pid}/ai-jobs/{jid}').json()['job'];assert view['status']=='running' and view['recovery_state']=='unknown'
    assert client.post(f'/api/projects/{pid}/ai-jobs/{jid}/recover',json={'confirmed_stopped':False}).status_code==422
    result=client.post(f'/api/projects/{pid}/ai-jobs/{jid}/recover',json={'confirmed_stopped':True});assert result.status_code==200,result.text
    assert result.json()['job']['status']=='failed' and result.json()['job']['scope']['egress_state']=='unknown'
    assert client.post(f'/api/projects/{pid}/ai-jobs/{jid}/recover',json={'confirmed_stopped':True}).json()['idempotent_replay']
    retry=client.post(f'/api/projects/{pid}/ai-jobs/{jid}/retry',json={'request_id':'owner-retry'})
    assert retry.status_code==409 and retry.json()['detail']['code']=='duplicate_cost_confirmation_required'
    conn=app.db();audit=conn.execute('SELECT status,error_code FROM ai_provider_audit WHERE id=?',(jid,)).fetchone();conn.close();assert dict(audit)=={'status':'unknown','error_code':'interrupted'}


def test_legacy_unknown_provider_test_is_preserved_and_explained(client,monkeypatch):
    from paperpilot.services import provider_runtime
    p=client.post('/api/ai/profiles',json={'name':'身份不明测试','base_url':'http://127.0.0.1:1/v1','auth_mode':'none','models':['m'],'request_id':'legacy-profile'}).json()
    monkeypatch.setattr(provider_runtime,'process_test',lambda *args:None)
    r=client.post(f"/api/ai/profiles/{p['id']}/test",json={'expected_version':1,'request_id':'legacy-test','model_id':'m','mode':'chat','consent_to_send':True,'possible_cost_confirmed':True});assert r.status_code==200,r.text
    conn=app.db()
    with conn:conn.execute("UPDATE ai_provider_tests SET owner_pid=0,owner_token='' WHERE request_id='legacy-test'")
    conn.close();app.init_db();state=client.get(f"/api/ai/profiles/{p['id']}/tests").json()[0]
    assert state['status']=='queued' and state['recovery_state']=='unknown' and 'owner_token' not in state


def test_unavailable_process_identity_prevents_sending_or_partial_task(client,monkeypatch):
    from paperpilot.services import tex_runtime
    pid=client.post('/api/projects',json={'name':'无法确认进程'}).json()['project']['id'];enter_revision(client,pid)
    def unavailable():raise OSError('identity unavailable')
    monkeypatch.setattr(tex_runtime,'current_owner',unavailable)
    response=queue_authoring(client,pid)
    assert response.status_code==503 and response.json()['detail']['code']=='worker_identity_unavailable'
    conn=app.db();assert conn.execute('SELECT COUNT(*) FROM ai_jobs WHERE project_id=?',(pid,)).fetchone()[0]==0
    assert conn.execute('SELECT COUNT(*) FROM ai_provider_audit WHERE project_id=?',(pid,)).fetchone()[0]==0;conn.close()
