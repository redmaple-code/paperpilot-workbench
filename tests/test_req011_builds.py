from pathlib import Path
import subprocess
import threading
import time

import app
import pytest
from paperpilot.adapters import latex
from tests.planning_helper import complete_planning


def _project(client):
    response = client.post("/api/projects", json={"name": "构建测试", "domain": "材料科学", "style": "严谨"})
    assert response.status_code == 200, response.text
    project_id = response.json()["project"]["id"]
    complete_planning(client, project_id)
    workflow = client.get(f"/api/projects/{project_id}/workflow").json()
    started = client.post(f"/api/projects/{project_id}/workflow/commands", json={"command": "start", "request_id": f"start-{project_id}", "base_version": workflow["version"]})
    assert started.status_code == 200, started.text
    entered = client.post(f"/api/projects/{project_id}/human-revision", json={"workflow_version": started.json()["version"]})
    assert entered.status_code == 200, entered.text
    return project_id


def test_build_snapshot_is_immutable_and_compile_history_keeps_previous_pdf(client, monkeypatch):
    project_id = _project(client)
    before = client.get(f"/api/projects/{project_id}").json()["project"]["project_revision"]
    snapshot = client.post(
        f"/api/projects/{project_id}/build-snapshots",
        json={"request_id": "snapshot-1", "purpose": "draft_preview", "expected_project_revision": before},
    )
    assert snapshot.status_code == 200, snapshot.text
    snapshot = snapshot.json()
    assert snapshot["manifest"]["purpose"] == "draft_preview"

    def successful_compile(files, project_dir, clean_path, work_dir, **_kwargs):
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        (Path(work_dir) / "main.pdf").write_bytes(b"pdf-v1")
        return {"ok": True, "log": "", "pdf_url": "__PDF__"}

    monkeypatch.setattr(app, "compile_project_files", successful_compile)
    built = client.post(
        f"/api/projects/{project_id}/compile",
        json={"request_id": "build-1", "purpose": "draft_preview", "snapshot_id": snapshot["id"]},
    )
    assert built.status_code == 200, built.text
    job_id = built.json()["build_job"]["id"]
    assert built.json()["build_job"]["status"] == "queued"
    assert client.get(f"/api/projects/{project_id}/builds/{job_id}").json()["status"] == "succeeded"
    assert client.get(f"/api/projects/{project_id}/pdf").status_code == 200
    assert client.get(f"/api/projects/{project_id}/builds/{job_id}/pdf").status_code == 200

    updated = client.put(f"/api/projects/{project_id}/files", json={"path": "main.tex", "content": "changed", "base_version": 1})
    assert updated.status_code == 200, updated.text
    old = client.get(f"/api/projects/{project_id}/build-snapshots/{snapshot['id']}").json()
    assert old["files"][0]["content"].startswith("\\documentclass")

    monkeypatch.setattr(app, "compile_project_files", lambda *args, **kwargs: {"ok": False, "log": "syntax error"})
    failed = client.post(f"/api/projects/{project_id}/compile", json={"request_id": "build-2", "purpose": "draft_preview"})
    assert failed.status_code == 200, failed.text
    assert client.get(f"/api/projects/{project_id}/builds/{failed.json()['build_job']['id']}").json()["status"] == "failed"
    assert client.get(f"/api/projects/{project_id}/builds").json()[0]["status"] == "failed"


def test_citation_report_requires_engineering_bib_not_library_presence(client):
    project_id = _project(client)
    reference = client.post("/api/references", json={"title": "Library only reference", "authors": "A. Author", "year": "2024"})
    assert reference.status_code == 200, reference.text
    linked = client.post(
        f"/api/projects/{project_id}/references/{reference.json()['id']}",
        json={"request_id": "link-library-only", "reference_version": 1, "cite_key": "libraryonly2024"},
    )
    assert linked.status_code == 200, linked.text
    project = client.get(f"/api/projects/{project_id}").json()
    main = next(item for item in project["files"] if item["path"] == "main.tex")
    saved_main = client.put(
        f"/api/projects/{project_id}/files",
        json={"path": "main.tex", "content": r"A claim\cite{libraryonly2024}.", "base_version": main["version"]},
    )
    assert saved_main.status_code == 200, saved_main.text
    compared = client.post(f"/api/projects/{project_id}/compare").json()
    assert compared["library_only"] == ["libraryonly2024"]
    assert compared["missing"] == ["libraryonly2024"]
    assert "references.bib" in compared["summary"]


def test_historical_build_can_only_prepare_human_revision_drafts(client, monkeypatch):
    project_id = _project(client)

    def successful_compile(files, project_dir, clean_path, work_dir, **_kwargs):
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        (Path(work_dir) / "main.pdf").write_bytes(b"pdf-restore")
        return {"ok": True, "log": "", "pdf_url": "__PDF__"}

    monkeypatch.setattr(app, "compile_project_files", successful_compile)
    built = client.post(f"/api/projects/{project_id}/compile", json={"request_id": "restore-build", "purpose": "submitted"})
    assert built.status_code == 200, built.text
    job_id = built.json()["build_job"]["id"]
    restored = client.post(f"/api/projects/{project_id}/builds/{job_id}/restore", json={"request_id": "restore-draft"})
    assert restored.status_code == 200, restored.text
    body = restored.json()
    assert body["status"] == "restore_candidate" and body["drafts"]
    current = client.get(f"/api/projects/{project_id}").json()
    assert current["files"][0]["content"].startswith("\\documentclass")
    replay = client.post(f"/api/projects/{project_id}/builds/{job_id}/restore", json={"request_id": "restore-draft"})
    assert replay.status_code == 200 and replay.json()["idempotent_replay"] is True


