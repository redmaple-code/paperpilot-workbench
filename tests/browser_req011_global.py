"""Real browser check of cross-paper capacity guidance and automatic wake."""
import argparse,hashlib,json,time
from pathlib import Path
from playwright.sync_api import sync_playwright
ROOT=Path(__file__).resolve().parents[1];parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,required=True);url=f'http://127.0.0.1:{parser.parse_args().port}'
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True);ctx=browser.new_context(viewport={'width':1400,'height':950});req=ctx.request
 def ok(response):
  assert response.ok,response.text();return response.json()
 projects=[ok(req.post(url+'/api/projects',data={'name':f'调度浏览器{i}'}))['project']['id'] for i in range(3)]
 jobs=[]
 for i,pid in enumerate(projects):
  result=ok(req.post(url+f'/api/projects/{pid}/compile',data={'request_id':f'ui-global-{i}','purpose':'draft_preview'}));jobs.append(result['build_job']['id'])
 deadline=time.monotonic()+5
 while time.monotonic()<deadline:
  states=[ok(req.get(url+f'/api/projects/{pid}/builds/{job}')) for pid,job in zip(projects,jobs)]
  if [s['status'] for s in states]==['running','running','queued']:break
  time.sleep(.05)
 assert [s['status'] for s in states]==['running','running','queued'],states
 assert states[2]['global_capacity_wait']
 page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
 page.goto(url);page.wait_for_load_state('networkidle');page.locator(f'.project-item[data-id="{projects[2]}"]').click();page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=projects[2]);page.locator('[data-tab="preview"]').click();page.locator('#refreshBuildHistory').click()
 page.locator(f'[data-build-job="{jobs[2]}"]').get_by_text('本机构建名额已满，完成后自动继续',exact=False).wait_for(timeout=5000)
 deadline=time.monotonic()+20
 while time.monotonic()<deadline:
  state=ok(req.get(url+f'/api/projects/{projects[2]}/builds/{jobs[2]}'))
  if state['status'] in ('running','succeeded'):break
  time.sleep(.1)
 assert state['status'] in ('running','succeeded'),state
 # Inject the API's unknown-owner response to verify actionable UI guidance.
 def unknown_owner(route):
  response=route.fetch();rows=response.json()
  for item in rows:
   if item['id']==jobs[2]:
    item.update(status='queued',global_capacity_wait=True,global_capacity_message='有旧构建的进程身份无法核实；请检查对应论文的构建历史和旧服务实例，系统不会强制释放资源。',global_capacity_blockers=[{'project_id':projects[0],'project_name':'调度浏览器0','reason':'owner_unknown'}])
  route.fulfill(response=response,json=rows)
 page.route(f'**/api/projects/{projects[2]}/builds?limit=30',unknown_owner);page.locator('#refreshBuildHistory').click()
 page.locator(f'[data-build-job="{jobs[2]}"]').get_by_text('身份无法核实',exact=False).wait_for()
 assert '调度浏览器0' in page.locator(f'[data-build-job="{jobs[2]}"]').inner_text()
 assert not errors,errors
 names=['app.py','paperpilot/services/build_queue.py','paperpilot/services/build_snapshots.py','paperpilot/services/tex_runtime.py','static/development-ui.js','tests/ui_global_server.py','tests/browser_req011_global.py'];report={'passed':True,'isolated_database':True,'fake_compiler':True,'checks':['two concurrent projects','third queued and visible guidance','third starts automatically after capacity release','injected unknown owner shows affected paper and guidance'],'browser_errors':errors,'source_sha256':{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names}}
 (ROOT/'.project-to-act/docs/project-development/evidence/full-product-20261101/GLOBAL-SCHEDULER-UI.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');browser.close();print(json.dumps({'passed':True,'checks':4}))
