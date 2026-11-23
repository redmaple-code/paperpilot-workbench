"""REQ-019 archive read-only and explicit deletion boundaries."""
import threading
from concurrent.futures import ThreadPoolExecutor
import app
from paperpilot.services import academic_search as search


def setup_search(client,monkeypatch):
 pid=client.post('/api/projects',json={'name':'历史治理'}).json()['project']['id']
 class Provider:
  name='fixture'
  def search(self,query,filters,cursor,limit):return {'results':[{'id':'https://example.org/a','title':'paper','doi':'10.1000/paper','publication_year':2024}],'next_cursor':None}
 monkeypatch.setattr(search,'provider_from_env',lambda:Provider())
 result=client.post(f'/api/projects/{pid}/search',json={'save':True,'query':'paper'}).json();return pid,result['search']['id'],result['candidates'][0]['id']


def test_archived_search_is_read_only(client,monkeypatch):
 pid,sid,cid=setup_search(client,monkeypatch);assert client.put(f'/api/projects/{pid}/archive').json()['archived']
 for method,path,body in [('post',f'/api/projects/{pid}/search',{'query':'new'}),('post',f'/api/projects/{pid}/search/{sid}/save',{}),('post',f'/api/projects/{pid}/search/candidates/{cid}/save',{}),('delete',f'/api/projects/{pid}/search/candidates/{cid}/save',None),('post',f'/api/projects/{pid}/search/handoff',{'candidate_id':cid}),('delete',f'/api/projects/{pid}/search/{sid}',None)]:
  response=getattr(client,method)(path,**({'json':body} if body is not None and method!='delete' else {}));assert response.status_code==409,(path,response.text)
  assert response.json()['detail']['code']=='search_project_read_only'
 assert client.get(f'/api/projects/{pid}/search/{sid}').status_code==200
 assert not client.put(f'/api/projects/{pid}/archive').json()['archived']
 assert client.post(f'/api/projects/{pid}/search/{sid}/save').status_code==200


def test_delete_search_preserves_draft_and_reference_provenance(client,monkeypatch):
 pid,sid,cid=setup_search(client,monkeypatch);handoff=client.post(f'/api/projects/{pid}/search/handoff',json={'candidate_id':cid}).json();draft=handoff['reference_draft'];trace=draft['source_trace']
 result=client.delete(f'/api/projects/{pid}/search/{sid}');assert result.status_code==200
 assert client.get(f'/api/projects/{pid}/search/{sid}').status_code==404
 assert client.get('/api/references/drafts/'+draft['id']).json()['source_trace']==trace
 confirmed=client.post('/api/references/drafts/'+draft['id']+'/confirm',json={'request_id':'confirm-after-delete','decision':'create'}).json();assert confirmed['reference']['source_trace']==trace
 conn=app.db();assert conn.execute('SELECT count(*) FROM academic_candidates WHERE search_id=?',(sid,)).fetchone()[0]==0;conn.close()


def test_archive_during_search_discards_late_result(client,monkeypatch):
 pid=client.post('/api/projects',json={'name':'归档中检索'}).json()['project']['id'];started=threading.Event();release=threading.Event()
 class Provider:
  name='fixture'
  def search(self,*_):started.set();assert release.wait(10);return {'results':[{'id':'https://example.org/late','title':'late'}],'next_cursor':None}
 monkeypatch.setattr(search,'provider_from_env',lambda:Provider())
 with ThreadPoolExecutor(max_workers=2) as pool:
  pending=pool.submit(client.post,f'/api/projects/{pid}/search',json={'save':True,'query':'slow'});assert started.wait(5)
  assert client.put(f'/api/projects/{pid}/archive').json()['archived'];release.set();result=pending.result(timeout=10).json();assert result['status']=='superseded'
 history=client.get(f'/api/projects/{pid}/searches').json()['searches'];assert history[0]['status']=='superseded' and history[0]['candidate_count']==0


def test_deletion_of_running_search_prevents_late_reappearance_and_cross_project_delete(client,monkeypatch):
 pid=client.post('/api/projects',json={'name':'删除执行中的搜索'}).json()['project']['id'];other=client.post('/api/projects',json={'name':'其他论文'}).json()['project']['id'];started=threading.Event();release=threading.Event()
 class Provider:
  name='fixture'
  def search(self,*_):started.set();assert release.wait(10);return {'results':[{'id':'https://example.org/late','title':'late'}],'next_cursor':None}
 monkeypatch.setattr(search,'provider_from_env',lambda:Provider())
 with ThreadPoolExecutor(max_workers=2) as pool:
  pending=pool.submit(client.post,f'/api/projects/{pid}/search',json={'save':True,'query':'slow'});assert started.wait(5)
  conn=app.db();sid=conn.execute('SELECT id FROM academic_searches WHERE project_id=?',(pid,)).fetchone()[0];conn.close()
  assert client.delete(f'/api/projects/{other}/search/{sid}').status_code==404
  assert client.delete(f'/api/projects/{pid}/search/{sid}').json()['deleted'];release.set();assert pending.result(timeout=10).json()['status']=='superseded'
 assert client.get(f'/api/projects/{pid}/search/{sid}').status_code==404


def test_delete_search_preserves_existing_oa_import(client,monkeypatch):
 pid,sid,cid=setup_search(client,monkeypatch)
 conn=app.db()
 with conn:conn.execute("UPDATE academic_candidates SET access_status='open_access',oa_url='https://example.org/paper' WHERE id=?",(cid,))
 conn.close();monkeypatch.setattr(app.oa_imports,'fetch_public',lambda _:(b'file','text/html','fixture'));monkeypatch.setattr(app.oa_imports,'extract_preview',lambda *_:{'text_preview':'kept evidence','fragments':[{'text':'kept evidence'}]})
 created=client.post(f'/api/projects/{pid}/imports',json={'candidate_id':cid}).json();iid=created['import']['id'];assert created['status']=='preview_ready'
 assert client.delete(f'/api/projects/{pid}/search/{sid}').json()['deleted']
 assert client.get(f'/api/projects/{pid}/imports/{iid}').json()['import']['candidate_id'] is None
 assert client.post(f'/api/projects/{pid}/imports/{iid}/confirm',json={}).json()['status']=='imported'


def test_publish_during_search_discards_late_result(client,monkeypatch):
 pid=client.post('/api/projects',json={'name':'发布中检索'}).json()['project']['id'];started=threading.Event();release=threading.Event()
 class Provider:
  name='fixture'
  def search(self,*_):started.set();assert release.wait(10);return {'results':[{'id':'https://example.org/late','title':'late'}],'next_cursor':None}
 monkeypatch.setattr(search,'provider_from_env',lambda:Provider())
 with ThreadPoolExecutor(max_workers=2) as pool:
  pending=pool.submit(client.post,f'/api/projects/{pid}/search',json={'save':True,'query':'slow'});assert started.wait(5)
  conn=app.db()
  with conn:conn.execute("UPDATE projects SET lifecycle_state='published' WHERE id=?",(pid,))
  conn.close();release.set();assert pending.result(timeout=10).json()['status']=='superseded'
 history=client.get(f'/api/projects/{pid}/searches').json()['searches'];assert history[0]['status']=='superseded' and history[0]['candidate_count']==0
