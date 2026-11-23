import threading
import time

import app
from paperpilot.services import build_queue, build_snapshots, tex_runtime
from tests.test_req011_builds import _project
from tests.test_req011_sources_queue import enqueue_fixture, fake_compiler


def test_two_global_slots_and_waiter_wakes_after_release(client, monkeypatch):
    projects=[_project(client) for _ in range(3)];conn=app.db()
    with conn:
        jobs=[enqueue_fixture(conn,pid,'global-'+str(i)) for i,pid in enumerate(projects)]
    conn.close()
    starts=[threading.Event() for _ in projects];release=[threading.Event() for _ in projects]
    guard=threading.Lock();active=0;maximum=0;job_index={job['id']:i for i,job in enumerate(jobs)}
    def compiler(*args,**kwargs):
        nonlocal active,maximum
        index=job_index[kwargs['job_id']]
        with guard:active+=1;maximum=max(maximum,active)
        starts[index].set()
        try:
            assert release[index].wait(15)
            return fake_compiler([])(*args,**kwargs)
        finally:
            with guard:active-=1
    monkeypatch.setattr(app,'compile_project_files',compiler)
    threads=[threading.Thread(target=app.execute_build_job,args=(pid,)) for pid in projects]
    try:
        threads[0].start();threads[1].start()
        assert starts[0].wait(5) and starts[1].wait(5)
        threads[2].start();threads[2].join(5);assert not threads[2].is_alive()
        waiting=client.get(f"/api/projects/{projects[2]}/builds/{jobs[2]['id']}").json()
        assert waiting['status']=='queued' and waiting['global_capacity_wait']
        cancelled=client.post(f"/api/projects/{projects[0]}/builds/{jobs[0]['id']}/cancel?request_id=cancel-global")
        assert cancelled.status_code==200 and cancelled.json()['status']=='cancelled'
        c=app.db();assert c.execute('SELECT COUNT(*) FROM build_global_slots').fetchone()[0]==2;c.close()
        assert not starts[2].wait(.2), 'cancelled status must not release an executing worker slot'
        release[0].set();assert starts[2].wait(8), 'capacity release must wake another paper without another request'
        assert not release[1].is_set() and maximum==2
    finally:
        for event in release:event.set()
        for thread in threads:
            if thread.ident is not None:thread.join(10)
    deadline=time.monotonic()+10
    while time.monotonic()<deadline:
        c=app.db();count=c.execute('SELECT COUNT(*) FROM build_global_slots').fetchone()[0];c.close()
        with app._build_locks_guard:
            waking=any(key[0]==str(app.DB_PATH.resolve()) for key in app._build_waking)
        if not count and not waking:break
        time.sleep(.05)
    assert count==0 and not waking and maximum==2


def test_dead_slot_retained_until_container_cleanup_verified(client,monkeypatch):
    pid=_project(client);conn=app.db()
    with conn:
        job=enqueue_fixture(conn,pid,'dead-global');claim=build_queue.claim_next(conn,pid)
        waiting=enqueue_fixture(conn,pid,'dead-waiter')
        conn.execute("UPDATE build_jobs SET owner_token='dead-token' WHERE id=?",(waiting['id'],))
        conn.execute("UPDATE build_global_slots SET owner_token='dead-token' WHERE job_id=?",(job['id'],))
    monkeypatch.setattr(tex_runtime,'owner_state',lambda owner:'dead' if owner.get('owner_token')=='dead-token' else 'alive')
    monkeypatch.setattr(build_snapshots,'recover_project_runtime',lambda *a,**kw:[{'cleanup_verified':False}])
    with conn:
        result=build_queue.recover_dead_slots(conn,app.PROJECTS_DIR)
        assert result['pending']==[job['id']] and conn.execute('SELECT COUNT(*) FROM build_global_slots').fetchone()[0]==1
        assert conn.execute('SELECT status FROM build_jobs WHERE id=?',(job['id'],)).fetchone()[0]=='running'
        assert result['queued_interrupted']==[waiting['id']]
        assert conn.execute('SELECT status FROM build_jobs WHERE id=?',(waiting['id'],)).fetchone()[0]=='interrupted'
    monkeypatch.setattr(build_snapshots,'recover_project_runtime',lambda *a,**kw:[{'cleanup_verified':True}])
    with conn:
        result=build_queue.recover_dead_slots(conn,app.PROJECTS_DIR)
        assert result['released']==[job['id']] and conn.execute('SELECT COUNT(*) FROM build_global_slots').fetchone()[0]==0
        assert conn.execute('SELECT status FROM build_jobs WHERE id=?',(job['id'],)).fetchone()[0]=='interrupted'
    conn.close()


