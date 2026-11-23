import time
from playwright.sync_api import sync_playwright
URL='http://127.0.0.1:28497'
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True);ctx=browser.new_context();req=ctx.request
 def ok(r):assert r.ok,r.text();return r.json()
 def wait_for(fn):
  end=time.monotonic()+10
  while time.monotonic()<end:
   if fn():return
   time.sleep(.05)
  raise AssertionError('condition timed out')
 pid=ok(req.post(URL+'/api/projects',data={'name':'预览竞态甲'}))['project']['id'];other=ok(req.post(URL+'/api/projects',data={'name':'预览竞态乙'}))['project']['id'];w=ok(req.get(f'{URL}/api/projects/{pid}/workflow'));w=ok(req.post(f'{URL}/api/projects/{pid}/workflow/commands',data={'command':'start','request_id':'start','base_version':w['version']}));ok(req.post(f'{URL}/api/projects/{pid}/human-revision',data={'workflow_version':w['version']}));f=ok(req.get(f'{URL}/api/projects/{pid}'))['files'][0];base={'target_kind':'file','target_path':f['path'],'base_version':f['version'],'expected_draft_updated_at':None};ok(req.post(f'{URL}/api/projects/{pid}/human-revision/drafts',data={**base,'copy_id':'copy-fast','content':'较快预览'}));assert req.post(f'{URL}/api/projects/{pid}/human-revision/drafts',data={**base,'copy_id':'copy-slow','content':'较慢预览'}).status==409
 page=ctx.new_page();page.goto(URL);page.evaluate('(id)=>window.loadProject(id)',pid);page.wait_for_function('(id)=>paperpilot.current?.project.id===id && paperpilot.isRevisionActive?.()',arg=pid);page.locator('#revisionCopiesRefresh').click();page.wait_for_function("document.querySelector('#revisionCopies').options.length===3");page.locator('#revisionCopies').select_option('copy-slow');wait_for(lambda:ok(req.get(URL+'/test/copy-started'))['started']);page.locator('#revisionCopies').select_option('copy-fast');page.wait_for_function("document.querySelector('#revisionCopyPreview').value==='较快预览'");page.wait_for_timeout(2300);assert page.locator('#revisionCopyPreview').input_value()=='较快预览'
 page.locator('#revisionCopies').select_option('copy-slow');page.evaluate('(id)=>window.loadProject(id)',other);page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=other);page.wait_for_timeout(2300);assert page.locator('#revisionCopyPreview').input_value()=='';browser.close();print('late copy previews stay scoped to selection and project')
