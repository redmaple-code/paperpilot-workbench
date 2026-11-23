import json
import os
from pathlib import Path
import subprocess
import sys

from paperpilot.services import host_budget, build_cache, tex_runtime
from tests.test_req011_builds import _project
from tests.test_req011_sources_queue import enqueue_fixture
import app


def test_machine_budget_is_shared_across_processes_and_workspaces(tmp_path):
    a=tmp_path/'database-a'/'paper';b=tmp_path/'database-b'/'paper';c=tmp_path/'database-c'/'paper'
    first=host_budget.acquire(a,'build-a')
    assert first
    script="from paperpilot.services import host_budget; import sys,json; print(json.dumps(host_budget.acquire(sys.argv[1],'map-b',kind='source_map')),flush=True); input()"
    child=subprocess.Popen([sys.executable,'-c',script,str(b)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:
        second=json.loads(child.stdout.readline())
        assert second
        assert host_budget.acquire(c,'build-c') is None
        assert host_budget.inventory()['occupied']==2
        host_budget.release(a,'build-a',first)
        third=host_budget.acquire(c,'build-c')
        assert third
        host_budget.release(c,'build-c',third)
    finally:
        child.communicate('\n',timeout=10)
    host_budget.recover()
    assert host_budget.inventory()['occupied']==0


def test_unknown_container_cleanup_retains_dead_owner_capacity(tmp_path,monkeypatch):
    claim=host_budget.acquire(tmp_path/'p','one')
    monkeypatch.setattr(tex_runtime,'owner_state',lambda row:'dead')
    monkeypatch.setattr(tex_runtime,'recover',lambda *a,**k:[{'cleanup_verified':False}])
    host_budget.recover()
    assert host_budget.inventory()['occupied']==1
    monkeypatch.setattr(tex_runtime,'recover',lambda *a,**k:[{'cleanup_verified':True}])
    host_budget.recover()
    assert host_budget.inventory()['occupied']==0


def test_failed_run_cleanup_protects_recent_and_active_preserves_history(client):
    pid=_project(client);conn=app.db();jobs=[]
    with conn:
        for i in range(5):
            job=enqueue_fixture(conn,pid,f'failure-{i}');jobs.append(job)
            conn.execute("UPDATE build_jobs SET status='failed',finished_at=?,diagnostics_json=? WHERE id=?",(f'2026-11-03T00:00:0{i}',json.dumps([{'code':'compile_failed','message':'retained diagnostic'}]),job['id']))
            run=app.PROJECTS_DIR/pid/'build-runs'/job['id'];run.mkdir(parents=True)
            (run/'failed.log').write_text('disposable build diagnostic')
    old=jobs[0]['id'];claim=host_budget.acquire(app.PROJECTS_DIR/pid,old)
    assert build_cache.prune_one(conn,app.PROJECTS_DIR,pid,old)=='protected'
    host_budget.release(app.PROJECTS_DIR/pid,old,claim)
    result=build_cache.prune_project(conn,app.PROJECTS_DIR,pid)
    assert set(result['pruned'])=={j['id'] for j in jobs[:2]}
    assert not (app.PROJECTS_DIR/pid/'build-runs'/old).exists()
    row=conn.execute('SELECT status,cache_pruned,diagnostics_json,snapshot_id FROM build_jobs WHERE id=?',(old,)).fetchone()
    assert row['status']=='failed' and row['cache_pruned'] and 'retained diagnostic' in row['diagnostics_json']
    assert conn.execute('SELECT 1 FROM build_snapshots WHERE id=?',(row['snapshot_id'],)).fetchone()
    assert (app.PROJECTS_DIR/pid/'build-runs'/jobs[-1]['id']).exists()
    conn.close()