def test_unknown_owner_keeps_slot_and_never_requests_container_cleanup(client,monkeypatch):
    pid=_project(client);waiting_project=_project(client);conn=app.db()
    with conn:
        job=enqueue_fixture(conn,pid,'unknown-owner');build_queue.claim_next(conn,pid)
        waiting=enqueue_fixture(conn,waiting_project,'waiting-unknown')
    monkeypatch.setattr(build_queue,'GLOBAL_BUILD_LIMIT',1)
    monkeypatch.setattr(tex_runtime,'owner_state',lambda owner:'unknown')
    monkeypatch.setattr(build_snapshots,'recover_project_runtime',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('must not kill unknown owner')))
    with conn:
        result=build_queue.recover_dead_slots(conn,app.PROJECTS_DIR)
        assert result['pending']==[job['id']]
        assert conn.execute('SELECT COUNT(*) FROM build_global_slots').fetchone()[0]==1
        described=build_snapshots.get_job(conn,waiting_project,waiting['id'])
        assert '无法核实' in described['global_capacity_message']
        assert described['global_capacity_blockers'][0]['project_id']==pid
    conn.close()


def test_global_claim_is_atomic_across_processes_and_recovers_dead_owners(client,tmp_path):
    import json, os, subprocess, sys
    projects=[_project(client) for _ in range(3)];conn=app.db()
    with conn:
        jobs=[enqueue_fixture(conn,pid,'process-'+str(i)) for i,pid in enumerate(projects)]
    conn.close()
    start=tmp_path/'go';script=r'''
import json, pathlib, sqlite3, sys, time
from paperpilot.services import build_queue
db, project, start = sys.argv[1:]
while not pathlib.Path(start).exists(): time.sleep(.01)
c=sqlite3.connect(db,timeout=10);c.row_factory=sqlite3.Row
with c: job=build_queue.claim_next(c,project)
print(json.dumps({'job': job['id'] if job else None}),flush=True)
time.sleep(30)
'''
    flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
    processes=[subprocess.Popen([sys.executable,'-X','utf8','-c',script,str(app.DB_PATH),pid,str(start)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,creationflags=flags) for pid in projects]
    results=[None]*3;readers=[]
    try:
        for i,p in enumerate(processes):
            reader=threading.Thread(target=lambda k=i,proc=p:results.__setitem__(k,proc.stdout.readline()),daemon=True);reader.start();readers.append(reader)
        start.write_text('go')
        for reader in readers:reader.join(12)
        assert all(not reader.is_alive() for reader in readers), 'child claim did not finish'
        claimed=[json.loads(line)['job'] for line in results]
        assert len([x for x in claimed if x])==2
    finally:
        for p in processes:
            if p.poll() is None:p.kill()
        for p in processes:p.wait(timeout=10)
    conn=app.db()
    try:
        with conn:
            recovery=build_queue.recover_dead_slots(conn,app.PROJECTS_DIR)
            assert set(recovery['released'])==set(x for x in claimed if x)
            assert not recovery['pending']
            statuses={row['id']:row['status'] for row in conn.execute('SELECT id,status FROM build_jobs')}
            assert sum(statuses[job['id']]=='interrupted' for job in jobs)==2
            assert sum(statuses[job['id']]=='queued' for job in jobs)==1
    finally:conn.close()


def test_periodic_dispatch_wakes_waiter_without_inprocess_notification(client,monkeypatch):
    projects=[_project(client) for _ in range(3)];conn=app.db()
    with conn:
        jobs=[enqueue_fixture(conn,pid,'periodic-'+str(i)) for i,pid in enumerate(projects)]
        assert build_queue.claim_next(conn,projects[0]) and build_queue.claim_next(conn,projects[1])
    conn.close()
    started=threading.Event()
    def compiler(*args,**kwargs):
        started.set();return fake_compiler([])(*args,**kwargs)
    monkeypatch.setattr(app,'compile_project_files',compiler)
    try:
        with client:
            assert not started.wait(1.5)
            conn=app.db()
            with conn:
                conn.execute("UPDATE build_jobs SET status='failed' WHERE id=?",(jobs[0]['id'],))
                build_queue.release_slot(conn,projects[0],jobs[0]['id'],tex_runtime.current_owner())
            conn.close()
            assert started.wait(6), 'periodic dispatcher must notice capacity released by another process'
            deadline=time.monotonic()+10
            while time.monotonic()<deadline:
                conn=app.db();running=conn.execute('SELECT COUNT(*) FROM build_global_slots WHERE job_id=?',(jobs[2]['id'],)).fetchone()[0];conn.close()
                with app._build_locks_guard:
                    waking=any(key[0]==str(app.DB_PATH.resolve()) for key in app._build_waking)
                if not running and not waking:break
                time.sleep(.05)
            assert not running and not waking
    finally:
        conn=app.db()
        with conn:
            conn.execute("UPDATE build_jobs SET status='failed' WHERE id=?",(jobs[1]['id'],))
            build_queue.release_slot(conn,projects[1],jobs[1]['id'],tex_runtime.current_owner())
        conn.close()


def test_finished_worker_keeps_capacity_until_cleanup_retry_succeeds(client,monkeypatch):
    pid=_project(client);conn=app.db()
    with conn: job=enqueue_fixture(conn,pid,'cleanup-retry')
    conn.close();actual=build_snapshots.recover_project_runtime;attempts=[]
    def recover(conn,project_id,storage_root,*,job_ids=None,force=False,build_only=False):
        if job_ids and force and not attempts:
            attempts.append(1)
            return [{'job_id':job['id'],'status':'cleanup_unavailable','cleanup_verified':False}]
        return actual(conn,project_id,storage_root,job_ids=job_ids,force=force,build_only=build_only)
    monkeypatch.setattr(build_snapshots,'recover_project_runtime',recover)
    monkeypatch.setattr(app,'compile_project_files',fake_compiler([]))
    app.execute_build_job(pid)
    conn=app.db();assert conn.execute('SELECT cleanup_pending FROM build_global_slots WHERE job_id=?',(job['id'],)).fetchone()[0]==1
    with conn:
        result=build_queue.recover_dead_slots(conn,app.PROJECTS_DIR)
        assert result['released']==[job['id']], 'another scheduler can retry cleanup even while the owner process lives'
    conn.close()
    app._retry_finished_build_slots()
    conn=app.db();assert conn.execute('SELECT COUNT(*) FROM build_global_slots WHERE job_id=?',(job['id'],)).fetchone()[0]==0;conn.close()


def test_woken_worker_remains_bound_to_originating_store(client,monkeypatch,tmp_path):
    pid=_project(client);conn=app.db()
    with conn: enqueue_fixture(conn,pid,'store-binding')
    conn.close();original_db=app.DB_PATH;original_root=app.PROJECTS_DIR
    entered=threading.Event();proceed=threading.Event();done=threading.Event();seen={}
    def worker(_pid):
        entered.set();assert proceed.wait(5)
        c=app.db();seen['db']=c.execute('PRAGMA database_list').fetchone()[2];c.close()
        seen['root']=app._build_projects_dir();done.set();return False
    monkeypatch.setattr(app,'execute_build_job',worker)
    try:
        app._wake_pending_builds();assert entered.wait(5)
        monkeypatch.setattr(app,'DB_PATH',tmp_path/'different.sqlite')
        monkeypatch.setattr(app,'PROJECTS_DIR',tmp_path/'different-projects')
        proceed.set();assert done.wait(5)
        assert str(original_db.resolve()).lower()==seen['db'].lower()
        assert seen['root']==original_root
    finally:
        proceed.set();monkeypatch.setattr(app,'DB_PATH',original_db);monkeypatch.setattr(app,'PROJECTS_DIR',original_root)


def test_build_cleanup_does_not_stop_active_source_map(tmp_path,monkeypatch):
    import json
    project=tmp_path/'paper';project.mkdir();job='a'*32
    build=tex_runtime.reserve(project,job,f'pwa-{job}-latex-1-12345678')
    mapping=tex_runtime.reserve(project,job,f'pwa-{job}-source-map-abcdef12')
    called=[]
    def cleanup(_project,path,**_options):
        called.append(path.name);return {'status':'cleaned','cleanup_verified':True}
    monkeypatch.setattr(tex_runtime,'cleanup',cleanup)
    assert tex_runtime.recover(project,job_ids={job},force=True,build_only=True)==[{'status':'cleaned','cleanup_verified':True}]
    assert called==[build.name] and mapping.exists()
    record=json.loads(mapping.read_text(encoding='utf-8'));record['labels']={};mapping.write_text(json.dumps(record),encoding='utf-8')
    called.clear();result=tex_runtime.recover(project,job_ids={job},force=True,build_only=True)
    assert called==[build.name] and any(item['status']=='invalid_lease' and not item['cleanup_verified'] for item in result)
    mapping.write_text('{}',encoding='utf-8');called.clear()
    result=tex_runtime.recover(project,job_ids={job},force=True,build_only=True)
    assert called==[build.name] and any(item['status']=='invalid_lease' and not item['cleanup_verified'] for item in result)
    record['job_id']='wrong-job';mapping.write_text(json.dumps(record),encoding='utf-8');called.clear()
    result=tex_runtime.recover(project,job_ids={job},force=True,build_only=True)
    assert called==[build.name] and any(item['status']=='invalid_lease' and not item['cleanup_verified'] for item in result)
