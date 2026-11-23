"""Browser configuration guidance + real local HTTP generation test, isolated app data."""
from pathlib import Path
import sys
import hashlib
import json
from datetime import datetime,timezone
from playwright.sync_api import sync_playwright
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tests.test_req014_provider_runtime import local_provider

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'.project-to-act/docs/project-development/evidence/full-product-20261101'
URL='http://127.0.0.1:8778'

with local_provider() as (provider_url,calls),sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    context=browser.new_context(viewport={'width':1440,'height':1000});req=context.request
    pid=req.post(URL+'/api/projects',data={'name':'模型配置浏览器验收'}).json()['project']['id']
    page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(URL);page.wait_for_load_state('networkidle')
    page.locator(f'.project-item[data-id="{pid}"]').click()
    page.locator('#openProviderSettings').click()
    page.locator('#providerNew').click()
    page.locator('#providerName').fill('真实本地测试模型')
    page.locator('#providerUrl').fill(provider_url+'/v1')
    page.locator('#providerAuth').select_option('none')
    page.locator('#providerModels').fill('test-model');page.locator('#providerModels').press('Tab')
    page.locator('#providerInputLimit').fill('100000')
    page.locator('#providerOutputLimit').fill('10000')
    page.locator('#providerCapacitySource').fill('隔离测试提供方声明的保守容量；非云模型证据')
    page.locator('#providerSave').click()
    page.wait_for_function('document.querySelector("#providerGuidance").textContent.includes("配置已保存")')
    assert not page.locator('#providerEnable').is_enabled()
    assert not calls
    page.locator('#providerTestConsent').check();page.locator('#providerTestCost').check()
    page.locator('#providerTest').focus();page.keyboard.press('Enter')
    page.wait_for_function('document.querySelector("#providerTestResults").textContent.includes("生成测试通过")')
    assert len(calls)==1 and calls[0]['auth'] is None
    assert 'No manuscript data' in calls[0]['body']['messages'][1]['content']
    page.locator('#providerEnable').click()
    page.wait_for_function('document.querySelector("#providerGuidance").textContent.includes("此版本已启用")')
    page.locator('#providerBind').click()
    page.wait_for_function('document.querySelector("#providerGuidance").textContent.includes("当前论文已明确绑定")')
    profile=next(v for v in req.get(URL+'/api/ai/profiles').json() if v['name']=='真实本地测试模型')
    assert req.get(URL+f'/api/projects/{pid}').json()['ai_binding']['profile_id']==profile['id']
    # Another writer changes the configuration; saving cannot overwrite it silently.
    page.locator('#providerName').fill('本地冲突后保留的名称')
    remote=req.put(URL+f"/api/ai/profiles/{profile['id']}",data={'base_version':profile['current_version'],'changes':{'name':'服务端已修改的名称'}})
    assert remote.ok
    page.locator('#providerSave').click()
    page.locator('#providerConflict').wait_for(state='visible')
    assert page.locator('#providerName').input_value()=='本地冲突后保留的名称'
    assert '服务端已修改的名称' in page.locator('#providerRemoteDetails').inner_text()
    page.locator('#providerRebase').click();page.locator('#providerSave').click()
    page.wait_for_function('document.querySelector("#providerGuidance").textContent.includes("配置已保存")')
    updated=next(v for v in req.get(URL+'/api/ai/profiles').json() if v['id']==profile['id'])
    assert updated['status']=='enabled'  # Name-only edits retain generation evidence.
    assert len(calls)==1
    old=req.get(URL+f'/api/projects/{pid}').json()['ai_binding']['ai_config_revision']
    assert old<updated['current_version']
    page.locator('#providerVersion').select_option(str(updated['current_version']))
    page.locator('#providerImpactRefresh').click()
    page.wait_for_function('document.querySelectorAll("#providerImpact input").length>0')
    page.locator(f'[data-provider-paper="{pid}"]').check();page.locator('#providerApply').click()
    page.wait_for_function('document.querySelector("#providerGuidance").textContent.includes("仅所选论文已应用") || document.querySelector("#providerGuidance").dataset.error==="true"')
    assert '仅所选论文已应用' in page.locator('#providerGuidance').inner_text(),page.locator('#providerGuidance').inner_text()
    assert req.get(URL+f'/api/projects/{pid}').json()['ai_binding']['ai_config_revision']==updated['current_version']
    page.locator('#providerDefault').click()
    page.wait_for_function('document.querySelector("#providerGuidance").textContent.includes("仅用于以后新建")')
    fresh=req.post(URL+'/api/projects',data={'name':'仅新论文使用默认'}).json()
    assert fresh['ai_binding']['profile_id']==profile['id']
    page.locator('#providerSettings').screenshot(path=str(OUT/'REQ-014-desktop.png'))
    page.set_viewport_size({'width':390,'height':844})
    page.locator('#providerSettings').scroll_into_view_if_needed()
    assert page.locator('#providerSettings').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
    page.locator('#providerSettings').screenshot(path=str(OUT/'REQ-014-mobile.png'))
    page.locator('#providerDisable').click()
    page.wait_for_function('document.querySelector("#providerGuidance").textContent.includes("配置状态已保存")')
    assert not page.locator('#providerBind').is_enabled()
    assert len(calls)==1 and not errors,errors
    report={'requirement':'REQ-014 v16','time':datetime.now(timezone.utc).isoformat(),'passed':True,
        'generation_test':'real local HTTP server + real adapter, fixed non-manuscript input','writing_model':'UI server test double; no cloud quality claim',
        'checks':['guided draft/test/enable/bind','explicit test and cost authorization','no-key local transport','name-only no retest','conflict comparison and input preservation','explicit rebase','old paper binding stays fixed','selected paper application','new-paper-only default','keyboard Enter','390px panel width','disabled reason and controls'],
        'browser_errors':errors,'source_sha256':{f:hashlib.sha256((ROOT/f).read_bytes()).hexdigest() for f in ['static/provider-settings-ui.js','paperpilot/services/provider_runtime.py','paperpilot/services/provider_config.py']}}
    (OUT/'REQ-014-BROWSER.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'checks':len(report['checks'])},ensure_ascii=False))
    browser.close()
