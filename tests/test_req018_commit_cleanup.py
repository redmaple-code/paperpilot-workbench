"""File deletion must follow durable import state, never precede commit."""
import app
from test_req018_cleanup import make_preview


def install_commit_failure(pid, target):
 conn=app.db()
 with conn:
  conn.executescript("""
  CREATE TABLE cleanup_test_parent(id INTEGER PRIMARY KEY);
  CREATE TABLE cleanup_test_child(id INTEGER REFERENCES cleanup_test_parent(id) DEFERRABLE INITIALLY DEFERRED);
  CREATE TRIGGER cleanup_test_commit_failure AFTER UPDATE OF status ON oa_imports
  WHEN NEW.status = '%s' BEGIN INSERT INTO cleanup_test_child(id) VALUES(999); END;
  """ % target)
 conn.close()


def test_confirm_commit_failure_preserves_preview_and_file(client,monkeypatch):
 pid,iid,path=make_preview(client,monkeypatch,'确认提交失败');install_commit_failure(pid,'imported')
 result=client.post(f'/api/projects/{pid}/imports/{iid}/confirm',json={'keep_attachment':False})
 assert result.status_code==503
 assert path.exists(), 'rollback must not destroy preview attachment'
 conn=app.db();assert conn.execute('SELECT status FROM oa_imports WHERE id=?',(iid,)).fetchone()[0]=='preview_ready';assert conn.execute('SELECT count(*) FROM external_evidence WHERE import_id=?',(iid,)).fetchone()[0]==0;conn.close()


def test_cancel_commit_failure_preserves_preview_and_file(client,monkeypatch):
 pid,iid,path=make_preview(client,monkeypatch,'取消提交失败');install_commit_failure(pid,'cancelled')
 result=client.post(f'/api/projects/{pid}/imports/{iid}/cancel')
 assert result.status_code==503
 assert path.exists(), 'rollback must not destroy preview attachment'
 conn=app.db();assert conn.execute('SELECT status FROM oa_imports WHERE id=?',(iid,)).fetchone()[0]=='preview_ready';conn.close()


def test_committed_cleanup_survives_process_termination(client,monkeypatch,tmp_path):
 import subprocess,sys,time
 from pathlib import Path
 for action in ('confirm','cancel'):
  pid,iid,path=make_preview(client,monkeypatch,'提交后退出'+action)
  marker=tmp_path/('committed-'+action)
  worker=subprocess.Popen([sys.executable,'-X','utf8',str(Path(__file__).with_name('oa_commit_worker.py')),str(app.DB_PATH),str(app.PROJECTS_DIR),pid,iid,action,str(marker)])
  try:
   deadline=time.monotonic()+15
   while not marker.exists() and time.monotonic()<deadline:
    assert worker.poll() is None;time.sleep(.05)
   assert marker.exists() and path.exists()
   conn=app.db();row=conn.execute('SELECT status FROM oa_imports WHERE id=?',(iid,)).fetchone();queued=conn.execute('SELECT count(*) FROM oa_cleanup_queue WHERE import_id=?',(iid,)).fetchone()[0];conn.close()
   assert row['status']==('imported' if action=='confirm' else 'cancelled') and queued==1
   worker.terminate();worker.wait(timeout=10);app.init_db()
   assert not path.exists()
   state=client.get(f'/api/projects/{pid}/imports/{iid}').json();assert not state['cleanup_pending']
   assert len(client.get(f'/api/projects/{pid}').json()['corpus'])==(1 if action=='confirm' else 0)
  finally:
   if worker.poll() is None:worker.terminate();worker.wait(timeout=10)


def test_failed_cancel_of_retained_attachment_keeps_evidence(client,monkeypatch):
 pid,iid,path=make_preview(client,monkeypatch,'已保留附件取消失败')
 assert client.post(f'/api/projects/{pid}/imports/{iid}/confirm',json={'keep_attachment':True}).json()['status']=='imported'
 install_commit_failure(pid,'cancelled')
 assert client.post(f'/api/projects/{pid}/imports/{iid}/cancel').status_code==503
 assert path.exists()
 conn=app.db();assert conn.execute('SELECT status FROM oa_imports WHERE id=?',(iid,)).fetchone()[0]=='imported';assert conn.execute('SELECT attachment_path FROM external_evidence WHERE import_id=?',(iid,)).fetchone()[0]==str(path);conn.close()


def test_cleanup_commit_failure_leaves_retryable_intent(client,monkeypatch):
 pid,iid,path=make_preview(client,monkeypatch,'清理提交失败')
 conn=app.db()
 with conn:
  conn.executescript("""
  CREATE TABLE cleanup_commit_parent(id INTEGER PRIMARY KEY);
  CREATE TABLE cleanup_commit_child(id INTEGER REFERENCES cleanup_commit_parent(id) DEFERRABLE INITIALLY DEFERRED);
  CREATE TRIGGER cleanup_commit_failure AFTER DELETE ON oa_cleanup_queue
  BEGIN INSERT INTO cleanup_commit_child(id) VALUES(999); END;
  """)
 conn.close()
 result=client.post(f'/api/projects/{pid}/imports/{iid}/confirm',json={'keep_attachment':False}).json()
 assert result['status']=='imported' and result['cleanup_pending'] and not path.exists()
 conn=app.db();assert conn.execute('SELECT count(*) FROM oa_cleanup_queue WHERE import_id=?',(iid,)).fetchone()[0]==1
 with conn:conn.execute('DROP TRIGGER cleanup_commit_failure')
 conn.close();state=client.get(f'/api/projects/{pid}/imports/{iid}').json()
 assert not state['cleanup_pending'] and len(client.get(f'/api/projects/{pid}').json()['corpus'])==1
