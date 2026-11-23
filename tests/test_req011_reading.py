"""Map identities, version eligibility and read-only query boundaries."""
import gzip
import io
from pathlib import Path

import pytest

import app
from paperpilot.adapters import latex
from paperpilot.services import build_source_maps
from tests.test_req011_builds import _project
from tests.test_req011_sources_queue import main_file, save_main, fake_compiler


def mapped_compile(files, project, clean, work, **kwargs):
    work = Path(work);work.mkdir(parents=True, exist_ok=True)
    for file in files:
        if 'content' in file:
            p = work/file['path'];p.parent.mkdir(parents=True, exist_ok=True);p.write_bytes(file['content'].encode('utf-8'))
    (work/'main.pdf').write_bytes(b'explicit mapping-consumer PDF fixture; real TeX verified separately')
    (work/'main.synctex.gz').write_bytes(gzip.compress(b'explicit mapping-consumer fixture'))
    return {'ok': True, 'log': ''}


def mapped_job(client, monkeypatch):
    pid = _project(client)
    monkeypatch.setattr(app, 'compile_project_files', mapped_compile)
    result = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'mapped', 'purpose': 'submitted'})
    assert result.status_code == 200, result.text
    job = client.get(result.json()['status_url']).json()
    assert job['status'] == 'succeeded'
    return pid, job


def forward_body(file, **extra):
    return {'path': file['path'], 'line': 1, 'source_sha256': build_source_maps.sha(file['content'].encode()), 'source_version': file['version'], 'source_context': 'editor', **extra}


def test_sealed_map_identity_and_source_are_scoped_to_own_paper(client, monkeypatch):
    pid, job = mapped_job(client, monkeypatch)
    info = client.get(f"/api/projects/{pid}/builds/{job['id']}/reading")
    assert info.status_code == 200 and info.json()['source_map']['available']
    assert info.json()['source_files'][0]['sha256'] == build_source_maps.sha(main_file(client, pid)['content'].encode())
    other = _project(client)
    assert client.get(f"/api/projects/{other}/builds/{job['id']}/reading").status_code == 404
    assert client.post(f"/api/projects/{other}/builds/{job['id']}/source-map/source", json={'path': 'main.tex'}).status_code == 404
    denied = client.post(f"/api/projects/{pid}/builds/{job['id']}/source-map/source", json={'path': '../outside'})
    assert denied.status_code == 404


@pytest.mark.parametrize('name', ['main.pdf', 'main.synctex.gz', 'main.tex', '.paperpilot-source-map.json'])
def test_changed_pdf_map_source_or_seal_disables_queries(client, monkeypatch, name):
    pid, job = mapped_job(client, monkeypatch)
    run = Path(job['artifact_path']).parent
    (run/name).write_bytes(b'changed content')
    monkeypatch.setattr(latex, 'query_synctex', lambda *a: pytest.fail('corrupt identity must never invoke a reader'))
    info = client.get(f"/api/projects/{pid}/builds/{job['id']}/reading").json()
    assert not info['source_map']['available']
    denied = client.post(f"/api/projects/{pid}/builds/{job['id']}/source-map/forward", json=forward_body(main_file(client, pid)))
    assert denied.status_code == 409 and denied.json()['detail']['code'] == 'source_map_unavailable'


def test_forward_requires_visible_hash_version_and_current_editor_eligibility(client, monkeypatch):
    pid, job = mapped_job(client, monkeypatch)
    file = main_file(client, pid)
    calls = []
    def reader(*args):
        calls.append(args)
        return {'ok': True, 'output': 'Page:2\nx:100.5\ny:200.5\n'}
    monkeypatch.setattr(latex, 'query_synctex', reader)
    url = f"/api/projects/{pid}/builds/{job['id']}/source-map/forward"
    assert client.post(url, json=forward_body(file, source_sha256='0'*64)).status_code == 409
    assert client.post(url, json=forward_body(file, source_version=file['version']+1)).status_code == 409
    assert not calls
    valid = client.post(url, json=forward_body(file))
    assert valid.status_code == 200 and valid.json()['page'] == 2
    save_main(client, pid, file['content']+'\n% newer')
    assert client.post(url, json=forward_body(file)).json()['detail']['code'] == 'source_map_version_mismatch'
    history = client.post(url, json=forward_body(file, source_context='snapshot'))
    assert history.status_code == 200


