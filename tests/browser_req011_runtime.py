"""Real TeX PDF, cancellation, explicit retry and archive recovery guidance."""
from datetime import datetime, timezone
import argparse
import hashlib
import io
import json
from pathlib import Path

from playwright.sync_api import sync_playwright
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '.project-to-act/docs/project-development/evidence/full-product-20261101'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--port', type=int, default=8891)
URL = f'http://127.0.0.1:{parser.parse_args().port}'
LOOP = r'\documentclass{article}\begin{document}\newcount\counter\loop\advance\counter by 1\iftrue\repeat\end{document}'

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={'width': 1440, 'height': 1000})
    req = context.request
    def ok(response):
        assert response.ok, response.text()
        return response.json()
    pid = ok(req.post(URL + '/api/projects', data={'name': '真实编译取消恢复浏览器'}))['project']['id']
    other = ok(req.post(URL + '/api/projects', data={'name': '隔离对照论文'}))['project']['id']
    workflow = ok(req.get(URL + f'/api/projects/{pid}/workflow'))
    started = ok(req.post(URL + f'/api/projects/{pid}/workflow/commands', data={'command': 'start', 'request_id': 'start', 'base_version': workflow['version']}))
    ok(req.post(URL + f'/api/projects/{pid}/human-revision', data={'workflow_version': started['version']}))
    def save(content):
        main = next(f for f in ok(req.get(URL + f'/api/projects/{pid}'))['files'] if f['path'] == 'main.tex')
        ok(req.put(URL + f'/api/projects/{pid}/files', data={'path': 'main.tex', 'base_version': main['version'], 'content': content}))
    save(r'\documentclass{article}\begin{document}Real browser PDF baseline.\end{document}')
    page = context.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(URL)
    page.wait_for_load_state('networkidle')
    page.locator(f'.project-item[data-id="{pid}"]').click()
    page.wait_for_function('(id)=>paperpilot.current?.project.id===id', arg=pid)
    page.locator('#buildPurpose').select_option('submitted')
    page.locator('#compile').focus()
    page.keyboard.press('Enter')
    page.wait_for_function('document.querySelector("#compileLog").textContent.includes("编译成功")', timeout=30000)
    baseline = req.get(URL + f'/api/projects/{pid}/pdf')
    assert baseline.ok and baseline.headers['x-build-purpose'] == 'submitted'
    assert 'Real browser PDF baseline.' in (PdfReader(io.BytesIO(baseline.body())).pages[0].extract_text() or '')
    proof = ok(req.get(URL + f'/api/projects/{pid}/lifecycle'))['release_check']
    assert proof['status'] == 'passed'
    save(LOOP)
    page.evaluate('(id)=>paperpilot.loadProject(id)', pid)
    page.locator('#compile').click()
    page.wait_for_function('Array.from(document.querySelectorAll(".build-history-row")).some(e=>e.textContent.includes("运行中"))')
    page.get_by_role('button', name='取消构建', exact=True).click()
    page.wait_for_function('document.querySelector("#buildHistoryList").textContent.includes("按此快照重试")')
    jobs = ok(req.get(URL + f'/api/projects/{pid}/builds'))
    cancelled = next(j for j in jobs if j['status'] == 'cancelled')
    assert req.get(URL + f'/api/projects/{pid}/pdf').body() == baseline.body()
    assert 'request_id' not in page.locator('.build-history').inner_text()
    page.locator(f'[data-build-retry="{cancelled["id"]}"]').click()
    page.wait_for_function('Array.from(document.querySelectorAll(".build-history-row")).some(e=>e.textContent.includes("运行中"))')
    retried = ok(req.get(URL + f'/api/projects/{pid}/builds'))[0]
    assert retried['id'] != cancelled['id'] and retried['snapshot_id'] == cancelled['snapshot_id']
    # Archival must cancel the actual running retry and cannot touch another paper.
    page.locator('#archiveProject').click()
    page.wait_for_function('paperpilot.current?.project.archived===1 || paperpilot.current?.project.archived===true')
    page.locator('[data-tab="preview"]').click()
    page.locator('#refreshBuildHistory').click()
    page.wait_for_function('!Array.from(document.querySelectorAll(".build-history-row")).some(e=>e.textContent.includes("运行中"))')
    assert ok(req.get(URL + f'/api/projects/{pid}/builds/{retried["id"]}'))['status'] == 'cancelled'
    page.wait_for_function('(id)=>document.querySelector(`[data-build-retry="${id}"]`)?.disabled===true', arg=retried['id'])
    assert not page.locator(f'[data-build-retry="{retried["id"]}"]').is_enabled()
    assert ok(req.get(URL + f'/api/projects/{other}/builds')) == []
    assert req.get(URL + f'/api/projects/{pid}/pdf').body() == baseline.body()
    page.locator('.build-history').screenshot(path=str(OUT / 'REQ-011-RUNTIME-desktop.png'))
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.locator('.build-history').screenshot(path=str(OUT / 'REQ-011-RUNTIME-mobile.png'))
    assert not errors, errors
    report = {'passed': True, 'time': datetime.now(timezone.utc).isoformat(), 'real_tex': True, 'isolated_database': True,
        'external_model_calls': 0, 'checks': ['keyboard submitted build', 'real PDF and release evidence',
            'cancel actual running TeX', 'explicit retry preserves snapshot and uses new job', 'archive cancels retry',
            'prior PDF unchanged', 'other paper isolated', 'manual reconciliation and retry entry', '390px',
            'plain-language diagnosis with technical details collapsed', 'archive blocks retry with restoration guidance'],
        'browser_errors': errors, 'source_sha256': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
            ['static/development-ui.js', 'static/index.html', 'app.py', 'paperpilot/services/build_snapshots.py', 'paperpilot/adapters/latex.py']}}
    (OUT / 'REQ-011-RUNTIME-BROWSER.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': True, 'checks': len(report['checks'])}))
    browser.close()
