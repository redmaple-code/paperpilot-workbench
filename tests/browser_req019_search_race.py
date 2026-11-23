"""A delayed search cannot replace newer results or cross paper boundaries."""
import hashlib,json,time
from datetime import datetime,timezone
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1];url='http://127.0.0.1:16834'
with sync_playwright() as pw:
 browser=pw.chromium.launch(headless=True);ctx=browser.new_context(viewport={'width':1280,'height':900});req=ctx.request
 def ok(r):
  assert r.ok,r.text();return r.json()
 items=ok(req.get(url+'/api/projects'));a=next(x['id'] for x in items if x['name']=='检索论文甲');b=next(x['id'] for x in items if x['name']=='检索论文乙')
 page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(url);page.wait_for_load_state('networkidle');page.locator(f'.project-item[data-id="{a}"]').click();page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=a);page.locator('[data-tab="knowledge"]').click()
 def submit(q):page.locator('#academicQuery').fill(q);page.locator('#academicSearch').click()
 def wait_started(q):
  deadline=time.monotonic()+8
  while time.monotonic()<deadline:
   if ok(req.get(url+'/test/search-status/'+q))['started']:return
   time.sleep(.05)
  raise AssertionError('provider did not start')
 submit('old');wait_started('old');submit('new');page.get_by_text('结果-new',exact=False).first.wait_for();ok(req.post(url+'/test/search-release/old'));time.sleep(.6);assert '结果-new' in page.locator('#academicCandidates').inner_text() and '结果-old' not in page.locator('#academicCandidates').inner_text()
 submit('late');wait_started('late');page.locator(f'.project-item[data-id="{b}"]').click();page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=b);assert page.locator('#academicCandidates').inner_text().strip()=='' and page.locator('#academicQuery').input_value()==''
 ok(req.post(url+'/test/search-release/late'));time.sleep(.6);assert page.locator('#academicCandidates').inner_text().strip()=='' and not errors,errors
 files=['app.py','paperpilot/services/academic_search.py','static/app.js','tests/test_req019_search_consistency.py','tests/browser_req019_search_race.py'];report={'passed':True,'isolated_database':True,'fixture_provider':True,'time':datetime.now(timezone.utc).isoformat(),'checks':['late old search cannot replace new results','switching paper clears candidates and query','late result cannot appear in another paper','no browser errors'],'source_sha256':{f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in files}}
 (root/'.project-to-act/docs/project-development/evidence/full-product-20261101/REQ-019-SEARCH-RACE-BROWSER.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');browser.close();print(json.dumps({'passed':True,'checks':len(report['checks'])}))