def test_inverse_returns_frozen_source_and_does_not_expose_outside_paths(client, monkeypatch):
    pid, job = mapped_job(client, monkeypatch)
    original = main_file(client, pid)['content']
    save_main(client, pid, original+'\nnew body')
    monkeypatch.setattr(latex, 'query_synctex', lambda *a: {'ok': True, 'output': 'Input:/workspace/../other/main.tex\nLine:1\nInput:/workspace/./chapters/../main.tex\nLine:2\n'})
    url = f"/api/projects/{pid}/builds/{job['id']}/source-map/inverse"
    result = client.post(url, json={'page': 1, 'x': 10, 'y': 20})
    assert result.status_code == 200, result.text
    assert result.json()['path'] == 'main.tex' and result.json()['content'] == original and not result.json()['can_open_current']
    assert main_file(client, pid)['content'].endswith('new body')
    monkeypatch.setattr(latex, 'query_synctex', lambda *a: {'ok': True, 'output': 'Input:/etc/host-file\nLine:1\n'})
    denied = client.post(url, json={'page': 1, 'x': 10, 'y': 20})
    assert denied.status_code == 409 and denied.json()['detail']['code'] == 'source_map_no_location'
    assert client.post(url, json={'page': 0, 'x': 10, 'y': 20}).status_code == 422


def test_missing_map_and_query_capacity_give_reading_fallback(client, monkeypatch):
    pid = _project(client);monkeypatch.setattr(app, 'compile_project_files', fake_compiler([]))
    result = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'no-map', 'purpose': 'submitted'}).json()
    info = client.get(f"/api/projects/{pid}/builds/{result['build_job']['id']}/reading").json()
    assert not info['source_map']['available'] and '源码' in info['source_map']['message']
    assert client.get(info['pdf_url']).status_code == 200
    pid, job = mapped_job(client, monkeypatch)
    build_source_maps._slots.acquire();build_source_maps._slots.acquire()
    try:
        denied = client.post(f"/api/projects/{pid}/builds/{job['id']}/source-map/inverse", json={'page': 1, 'x': 1, 'y': 1})
        assert denied.status_code == 429 and denied.json()['detail']['code'] == 'source_map_busy'
    finally:
        build_source_maps._slots.release();build_source_maps._slots.release()


def test_failed_build_diagnostics_point_only_to_snapshot_files(client, monkeypatch):
    pid = _project(client)
    monkeypatch.setattr(app, 'compile_project_files', lambda *a, **k: {'ok': False, 'log': '/workspace/main.tex:2: Undefined control sequence\n/workspace/../private:1: outside\n/other/main.tex:1: package error'})
    response = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'error', 'purpose': 'submitted'}).json()
    job = client.get(response['status_url']).json()
    errors = [d for d in job['diagnostics'] if d['code'] == 'tex_source_error']
    assert len(errors) == 1 and errors[0]['path'] == 'main.tex' and errors[0]['line'] == 2
    source = client.post(f"/api/projects/{pid}/builds/{job['id']}/source-map/source", json={'path': 'main.tex', 'line': 2})
    assert source.status_code == 200 and source.json()['version'] == main_file(client, pid)['version']


def test_windows_newlines_preserve_raw_map_identity_and_match_visible_editor(client, monkeypatch):
    pid = _project(client)
    save_main(client, pid, main_file(client, pid)['content'].replace('\n', '\r\n'))
    monkeypatch.setattr(app, 'compile_project_files', mapped_compile)
    built = client.post(f'/api/projects/{pid}/compile', json={'request_id':'crlf','purpose':'submitted'}).json()
    job = client.get(built['status_url']).json();file = main_file(client, pid)
    reading = client.get(f"/api/projects/{pid}/builds/{job['id']}/reading").json()
    assert reading['source_map']['available']
    assert (Path(job['artifact_path']).parent/'main.tex').read_bytes() == file['content'].encode()
    monkeypatch.setattr(latex, 'query_synctex', lambda *a: {'ok':True,'output':'Page:1\nx:100\ny:200\n'})
    visible = build_source_maps.display_text(file['content'])
    located = client.post(f"/api/projects/{pid}/builds/{job['id']}/source-map/forward", json=forward_body(file, source_sha256=build_source_maps.sha(visible.encode())))
    assert located.status_code == 200, located.text


