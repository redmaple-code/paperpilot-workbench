"""OA UI explains refusal of a non-public address without importing content."""
import argparse,hashlib,json
from datetime import datetime,timezone
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1];p=argparse.ArgumentParser();p.add_argument('--port',required=True,type=int);url=f'http://127.0.0.1:{p.parse_args().port}'
with sync_playwright() as pw:
 browser=pw.chromium.launch(headless=True);ctx=browser.new_context(viewport={'width':390,'height':844});req=ctx.request
 def ok(r):
  assert r.ok,r.text()
  return r.json()
 pid=next(x['id'] for x in ok(req.get(url+'/api/projects')) if x['name']=='公开抓取检查');items=ok(req.get(url+f'/api/projects/{pid}/search/public-test-search'))['candidates']
 page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(url);page.wait_for_load_state('networkidle');page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=pid);page.locator('[data-tab="knowledge"]').click();page.evaluate('(items)=>renderAcademicCandidates(items)',items);page.locator('[data-fetch-oa="private-test"]').click();page.get_by_text('来源或跳转指向非公网地址',exact=False).first.wait_for();assert not ok(req.get(url+f'/api/projects/{pid}'))['corpus'];assert not errors,errors
 files=['app.py','paperpilot/services/imports.py','static/app.js','tests/test_req018_public_fetch.py','tests/browser_req018_public_fetch.py'];report={'passed':True,'isolated_database':True,'time':datetime.now(timezone.utc).isoformat(),'checks':['non-public source refusal shown','no imported corpus','no browser errors'],'source_sha256':{f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in files}}
 (root/'.project-to-act/docs/project-development/evidence/full-product-20261101/REQ-018-PUBLIC-FETCH-BROWSER.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');browser.close();print(json.dumps({'passed':True,'checks':len(report['checks'])}))
