"""Kill a real build worker, restart the app, and verify durable recovery/old PDF."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
LOCAL = ROOT / '.project-to-act/local/project-development/full-product-20261101'
OUT = ROOT / '.project-to-act/docs/project-development/evidence/full-product-20261101'
LOOP = r'\documentclass{article}\begin{document}\newcount\counter\loop\advance\counter by 1\iftrue\repeat\end{document}'


def configure(directory):
    assert directory.resolve().is_relative_to(LOCAL.resolve())
    os.environ['PWA_TEX_SANDBOX_IMAGE'] = 'paperpilot-tex:local'
    os.environ['PWA_LATEX_ENGINE'] = 'pdflatex'
    os.environ['PWA_BIB_ENGINE'] = 'auto'
    os.environ['PWA_LATEX_TIMEOUT_SECONDS'] = '20'
    import app
    app.DB_PATH = directory / 'app.db'
    app.PROJECTS_DIR = directory / 'projects'
    app.PROJECTS_DIR.mkdir(exist_ok=True)
    return app


def worker(directory):
    app = configure(directory)
    from fastapi.testclient import TestClient
    with TestClient(app.app) as client:
        def ok(response):
            assert response.status_code == 200, response.text
            return response.json()
        pid = ok(client.post('/api/projects', json={'name': 'Synthetic real worker restart probe'}))['project']['id']
        workflow = ok(client.get(f'/api/projects/{pid}/workflow'))
        started = ok(client.post(f'/api/projects/{pid}/workflow/commands', json={'command': 'start', 'request_id': 'start', 'base_version': workflow['version']}))
        ok(client.post(f'/api/projects/{pid}/human-revision', json={'workflow_version': started['version']}))
        def save(content):
            main = next(f for f in ok(client.get(f'/api/projects/{pid}'))['files'] if f['path'] == 'main.tex')
            ok(client.put(f'/api/projects/{pid}/files', json={'path': 'main.tex', 'base_version': main['version'], 'content': content}))
        save(r'\documentclass{article}\begin{document}Prior successful PDF.\end{document}')
        built = ok(client.post(f'/api/projects/{pid}/compile', json={'request_id': 'prior-success', 'purpose': 'submitted'}))
        baseline = ok(client.get(built['status_url']))
        assert baseline['status'] == 'succeeded', baseline
        baseline_sha = hashlib.sha256(client.get(f'/api/projects/{pid}/pdf').content).hexdigest()
        save(LOOP)
        (directory / 'ready.json').write_text(json.dumps({'project_id': pid, 'baseline_sha': baseline_sha}), encoding='utf-8')
        client.post(f'/api/projects/{pid}/compile', json={'request_id': 'will-be-killed', 'purpose': 'submitted'})


def recover(directory):
    app = configure(directory)
    app.init_db()
    marker = json.loads((directory / 'ready.json').read_text(encoding='utf-8'))
    conn = app.db()
    rows = [dict(r) for r in conn.execute('SELECT id,status,request_id,diagnostics_json FROM build_jobs WHERE project_id=? ORDER BY created_at', (marker['project_id'],))]
    conn.close()
    pdf = app.PROJECTS_DIR / marker['project_id'] / 'build/main.pdf'
    print(json.dumps({'jobs': rows, 'pdf_sha': hashlib.sha256(pdf.read_bytes()).hexdigest()}, ensure_ascii=False))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['parent', 'worker', 'recover'], default='parent')
    parser.add_argument('--directory', type=Path)
    args = parser.parse_args()
    if args.mode != 'parent':
        assert args.directory
        return worker(args.directory) if args.mode == 'worker' else recover(args.directory)
    from paperpilot.services import tex_runtime
    LOCAL.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='real-restart-', dir=LOCAL) as temporary:
        directory = Path(temporary).resolve()
        assert directory.is_relative_to(LOCAL.resolve())
        process = subprocess.Popen([sys.executable, '-X', 'utf8', __file__, '--mode', 'worker', '--directory', str(directory)], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        marker = None
        try:
            deadline = time.monotonic() + 35
            target = None
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    _, error = process.communicate()
                    raise AssertionError('Worker exited before controlled kill: ' + error.decode('utf-8', errors='replace')[-2500:])
                if (directory / 'ready.json').exists():
                    marker = json.loads((directory / 'ready.json').read_text(encoding='utf-8'))
                    conn = sqlite3.connect(directory / 'app.db')
                    target = conn.execute("SELECT id FROM build_jobs WHERE request_id='will-be-killed' AND status='running'").fetchone()
                    conn.close()
                    if target:
                        listed = subprocess.run(['docker', 'ps', '-q', '--filter', 'label=io.paperpilot.job=' + target[0]], capture_output=True, text=True, timeout=5)
                        if listed.stdout.strip():
                            break
                time.sleep(.1)
            assert target and listed.stdout.strip(), 'Real queued build did not become a running sandbox'
            if os.name == 'nt':
                subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=10)
            else:
                process.kill()
            process.wait(10)
            command = [sys.executable, '-X', 'utf8', __file__, '--mode', 'recover', '--directory', str(directory)]
            cold = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=20)
            assert cold.returncode == 0, cold.stderr
            result = json.loads(cold.stdout)
            interrupted = next(row for row in result['jobs'] if row['id'] == target[0])
            cleanup = subprocess.run(['docker', 'ps', '-aq', '--filter', 'label=io.paperpilot.job=' + target[0]], capture_output=True, text=True, timeout=5)
            assert interrupted['status'] == 'interrupted' and not cleanup.stdout.strip(), interrupted
            assert result['pdf_sha'] == marker['baseline_sha'], 'Old successful PDF changed'
            assert len(result['jobs']) == 2
            replay = subprocess.run(command, capture_output=True, text=True, encoding='utf-8', timeout=20)
            assert replay.returncode == 0 and json.loads(replay.stdout) == result, 'Restart recovery must be idempotent and must not resubmit'
            report = {'passed': True, 'time': datetime.now(timezone.utc).isoformat(), 'real_worker_kill': True, 'real_docker': True,
                'isolated_database': True, 'external_model_calls': 0, 'checks': ['actual worker and Docker CLI tree killed',
                'fresh app startup auto-reconciles recent running job', 'container removed by persistent lease identity',
                'old successful PDF hash unchanged', 'no resubmission or cross-project mutation', 'second startup idempotent'],
                'jobs': result['jobs'], 'pdf_sha': result['pdf_sha'],
                'source_sha256': {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in ['app.py', 'paperpilot/services/tex_runtime.py', 'paperpilot/services/build_snapshots.py', 'paperpilot/adapters/latex.py', 'tests/verify_build_restart.py']}}
            (OUT / 'BUILD-RESTART-REAL.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({'passed': True, 'checks': len(report['checks'])}))
        finally:
            if process.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=10)
                else:
                    process.kill()
                process.wait(10)
            if marker:
                tex_runtime.recover(directory / 'projects' / marker['project_id'], force=True)


if __name__ == '__main__':
    main()
