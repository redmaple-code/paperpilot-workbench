"""Exercise partial settings sync, source labels and unbound creation in a browser."""
import argparse,hashlib,json
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1];parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,required=True);url=f'http://127.0.0.1:{parser.parse_args().port}'
with sync_playwright() as pw:
 browser=pw.chromium.launch(headless=True);ctx=browser.new_context(viewport={'width':1280,'height':900});req=ctx.request
 def ok(r):
  assert r.ok,r.text()
  return r.json()
 domain=ok(req.post(url+'/api/domains',data={'name':'逐项来源领域'}));project=ok(req.post(url+'/api/projects',data={'name':'来源论文','domain_id':domain['id']}))['project'];pid=project['id']
 ok(req.put(url+f"/api/domains/{domain['id']}/settings",data={'expected_version':1,'request_id':'browser-domain','settings':{'term_policy':'domain_preferred','format_policy':'strict'}}))
 page=ctx.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)));page.goto(url);page.wait_for_load_state('networkidle');page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=pid)
 page.locator('[data-tab="knowledge"]').click();page.locator('#settingsSync').wait_for(state='visible');page.get_by_role('button',name='检查领域更新').click();sync=page.locator('#settingsSync');sync.locator('input[value="format_policy"]').uncheck();sync.get_by_role('button',name='按选择同步到论文基线').click()
 page.wait_for_function("document.querySelector('#setting-term_policy')?.value === 'domain_preferred'");view=ok(req.get(url+f'/api/projects/{pid}/settings'))
 assert view['sources']['term_policy']['domain_version']==2 and view['sources']['format_policy']['domain_version']==1
 assert '领域 v2' in page.locator('#setting-term_policy').locator('..').inner_text() and '领域 v1' in page.locator('#setting-format_policy').locator('..').inner_text()
 page.locator('#newProject').click();page.locator('#newDialog').wait_for(state='visible');page.locator('#newName').fill('无领域论文');page.locator('#newDomainSelect').select_option('');page.locator('#createProject').click();page.locator('#newDialog').wait_for(state='hidden');page.wait_for_function("paperpilot.current?.project.name==='无领域论文'");assert not page.evaluate('paperpilot.current.project.domain_id')
 assert not errors,errors
 files=['app.py','paperpilot/core/database.py','paperpilot/services/settings.py','paperpilot/services/domain_workspace.py','paperpilot/domain/schemas.py','static/development-ui.js','static/app.js','static/workflow-ui.js','tests/browser_req017_setting_sources.py'];report={'passed':True,'isolated_database':True,'time':datetime.now(timezone.utc).isoformat(),'checks':['settings sync control visible','partial sync preserves unselected source version','per-key source versions shown','unbound paper can be created','no browser errors'],'source_sha256':{f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in files}}
 (root/'.project-to-act/docs/project-development/evidence/full-product-20261101/REQ-017-SETTING-SOURCES-BROWSER.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');browser.close();print(json.dumps({'passed':True,'checks':len(report['checks'])}))
