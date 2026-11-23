"""Restore a saved search with original partial-source diagnostics."""
import hashlib,json
from datetime import datetime,timezone
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1];url='http://127.0.0.1:33486'
with sync_playwright() as pw:
 browser=pw.chromium.launch(headless=True);ctx=browser.new_context(viewport={'width':1280,'height':900});req=ctx.request
 def ok(r):
  assert r.ok,r.text();return r.json()
 items=ok(req.get(url+'/api/projects'));a=next(x['id'] for x in items if x['name']=='快照论文甲');b=next(x['id'] for x in items if x['name']=='快照论文乙')
 page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(url);page.wait_for_load_state('networkidle');page.locator(f'.project-item[data-id="{a}"]').click();page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=a);page.locator('[data-tab="knowledge"]').click();page.locator('#academicQuery').fill('graphene');page.locator('#academicSearch').click();summary=page.locator('#academicSearchSummary');summary.get_by_text('请求过于频繁',exact=False).wait_for();assert '可能未覆盖' in summary.inner_text();assert '自动翻页 2 页' in summary.inner_text();page.locator('#academicSaveSearch').click();page.get_by_text('检索快照已保存',exact=False).wait_for()
 page.locator(f'.project-item[data-id="{b}"]').click();page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=b);page.locator(f'.project-item[data-id="{a}"]').click();page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=a);page.locator('#academicHistory').get_by_role('button',name='恢复').first.click();summary.get_by_text('请求过于频繁',exact=False).wait_for();assert '可能未覆盖' in summary.inner_text() and '自动翻页 2 页' in summary.inner_text();assert page.locator('#academicQuery').input_value()=='graphene' and ok(req.get(url+'/test/search-calls'))['count']==1;assert not errors,errors
 files=['app.py','paperpilot/core/database.py','paperpilot/services/academic_search.py','static/app.js','tests/test_req019_snapshot_provenance.py','tests/browser_req019_snapshot.py'];report={'passed':True,'isolated_database':True,'fixture_provider':True,'time':datetime.now(timezone.utc).isoformat(),'checks':['partial source and truncation shown','saved snapshot restored after paper switch','restore did not call provider','no browser errors'],'source_sha256':{f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in files}}
 (root/'.project-to-act/docs/project-development/evidence/full-product-20261101/REQ-019-SNAPSHOT-BROWSER.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');browser.close();print(json.dumps({'passed':True,'checks':len(report['checks'])}))
