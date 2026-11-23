"""OA attachment retention and durable, identity-scoped cleanup."""
import subprocess,sys,time
from pathlib import Path
import app


def make_preview(client,monkeypatch,name='附件测试'):
 pid=client.post('/api/projects',json={'name':name}).json()['project']['id']
 monkeypatch.setattr(app.oa_imports,'fetch_public',lambda _:(b'attachment','text/html','fixture'))
 monkeypatch.setattr(app.oa_imports,'extract_preview',lambda *_:{'text_preview':'内容','fragments':[{'text':'内容'}]})
 result=client.post(f'/api/projects/{pid}/imports',json={'url':'https://example.org/file'}).json()
 iid=result['import']['id'];conn=app.db();row=conn.execute('SELECT file_path FROM oa_imports WHERE id=?',(iid,)).fetchone();conn.close()
 return pid,iid,Path(row['file_path'])


def test_failed_cleanup_is_reported_and_retried(client,monkeypatch):
 pid,iid,path=make_preview(client,monkeypatch);original=Path.unlink
 def fail_once(self,*args,**kwargs):
  if self==path:raise PermissionError('fixture locked')
  return original(self,*args,**kwargs)
 monkeypatch.setattr(Path,'unlink',fail_once)
 result=client.post(f'/api/projects/{pid}/imports/{iid}/confirm',json={'keep_attachment':False}).json()
 assert result['status']=='imported' and result['cleanup_pending'] and path.exists()
 conn=app.db();assert conn.execute('SELECT last_error FROM oa_cleanup_queue WHERE import_id=?',(iid,)).fetchone()[0]=='cleanup_unavailable';conn.close()
 monkeypatch.setattr(Path,'unlink',original)
 result=client.get(f'/api/projects/{pid}/imports/{iid}').json()
 assert not result['cleanup_pending'] and not path.exists()
 assert len(client.get(f'/api/projects/{pid}').json()['corpus'])==1


def test_retained_attachment_and_missing_attachment_are_honest(client,monkeypatch):
 pid,iid,path=make_preview(client,monkeypatch,'保留附件')
 result=client.post(f'/api/projects/{pid}/imports/{iid}/confirm',json={'keep_attachment':True}).json()
 assert not result['cleanup_pending'] and path.is_file();app.init_db();assert path.is_file()
 conn=app.db();assert conn.execute('SELECT attachment_path FROM external_evidence WHERE import_id=?',(iid,)).fetchone()[0]==str(path);conn.close()
 cancelled=client.post(f'/api/projects/{pid}/imports/{iid}/cancel').json();assert not cancelled['cleanup_pending'] and not path.exists()
 conn=app.db();assert conn.execute('SELECT attachment_path FROM external_evidence WHERE import_id=?',(iid,)).fetchone()[0]=='';conn.close()
 pid2,iid2,path2=make_preview(client,monkeypatch,'缺失附件');path2.unlink()
 denied=client.post(f'/api/projects/{pid2}/imports/{iid2}/confirm',json={'keep_attachment':True})
 assert denied.status_code==409 and denied.json()['detail']['code']=='attachment_unavailable'
 assert not client.get(f'/api/projects/{pid2}').json()['corpus']
 assert client.post(f'/api/projects/{pid2}/imports/{iid2}/confirm',json={'keep_attachment':False}).json()['status']=='imported'


def test_real_worker_killed_after_write_is_recovered_and_file_cleaned(client,tmp_path):
 pid=client.post('/api/projects',json={'name':'写入后中断'}).json()['project']['id']
 iid=client.post(f'/api/projects/{pid}/imports',json={'url':'https://example.org/crash','defer_fetch':True}).json()['import']['id'];marker=tmp_path/'written'
 worker=subprocess.Popen([sys.executable,'-X','utf8',str(Path(__file__).with_name('oa_cleanup_worker.py')),str(app.DB_PATH),str(app.PROJECTS_DIR),pid,iid,str(marker)])
 try:
  deadline=time.monotonic()+15
  while not marker.exists() and time.monotonic()<deadline:
   assert worker.poll() is None;time.sleep(.05)
  assert marker.exists();files=list((app.PROJECTS_DIR/pid/'imports'/iid).glob('source-*.bin'));assert len(files)==1
  app.init_db();assert files[0].exists()
  # Simulate an interrupted fetch recorded by the previous schema version.
  conn=app.db()
  with conn:conn.execute('DELETE FROM oa_cleanup_queue WHERE import_id=?',(iid,))
  conn.close()
  worker.terminate();worker.wait(timeout=10);app.init_db()
  assert not files[0].exists();result=client.get(f'/api/projects/{pid}/imports/{iid}').json()
  assert result['import']['status']=='failed' and not result['cleanup_pending']
 finally:
  if worker.poll() is None:worker.terminate();worker.wait(timeout=10)


def test_invalid_cleanup_identity_is_never_deleted(client,tmp_path):
 pid=client.post('/api/projects',json={'name':'未知文件'}).json()['project']['id'];sentinel=tmp_path/'outside.bin';sentinel.write_bytes(b'keep')
 conn=app.db()
 with conn:
  conn.execute('INSERT INTO oa_cleanup_queue(project_id,import_id,attempt_id) VALUES(?,?,?)',(pid,'../outside','invalid'))
  app._oa_drain_cleanup(conn,pid)
  row=conn.execute('SELECT last_error FROM oa_cleanup_queue WHERE project_id=?',(pid,)).fetchone();assert row[0]=='cleanup_unavailable'
 conn.close();assert sentinel.read_bytes()==b'keep'


def test_processing_failure_reports_pending_cleanup(client,monkeypatch):
 pid=client.post('/api/projects',json={'name':'解析和清理失败'}).json()['project']['id']
 monkeypatch.setattr(app.oa_imports,'fetch_public',lambda _:(b'file','text/html','fixture'))
 monkeypatch.setattr(app.oa_imports,'extract_preview',lambda *_:(_ for _ in ()).throw(ValueError('parser failure')))
 original=Path.unlink
 def locked(self,*args,**kwargs):
  if self.name.startswith('source-'):raise PermissionError('fixture locked')
  return original(self,*args,**kwargs)
 monkeypatch.setattr(Path,'unlink',locked)
 result=client.post(f'/api/projects/{pid}/imports',json={'url':'https://example.org/fail'}).json()
 assert result['status']=='failed' and result['cleanup_pending']
 monkeypatch.setattr(Path,'unlink',original)
 assert not client.get(f"/api/projects/{pid}/imports/{result['import']['id']}").json()['cleanup_pending']
