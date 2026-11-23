"""The opt-in session endpoint stores search data in memory only."""
import threading
from concurrent.futures import ThreadPoolExecutor
import app
from paperpilot.services import academic_search

A={'X-Search-Session':'a'*32};B={'X-Search-Session':'b'*32}


def provider(monkeypatch):
 class Provider:
  name='fixture'
  def search(self,query,filters,cursor,limit):
   return {'results':[{'id':'https://example.org/'+str(cursor),'title':'private '+query,'publication_year':2024}],'next_cursor':'next' if cursor is None else None}
 monkeypatch.setattr(academic_search,'provider_from_env',lambda:Provider())


def test_session_search_never_writes_search_tables_and_is_isolated(client,monkeypatch):
 provider(monkeypatch);pid=client.post('/api/projects',json={'name':'会话测试'}).json()['project']['id']
 result=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'private research'}).json();assert result['status']=='succeeded';sid=result['search']['id'];assert len(result['candidates'])==1
 conn=app.db();assert conn.execute('SELECT count(*) FROM academic_searches WHERE project_id=?',(pid,)).fetchone()[0]==0;assert conn.execute('SELECT count(*) FROM academic_candidates WHERE project_id=?',(pid,)).fetchone()[0]==0;conn.close()
 assert client.get(f'/api/projects/{pid}/session-search/{sid}',headers=B).status_code==404
 page=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'private research','search_id':sid,'cursor':'next'}).json();assert len(page['candidates'])==2
 assert client.delete('/api/session-search',headers=A).json()['closed']
 assert client.get(f'/api/projects/{pid}/session-search/{sid}',headers=A).status_code==404


def test_session_endpoint_rejects_invalid_token_and_unsupported_durable_save(client,monkeypatch):
 provider(monkeypatch);pid=client.post('/api/projects',json={'name':'会话边界'}).json()['project']['id']
 assert client.post(f'/api/projects/{pid}/session-search',json={'query':'paper'}).status_code==400
 result=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'paper','save':True})
 assert result.status_code==409 and result.json()['detail']['code']=='session_save_not_ready'
 conn=app.db();assert conn.execute('SELECT count(*) FROM academic_searches WHERE project_id=?',(pid,)).fetchone()[0]==0;conn.close()


def test_archiving_during_session_search_discards_result(client,monkeypatch):
 pid=client.post('/api/projects',json={'name':'会话归档'}).json()['project']['id'];started=threading.Event();release=threading.Event()
 class Provider:
  name='fixture'
  def search(self,*_):started.set();assert release.wait(10);return {'results':[{'id':'https://example.org/a','title':'late'}],'next_cursor':None}
 monkeypatch.setattr(academic_search,'provider_from_env',lambda:Provider())
 with ThreadPoolExecutor(max_workers=2) as pool:
  pending=pool.submit(client.post,f'/api/projects/{pid}/session-search',headers=A,json={'query':'late'});assert started.wait(5)
  assert client.put(f'/api/projects/{pid}/archive').json()['archived'];release.set();result=pending.result(timeout=10)
  assert result.status_code==409 and result.json()['detail']['code']=='search_project_read_only'
 conn=app.db();assert conn.execute('SELECT count(*) FROM academic_candidates WHERE project_id=?',(pid,)).fetchone()[0]==0;conn.close()


def test_explicit_save_promotes_snapshot_and_survives_session_close(client,monkeypatch):
 provider(monkeypatch);pid=client.post('/api/projects',json={'name':'明确保存会话'}).json()['project']['id']
 result=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'save this'}).json();sid=result['search']['id'];assert not result['search']['saved']
 saved=client.post(f'/api/projects/{pid}/session-search/{sid}/save',headers=A).json();assert saved['saved']
 assert client.get(f'/api/projects/{pid}/session-search/{sid}',headers=A).json()['search']['saved']
 assert client.delete('/api/session-search',headers=A).json()['closed']
 restored=client.get(f'/api/projects/{pid}/search/{sid}').json();assert restored['search']['saved'] and restored['summary']==result['summary'] and len(restored['candidates'])==1


