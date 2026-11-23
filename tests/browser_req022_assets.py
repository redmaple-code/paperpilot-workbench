"""Real browser asset workflow against tests/ui_server.py's isolated database."""
from __future__ import annotations

import io
import json
from pathlib import Path

from PIL import Image
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / '.project-to-act/docs/project-development/evidence/full-product-20261101'
OUTPUT.mkdir(parents=True, exist_ok=True)


def image(color):
    stream = io.BytesIO()
    Image.new('RGB', (320, 180), color).save(stream, 'PNG')
    return stream.getvalue()


with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={'width': 1440, 'height': 1100})
    project = context.request.post('http://127.0.0.1:8772/api/projects', data={'name': '图像工作台验收论文', 'domain': '材料科学', 'style': '严谨'}).json()
    pid = project['project']['id']
    workflow = context.request.get(f'http://127.0.0.1:8772/api/projects/{pid}/workflow').json()
    started = context.request.post(f'http://127.0.0.1:8772/api/projects/{pid}/workflow/commands', data={'command': 'start', 'request_id': 'browser-start', 'base_version': workflow['version']}).json()
    assert context.request.post(f'http://127.0.0.1:8772/api/projects/{pid}/human-revision', data={'workflow_version': started['version']}).ok
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto('http://127.0.0.1:8772')
    page.wait_for_load_state('networkidle')
    page.locator('[data-tab="visual"]').wait_for()
    page.locator('[data-tab="visual"]').click()
    page.locator('#visualImportForm').wait_for(state='visible')
    page.locator('#visualAssetFile').set_input_files({'name': 'apparatus.png', 'mimeType': 'image/png', 'buffer': image('red')})
    page.locator('#visualAssetTitle').fill('实验装置示意图')
    page.locator('#importVisualAsset').click()
    page.locator('.visual-asset-item').wait_for()
    page.wait_for_function('document.querySelector("#visualAssetStatus").textContent.includes("已导入")')
    page.locator('#visualMainPreview').wait_for()
    page.wait_for_function('document.querySelector("#visualMainPreview").naturalWidth > 0')
    first_preview = page.locator('#visualMainPreview').get_attribute('src')
    page.locator('#visualInsertCaption').fill('受管图像的固定版本')
    page.locator('#insertVisualAsset').click()
    page.wait_for_function('document.querySelector("#visualAssetStatus").textContent.includes("已经插入")')
    assert '\\includegraphics' in page.locator('#editor').input_value()
    page.locator('#visualReplacementFile').set_input_files({'name': 'apparatus-blue.png', 'mimeType': 'image/png', 'buffer': image('blue')})
    page.wait_for_function('document.querySelector("#visualAssetStatus").textContent.includes("新版本已保存")')
    assert page.locator('#visualVersionSelect option').count() == 2
    first_id = first_preview.split('/versions/')[1].split('/')[0]
    page.locator('#visualVersionSelect').select_option(first_id)
    assert page.locator('#visualMainPreview').get_attribute('src') == first_preview
    assert '历史版本' in page.locator('.visual-history-note').inner_text()
    page.screenshot(path=str(OUTPUT / 'REQ-022-desktop.png'), full_page=True)
    page.locator('#archiveVisualAsset').click()
    page.wait_for_function('document.querySelector("#visualAssetStatus").textContent.includes("已归档")')
    assert page.locator('.visual-asset-item').count() == 0
    page.locator('#visualShowArchived').check()
    page.locator('.visual-asset-item').wait_for()
    page.locator('#archiveVisualAsset').click()
    page.wait_for_function('document.querySelector("#visualAssetStatus").textContent.includes("已恢复")')
    page.set_viewport_size({'width': 390, 'height': 844})
    page.locator('[data-tab="visual"]').click()
    page.locator('#visualImportForm').scroll_into_view_if_needed()
    assert page.locator('#visual').bounding_box()['width'] >= 300, 'workspace collapsed behind a desktop sidebar'
    page.locator('#visual').screenshot(path=str(OUTPUT / 'REQ-022-mobile.png'))
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1'), 'mobile overflow'
    assert not errors, errors
    report = {'scope': 'real PNG upload/preview, version replacement/history, guarded editor insertion, archive/restore, desktop and 390px UI',
              'passed': True, 'runtime_errors': errors, 'screenshots': ['REQ-022-desktop.png', 'REQ-022-mobile.png'],
              'not_covered': ['real TeX engine output', 'PDF upload browser interaction', 'screen-reader user validation', 'REQ-023/024 generation and verification UI']}
    (OUTPUT / 'REQ-022-BROWSER.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))
    browser.close()
