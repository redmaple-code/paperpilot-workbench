"""Lifecycle, identity and filesystem regressions; real Docker probes are separate."""
from pathlib import Path
from types import SimpleNamespace
import json
import os

import pytest

import app
from paperpilot.adapters import latex
from paperpilot.services import build_snapshots, tex_runtime


def lease(tmp_path, *, live=False):
    project = tmp_path / 'project'
    path = tex_runtime.reserve(project, 'test-job', 'pwa-test-job-latex-1-01234567')
    if not live:
        record = json.loads(path.read_text())
        record['owner_token'] = 'previous-worker-birth'
        path.write_text(json.dumps(record), encoding='utf-8')
    return project, path


def test_native_process_birth_identity_and_pid_reuse_guard():
    current = tex_runtime.current_owner()
    assert current['owner_pid'] == os.getpid() and tex_runtime.owner_alive(current)
    assert not tex_runtime.owner_alive({**current, 'owner_token': 'another-birth'})
    assert not tex_runtime.owner_alive({'owner_pid': 0, 'owner_token': ''})


def test_live_owner_lease_is_never_stolen(tmp_path, monkeypatch):
    project, path = lease(tmp_path, live=True)
    monkeypatch.setattr(tex_runtime, '_run', lambda *args: pytest.fail('must not touch another live worker container'))
    result = tex_runtime.recover(project)
    assert result[0]['status'] == 'active_owner' and path.exists()


def test_dead_owner_cleanup_requires_labels_and_verified_absence(tmp_path, monkeypatch):
    project, path = lease(tmp_path)
    record = json.loads(path.read_text())
    calls = []
    removed = False
    def run(_docker, *args):
        nonlocal removed
        calls.append(args)
        if args[0] == 'ps':
            return SimpleNamespace(returncode=0, stdout='' if removed else 'container-id')
        if args[0] == 'inspect':
            return SimpleNamespace(returncode=0, stdout=json.dumps(record['labels']))
        assert args == ('rm', '-f', record['container_name'])
        removed = True
        return SimpleNamespace(returncode=0, stdout='')
    monkeypatch.setattr(tex_runtime, '_run', run)
    result = tex_runtime.recover(project)
    assert result[0]['cleanup_verified'] and not path.exists()
    assert [c[0] for c in calls] == ['ps', 'inspect', 'rm', 'ps']


@pytest.mark.parametrize('unavailable', [False, True])
def test_mismatched_container_or_unavailable_docker_retains_lease(tmp_path, monkeypatch, unavailable):
    project, path = lease(tmp_path)
    calls = []
    def run(_docker, *args):
        calls.append(args[0])
        if unavailable:
            return SimpleNamespace(returncode=1, stdout='')
        if args[0] == 'ps':
            return SimpleNamespace(returncode=0, stdout='foreign-id')
        assert args[0] == 'inspect'
        return SimpleNamespace(returncode=0, stdout=json.dumps({'io.paperpilot.managed': 'tex', 'io.paperpilot.project': 'different-project'}))
    monkeypatch.setattr(tex_runtime, '_run', run)
    result = tex_runtime.recover(project)
    assert not result[0]['cleanup_verified'] and path.exists() and 'rm' not in calls
    assert result[0]['status'] == ('cleanup_unavailable' if unavailable else 'container_identity_conflict')


def test_corrupt_lease_cannot_name_arbitrary_container(tmp_path, monkeypatch):
    project, path = lease(tmp_path)
    record = json.loads(path.read_text())
    record['container_name'] = 'another-service'
    path.write_text(json.dumps(record))
    monkeypatch.setattr(tex_runtime, '_run', lambda *args: pytest.fail('corrupt identity must not reach Docker'))
    assert tex_runtime.recover(project)[0]['status'] == 'invalid_lease'
    assert path.exists()


@pytest.mark.parametrize('name', ['../outside.txt', 'C:/outside.txt', 'main.tex:secret'])
def test_source_path_escape_rejected_before_cache_removal(tmp_path, monkeypatch, name):
    project = tmp_path / 'project'
    work = project / 'run'
    work.mkdir(parents=True)
    existing = work / 'retained.txt'
    existing.write_text('existing controlled cache')
    monkeypatch.setattr(latex.subprocess, 'Popen', lambda *args, **kwargs: pytest.fail('must not launch'))
    result = latex.compile_project_files([{'path': name, 'content': 'invalid'}], project, lambda p: p, work)
    assert result['code'] == 'unsafe_build_path'
    assert existing.read_text() == 'existing controlled cache'
    assert not (tmp_path / 'outside.txt').exists()


def test_external_work_directory_never_recursively_removed(tmp_path):
    outside = tmp_path / 'outside'
    outside.mkdir()
    canary = outside / 'keep.txt'
    canary.write_text('synthetic canary')
    result = latex.compile_project_files([], tmp_path / 'project', lambda p: p, outside)
    assert result['code'] == 'unsafe_build_path' and canary.read_text() == 'synthetic canary'


