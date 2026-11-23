"""Real local worker and browser; writing provider is the UI server's test double."""
from pathlib import Path
import hashlib
import json
from datetime import datetime, timezone
from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
OUTPUT=ROOT/'.project-to-act/docs/project-development/evidence/full-product-20261101'
URL='http://127.0.0.1:8777'

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    context=browser.new_context(viewport={'width':1440,'height':1100})
    req=context.request
    pid=req.post(URL+'/api/projects',data={'name':'文风学习浏览器验证','domain':'材料科学','style':'严谨'}).json()['project']['id']
    other=req.post(URL+'/api/projects',data={'name':'独立论文','domain':'材料科学'}).json()['project']['id']
    original=req.get(URL+f'/api/projects/{pid}').json()['files']
    page=context.new_page();errors=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.goto(URL);page.wait_for_load_state('networkidle')
    page.locator(f'.project-item[data-id="{pid}"]').click()
    page.wait_for_function('(pid)=>document.querySelector("#styleLearningPanel").dataset.projectId===pid',arg=pid)
    page.locator('#styleLearningPanel > summary').click()
    assert not req.get(URL+f'/api/projects/{pid}/style-learning').json()['opt_in']
    page.locator('#learningSignalText').fill('未授权时保留的输入。')
    assert not page.locator('#learningAddSignal').is_enabled()
    page.locator('#learningAuthorized').check()
    page.locator('#learningEnable').focus();page.keyboard.press('Enter')
    page.wait_for_function('document.querySelector("#learningStatus").textContent.includes("已启用当前论文学习")')
    page.locator('#learningSignalKind').select_option('feedback')
    page.locator('#learningSignalText').fill('仅在证据充分时提出结论，先说明适用范围。')
    page.locator('#learningAddSignal').click()
    # No explicit distill request: the actual lifespan worker must complete it.
    page.locator('[data-select-rule]').first.wait_for(timeout=15000)
    state=req.get(URL+f'/api/projects/{pid}/style-learning').json()
    assert state['runs'][0]['status']=='completed'
    assert not state['profile_applies']
    assert '仅在证据充分时提出结论' not in req.get(URL+f'/api/projects/{pid}/ai-context').json()['preview']
    page.locator('[data-select-rule]').first.check()
    page.locator('#learningApprove').focus();page.keyboard.press('Enter')
    page.wait_for_function('document.querySelector("#learningEffective").textContent.includes("有效档案 v")')
    preview=req.get(URL+f'/api/projects/{pid}/ai-context').json()
    assert '仅在证据充分时提出结论' in preview['preview']
    assert any(s['kind']=='style_profile' for s in preview['sources'])
    page.locator('#learningPreview').locator('..').evaluate('(el)=>el.open=true')
    page.locator('#learningPreview').click()
    page.wait_for_function('document.querySelector("#learningContextPreview").textContent.includes("授权文风经验")')
    # Preserve input through rejected write and manual refresh, then rebase explicitly.
    state=req.get(URL+f'/api/projects/{pid}/style-learning').json()
    page.locator('#learningSignalText').fill('版本冲突后必须保留的输入。')
    changed=req.post(URL+f'/api/projects/{pid}/style-learning/pause',data={'request_id':'remote-pause','expected_version':state['version']})
    assert changed.ok
    page.locator('#learningAddSignal').click()
    page.wait_for_function('document.querySelector("#learningStatus").dataset.error==="true"')
    assert page.locator('#learningSignalText').input_value()=='版本冲突后必须保留的输入。'
    page.locator('#learningRefresh').click()
    page.wait_for_function('document.querySelector("#learningBadge").textContent==="已暂停"')
    assert '授权文风经验' not in page.locator('#learningContextPreview').inner_text()
    page.locator('#learningResume').click()
    page.wait_for_function('document.querySelector("#learningBadge").textContent==="已生效"')
    rule=page.locator('[data-edit-rule]').first
    rule.fill('编辑后的明确经验仍需批准。')
    page.locator('[data-save-rule]').first.click()
    page.wait_for_function('document.querySelectorAll("[data-select-rule]").length===1')
    assert '编辑后的明确经验' not in req.get(URL+f'/api/projects/{pid}/ai-context').json()['preview']
    page.locator('[data-select-rule]').first.check();page.locator('#learningApprove').click()
    page.wait_for_function('document.querySelector("#learningEffective").textContent.includes("编辑后的明确经验")')
    state=req.get(URL+f'/api/projects/{pid}/style-learning').json()
    original_profile=state['profiles'][-1]['id']
    page.locator('#learningHistory').locator('..').locator('..').evaluate('(el)=>el.open=true')
    page.locator('#learningHistory').select_option(original_profile);page.locator('#learningRollback').click()
    page.wait_for_function('document.querySelector("#learningEffective").textContent.includes("仅在证据充分时")')
    page.locator('#styleLearningPanel').scroll_into_view_if_needed()
    page.screenshot(path=str(OUTPUT/'REQ-020-desktop.png'),full_page=True)
    # Project switching restores only this paper's local sample draft.
    page.locator('#learningSignalText').fill('当前论文未提交的独立草稿。')
    page.locator(f'.project-item[data-id="{other}"]').click()
    page.wait_for_function('(pid)=>document.querySelector("#styleLearningPanel").dataset.projectId===pid',arg=other)
    assert page.locator('#learningSignalText').input_value()==''
    assert not req.get(URL+f'/api/projects/{other}/style-learning').json()['signals']
    page.locator(f'.project-item[data-id="{pid}"]').click()
    page.wait_for_function('(pid)=>document.querySelector("#styleLearningPanel").dataset.projectId===pid',arg=pid)
    assert page.locator('#learningSignalText').input_value()=='当前论文未提交的独立草稿。'
    page.set_viewport_size({'width':390,'height':844})
    page.locator('#styleLearningPanel').scroll_into_view_if_needed()
    assert page.locator('#styleLearningPanel').evaluate('(el)=>el.scrollWidth<=el.clientWidth+1')
    page.screenshot(path=str(OUTPUT/'REQ-020-mobile.png'),full_page=True)
    # Delete is an explicit control with a separately checked scope confirmation.
    page.locator('#learningDeleteConfirmed').locator('..').locator('..').evaluate('(el)=>el.open=true')
    page.locator('#learningDeleteConfirmed').check()
    page.locator('#learningDeleteAll').click()
    page.wait_for_function('document.querySelector("#learningBadge").textContent==="已关闭"')
    final=req.get(URL+f'/api/projects/{pid}/style-learning').json()
    assert not final['signals'] and not final['rules'] and not final['profile_applies']
    assert '授权文风经验' not in req.get(URL+f'/api/projects/{pid}/ai-context').json()['preview']
    assert req.get(URL+f'/api/projects/{pid}').json()['files']==original
    assert not errors,errors
    report={'requirement':'REQ-020 v4','human_verification':'unverified','time':datetime.now(timezone.utc).isoformat(),'passed':True,
        'automatic_worker':'real local daemon, no manual distill request','external_writing_model':'test double; no real provider or cost claim',
        'checks':['default off','explicit consent','automatic candidate','approval boundary','actual context provenance','conflict preserves input','edit and reapprove','rollback','paper isolation and draft restore','keyboard activation','390px panel width','delete and manuscript unchanged'],
        'browser_errors':errors,'source_sha256':{str(f.relative_to(ROOT)):hashlib.sha256(f.read_bytes()).hexdigest() for f in [ROOT/'static/style-learning-ui.js',ROOT/'paperpilot/services/style_learning.py',ROOT/'paperpilot/services/context.py']}}
    (OUTPUT/'REQ-020-BROWSER.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'checks':len(report['checks']),'report':str(OUTPUT/'REQ-020-BROWSER.json')},ensure_ascii=False))
    browser.close()
