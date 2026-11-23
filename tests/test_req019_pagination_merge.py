"""Pagination preserves candidate identity, saved state and source evidence."""
import app
from paperpilot.services import academic_search as search


def test_cross_page_duplicate_merges_sources_and_upgrades_oa(client,monkeypatch):
 pid=client.post('/api/projects',json={'name':'跨页合并'}).json()['project']['id']
 class Provider:
  name='fixture'
  def search(self,query,filters,cursor,limit):
   if not cursor:return {'results':[{'__provider':'crossref','title':['Paper title'],'DOI':'10.1000/x','URL':'https://example.org/landing','published':{'date-parts':[[2024]]}}],'next_cursor':'page2'}
   return {'results':[{'__provider':'openalex','id':'https://example.org/oa','title':'Paper title','doi':'10.1000/x','publication_year':2024,'best_oa_location':{'is_oa':True,'pdf_url':'https://example.org/paper.pdf'},'open_access':{'is_oa':True}}],'next_cursor':None}
 monkeypatch.setattr(search,'provider_from_env',lambda:Provider())
 first=client.post(f'/api/projects/{pid}/search',json={'save':True,'query':'paper'}).json();sid=first['search']['id'];cid=first['candidates'][0]['id'];assert first['candidates'][0]['access_status']=='metadata_only'
 assert client.post(f'/api/projects/{pid}/search/candidates/{cid}/save').json()['saved']
 second=client.post(f'/api/projects/{pid}/search',json={'save':True,'query':'paper','search_id':sid,'cursor':'page2'}).json();assert second['status']=='succeeded' and len(second['candidates'])==1
 candidate=second['candidates'][0];assert candidate['id']==cid and candidate['saved'] and candidate['access_status']=='open_access' and candidate['oa_url']=='https://example.org/paper.pdf'
 assert {x['provider'] for x in candidate['sources']}=={'crossref','openalex'}
 assert client.get(f'/api/projects/{pid}/search/{sid}').json()['candidates'][0]['sources']==candidate['sources']


def test_page_failure_keeps_existing_candidates_and_same_cursor_retries(client,monkeypatch):
 pid=client.post('/api/projects',json={'name':'分页故障重试'}).json()['project']['id'];attempts=[]
 class Provider:
  name='fixture'
  def search(self,query,filters,cursor,limit):
   if not cursor:return {'results':[{'id':'https://example.org/a','title':'first'}],'next_cursor':'next'}
   attempts.append(cursor)
   if len(attempts)==1:raise search.AcademicSearchError('rate_limited','限流，请稍后重试',retryable=True)
   return {'results':[{'id':'https://example.org/b','title':'second'}],'next_cursor':None}
 monkeypatch.setattr(search,'provider_from_env',lambda:Provider())
 first=client.post(f'/api/projects/{pid}/search',json={'save':True,'query':'paper'}).json();sid=first['search']['id'];cid=first['candidates'][0]['id'];request={'query':'paper','search_id':sid,'cursor':'next'}
 failed=client.post(f'/api/projects/{pid}/search',json=request).json();assert failed['status']=='failed' and failed['error']['retryable']
 state=client.get(f'/api/projects/{pid}/search/{sid}').json();assert state['search']['next_cursor']=='next' and state['candidates'][0]['id']==cid
 retry=client.post(f'/api/projects/{pid}/search',json=request).json();assert retry['status']=='succeeded' and len(retry['candidates'])==2 and attempts==['next','next']


def test_cross_page_merge_is_independent_of_page_order(client,monkeypatch):
 metadata={'__provider':'crossref','title':['Paper title'],'DOI':'10.1000/x','URL':'https://example.org/landing','published':{'date-parts':[[2024]]}}
 opened={'__provider':'openalex','id':'https://example.org/oa','title':'Paper title','doi':'10.1000/x','publication_year':2024,'best_oa_location':{'is_oa':True,'pdf_url':'https://example.org/paper.pdf'},'open_access':{'is_oa':True}}
 class Provider:
  name='fixture'
  def search(self,query,filters,cursor,limit):
   pages=[metadata,opened] if query=='forward' else [opened,metadata]
   return {'results':[pages[1 if cursor else 0]],'next_cursor':None if cursor else 'next'}
 monkeypatch.setattr(search,'provider_from_env',lambda:Provider());pid=client.post('/api/projects',json={'name':'分页稳定'}).json()['project']['id'];results=[]
 for query in ('forward','reverse'):
  first=client.post(f'/api/projects/{pid}/search',json={'save':True,'query':query}).json();second=client.post(f'/api/projects/{pid}/search',json={'save':True,'query':query,'search_id':first['search']['id'],'cursor':'next'}).json();item=second['candidates'][0]
  results.append({k:item[k] for k in ('title','provider','access_status','oa_url','canonical_url','sources')})
 assert results[0]==results[1] and results[0]['access_status']=='open_access'