def test_persistent_cancel_guard_blocks_before_work_or_subprocess(tmp_path, monkeypatch):
    monkeypatch.setattr(latex.subprocess, 'Popen', lambda *a, **k: pytest.fail('cancelled job cannot start'))
    project = tmp_path / 'project'
    result = latex.compile_project_files([{'path': 'main.tex', 'content': 'controlled'}], project, lambda p: p, project / 'work', cancel_check=lambda: True)
    assert result['cancelled'] and not project.exists()


def queued(client, name):
    pid = client.post('/api/projects', json={'name': name}).json()['project']['id']
    conn = app.db()
    with conn:
        snapshot = build_snapshots.create(conn, pid, name + '-snapshot')
        job = build_snapshots.create_job(conn, pid, snapshot['id'], name + '-job', 'draft_preview')
    conn.close()
    return pid, job


def test_recent_dead_workers_recovered_at_startup_but_live_owner_untouched(client):
    pid, job = queued(client, 'restart')
    other, live = queued(client, 'live')
    pdf = app.PROJECTS_DIR / pid / 'build/main.pdf'
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b'preserved-prior-pdf')
    conn = app.db()
    with conn:
        conn.execute("UPDATE build_jobs SET status='running',owner_token='dead-process-birth' WHERE id=?", (job['id'],))
        conn.execute("UPDATE build_jobs SET updated_at='2020-01-01T00:00:00+00:00' WHERE id=?", (live['id'],))
    conn.close()
    app.init_db()
    recovered = client.get(f"/api/projects/{pid}/builds/{job['id']}").json()
    assert recovered['status'] == 'interrupted' and recovered['diagnostics'][0]['code'] == 'worker_restart'
    result = client.post(f'/api/projects/{other}/builds/reconcile?stale_after_seconds=0').json()
    assert result['count'] == 0
    assert client.get(f"/api/projects/{other}/builds/{live['id']}").json()['status'] == 'queued'
    assert pdf.read_bytes() == b'preserved-prior-pdf'


def test_archival_cancels_only_its_jobs_and_rejects_new_compile(client, monkeypatch):
    pid, job = queued(client, 'archived')
    other, untouched = queued(client, 'other')
    calls = []
    monkeypatch.setattr(latex, 'cancel_compile', lambda job_id: calls.append(job_id))
    archived = client.put(f'/api/projects/{pid}/archive')
    assert archived.status_code == 200 and archived.json()['archived']
    assert calls == [job['id']]
    assert client.get(f"/api/projects/{pid}/builds/{job['id']}").json()['status'] == 'cancelled'
    assert client.get(f"/api/projects/{other}/builds/{untouched['id']}").json()['status'] == 'queued'
    denied = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'cannot-run'})
    assert denied.status_code == 409 and denied.json()['detail']['code'] == 'project_archived'


def test_pending_cleanup_blocks_new_run_with_actionable_repair(client, monkeypatch):
    pid = client.post('/api/projects', json={'name': 'pending cleanup'}).json()['project']['id']
    monkeypatch.setattr(tex_runtime, 'recover', lambda *args, **kwargs: [{'status': 'cleanup_unavailable', 'cleanup_verified': False}])
    response = client.post(f'/api/projects/{pid}/compile', json={'request_id': 'blocked'})
    assert response.status_code == 503 and response.json()['detail']['code'] == 'runtime_cleanup_pending'
    assert 'Docker' in response.json()['detail']['message']
    assert client.get(f'/api/projects/{pid}/builds').json() == []


def test_cleanup_failure_is_durable_and_successful_reconcile_closes_guidance(client, monkeypatch):
    pid, job = queued(client, 'cleanup guidance')
    conn = app.db()
    with conn:
        build_snapshots.cancel_job(conn, pid, job['id'], 'cancel')
        build_snapshots.record_runtime_cleanup(conn, pid, [{'job_id': job['id'], 'status': 'cleanup_unavailable', 'cleanup_verified': False}])
    conn.close()
    pending = client.get(f"/api/projects/{pid}/builds/{job['id']}").json()
    assert any(d['code'] == 'runtime_cleanup_pending' for d in pending['diagnostics'])
    monkeypatch.setattr(tex_runtime, 'recover', lambda *a, **k: [{'job_id': job['id'], 'status': 'cleaned', 'cleanup_verified': True}])
    response = client.post(f'/api/projects/{pid}/builds/reconcile')
    assert response.status_code == 200
    fixed = client.get(f"/api/projects/{pid}/builds/{job['id']}").json()
    assert fixed['status'] == 'cancelled' and not any(d['code'] == 'runtime_cleanup_pending' for d in fixed['diagnostics'])
