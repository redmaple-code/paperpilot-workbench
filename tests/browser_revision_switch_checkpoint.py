import time
from playwright.sync_api import sync_playwright
URL='http://127.0.0.1:28496'
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True);ctx=browser.new_context();req=ctx.request
 def ok(r):assert r.ok,r.text();return r.json()
 def wait_for(fn):
  end=time.monotonic()+12
  while time.monotonic()<end:
   if fn():return
   time.sleep(.05)
  raise AssertionError('condition timed out')
 def make(name):
  pid=ok(req.post(URL+'/api/projects',data={'name':name}))['project']['id'];w=ok(req.get(f'{URL}/api/projects/{pid}/workflow'));w=ok(req.post(f'{URL}/api/projects/{pid}/workflow/commands',data={'command':'start','request_id':'start-'+pid,'base_version':w['version']}));ok(req.post(f'{URL}/api/projects/{pid}/human-revision',data={'workflow_version':w['version']}));return pid
 a=make('甲');b=make('乙');ok(req.post(f'{URL}/test/arm-checkpoint/{a}'));page=ctx.new_page();page.goto(URL);page.wait_for_function('paperpilot.current?.project?.id');page.evaluate('(id)=>window.loadProject(id)',a);page.wait_for_function('(id)=>paperpilot.current?.project.id===id && paperpilot.isRevisionActive?.()',arg=a);target=page.locator('#fileSelect').input_value();page.locator('#editor').fill('甲的检查点');page.locator('#saveFile').click();wait_for(lambda:ok(req.get(URL+'/test/checkpoint-started'))['started']);page.evaluate('(id)=>window.loadProject(id)',b);page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=b);wait_for(lambda:next(f for f in ok(req.get(f'{URL}/api/projects/{a}'))['files'] if f['path']==target)['content']=='甲的检查点');page.wait_for_function("!document.querySelector('#editor').disabled",timeout=10000);assert '甲的检查点' not in page.locator('#editor').input_value(), ('editor',page.locator('#editor').input_value());assert '已保存检查点' not in page.locator('#workflowAnnouncement').inner_text(), ('announcement',page.locator('#workflowAnnouncement').inner_text());assert next(f for f in ok(req.get(f'{URL}/api/projects/{a}'))['files'] if f['path']==target)['content']=='甲的检查点', ('later',ok(req.get(f'{URL}/api/projects/{a}'))['files'],target);page.locator('#editor').fill('乙页面的新输入');wait_for(lambda:any(x['content']=='乙页面的新输入' for x in ok(req.get(f'{URL}/api/projects/{b}/human-revision/drafts'))));assert ok(req.get(f'{URL}/api/projects/{b}/human-revision/drafts'))[0]['content']=='乙页面的新输入';browser.close();print('late checkpoint does not reload or announce in another project')
