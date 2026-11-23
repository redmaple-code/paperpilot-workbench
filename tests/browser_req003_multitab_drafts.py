"""Browser regression for independent outline drafts and explicit recovery."""
import hashlib,json,time
from datetime import datetime,timezone
from pathlib import Path
from playwright.sync_api import sync_playwright
URL='http://127.0.0.1:28495';pid='abcdef123456'
with sync_playwright() as pw:
 browser=pw.chromium.launch(headless=True);ca=browser.new_context();cb=browser.new_context();req=ca.request
 def ok(r):
  assert r.ok,r.text();return r.json()
 def put(key,content):return ok(req.put(f'{URL}/api/projects/{pid}/outline/drafts/{key}',data={'draft_key':key,'base_version':0,'content':content}))
 def listing(kind,node_id=''):
  return ok(req.get(f'{URL}/api/projects/{pid}/outline/draft-recovery',params={'kind':kind,'node_id':node_id,'limit':100}))['drafts']
 def wait_previews(kind,expected,node_id=''):
  deadline=time.monotonic()+10
  while time.monotonic()<deadline:
   found={x['preview'] for x in listing(kind,node_id)}
   if set(expected)<=found:return found
   time.sleep(.1)
  raise AssertionError(f'missing {expected}: {found}')
 node=ok(req.post(f'{URL}/api/projects/{pid}/outline/nodes',data={'command':'create','request_id':'multi-node','base_version':0,'node':{'id':'node-1','title':'原节点'}}))
 for index in range(31):put(f'tab:older-{index}:editor',{'text':f'分页旧草稿{index:02d}'})
 put('editor',{'text':'旧版全文草稿'});put('node:node-1',{'node_id':'node-1','node':{'title':'旧版节点草稿'}})
 a=ca.new_page();b=cb.new_page();errors=[]
 for page in (a,b):
  page.on('pageerror',lambda e:errors.append(str(e)));page.goto(URL);page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=pid);page.locator('[data-tab="outline"]').click();page.locator('#outlineNodeCreate').wait_for()
 a.locator('#outlineText').fill('甲标签全文草稿');b.locator('#outlineText').fill('乙标签全文草稿');a.locator('#outlineSaveDraft').click();b.locator('#outlineSaveDraft').click()
 wait_previews('editor',['旧版全文草稿','甲标签全文草稿','乙标签全文草稿']);assert len({d['draft_key'] for d in listing('editor')})==34
 a.locator('#saveOutline').click();a.wait_for_function("document.querySelector('#outlineStructuredStatus').textContent==='大纲已保存'")
 current=ok(req.get(f'{URL}/api/projects/{pid}'));assert current['outline']=='甲标签全文草稿'
 remaining={d['preview'] for d in listing('editor')};assert {'旧版全文草稿','乙标签全文草稿'}<=remaining and '甲标签全文草稿' not in remaining
 for page in (a,b):page.locator('#outlineTree .outline-node-select').first.click()
 a.locator('#outlineDetailTitle').fill('甲节点草稿');b.locator('#outlineDetailTitle').fill('乙节点草稿');wait_previews('node',['旧版节点草稿','甲节点草稿','乙节点草稿'],'node-1')
 a.locator('#outlineNodeSave').click();a.wait_for_function("document.querySelector('#outlineOperationStatus').textContent.includes('节点已保存')")
 remaining={d['preview'] for d in listing('node','node-1')};assert '乙节点草稿' in remaining and '旧版节点草稿' in remaining and '甲节点草稿' not in remaining
 cc=browser.new_context();c=cc.new_page();c.on('pageerror',lambda e:errors.append(str(e)));c.goto(URL);c.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=pid);c.locator('[data-tab="outline"]').click();c.locator('#outlineTextRecovery').wait_for();c.locator('#outlineTextDraftMore').click();c.locator('#outlineTextDraftSelect option').filter(has_text='分页旧草稿00').wait_for(state='attached')
 # A selection is only valid for the version displayed in the list.
 stale=c.locator('#outlineTextDraftSelect option').filter(has_text='乙标签全文草稿');stale_key=stale.get_attribute('value');c.locator('#outlineTextDraftSelect').select_option(stale_key)
 put(stale_key,{'text':'其他页面更新后的草稿'});prior=c.locator('#outlineText').input_value();c.locator('#restoreOutlineTextDraft').click();c.wait_for_function("document.querySelector('#outlineStructuredStatus').textContent.includes('草稿已被其他页面更新')");assert c.locator('#outlineText').input_value()==prior
 c.locator('#outlineTextDraftSelect').select_option(stale_key);put(stale_key,{'text':'删除前再次更新'});c.once('dialog',lambda dialog:dialog.accept());c.locator('#discardOutlineTextDraft').click();c.wait_for_function("document.querySelector('#outlineStructuredStatus').textContent.includes('删除未完成')");assert ok(req.get(f'{URL}/api/projects/{pid}/outline/drafts/{stale_key}'))['content']['text']=='删除前再次更新'
 put(stale_key,{'text':'乙标签全文草稿'});c.reload();c.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=pid);c.locator('[data-tab="outline"]').click();c.locator('#outlineTextRecovery').wait_for()
 option=c.locator('#outlineTextDraftSelect option').filter(has_text='乙标签全文草稿');c.locator('#outlineTextDraftSelect').select_option(option.get_attribute('value'));c.locator('#restoreOutlineTextDraft').click();c.wait_for_function("document.querySelector('#outlineText').value==='乙标签全文草稿'")
 assert {'旧版全文草稿','乙标签全文草稿'}<={d['preview'] for d in listing('editor')}
 c.locator('#outlineTree .outline-node-select').first.click();node_option=c.locator('#outlineNodeDraftSelect option').filter(has_text='乙节点草稿');node_option.wait_for(state='attached');c.locator('#outlineNodeDraftSelect').select_option(node_option.get_attribute('value'));c.locator('#outlineNodeDraftRestore').click();c.wait_for_function("document.querySelector('#outlineDetailTitle').value==='乙节点草稿'");c.locator('#outlineNodeSave').click();c.wait_for_function("document.querySelector('#outlineNodeDraftStatus').textContent.includes('版本')");assert c.locator('#outlineDetailTitle').input_value()=='乙节点草稿';assert '乙节点草稿' in {d['preview'] for d in listing('node','node-1')}
 c.locator('#outlineText').fill('第三页接续草稿');c.locator('#outlineSaveDraft').click();wait_previews('editor',['旧版全文草稿','乙标签全文草稿','第三页接续草稿'])
 old_option=c.locator('#outlineTextDraftSelect option').filter(has_text='旧版全文草稿');old_option.wait_for(state='attached');c.locator('#outlineTextDraftSelect').select_option(old_option.get_attribute('value'));c.once('dialog',lambda dialog:dialog.accept());c.locator('#discardOutlineTextDraft').click()
 deadline=time.monotonic()+10
 while time.monotonic()<deadline:
  remaining={d['preview'] for d in listing('editor')}
  if '旧版全文草稿' not in remaining:break
  time.sleep(.1)
 assert '旧版全文草稿' not in remaining and {'乙标签全文草稿','第三页接续草稿'}<=remaining
 assert not errors,errors
 files=['app.py','static/development-ui.js','tests/browser_req003_multitab_drafts.py','.project-to-act/local/project-development/full-product-20261101/ui-legacy-outline-server.py']
 report={'passed':True,'isolated_database':True,'time':datetime.now(timezone.utc).isoformat(),'source_sha256':{f:hashlib.sha256(Path(f).read_bytes()).hexdigest() for f in files},'checks':['two tabs keep separate text drafts','saving one preserves other and legacy drafts','node drafts stay isolated and stale restore cannot overwrite newer node','new page selects and restores another draft without deleting source','stale selections cannot restore or delete a changed draft','continued edit creates independent draft','older recovery pages can be loaded','explicit delete preserves other drafts','no page errors']}
 Path('.project-to-act/docs/project-development/evidence/full-product-20261101/REQ-003-MULTITAB-DRAFTS-BROWSER.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');browser.close();print(json.dumps(report,ensure_ascii=False))
