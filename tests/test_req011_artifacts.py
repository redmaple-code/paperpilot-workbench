"""Execution success, current-PDF promotion and release evidence are distinct."""
from pathlib import Path
import threading

import pytest

import app
from tests.test_req011_builds import _project


def compiler(value=b'controlled-artifact'):
    def compile(_files, _project, _clean, work_dir, **kwargs):
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        (Path(work_dir) / 'main.pdf').write_bytes(value)
        return {'ok': True, 'log': '', 'pdf_url': '__PDF__'}
    return compile


def build(client, pid, request, purpose='submitted', **extra):
    response = client.post(f'/api/projects/{pid}/compile', json={'request_id': request, 'purpose': purpose, **extra})
    assert response.status_code == 200, response.text
    result = client.get(response.json()['status_url'])
    assert result.status_code == 200
    return result.json()


def test_preview_purpose_cannot_publish_or_replace_submitted_pdf(client, monkeypatch):
    pid = _project(client)
    monkeypatch.setattr(app, 'compile_project_files', compiler(b'submitted-pdf'))
    submitted = build(client, pid, 'submitted')
    assert submitted['artifact_eligibility'] == 'current' and submitted['promoted'] == 1
    proof = client.get(f'/api/projects/{pid}/lifecycle').json()['release_check']
    assert proof['status'] == 'passed'
    monkeypatch.setattr(app, 'compile_project_files', compiler(b'draft-pdf'))
    preview = build(client, pid, 'draft', 'draft_preview')
    assert preview['status'] == 'succeeded' and preview['artifact_eligibility'] == 'purpose_preview'
    current = client.get(f'/api/projects/{pid}/pdf')
    assert current.content == b'submitted-pdf' and current.headers['x-build-purpose'] == 'submitted'
    assert client.get(f"/api/projects/{pid}/builds/{preview['id']}/pdf").content == b'draft-pdf'
    assert client.get(f'/api/projects/{pid}/lifecycle').json()['release_check']['id'] == proof['id']


def test_draft_only_and_mismatched_snapshot_never_create_release_evidence(client, monkeypatch):
    pid = _project(client)
    monkeypatch.setattr(app, 'compile_project_files', compiler())
    preview = build(client, pid, 'preview', 'draft_preview')
    assert client.get(f'/api/projects/{pid}/lifecycle').json()['release_check'] is None
    mismatched = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'not-a-release', 'snapshot_id': preview['snapshot_id'], 'purpose': 'release'})
    assert mismatched.status_code == 409 and mismatched.json()['detail']['code'] == 'build_purpose_mismatch'
    assert len(client.get(f'/api/projects/{pid}/builds').json()) == 1


def test_late_success_and_explicit_old_snapshot_retry_stay_historical(client, monkeypatch):
    pid = _project(client)
    monkeypatch.setattr(app, 'compile_project_files', compiler(b'prior-current-pdf'))
    baseline = build(client, pid, 'baseline')
    main = next(f for f in client.get(f'/api/projects/{pid}').json()['files'] if f['path'] == 'main.tex')
    assert client.put(f'/api/projects/{pid}/files', json={'path': 'main.tex', 'base_version': main['version'], 'content': main['content'] + '\n% distinct queued snapshot'}).status_code == 200
    started, finish = threading.Event(), threading.Event()
    results = []
    def slow(*args, **kwargs):
        started.set()
        assert finish.wait(5), 'controlled late worker was not released'
        return compiler(b'late-old-pdf')(*args, **kwargs)
    monkeypatch.setattr(app, 'compile_project_files', slow)
    worker = threading.Thread(target=lambda: results.append(build(client, pid, 'late')))
    worker.start()
    try:
        assert started.wait(5)
        main = next(f for f in client.get(f'/api/projects/{pid}').json()['files'] if f['path'] == 'main.tex')
        saved = client.put(f'/api/projects/{pid}/files', json={'path': 'main.tex', 'base_version': main['version'], 'content': main['content'] + '\nNew saved version.'})
        assert saved.status_code == 200, saved.text
    finally:
        finish.set()
        worker.join(8)
    assert not worker.is_alive() and results
    late = results[0]
    assert late['status'] == 'succeeded' and late['artifact_eligibility'] == 'stale' and late['promoted'] == 0
    assert client.get(f'/api/projects/{pid}/pdf').content == b'prior-current-pdf'
    assert client.get(f"/api/projects/{pid}/builds/{late['id']}/pdf").content == b'late-old-pdf'
    monkeypatch.setattr(app, 'compile_project_files', compiler(b'old-explicit-retry'))
    retried = build(client, pid, 'retry', snapshot_id=late['snapshot_id'])
    assert retried['status'] == 'succeeded' and retried['promoted'] == 0 and retried['artifact_eligibility'] == 'stale'
    assert client.get(f'/api/projects/{pid}/pdf').content == b'prior-current-pdf'
    assert not client.get(f'/api/projects/{pid}/lifecycle').json()['release_check']['current']