def test_build_reconcile_marks_stale_worker_jobs_as_interrupted(client):
    from paperpilot.services.tex_runtime import current_owner
    project_id = _project(client)
    snapshot = client.post(f"/api/projects/{project_id}/build-snapshots", json={"request_id": "reconcile-snapshot"}).json()
    conn = app.db()
    conn.execute(
        "INSERT INTO build_jobs(id,project_id,snapshot_id,request_id,purpose,status,created_at,updated_at,owner_pid,owner_token) VALUES(?,?,?,?,?,?,?,?,?,?)",
        ("stale-job", project_id, snapshot["id"], "stale-job-request", "draft_preview", "running", "2020-01-01T00:00:00+00:00", "2020-01-01T00:00:00+00:00",current_owner()['owner_pid'],'dead-birth'),
    )
    conn.commit()
    conn.close()
    reconciled = client.post(f"/api/projects/{project_id}/builds/reconcile?stale_after_seconds=0")
    assert reconciled.status_code == 200, reconciled.text
    assert reconciled.json()["count"] == 1
    job = client.get(f"/api/projects/{project_id}/builds/stale-job").json()
    assert job["status"] == "interrupted"
    assert job["diagnostics"][0]["code"] == "worker_restart"
    replay = client.post(f"/api/projects/{project_id}/builds/reconcile?stale_after_seconds=0").json()
    assert replay["count"] == 0


def test_cancel_compile_terminates_running_process(tmp_path, monkeypatch):
    stopped = threading.Event()
    process_ready = threading.Event()
    result = []

    class FakeProcess:
        returncode = -15

        def __init__(self, *_args, **_kwargs):
            self.alive = True
            process_ready.set()

        def poll(self):
            return None if self.alive else self.returncode

        def communicate(self, timeout=None):
            if self.alive and stopped.wait(timeout):
                self.alive = False
            if self.alive:
                raise subprocess.TimeoutExpired("fake-tex", timeout)
            return "", ""

        def terminate(self):
            self.alive = False
            stopped.set()

        def kill(self):
            self.terminate()

    monkeypatch.setattr(latex.subprocess, "Popen", FakeProcess)
    monkeypatch.setenv("PWA_TEX_SANDBOX_IMAGE", "tex-sandbox:test")
    monkeypatch.setattr(latex, "_stop_container", lambda *_args: stopped.set())
    project_dir = tmp_path / "project"
    work_dir = project_dir / "run"
    files = [{"path": "main.tex", "content": "\\documentclass{article}\\begin{document}x\\end{document}"}]
    worker = threading.Thread(
        target=lambda: result.append(
            latex.compile_project_files(files, project_dir, lambda path: path, work_dir, job_id="cancel-test")
        )
    )
    worker.start()
    assert process_ready.wait(2)
    for _ in range(100):
        with latex._process_lock:
            if "cancel-test" in latex._processes:
                break
        time.sleep(0.01)
    assert latex.cancel_compile("cancel-test") is True
    worker.join(3)
    assert not worker.is_alive()
    assert result[0]["cancelled"] is True


def test_bibliography_build_runs_bibtex_and_latex_passes(tmp_path, monkeypatch):
    commands = []
    monkeypatch.setenv("PWA_TEX_SANDBOX_IMAGE", "tex-sandbox:test")

    class CompletedProcess:
        returncode = 0

        def __init__(self, command, **_kwargs):
            commands.append(command)
            self.command = command
            engine_index = command.index("tex-sandbox:test") + 4
            if command[engine_index] == "pdflatex":
                work_path = Path(command[command.index("--mount") + 1].split("source=", 1)[1].split(",target=", 1)[0])
                (work_path / "main.pdf").write_bytes(b"pdf")

        def poll(self):
            return self.returncode

        def communicate(self, timeout=None):
            return "", ""

        def terminate(self):
            self.returncode = -15

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(latex.subprocess, "Popen", CompletedProcess)
    files = [{
        "path": "main.tex",
        "content": "\\documentclass{article}\\begin{document}x\\bibliography{references}\\end{document}",
    }]
    result = latex.compile_project_files(
        files, tmp_path / "project", lambda path: path, tmp_path / "project/run",
        job_id="bibtex-test",
    )
    assert result["ok"] is True
    commands = [command for command in commands if 'tex-sandbox:test' in command]
    inner = [command[command.index("tex-sandbox:test") + 4:] for command in commands]
    assert all(command[command.index('tex-sandbox:test')+1:command.index('tex-sandbox:test')+3] == ['timeout','--signal=KILL'] for command in commands)
    assert [command[0] for command in inner] == ["pdflatex", "bibtex", "pdflatex", "pdflatex"]
    assert inner[1][1] == "main"
    assert all("-no-shell-escape" in command for command in (inner[0], inner[2], inner[3]))
    assert all("--network=none" in command and "--read-only" in command for command in commands)
    assert all("--cap-drop=ALL" in command and "--pull=never" in command for command in commands)
    assert all(not command[command.index("--mount")+1].endswith(",rw") for command in commands)


def test_compile_fails_closed_when_sandbox_image_is_missing(tmp_path, monkeypatch):
    monkeypatch.delenv("PWA_TEX_SANDBOX_IMAGE", raising=False)
    monkeypatch.setattr(latex.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("must not start host compiler"))
    result = latex.compile_project_files(
        [{"path": "main.tex", "content": "\\documentclass{article}"}],
        tmp_path / "project", lambda path: path, tmp_path / "project/run",
    )
    assert result["ok"] is False
    assert result["code"] == "sandbox_not_configured"
    assert "拒绝直接执行 TeX" in result["log"]

