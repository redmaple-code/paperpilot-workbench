"""Actual persisted source payloads, queue events and immutable request aliases."""
from pathlib import Path
import json
import threading

import pytest

import app
from tests.test_req001 import save_revision
from paperpilot.services import build_queue, build_snapshots
from tests.test_req001 import make_candidate
from tests.test_req011_builds import _project


def main_file(client, pid):
    return next(f for f in client.get(f'/api/projects/{pid}').json()['files'] if f['path'] == 'main.tex')


def editor_draft(client, pid, content):
    file = main_file(client, pid)
    result = client.put(f'/api/projects/{pid}/editor/draft', json={'path': 'main.tex', 'base_version': file['version'], 'base_content': file['content'], 'content': content})
    assert result.status_code == 200, result.text


def save_main(client, pid, content):
    file = main_file(client, pid)
    saved = client.put(f'/api/projects/{pid}/files', json={'path': 'main.tex', 'base_version': file['version'], 'content': content})
    assert saved.status_code == 200, saved.text


def fake_compiler(observed):
    def run(files, _project, _clean, work_dir, **kwargs):
        observed.append({f['path']: f.get('content') for f in files})
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        (Path(work_dir) / 'main.pdf').write_bytes(b'controlled PDF consumer fixture')
        return {'ok': True, 'log': ''}
    return run


def test_editor_and_revision_draft_contents_are_read_only_and_immutable(client, monkeypatch):
    pid = _project(client)
    before = client.get(f'/api/projects/{pid}').json()['files']
    draft = main_file(client, pid)['content'] + '\n% Draft payload only'
    editor_draft(client, pid, draft)
    observed = []
    monkeypatch.setattr(app, 'compile_project_files', fake_compiler(observed))
    response = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'draft', 'purpose': 'draft_preview'})
    assert response.status_code == 200, response.text
    snapshot = response.json()['snapshot']
    assert observed[0]['main.tex'] == draft
    assert snapshot['manifest']['source']['entries'][0]['kind'] == 'editor'
    assert client.get(f'/api/projects/{pid}').json()['files'] == before
    assert client.get(f'/api/projects/{pid}/editor/draft?path=main.tex').json()['content'] == draft
    editor_draft(client, pid, draft + '\n% new persistent draft')
    old = client.get(f"/api/projects/{pid}/build-snapshots/{snapshot['id']}").json()
    assert old['files'] == snapshot['files']
    job = client.get(response.json()['status_url']).json()
    assert job['status'] == 'succeeded' and not job['current']
    assert client.get(f'/api/projects/{pid}/lifecycle').json()['release_check'] is None


def test_conflicting_draft_sources_require_choice_and_keep_both(client, monkeypatch):
    pid = _project(client)
    file = main_file(client, pid)
    editor_draft(client, pid, file['content'] + '\n% EDITOR')
    revised = file['content'] + '\n% REVISION'
    stored = save_revision(client,pid,{'target_kind': 'file', 'target_path': 'main.tex', 'base_version': file['version'], 'content': revised})
    assert stored.status_code == 200, stored.text
    blocked = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'conflict'})
    assert blocked.status_code == 409 and blocked.json()['detail']['code'] == 'build_draft_conflict'
    observed = []
    monkeypatch.setattr(app, 'compile_project_files', fake_compiler(observed))
    selected = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'chosen', 'draft_source': 'revision'})
    assert selected.status_code == 200, selected.text
    assert observed[-1]['main.tex'] == revised
    assert client.get(f'/api/projects/{pid}/editor/draft?path=main.tex').json()['content'].endswith('EDITOR')
    assert client.get(f'/api/projects/{pid}/human-revision/drafts').json()[0]['content'] == revised
    assert main_file(client, pid)['content'] == file['content']