def test_save_commit_failure_keeps_session_and_persistent_store_unmodified(client,monkeypatch):
 provider(monkeypatch);pid=client.post('/api/projects',json={'name':'会话保存失败'}).json()['project']['id'];result=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'rollback'}).json();sid=result['search']['id']
 conn=app.db()
 with conn:conn.executescript("""CREATE TABLE session_parent(id INTEGER PRIMARY KEY); CREATE TABLE session_child(id INTEGER REFERENCES session_parent(id) DEFERRABLE INITIALLY DEFERRED); CREATE TRIGGER session_commit_failure AFTER INSERT ON academic_candidates BEGIN INSERT INTO session_child(id) VALUES(999); END;""")
 conn.close();failed=client.post(f'/api/projects/{pid}/session-search/{sid}/save',headers=A);assert failed.status_code==503
 assert not client.get(f'/api/projects/{pid}/session-search/{sid}',headers=A).json()['search']['saved']
 conn=app.db();assert conn.execute('SELECT count(*) FROM academic_searches WHERE id=?',(sid,)).fetchone()[0]==0
 with conn:conn.execute('DROP TRIGGER session_commit_failure')
 conn.close();assert client.post(f'/api/projects/{pid}/session-search/{sid}/save',headers=A).json()['saved']


def test_repeated_save_detects_changed_persistent_snapshot(client,monkeypatch):
 provider(monkeypatch);pid=client.post('/api/projects',json={'name':'保存冲突'}).json()['project']['id'];sid=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'conflict'}).json()['search']['id']
 assert client.post(f'/api/projects/{pid}/session-search/{sid}/save',headers=A).json()['saved']
 conn=app.db()
 with conn:conn.execute("UPDATE academic_searches SET updated_at='changed' WHERE id=?",(sid,))
 conn.close();result=client.post(f'/api/projects/{pid}/session-search/{sid}/save',headers=A)
 assert result.status_code==409 and result.json()['detail']['code']=='search_saved_snapshot_changed'
 assert client.get(f'/api/projects/{pid}/search/{sid}').json()['search']['updated_at']=='changed'


def test_repeated_explicit_save_and_later_page_update(client,monkeypatch):
 provider(monkeypatch);pid=client.post('/api/projects',json={'name':'保存更多会话结果'}).json()['project']['id'];first=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'pages'}).json();sid=first['search']['id']
 assert client.post(f'/api/projects/{pid}/session-search/{sid}/save',headers=A).json()['saved']
 assert client.post(f'/api/projects/{pid}/session-search/{sid}/save',headers=A).json()['saved']
 second=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'pages','search_id':sid,'cursor':'next'}).json();assert len(second['candidates'])==2
 assert len(client.get(f'/api/projects/{pid}/search/{sid}').json()['candidates'])==1
 assert client.post(f'/api/projects/{pid}/session-search/{sid}/save',headers=A).json()['saved']
 assert len(client.get(f'/api/projects/{pid}/search/{sid}').json()['candidates'])==2


def test_unexpected_provider_error_is_explained_without_disk_write(client,monkeypatch):
 pid=client.post('/api/projects',json={'name':'会话异常'}).json()['project']['id']
 class Broken:
  name='fixture'
  def search(self,*_):raise ValueError('private-provider-detail')
 monkeypatch.setattr(academic_search,'provider_from_env',lambda:Broken())
 result=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'paper'}).json()
 assert result['status']=='failed' and result['error']['code']=='provider_invalid_response' and 'private-provider-detail' not in str(result)
 assert client.get(f"/api/projects/{pid}/session-search/{result['search_id']}",headers=A).json()['status']=='failed'
 conn=app.db();assert conn.execute('SELECT count(*) FROM academic_searches WHERE project_id=?',(pid,)).fetchone()[0]==0;conn.close()


