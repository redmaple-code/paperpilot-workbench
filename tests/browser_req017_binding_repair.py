"""Test guided binding repair using a seeded isolated legacy database."""
import argparse,hashlib,json
from datetime import datetime,timezone
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1];p=argparse.ArgumentParser();p.add_argument('--port',type=int,required=True);url=f'http://127.0.0.1:{p.parse_args().port}'
with sync_playwright() as pw:
 browser=pw.chromium.launch(headless=True);ctx=browser.new_context(viewport={'width':390,'height':844});req=ctx.request
 def ok(r):
  assert r.ok,r.text()
  return r.json()
 projects=ok(req.get(url+'/api/projects'));pid=next(x['id'] for x in projects if x['name']=='待修复论文');before=ok(req.get(url+f'/api/projects/{pid}/settings'))
 page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(url);page.wait_for_load_state('networkidle');page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=pid)
 page.locator('[data-tab="knowledge"]').click();page.get_by_role('button',name='检查领域更新').click();host=page.locator('#settingsSync');host.get_by_role('button',name='保留配置并修复基线归属').wait_for();assert '历史领域' in host.inner_text() and '当前领域' in host.inner_text() and '保留写作风格' in host.inner_text()
 fix=host.get_by_role('button',name='保留配置并修复基线归属');page.once('dialog',lambda dialog:dialog.dismiss());fix.click();assert ok(req.get(url+f'/api/projects/{pid}/settings'))==before
 page.once('dialog',lambda dialog:dialog.accept());fix.click();page.wait_for_function("document.querySelector('#status')?.textContent.includes('基线归属已修复')")
 after=ok(req.get(url+f'/api/projects/{pid}/settings'));project=ok(req.get(url+f'/api/projects/{pid}'))['project'];assert after['effective']==before['effective'] and after['overrides']==before['overrides'] and after['baseline']['domain_id']==project['domain_id']
 assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+2');assert not errors,errors
 files=['app.py','paperpilot/services/settings.py','paperpilot/domain/schemas.py','static/development-ui.js','tests/test_req017_binding_repair.py','tests/browser_req017_binding_repair.py'];report={'passed':True,'isolated_database':True,'time':datetime.now(timezone.utc).isoformat(),'checks':['explains two domains and current effective values','cancel confirmation leaves configuration unchanged','confirmation preserves values and aligns baseline','390px no horizontal overflow','no browser errors'],'source_sha256':{f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in files}}
 (root/'.project-to-act/docs/project-development/evidence/full-product-20261101/REQ-017-BINDING-REPAIR-BROWSER.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');browser.close();print(json.dumps({'passed':True,'checks':len(report['checks'])}))
