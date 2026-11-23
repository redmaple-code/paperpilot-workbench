"""Restore the allowlisted source archive into a separate directory and boot it."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--bundle-dir',required=True)
    parser.add_argument('--output',required=True)
    parser.add_argument('--port',type=int,default=8899)
    args=parser.parse_args()
    root=Path(__file__).resolve().parents[1]
    bundle=Path(args.bundle_dir).resolve()
    manifest=json.loads((bundle/'SOURCE-DELIVERY-MANIFEST.json').read_text(encoding='utf8'))
    archive=bundle/manifest['archive']['name']
    assert hashlib.sha256(archive.read_bytes()).hexdigest()==manifest['archive']['sha256']
    parent=root/'.project-to-act/local';parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='source-restore-',dir=parent,ignore_cleanup_errors=True) as temporary:
        restored=Path(temporary)/'source';store=Path(temporary)/'isolated-data'
        with zipfile.ZipFile(archive) as z:
            for name in z.namelist():
                parts=Path(name).parts
                assert not Path(name).is_absolute() and '..' not in parts
                assert not {'.env','data','_complete_archive','.git','.project-to-act','__pycache__'}.intersection(parts)
            z.extractall(restored)
        for row in manifest['files']:
            assert hashlib.sha256((restored/row['path']).read_bytes()).hexdigest()==row['sha256']
        env={k:v for k,v in os.environ.items() if not k.startswith(('PWA_','PAPERPILOT_'))}
        env.update(PWA_DATA_DIR=str(store),PWA_HOST_BUDGET_DB=str(store/'budget.sqlite3'),PYTHONUTF8='1')
        for _ in range(2):
            result=subprocess.run([sys.executable,'scripts/seed_demo.py','--data-dir',str(store)],cwd=restored,env=env,capture_output=True,encoding='utf8',timeout=90)
            assert result.returncode==0,result.stderr[-1000:]
        server=subprocess.Popen([sys.executable,'-m','uvicorn','app:app','--host','127.0.0.1','--port',str(args.port)],cwd=restored,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        def get(path):
            with urllib.request.urlopen(f'http://127.0.0.1:{args.port}'+path,timeout=2) as response:return json.load(response)
        try:
            for _ in range(100):
                try:health=get('/api/health');break
                except (OSError,ValueError):
                    assert server.poll() is None,'Restored server exited early'
                    time.sleep(.1)
            else:raise AssertionError('Restored startup timeout')
            assert health['ok'] and not health['ai_configured']
            projects=get('/api/projects');assert len(projects)==1
            project=get('/api/projects/'+projects[0]['id'])
            assert len(project['terms'])==1 and len(project['corpus'])==1
            assert not (restored/'.env').exists() and not (restored/'data').exists()
        finally:
            server.terminate();server.wait(timeout=15)
    report={'status':'passed','source_files':len(manifest['files']),'archive_sha256':manifest['archive']['sha256'],
            'checks':['all-source-hashes','private-paths-excluded','separate-directory-startup','seed-twice-one-project','no-configured-ai','one-term-one-corpus'],
            'dependencies':'existing installed Python environment; fresh virtualenv installation not tested',
            'python':sys.version.split()[0],'product_acceptance':False}
    out=Path(args.output);out.mkdir(parents=True,exist_ok=True)
    (out/'SOURCE-RESTORE.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf8')
    print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
