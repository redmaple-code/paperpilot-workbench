"""REQ-019 source defaults and durable search provenance."""
import app
from paperpilot.services import academic_search as search


def test_default_single_provider_and_explicit_multiple(monkeypatch):
 monkeypatch.delenv('PWA_ACADEMIC_SOURCES',raising=False)
 assert isinstance(search.provider_from_env(),search.OpenAlexProvider)
 monkeypatch.setenv('PWA_ACADEMIC_SOURCES','openalex,crossref')
 provider=search.provider_from_env();assert isinstance(provider,search.MultiSourceProvider) and [p.name for p in provider.providers]==['openalex','crossref']


def test_saved_summary_and_handoff_survive_snapshot_deletion(client,monkeypatch):
 pid=client.post('/api/projects',json={'name':'检索追溯'}).json()['project']['id'];calls=[]
 class Provider:
  name='openalex+crossref'
  def search(self,query,filters,cursor,limit):
   calls.append(query)
   return {'results':[{'__provider':'openalex','id':'https://example.org/paper','title':'Graphene battery','doi':'10.1000/test','publication_year':2024}], 'next_cursor':None,'expanded_queries':['graphene','graphene variant'],'diagnostics':[{'provider':'openalex','query':query,'pages':2,'raw_count':12,'exhausted':False},{'provider':'crossref','query':query,'pages':0,'raw_count':0,'error':'rate_limited'}]}
 monkeypatch.setattr(search,'provider_from_env',lambda:Provider())
 original=client.post(f'/api/projects/{pid}/search',json={'save':True,'query':'graphene','filters':{'year_from':2020}}).json();assert original['ok'];sid=original['search']['id'];cid=original['candidates'][0]['id'];summary=original['summary']
 assert summary['source_failures']==[{'provider':'crossref','error':'rate_limited'}] and summary['truncated'] and summary['pages_fetched']==2
 assert client.post(f'/api/projects/{pid}/search/{sid}/save').json()['saved']
 restored=client.get(f'/api/projects/{pid}/search/{sid}').json();assert restored['summary']==summary and calls==['graphene']
 handoff=client.post(f'/api/projects/{pid}/search/handoff',json={'candidate_id':cid}).json();draft=handoff['reference_draft'];trace=draft['source_trace']
 assert trace['query_snapshot']['query']=='graphene' and trace['query_snapshot']['filters']=={'year_from':2020} and trace['query_snapshot']['summary']==summary and trace['query_snapshot']['retrieved_at']
 conn=app.db()
 with conn:conn.execute('DELETE FROM academic_searches WHERE id=? AND project_id=?',(sid,pid))
 conn.close();restored_draft=client.get('/api/references/drafts/'+draft['id']).json();assert restored_draft['source_trace']==trace
 confirmed=client.post('/api/references/drafts/'+draft['id']+'/confirm',json={'request_id':'provenance-confirm','decision':'create'}).json();assert confirmed['reference']['source_trace']==trace


def test_legacy_snapshot_does_not_invent_diagnostics(client,monkeypatch):
 pid=client.post('/api/projects',json={'name':'旧检索'}).json()['project']['id']
 class Provider:
  name='fake'
  def search(self,*_):return {'results':[],'next_cursor':None}
 monkeypatch.setattr(search,'provider_from_env',lambda:Provider());sid=client.post(f'/api/projects/{pid}/search',json={'save':True,'query':'old'}).json()['search']['id']
 conn=app.db()
 with conn:conn.execute("UPDATE academic_searches SET summary_json='' WHERE id=?",(sid,))
 conn.close();summary=client.get(f'/api/projects/{pid}/search/{sid}').json()['summary'];assert summary['diagnostics_unavailable'] is True


def test_legacy_schema_migrates_without_fabricating_summary(client,monkeypatch):
 pid=client.post('/api/projects',json={'name':'旧表迁移'}).json()['project']['id']
 class Provider:
  name='fake'
  def search(self,*_):return {'results':[],'next_cursor':None}
 monkeypatch.setattr(search,'provider_from_env',lambda:Provider());sid=client.post(f'/api/projects/{pid}/search',json={'save':True,'query':'legacy schema'}).json()['search']['id']
 conn=app.db()
 with conn:conn.execute('ALTER TABLE academic_searches DROP COLUMN summary_json')
 conn.close();app.init_db();restored=client.get(f'/api/projects/{pid}/search/{sid}').json()
 assert restored['summary']['diagnostics_unavailable'] is True and restored['summary']['raw_count'] is None


def test_summary_accumulates_single_source_pages(client,monkeypatch):
 pid=client.post('/api/projects',json={'name':'分页快照'}).json()['project']['id']
 class Provider:
  name='fake'
  def search(self,query,filters,cursor,limit):
   return {'results':[{'id':'https://example.org/'+str(cursor),'title':'paper '+str(cursor)}],'next_cursor':'next' if cursor is None else None}
 monkeypatch.setattr(search,'provider_from_env',lambda:Provider())
 first=client.post(f'/api/projects/{pid}/search',json={'save':True,'query':'paper'}).json();sid=first['search']['id'];assert first['summary']['pages_fetched']==1 and first['summary']['truncated']
 second=client.post(f'/api/projects/{pid}/search',json={'save':True,'query':'paper','search_id':sid,'cursor':'next'}).json();assert second['summary']['pages_fetched']==2 and second['summary']['raw_count']==2 and not second['summary']['truncated']
 assert client.get(f'/api/projects/{pid}/search/{sid}').json()['summary']==second['summary']


def test_empty_next_page_preserves_nonempty_result_status(client,monkeypatch):
 pid=client.post('/api/projects',json={'name':'空后续页'}).json()['project']['id']
 class Provider:
  name='fake'
  def search(self,query,filters,cursor,limit):
   return {'results':[] if cursor else [{'id':'https://example.org/a','title':'paper'}],'next_cursor':None if cursor else 'next'}
 monkeypatch.setattr(search,'provider_from_env',lambda:Provider())
 first=client.post(f'/api/projects/{pid}/search',json={'save':True,'query':'paper'}).json();sid=first['search']['id']
 second=client.post(f'/api/projects/{pid}/search',json={'save':True,'query':'paper','search_id':sid,'cursor':'next'}).json()
 assert second['status']=='succeeded' and len(second['candidates'])==1 and second['summary']['counts']['total']==1


def test_changing_provider_requires_new_search_for_pagination(client,monkeypatch):
 pid=client.post('/api/projects',json={'name':'来源变化'}).json()['project']['id']
 class Provider:
  def __init__(self,name):self.name=name
  def search(self,*_):return {'results':[],'next_cursor':'next'}
 monkeypatch.setattr(search,'provider_from_env',lambda:Provider('first'))
 sid=client.post(f'/api/projects/{pid}/search',json={'save':True,'query':'paper'}).json()['search']['id']
 monkeypatch.setattr(search,'provider_from_env',lambda:Provider('second'))
 result=client.post(f'/api/projects/{pid}/search',json={'save':True,'query':'paper','search_id':sid,'cursor':'next'})
 assert result.status_code==409 and result.json()['detail']['code']=='search_provider_changed'