def test_stale_draft_is_not_silently_replaced_with_saved_body(client, monkeypatch):
    pid = _project(client)
    file = main_file(client, pid)
    editor_draft(client, pid, file['content'] + '\n% old draft')
    save_main(client, pid, file['content'] + '\n% newer body')
    observed = []
    monkeypatch.setattr(app, 'compile_project_files', fake_compiler(observed))
    response = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'stale'})
    assert response.status_code == 409 and response.json()['detail']['code'] == 'build_draft_stale' and observed == []
    submitted = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'saved', 'purpose': 'submitted'})
    assert submitted.status_code == 200 and observed[-1]['main.tex'].endswith('newer body')
    assert client.get(f'/api/projects/{pid}/editor/draft?path=main.tex').json()['content'].endswith('old draft')


def test_specific_multifile_candidate_is_compiled_without_adoption(client, monkeypatch):
    pid = _project(client)
    completed, _ = make_candidate(client, monkeypatch, pid, request_id='candidate', content='Preflight candidate')
    candidate = completed['candidate']
    before = client.get(f'/api/projects/{pid}').json()['files']
    bib = next(f for f in before if f['path'] == 'references.bib')
    # A persisted multi-file candidate fixture isolates the build consumer, not model quality.
    conn = app.db()
    with conn:
        row = conn.execute('SELECT * FROM draft_candidates WHERE id=?', (candidate['id'],)).fetchone()
        patches = json.loads(row['content_json']) + [{'kind': 'file', 'path': 'references.bib', 'content': '% Candidate bibliography'}]
        versions = json.loads(row['base_file_versions_json']) | {'references.bib': bib['version']}
        conn.execute('UPDATE draft_candidates SET content_json=?,base_file_versions_json=? WHERE id=?', (json.dumps(patches), json.dumps(versions), candidate['id']))
    conn.close()
    observed = []
    monkeypatch.setattr(app, 'compile_project_files', fake_compiler(observed))
    monkeypatch.setattr(app, 'call_ai', lambda *a, **k: pytest.fail('preflight must never invoke a model'))
    mixed = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'mixed', 'purpose': 'candidate_preview', 'candidate_id': candidate['id'], 'draft_source': 'editor'})
    assert mixed.status_code == 422 and mixed.json()['detail']['code'] == 'build_source_mismatch'
    response = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'preflight', 'purpose': 'candidate_preview', 'candidate_id': candidate['id']})
    assert response.status_code == 200, response.text
    assert 'Preflight candidate' in observed[0]['main.tex'] and observed[0]['references.bib'] == '% Candidate bibliography'
    assert response.json()['snapshot']['manifest']['source']['candidate_id'] == candidate['id']
    assert client.get(f'/api/projects/{pid}').json()['files'] == before
    assert client.get(f"/api/projects/{pid}/candidates/{candidate['id']}").json()['status'] == 'candidate_ready'
    assert client.get(f'/api/projects/{pid}/lifecycle').json()['release_check'] is None
    other = _project(client)
    wrong_paper = client.post(f'/api/projects/{other}/compile', json={'request_id': 'cross', 'purpose': 'candidate_preview', 'candidate_id': candidate['id']})
    assert wrong_paper.status_code == 404


def test_missing_or_stale_candidate_has_guidance_not_saved_body_fallback(client, monkeypatch):
    pid = _project(client)
    missing = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'missing', 'purpose': 'candidate_preview'})
    assert missing.status_code == 422 and missing.json()['detail']['code'] == 'build_candidate_required'
    completed, _ = make_candidate(client, monkeypatch, pid, request_id='candidate', content='Candidate')
    candidate = completed['candidate']
    file = main_file(client, pid)
    save_main(client, pid, file['content'] + '\n% newer content')
    stale = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'stale', 'purpose': 'candidate_preview', 'candidate_id': candidate['id']})
    assert stale.status_code == 409 and stale.json()['detail']['code'] == 'build_candidate_stale'
    assert client.get(f'/api/projects/{pid}/builds').json() == []


