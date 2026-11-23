"""Do not replace newer outline input while a recovery waits for an older draft write."""
import hashlib,json,time
from datetime import datetime,timezone
from pathlib import Path
from playwright.sync_api import sync_playwright
URL='http://127.0.0.1:28494'
with sync_playwright() as pw:
 browser=pw.chromium.launch(headless=True);ctx=browser.new_context();req=ctx.request
 pid=req.post(URL+'/api/projects',data={'name':'恢复期间继续输入'}).json()['project']['id']
 assert req.put(f'{URL}/api/projects/{pid}/outline/drafts/editor',data={'draft_key':'editor','base_version':1,'content':{'text':'历史待恢复内容'}}).ok
 page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(URL);page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=pid);page.locator('[data-tab="outline"]').click();page.locator('#outlineTextRecovery').wait_for()
 option=page.locator('#outlineTextDraftSelect option').filter(has_text='历史待恢复内容');page.locator('#outlineTextDraftSelect').select_option(option.get_attribute('value'))
 page.locator('#outlineText').fill('慢写入旧内容');page.wait_for_function("async()=> (await (await fetch('/test/draft-started')).json()).started")
 page.locator('#restoreOutlineTextDraft').click();page.locator('#outlineText').fill('恢复期间的新输入')
 page.wait_for_function("document.querySelector('#outlineStructuredStatus').textContent.includes('输入仍在变化')");assert page.locator('#outlineText').input_value()=='恢复期间的新输入'
 deadline=time.monotonic()+10
 while time.monotonic()<deadline:
  drafts=req.get(f'{URL}/api/projects/{pid}/outline/draft-recovery?kind=editor').json()['drafts'];previews={d['preview'] for d in drafts}
  if {'历史待恢复内容','恢复期间的新输入'}<=previews:break
  time.sleep(.1)
 assert {'历史待恢复内容','恢复期间的新输入'}<=previews and not errors,errors
 files=['app.py','static/development-ui.js','tests/browser_req003_recovery_race.py','.project-to-act/local/project-development/full-product-20261101/ui-outline-draft-server.py']
 report={'passed':True,'isolated_database':True,'time':datetime.now(timezone.utc).isoformat(),'source_sha256':{f:hashlib.sha256(Path(f).read_bytes()).hexdigest() for f in files},'checks':['recovery waits for slow write','new typing aborts replacement with guidance','both current input and selected source remain recoverable','no page errors']}
 Path('.project-to-act/docs/project-development/evidence/full-product-20261101/REQ-003-RECOVERY-RACE-BROWSER.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');browser.close();print(json.dumps(report,ensure_ascii=False))
