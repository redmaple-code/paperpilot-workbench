"""Real TeX historical restore through the reviewed browser path."""
import argparse
from datetime import datetime, timezone
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
parser.add_argument('--port', type=int, required=True)
URL = f'http://127.0.0.1:{parser.parse_args().port}'
BASE = r'\documentclass{article}\begin{document}Historical source restored.\end{document}'
NEW = BASE.replace('Historical source restored.', 'Current text before restore.')

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    context = browser.new_context(viewport={'width': 1440, 'height': 1050})
    req = context.request
    def ok(response):
        assert response.ok, response.text()
        return response.json()
    pid = ok(req.post(URL+'/api/projects', data={'name': '历史恢复实际浏览器'}))['project']['id']
    def revision_payload(payload):
        drafts=ok(req.get(URL+f'/api/projects/{pid}/human-revision/drafts'))
        current=next((d for d in drafts if d['target_kind']==payload['target_kind'] and d['target_path']==payload['target_path']),None)
        return {**payload,'expected_draft_updated_at':current['updated_at'] if current else None}
    other = ok(req.post(URL+'/api/projects', data={'name': '恢复跨论文隔离对照'}))['project']['id']
    wf = ok(req.get(URL+f'/api/projects/{pid}/workflow'))
    started = ok(req.post(URL+f'/api/projects/{pid}/workflow/commands', data={'command': 'start', 'request_id': 'start', 'base_version': wf['version']}))
    ok(req.post(URL+f'/api/projects/{pid}/human-revision', data={'workflow_version': started['version']}))
    def files(paper=pid): return ok(req.get(URL+f'/api/projects/{paper}'))['files']
    def save(content):
        f = next(f for f in files() if f['path'] == 'main.tex')
        return ok(req.put(URL+f'/api/projects/{pid}/files', data={'path': 'main.tex', 'base_version': f['version'], 'content': content}))
    def wait_job(job_id):
        until = time.monotonic()+30
        while time.monotonic()<until:
            job = ok(req.get(URL+f'/api/projects/{pid}/builds/{job_id}'))
            if job['status'] not in {'queued', 'running'}:
                assert job['status'] == 'succeeded', job
                return job
            time.sleep(.1)
        raise AssertionError('Build did not finish')
    save(BASE)
    built = ok(req.post(URL+f'/api/projects/{pid}/compile', data={'request_id': 'history', 'purpose': 'submitted'}))
    job_id = built['build_job']['id'];wait_job(job_id)
    historic_style=ok(req.get(URL+f'/api/projects/{pid}/settings'))['effective']['style']
    revision=ok(req.get(URL+f'/api/projects/{pid}'))['project']['project_revision']
    ok(req.put(URL+f'/api/projects/{pid}/settings/style',data={'value':'当前风格','expected_project_revision':revision,'request_id':'browser-style'}))
    save(NEW)
    ok(req.put(URL+f'/api/projects/{pid}/files',data={'path':'extra.tex','base_version':0,'content':'CURRENT ONLY CONTENT'}))
    before = files();other_before = files(other)
    page = context.new_page();errors = []
    page.on('pageerror', lambda e: errors.append(str(e)))
    page.goto(URL);page.wait_for_load_state('networkidle')
    page.locator(f'.project-item[data-id="{pid}"]').click()
    page.wait_for_function('(id)=>paperpilot.current?.project.id===id && paperpilot.isRevisionActive?.()', arg=pid)
    page.locator('#autoPreviewBuild').uncheck()
    page.locator('#fileSelect').select_option('main.tex')
    page.locator('[data-tab="preview"]').click()
    page.locator('#refreshBuildHistory').click()
    def open_restore():
        page.locator(f'[data-build-job="{job_id}"]').get_by_role('button', name='载入为修订草稿', exact=True).click()
        page.locator('#buildRestorePanel').wait_for(state='visible')
    open_restore()
    panel = page.locator('#buildRestorePanel')
    panel.locator('details').first.locator('summary').click()
    assert 'Current text before restore.' in panel.inner_text() and 'Historical source restored.' in panel.inner_text()
    assert files() == before
    panel.get_by_role('button', name='取消恢复', exact=True).click()
    assert files() == before and ok(req.get(URL+f'/api/projects/{pid}/human-revision/drafts')) == []
    # Existing unsaved revision draft produces guidance; no silent overwrite.
    main = next(f for f in files() if f['path'] == 'main.tex')
    draft = NEW+'\nKEEP UNCOMMITTED DRAFT'
    ok(req.post(URL+f'/api/projects/{pid}/human-revision/drafts', data=revision_payload({'target_kind': 'file', 'target_path': 'main.tex', 'base_version': main['version'], 'content': draft})))
    open_restore()
    assert panel.locator('[data-restore-prepare]').is_disabled()
    assert '草稿冲突' in panel.inner_text()
    assert ok(req.get(URL+f'/api/projects/{pid}/human-revision/drafts'))[0]['content'] == draft
    panel.get_by_role('button', name='取消恢复', exact=True).click()
    # Explicitly save that current draft via its owner; then update it to saved
    # content through the existing draft API to isolate the restore happy path.
    ok(req.post(URL+f'/api/projects/{pid}/human-revision/drafts', data=revision_payload({'target_kind': 'file', 'target_path': 'main.tex', 'base_version': main['version'], 'content': NEW})))
    page.locator('[data-tab="write"]').click()
    page.locator('#editor').fill(BASE)  # Same as historical source, unsubmitted.
    page.locator('[data-tab="preview"]').click()
    assert page.locator('#editor').input_value()==BASE, 'Tab switch must retain live input'
    open_restore()
    panel.locator('details').first.locator('summary').click()
    panel.get_by_text('查看完整资源、配置及当前独有文件差异', exact=True).click()
    panel.screenshot(path=str(OUT/'REQ-011-RESTORE-SETTINGS-desktop.png'))
    page.set_viewport_size({'width': 390, 'height': 844})
    panel.screenshot(path=str(OUT/'REQ-011-RESTORE-SETTINGS-mobile.png'))
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+2')
    extra=panel.locator('[data-restore-remove-path][value="extra.tex"]')
    assert not extra.is_checked()
    extra.check();style=panel.locator('[data-restore-setting][value="style"]')
    assert not style.is_checked() and style.is_enabled();style.check()
    assert panel.locator('[data-restore-prepare]').is_disabled()
    with page.expect_response(lambda response: '/restore-preview' in response.url and response.request.method=='POST'):
        panel.get_by_role('button',name='更新所选差异').click()
    page.wait_for_function('()=>!document.querySelector("[data-restore-prepare]")?.disabled')
    assert panel.locator('[data-restore-remove-path][value="extra.tex"]').is_checked()
    assert panel.locator('[data-restore-setting][value="style"]').is_checked()
    assert panel.locator('[data-restore-prepare]').is_enabled(), panel.inner_text()
    panel.locator('[data-restore-prepare]').click()
    panel.locator('[data-restore-commit]').wait_for(state='visible')
    assert files() == before
    assert ok(req.get(URL+f'/api/projects/{pid}/human-revision/drafts'))
    panel.locator('[data-restore-commit]').click()
    page.wait_for_function('document.querySelector("[data-restore-status]").textContent.includes("全部所选文件已保存")')
    latest = next(j for j in ok(req.get(URL+f'/api/projects/{pid}/builds')) if j['id'] != job_id and j['purpose']=='submitted')
    result = wait_job(latest['id'])
    assert ok(req.get(URL+f'/api/projects/{pid}/settings'))['effective']['style']==historic_style
    assert all(f['path']!='extra.tex' for f in files())
    restored = next(f for f in files() if f['path']=='main.tex')
    assert restored['content']==BASE and restored['version']==main['version']+1
    retained = ok(req.get(URL+f'/api/projects/{pid}/editor/draft?path=main.tex'))
    assert retained['content']==BASE and not retained['stale']
    text = '\n'.join(p.extract_text() or '' for p in PdfReader(io.BytesIO(req.get(URL+f'/api/projects/{pid}/builds/{result["id"]}/pdf').body())).pages)
    assert 'Historical source restored.' in text and 'Current text before restore.' not in text
    assert files(other)==other_before
    page.locator(f'.project-item[data-id="{other}"]').click()
    page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=other)
    panel.wait_for(state='hidden')
    assert panel.is_hidden()
    checks=['complete file/resource/configuration diff before writes', 'cancel leaves body and drafts unchanged', 'unsubmitted draft conflict explained and preserved', '390px no horizontal overflow', 'prepare changes only revision drafts', 'explicit confirmation creates new source versions', 'automatic explicit submitted rebuild produces actual restored PDF', 'other paper unchanged and switch hides old restore controls', 'repeated identical editor persistence does not invalidate preview/commit and preserved draft rebased','current-only file defaults to keep and explicit selection removes on confirmation','explicit historical style selection applies with file restore and rebuild']
    assert not errors, errors
    source_files=['app.py','paperpilot/services/build_restores.py','paperpilot/services/human_revision.py','paperpilot/services/build_snapshots.py','paperpilot/domain/schemas.py','static/development-ui.js','static/workflow-ui.js','static/index.html','tests/browser_req011_restore_settings.py']
    (OUT/'REQ-011-RESTORE-SETTINGS-BROWSER.json').write_text(json.dumps({'passed':True,'time':datetime.now(timezone.utc).isoformat(),'real_tex':True,'isolated_database':True,'checks':checks,'browser_errors':errors,'source_sha256':{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in source_files}},ensure_ascii=False,indent=2),encoding='utf-8')
    browser.close()
    print(json.dumps({'passed':True,'checks':len(checks)}))
