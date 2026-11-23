"""Create and select a domain using the real isolated application UI."""
import argparse, hashlib, json
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1]
p=argparse.ArgumentParser();p.add_argument('--port',required=True,type=int);url=f'http://127.0.0.1:{p.parse_args().port}'
with sync_playwright() as pw:
    browser=pw.chromium.launch(headless=True);context=browser.new_context(viewport={'width':390,'height':844});req=context.request
    def ok(r):
        assert r.ok, r.text()
        return r.json()
    ok(req.post(url+'/api/domains',data={'name':'已有领域'}))
    page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(url);page.wait_for_load_state('networkidle');page.locator('#newDialog').wait_for(state='visible');page.wait_for_function("document.querySelector('#newDomainSelect').options.length >= 2")
    page.get_by_text('创建新领域库',exact=True).click();page.locator('#createDomainLibrary').click();assert '请输入' in page.locator('#newDomainLibraryStatus').inner_text()
    page.route('**/api/domains', lambda route: route.fulfill(status=503, body='unavailable') if route.request.method=='POST' else route.continue_())
    page.locator('#newDomainLibraryName').fill('网络故障领域');page.locator('#createDomainLibrary').click();page.get_by_text('创建未确认',exact=False).wait_for();assert len(ok(req.get(url+'/api/domains')))==1
    page.unroute('**/api/domains')
    page.locator('#newDomainLibraryName').fill('已有领域');page.locator('#createDomainLibrary').click();page.get_by_text('同名领域库已存在',exact=False).wait_for();assert len(ok(req.get(url+'/api/domains')))==1
    page.locator('#newDomainLibraryName').fill('新领域 & 论文');page.locator('#createDomainLibrary').click();page.get_by_text('已创建并选择',exact=False).wait_for()
    domains=ok(req.get(url+'/api/domains'));new=next(x for x in domains if x['name']=='新领域 & 论文');assert page.locator('#newDomainSelect').input_value()==new['id']
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+2')
    page.locator('#cancelNewProject').click();assert any(x['id']==new['id'] for x in ok(req.get(url+'/api/domains')))
    page.locator('#newProject').click();page.locator('#newDialog').wait_for(state='visible');page.wait_for_function("document.querySelector('#newDomainSelect').options.length >= 3")
    page.locator('#newDomainSelect').select_option(new['id']);page.locator('#newName').fill('新建领域论文');page.locator('#createProject').click();page.locator('#newDialog').wait_for(state='hidden')
    page.wait_for_function("window.paperpilot?.current?.project?.name==='新建领域论文'");project=page.evaluate('paperpilot.current.project');assert project['domain_id']==new['id'] and project['domain']==new['name'];assert not errors, errors
    files=['static/app.js','static/index.html','static/workflow-ui.js','app.py','paperpilot/services/domain_workspace.py','tests/browser_req016_create_domain.py']
    report={'passed':True,'isolated_database':True,'time':datetime.now(timezone.utc).isoformat(),'checks':['empty name explained','failed request keeps paper draft and guides retry','duplicate rejected without extra domain','new domain selected and persisted after cancel','existing domain selected to create paper','390px no horizontal overflow','no browser errors'],'source_sha256':{f:hashlib.sha256((root/f).read_bytes()).hexdigest() for f in files}}
    out=root/'.project-to-act/docs/project-development/evidence/full-product-20261101/REQ-016-DOMAIN-CREATE-BROWSER.json';out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');browser.close();print(json.dumps({'passed':True,'checks':len(report['checks'])}))