def test_real_server_restart_discards_unsaved_but_keeps_explicit_save(tmp_path):
 import http.client,json,socket,sqlite3,subprocess,sys,time
 from pathlib import Path
 with socket.socket() as sock:sock.bind(('127.0.0.1',0));port=sock.getsockname()[1]
 dbpath=tmp_path/'workspace.db';projects=tmp_path/'projects';worker_path=Path(__file__).with_name('session_search_worker.py')
 def launch():
  proc=subprocess.Popen([sys.executable,'-X','utf8',str(worker_path),str(dbpath),str(projects),str(port)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
  deadline=time.monotonic()+20
  while time.monotonic()<deadline:
   if proc.poll() is not None:raise AssertionError('fixture server stopped')
   try:
    status,_=request('GET','/api/health');
    if status==200:return proc
   except OSError:time.sleep(.1)
  proc.terminate();proc.wait(timeout=10);raise AssertionError('fixture server did not start')
 def request(method,path,body=None,headers=None):
  c=http.client.HTTPConnection('127.0.0.1',port,timeout=5)
  try:
   h={'Content-Type':'application/json',**(headers or {})};c.request(method,path,body=json.dumps(body) if body is not None else None,headers=h);r=c.getresponse();return r.status,json.loads(r.read())
  finally:c.close()
 worker=launch()
 try:
  _,project=request('POST','/api/projects',{'name':'进程会话验证'});pid=project['project']['id']
  _,one=request('POST',f'/api/projects/{pid}/session-search',{'query':'unsaved marker'},A);unsaved=one['search']['id']
  _,two=request('POST',f'/api/projects/{pid}/session-search',{'query':'saved marker'},B);saved=two['search']['id']
  assert request('POST',f'/api/projects/{pid}/session-search/{saved}/save',headers=B)[1]['saved']
  conn=sqlite3.connect(dbpath);rows=conn.execute('SELECT query FROM academic_searches WHERE project_id=?',(pid,)).fetchall();conn.close();assert rows==[('saved marker',)]
  worker.terminate();worker.wait(timeout=10);worker=launch()
  assert request('GET',f'/api/projects/{pid}/session-search/{unsaved}',headers=A)[0]==404
  status,result=request('GET',f'/api/projects/{pid}/search/{saved}');assert status==200 and result['search']['saved'] and result['search']['query']=='saved marker'
 finally:
  if worker.poll() is None:worker.terminate();worker.wait(timeout=10)


def test_single_candidate_promotion_does_not_save_other_results_and_can_promote_full_snapshot(client,monkeypatch):
 class Provider:
  name='fixture'
  def search(self,*_):return {'results':[{'id':'https://example.org/one','title':'selected marker'},{'id':'https://example.org/two','title':'private other marker'}],'next_cursor':None}
 monkeypatch.setattr(academic_search,'provider_from_env',lambda:Provider())
 pid=client.post('/api/projects',json={'name':'单篇保存'}).json()['project']['id']
 result=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'test'}).json();sid=result['search']['id']
 selected=next(c for c in result['candidates'] if c['title']=='selected marker');cid=selected['id']
 path=f'/api/projects/{pid}/session-search/{sid}/candidates/{cid}/save'
 assert client.post(path,headers=B).status_code==409
 assert client.post(path,headers=A).status_code==200
 assert client.post(path,headers=A).status_code==200
 conn=app.db();rows=conn.execute('SELECT title,raw_json FROM academic_candidates WHERE search_id=?',(sid,)).fetchall();search=conn.execute('SELECT * FROM academic_searches WHERE id=?',(sid,)).fetchone();conn.close()
 assert len(rows)==1 and rows[0]['title']=='selected marker' and 'private other marker' not in str(dict(search)) and 'private other marker' not in rows[0]['raw_json']
 assert search['status']=='partial' and not search['saved']
 restored=client.get(f'/api/projects/{pid}/search/{sid}').json();assert len(restored['candidates'])==1 and restored['summary']['partial_saved_candidates']
 refused=client.post(f'/api/projects/{pid}/search/{sid}/save');assert refused.status_code==409 and refused.json()['detail']['code']=='partial_snapshot'
 assert client.post(f'/api/projects/{pid}/session-search/{sid}/save',headers=A).status_code==200
 restored=client.get(f'/api/projects/{pid}/search/{sid}').json();assert len(restored['candidates'])==2 and restored['search']['saved']
 assert next(c for c in restored['candidates'] if c['id']==cid)['saved']


def test_single_candidate_commit_failure_rolls_back_and_preserves_session(client,monkeypatch):
 provider(monkeypatch);pid=client.post('/api/projects',json={'name':'单篇回滚'}).json()['project']['id']
 result=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'test'}).json();sid=result['search']['id'];cid=result['candidates'][0]['id']
 conn=app.db()
 with conn:conn.executescript("CREATE TABLE promote_parent(id INTEGER PRIMARY KEY); CREATE TABLE promote_child(id INTEGER REFERENCES promote_parent(id) DEFERRABLE INITIALLY DEFERRED); CREATE TRIGGER promote_failure AFTER INSERT ON academic_candidates BEGIN INSERT INTO promote_child(id) VALUES(999); END;")
 conn.close();path=f'/api/projects/{pid}/session-search/{sid}/candidates/{cid}/save'
 assert client.post(path,headers=A).status_code==503
 assert not client.get(f'/api/projects/{pid}/session-search/{sid}',headers=A).json()['candidates'][0]['saved']
 conn=app.db();assert conn.execute('SELECT count(*) FROM academic_searches WHERE id=?',(sid,)).fetchone()[0]==0
 with conn:conn.execute('DROP TRIGGER promote_failure')
 conn.close();assert client.post(path,headers=A).status_code==200


