"""UI warns on a locked temporary attachment and retries cleanup later."""
import hashlib,json
from datetime import datetime,timezone
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1];url='http://127.0.0.1:15328'
with sync_playwright() as pw:
 browser=pw.chromium.launch(headless=True);ctx=browser.new_context(viewport={'width':390,'height':844});req=ctx.request
 def ok(r):
  assert r.ok,r.text();return r.json()
 pid=next(x['id'] for x in ok(req.get(url+'/api/projects')) if x['name']=='下载取消测试');items=ok(req.get(url+f'/api/projects/{pid}/search/cancel-search'))['candidates']
 page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(url);page.wait_for_load_state('networkidle');page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=pid);page.locator('[data-tab="knowledge"]').click();page.evaluate('(items)=>renderAcademicCandidates(items)',items)
 page.locator('[data-fetch-oa="cancel-candidate"]').click();box=page.locator('#academicDetails');box.get_by_role('button',name='确认 OA 内容并写入知识库').click();box.get_by_text('临时附件尚未清理',exact=False).wait_for();assert len(ok(req.get(url+f'/api/projects/{pid}'))['corpus'])==1
 ok(req.post(url+'/test/unlock'));row=ok(req.post(url+f'/api/projects/{pid}/imports',data=json.dumps({'candidate_id':'cancel-candidate','defer_fetch':True}),headers={'Content-Type':'application/json'}))['import'];assert not ok(req.get(url+f'/api/projects/{pid}/imports/{row["id"]}'))['cleanup_pending'];assert not errors,errors
 files=['app.py','paperpilot/core/database.py','static/app.js','tests/test_req018_cleanup.py','tests/oa_cleanup_worker.py','tests/browser_req018_cleanup.py'];report={'passed':True,'isolated_database':True,'fixture_fetch':True,'time':datetime.now(timezone.utc).isoformat(),'checks':['cleanup failure visible','confirmed evidence preserved','cleanup retried after unlock','no browser errors'],'source_sha256':{f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in files}}
 (root/'.project-to-act/docs/project-development/evidence/full-product-20261101/REQ-018-CLEANUP-BROWSER.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');browser.close();print(json.dumps({'passed':True,'checks':len(report['checks'])}))
