"""Browser check for cache status, cleanup action and rebuild guidance."""
import argparse, hashlib, json, time
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1]
parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,required=True);url=f'http://127.0.0.1:{parser.parse_args().port}'
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True);ctx=browser.new_context(viewport={'width':1400,'height':950});req=ctx.request
 def ok(response):
  assert response.ok,response.text();return response.json()
 pid=ok(req.post(url+'/api/projects',data={'name':'缓存清理浏览器验证'}))['project']['id'];jobs=[]
 for i in range(11):
  build=ok(req.post(url+f'/api/projects/{pid}/compile',data={'request_id':f'cache-ui-{i}','purpose':'draft_preview','force_rebuild':True}));deadline=time.monotonic()+20
  while time.monotonic()<deadline:
   job=ok(req.get(url+build['status_url']))
   if job['status'] not in ['queued','running']:break
   time.sleep(.1)
  assert job['status']=='succeeded',job;jobs.append(job)
 assert ok(req.get(url+f"/api/projects/{pid}/builds/{jobs[0]['id']}"))['cache_pruned']==1
 pinjob=jobs[1]['id'];pin='r-'+'b'*32
 ok(req.post(url+f'/api/projects/{pid}/builds/{pinjob}/read-lease?lease_id={pin}&persistent=true'))
 page=ctx.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
 page.goto(url);page.wait_for_load_state('networkidle');page.locator(f'.project-item[data-id="{pid}"]').click();page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=pid)
 page.locator('[data-tab="preview"]').click();page.locator('#refreshBuildHistory').click()
 row=page.locator(f"[data-build-job='{jobs[0]['id']}']");row.get_by_role('button',name='缓存已清理，按快照重建').wait_for()
 page.locator('[data-cache-inventory]').get_by_text('最近 10 次',exact=False).wait_for()
 pinned=page.locator(f"[data-build-job='{pinjob}']").get_by_role('button',name='释放遗留阅读保护（1）');pinned.wait_for()
 page.once('dialog',lambda dialog:dialog.dismiss());pinned.click()
 assert ok(req.get(url+f'/api/projects/{pid}/builds/cache-inventory'))['persistent_pin_counts'][pinjob]==1
 page.once('dialog',lambda dialog:dialog.accept());pinned.click()
 page.wait_for_function('(id)=>!document.querySelector(`[data-build-job=\"${id}\"]`)?.textContent.includes(\"释放遗留阅读保护\")',arg=pinjob)
 assert pinjob not in ok(req.get(url+f'/api/projects/{pid}/builds/cache-inventory'))['persistent_pin_counts']
 with page.expect_response(lambda r:'/builds/cache-cleanup' in r.url and r.request.method=='POST') as response:
  page.get_by_role('button',name='检查并清理可释放的构建缓存').click()
 assert response.value.ok and response.value.json()['pruned']==[]
 row=page.locator(f"[data-build-job='{jobs[0]['id']}']")
 with page.expect_response(lambda r:r.url.endswith('/compile') and r.request.method=='POST') as response:
  row.get_by_role('button',name='缓存已清理，按快照重建').click()
 assert response.value.ok and response.value.json()['build_job']['id']!=jobs[0]['id']
 assert not errors,errors
 out=ROOT/'.project-to-act/docs/project-development/evidence/full-product-20261101/CACHE-PIN-UI.json';names=['app.py','paperpilot/services/build_cache.py','paperpilot/core/database.py','static/development-ui.js','tests/browser_req011_pin_ui.py','tests/ui_cache_server.py'];out.write_text(json.dumps({'passed':True,'isolated_database':True,'fake_compiler':True,'checks':['automatic pruning of oldest','history identifies pruned cache','manual cleanup action','rebuild from old snapshot','cancel release retains pin','confirmed release clears pin'],'browser_errors':errors,'source_sha256':{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names}},ensure_ascii=False,indent=2),encoding='utf-8')
 browser.close();print(json.dumps({'passed':True,'checks':6}))