def test_candidate_promotion_rejects_read_only_and_external_change(client,monkeypatch):
 provider(monkeypatch);pid=client.post('/api/projects',json={'name':'单篇冲突'}).json()['project']['id']
 result=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'test'}).json();sid=result['search']['id'];cid=result['candidates'][0]['id']
 path=f'/api/projects/{pid}/session-search/{sid}/candidates/{cid}/save'
 assert client.post(path,headers=A).status_code==200
 assert client.delete(f'/api/projects/{pid}/search/candidates/{cid}/save').status_code==200
 assert client.post(path,headers=A).json()['detail']['code']=='search_saved_snapshot_changed'
 assert client.put(f'/api/projects/{pid}/archive').json()['archived']
 assert client.post(path,headers=A).json()['detail']['code']=='search_project_read_only'


def test_unsave_partial_prunes_unreferenced_and_keeps_handoff(client,monkeypatch):
 class Provider:
  name='fixture'
  def search(self,*_):return {'results':[{'id':'https://example.org/a','title':'selected one'},{'id':'https://example.org/b','title':'selected two'}],'next_cursor':None}
 monkeypatch.setattr(academic_search,'provider_from_env',lambda:Provider())
 pid=client.post('/api/projects',json={'name':'撤销单篇保存'}).json()['project']['id']
 result=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'test'}).json();sid=result['search']['id'];one,two=[c['id'] for c in result['candidates']]
 for cid in (one,two):assert client.post(f'/api/projects/{pid}/session-search/{sid}/candidates/{cid}/save',headers=A).status_code==200
 assert client.delete(f'/api/projects/{pid}/search/candidates/{one}/save').status_code==200
 restored=client.get(f'/api/projects/{pid}/search/{sid}').json();assert [c['id'] for c in restored['candidates']]==[two] and restored['summary']['counts']['total']==1
 handoff=client.post(f'/api/projects/{pid}/search/handoff',json={'candidate_id':two,'idempotency_key':'keep-partial-handoff'});assert handoff.status_code==200
 assert client.delete(f'/api/projects/{pid}/search/candidates/{two}/save').status_code==200
 conn=app.db();assert conn.execute('SELECT count(*) FROM academic_candidates WHERE id=?',(two,)).fetchone()[0]==1;assert conn.execute('SELECT count(*) FROM reference_drafts WHERE id=?',(handoff.json()['reference_draft']['id'],)).fetchone()[0]==1;conn.close()
 assert client.post(f'/api/projects/{pid}/session-search/{sid}/save',headers=A).json()['detail']['code']=='search_saved_snapshot_changed'


def test_session_handoff_is_atomic_private_and_idempotent(client,monkeypatch):
 class Provider:
  name='fixture'
  def search(self,*_):return {'results':[{'id':'https://example.org/a','title':'handoff selected'},{'id':'https://example.org/b','title':'other private candidate'}],'next_cursor':None}
 monkeypatch.setattr(academic_search,'provider_from_env',lambda:Provider())
 pid=client.post('/api/projects',json={'name':'会话文献交接'}).json()['project']['id']
 result=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'research'}).json();sid=result['search']['id'];cid=next(c['id'] for c in result['candidates'] if c['title']=='handoff selected')
 path=f'/api/projects/{pid}/session-search/{sid}/handoff';payload={'candidate_id':cid,'idempotency_key':'session-handoff-1'}
 assert client.post(path,headers=B,json=payload).status_code==409
 first=client.post(path,headers=A,json=payload);assert first.status_code==200;data=first.json()
 replay=client.post(path,headers=A,json=payload);assert replay.status_code==200 and replay.json()['idempotent_replay'] and replay.json()['reference_draft']['id']==data['reference_draft']['id']
 conn=app.db();rows=conn.execute('SELECT title,raw_json FROM academic_candidates WHERE search_id=?',(sid,)).fetchall();traces=conn.execute('SELECT source_trace_json FROM academic_handoffs WHERE project_id=?',(pid,)).fetchall();conn.close()
 assert len(rows)==1 and rows[0]['title']=='handoff selected' and len(traces)==1 and 'other private candidate' not in traces[0][0] and 'other private candidate' not in rows[0]['raw_json']
 assert not client.get(f'/api/projects/{pid}/session-search/{sid}',headers=A).json()['candidates'][0]['saved']
 assert client.post(f'/api/projects/{pid}/session-search/{sid}/save',headers=A).status_code==200


