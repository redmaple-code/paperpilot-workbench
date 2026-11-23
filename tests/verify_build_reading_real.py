"""Actual offline TeX and SyncTeX queries against isolated frozen builds."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import tempfile
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient
import app
from tests.test_req011_builds import _project
from tests.test_req011_sources_queue import main_file, save_main

OUT = ROOT/'.project-to-act/docs/project-development/evidence/full-product-20261101'
TEXT = '\\documentclass{article}\n\\begin{document}\n\\section{First page}\nFirst mapping sentence.\n\\newpage\n\\section{Second page}\nSecond mapping sentence.\n\\end{document}\n'

with tempfile.TemporaryDirectory(prefix='paperpilot-reading-real-') as temp:
    app.DB_PATH = Path(temp)/'test.db';app.PROJECTS_DIR = Path(temp)/'projects';app.PROJECTS_DIR.mkdir()
    os.environ.update(PWA_TEX_SANDBOX_IMAGE='paperpilot-tex:local', PWA_LATEX_ENGINE='pdflatex', PWA_BIB_ENGINE='auto', PWA_LATEX_TIMEOUT_SECONDS='30')
    app.init_db();client = TestClient(app.app)
    pid = _project(client);other = _project(client)
    save_main(client, pid, TEXT)
    file = main_file(client, pid)
    built = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'actual-map', 'purpose': 'submitted'})
    assert built.status_code == 200, built.text
    job = client.get(built.json()['status_url']).json();assert job['status'] == 'succeeded', job
    reading = client.get(f"/api/projects/{pid}/builds/{job['id']}/reading").json()
    assert reading['source_map']['available'], reading
    base = f"/api/projects/{pid}/builds/{job['id']}"
    body = {'path': 'main.tex', 'line': 7, 'column': 0, 'source_sha256': hashlib.sha256(TEXT.encode()).hexdigest(), 'source_version': file['version'], 'source_context': 'editor'}
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(job['artifact_path']).parent.iterdir() if p.is_file()}
    located = client.post(base+'/source-map/forward', json=body)
    assert located.status_code == 200, located.text
    point = located.json();assert point['page'] == 2, point
    reverse = client.post(base+'/source-map/inverse', json={k: point[k] for k in ['page','x','y']})
    assert reverse.status_code == 200, reverse.text
    source = reverse.json();assert source['path'] == 'main.tex' and source['line'] in {6,7} and source['content'] == TEXT and source['can_open_current'], source
    after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in Path(job['artifact_path']).parent.iterdir() if p.is_file()}
    assert before == after, 'Read-only location queries changed frozen build files'
    save_main(client, pid, TEXT+'% new submitted revision\n')
    assert client.post(base+'/source-map/forward', json=body).status_code == 409
    historical = client.post(base+'/source-map/forward', json={**body,'source_context':'snapshot'})
    assert historical.status_code == 200, historical.text
    historic_source = client.post(base+'/source-map/inverse', json={k: point[k] for k in ['page','x','y']}).json()
    assert not historic_source['can_open_current'] and historic_source['content'] == TEXT
    assert client.get(f"/api/projects/{other}/builds/{job['id']}/reading").status_code == 404
    run = Path(job['artifact_path']).parent;mapping = run/'main.synctex.gz';original_map = mapping.read_bytes()
    mapping.write_bytes(original_map+b'corruption')
    assert not client.get(base+'/reading').json()['source_map']['available']
    assert client.post(base+'/source-map/inverse', json={k: point[k] for k in ['page','x','y']}).status_code == 409
    assert client.get(base+'/pdf').status_code == 200, 'Map failure must not hide the real PDF'
    mapping.write_bytes(original_map)
    bad = TEXT.replace('First mapping sentence.', '\\undefinedReadingCommand')
    save_main(client, pid, bad)
    failure = client.post(f'/api/projects/{pid}/compile', json={'request_id':'actual-source-error','purpose':'submitted'}).json()
    failed = client.get(failure['status_url']).json()
    assert failed['status'] == 'failed', failed
    error = next(d for d in failed['diagnostics'] if d['code'] == 'tex_source_error')
    assert error['path'] == 'main.tex' and error['line'] == 4, error
    fixed_source = client.post(f"/api/projects/{pid}/builds/{failed['id']}/source-map/source",json={'path':'main.tex','line':4})
    assert fixed_source.status_code == 200 and fixed_source.json()['content'] == bad
    multi = _project(client)
    child = 'First nested sentence.\nSecond nested sentence.\n'
    save_main(client,multi,'\\documentclass{article}\n\\begin{document}\n\\input{sections/./body}\n\\end{document}\n')
    saved_child = client.put(f'/api/projects/{multi}/files',json={'path':'sections/body.tex','content':child,'base_version':0})
    assert saved_child.status_code == 200,saved_child.text
    multi_build = client.post(f'/api/projects/{multi}/compile',json={'request_id':'nested','purpose':'submitted'}).json()
    multi_job = client.get(multi_build['status_url']).json();assert multi_job['status']=='succeeded',multi_job
    multi_base = f"/api/projects/{multi}/builds/{multi_job['id']}"
    child_file = next(f for f in client.get(f'/api/projects/{multi}').json()['files'] if f['path']=='sections/body.tex')
    child_forward = client.post(multi_base+'/source-map/forward',json={'path':child_file['path'],'line':2,'column':0,'source_sha256':hashlib.sha256(child.encode()).hexdigest(),'source_version':child_file['version'],'source_context':'editor'})
    assert child_forward.status_code==200,child_forward.text
    child_point = child_forward.json()
    child_inverse = client.post(multi_base+'/source-map/inverse',json={k:child_point[k] for k in ['page','x','y']})
    assert child_inverse.status_code==200,child_inverse.text
    assert child_inverse.json()['path']=='sections/body.tex' and child_inverse.json()['content']==child
    checks=['real two-page TeX PDF and sealed SyncTeX map', 'source line7 maps to actual page2', 'page2 point maps back to its exact frozen source', 'location queries leave build files byte-identical', 'new editor version cannot use old map', 'history maps only to frozen source and remains read-only', 'cross-paper job access refused', 'corrupt map stops location while real PDF remains readable', 'actual TeX file-line diagnostic resolves only matching frozen source', 'real nested file forward/inverse with dot-path normalization']
    names=['app.py','paperpilot/adapters/latex.py','paperpilot/services/build_source_maps.py','paperpilot/services/build_snapshots.py','paperpilot/domain/schemas.py','tests/verify_build_reading_real.py']
    report={'passed':True,'time':datetime.now(timezone.utc).isoformat(),'real_tex':True,'real_synctex':True,'isolated_database':True,'checks':checks,'actual_forward':point,'actual_inverse':{k:source[k] for k in ['path','line','version','can_open_current']},'source_sha256':{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names}}
    (OUT/'BUILD-READING-REAL.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'passed':True,'checks':len(checks),'forward_page':point['page'],'inverse_line':source['line']}))
