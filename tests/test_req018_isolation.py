"""Project and request isolation for OA import."""
import app

def test_explicit_import_key_cannot_cross_papers_or_sources(client,monkeypatch):
    p1=client.post('/api/projects',json={'name':'导入隔离一'}).json()['project']['id'];p2=client.post('/api/projects',json={'name':'导入隔离二'}).json()['project']['id']
    monkeypatch.setattr(app.oa_imports,'fetch_public',lambda url:(b'fake','text/html','test-hash'))
    monkeypatch.setattr(app.oa_imports,'extract_preview',lambda *_:{'fragments':[{'location':'p1','text':'隔离内容'}],'text_preview':'隔离内容','parser_version':'fixture'})
    first=client.post(f'/api/projects/{p1}/imports',json={'url':'https://example.org/a','idempotency_key':'shared-key'});assert first.status_code==200
    cross=client.post(f'/api/projects/{p2}/imports',json={'url':'https://example.org/a','idempotency_key':'shared-key'})
    assert cross.status_code==409 and '隔离内容' not in cross.text
    changed=client.post(f'/api/projects/{p1}/imports',json={'url':'https://example.org/b','idempotency_key':'shared-key'});assert changed.status_code==409


def test_cancel_and_retry_during_fetch_cannot_restore_late_old_result(client,monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    pid=client.post('/api/projects',json={'name':'迟到抓取'}).json()['project']['id'];started=threading.Event();release=threading.Event();calls=[]
    def fetch(url):
        calls.append(len(calls)+1);number=calls[-1]
        if number==1:started.set();assert release.wait(10)
        return (f'payload-{number}'.encode(),'text/html',f'hash-{number}')
    monkeypatch.setattr(app.oa_imports,'fetch_public',fetch)
    monkeypatch.setattr(app.oa_imports,'extract_preview',lambda content,*_:{'fragments':[{'location':'p1','text':content.decode()}],'text_preview':content.decode(),'parser_version':'fixture'})
    payload={'url':'https://example.org/paper','idempotency_key':'late-retry'}
    with ThreadPoolExecutor(max_workers=2) as pool:
        first=pool.submit(client.post,f'/api/projects/{pid}/imports',json=payload)
        assert started.wait(5)
        conn=app.db();iid=conn.execute('SELECT id FROM oa_imports WHERE project_id=?',(pid,)).fetchone()['id'];conn.close()
        assert client.post(f'/api/projects/{pid}/imports/{iid}/cancel').json()['status']=='cancelled'
        second=client.post(f'/api/projects/{pid}/imports',json=payload);assert second.status_code==200 and second.json()['import']['preview']['text_preview']=='payload-2'
        release.set();late=first.result(timeout=10);assert late.status_code==200 and late.json()['status']=='superseded'
    final=client.get(f'/api/projects/{pid}/imports/{iid}').json()['import'];assert final['status']=='preview_ready' and final['preview']['text_preview']=='payload-2'
    conn=app.db();row=conn.execute('SELECT file_path FROM oa_imports WHERE id=?',(iid,)).fetchone();conn.close()
    from pathlib import Path
    assert Path(row['file_path']).read_bytes()==b'payload-2'
    assert len(list(Path(row['file_path']).parent.glob('source-*.bin')))==1


def test_concurrent_confirmation_has_one_evidence_and_rejects_unpreviewed_fragments(client,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    pid=client.post('/api/projects',json={'name':'导入并发确认'}).json()['project']['id']
    monkeypatch.setattr(app.oa_imports,'fetch_public',lambda url:(b'fake','text/html','hash'))
    fragment={'location':'p1','text':'真实预览'}
    monkeypatch.setattr(app.oa_imports,'extract_preview',lambda *_:{'fragments':[fragment],'text_preview':'真实预览','parser_version':'fixture'})
    iid=client.post(f'/api/projects/{pid}/imports',json={'url':'https://example.org/paper'}).json()['import']['id']
    rejected=client.post(f'/api/projects/{pid}/imports/{iid}/confirm',json={'selected_fragments':[{'location':'p1','text':'伪造片段'}]})
    assert rejected.status_code==422 and rejected.json()['detail']['code']=='import_fragment_invalid'
    with ThreadPoolExecutor(max_workers=2) as pool:
        results=list(pool.map(lambda _:client.post(f'/api/projects/{pid}/imports/{iid}/confirm',json={'selected_fragments':[fragment]}),range(2)))
    assert all(r.status_code==200 for r in results),[r.text for r in results]
    assert results[0].json()['evidence_id']==results[1].json()['evidence_id']
    conn=app.db();assert conn.execute('SELECT COUNT(*) FROM external_evidence WHERE import_id=?',(iid,)).fetchone()[0]==1;assert conn.execute('SELECT COUNT(*) FROM corpus WHERE project_id=?',(pid,)).fetchone()[0]==1;conn.close()


def test_legacy_record_only_replays_when_its_project_and_source_are_proven(client,monkeypatch):
    pid=client.post('/api/projects',json={'name':'旧导入'}).json()['project']['id'];other=client.post('/api/projects',json={'name':'另一论文'}).json()['project']['id']
    monkeypatch.setattr(app.oa_imports,'fetch_public',lambda url:(b'fake','text/html','hash'))
    monkeypatch.setattr(app.oa_imports,'extract_preview',lambda *_:{'fragments':[{'location':'p1','text':'旧预览'}],'text_preview':'旧预览','parser_version':'fixture'})
    payload={'url':'https://example.org/legacy','idempotency_key':'legacy-key'};initial=client.post(f'/api/projects/{pid}/imports',json=payload);assert initial.status_code==200
    conn=app.db()
    with conn:conn.execute("UPDATE oa_imports SET request_fingerprint='' WHERE id=?",(initial.json()['import']['id'],))
    conn.close()
    replay=client.post(f'/api/projects/{pid}/imports',json=payload);assert replay.status_code==200 and replay.json()['idempotent_replay']
    assert client.post(f'/api/projects/{pid}/imports',json=payload | {'url':'https://example.org/other'}).status_code==409
    assert client.post(f'/api/projects/{other}/imports',json=payload).status_code==409


def test_parser_failure_does_not_leave_import_stuck_or_partial_file(client,monkeypatch):
    pid=client.post('/api/projects',json={'name':'解析失败'}).json()['project']['id']
    monkeypatch.setattr(app.oa_imports,'fetch_public',lambda url:(b'fake','text/html','hash'))
    def broken(*args):raise ValueError('private parser details')
    monkeypatch.setattr(app.oa_imports,'extract_preview',broken)
    result=client.post(f'/api/projects/{pid}/imports',json={'url':'https://example.org/broken'});assert result.status_code==200
    assert result.json()['status']=='failed' and result.json()['error']['code']=='import_processing_failed' and 'private parser details' not in result.text
    from pathlib import Path
    assert not list((Path(app.PROJECTS_DIR)/pid/'imports').rglob('source-*.bin'))


def test_confirmation_failure_rolls_back_evidence_and_allows_retry(client,monkeypatch):
    import sqlite3
    pid=client.post('/api/projects',json={'name':'导入回滚'}).json()['project']['id']
    monkeypatch.setattr(app.oa_imports,'fetch_public',lambda url:(b'fake','text/html','hash'))
    monkeypatch.setattr(app.oa_imports,'extract_preview',lambda *_:{'fragments':[{'location':'p1','text':'原始预览'}],'parser_version':'fixture'})
    iid=client.post(f'/api/projects/{pid}/imports',json={'url':'https://example.org/rollback'}).json()['import']['id']
    original=app.corpus_lifecycle.rebuild_index
    def fail(*args):raise sqlite3.OperationalError('injected index failure')
    monkeypatch.setattr(app.corpus_lifecycle,'rebuild_index',fail)
    result=client.post(f'/api/projects/{pid}/imports/{iid}/confirm',json={});assert result.status_code==503
    conn=app.db();assert conn.execute('SELECT status FROM oa_imports WHERE id=?',(iid,)).fetchone()[0]=='preview_ready';assert conn.execute('SELECT COUNT(*) FROM external_evidence WHERE import_id=?',(iid,)).fetchone()[0]==0;assert conn.execute('SELECT COUNT(*) FROM corpus WHERE project_id=?',(pid,)).fetchone()[0]==0;conn.close()
    monkeypatch.setattr(app.corpus_lifecycle,'rebuild_index',original)
    assert client.post(f'/api/projects/{pid}/imports/{iid}/confirm',json={}).status_code==200


def test_confirmation_and_cancel_race_never_reactivates_cancelled_evidence(client,monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    pid=client.post('/api/projects',json={'name':'确认取消竞争'}).json()['project']['id']
    monkeypatch.setattr(app.oa_imports,'fetch_public',lambda url:(b'fake','text/html','hash'))
    monkeypatch.setattr(app.oa_imports,'extract_preview',lambda *_:{'fragments':[{'location':'p1','text':'原文证据'}],'parser_version':'fixture'})
    iid=client.post(f'/api/projects/{pid}/imports',json={'url':'https://example.org/race'}).json()['import']['id'];barrier=threading.Barrier(2)
    def action(kind):
        barrier.wait();return client.post(f'/api/projects/{pid}/imports/{iid}/'+kind,json={} if kind=='confirm' else None)
    with ThreadPoolExecutor(max_workers=2) as pool:
        a=pool.submit(action,'confirm');b=pool.submit(action,'cancel');confirmed=a.result(timeout=15);cancelled=b.result(timeout=15)
    assert confirmed.status_code in (200,409) and cancelled.status_code==200
    assert client.get(f'/api/projects/{pid}/imports/{iid}').json()['import']['status']=='cancelled'
    conn=app.db();assert conn.execute('SELECT COUNT(*) FROM corpus_index_chunks i JOIN corpus c ON c.id=i.corpus_id WHERE c.project_id=?',(pid,)).fetchone()[0]==0
    versions=conn.execute('SELECT s.permission_state FROM corpus_source_versions s JOIN corpus c ON c.id=s.corpus_id WHERE c.project_id=?',(pid,)).fetchall();assert all(row['permission_state']=='revoked' for row in versions);conn.close()