def test_session_handoff_commit_failure_leaves_no_durable_data(client,monkeypatch):
 provider(monkeypatch);pid=client.post('/api/projects',json={'name':'交接提交故障'}).json()['project']['id'];result=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'rollback'}).json();sid=result['search']['id'];cid=result['candidates'][0]['id']
 conn=app.db()
 with conn:conn.executescript("CREATE TABLE handoff_parent(id INTEGER PRIMARY KEY); CREATE TABLE handoff_child(id INTEGER REFERENCES handoff_parent(id) DEFERRABLE INITIALLY DEFERRED); CREATE TRIGGER handoff_commit_failure AFTER INSERT ON academic_handoffs BEGIN INSERT INTO handoff_child(id) VALUES(999); END;")
 conn.close();path=f'/api/projects/{pid}/session-search/{sid}/handoff';payload={'candidate_id':cid,'idempotency_key':'session-fail'}
 assert client.post(path,headers=A,json=payload).status_code==503
 conn=app.db();assert conn.execute('SELECT count(*) FROM academic_searches WHERE id=?',(sid,)).fetchone()[0]==0;assert conn.execute('SELECT count(*) FROM reference_drafts WHERE project_id=?',(pid,)).fetchone()[0]==0
 with conn:conn.execute('DROP TRIGGER handoff_commit_failure')
 conn.close();assert client.post(path,headers=A,json=payload).status_code==200


def test_session_handoff_reused_key_does_not_promote_other_candidate(client,monkeypatch):
 provider(monkeypatch);pid=client.post('/api/projects',json={'name':'交接键冲突'}).json()['project']['id']
 first=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'first'}).json();sid1=first['search']['id'];cid1=first['candidates'][0]['id']
 assert client.post(f'/api/projects/{pid}/session-search/{sid1}/handoff',headers=A,json={'candidate_id':cid1,'idempotency_key':'reuse-check'}).status_code==200
 second=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'second'}).json();sid2=second['search']['id'];cid2=second['candidates'][0]['id']
 response=client.post(f'/api/projects/{pid}/session-search/{sid2}/handoff',headers=A,json={'candidate_id':cid2,'idempotency_key':'reuse-check'})
 assert response.status_code==409 and response.json()['detail']['code']=='idempotency_key_reused'
 conn=app.db();assert conn.execute('SELECT count(*) FROM academic_searches WHERE id=?',(sid2,)).fetchone()[0]==0;assert conn.execute('SELECT count(*) FROM academic_candidates WHERE id=?',(cid2,)).fetchone()[0]==0;conn.close()
 assert client.post(f'/api/projects/{pid}/session-search/{sid2}/handoff',headers=A,json={'candidate_id':cid2,'idempotency_key':'new-key'}).status_code==200


def test_session_oa_import_promotes_only_selected_and_reuses_existing_fetch(client,monkeypatch):
 class Provider:
  name='openalex'
  def search(self,*_):return {'results':[{'id':'https://example.org/a','title':'selected oa','open_access':{'is_oa':True},'best_oa_location':{'is_oa':True,'pdf_url':'https://example.org/paper.pdf'}},{'id':'https://example.org/b','title':'private other'}],'next_cursor':None}
 monkeypatch.setattr(academic_search,'provider_from_env',lambda:Provider())
 monkeypatch.setattr(app.oa_imports,'fetch_public',lambda _:(b'fixture','text/html','hash'))
 monkeypatch.setattr(app.oa_imports,'extract_preview',lambda *_:{'text_preview':'oa preview','fragments':[{'location':'p1','text':'oa preview'}]})
 pid=client.post('/api/projects',json={'name':'会话OA导入'}).json()['project']['id'];result=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'test'}).json();sid=result['search']['id'];selected=next(c for c in result['candidates'] if c['title']=='selected oa');cid=selected['id'];assert selected['access_status']=='open_access'
 path=f'/api/projects/{pid}/session-search/{sid}/imports';payload={'candidate_id':cid,'idempotency_key':'session-oa','defer_fetch':True}
 assert client.post(path,headers=B,json=payload).status_code==409
 created=client.post(path,headers=A,json=payload);assert created.status_code==200 and created.json()['status']=='created';iid=created.json()['import']['id']
 replay=client.post(path,headers=A,json=payload);assert replay.status_code==200 and replay.json()['idempotent_replay'] and replay.json()['import']['id']==iid
 assert client.post(f'/api/projects/{pid}/imports/{iid}/cancel').json()['status']=='cancelled'
 retry=client.post(path,headers=A,json=payload);assert retry.status_code==200 and retry.json()['status']=='created' and retry.json()['import']['id']==iid
 conn=app.db();rows=conn.execute('SELECT title,raw_json FROM academic_candidates WHERE search_id=?',(sid,)).fetchall();imp=conn.execute('SELECT * FROM oa_imports WHERE id=?',(iid,)).fetchone();conn.close()
 assert len(rows)==1 and rows[0]['title']=='selected oa' and 'private other' not in rows[0]['raw_json'] and 'private other' not in str(dict(imp))
 fetched=client.post(f'/api/projects/{pid}/imports/{iid}/fetch').json();assert fetched['status']=='preview_ready' and fetched['import']['preview']['text_preview']=='oa preview'
 assert client.post(f'/api/projects/{pid}/imports/{iid}/confirm',json={}).json()['status']=='imported'
 assert client.post(f'/api/projects/{pid}/session-search/{sid}/save',headers=A).status_code==200


