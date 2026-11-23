"""Load another page and retain saved identity while upgrading OA metadata."""
import hashlib,json
from datetime import datetime,timezone
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1];url='http://127.0.0.1:25811'
with sync_playwright() as pw:
 browser=pw.chromium.launch(headless=True);ctx=browser.new_context(viewport={'width':1280,'height':900});req=ctx.request
 def ok(r):
  assert r.ok,r.text();return r.json()
 pid=next(x['id'] for x in ok(req.get(url+'/api/projects')) if x['name']=='分页验证论文')
 page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(url);page.wait_for_load_state('networkidle');page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=pid);page.locator('[data-tab="knowledge"]').click();page.locator('#academicQuery').fill('paper');page.locator('#academicSearch').click();card=page.locator('.candidate-card').filter(has_text='分页论文');card.get_by_text('非 OA / 仅元数据').wait_for();page.get_by_text('还有更多结果',exact=False).wait_for();save=card.get_by_role('button',name='保存候选');cid=save.get_attribute('data-save-candidate');save.click();card.get_by_role('button',name='取消保存').wait_for();more=page.locator('#academicMore');more.wait_for();more.click();page.get_by_text('新结果',exact=False).first.wait_for();card=page.locator('.candidate-card').filter(has_text='分页论文');card.get_by_text('OA 可获取').wait_for();assert card.get_by_role('button',name='取消保存').get_attribute('data-save-candidate')==cid;assert card.get_by_role('button',name='获取 OA 全文').is_visible();assert more.is_hidden();assert not errors,errors
 history=ok(req.get(url+f'/api/projects/{pid}/searches'))['searches'];snap=ok(req.get(url+f'/api/projects/{pid}/search/{history[0]["id"]}'));merged=next(x for x in snap['candidates'] if x['id']==cid);assert {x['provider'] for x in merged['sources']}=={'openalex','crossref'} and merged['saved']
 files=['app.py','paperpilot/services/academic_search.py','static/app.js','static/index.html','tests/test_req019_pagination_merge.py','tests/browser_req019_pagination.py'];report={'passed':True,'isolated_database':True,'fixture_provider':True,'time':datetime.now(timezone.utc).isoformat(),'checks':['load more visible and completes','duplicate upgraded to OA','candidate identity and saved state preserved','source list retained','no browser errors'],'source_sha256':{f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in files}}
 (root/'.project-to-act/docs/project-development/evidence/full-product-20261101/REQ-019-PAGINATION-BROWSER.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');browser.close();print(json.dumps({'passed':True,'checks':len(report['checks'])}))
