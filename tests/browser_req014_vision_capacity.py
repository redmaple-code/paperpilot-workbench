"""Missing-capacity recovery to an actual local HTTP vision call, isolated paper data."""
from pathlib import Path
import base64
import hashlib
import json
import sys
from datetime import datetime, timezone

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_req014_provider_runtime import local_provider
from tests.test_req022_assets import png

OUT = ROOT / '.project-to-act/docs/project-development/evidence/full-product-20261101'
URL = 'http://127.0.0.1:8780'

with local_provider() as (provider_url, calls), sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    req = context.request
    pid = req.post(URL + '/api/projects', data={'name': '视觉容量修复浏览器论文'}).json()['project']['id']
    asset = req.post(URL + f'/api/projects/{pid}/drawing/assets/import', data={'filename': 'fixture.png',
        'content_base64': base64.b64encode(png()).decode(), 'request_id': 'asset'}).json()['version']['id']
    created = req.post(URL + '/api/ai/profiles', data={'name': '缺少视觉容量的本地模型', 'base_url': provider_url + '/v1',
        'auth_mode': 'none', 'models': ['test-model'], 'capabilities': {'test-model': {'vision_analysis': True, 'price_per_check': 0}},
        'model_limits': {'test-model': {'input_tokens': 100000, 'output_tokens': 10000, 'source': 'isolated local model fixture'}}, 'request_id': 'profile'})
    assert created.ok, created.text()
    profile = created.json()
    tested = req.post(URL + f"/api/ai/profiles/{profile['id']}/test", data={'request_id': 'test', 'expected_version': 1,
        'model_id': 'test-model', 'mode': 'vision_analysis', 'consent_to_send': True, 'possible_cost_confirmed': True})
    assert tested.ok
    # Background test executes asynchronously in uvicorn; poll the observable API.
    import time
    for _ in range(100):
        if req.get(URL + f"/api/ai/profiles/{profile['id']}/tests").json()[0]['status'] == 'ready':
            break
        time.sleep(.1)
    profile = req.put(URL + f"/api/ai/profiles/{profile['id']}", data={'base_version': 1, 'changes': {'status': 'enabled'}}).json()
    revision = req.get(URL + f'/api/projects/{pid}').json()['project']['project_revision']
    bound = req.put(URL + f'/api/projects/{pid}/drawing/provider-binding', data={'profile_id': profile['id'],
        'config_revision': profile['current_version'], 'model_id': 'test-model', 'purpose': 'vision', 'expected_project_revision': revision})
    assert bound.ok, bound.text()
    calls.clear()
    before = req.get(URL + f'/api/projects/{pid}').json()['files']
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(URL)
    page.wait_for_load_state('networkidle')
    page.locator(f'.project-item[data-id="{pid}"]').click()
    page.locator('[data-tab="visual"]').click()
    page.locator('#refreshVisualReports').click()
    page.wait_for_function('document.querySelector("#evidenceAsset").options.length===2')
    page.get_by_text('可选云端视觉核验', exact=True).click()
    page.locator('#previewVisualCapacity').focus()
    page.keyboard.press('Enter')
    page.wait_for_function('document.querySelector("#visualCapacityPreview").textContent.includes("视觉图片容量未知")')
    assert calls == []
    page.locator('#repairVisualCapacity').click()
    page.wait_for_function('document.querySelector("#providerSettings").open && document.querySelector("#providerModel").value==="test-model"')
    assert page.locator('#providerBindingPurpose').input_value() == 'vision'
    assert page.locator('#providerTestMode').input_value() == 'vision_analysis'
    page.locator('#providerVisionTokens').fill('2000')
    page.locator('#providerVisionWidth').fill('1600')
    page.locator('#providerVisionHeight').fill('1600')
    page.locator('#providerVisionSource').fill('隔离本地HTTP fixture，high detail上界；非真实模型容量')
    page.locator('#providerSave').click()
    page.wait_for_function('document.querySelector("#providerGuidance").textContent.includes("配置已保存")')
    assert len(calls) == 0
    page.locator('#providerTestConsent').check()
    page.locator('#providerTestCost').check()
    page.locator('#providerTest').click()
    page.wait_for_function('document.querySelector("#providerTestResults").textContent.includes("生成测试通过")')
    page.locator('#providerEnable').click()
    page.wait_for_function('document.querySelector("#providerGuidance").textContent.includes("此版本已启用")')
    page.locator('#providerBind').click()
    page.wait_for_function('document.querySelector("#providerGuidance").textContent.includes("当前论文已明确绑定")')
    page.locator('#previewVisualCapacity').click()
    page.wait_for_function('document.querySelector("#visualCapacityPreview").textContent.includes("1张图")')
    assert '未实测' in page.locator('#visualCapacityPreview').inner_text()
    assert len(calls) == 1  # Only the explicitly authorized protocol test.
    page.get_by_text('参考证据与定位（可选）', exact=True).click()
    page.locator('#evidenceCloud').check()
    page.locator('#evidenceSourceAsset').select_option(asset)
    assert not page.locator('#evidenceCloud').is_checked()
    page.locator('#previewVisualCapacity').click()
    page.wait_for_function('document.querySelector("#visualCapacityPreview").textContent.includes("2张图")')
    assert len(calls) == 1
    page.locator('#evidenceCloud').check()
    page.locator('#runVisualEvidence').click()
    page.wait_for_function('document.querySelector("#visualEvidenceStatus").textContent.includes("报告已生成")')
    assert len(calls) == 2 and len(calls[-1]['body']['messages'][0]['content']) == 3
    assert req.get(URL + f'/api/projects/{pid}').json()['files'] == before
    page.locator('.visual-evidence-studio').screenshot(path=str(OUT / 'REQ-014-VISION-desktop.png'))
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.locator('.visual-evidence-studio').screenshot(path=str(OUT / 'REQ-014-VISION-mobile.png'))
    # A total-capacity change becomes a new draft; test/enable/bind then preview.
    latest = next(v for v in req.get(URL + '/api/ai/profiles').json() if v['id'] == profile['id'])
    limits = latest['model_limits']
    limits['test-model']['input_tokens'] = 1000
    changed = req.put(URL + f"/api/ai/profiles/{profile['id']}", data={'base_version': latest['current_version'], 'changes': {'model_limits': limits}}).json()
    version = changed['current_version']
    req.post(URL + f"/api/ai/profiles/{profile['id']}/test", data={'request_id': 'small-test', 'expected_version': version,
        'model_id': 'test-model', 'mode': 'vision_analysis', 'consent_to_send': True, 'possible_cost_confirmed': True})
    for _ in range(100):
        if req.get(URL + f"/api/ai/profiles/{profile['id']}/tests").json()[0]['status'] == 'ready':
            break
        time.sleep(.1)
    changed = req.put(URL + f"/api/ai/profiles/{profile['id']}", data={'base_version': version, 'changes': {'status': 'enabled'}}).json()
    revision = req.get(URL + f'/api/projects/{pid}').json()['project']['project_revision']
    assert req.put(URL + f'/api/projects/{pid}/drawing/provider-binding', data={'profile_id': profile['id'],
        'config_revision': changed['current_version'], 'model_id': 'test-model', 'purpose': 'vision', 'expected_project_revision': revision}).ok
    baseline = len(calls)
    page.locator('#previewVisualCapacity').click()
    page.wait_for_function('document.querySelector("#visualCapacityPreview").textContent.includes("超过模型上限")')
    assert len(calls) == baseline
    assert page.locator('#evidenceSourceAsset').input_value() == asset
    assert not errors, errors
    report = {'requirement': 'REQ-014 v16 / AC-014-20', 'time': datetime.now(timezone.utc).isoformat(), 'passed': True,
        'model': 'actual local HTTP protocol fixture; no cloud quality/billing claim',
        'checks': ['unknown capacity zero egress', 'keyboard preview', 'repair opens exact model and vision purpose',
            'save/test/enable/bind recovery', 'one-image capacity and unverified source', 'reference change clears consent',
            'two-image preview zero egress', 'explicit two-image HTTP send', 'manuscript unchanged', '390px',
            'capacity exceeded UI gives repair and zero egress', 'input retained'], 'browser_errors': errors,
        'source_sha256': {f: hashlib.sha256((ROOT / f).read_bytes()).hexdigest() for f in ['static/visual-evidence-ui.js',
            'static/provider-settings-ui.js', 'static/index.html', 'paperpilot/services/vision_capacity.py', 'paperpilot/services/visual_evidence.py']}}
    (OUT / 'REQ-014-VISION-BROWSER.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': True, 'checks': len(report['checks'])}))
    browser.close()
