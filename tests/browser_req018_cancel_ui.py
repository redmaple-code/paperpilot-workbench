"""Exercise a real browser cancel while the server's isolated fetch is held."""
import hashlib,json,time
from datetime import datetime,timezone
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1];url='http://127.0.0.1:15326'
with sync_playwright() as pw:
 browser=pw.chromium.launch(headless=True);ctx=browser.new_context(viewport={'width':390,'height':844});req=ctx.request
 def ok(r):
  assert r.ok,r.text();return r.json()
 pid=next(x['id'] for x in ok(req.get(url+'/api/projects')) if x['name']=='下载取消测试')
 items=ok(req.get(url+f'/api/projects/{pid}/search/cancel-search'))['candidates']
 page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(url);page.wait_for_load_state('networkidle');page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=pid);page.locator('[data-tab="knowledge"]').click();page.evaluate('(items)=>renderAcademicCandidates(items)',items)
 page.locator('[data-fetch-oa="cancel-candidate"]').click();box=page.locator('#academicDetails');page.locator('#academicOaProgress').get_by_text('正在抓取 OA 全文',exact=False).wait_for();assert ok(req.get(url+'/test/fetch-status'))['started']
 page.locator('[data-candidate="cancel-candidate"]').click();progress=page.locator('#academicOaProgress');progress.get_by_role('button',name='取消抓取').click();progress.get_by_text('已取消抓取',exact=False).wait_for();assert not ok(req.get(url+f'/api/projects/{pid}'))['corpus']
 ok(req.post(url+'/test/release'));time.sleep(1);assert '已取消抓取' in progress.inner_text() and '迟到预览' not in box.inner_text();assert not ok(req.get(url+f'/api/projects/{pid}'))['corpus']
 page.locator('[data-fetch-oa="cancel-candidate"]').click();box.get_by_text('迟到预览',exact=False).wait_for();box.get_by_role('button',name='确认 OA 内容并写入知识库').click();box.get_by_text('已写入知识库',exact=False).wait_for();page.locator('[data-fetch-oa="cancel-candidate"]').click();box.get_by_text('已导入知识库',exact=False).wait_for();assert len(ok(req.get(url+f'/api/projects/{pid}'))['corpus'])==1;assert not errors,errors
 files=['app.py','paperpilot/domain/schemas.py','paperpilot/services/imports.py','static/app.js','static/index.html','tests/test_req018_cancel_ui.py','tests/browser_req018_cancel_ui.py'];report={'passed':True,'isolated_database':True,'fixture_fetch':True,'time':datetime.now(timezone.utc).isoformat(),'checks':['cancel remains visible after opening candidate details','cancel acknowledged','late result not displayed or imported','retry produces preview and repeat import does not duplicate corpus','no browser errors'],'source_sha256':{f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in files}}
 (root/'.project-to-act/docs/project-development/evidence/full-product-20261101/REQ-018-CANCEL-BROWSER.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');browser.close();print(json.dumps({'passed':True,'checks':len(report['checks'])}))
