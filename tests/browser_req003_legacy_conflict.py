"""Browser regression: legacy outline conflict retains the losing tab's input."""
import hashlib,json
from datetime import datetime,timezone
from pathlib import Path
from playwright.sync_api import sync_playwright
URL='http://127.0.0.1:28495';pid='abcdef123456'
with sync_playwright() as pw:
 browser=pw.chromium.launch(headless=True);one=browser.new_context();two=browser.new_context();a=one.new_page();b=two.new_page();errors=[]
 for page in (a,b):
  page.on('pageerror',lambda e:errors.append(str(e)));page.goto(URL);page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=pid);page.locator('[data-tab="outline"]').click();page.locator('#outlineText').wait_for()
 assert a.locator('#outlineText').input_value()=='旧大纲原文' and b.locator('#outlineText').input_value()=='旧大纲原文'
 a.locator('#outlineText').fill('第一个窗口保存');a.locator('#saveOutline').click();a.wait_for_function("document.querySelector('#outlineStructuredStatus').textContent==='大纲已保存'")
 b.locator('#outlineText').fill('第二个窗口未丢失');b.locator('#saveOutline').click();b.wait_for_function("document.querySelector('#outlineStructuredStatus').textContent.includes('版本已变化')")
 assert b.locator('#outlineText').input_value()=='第二个窗口未丢失';r=one.request.get(f'{URL}/api/projects/{pid}').json();assert r['outline']=='第一个窗口保存' and r['outline_version']==2
 drafts=one.request.get(f'{URL}/api/projects/{pid}/outline/draft-recovery?kind=editor').json()['drafts'];assert any(d['preview']=='第二个窗口未丢失' for d in drafts);assert not errors,errors
 files=['app.py','static/app.js','static/development-ui.js','static/project-management-ui.js','tests/browser_req003_legacy_conflict.py','.project-to-act/local/project-development/full-product-20261101/ui-legacy-outline-server.py']
 report={'passed':True,'isolated_database':True,'time':datetime.now(timezone.utc).isoformat(),'source_sha256':{f:hashlib.sha256(Path(f).read_bytes()).hexdigest() for f in files},'checks':['legacy first save creates version','stale tab receives conflict','losing text and draft remain','no page errors']}
 Path('.project-to-act/docs/project-development/evidence/full-product-20261101/REQ-003-MULTITAB-LEGACY-CONFLICT-BROWSER.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');browser.close();print(json.dumps(report,ensure_ascii=False))
