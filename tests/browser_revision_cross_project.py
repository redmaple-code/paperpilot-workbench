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
 a=make('甲论文');b=make('乙论文');page=ctx.new_page();page.goto(URL);page.wait_for_function('paperpilot.current?.project?.id');page.evaluate('(id)=>window.loadProject(id)',a);page.wait_for_function('(id)=>paperpilot.current?.project.id===id && paperpilot.isRevisionActive?.()',arg=a);page.locator('#editor').fill('甲论文慢失败');wait_for(lambda:ok(req.get(URL+'/test/revision-started'))['started']);page.evaluate('(id)=>window.loadProject(id)',b);page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=b);page.locator('#editor').fill('乙论文独立内容');page.wait_for_function("document.querySelector('#workflowModeState').textContent.includes('1 项可恢复草稿')");page.wait_for_timeout(2200);assert page.locator('#editor').input_value()=='乙论文独立内容';assert '甲论文故障' not in page.locator('#workflowAnnouncement').inner_text(),page.locator('#workflowAnnouncement').inner_text()
 drafts=ok(req.get(f'{URL}/api/projects/{b}/human-revision/drafts'));assert drafts[0]['content']=='乙论文独立内容';assert not ok(req.get(f'{URL}/api/projects/{a}/human-revision/drafts'))
 page.evaluate('(id)=>window.loadProject(id)',a);page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=a);page.wait_for_function("document.querySelector('#editor').value==='甲论文慢失败'");assert '未保存' in page.locator('#workflowAnnouncement').inner_text();page.locator('#saveFile').click();
 try: page.wait_for_function("document.querySelector('#workflowAnnouncement').textContent.includes('已保存检查点') && !document.querySelector('#editor').disabled",timeout=10000)
 except Exception as e: raise AssertionError('status='+page.locator('#workflowAnnouncement').inner_text()+' editor='+page.locator('#editor').input_value()+' selected='+page.locator('#fileSelect').input_value()) from e
 assert ok(req.get(f'{URL}/api/projects/{a}'))['files'][0]['content']=='甲论文慢失败';assert ok(req.get(f'{URL}/api/projects/{b}/human-revision/drafts'))[0]['content']=='乙论文独立内容';browser.close();print('cross-project failure isolation and retry passed')