def test_session_oa_conflict_and_commit_failure_leave_no_partial_candidate(client,monkeypatch):
 provider(monkeypatch);pid=client.post('/api/projects',json={'name':'会话OA回滚'}).json()['project']['id'];first=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'first'}).json();sid=first['search']['id'];cid=first['candidates'][0]['id'];path=f'/api/projects/{pid}/session-search/{sid}/imports';payload={'candidate_id':cid,'idempotency_key':'oa-rollback','defer_fetch':True}
 assert client.post(path,headers=A,json={**payload,'url':'https://example.org'}).status_code==400
 conn=app.db()
 with conn:conn.executescript("CREATE TABLE session_oa_parent(id INTEGER PRIMARY KEY); CREATE TABLE session_oa_child(id INTEGER REFERENCES session_oa_parent(id) DEFERRABLE INITIALLY DEFERRED); CREATE TRIGGER session_oa_failure AFTER INSERT ON oa_imports BEGIN INSERT INTO session_oa_child(id) VALUES(999); END;")
 conn.close();assert client.post(path,headers=A,json=payload).status_code==503
 conn=app.db();assert conn.execute('SELECT count(*) FROM academic_searches WHERE id=?',(sid,)).fetchone()[0]==0;assert conn.execute('SELECT count(*) FROM oa_imports WHERE project_id=?',(pid,)).fetchone()[0]==0
 with conn:conn.execute('DROP TRIGGER session_oa_failure')
 conn.close();assert client.post(path,headers=A,json=payload).status_code==200
 second=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'second'}).json();sid2=second['search']['id'];cid2=second['candidates'][0]['id']
 conflict=client.post(f'/api/projects/{pid}/session-search/{sid2}/imports',headers=A,json={'candidate_id':cid2,'idempotency_key':'oa-rollback','defer_fetch':True});assert conflict.status_code==409 and conflict.json()['detail']['code']=='import_idempotency_conflict'
 conn=app.db();assert conn.execute('SELECT count(*) FROM academic_searches WHERE id=?',(sid2,)).fetchone()[0]==0;conn.close()
 other=client.post('/api/projects',json={'name':'另一篇论文'}).json()['project']['id']
 assert client.post(f'/api/projects/{other}/session-search/{sid2}/imports',headers=A,json={'candidate_id':cid2,'idempotency_key':'other','defer_fetch':True}).status_code==409
 assert client.put(f'/api/projects/{pid}/archive').json()['archived']
 blocked=client.post(f'/api/projects/{pid}/session-search/{sid2}/imports',headers=A,json={'candidate_id':cid2,'idempotency_key':'new','defer_fetch':True})
 assert blocked.status_code==409
 conn=app.db();assert conn.execute('SELECT count(*) FROM academic_searches WHERE id=?',(sid2,)).fetchone()[0]==0;conn.close()


def test_session_unsave_can_reselect_without_resurrecting_deleted_history(client,monkeypatch):
 provider(monkeypatch);pid=client.post('/api/projects',json={'name':'会话取消保存'}).json()['project']['id'];result=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'private'}).json();sid=result['search']['id'];cid=result['candidates'][0]['id'];path=f'/api/projects/{pid}/session-search/{sid}/candidates/{cid}/save'
 assert client.post(path,headers=A).status_code==200
 assert client.delete(path,headers=B).status_code==409
 assert client.delete(path,headers=A).status_code==200
 conn=app.db();assert conn.execute('SELECT count(*) FROM academic_searches WHERE id=?',(sid,)).fetchone()[0]==0;conn.close()
 assert not client.get(f'/api/projects/{pid}/session-search/{sid}',headers=A).json()['candidates'][0]['saved']
 assert client.post(path,headers=A).status_code==200
 assert client.get(f'/api/projects/{pid}/search/{sid}').json()['candidates'][0]['saved']