def test_alias_deduplication_and_exact_request_replay(client, monkeypatch):
    pid = _project(client)
    observed = []
    monkeypatch.setattr(app, 'compile_project_files', fake_compiler(observed))
    one = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'one', 'purpose': 'submitted'}).json()
    two = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'two', 'purpose': 'submitted'}).json()
    assert two['build_job']['id'] == one['build_job']['id'] and two['build_job']['deduplicated']
    assert len(observed) == 1
    conflicting = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'two', 'purpose': 'draft_preview'})
    assert conflicting.status_code == 409 and conflicting.json()['detail']['code'] == 'build_request_conflict'
    file = main_file(client, pid)
    save_main(client, pid, file['content'] + '\n% a new source')
    replay = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'two', 'purpose': 'submitted'}).json()
    assert replay['build_job']['id'] == one['build_job']['id'] and replay['build_job']['idempotent_replay']
    assert replay['snapshot']['id'] == one['snapshot']['id'] and len(observed) == 1


def enqueue_fixture(conn, pid, label, *, trigger='explicit', purpose='draft_preview'):
    conn.execute("UPDATE files SET content=? WHERE project_id=? AND path='main.tex'", (r'\documentclass{article}\begin{document}' + label + r'\end{document}', pid))
    snapshot = build_snapshots.create(conn, pid, 's-' + label, purpose)
    return build_snapshots.create_job(conn, pid, snapshot['id'], label, purpose, trigger=trigger)


def test_running_A_then_explicit_B_E_then_latest_automatic_D(client):
    pid = _project(client)
    conn = app.db()
    with conn:
        a = enqueue_fixture(conn, pid, 'A', trigger='automatic')
        assert build_queue.claim_next(conn, pid)['id'] == a['id']
        b = enqueue_fixture(conn, pid, 'B')
        c = enqueue_fixture(conn, pid, 'C', trigger='automatic')
        d = enqueue_fixture(conn, pid, 'D', trigger='automatic')
        e = enqueue_fixture(conn, pid, 'E', purpose='submitted')
        assert d['superseded_job_ids'] == [c['id']]
        assert build_snapshots.get_job(conn, pid, c['id'])['status'] == 'superseded'
        assert build_queue.claim_next(conn, pid) is None
        assert [build_snapshots.get_job(conn, pid, j['id'])['queue_position'] for j in (b, e, d)] == [1, 2, 3]
        conn.execute("UPDATE build_jobs SET status='cancelled' WHERE id=?", (a['id'],))
        from paperpilot.services.tex_runtime import current_owner
        build_queue.release_slot(conn, pid, a['id'], current_owner())
        order = []
        while (job := build_queue.claim_next(conn, pid)):
            order.append(job['request_id'])
            conn.execute("UPDATE build_jobs SET status='failed' WHERE id=?", (job['id'],))
            build_queue.release_slot(conn, pid, job['id'], current_owner())
        assert order == ['B', 'E', 'D']
    conn.close()


def test_explicit_reuse_protects_automatic_item_and_preserves_explicit_fifo(client):
    pid = _project(client)
    conn = app.db()
    with conn:
        auto = enqueue_fixture(conn, pid, 'auto', trigger='automatic')
        first = enqueue_fixture(conn, pid, 'first-explicit', purpose='submitted')
        upgraded = build_snapshots.create_job(conn, pid, auto['snapshot_id'], 'explicit-alias', 'draft_preview')
        assert upgraded['id'] == auto['id'] and upgraded['trigger_kind'] == 'explicit'
        newer = enqueue_fixture(conn, pid, 'new-auto', trigger='automatic')
        assert not newer['superseded_job_ids']
        assert build_queue.claim_next(conn, pid)['id'] == first['id']
        conn.execute("UPDATE build_jobs SET status='failed' WHERE id=?", (first['id'],))
        from paperpilot.services.tex_runtime import current_owner
        build_queue.release_slot(conn, pid, first['id'], current_owner())
        assert build_queue.claim_next(conn, pid)['id'] == auto['id']
    conn.close()


