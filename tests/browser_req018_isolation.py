"""OA preview, replay and late response across paper switches in an isolated UI."""
import argparse,hashlib,json,time
from datetime import datetime,timezone
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1];p=argparse.ArgumentParser();p.add_argument('--port',required=True,type=int);url=f'http://127.0.0.1:{p.parse_args().port}'
with sync_playwright() as pw:
 browser=pw.chromium.launch(headless=True);ctx=browser.new_context(viewport={'width':1280,'height':900});req=ctx.request
 def ok(r):
  assert r.ok,r.text()
  return r.json()
 projects=ok(req.get(url+'/api/projects'));a=next(x['id'] for x in projects if x['name']=='导入来源论文');b=next(x['id'] for x in projects if x['name']=='切换目标论文');items=ok(req.get(url+f'/api/projects/{a}/search/oa-ui-search'))['candidates']
 page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(url);page.wait_for_load_state('networkidle');page.locator(f'.project-item[data-id="{a}"]').click();page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=a);page.locator('[data-tab="knowledge"]').click();page.evaluate('(items)=>renderAcademicCandidates(items)',items)
 page.locator('[data-fetch-oa="oa-fast"]').click();box=page.locator('#academicDetails');box.get_by_text('正常公开论文预览',exact=False).wait_for();box.get_by_role('button',name='取消本次导入').click();box.wait_for(state='hidden');assert box.is_hidden() and len(ok(req.get(url+f'/api/projects/{a}'))['corpus'])==0;page.locator('[data-fetch-oa="oa-fast"]').click();box.get_by_role('button',name='确认 OA 内容并写入知识库').wait_for();page.locator('[data-fetch-oa="oa-fast"]').click();box.get_by_role('button',name='确认 OA 内容并写入知识库').wait_for();assert '正常公开论文预览' in box.inner_text()
 box.get_by_role('button',name='确认 OA 内容并写入知识库').click();box.get_by_text('已写入知识库',exact=False).wait_for();assert len(ok(req.get(url+f'/api/projects/{a}'))['corpus'])==1
 page.locator('[data-fetch-oa="oa-slow"]').click();page.locator(f'.project-item[data-id="{b}"]').click();page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=b);time.sleep(2.5);assert box.is_hidden() and len(ok(req.get(url+f'/api/projects/{b}'))['corpus'])==0
 assert not errors,errors
 files=['app.py','paperpilot/core/database.py','paperpilot/services/imports.py','static/app.js','tests/test_req018_isolation.py','tests/browser_req018_isolation.py'];report={'passed':True,'time':datetime.now(timezone.utc).isoformat(),'isolated_database':True,'fixture_fetch':True,'checks':['preview from same paper','cancelled preview leaves knowledge base unchanged and can retry','replayed preview displayed','confirmation imports once','late response hidden after paper switch','other paper unchanged','no browser errors'],'source_sha256':{f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in files}}
 (root/'.project-to-act/docs/project-development/evidence/full-product-20261101/REQ-018-ISOLATION-BROWSER.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');browser.close();print(json.dumps({'passed':True,'checks':len(report['checks'])}))
