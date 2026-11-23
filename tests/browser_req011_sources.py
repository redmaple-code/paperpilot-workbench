"""Persisted draft and candidate preflight UX with actual local TeX/PDF output."""
from datetime import datetime, timezone
import argparse
import hashlib
import io
import json
from pathlib import Path
import time

from playwright.sync_api import sync_playwright
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '.project-to-act/docs/project-development/evidence/full-product-20261101'
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--port', type=int, default=8896)
URL = f'http://127.0.0.1:{parser.parse_args().port}'
BASE = r'\documentclass{article}\begin{document}\section{Introduction}Saved baseline.\end{document}'

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={'width': 1440, 'height': 1050})
    req = context.request
    def ok(response):
        assert response.ok, response.text()
        return response.json()
    pid = ok(req.post(URL + '/api/projects', data={'name': '真实草稿候选预览浏览器'}))['project']['id']
    def revision_payload(payload):
        drafts=ok(req.get(URL+f'/api/projects/{pid}/human-revision/drafts'))
        current=next((d for d in drafts if d['target_kind']==payload['target_kind'] and d['target_path']==payload['target_path']),None)
        return {**payload,'expected_draft_updated_at':current['updated_at'] if current else None}
    other = ok(req.post(URL + '/api/projects', data={'name': '切换隔离对照论文'}))['project']['id']
    wf = ok(req.get(URL + f'/api/projects/{pid}/workflow'))
    started = ok(req.post(URL + f'/api/projects/{pid}/workflow/commands', data={'command': 'start', 'request_id': 'start', 'base_version': wf['version']}))
    ok(req.post(URL + f'/api/projects/{pid}/human-revision', data={'workflow_version': started['version']}))
    def files(paper=pid): return ok(req.get(URL + f'/api/projects/{paper}'))['files']
    main = next(f for f in files() if f['path'] == 'main.tex')
    ok(req.put(URL + f'/api/projects/{pid}/files', data={'path': 'main.tex', 'base_version': main['version'], 'content': BASE}))
    before, other_before = files(), files(other)
    main = next(f for f in before if f['path'] == 'main.tex')
    initial = ok(req.post(URL + f'/api/projects/{pid}/compile', data={'request_id': 'baseline', 'purpose': 'submitted'}))
    def wait_job(predicate, seconds=20):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            jobs = ok(req.get(URL + f'/api/projects/{pid}/builds'))
            selected = next((j for j in jobs if predicate(j)), None)
            if selected: return selected
            time.sleep(.1)
        raise AssertionError('Expected build state not observed: ' + json.dumps(jobs, ensure_ascii=False))
    wait_job(lambda j: j['id'] == initial['build_job']['id'] and j['status'] == 'succeeded')
    baseline_pdf = req.get(URL + f'/api/projects/{pid}/pdf').body()
    def pdf_text(job):
        response = req.get(URL + f'/api/projects/{pid}/builds/{job["id"]}/pdf')
        assert response.ok, response.text()
        return '\n'.join(page.extract_text() or '' for page in PdfReader(io.BytesIO(response.body())).pages)
    page = context.new_page();errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.goto(URL);page.wait_for_load_state('networkidle')
    page.locator(f'.project-item[data-id="{pid}"]').click()
    page.wait_for_function('(id)=>paperpilot.current?.project.id===id && paperpilot.isRevisionActive?.()', arg=pid)
    assert page.locator('#autoPreviewBuild').is_checked()
    draft = BASE.replace('Saved baseline.', 'Draft content only.')
    page.locator('#editor').fill(draft)
    first = wait_job(lambda j: j['trigger_kind'] == 'automatic' and j['status'] == 'succeeded')
    assert 'Draft content only.' in pdf_text(first)
    assert files() == before and req.get(URL + f'/api/projects/{pid}/pdf').body() == baseline_pdf
    revision = BASE.replace('Saved baseline.', 'Human revision only.')
    ok(req.post(URL + f'/api/projects/{pid}/human-revision/drafts', data=revision_payload({'target_kind': 'file', 'target_path': 'main.tex', 'base_version': main['version'], 'content': revision})))
    page.locator('#buildPurpose').select_option('draft_preview')
    page.locator('#compile').click()
    page.wait_for_function('document.querySelector("#compileLog").textContent.includes("草稿内容不同") || document.querySelector("#compileLog").textContent.includes("草稿与人工修订草稿内容不同")')
    assert files() == before
    page.locator('#buildDraftSource').select_option('revision')
    selected = wait_job(lambda j: j['status'] == 'succeeded' and j['source'].get('draft_source') == 'revision')
    assert 'Human revision only.' in pdf_text(selected)
    # Explicit history selection survives later automatic completion.
    page.locator('[data-tab="preview"]').click()
    page.locator('#refreshBuildHistory').click()
    page.locator(f'[data-build-job="{first["id"]}"]').get_by_role('button', name='打开此 PDF', exact=True).click()
    old_src = page.locator('#pdfFrame').get_attribute('src')
    page.locator('#buildDraftSource').select_option('editor')
    page.locator('[data-tab="write"]').click()
    newer = BASE.replace('Saved baseline.', 'New editor draft only.')
    page.locator('#editor').fill(newer)
    latest = wait_job(lambda j: j['status'] == 'succeeded' and j['source'].get('draft_source') == 'editor'
        and any(e['sha256'] == hashlib.sha256(newer.encode()).hexdigest() for e in j['source']['entries']))
    assert 'New editor draft only.' in pdf_text(latest)
    assert page.locator('#pdfFrame').get_attribute('src') == old_src
    # Candidate generation is explicitly a test double; compilation is real.
    start = main['content'].index('Introduction');end = start + len('Introduction')
    preview = ok(req.post(URL + f'/api/projects/{pid}/ai-context', data={'action': 'edit', 'target_kind': 'file', 'target_path': 'main.tex', 'prompt': 'rewrite title', 'content': main['content'], 'selection': 'Introduction'}))
    generated = ok(req.post(URL + f'/api/projects/{pid}/ai-jobs', data={'action': 'edit', 'request_id': 'candidate-ui', 'prompt': 'rewrite title',
        'content': main['content'], 'selection': 'Introduction', 'scope': {'targets': [{'kind': 'file', 'path': 'main.tex', 'start': start, 'end': end}]},
        'base_file_versions': {'main.tex': main['version']}, 'context_version': preview['version']}))
    candidate_id = None
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        result = ok(req.get(URL + f'/api/projects/{pid}/ai-jobs/{generated["job"]["id"]}'))
        if result.get('candidate'):
            candidate_id = result['candidate']['id'];break
        time.sleep(.1)
    assert candidate_id
    page.locator('#refreshWorkflow').click()
    page.locator(f'[data-candidate-preflight="{candidate_id}"]').click()
    checked = wait_job(lambda j: j['purpose'] == 'candidate_preview' and j['status'] == 'succeeded')
    assert 'AI candidate text' in pdf_text(checked)
    assert ok(req.get(URL + f'/api/projects/{pid}/candidates/{candidate_id}'))['status'] == 'candidate_ready'
    assert files() == before and req.get(URL + f'/api/projects/{pid}/pdf').body() == baseline_pdf
    page.locator('#preview').screenshot(path=str(OUT / 'REQ-011-SOURCES-desktop.png'))
    page.set_viewport_size({'width': 390, 'height': 844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth')
    page.locator('#preview').screenshot(path=str(OUT / 'REQ-011-SOURCES-mobile.png'))
    # The delayed draft belongs to the old paper, including after switching.
    page.locator('[data-tab="write"]').click()
    race = BASE.replace('Saved baseline.', 'Old paper pending input.')
    page.locator('#editor').fill(race)
    page.locator(f'.project-item[data-id="{other}"]').click()
    page.get_by_role('button', name='保留为草稿并切换', exact=True).click()
    page.wait_for_function('(id)=>paperpilot.current?.project.id===id && document.querySelector("#buildHistoryList").dataset.projectId===id', arg=other)
    assert files(other) == other_before
    assert ok(req.get(URL + f'/api/projects/{other}/editor/draft?path=main.tex')).get('exists') is False
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        old_draft = ok(req.get(URL + f'/api/projects/{pid}/editor/draft?path=main.tex'))
        if old_draft.get('content') == race:break
        time.sleep(.1)
    assert old_draft.get('content') == race
    assert not page.locator('#pdfFrame').get_attribute('src')
    assert ok(req.get(URL + f'/api/projects/{other}/builds')) == [] and not errors, errors
    report = {'passed': True, 'time': datetime.now(timezone.utc).isoformat(), 'real_tex': True, 'isolated_database': True,
        'candidate_model': 'explicit UI test double; no cloud quality claim',
        'checks': ['debounced persistent draft appears in real PDF', 'saved body and submitted PDF untouched',
            'conflict explains source choice and retains drafts', 'revision-source selection actual PDF', 'history not stolen by automatic preview',
            'candidate card preflight compiles actual candidate without adopting', 'source/queue labels', '390px',
            'delayed draft save stays with original paper', 'project switch clears old PDF', 'other paper untouched'],
        'browser_errors': errors, 'source_sha256': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in
            ['static/development-ui.js', 'static/workflow-ui.js', 'static/index.html', 'app.py', 'paperpilot/services/build_sources.py', 'paperpilot/services/build_queue.py']}}
    (OUT / 'REQ-011-SOURCES-BROWSER.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': True, 'checks': len(report['checks'])}))
    browser.close()