def test_queue_full_rolls_back_rejection_but_automatic_replacement_is_allowed(client, monkeypatch):
    pid = _project(client)
    monkeypatch.setenv('PWA_BUILD_QUEUE_LIMIT', '2')
    monkeypatch.setattr(app, 'execute_build_job', lambda *a, **k: None)
    a = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'explicit', 'purpose': 'submitted'})
    assert a.status_code == 200
    auto = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'auto', 'trigger': 'automatic'}).json()
    file = main_file(client, pid)
    save_main(client, pid, file['content'] + '\n% automatic new source')
    replacement = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'replacement', 'trigger': 'automatic'})
    assert replacement.status_code == 200 and replacement.json()['build_job']['superseded_job_ids'] == [auto['build_job']['id']]
    refused = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'full', 'purpose': 'submitted'})
    assert refused.status_code == 429 and refused.json()['detail']['code'] == 'build_queue_full'
    jobs = client.get(f'/api/projects/{pid}/builds').json()
    assert len([j for j in jobs if j['status'] == 'queued']) == 2
    assert not any(j['request_id'] == 'full' for j in jobs)
    assert not any(s['request_id'] == 'full' for s in client.get(f'/api/projects/{pid}/build-snapshots').json())
    assert client.get(f"/api/projects/{pid}/builds/{auto['build_job']['id']}").json()['status'] == 'superseded'


def test_sqlite_claim_is_serial_even_without_inprocess_lock(client):
    pid = _project(client)
    conn = app.db()
    with conn:
        for n in range(3):
            enqueue_fixture(conn, pid, 'claim-' + str(n))
    conn.close()
    barrier = threading.Barrier(6)
    results = []
    def claim():
        c = app.db()
        barrier.wait()
        try:
            with c:
                results.append(build_queue.claim_next(c, pid))
        finally:
            c.close()
    workers = [threading.Thread(target=claim) for _ in range(6)]
    for worker in workers: worker.start()
    for worker in workers: worker.join(7)
    assert all(not worker.is_alive() for worker in workers)
    assert sum(r is not None for r in results) == 1


def test_actual_dispatcher_drains_persisted_order_and_frozen_candidate(client, monkeypatch):
    pid = _project(client)
    dispatcher = app.execute_build_job
    monkeypatch.setattr(app, 'execute_build_job', lambda *a, **k: None)
    original = main_file(client, pid)['content']
    order, content_by_request = [], {}
    start, finish = threading.Event(), threading.Event()
    def compile(files, _project, _clean, work_dir, **kwargs):
        c = app.db();request = c.execute('SELECT request_id FROM build_jobs WHERE id=?', (kwargs['job_id'],)).fetchone()[0];c.close()
        order.append(request);content_by_request[request] = next(f['content'] for f in files if f['path'] == 'main.tex')
        if request == 'A': start.set();assert finish.wait(8)
        return fake_compiler([])(files, _project, _clean, work_dir, **kwargs)
    monkeypatch.setattr(app, 'compile_project_files', compile)
    a = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'A', 'trigger': 'automatic'})
    assert a.status_code == 200
    worker = threading.Thread(target=dispatcher, args=(pid,))
    worker.start()
    try:
        assert start.wait(5)
        completed, _ = make_candidate(client, monkeypatch, pid, request_id='candidate-B', content='Explicit candidate B')
        b = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'B', 'purpose': 'candidate_preview', 'candidate_id': completed['candidate']['id']})
        assert b.status_code == 200, b.text
        for label in ['C', 'D']:
            save_main(client, pid, original + '\n% source ' + label)
            assert client.post(f'/api/projects/{pid}/compile', json={'request_id': label, 'trigger': 'automatic'}).status_code == 200
        save_main(client, pid, original + '\n% source E')
        assert client.post(f'/api/projects/{pid}/compile', json={'request_id': 'E', 'purpose': 'submitted'}).status_code == 200
    finally:
        finish.set();worker.join(12)
    assert not worker.is_alive() and order == ['A', 'B', 'E', 'D']
    assert 'Explicit candidate B' in content_by_request['B']
    assert content_by_request['D'].endswith('source D') and main_file(client, pid)['content'].endswith('source E')