def test_worker_affinity_rejects_wrong_process_without_mutating_session(client,monkeypatch):
 provider(monkeypatch);pid=client.post('/api/projects',json={'name':'会话进程隔离'}).json()['project']['id']
 first=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'worker'});worker=first.headers['X-Search-Worker'];sid=first.json()['search']['id'];cid=first.json()['candidates'][0]['id']
 wrong={**A,'X-Search-Worker':'0'*32};refused=client.post(f'/api/projects/{pid}/session-search/{sid}/candidates/{cid}/save',headers=wrong)
 assert refused.status_code==409 and refused.json()['detail']['code']=='search_worker_changed'
 conn=app.db();assert conn.execute('SELECT count(*) FROM academic_searches WHERE id=?',(sid,)).fetchone()[0]==0;conn.close()
 assert client.post(f'/api/projects/{pid}/session-search/{sid}/candidates/{cid}/save',headers={**A,'X-Search-Worker':worker}).status_code==200


def test_real_two_workers_refuse_wrong_affinity_without_disk_write(tmp_path):
 import http.client,json,socket,subprocess,sys,time
 from pathlib import Path
 def port():
  with socket.socket() as sock:sock.bind(('127.0.0.1',0));return sock.getsockname()[1]
 ports=[port(),port()]
 while ports[1]==ports[0]:ports[1]=port()
 worker_path=Path(__file__).with_name('session_search_worker.py');processes=[]
 def request(index,method,path,body=None,headers=None):
  c=http.client.HTTPConnection('127.0.0.1',ports[index],timeout=5)
  try:
   c.request(method,path,body=json.dumps(body) if body is not None else None,headers={'Content-Type':'application/json',**(headers or {})});r=c.getresponse();return r.status,dict(r.getheaders()),json.loads(r.read())
  finally:c.close()
 try:
  for index in range(2):
   proc=subprocess.Popen([sys.executable,'-X','utf8',str(worker_path),str(tmp_path/'shared.db'),str(tmp_path/'projects'),str(ports[index])],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL);processes.append(proc);deadline=time.monotonic()+20
   while time.monotonic()<deadline:
    if proc.poll() is not None:raise AssertionError('worker exited')
    try:
     if request(index,'GET','/api/health')[0]==200:break
    except OSError:time.sleep(.1)
   else:raise AssertionError('worker start timed out')
  pid=request(0,'POST','/api/projects',{'name':'双进程隔离'})[2]['project']['id']
  status,headers,result=request(0,'POST',f'/api/projects/{pid}/session-search',{'query':'worker isolated'},A);assert status==200
  worker=headers['x-search-worker'];sid=result['search']['id'];cid=result['candidates'][0]['id'];path=f'/api/projects/{pid}/session-search/{sid}/candidates/{cid}/save';bound={**A,'X-Search-Worker':worker}
  status,_,error=request(1,'POST',path,headers=bound);assert status==409 and error['detail']['code']=='search_worker_changed'
  assert request(0,'GET',f'/api/projects/{pid}/searches')[2]['searches']==[]
  assert request(0,'POST',path,headers=bound)[0]==200
 finally:
  for proc in processes:
   if proc.poll() is None:proc.terminate()
  for proc in processes:proc.wait(timeout=10)


def test_saved_candidate_upgraded_on_later_page_uses_current_oa_metadata(client,monkeypatch):
 class Provider:
  name='fixture'
  def search(self,query,filters,cursor,limit):
   if not cursor:return {'results':[{'__provider':'crossref','title':['same paper'],'DOI':'10.1000/same','URL':'https://example.org/landing'}],'next_cursor':'page2'}
   return {'results':[{'__provider':'openalex','id':'https://example.org/oa','title':'same paper','doi':'10.1000/same','open_access':{'is_oa':True},'best_oa_location':{'is_oa':True,'pdf_url':'https://example.org/paper.pdf'}}],'next_cursor':None}
 monkeypatch.setattr(academic_search,'provider_from_env',lambda:Provider())
 pid=client.post('/api/projects',json={'name':'跨页OA更新'}).json()['project']['id'];first=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'paper'}).json();sid=first['search']['id'];cid=first['candidates'][0]['id'];assert first['candidates'][0]['access_status']=='metadata_only'
 assert client.post(f'/api/projects/{pid}/session-search/{sid}/candidates/{cid}/save',headers=A).status_code==200
 second=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'paper','search_id':sid,'cursor':'page2'}).json();candidate=next(c for c in second['candidates'] if c['id']==cid);assert candidate['saved'] and candidate['access_status']=='open_access'
 restored=client.get(f'/api/projects/{pid}/search/{sid}').json();assert len(restored['candidates'])==1 and restored['candidates'][0]['access_status']=='open_access'
 assert {'crossref','openalex'}=={source['provider'] for source in restored['candidates'][0]['sources']}
 imported=client.post(f'/api/projects/{pid}/session-search/{sid}/imports',headers=A,json={'candidate_id':cid,'defer_fetch':True});assert imported.status_code==200 and imported.json()['status']=='created'
 conn=app.db();row=conn.execute('SELECT access_status,oa_url,sources_json FROM academic_candidates WHERE id=?',(cid,)).fetchone();conn.close();assert row['access_status']=='open_access' and row['oa_url']=='https://example.org/paper.pdf' and 'crossref' in row['sources_json'] and 'openalex' in row['sources_json']


