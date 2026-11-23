import json
import sqlite3
import pytest

from paperpilot.services import build_cache
from tests.test_req011_builds import _project


def test_retention_inventory_protects_recent_current_release_and_restore():
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript('''
      CREATE TABLE projects(id TEXT);
      CREATE TABLE build_jobs(id TEXT,project_id TEXT,status TEXT,purpose TEXT,promoted INTEGER,
        artifact_path TEXT,artifact_sha256 TEXT,created_at TEXT,finished_at TEXT);
      CREATE TABLE build_restore_events(project_id TEXT,job_id TEXT);
      CREATE TABLE project_release_checks(project_id TEXT,result_json TEXT);
      INSERT INTO projects VALUES('p'),('other');
    ''')
    build_cache.initialize_schema(conn)
    for i in range(15):
        conn.execute('INSERT INTO build_jobs(id,project_id,status,purpose,promoted,artifact_path,artifact_sha256,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?)',
                     (str(i), 'p', 'succeeded', 'submitted' if i == 0 else 'candidate_preview',
                      int(i == 0), f'{i}.pdf', 'sha', f'{i:02}', f'{i:02}'))
    conn.execute('INSERT INTO build_restore_events VALUES(?,?)', ('p', '1'))
    conn.execute('INSERT INTO project_release_checks VALUES(?,?)', ('p', json.dumps({'compile': {'build_job_id': '2'}})))
    conn.execute('INSERT INTO build_restore_events VALUES(?,?)', ('other', '3'))
    result = build_cache.inventory(conn, 'p')
    items = {row['job_id']: row['protected_by'] for row in result['items']}
    assert all('recent_success' in items[str(i)] for i in range(5, 15))
    assert 'current_pdf' in items['0']
    assert 'restore_reference' in items['1'] and 'release_reference' in items['2']
    assert result['potential_old_unreferenced'] == ['4', '3']
    assert result['deletion_enabled'] is True
    build_cache.hold(conn, 'p', '3', 'reader-one')
    assert 'active_reader' in next(item for item in build_cache.inventory(conn, 'p')['items'] if item['job_id'] == '3')['protected_by']
    conn.execute("UPDATE build_read_leases SET expires_at=0 WHERE lease_id='reader-one'")
    assert '3' in build_cache.inventory(conn, 'p')['potential_old_unreferenced']
    conn.execute('INSERT INTO project_release_checks VALUES(?,?)', ('p', '{bad'))
    assert build_cache.inventory(conn, 'p')['unknown_release_references']
    conn.execute('DELETE FROM project_release_checks WHERE result_json=?', ('{bad',))
    conn.execute('INSERT INTO project_release_checks VALUES(?,?)', ('p', '{"compile":{"ok":true}}'))
    assert build_cache.inventory(conn, 'p')['unknown_release_references']


def test_inventory_endpoint_is_read_only_and_scoped(client):
    pid = _project(client)
    response = client.get(f'/api/projects/{pid}/builds/cache-inventory')
    assert response.status_code == 200
    assert response.json()['success_count'] == 0
    assert response.json()['deletion_enabled'] is True
    assert client.get('/api/projects/no-such-project/builds/cache-inventory').status_code == 404


def test_reader_lease_is_project_scoped_renewable_and_download_holds(client, monkeypatch):
    from tests.test_req011_reading import mapped_job
    pid, job = mapped_job(client, monkeypatch)
    job_id = job['id']
    url = f'/api/projects/{pid}/builds/{job_id}/read-lease?lease_id=reader_one'
    assert client.post(url).status_code == 200
    assert client.post(url).status_code == 200
    inventory = client.get(f'/api/projects/{pid}/builds/cache-inventory').json()
    assert 'active_reader' in inventory['items'][0]['protected_by']
    other = _project(client)
    assert client.post(f'/api/projects/{other}/builds/{job_id}/read-lease?lease_id=reader_one').status_code == 404
    assert client.post(f'/api/projects/{pid}/builds/{job_id}/read-lease?lease_id=bad%20id').status_code == 422
    assert client.get(f'/api/projects/{pid}/builds/{job_id}/pdf').status_code == 200