def test_static_module_and_wasm_types_are_valid_on_windows(client):
    for name in ['build/pdf.mjs','build/pdf.worker.mjs','web/pdf_viewer.mjs']:
        response = client.head('/static/vendor/pdfjs-6.4.299/'+name)
        assert response.status_code == 200 and response.headers['content-type'].startswith(('text/javascript','application/javascript'))
    directory = Path(__file__).resolve().parents[1]/'static/vendor/pdfjs-6.4.299/wasm'
    path = next(directory.glob('*.wasm'))
    response = client.head('/static/vendor/pdfjs-6.4.299/wasm/'+path.name)
    assert response.status_code == 200 and response.headers['content-type'].startswith('application/wasm')


def test_map_change_during_query_discards_result(client, monkeypatch):
    pid, job = mapped_job(client, monkeypatch)
    def reader(*args):
        (Path(job['artifact_path']).parent/'main.synctex.gz').write_bytes(b'changed during query')
        return {'ok':True,'output':'Page:1\nx:100\ny:200\n'}
    monkeypatch.setattr(latex, 'query_synctex', reader)
    response = client.post(f"/api/projects/{pid}/builds/{job['id']}/source-map/forward", json=forward_body(main_file(client, pid)))
    assert response.status_code == 409 and response.json()['detail']['code'] == 'source_map_changed'


def test_candidate_map_can_only_open_its_frozen_source_not_current_body(client, monkeypatch):
    from tests.test_req001 import make_candidate
    pid = _project(client);before = main_file(client, pid)
    generated, _ = make_candidate(client, monkeypatch, pid, request_id='candidate-map', content='Mapped candidate')
    candidate = generated['candidate']
    monkeypatch.setattr(app, 'compile_project_files', mapped_compile)
    built = client.post(f'/api/projects/{pid}/compile', json={'request_id':'candidate-preflight','purpose':'candidate_preview','candidate_id':candidate['id']}).json()
    job = client.get(built['status_url']).json()
    frozen = client.post(f"/api/projects/{pid}/builds/{job['id']}/source-map/source", json={'path':'main.tex','line':1}).json()
    assert not frozen['can_open_current'] and main_file(client, pid) == before
    monkeypatch.setattr(latex, 'query_synctex', lambda *a: {'ok':True,'output':'Page:1\nx:100\ny:200\n'})
    body = {'path':'main.tex','line':1,'source_sha256':frozen['sha256'],'source_version':frozen['version'],'source_context':'editor'}
    url = f"/api/projects/{pid}/builds/{job['id']}/source-map/forward"
    assert client.post(url,json=body).json()['detail']['code'] == 'source_map_version_mismatch'
    assert client.post(url,json={**body,'source_context':'snapshot'}).status_code == 200
    assert client.get(f"/api/projects/{pid}/candidates/{candidate['id']}").json()['status'] == 'candidate_ready'


def test_map_seal_cannot_overwrite_legitimate_source_map_json_input(client, monkeypatch):
    pid = _project(client)
    content = '{"paper_data": [1, 2, 3]}\n'
    saved = client.put(f'/api/projects/{pid}/files', json={'path':'source-map.json','content':content,'base_version':0})
    assert saved.status_code == 200, saved.text
    monkeypatch.setattr(app, 'compile_project_files', mapped_compile)
    built = client.post(f'/api/projects/{pid}/compile', json={'request_id':'no-seal-collision','purpose':'submitted'}).json()
    job = client.get(built['status_url']).json()
    assert (Path(job['artifact_path']).parent/'source-map.json').read_bytes() == content.encode()
    assert client.get(f"/api/projects/{pid}/builds/{job['id']}/reading").json()['source_map']['available']