def test_saved_candidate_page_sync_conflict_preserves_new_results_and_external_history(client,monkeypatch):
 provider(monkeypatch);pid=client.post('/api/projects',json={'name':'跨页保存冲突'}).json()['project']['id'];first=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'pages'}).json();sid=first['search']['id'];cid=first['candidates'][0]['id']
 assert client.post(f'/api/projects/{pid}/session-search/{sid}/candidates/{cid}/save',headers=A).status_code==200
 conn=app.db()
 with conn:conn.execute("UPDATE academic_searches SET updated_at='external-change' WHERE id=?",(sid,))
 conn.close();result=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'pages','search_id':sid,'cursor':'next'}).json()
 assert result['ok'] and len(result['candidates'])==2 and result['warning']['code']=='saved_candidate_refresh_failed'
 conn=app.db();assert conn.execute('SELECT updated_at FROM academic_searches WHERE id=?',(sid,)).fetchone()[0]=='external-change';assert conn.execute('SELECT count(*) FROM academic_candidates WHERE search_id=?',(sid,)).fetchone()[0]==1;conn.close()


def test_saved_candidate_page_sync_commit_failure_preserves_session_results(client,monkeypatch):
 provider(monkeypatch);pid=client.post('/api/projects',json={'name':'跨页保存失败'}).json()['project']['id'];first=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'pages'}).json();sid=first['search']['id'];cid=first['candidates'][0]['id']
 assert client.post(f'/api/projects/{pid}/session-search/{sid}/candidates/{cid}/save',headers=A).status_code==200
 conn=app.db()
 with conn:conn.executescript("CREATE TABLE page_parent(id INTEGER PRIMARY KEY); CREATE TABLE page_child(id INTEGER REFERENCES page_parent(id) DEFERRABLE INITIALLY DEFERRED); CREATE TRIGGER page_refresh_failure AFTER UPDATE ON academic_candidates BEGIN INSERT INTO page_child(id) VALUES(999); END;")
 conn.close();result=client.post(f'/api/projects/{pid}/session-search',headers=A,json={'query':'pages','search_id':sid,'cursor':'next'}).json();assert result['ok'] and len(result['candidates'])==2 and result['warning']['code']=='saved_candidate_refresh_failed'
 conn=app.db();assert conn.execute('SELECT count(*) FROM academic_candidates WHERE search_id=?',(sid,)).fetchone()[0]==1
 with conn:conn.execute('DROP TRIGGER page_refresh_failure')
 conn.close();assert client.post(f'/api/projects/{pid}/session-search/{sid}/candidates/{cid}/save',headers=A).status_code==200


def test_legacy_search_requires_explicit_save_and_preserves_existing_unsaved_history(client,monkeypatch):
 provider(monkeypatch);pid=client.post('/api/projects',json={'name':'旧接口迁移'}).json()['project']['id']
 rejected=client.post(f'/api/projects/{pid}/search',json={'query':'private legacy'})
 assert rejected.status_code==409 and rejected.json()['detail']['code']=='legacy_search_requires_explicit_save'
 conn=app.db();assert conn.execute('SELECT count(*) FROM academic_searches WHERE project_id=?',(pid,)).fetchone()[0]==0;conn.close()
 created=client.post(f'/api/projects/{pid}/search',json={'query':'old history','save':True}).json();sid=created['search']['id']
 conn=app.db()
 with conn:conn.execute('UPDATE academic_searches SET saved=0 WHERE id=?',(sid,))
 conn.close();before=client.get(f'/api/projects/{pid}/search/{sid}').json();assert not before['search']['saved']
 refused=client.post(f'/api/projects/{pid}/search',json={'query':'old history','search_id':sid,'cursor':'next'});assert refused.status_code==409 and refused.json()['detail']['code']=='legacy_search_requires_explicit_save'
 assert client.get(f'/api/projects/{pid}/search/{sid}').json()==before
 assert client.post(f'/api/projects/{pid}/search/{sid}/save').json()['saved']
 continued=client.post(f'/api/projects/{pid}/search',json={'query':'old history','search_id':sid,'cursor':'next'});assert continued.status_code==200 and len(continued.json()['candidates'])==2
