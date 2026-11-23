"""OpenAlex cursor auth errors give a safe, actionable recovery path."""
import app
from paperpilot.services import academic_search as search


def test_openalex_cursor_key_requirement_explained_without_leaking_secret(monkeypatch):
 class Response:
  status_code=401
  def json(self):return {'error':'API key required for cursor paging','message':'Paging past the first page with cursor= needs a free API key.'}
 monkeypatch.setattr(search.requests,'get',lambda *a,**k:Response())
 provider=search.OpenAlexProvider(api_key='')
 try:provider.search('paper',{},'next',5)
 except search.AcademicSearchError as exc:
  assert exc.code=='provider_auth_failed' and 'PWA_ACADEMIC_SEARCH_API_KEY' in str(exc) and '重启' in str(exc)
 else:raise AssertionError('expected auth failure')


def test_cursor_auth_error_preserves_prior_results_and_retry(client,monkeypatch):
 pid=client.post('/api/projects',json={'name':'分页鉴权提示'}).json()['project']['id'];calls=[]
 class Provider:
  name='openalex'
  def search(self,query,filters,cursor,limit):
   if cursor:
    calls.append(cursor)
    if len(calls)==1:raise search.AcademicSearchError('provider_auth_failed','请配置 PWA_ACADEMIC_SEARCH_API_KEY 并重启服务。')
    return {'results':[{'id':'https://example.org/two','title':'two'}],'next_cursor':None}
   return {'results':[{'id':'https://example.org/one','title':'one'}],'next_cursor':'next'}
 monkeypatch.setattr(search,'provider_from_env',lambda:Provider())
 first=client.post(f'/api/projects/{pid}/search',json={'save':True,'query':'paper'}).json();sid=first['search']['id'];payload={'query':'paper','search_id':sid,'cursor':'next'}
 failed=client.post(f'/api/projects/{pid}/search',json=payload).json();assert failed['status']=='failed' and 'PWA_ACADEMIC_SEARCH_API_KEY' in failed['error']['message']
 old=client.get(f'/api/projects/{pid}/search/{sid}').json();assert len(old['candidates'])==1 and old['search']['next_cursor']=='next'
 retry=client.post(f'/api/projects/{pid}/search',json=payload).json();assert retry['status']=='succeeded' and len(retry['candidates'])==2


def test_other_auth_errors_do_not_expose_configured_key(monkeypatch):
 class Response:
  status_code=403
  def json(self):return {'error':'unexpected credential rejection'}
 monkeypatch.setattr(search.requests,'get',lambda *a,**k:Response())
 try:search.OpenAlexProvider(api_key='synthetic-test-secret').search('paper',{},'next',5)
 except search.AcademicSearchError as exc:
  assert exc.code=='provider_auth_failed' and 'synthetic-test-secret' not in str(exc) and '免费 API Key' not in str(exc)
 else:raise AssertionError('expected auth failure')
