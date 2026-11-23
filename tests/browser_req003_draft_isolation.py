"""Browser regression for outline drafts across rapid changes and slow saves."""
import hashlib,json,time
from datetime import datetime,timezone
from pathlib import Path
from playwright.sync_api import sync_playwright
URL='http://127.0.0.1:28494'
with sync_playwright() as pw:
 browser=pw.chromium.launch(headless=True);ctx=browser.new_context(viewport={'width':1280,'height':900});req=ctx.request
 def ok(response):
  assert response.ok,response.text();return response.json()
 def wait_draft(pid,key,expected):
  from urllib.parse import unquote
  logical=unquote(key);params={'kind':'editor'} if logical=='editor' else {'kind':'node','node_id':logical.removeprefix('node:')}
  deadline=time.monotonic()+10
  while time.monotonic()<deadline:
   listed=req.get(f'{URL}/api/projects/{pid}/outline/draft-recovery',params=params)
   if listed.ok:
    for item in listed.json()['drafts']:
     if item['preview']==expected:
      r=req.get(f"{URL}/api/projects/{pid}/outline/drafts/{item['draft_key']}")
      if r.ok:return r.json()
   time.sleep(.1)
  raise AssertionError(f'draft not found: {pid} {key} {expected}')
 a=ok(req.post(URL+'/api/projects',data={'name':'大纲隔离甲'}))['project']['id'];b=ok(req.post(URL+'/api/projects',data={'name':'大纲隔离乙'}))['project']['id']
 preview=ok(req.post(f'{URL}/api/projects/{a}/outline/migration/preview',data={'request_id':'browser-preview'}))
 graph=ok(req.post(f'{URL}/api/projects/{a}/outline/migration/commit',data={'draft_id':preview['id'],'request_id':'browser-commit'}));nodes=graph['nodes'];assert len(nodes)>=2
 page=ctx.new_page();errors=[];page.on('pageerror',lambda error:errors.append(str(error)));page.goto(URL);page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=a);page.locator('[data-tab="outline"]').click();page.locator('#outlineNodeCreate').wait_for()
 page.locator('#outlineText').fill('甲论文独立大纲草稿');page.evaluate('(id)=>window.loadProject(id)',b);page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=b);page.locator('#outlineText').fill('乙论文独立大纲草稿')
 wait_draft(a,'editor','甲论文独立大纲草稿');wait_draft(b,'editor','乙论文独立大纲草稿')
 page.locator('#outlineText').fill('慢写入旧内容');page.wait_for_function("async()=> (await (await fetch('/test/draft-started')).json()).started");page.locator('#outlineText').fill('显式保存的新内容');page.locator('#outlineSaveDraft').click();wait_draft(b,'editor','显式保存的新内容')
 page.locator(f'.project-item[data-id="{a}"]').click();page.get_by_role('button',name='保留大纲草稿并切换').click();page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=a);page.locator('#outlineTextRecovery').wait_for();option=page.locator('#outlineTextDraftSelect option').filter(has_text='甲论文独立大纲草稿');page.locator('#outlineTextDraftSelect').select_option(option.get_attribute('value'));page.locator('#restoreOutlineTextDraft').click();page.wait_for_function("document.querySelector('#outlineText').value==='甲论文独立大纲草稿'")
 tree=page.locator('#outlineTree .outline-node-select');tree.first.wait_for();assert tree.count()>=2
 tree.nth(0).click();page.locator('#outlineDetailTitle').fill('节点一草稿');tree.nth(1).click();page.locator('#outlineDetailTitle').fill('节点二草稿')
 wait_draft(a,'node%3A'+nodes[0]['id'],'节点一草稿');wait_draft(a,'node%3A'+nodes[1]['id'],'节点二草稿')
 page.locator('#outlineDetailTitle').fill('保存时旧值');page.locator('#outlineNodeSave').click();page.wait_for_function("async()=> (await (await fetch('/test/node-started')).json()).started");page.locator('#outlineDetailTitle').fill('保存期间新值')
 page.wait_for_function("document.querySelector('#outlineNodeDraftStatus').textContent.includes('保存期间的新输入')");wait_draft(a,'node%3A'+nodes[1]['id'],'保存期间新值')
 latest=ok(req.get(f'{URL}/api/projects/{a}/outline/structured'));assert next(n for n in latest['nodes'] if n['id']==nodes[1]['id'])['title']=='保存时旧值';assert page.locator('#outlineDetailTitle').input_value()=='保存期间新值'
 assert not errors,errors
 report={'passed':True,'isolated_database':True,'time':datetime.now(timezone.utc).isoformat(),'source_sha256':{f:hashlib.sha256(Path(f).read_bytes()).hexdigest() for f in ['app.py','static/app.js','static/development-ui.js','static/project-management-ui.js','tests/browser_req003_draft_isolation.py','.project-to-act/local/project-development/full-product-20261101/ui-outline-draft-server.py']},'checks':['rapid project switch keeps text drafts isolated','explicit save follows an older slow draft write','restore uses current project draft','rapid node switch preserves both drafts','new input during slow save remains draft','no page errors']}
 Path('.project-to-act/docs/project-development/evidence/full-product-20261101/REQ-003-MULTITAB-ISOLATION-BROWSER.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');browser.close();print(json.dumps(report,ensure_ascii=False))