def test_snapshot_request_ids_and_automatic_purpose_are_checked(client):
    pid = _project(client)
    first = client.post(f'/api/projects/{pid}/build-snapshots', json={'request_id': 'one', 'draft_source': 'editor'})
    assert first.status_code == 200
    conflict = client.post(f'/api/projects/{pid}/build-snapshots', json={'request_id': 'one', 'draft_source': 'revision'})
    assert conflict.status_code == 409 and conflict.json()['detail']['code'] == 'build_snapshot_request_conflict'
    invalid = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'auto-release', 'purpose': 'release', 'trigger': 'automatic'})
    assert invalid.status_code == 422 and invalid.json()['detail']['code'] == 'invalid_build_trigger'
    assert client.get(f'/api/projects/{pid}/builds').json() == []


def test_dead_running_owner_does_not_block_new_accepted_work_forever(client):
    pid = _project(client)
    conn = app.db()
    with conn:
        old = enqueue_fixture(conn, pid, 'dead-running')
        assert build_queue.claim_next(conn, pid)['id'] == old['id']
        conn.execute("UPDATE build_jobs SET owner_token='dead-birth' WHERE id=?", (old['id'],))
        next_job = enqueue_fixture(conn, pid, 'new-explicit')
        claimed = build_queue.claim_next(conn, pid)
        assert claimed['id'] == next_job['id']
        assert conn.execute('SELECT status FROM build_jobs WHERE id=?', (old['id'],)).fetchone()[0] == 'interrupted'
    conn.close()


def test_duplicate_dispatch_wakeups_return_while_current_build_is_busy(client, monkeypatch):
    pid = _project(client)
    conn = app.db()
    with conn:
        enqueue_fixture(conn, pid, 'busy')
    conn.close()
    entered, release = threading.Event(), threading.Event()
    def slow(*args, **kwargs):
        entered.set();assert release.wait(8)
        return fake_compiler([])(*args, **kwargs)
    monkeypatch.setattr(app, 'compile_project_files', slow)
    original = threading.Thread(target=app.execute_build_job, args=(pid,))
    original.start()
    wakeups = []
    try:
        assert entered.wait(4)
        wakeups = [threading.Thread(target=app.execute_build_job, args=(pid,)) for _ in range(50)]
        for worker in wakeups: worker.start()
        for worker in wakeups: worker.join(1)
        assert all(not worker.is_alive() for worker in wakeups), 'Duplicate notifications cannot occupy HTTP worker threads until compilation finishes'
        assert original.is_alive()
    finally:
        release.set();original.join(8)
        for worker in wakeups: worker.join(5)
    assert not original.is_alive()


def test_preparation_failure_closes_rolled_back_claim_and_next_job_runs(client, monkeypatch):
    pid = _project(client)
    conn = app.db()
    with conn:
        bad = enqueue_fixture(conn, pid, 'prepare-failure')
        good = enqueue_fixture(conn, pid, 'next-valid')
    conn.close()
    actual = app.citation_report
    attempts = []
    def fail_once(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise ValueError('controlled preparation failure before claim transaction commits')
        return actual(*args, **kwargs)
    monkeypatch.setattr(app, 'citation_report', fail_once)
    monkeypatch.setattr(app, 'compile_project_files', fake_compiler([]))
    app.execute_build_job(pid)
    assert client.get(f"/api/projects/{pid}/builds/{bad['id']}").json()['status'] == 'failed'
    assert client.get(f"/api/projects/{pid}/builds/{good['id']}").json()['status'] == 'succeeded'
    assert len(attempts) == 2
