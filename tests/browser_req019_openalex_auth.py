"""Provider pagination auth failure is actionable and keeps existing results."""
import hashlib,json
from datetime import datetime,timezone
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1];url='http://127.0.0.1:40960'
with sync_playwright() as pw:
 browser=pw.chromium.launch(headless=True);ctx=browser.new_context(viewport={'width':1280,'height':900});req=ctx.request
 def ok(r):
  assert r.ok,r.text();return r.json()
 pid=next(x['id'] for x in ok(req.get(url+'/api/projects')) if x['name']=='检索鉴权验证');page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(url);page.wait_for_load_state('networkidle');page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=pid);page.locator('[data-tab="knowledge"]').click();page.locator('#academicQuery').fill('paper');page.locator('#academicSearch').click();page.get_by_text('第一页结果',exact=False).first.wait_for();more=page.locator('#academicMore');more.click();page.get_by_text('PWA_ACADEMIC_SEARCH_API_KEY',exact=False).first.wait_for();assert page.get_by_text('第一页结果',exact=False).first.is_visible() and more.is_visible();more.click();page.get_by_text('第二页结果',exact=False).first.wait_for();assert page.get_by_text('第一页结果',exact=False).first.is_visible() and more.is_hidden() and not errors,errors
 files=['app.py','paperpilot/services/academic_search.py','static/app.js','tests/test_req019_openalex_auth.py','tests/browser_req019_openalex_auth.py'];report={'passed':True,'isolated_database':True,'fixture_provider':True,'time':datetime.now(timezone.utc).isoformat(),'checks':['cursor auth setup guidance visible','existing results and retry retained','retry restores pagination','no browser errors'],'source_sha256':{f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in files}}
 (root/'.project-to-act/docs/project-development/evidence/full-product-20261101/REQ-019-AUTH-BROWSER.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');browser.close();print(json.dumps({'passed':True,'checks':len(report['checks'])}))
