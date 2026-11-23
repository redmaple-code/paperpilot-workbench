"""Exercise domain selection and rejected migration in an isolated browser."""
import argparse, hashlib, json
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright

root=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--port',required=True,type=int);url=f'http://127.0.0.1:{p.parse_args().port}'
with sync_playwright() as pw:
    browser=pw.chromium.launch(headless=True);context=browser.new_context(viewport={'width':1280,'height':900});req=context.request
    def ok(r):
        assert r.ok, r.text()
        return r.json()
    a=ok(req.post(url+'/api/domains',data={'name':'浏览器领域甲'}));b=ok(req.post(url+'/api/domains',data={'name':'浏览器领域乙'}))
    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(url);page.wait_for_load_state('networkidle');page.locator('#newDialog').wait_for(state='visible')
    page.wait_for_function("document.querySelector('#newDomainSelect').options.length >= 3")
    page.locator('#newName').fill('选择领域的新论文');page.locator('#newDomainSelect').select_option(a['id']);page.locator('#createProject').click()
    page.locator('#newDialog').wait_for(state='hidden');page.wait_for_function("window.paperpilot?.current?.project?.name === '选择领域的新论文'")
    project=page.evaluate('paperpilot.current.project');assert project['domain_id']==a['id'] and project['domain']==a['name']
    before=ok(req.get(url+f"/api/projects/{project['id']}"))['project']
    page.locator('[data-tab="knowledge"]').click();page.locator('#domainSelect').select_option(b['id']);page.locator('#saveDomainBinding').click()
    assert '暂不支持跨领域迁移' in page.locator('#configurationStatus').inner_text()
    assert ok(req.get(url+f"/api/projects/{project['id']}"))['project']==before
    assert not errors, errors
    files=['app.py','paperpilot/services/domain_workspace.py','paperpilot/services/build_restores.py','paperpilot/services/settings.py','static/development-ui.js','static/app.js','static/workflow-ui.js','static/index.html','tests/browser_req016_domain_binding.py']
    report={'passed':True,'isolated_database':True,'time':datetime.now(timezone.utc).isoformat(),'checks':['select existing domain when creating paper','created identity and display name match selected domain','attempted migration explained and project unchanged','no browser errors'],'source_sha256':{f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in files}}
    out=root/'.project-to-act/docs/project-development/evidence/full-product-20261101/REQ-016-DOMAIN-BINDING-BROWSER.json';out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    browser.close();print(json.dumps({'passed':True,'checks':len(report['checks'])}))