def test_persistent_reader_pin_release_and_stream_lifetime(client, monkeypatch):
    import asyncio
    import app
    from tests.test_req011_reading import mapped_job
    pid,job=mapped_job(client,monkeypatch);job_id=job['id'];reader='r-'+'a'*32
    url=f'/api/projects/{pid}/builds/{job_id}/read-lease?lease_id={reader}&persistent=true'
    assert client.post(url).json()['expires_in_seconds'] is None
    conn=app.db()
    try:
        lease=conn.execute('SELECT expires_at FROM build_read_leases WHERE project_id=? AND job_id=? AND lease_id=?',(pid,job_id,reader)).fetchone()
        assert lease['expires_at']==build_cache.PERSISTENT_EXPIRY
    finally:conn.close()
    response=app.read_build_pdf(pid,job_id)
    info=client.get(f'/api/projects/{pid}/builds/cache-inventory').json()
    assert info['persistent_pin_counts'][job_id]==2
    asyncio.run(response.background())
    assert client.get(f'/api/projects/{pid}/builds/cache-inventory').json()['persistent_pin_counts'][job_id]==1
    other=_project(client)
    assert not client.post(f'/api/projects/{other}/builds/{job_id}/read-lease-release?lease_id={reader}').json()['released']
    assert client.post(f'/api/projects/{pid}/builds/{job_id}/read-lease-release?lease_id={reader}').json()['released']
    assert job_id not in client.get(f'/api/projects/{pid}/builds/cache-inventory').json()['persistent_pin_counts']
    assert client.post(url).status_code==200
    assert client.post(f'/api/projects/{pid}/builds/{job_id}/clear-read-pins').json()['released']==1
    response=app.read_build_pdf(pid,job_id);events=[]
    async def send(message):
        events.append(message['type'])
        if message['type']=='http.response.body':
            c=app.db()
            try:
                assert c.execute('SELECT COUNT(*) FROM build_read_leases WHERE project_id=? AND job_id=? AND expires_at=?',(pid,job_id,build_cache.PERSISTENT_EXPIRY)).fetchone()[0]==1
            finally:c.close()
    async def receive(): return {'type':'http.request','body':b'','more_body':False}
    asyncio.run(response({'type':'http','method':'GET','headers':[], 'extensions':{'http.response.pathsend':{}}},receive,send))
    assert 'http.response.pathsend' not in events and 'http.response.body' in events
    assert job_id not in client.get(f'/api/projects/{pid}/builds/cache-inventory').json()['persistent_pin_counts']


def test_prune_preserves_references_and_recovers_both_crash_windows(tmp_path):
    conn = sqlite3.connect(':memory:'); conn.row_factory = sqlite3.Row
    conn.executescript('''CREATE TABLE projects(id TEXT);
      CREATE TABLE build_jobs(id TEXT,project_id TEXT,status TEXT,purpose TEXT,promoted INTEGER,
       artifact_path TEXT,artifact_sha256 TEXT,created_at TEXT,finished_at TEXT);
      CREATE TABLE build_restore_events(project_id TEXT,job_id TEXT);
      CREATE TABLE project_release_checks(project_id TEXT,result_json TEXT);
      INSERT INTO projects VALUES('paper');''')
    build_cache.initialize_schema(conn)
    ids = [f'{i:032x}' for i in range(14)]
    for i, job in enumerate(ids):
        run = tmp_path/'paper'/'build-runs'/job; run.mkdir(parents=True)
        (run/'main.pdf').write_bytes(b'pdf'); (run/'main.tex').write_text('source')
        conn.execute('INSERT INTO build_jobs(id,project_id,status,purpose,promoted,artifact_path,artifact_sha256,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?)',
                     (job,'paper','succeeded','candidate_preview',0,str(run/'main.pdf'),'hash',f'{i:02}',f'{i:02}'))
    conn.execute('INSERT INTO build_restore_events VALUES(?,?)', ('paper',ids[1]))
    build_cache.hold(conn,'paper',ids[2],'reader'); conn.commit()
    assert build_cache.prune_one(conn,tmp_path,'paper',ids[13]) == 'protected'
    assert build_cache.prune_one(conn,tmp_path,'paper',ids[1]) == 'protected'
    assert build_cache.prune_one(conn,tmp_path,'paper',ids[2]) == 'protected'
    assert build_cache.prune_one(conn,tmp_path,'paper',ids[0]) == 'pruned'
    assert not (tmp_path/'paper'/'build-runs'/ids[0]).exists()
    assert conn.execute('SELECT cache_pruned FROM build_jobs WHERE id=?',(ids[0],)).fetchone()[0] == 1
    outside=tmp_path/'outside.pdf';outside.write_bytes(b'untouched')
    conn.execute('UPDATE build_jobs SET artifact_path=? WHERE id=?',(str(outside),ids[3]));conn.commit()
    with pytest.raises(ValueError):build_cache.prune_one(conn,tmp_path,'paper',ids[3])
    assert outside.read_bytes()==b'untouched'
    conn.execute('UPDATE build_jobs SET artifact_path=? WHERE id=?',(str(tmp_path/'paper'/'build-runs'/ids[3]/'main.pdf'),ids[3]));conn.commit()
    conn.execute('INSERT INTO project_release_checks VALUES(?,?)',('paper','{broken'));conn.commit()
    assert build_cache.prune_one(conn,tmp_path,'paper',ids[3])=='protected'
    conn.execute('DELETE FROM project_release_checks WHERE project_id=?',('paper',));conn.commit()
    conn.execute("CREATE TRIGGER fail_cache_mark BEFORE UPDATE OF cache_pruned ON build_jobs BEGIN SELECT RAISE(ABORT,'injected failure'); END");conn.commit()
    with pytest.raises(sqlite3.IntegrityError):build_cache.prune_one(conn,tmp_path,'paper',ids[3])
    assert (tmp_path/'paper'/'build-runs'/ids[3]/'main.pdf').is_file()
    assert not (tmp_path/'paper'/'build-runs'/('.prune-'+ids[3])).exists()
    conn.execute('DROP TRIGGER fail_cache_mark');conn.commit()
    link=tmp_path/'paper'/'build-runs'/ids[3]/'external-link'
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pass  # Windows may disallow symlink creation for this account.
    else:
        with pytest.raises(ValueError):build_cache.prune_one(conn,tmp_path,'paper',ids[3])
        assert link.is_symlink() and outside.read_bytes()==b'untouched'
        link.unlink()
    run=tmp_path/'paper'/'build-runs'/ids[3]; stage=run.with_name('.prune-'+ids[3]); run.rename(stage)
    assert build_cache.recover_staged(conn,tmp_path,'paper')['restored'] == [ids[3]]
    conn.commit(); assert (run/'main.pdf').is_file()
    run.rename(stage); conn.execute('UPDATE build_jobs SET cache_pruned=1 WHERE id=?',(ids[3],)); conn.commit()
    assert build_cache.recover_staged(conn,tmp_path,'paper')['removed'] == [ids[3]]
    conn.commit(); assert not stage.exists()


