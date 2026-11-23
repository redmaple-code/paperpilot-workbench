import threading
from concurrent.futures import ThreadPoolExecutor
import app


def test_deferred_fetch_cancel_and_late_result(client, monkeypatch):
    pid=client.post('/api/projects',json={'name':'可取消下载'}).json()['project']['id']
    started=threading.Event();release=threading.Event();calls=[]
    def fetch(url):
        calls.append(url);started.set();assert release.wait(10)
        return b'late', 'text/html', 'digest'
    monkeypatch.setattr(app.oa_imports,'fetch_public',fetch)
    monkeypatch.setattr(app.oa_imports,'extract_preview',lambda *_:{'text_preview':'late','fragments':[]})
    created=client.post(f'/api/projects/{pid}/imports',json={'url':'https://example.org/paper','defer_fetch':True}).json()
    assert created['status']=='created' and calls==[]
    iid=created['import']['id']
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending=pool.submit(client.post,f'/api/projects/{pid}/imports/{iid}/fetch')
        assert started.wait(5)
        duplicate=client.post(f'/api/projects/{pid}/imports/{iid}/fetch').json()
        assert duplicate['status']=='fetching' and len(calls)==1
        assert client.post(f'/api/projects/{pid}/imports/{iid}/cancel').json()['status']=='cancelled'
        release.set();assert pending.result(timeout=10).json()['status']=='superseded'
    assert client.get(f'/api/projects/{pid}/imports/{iid}').json()['import']['status']=='cancelled'
    assert client.post(f'/api/projects/{pid}/imports/{iid}/fetch').json()['status']=='cancelled'
    assert not client.get(f'/api/projects/{pid}').json()['corpus']
    assert not list((app.PROJECTS_DIR/pid/'imports').rglob('source-*.bin'))


def test_deferred_fetch_project_isolation_and_cancel_before_start(client,monkeypatch):
    p1=client.post('/api/projects',json={'name':'下载一'}).json()['project']['id'];p2=client.post('/api/projects',json={'name':'下载二'}).json()['project']['id']
    created=client.post(f'/api/projects/{p1}/imports',json={'url':'https://example.org/paper','defer_fetch':True}).json();iid=created['import']['id']
    assert client.post(f'/api/projects/{p2}/imports/{iid}/fetch').status_code==404
    assert client.post(f'/api/projects/{p1}/imports/{iid}/cancel').json()['status']=='cancelled'
    monkeypatch.setattr(app.oa_imports,'fetch_public',lambda *_: (_ for _ in ()).throw(AssertionError('must not fetch')))
    assert client.post(f'/api/projects/{p1}/imports/{iid}/fetch').json()['status']=='cancelled'
