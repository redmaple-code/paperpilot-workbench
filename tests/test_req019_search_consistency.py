"""REQ-019 search generation, project isolation and deterministic OA merge."""
from concurrent.futures import ThreadPoolExecutor
import threading
import app
from paperpilot.services import academic_search as search


def test_slow_old_search_cannot_revive_or_overwrite_new_search(client,monkeypatch):
 pid=client.post('/api/projects',json={'name':'代际隔离'}).json()['project']['id'];started=threading.Event();release=threading.Event()
 class Provider:
  name='fake'
  def search(self,query,filters,cursor,limit):
   if query=='old':started.set();assert release.wait(10)
   return {'results':[{'id':'https://example.org/'+query,'title':query,'publication_year':2024}],'next_cursor':None}
 monkeypatch.setattr(app.academic_search,'provider_from_env',lambda:Provider())
 with ThreadPoolExecutor(max_workers=2) as pool:
  old=pool.submit(client.post,f'/api/projects/{pid}/search',json={'save':True,'query':'old'});assert started.wait(5)
  new=client.post(f'/api/projects/{pid}/search',json={'save':True,'query':'new'}).json();assert new['status']=='succeeded'
  release.set();late=old.result(timeout=10).json();assert late['status']=='superseded' and not late['candidates']
 history=client.get(f'/api/projects/{pid}/searches').json()['searches'];assert next(x for x in history if x['query']=='old')['status']=='superseded'


def test_duplicate_source_order_does_not_change_result_or_hide_oa():
 base={'title':'Same paper','authors':'Author','year':'2024','doi':'10.1000/a','dedupe_key':'same','abstract':'','external_id':'x','venue':'Journal'}
 closed={**base,'provider':'crossref','canonical_url':'https://example.org/metadata','access_status':'metadata_only','oa_url':''}
 opened={**base,'provider':'openalex','canonical_url':'https://example.org/full','access_status':'open_access','oa_url':'https://example.org/file.pdf'}
 a=search.rank_and_deduplicate([closed,opened],'paper');b=search.rank_and_deduplicate([opened,closed],'paper')
 assert a==b and a[0]['access_status']=='open_access' and a[0]['oa_url'].endswith('.pdf') and len(a[0]['sources'])==2


def test_late_failure_does_not_revive_old_search(client,monkeypatch):
 pid=client.post('/api/projects',json={'name':'迟到错误'}).json()['project']['id'];started=threading.Event();release=threading.Event()
 class Provider:
  name='fake'
  def search(self,query,filters,cursor,limit):
   if query=='old':started.set();assert release.wait(10);raise search.AcademicSearchError('provider_unavailable','暂时不可用',retryable=True)
   return {'results':[{'id':'https://example.org/new','title':'new'}],'next_cursor':None}
 monkeypatch.setattr(search,'provider_from_env',lambda:Provider())
 with ThreadPoolExecutor(max_workers=2) as pool:
  old=pool.submit(client.post,f'/api/projects/{pid}/search',json={'save':True,'query':'old'});assert started.wait(5)
  assert client.post(f'/api/projects/{pid}/search',json={'save':True,'query':'new'}).json()['status']=='succeeded'
  release.set();assert old.result(timeout=10).json()['status']=='superseded'
 history=client.get(f'/api/projects/{pid}/searches').json()['searches'];assert next(x for x in history if x['query']=='old')['status']=='superseded'


def test_pagination_single_flight_and_new_query_supersedes_it(client,monkeypatch):
 pid=client.post('/api/projects',json={'name':'分页并发'}).json()['project']['id'];started=threading.Event();release=threading.Event()
 class Provider:
  name='fake'
  def search(self,query,filters,cursor,limit):
   if cursor:started.set();assert release.wait(10)
   return {'results':[{'id':'https://example.org/'+query+str(cursor),'title':query}],'next_cursor':'next' if not cursor and query=='first' else None}
 monkeypatch.setattr(search,'provider_from_env',lambda:Provider())
 first=client.post(f'/api/projects/{pid}/search',json={'save':True,'query':'first'}).json();sid=first['search']['id']
 payload={'query':'first','search_id':sid,'cursor':'next'}
 with ThreadPoolExecutor(max_workers=2) as pool:
  old=pool.submit(client.post,f'/api/projects/{pid}/search',json=payload);assert started.wait(5)
  duplicate=client.post(f'/api/projects/{pid}/search',json=payload);assert duplicate.status_code==409 and duplicate.json()['detail']['code']=='search_in_progress'
  assert client.post(f'/api/projects/{pid}/search',json={'save':True,'query':'new'}).json()['status']=='succeeded'
  release.set();assert old.result(timeout=10).json()['status']=='superseded'
 snapshot=client.get(f'/api/projects/{pid}/search/{sid}').json();assert snapshot['search']['status']=='superseded' and len(snapshot['candidates'])==1


def test_restored_snapshot_keeps_original_rule_version(client,monkeypatch):
 pid=client.post('/api/projects',json={'name':'历史规则版本'}).json()['project']['id']
 class Provider:
  name='fake'
  def search(self,*args):return {'results':[],'next_cursor':None}
 monkeypatch.setattr(search,'provider_from_env',lambda:Provider())
 sid=client.post(f'/api/projects/{pid}/search',json={'save':True,'query':'legacy'}).json()['search']['id']
 conn=app.db()
 with conn:conn.execute("UPDATE academic_searches SET rule_version='hybrid-v2' WHERE id=?",(sid,))
 conn.close();restored=client.get(f'/api/projects/{pid}/search/{sid}').json()
 assert restored['search']['rule_version']=='hybrid-v2' and restored['summary']['rule_version']=='hybrid-v2'
 denied=client.post(f'/api/projects/{pid}/search',json={'save':True,'query':'legacy','search_id':sid,'cursor':'next'})
 assert denied.status_code==409 and denied.json()['detail']['code']=='search_rule_changed'