def test_prune_waits_for_concurrent_read_lease(tmp_path):
    import threading
    import time
    db_path = tmp_path/'state.sqlite'
    def connect():
        c=sqlite3.connect(db_path,timeout=5); c.row_factory=sqlite3.Row; return c
    conn=connect(); conn.executescript('''CREATE TABLE projects(id TEXT);
      CREATE TABLE build_jobs(id TEXT,project_id TEXT,status TEXT,purpose TEXT,promoted INTEGER,
       artifact_path TEXT,artifact_sha256 TEXT,created_at TEXT,finished_at TEXT);
      CREATE TABLE build_restore_events(project_id TEXT,job_id TEXT);
      CREATE TABLE project_release_checks(project_id TEXT,result_json TEXT);
      INSERT INTO projects VALUES('paper');'''); build_cache.initialize_schema(conn)
    ids=[f'{i:032x}' for i in range(11)]
    for i,job in enumerate(ids):
        run=tmp_path/'paper'/'build-runs'/job;run.mkdir(parents=True);(run/'main.pdf').write_bytes(b'pdf')
        conn.execute('INSERT INTO build_jobs(id,project_id,status,purpose,promoted,artifact_path,artifact_sha256,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?)',
                     (job,'paper','succeeded','candidate_preview',0,str(run/'main.pdf'),'hash',f'{i:02}',f'{i:02}'))
    conn.commit()
    build_cache.hold(conn,'paper',ids[0],'concurrent-reader')
    started=threading.Event();results=[]
    def cleaner():
        c=connect();started.set()
        try: results.append(build_cache.prune_one(c,tmp_path,'paper',ids[0]))
        finally:c.close()
    thread=threading.Thread(target=cleaner);thread.start();assert started.wait(2)
    time.sleep(.1);assert thread.is_alive()
    conn.commit();thread.join(5)
    assert not thread.is_alive() and results==['protected']
    assert (tmp_path/'paper'/'build-runs'/ids[0]/'main.pdf').is_file()
    conn.close()


def test_successful_builds_automatically_prune_old_cache_and_keep_snapshot(client, monkeypatch):
    import app
    from tests.test_req011_sources_queue import fake_compiler
    pid=_project(client);monkeypatch.setattr(app,'compile_project_files',fake_compiler([]))
    jobs=[]
    for i in range(11):
        response=client.post(f'/api/projects/{pid}/compile',json={'request_id':f'cache-{i}', 'purpose':'draft_preview','force_rebuild':True})
        assert response.status_code==200,response.text
        job=client.get(response.json()['status_url']).json();assert job['status']=='succeeded';jobs.append(job)
    oldest=client.get(f"/api/projects/{pid}/builds/{jobs[0]['id']}").json()
    assert oldest['cache_pruned']==1
    assert client.get(f"/api/projects/{pid}/build-snapshots/{oldest['snapshot_id']}").status_code==200
    missing=client.get(f"/api/projects/{pid}/builds/{oldest['id']}/pdf")
    assert missing.status_code==409 and '重新编译' in missing.json()['detail']['message']
    for job in jobs[1:]:
        assert client.get(f"/api/projects/{pid}/builds/{job['id']}").json()['cache_pruned']==0
