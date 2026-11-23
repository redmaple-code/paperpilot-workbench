"""Recovery is based on actual process identity, never an assumed restart."""
import subprocess,sys,time
from pathlib import Path
import app


def test_live_owner_preserved_then_killed_worker_recovered_and_retried(client,monkeypatch,tmp_path):
    pid=client.post('/api/projects',json={'name':'进程中断恢复'}).json()['project']['id']
    payload={'url':'https://example.org/recovery','defer_fetch':True}
    iid=client.post(f'/api/projects/{pid}/imports',json=payload).json()['import']['id']
    marker=tmp_path/'started';worker=subprocess.Popen([sys.executable,'-X','utf8',str(Path(__file__).with_name('oa_fetch_worker.py')),str(app.DB_PATH),str(app.PROJECTS_DIR),pid,iid,str(marker)])
    try:
        deadline=time.monotonic()+15
        while not marker.exists() and time.monotonic()<deadline:
            assert worker.poll() is None;time.sleep(.05)
        assert marker.exists()
        app.init_db()
        active=client.get(f'/api/projects/{pid}/imports/{iid}').json()['import']
        assert active['status']=='fetching' and active['recovery_state']=='alive'
        assert 'owner_pid' not in active and 'owner_token' not in active
        worker.terminate();worker.wait(timeout=10)
        app.init_db()
        recovered=client.get(f'/api/projects/{pid}/imports/{iid}').json()['import']
        assert recovered['status']=='failed' and recovered['error_code']=='fetch_interrupted'
        assert not client.get(f'/api/projects/{pid}').json()['corpus']
        monkeypatch.setattr(app.oa_imports,'fetch_public',lambda _:(b'fresh','text/html','hash'))
        monkeypatch.setattr(app.oa_imports,'extract_preview',lambda *_:{'text_preview':'fresh','fragments':[]})
        retried=client.post(f'/api/projects/{pid}/imports',json={'url':'https://example.org/recovery'}).json()
        assert retried['status']=='preview_ready' and retried['import']['preview']['text_preview']=='fresh'
    finally:
        if worker.poll() is None:worker.terminate();worker.wait(timeout=10)


def test_unknown_owner_kept_cancellable_and_no_implicit_retry(client):
    pid=client.post('/api/projects',json={'name':'未知执行者'}).json()['project']['id']
    payload={'url':'https://example.org/unknown','defer_fetch':True}
    iid=client.post(f'/api/projects/{pid}/imports',json=payload).json()['import']['id']
    conn=app.db()
    with conn:conn.execute("UPDATE oa_imports SET status='fetching',owner_pid=0,owner_token='' WHERE id=?",(iid,))
    conn.close();app.init_db()
    view=client.get(f'/api/projects/{pid}/imports/{iid}').json()['import']
    assert view['status']=='fetching' and view['recovery_state']=='unknown'
    replay=client.post(f'/api/projects/{pid}/imports',json=payload).json()
    assert replay['import']['status']=='fetching' and replay['idempotent_replay']
    assert client.post(f'/api/projects/{pid}/imports/{iid}/cancel').json()['status']=='cancelled'
    retry=client.post(f'/api/projects/{pid}/imports',json=payload).json()
    assert retry['status']=='created'


def test_identity_unavailable_does_not_start_fetch(client,monkeypatch):
    from paperpilot.services import tex_runtime
    pid=client.post('/api/projects',json={'name':'无法确认进程身份'}).json()['project']['id'];iid=client.post(f'/api/projects/{pid}/imports',json={'url':'https://example.org/x','defer_fetch':True}).json()['import']['id']
    monkeypatch.setattr(tex_runtime,'current_owner',lambda:(_ for _ in ()).throw(OSError('unavailable')))
    result=client.post(f'/api/projects/{pid}/imports/{iid}/fetch')
    assert result.status_code==503 and result.json()['detail']['code']=='worker_identity_unavailable'
    assert client.get(f'/api/projects/{pid}/imports/{iid}').json()['import']['status']=='created'


def test_synchronous_retry_claims_previously_created_record(client,monkeypatch):
    pid=client.post('/api/projects',json={'name':'准备阶段恢复'}).json()['project']['id']
    payload={'url':'https://example.org/created','idempotency_key':'created-recovery'}
    iid=client.post(f'/api/projects/{pid}/imports',json={**payload,'defer_fetch':True}).json()['import']['id']
    monkeypatch.setattr(app.oa_imports,'fetch_public',lambda _:(b'fresh','text/html','hash'))
    monkeypatch.setattr(app.oa_imports,'extract_preview',lambda *_:{'text_preview':'fresh','fragments':[]})
    retry=client.post(f'/api/projects/{pid}/imports',json=payload).json()
    assert retry['status']=='preview_ready' and retry['import']['id']==iid
