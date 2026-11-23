import json,time
from pathlib import Path
from playwright.sync_api import sync_playwright
URL='http://127.0.0.1:28494';out=Path('.project-to-act/local/project-development/full-product-20261101/outline-keyboard-responsive-shots');out.mkdir(exist_ok=True)
with sync_playwright() as p:
 browser=p.chromium.launch(headless=True);ctx=browser.new_context(viewport={'width':320,'height':800});req=ctx.request
 def ok(r):assert r.ok,r.text();return r.json()
 pid=ok(req.post(URL+'/api/projects',data={'name':'键盘与窄屏验证'}))['project']['id'];preview=ok(req.post(f'{URL}/api/projects/{pid}/outline/migration/preview',data={'request_id':'preview-'+pid}));graph=ok(req.post(f'{URL}/api/projects/{pid}/outline/migration/commit',data={'draft_id':preview['id'],'request_id':'migrate-'+pid}));r=ok(req.post(f'{URL}/api/projects/{pid}/outline/nodes',data={'command':'create','request_id':'long','base_version':graph['version'],'node':{'id':'long-node','title':'长节点'+('VeryLongUnbrokenTitle'*15),'status':'legacy-custom'}}));w=ok(req.get(f'{URL}/api/projects/{pid}/workflow'));w=ok(req.post(f'{URL}/api/projects/{pid}/workflow/commands',data={'command':'start','request_id':'start','base_version':w['version']}));ok(req.post(f'{URL}/api/projects/{pid}/human-revision',data={'workflow_version':w['version']}));f=ok(req.get(f'{URL}/api/projects/{pid}'))['files'][0];ok(req.post(f'{URL}/api/projects/{pid}/human-revision/drafts',data={'target_kind':'file','target_path':f['path'],'base_version':f['version'],'content':'恢复副本键盘验证','copy_id':'keyboard-copy','expected_draft_updated_at':None}))
 page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(URL,wait_until='networkidle');page.wait_for_function('paperpilot.current?.project?.id');page.evaluate('(id)=>window.loadProject(id)',pid);page.wait_for_function('(id)=>paperpilot.current?.project.id===id && paperpilot.isRevisionActive?.()',arg=pid);page.locator('[data-tab="outline"]').click();page.locator('#outlineNodeCreate').wait_for();node=page.locator('#outlineTree .outline-node-select').first;node.focus();page.keyboard.press('Enter');page.wait_for_function("document.querySelector('#outlineTree .outline-node-select')?.getAttribute('aria-pressed')==='true'");page.wait_for_timeout(400);assert page.locator('#outlineDetailStatus').input_value()=='draft';assert page.evaluate("document.activeElement.matches('#outlineTree .outline-node-select')"),'focus lost'
 def tab_to(selector):
  for _ in range(160):
   if page.evaluate('(s)=>document.activeElement.matches(s)',selector):return
   page.keyboard.press('Tab')
  raise AssertionError('cannot keyboard reach '+selector)
 tab_to('#outlineDetailTitle');page.keyboard.press('Control+A');page.keyboard.insert_text('键盘修改标题');tab_to('#outlineNodeSave');page.keyboard.press('Enter');page.wait_for_function("document.querySelector('#outlineOperationStatus').textContent.includes('节点已保存')");assert ok(req.get(f'{URL}/api/projects/{pid}/outline/structured'))['nodes'][0]['status']=='draft';long=page.locator('#outlineTree .outline-node-select').filter(has_text='长节点');long.focus();page.keyboard.press('Enter');page.wait_for_function("document.querySelector('#outlineDetailStatus').value==='legacy-custom'");assert '历史状态' in page.locator('#outlineDetailStatus option:checked').inner_text();tab_to('#outlineDetailPurpose');page.keyboard.insert_text('保留历史状态');tab_to('#outlineNodeSave');page.keyboard.press('Enter');
 end=time.monotonic()+10
 while time.monotonic()<end:
  saved=next(n for n in ok(req.get(f'{URL}/api/projects/{pid}/outline/structured'))['nodes'] if n['id']=='long-node')
  if saved['purpose']=='保留历史状态':break
  time.sleep(.05)
 assert saved['purpose']=='保留历史状态' and saved['status']=='legacy-custom'
 for width in (320,390,768,900,901,1024,1100,1280):
  page.set_viewport_size({'width':width,'height':800});page.wait_for_timeout(100)
  for sel in ('#outlineHistorySelect','#outlineHistoryView','#outlineHistoryRestore','#outlineNodeTitle','#outlineNodeCreate','#outlineTree .outline-node-select'):
   node=page.locator(sel).first;box=node.bounding_box();assert box and box['x']>=-1 and box['x']+box['width']<=width+1,(width,sel,box)
  assert page.evaluate('document.documentElement.scrollWidth<=document.documentElement.clientWidth+1'),(width,page.evaluate("({scroll:document.documentElement.scrollWidth,items:[...document.querySelectorAll('body *')].filter(e=>{let r=e.getBoundingClientRect();return r.width>0&&r.right>innerWidth+1}).slice(0,15).map(e=>({id:e.id,cls:e.className,right:Math.round(e.getBoundingClientRect().right),width:Math.round(e.getBoundingClientRect().width)}))})"))
 page.set_viewport_size({'width':320,'height':800});page.locator('#outlineOperations').screenshot(path=str(out/'outline-320.png'))
 page.locator('[data-tab="write"]').click()
 for width in (320,390,768,900,901,1024,1100,1280):
  page.set_viewport_size({'width':width,'height':800});page.wait_for_timeout(100);assert page.evaluate('document.documentElement.scrollWidth<=document.documentElement.clientWidth+1'),f'write overflow at {width}'
  for sel in ('#revisionCopiesRefresh','#revisionCopies','#revisionCopyRestore','#revisionCopyDelete','#saveFile','#convert','#sourceToPdf'):
   box=page.locator(sel).bounding_box();assert box and box['x']>=-1 and box['x']+box['width']<=width+1,(width,sel,box)
 page.set_viewport_size({'width':320,'height':800});page.locator('#revisionCopiesRefresh').focus();page.keyboard.press('Enter');page.wait_for_function("document.querySelector('#revisionCopies').options.length>1");tab_to('#revisionCopies');page.keyboard.press('ArrowDown');page.keyboard.press('Enter');page.wait_for_function("document.querySelector('#revisionCopyPreview').value==='恢复副本键盘验证'");assert not errors,errors;page.locator('#workflowDashboard').screenshot(path=str(out/'workflow-320.png'));browser.close();print('keyboard selection, editing, recovery and responsive layout passed')