def test_explicit_rebuild_repairs_lost_mapping_and_preserves_request_compatibility(client, monkeypatch):
    from paperpilot.services import build_queue
    pid, original = mapped_job(client, monkeypatch)
    file_before = client.get(f'/api/projects/{pid}').json()['files']
    conn = app.db()
    alias = conn.execute('SELECT request_fingerprint FROM build_request_aliases WHERE project_id=? AND request_id=?', (pid, 'mapped')).fetchone()[0]
    conn.close()
    old_params = {'purpose':'submitted','snapshot_id':None,'draft_source':None,'candidate_id':None,'trigger':'explicit'}
    assert alias == build_queue.fingerprint(old_params), 'Default parameter must not invalidate old request aliases'
    (Path(original['artifact_path']).parent/'.paperpilot-source-map.json').unlink()
    reading = client.get(f"/api/projects/{pid}/builds/{original['id']}/reading").json()
    assert not reading['source_map']['available'] and reading['can_rebuild_map']
    same = client.post(f'/api/projects/{pid}/compile', json={'request_id':'normal-again','snapshot_id':original['snapshot_id'],'purpose':'submitted'}).json()
    assert same['build_job']['id'] == original['id']
    request = {'request_id':'repair','snapshot_id':original['snapshot_id'],'purpose':'submitted','force_rebuild':True}
    repaired = client.post(f'/api/projects/{pid}/compile',json=request)
    assert repaired.status_code == 200, repaired.text
    new_job = repaired.json()['build_job']
    assert new_job['id'] != original['id']
    assert client.get(f"/api/projects/{pid}/builds/{new_job['id']}/reading").json()['source_map']['available']
    replay = client.post(f'/api/projects/{pid}/compile',json=request).json()
    assert replay['build_job']['id'] == new_job['id'] and replay['build_job']['idempotent_replay']
    assert client.post(f'/api/projects/{pid}/compile',json={**request,'force_rebuild':False}).status_code == 409
    assert client.post(f'/api/projects/{pid}/compile',json={'request_id':'automatic-force','trigger':'automatic','force_rebuild':True}).status_code == 422
    assert client.get(f'/api/projects/{pid}').json()['files'] == file_before
    assert client.get(f"/api/projects/{pid}/builds/{original['id']}/pdf").status_code == 200


def test_force_rebuild_deduplicates_an_already_queued_repair(client, monkeypatch):
    pid, original = mapped_job(client, monkeypatch)
    monkeypatch.setattr(app, 'execute_build_job', lambda *args, **kwargs: None)
    body = {'snapshot_id':original['snapshot_id'],'purpose':'submitted','force_rebuild':True}
    first = client.post(f'/api/projects/{pid}/compile',json={**body,'request_id':'repair-1'}).json()
    second = client.post(f'/api/projects/{pid}/compile',json={**body,'request_id':'repair-2'}).json()
    assert first['build_job']['status'] == 'queued' and first['build_job']['id'] != original['id']
    assert second['build_job']['id'] == first['build_job']['id'] and second['build_job']['deduplicated']


def test_old_fixed_environment_explains_why_map_cannot_be_rebuilt(client, monkeypatch):
    import json
    pid, job = mapped_job(client, monkeypatch)
    conn = app.db()
    with conn:
        row = conn.execute('SELECT manifest_json FROM build_snapshots WHERE id=?', (job['snapshot_id'],)).fetchone()
        manifest = json.loads(row[0]);manifest['build_profile'].pop('source_map_format')
        conn.execute('UPDATE build_snapshots SET manifest_json=? WHERE id=?',(json.dumps(manifest),job['snapshot_id']))
    conn.close()
    result = client.get(f"/api/projects/{pid}/builds/{job['id']}/reading").json()
    assert not result['can_rebuild_map'] and '恢复草稿' in result['rebuild_reason']


@pytest.mark.parametrize('oversized', [False, True])
def test_synctex_reader_uses_read_only_mount_no_network_deadline_and_output_cap(tmp_path, monkeypatch, oversized):
    from paperpilot.services import tex_runtime
    project = tmp_path/'paper';run = project/'build-runs'/'job';run.mkdir(parents=True)
    commands, cleanup = [], []
    monkeypatch.setattr(tex_runtime, 'reserve', lambda *a: {'fixture': True})
    monkeypatch.setattr(tex_runtime, 'cleanup', lambda *a, **k: cleanup.append(True))
    class Process:
        returncode = 0
        def __init__(self, command, **kwargs):
            commands.append(command);self.stdout = io.StringIO('x'*150000 if oversized else 'Page:1\nx:100\ny:200\n')
        def poll(self): return 0
    monkeypatch.setattr(latex.subprocess, 'Popen', Process)
    result = latex.query_synctex(run, project, 'job', ['view', '-i', '2:0:/workspace/main.tex', '-o', '/workspace/main.pdf'], {'PWA_TEX_SANDBOX_IMAGE': 'fixed-image'})
    assert bool(result['ok']) != oversized and cleanup
    command = commands[0]
    assert '--network=none' in command and '--read-only' in command
    assert command[command.index('--mount')+1].endswith(',readonly')
    assert ['--signal=KILL', '8', 'synctex'] == command[command.index('timeout')+1:command.index('timeout')+4]
