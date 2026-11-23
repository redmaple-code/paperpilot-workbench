"""Ephemeral search state has no durable side effects and isolates sessions."""
import subprocess,sys,threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from paperpilot.services import academic_search
from paperpilot.services.search_sessions import SearchSessionError,SearchSessionStore

A='a'*32;B='b'*32;PROJECT='paper-one';RULE=academic_search.RULE_VERSION


def start(store,session=A,project=PROJECT,query='paper'):
 return store.begin(session,project,query,{},'fixture',RULE,'request')


def candidate(provider='openalex',opened=False):
 item=academic_search.normalize_candidate({'id':'https://example.org/'+provider,'title':'paper','doi':'10.1000/test','publication_year':2024,'primary_location':{'landing_page_url':'https://example.org/'+provider},'best_oa_location':{'is_oa':True,'pdf_url':'https://example.org/file.pdf'} if opened else {},'open_access':{'is_oa':opened}},'openalex')
 item['provider']=provider;return item


def test_sessions_projects_and_late_results_are_isolated():
 store=SearchSessionStore();old=start(store);other=start(store,B);new=start(store,query='new')
 with pytest.raises(SearchSessionError) as error:store.complete(A,PROJECT,old['search']['id'],old['attempt'],candidates=[candidate()],next_cursor=None)
 assert error.value.code=='search_superseded'
 assert store.get(B,PROJECT,other['search']['id'])['status']=='loading'
 with pytest.raises(SearchSessionError) as error:store.get(B,PROJECT,new['search']['id'])
 assert error.value.code=='search_missing'
 with pytest.raises(SearchSessionError):store.get(A,'other-paper',new['search']['id'])


def test_page_merge_preserves_identity_saved_status_and_copy_isolation():
 store=SearchSessionStore();first=start(store);sid=first['search']['id']
 result=store.complete(A,PROJECT,sid,first['attempt'],candidates=[candidate('crossref')],next_cursor='next')
 cid=result['candidates'][0]['id'];store.set_candidate_saved(A,PROJECT,sid,cid,True)
 page=store.begin_page(A,PROJECT,sid,query='paper',filters={},provider='fixture',rule_version=RULE,cursor='next')
 result=store.complete(A,PROJECT,sid,page['attempt'],candidates=[candidate('openalex',True)],next_cursor=None)
 assert len(result['candidates'])==1 and result['candidates'][0]['id']==cid and result['candidates'][0]['saved']
 assert result['candidates'][0]['access_status']=='open_access' and {x['provider'] for x in result['candidates'][0]['sources']}=={'crossref','openalex'}
 assert result['summary']['pages_fetched']==2 and result['summary']['raw_count']==2
 result['candidates'][0]['title']='mutated';assert store.get(A,PROJECT,sid)['candidates'][0]['title']=='paper'


def test_page_failure_retry_and_capacity_preserve_prior_results():
 store=SearchSessionStore(max_candidates=1);first=start(store);sid=first['search']['id']
 initial=store.complete(A,PROJECT,sid,first['attempt'],candidates=[candidate()],next_cursor='next');cid=initial['candidates'][0]['id']
 page=store.begin_page(A,PROJECT,sid,query='paper',filters={},provider='fixture',rule_version=RULE,cursor='next')
 store.fail(A,PROJECT,sid,page['attempt'],code='rate_limited',message='限流',retryable=True)
 assert store.get(A,PROJECT,sid)['next_cursor']=='next'
 page=store.begin_page(A,PROJECT,sid,query='paper',filters={},provider='fixture',rule_version=RULE,cursor='next')
 extra={**candidate(),'dedupe_key':'different'}
 with pytest.raises(SearchSessionError) as error:store.complete(A,PROJECT,sid,page['attempt'],candidates=[extra],next_cursor=None)
 assert error.value.code=='candidate_capacity' and store.get(A,PROJECT,sid)['candidates'][0]['id']==cid


def test_locked_snapshot_prevents_concurrent_page_or_close():
 store=SearchSessionStore();first=start(store);sid=first['search']['id'];store.complete(A,PROJECT,sid,first['attempt'],candidates=[candidate()],next_cursor='next')
 entered=threading.Event();release=threading.Event();closed=threading.Event()
 def promote():
  with store.locked_snapshot(A,PROJECT,sid) as snapshot:
   assert len(snapshot['candidates'])==1;entered.set();assert release.wait(5)
 def close():store.close(A);closed.set()
 with ThreadPoolExecutor(max_workers=2) as pool:
  a=pool.submit(promote);assert entered.wait(5);b=pool.submit(close);assert not closed.wait(.2);release.set();a.result(timeout=5);b.result(timeout=5)
 with pytest.raises(SearchSessionError) as error:store.get(A,PROJECT,sid)
 assert error.value.code=='session_expired'


def test_session_expiry_and_bounded_capacity_do_not_evict_active_session():
 now=[0.0];store=SearchSessionStore(max_sessions=1,idle_seconds=10,clock=lambda:now[0]);first=start(store)
 with pytest.raises(SearchSessionError) as error:start(store,B)
 assert error.value.code=='session_capacity' and store.get(A,PROJECT,first['search']['id'])['status']=='loading'
 now[0]=11;start(store,B)
 with pytest.raises(SearchSessionError) as error:store.get(A,PROJECT,first['search']['id'])
 assert error.value.code=='session_expired'


def test_process_restart_has_no_session_state_or_disk_artifacts(tmp_path):
 script="from paperpilot.services.search_sessions import SearchSessionStore; s=SearchSessionStore(); s.begin('a'*32,'p','paper',{},'fixture','v','r'); print('ready',flush=True); input()"
 worker=subprocess.Popen([sys.executable,'-X','utf8','-c',script],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
 try:
  assert worker.stdout.readline().strip()=='ready';worker.terminate();worker.wait(timeout=10)
  fresh=SearchSessionStore()
  with pytest.raises(SearchSessionError) as error:fresh.get(A,'p','unknown')
  assert error.value.code=='session_expired' and list(tmp_path.iterdir())==[]
 finally:
  if worker.poll() is None:worker.terminate();worker.wait(timeout=10)
