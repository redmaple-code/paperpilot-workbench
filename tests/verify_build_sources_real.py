"""Real TeX output and persisted A/B/C/D/E ordering in an isolated app database."""
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))
from fastapi.testclient import TestClient
from pypdf import PdfReader
import pytest
import app
from tests.provider_fixtures import install_default_provider_fixture
from tests.test_req001 import make_candidate

LOCAL = ROOT / '.project-to-act/local/project-development/full-product-20261101'
OUT = ROOT / '.project-to-act/docs/project-development/evidence/full-product-20261101'
LOOP = r'\documentclass{article}\begin{document}\newcount\counter\loop\advance\counter by 1\iftrue\repeat\end{document}'
BASE = r'\documentclass{article}\begin{document}\section{Introduction}Saved baseline.\end{document}'

os.environ['PWA_TEX_SANDBOX_IMAGE'] = 'paperpilot-tex:local'
os.environ['PWA_LATEX_ENGINE'] = 'pdflatex'
os.environ['PWA_BIB_ENGINE'] = 'auto'
os.environ['PWA_LATEX_TIMEOUT_SECONDS'] = '30'
os.environ['PWA_BUILD_QUEUE_LIMIT'] = '8'

with tempfile.TemporaryDirectory(prefix='real-build-sources-', dir=LOCAL) as temporary, pytest.MonkeyPatch.context() as patch:
    workspace = Path(temporary).resolve()
    assert workspace.is_relative_to(LOCAL.resolve())
    app.DB_PATH = workspace / 'app.db'
    app.PROJECTS_DIR = workspace / 'projects'
    app.PROJECTS_DIR.mkdir()
    app.init_db()
    install_default_provider_fixture()
    client = TestClient(app.app)
    def ok(response):
        assert response.status_code in {200, 202}, response.text
        return response.json()
    pid = ok(client.post('/api/projects', json={'name': 'Real source and queue verification'}))['project']['id']
    wf = ok(client.get(f'/api/projects/{pid}/workflow'))
    started = ok(client.post(f'/api/projects/{pid}/workflow/commands', json={'command': 'start', 'base_version': wf['version'], 'request_id': 'start'}))
    ok(client.post(f'/api/projects/{pid}/human-revision', json={'workflow_version': started['version']}))
    def main_file():
        return next(f for f in ok(client.get(f'/api/projects/{pid}'))['files'] if f['path'] == 'main.tex')
    def save(content):
        f = main_file()
        ok(client.put(f'/api/projects/{pid}/files', json={'path': 'main.tex', 'content': content, 'base_version': f['version']}))
    def draft(content):
        f = main_file()
        ok(client.put(f'/api/projects/{pid}/editor/draft', json={'path': 'main.tex', 'base_version': f['version'], 'base_content': f['content'], 'content': content}))
    def compile(request, **extra):
        return ok(client.post(f'/api/projects/{pid}/compile', json={'request_id': request, **extra}))
    def text(job_id):
        response = client.get(f'/api/projects/{pid}/builds/{job_id}/pdf')
        assert response.status_code == 200, response.text
        return '\n'.join(p.extract_text() or '' for p in PdfReader(io.BytesIO(response.content)).pages)
    save(BASE)
    baseline = compile('baseline', purpose='submitted')
    baseline_pdf = client.get(f'/api/projects/{pid}/pdf').content
    draft(BASE.replace('Saved baseline.', 'Persisted editor draft only.'))
    preview = compile('draft-proof')
    assert 'Persisted editor draft only.' in text(preview['build_job']['id'])
    assert main_file()['content'] == BASE and client.get(f'/api/projects/{pid}/pdf').content == baseline_pdf
    observed = []
    actual_compile = app.compile_project_files
    dispatcher = app.execute_build_job
    def recorded(files, project, clean, work, **kwargs):
        conn = app.db()
        request = conn.execute('SELECT request_id FROM build_jobs WHERE id=?', (kwargs['job_id'],)).fetchone()[0]
        conn.close()
        observed.append(request)
        return actual_compile(files, project, clean, work, **kwargs)
    patch.setattr(app, 'compile_project_files', recorded)
    patch.setattr(app, 'execute_build_job', lambda *a, **k: None)
    draft(LOOP)
    a = compile('A', trigger='automatic')
    worker = threading.Thread(target=dispatcher, args=(pid,))
    worker.start()
    try:
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if observed == ['A']:
                break
            time.sleep(.05)
        assert observed == ['A']
        completed, _ = make_candidate(client, patch, pid, request_id='candidate-B', content='Real candidate preflight')
        b = compile('B', purpose='candidate_preview', candidate_id=completed['candidate']['id'])
        draft(BASE.replace('Saved baseline.', 'Automatic C.'))
        c = compile('C', trigger='automatic')
        draft(BASE.replace('Saved baseline.', 'Latest automatic D.'))
        d = compile('D', trigger='automatic')
        save(BASE.replace('Saved baseline.', 'Explicit submitted E.'))
        e = compile('E', purpose='submitted')
        cancelled = ok(client.post(f"/api/projects/{pid}/builds/{a['build_job']['id']}/cancel"))
        assert cancelled['status'] == 'cancelled'
        worker.join(45)
        assert not worker.is_alive(), 'Real dispatcher did not drain accepted tasks'
        jobs = {j['request_id']: j for j in ok(client.get(f'/api/projects/{pid}/builds'))}
        assert observed == ['A', 'B', 'E', 'D'], observed
        assert jobs['C']['status'] == 'superseded'
        assert all(jobs[k]['status'] == 'succeeded' for k in ['B', 'D', 'E'])
        assert 'Real candidate preflight' in text(jobs['B']['id'])
        assert 'Latest automatic D.' in text(jobs['D']['id'])
        assert 'Explicit submitted E.' in text(jobs['E']['id'])
        assert jobs['D']['promoted'] == 0 and jobs['B']['promoted'] == 0 and jobs['E']['promoted'] == 1
        assert 'Explicit submitted E.' in '\n'.join(p.extract_text() or '' for p in PdfReader(io.BytesIO(client.get(f'/api/projects/{pid}/pdf').content)).pages)
        assert main_file()['content'].endswith('Explicit submitted E.\\end{document}')
        assert ok(client.get(f"/api/projects/{pid}/candidates/{completed['candidate']['id']}"))['status'] != 'applied'
        report = {'passed': True, 'time': datetime.now(timezone.utc).isoformat(), 'real_tex': True, 'isolated_database': True,
            'model': 'explicit candidate test double; not cloud quality evidence', 'order': observed,
            'checks': ['persisted editor draft appears in real PDF', 'preview does not commit body or replace submitted PDF',
                'real A cancelled', 'C superseded, B/E protected and ordered before D', 'candidate frozen content survives later body changes',
                'B/D/E real PDF text matches each frozen source', 'only E promoted', 'candidate not adopted'],
            'jobs': {k: {'id': v['id'], 'status': v['status'], 'purpose': v['purpose'], 'promoted': v['promoted']} for k,v in jobs.items()},
            'source_sha256': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ['app.py', 'paperpilot/services/build_sources.py', 'paperpilot/services/build_queue.py', 'paperpilot/services/build_snapshots.py', 'tests/verify_build_sources_real.py']}}
        (OUT / 'BUILD-SOURCES-REAL.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps({'passed': True, 'order': observed, 'checks': len(report['checks'])}))
    finally:
        if worker.is_alive():
            for job in ok(client.get(f'/api/projects/{pid}/builds')):
                if job['status'] in {'queued', 'running'}:
                    client.post(f"/api/projects/{pid}/builds/{job['id']}/cancel")
            worker.join(15)
