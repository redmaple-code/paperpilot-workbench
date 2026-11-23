"""Real, synthetic Docker/TeX isolation and process lifecycle probes; no user papers."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from paperpilot.adapters import latex

LOCAL = ROOT / '.project-to-act/local/project-development/full-product-20261101'
OUT = ROOT / '.project-to-act/docs/project-development/evidence/full-product-20261101'
INFINITE = r'\documentclass{article}\begin{document}\newcount\counter\loop\advance\counter by 1\iftrue\repeat\end{document}'


def docker(*args, timeout=10):
    return subprocess.run(['docker', *args], capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout)


def containers(job):
    result = docker('ps', '-aq', '--filter', 'name=pwa-' + job + '-')
    assert result.returncode == 0, 'Docker not available'
    return result.stdout.split()


def wait_container(job, seconds=10):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        for cid in containers(job):
            read = docker('inspect', cid)
            if read.returncode == 0:
                item = json.loads(read.stdout)[0]
                if item['State']['Running']:
                    return cid, item
        time.sleep(.1)
    raise AssertionError('Controlled infinite-loop container did not start')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', default='TEX-ISOLATION-BASELINE.json')
    parser.add_argument('--include-process-kill', action='store_true')
    args = parser.parse_args()
    assert Path(args.report).name == args.report and args.report.endswith('.json')
    os.environ['PWA_TEX_SANDBOX_IMAGE'] = 'paperpilot-tex:local'
    os.environ['PWA_LATEX_ENGINE'] = 'pdflatex'
    os.environ['PWA_BIB_ENGINE'] = 'auto'
    os.environ['PWA_LATEX_TIMEOUT_SECONDS'] = '20'
    LOCAL.mkdir(parents=True, exist_ok=True)
    report = {'time': datetime.now(timezone.utc).isoformat(), 'synthetic_inputs_only': True,
        'image_id': docker('image', 'inspect', 'paperpilot-tex:local', '--format', '{{.Id}}').stdout.strip(), 'checks': []}
    owned_jobs = []
    with tempfile.TemporaryDirectory(prefix='tex-isolation-', dir=LOCAL) as temp:
        base = Path(temp).resolve()
        assert base.is_relative_to(LOCAL.resolve())
        canary = base / 'outside-work-canary.txt'
        canary.write_text('synthetic host canary, not a user file', encoding='utf-8')
        def compile_case(name, source):
            job = 'probe-' + uuid.uuid4().hex[:12]
            owned_jobs.append(job)
            project = base / name
            result = latex.compile_project_files([{'path': 'main.tex', 'content': source}], project, lambda x: x, project / 'work', job_id=job)
            report['checks'].append({'name': name, 'result': {k: v for k, v in result.items() if k != 'log'},
                'log_tail': result.get('log', '')[-3000:], 'remaining_containers': containers(job)})
            return result, project / 'work'
        try:
            result, work = compile_case('shell-escape', r'\documentclass{article}\begin{document}\immediate\write18{touch /workspace/shell-escape-created}Probe\end{document}')
            report['checks'][-1]['passed'] = result.get('ok') and not (work / 'shell-escape-created').exists()
            result, work = compile_case('outside-read', r'\documentclass{article}\begin{document}\newread\probe\openin\probe=/etc/hostname\read\probe to\line\closein\probe\line\end{document}')
            report['checks'][-1]['passed'] = not result.get('ok') and 'openin_any' in result.get('log', '')
            result, work = compile_case('parent-write', r'\documentclass{article}\begin{document}\newwrite\probe\immediate\openout\probe=../escaped.txt\immediate\write\probe{escape}\immediate\closeout\probe\end{document}')
            report['checks'][-1]['passed'] = not result.get('ok') and not (work.parent / 'escaped.txt').exists() and 'openout_any' in result.get('log', '')
            job = 'probe-' + uuid.uuid4().hex[:12]
            owned_jobs.append(job)
            results = []
            worker = threading.Thread(target=lambda: results.append(latex.compile_project_files(
                [{'path': 'main.tex', 'content': INFINITE}], base / 'cancel', lambda x: x, base / 'cancel/work', job_id=job)))
            worker.start()
            cid, inspected = wait_container(job)
            host, config = inspected['HostConfig'], inspected['Config']
            isolated = host['NetworkMode'] == 'none' and host['ReadonlyRootfs'] and host['CapDrop'] == ['ALL'] and host['Memory'] > 0 and host['PidsLimit'] > 0
            mount = inspected['Mounts']
            isolated = isolated and len(mount) == 1 and mount[0]['Destination'] == '/workspace' and config['User'] == '10001:10001'
            report['checks'].append({'name': 'actual-runtime-restrictions', 'passed': bool(isolated),
                'network': host['NetworkMode'], 'readonly_root': host['ReadonlyRootfs'], 'cap_drop': host['CapDrop'],
                'memory_bytes': host['Memory'], 'pids': host['PidsLimit'], 'user': config['User'], 'mount_count': len(mount), 'destination': mount[0]['Destination']})
            cancelled = latex.cancel_compile(job)
            worker.join(12)
            report['checks'].append({'name': 'real-cancel', 'passed': cancelled and not worker.is_alive() and bool(results[0].get('cancelled')) and not containers(job),
                'worker_joined': not worker.is_alive(), 'result': results[0] if results else {}, 'remaining_containers': containers(job)})
            os.environ['PWA_LATEX_TIMEOUT_SECONDS'] = '2'
            start = time.monotonic()
            result, _ = compile_case('timeout', INFINITE)
            report['checks'][-1]['elapsed_seconds'] = time.monotonic() - start
            report['checks'][-1]['passed'] = result.get('code') == 'compile_timeout' and not report['checks'][-1]['remaining_containers']
            if args.include_process_kill:
                job = 'probe-' + uuid.uuid4().hex[:12]
                owned_jobs.append(job)
                project = base / 'killed'
                program = "import sys,os;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from paperpilot.adapters.latex import compile_project_files;os.environ['PWA_LATEX_TIMEOUT_SECONDS']='20';compile_project_files([{'path':'main.tex','content':sys.argv[4]}],Path(sys.argv[2]),lambda x:x,Path(sys.argv[2])/'work',job_id=sys.argv[3])"
                process = subprocess.Popen([sys.executable, '-X', 'utf8', '-c', program, str(ROOT), str(project), job, INFINITE], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                cid, _ = wait_container(job)
                if os.name == 'nt':
                    subprocess.run(['taskkill.exe', '/PID', str(process.pid), '/T', '/F'], capture_output=True, timeout=10)
                else:
                    process.kill()
                process.wait(10)
                time.sleep(.5)
                remaining = containers(job)
                from paperpilot.services import tex_runtime
                if hasattr(tex_runtime, 'recover'):
                    # A fresh process has no in-memory registry of the killed worker.
                    recovery = subprocess.run([sys.executable, '-X', 'utf8', '-c',
                        "import sys,json;from pathlib import Path;sys.path.insert(0,sys.argv[1]);from paperpilot.services.tex_runtime import recover;print(json.dumps(recover(Path(sys.argv[2]))))", str(ROOT), str(project)],
                        capture_output=True, text=True, timeout=15)
                    cleaned = containers(job)
                    report['checks'].append({'name': 'process-kill-cold-recovery', 'passed': recovery.returncode == 0 and not cleaned,
                        'worker_terminated': process.poll() is not None, 'containers_before_recovery': remaining,
                        'recovery': json.loads(recovery.stdout) if recovery.returncode == 0 else 'failed', 'remaining_containers': cleaned})
            report['host_canary_unchanged'] = canary.read_text(encoding='utf-8') == 'synthetic host canary, not a user file'
        finally:
            # Only exact IDs discovered under this run's random probe job names.
            for job in owned_jobs:
                for cid in containers(job):
                    docker('rm', '-f', cid)
    report['passed'] = all(c.get('passed') for c in report['checks']) and report.get('host_canary_unchanged', False)
    report['source_sha256'] = {p: hashlib.sha256((ROOT/p).read_bytes()).hexdigest() for p in ['paperpilot/adapters/latex.py', 'tests/verify_tex_isolation.py']}
    (OUT / args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({'passed': report['passed'], 'checks': [{'name': c['name'], 'passed': c.get('passed')} for c in report['checks']]}, ensure_ascii=False))
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