def test_claimed_success_without_pdf_never_passes_publish_check(client, monkeypatch):
    pid = _project(client)
    monkeypatch.setattr(app, 'compile_project_files', lambda *args, **kwargs: {'ok': True, 'log': 'no artifact'})
    job = build(client, pid, 'missing-pdf')
    assert job['status'] == 'failed'
    proof = client.get(f'/api/projects/{pid}/lifecycle').json()['release_check']
    assert not proof['compile_ok'] and proof['status'] == 'blocked'


def test_environment_or_same_revision_file_change_invalidates_release(client, monkeypatch):
    pid = _project(client)
    monkeypatch.setattr(app, 'compile_project_files', compiler())
    job = build(client, pid, 'actual-artifact')
    assert client.get(f'/api/projects/{pid}/lifecycle').json()['release_check']['status'] == 'passed'
    previous_engine = app.build_snapshots.build_profile()['PWA_LATEX_ENGINE']
    monkeypatch.setenv('PWA_LATEX_ENGINE', 'lualatex' if previous_engine != 'lualatex' else 'xelatex')
    assert not client.get(f'/api/projects/{pid}/lifecycle').json()['release_check']['current']
    monkeypatch.setenv('PWA_LATEX_ENGINE', previous_engine)
    conn = app.db()
    with conn:
        conn.execute("UPDATE files SET content=content||'new dependency' WHERE project_id=? AND path='references.bib'", (pid,))
    conn.close()
    assert not client.get(f'/api/projects/{pid}/lifecycle').json()['release_check']['current']
    assert client.get(f"/api/projects/{pid}/builds/{job['id']}").json()['artifact_eligibility'] == 'stale'


def test_corrupt_pdf_is_detected_for_preview_history_and_release(client, monkeypatch):
    pid = _project(client)
    monkeypatch.setattr(app, 'compile_project_files', compiler())
    job = build(client, pid, 'corrupt-artifact')
    Path(job['artifact_path']).write_bytes(b'tampered')
    assert client.get(f'/api/projects/{pid}/pdf').status_code == 409
    assert client.get(f"/api/projects/{pid}/builds/{job['id']}/pdf").status_code == 409
    assert not client.get(f'/api/projects/{pid}/lifecycle').json()['release_check']['compile_ok']


def test_old_queue_uses_fixed_build_configuration_and_stays_historical(client, monkeypatch):
    pid = _project(client)
    original_engine = app.build_snapshots.build_profile()['PWA_LATEX_ENGINE']
    snapshot = client.post(f'/api/projects/{pid}/build-snapshots', json={'request_id': 'fixed-env', 'purpose': 'submitted'}).json()
    seen = []
    def fixed(*args, **kwargs):
        seen.append(kwargs['profile'])
        return compiler()(*args, **kwargs)
    monkeypatch.setattr(app, 'compile_project_files', fixed)
    monkeypatch.setenv('PWA_LATEX_ENGINE', 'xelatex' if original_engine != 'xelatex' else 'lualatex')
    result = build(client, pid, 'fixed-env-build', snapshot_id=snapshot['id'])
    assert seen[0]['PWA_LATEX_ENGINE'] == original_engine
    assert result['artifact_eligibility'] == 'stale' and result['promoted'] == 0


def test_failed_atomic_pdf_promotion_keeps_old_pdf_and_closes_job(client, monkeypatch):
    pid = _project(client)
    monkeypatch.setattr(app, 'compile_project_files', compiler(b'old-pdf'))
    build(client, pid, 'before-io-failure')
    main = next(f for f in client.get(f'/api/projects/{pid}').json()['files'] if f['path'] == 'main.tex')
    assert client.put(f'/api/projects/{pid}/files', json={'path': 'main.tex', 'base_version': main['version'], 'content': main['content'] + '\n% new source to build'}).status_code == 200
    original_replace = Path.replace
    def fail_pdf_promotion(path, target):
        if path.name.endswith('.pdf.tmp'):
            raise OSError('controlled artifact promotion failure')
        return original_replace(path, target)
    monkeypatch.setattr(Path, 'replace', fail_pdf_promotion)
    monkeypatch.setattr(app, 'compile_project_files', compiler(b'new-pdf'))
    failed = build(client, pid, 'failed-io-promotion')
    assert failed['status'] == 'failed' and failed['promoted'] == 0
    assert any(d['code'] == 'artifact_storage_failed' for d in failed['diagnostics'])
    assert client.get(f'/api/projects/{pid}/pdf').content == b'old-pdf'
