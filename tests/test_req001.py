from __future__ import annotations

import json
import sqlite3

import app
import pytest


def create_project(client, *, request_id: str | None = None) -> dict:
    body = {"name": "REQ-001 测试论文", "domain": "材料科学", "style": "严谨"}
    if request_id:
        body["request_id"] = request_id
    response = client.post("/api/projects", json=body)
    assert response.status_code == 200, response.text
    return response.json()


def complete_planning(client, project_id: str) -> dict:
    """Confirm all five planning stages, which is what opens human_revision."""
    for _ in range(5):
        view = client.get(f"/api/projects/{project_id}/workflow/startup").json()
        definition = next(s for s in view["definitions"] if s["id"] == view["current_stage"])
        fields = {key: "test-" + key for key in definition["fields"]}
        reviewed = client.post(
            f"/api/projects/{project_id}/workflow/startup/commands",
            json={"command": "review", "request_id": f"plan-review-{view['workflow_version']}",
                  "expected_workflow_version": view["workflow_version"], "stage_id": view["current_stage"],
                  "fields": fields},
        )
        assert reviewed.status_code == 200, reviewed.text
        confirmed = client.post(
            f"/api/projects/{project_id}/workflow/startup/commands",
            json={"command": "confirm", "request_id": f"plan-confirm-{view['workflow_version']}",
                  "expected_workflow_version": reviewed.json()["workflow_version"], "stage_id": view["current_stage"]},
        )
        assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def enter_revision(client, project_id: str) -> dict:
    complete_planning(client, project_id)
    workflow = client.get(f"/api/projects/{project_id}/workflow").json()
    started = client.post(
        f"/api/projects/{project_id}/workflow/commands",
        json={"command": "start", "request_id": f"start-{project_id}", "base_version": workflow["version"]},
    )
    assert started.status_code == 200, started.text
    workflow = started.json()
    entered = client.post(
        f"/api/projects/{project_id}/human-revision",
        json={"workflow_version": workflow["version"]},
    )
    assert entered.status_code == 200, entered.text
    return entered.json()["workflow"]


def save_revision(client, project_id, payload):
    drafts=client.get(f'/api/projects/{project_id}/human-revision/drafts').json()
    current=next((d for d in drafts if d['target_kind']==payload['target_kind'] and d['target_path']==payload['target_path']),None)
    return client.post(f'/api/projects/{project_id}/human-revision/drafts',json={**payload,'expected_draft_updated_at':current['updated_at'] if current else None})


def revision_snapshot(client, project_id):
    return [{k:d[k] for k in ('target_kind','target_path','updated_at','base_version','mode_id','content')} for d in client.get(f'/api/projects/{project_id}/human-revision/drafts').json()]


def make_candidate(client, monkeypatch, project_id: str, *, request_id: str, content: str = "Generated draft") -> tuple[dict, dict]:
    monkeypatch.setattr(app, "call_ai", lambda *args, **kwargs: {"ok": True, "content": content})
    file_data = client.get(f"/api/projects/{project_id}").json()["files"][0]
    selected_text = "Introduction"
    start = file_data["content"].index(selected_text)
    end = start + len(selected_text)
    preview = client.post(
        f"/api/projects/{project_id}/ai-context",
        json={
            "action": "edit", "target_kind": "file", "target_path": file_data["path"],
            "prompt": "rewrite the introduction", "content": file_data["content"], "selection": selected_text,
        },
    )
    assert preview.status_code == 200, preview.text
    request = {
        "action": "edit",
        "request_id": request_id,
        "prompt": "rewrite the introduction",
        "content": file_data["content"],
        "selection": selected_text,
        "scope": {"targets": [{"kind": "file", "path": file_data["path"], "start": start, "end": end}]},
        "base_file_versions": {file_data["path"]: file_data["version"]},
        "context_version": preview.json()["version"],
    }
    response = client.post(f"/api/projects/{project_id}/ai-jobs", json=request)
    assert response.status_code == 202, response.text
    created = response.json()
    completed = client.get(f"/api/projects/{project_id}/ai-jobs/{created['job']['id']}")
    assert completed.status_code == 200, completed.text
    return completed.json(), request


def test_workspace_create_is_atomic_and_idempotent(client, monkeypatch):
    created = create_project(client, request_id="create-once")
    project_id = created["project"]["id"]
    assert created["workflow_enabled"] is True
    assert created["workspace_run"]["status"] == "completed"
    assert created["workspace_run"]["request_fingerprint"]

    conn = app.db()
    items = conn.execute("SELECT resource_kind,status FROM workflow_work_items WHERE project_id=? ORDER BY resource_kind", (project_id,)).fetchall()
    snapshot_count = conn.execute("SELECT COUNT(*) FROM workflow_snapshots WHERE project_id=?", (project_id,)).fetchone()[0]
    conn.close()
    assert [(row["resource_kind"], row["status"]) for row in items] == [("corpus", "pending"), ("references", "pending"), ("terms", "pending")]
    assert snapshot_count == 1

    replay = create_project(client, request_id="create-once")
    assert replay["project"]["id"] == project_id
    assert replay["workspace_run"]["id"] == created["workspace_run"]["id"]
    conflict = client.post("/api/projects", json={"name": "different", "request_id": "create-once"})
    assert conflict.status_code == 409

    original = app.create_initial_workflow
    app.create_initial_workflow = lambda *args: (_ for _ in ()).throw(RuntimeError("injected"))
    try:
        failure = client.post("/api/projects", json={"name": "retryable", "request_id": "create-failure"})
    finally:
        app.create_initial_workflow = original
    assert failure.status_code == 500
    conn = app.db()
    failed_run = conn.execute("SELECT status FROM workspace_runs WHERE request_id='create-failure'").fetchone()
    no_partial_project = conn.execute("SELECT COUNT(*) FROM projects WHERE name='retryable'").fetchone()[0]
    conn.close()
    assert failed_run["status"] == "failed"
    assert no_partial_project == 0
    retry_response = client.post(
        "/api/projects",
        json={"name": "retryable", "request_id": "create-failure"},
    )
    assert retry_response.status_code == 200, retry_response.text
    retried = retry_response.json()
    assert retried["workspace_run"]["status"] == "completed"


def test_workflow_gate_and_draft_exit_decision(client):
    project_id = create_project(client)["project"]["id"]
    initial = client.get(f"/api/projects/{project_id}/workflow").json()
    assert initial["current_stage"] == "STAGE-01" and initial["phase"] == "planning"
    # Entering revision no longer requires completed planning.
    early = client.post(
        f"/api/projects/{project_id}/human-revision",
        json={"workflow_version": initial["version"]},
    )
    assert early.status_code == 200

    complete_planning(client, project_id)
    ready = client.get(f"/api/projects/{project_id}/workflow").json()
    assert ready["current_stage"] == "human_revision"
    command = {"command": "start", "request_id": "workflow-start-idempotent", "base_version": ready["version"]}
    first_result = client.post(f"/api/projects/{project_id}/workflow/commands", json=command)
    assert first_result.status_code == 200
    replay_result = client.post(f"/api/projects/{project_id}/workflow/commands", json=command)
    assert replay_result.status_code == 200
    assert replay_result.json() == first_result.json()
    reused = client.post(
        f"/api/projects/{project_id}/workflow/commands",
        json={**command, "command": "cancel"},
    )
    assert reused.status_code == 409

    entered = client.post(
        f"/api/projects/{project_id}/human-revision",
        json={"workflow_version": first_result.json()["version"]},
    )
    assert entered.status_code == 200
    workflow = entered.json()["workflow"]
    version_before_mode = workflow["version"]
    workflow = client.get(f"/api/projects/{project_id}/workflow").json()
    assert workflow["current_stage"] == "human_revision"
    assert workflow["version"] == version_before_mode
    assert workflow["human_revision_mode"]["status"] == "active"

    invalid = client.post(
        f"/api/projects/{project_id}/workflow/commands",
        json={"command": "confirm", "request_id": "invalid-confirm", "base_version": workflow["version"],"expected_drafts":[]},
    )
    assert invalid.status_code == 409
    assert client.get(f"/api/projects/{project_id}/workflow").json()["version"] == workflow["version"]

    file_data = client.get(f"/api/projects/{project_id}").json()["files"][0]
    draft = save_revision(client,project_id,{"target_kind": "file", "target_path": file_data["path"], "content": "recover me", "base_version": file_data["version"]})
    assert draft.status_code == 200
    reviewed = client.post(
        f"/api/projects/{project_id}/workflow/commands",
        json={"command": "review", "request_id": "review-once", "base_version": workflow["version"]},
    ).json()
    no_decision = client.post(
        f"/api/projects/{project_id}/workflow/commands",
        json={"command": "confirm", "request_id": "confirm-missing-draft-action", "base_version": reviewed["version"],"expected_drafts":revision_snapshot(client,project_id)},
    )
    assert no_decision.status_code == 409
    kept = client.post(
        f"/api/projects/{project_id}/workflow/commands",
        json={"command": "confirm", "request_id": "confirm-keep-drafts", "base_version": reviewed["version"], "draft_action": "keep","expected_drafts":revision_snapshot(client,project_id)},
    )
    assert kept.status_code == 200, kept.text
    assert kept.json()["current_stage"] == "stage_confirmed"
    assert kept.json()["human_revision_mode"] is None
    assert client.get(f"/api/projects/{project_id}/human-revision/drafts").json()[0]["content"] == "recover me"


def test_outdated_revision_clients_cannot_mutate_or_discard_drafts(client):
    pid=create_project(client)['project']['id'];workflow=enter_revision(client,pid)
    file=client.get(f'/api/projects/{pid}').json()['files'][0]
    payload={'target_kind':'file','target_path':file['path'],'base_version':file['version'],'content':'应保留的草稿'}
    saved=save_revision(client,pid,payload);assert saved.status_code==200
    for url,body in [('/human-revision/drafts',{**payload,'content':'旧页面覆盖'}),('/human-revision/checkpoint',{k:payload[k] for k in ('target_kind','target_path','base_version')}),('/human-revision/exit',{'draft_action':'discard'}),('/workflow/commands',{'command':'cancel','request_id':'legacy-cancel','base_version':workflow['version'],'draft_action':'discard'})]:
        response=client.post(f'/api/projects/{pid}{url}',json=body)
        assert response.status_code==428 and response.json()['detail']['code']=='revision_client_outdated'
    assert client.get(f'/api/projects/{pid}/human-revision/drafts').json()[0]['content']=='应保留的草稿'
    assert client.get(f'/api/projects/{pid}/workflow').json()['human_revision_mode']['status']=='active'
    assert client.get(f'/api/projects/{pid}').json()['files'][0]['content']==file['content']


@pytest.mark.parametrize('action',['commit','discard','keep'])
def test_exit_rejects_unreviewed_changes_to_draft_set(client, action):
    pid=create_project(client)['project']['id'];enter_revision(client,pid)
    file=client.get(f'/api/projects/{pid}').json()['files'][0]
    payload={'target_kind':'file','target_path':file['path'],'base_version':file['version'],'content':'初始'}
    assert save_revision(client,pid,payload).status_code==200
    drafts=client.get(f'/api/projects/{pid}/human-revision/drafts').json()
    snapshot=[{k:d[k] for k in ('target_kind','target_path','updated_at','base_version','mode_id','content')} for d in drafts]
    assert save_revision(client,pid,{**payload,'content':'其他页面更新'}).status_code==200
    conn=app.db()
    with conn: conn.execute('UPDATE revision_drafts SET updated_at=? WHERE project_id=?',(snapshot[0]['updated_at'],pid))
    conn.close()
    result=client.post(f'/api/projects/{pid}/human-revision/exit',json={'draft_action':action,'expected_drafts':snapshot})
    assert result.status_code==409 and result.json()['detail']['code']=='draft_set_changed'
    assert client.get(f'/api/projects/{pid}/human-revision/drafts').json()[0]['content']=='其他页面更新'
    assert client.get(f'/api/projects/{pid}/workflow').json()['human_revision_mode']['status']=='active'

def test_workflow_exit_checks_empty_snapshot_and_idempotency(client):
    pid=create_project(client)['project']['id'];workflow=enter_revision(client,pid)
    reviewed=client.post(f'/api/projects/{pid}/workflow/commands',json={'command':'review','request_id':'review-snapshot','base_version':workflow['version']}).json()
    file=client.get(f'/api/projects/{pid}').json()['files'][0]
    save_revision(client,pid,{'target_kind':'file','target_path':file['path'],'base_version':file['version'],'content':'新草稿'})
    command={'command':'confirm','request_id':'confirm-snapshot','base_version':reviewed['version'],'draft_action':'discard','expected_drafts':[]}
    rejected=client.post(f'/api/projects/{pid}/workflow/commands',json=command)
    assert rejected.status_code==409 and rejected.json()['detail']['code']=='draft_set_changed'
    drafts=client.get(f'/api/projects/{pid}/human-revision/drafts').json();command['expected_drafts']=[{k:d[k] for k in ('target_kind','target_path','updated_at','base_version','mode_id','content')} for d in drafts]
    first=client.post(f'/api/projects/{pid}/workflow/commands',json=command);assert first.status_code==200,first.text
    assert client.post(f'/api/projects/{pid}/workflow/commands',json=command).json()==first.json()
    assert client.post(f'/api/projects/{pid}/workflow/commands',json={**command,'expected_drafts':[]}).status_code==409


def test_concurrent_revision_drafts_keep_independent_recovery_copies(client, monkeypatch):
    project_id = create_project(client)["project"]["id"]
    enter_revision(client, project_id)
    monkeypatch.setattr('paperpilot.services.human_revision.timestamp', lambda: '2026-01-01T00:00:00+00:00')
    file_data = next(item for item in client.get(f"/api/projects/{project_id}").json()["files"] if item["path"] == "references.bib")
    payload = {"target_kind": "file", "target_path": file_data["path"], "base_version": file_data["version"], "expected_draft_updated_at": None}
    first = client.post(f"/api/projects/{project_id}/human-revision/drafts", json={**payload, "content": "page A", "copy_id": "page-a"})
    second = client.post(f"/api/projects/{project_id}/human-revision/drafts", json={**payload, "content": "page B", "copy_id": "page-b"})
    assert first.status_code == 200 and second.status_code == 409
    assert second.json()['detail']['copy_id'] == 'page-b'
    assert client.get(f"/api/projects/{project_id}/human-revision/drafts").json()[0]['content'] == 'page A'
    copies = client.get(f"/api/projects/{project_id}/human-revision/draft-copies").json()
    assert {x['content'] for x in copies} == {'page A', 'page B'}
    original_a=next(x for x in copies if x['copy_id']=='page-a')
    original_b=next(x for x in copies if x['copy_id']=='page-b')
    again=client.post(f"/api/projects/{project_id}/human-revision/drafts", json={**payload, "content":"page B again", "copy_id":"page-b"})
    assert again.status_code==409
    assert client.delete(f"/api/projects/{project_id}/human-revision/draft-copies/page-b",params={'updated_at':original_b['updated_at']}).status_code==409
    latest=first.json()['saved_at']
    updated=client.post(f"/api/projects/{project_id}/human-revision/drafts", json={**payload, 'expected_draft_updated_at':latest,'content':'page A updated','copy_id':'page-a'})
    assert updated.status_code == 200
    copies = client.get(f"/api/projects/{project_id}/human-revision/draft-copies").json()
    assert {x['content'] for x in copies} == {'page A updated', 'page B again'}
    stale_delete=client.delete(f"/api/projects/{project_id}/human-revision/draft-copies/page-a",params={'updated_at':original_a['updated_at']})
    assert stale_delete.status_code == 409
    current_a=next(x for x in copies if x['copy_id']=='page-a')
    assert client.delete(f"/api/projects/{project_id}/human-revision/draft-copies/page-a",params={'updated_at':current_a['updated_at']}).json()['deleted']
    assert client.get(f"/api/projects/{project_id}/human-revision/draft-copies").json()[0]['content']=='page B again'


def test_checkpoint_rejects_draft_changed_after_save(client):
    project_id = create_project(client)["project"]["id"]
    enter_revision(client, project_id)
    file_data = next(item for item in client.get(f"/api/projects/{project_id}").json()["files"] if item["path"] == "references.bib")
    payload = {"target_kind": "file", "target_path": file_data["path"], "base_version": file_data["version"]}
    first = save_revision(client,project_id,{**payload, "content": "first page"}).json()
    second = save_revision(client,project_id,{**payload, "content": "second page"}).json()
    stale = client.post(f"/api/projects/{project_id}/human-revision/checkpoint", json={**payload, "expected_draft_updated_at": first["saved_at"]})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "draft_changed"
    assert client.get(f"/api/projects/{project_id}/human-revision/drafts").json()[0]["content"] == "second page"
    current = client.post(f"/api/projects/{project_id}/human-revision/checkpoint", json={**payload, "expected_draft_updated_at": second["saved_at"]})
    assert current.status_code == 200
    assert next(item for item in client.get(f"/api/projects/{project_id}").json()["files"] if item["path"] == file_data["path"])["content"] == "second page"


def test_content_versions_checkpoint_undo_redo_and_delete_restore(client):
    project_id = create_project(client)["project"]["id"]
    enter_revision(client, project_id)
    files = client.get(f"/api/projects/{project_id}").json()["files"]
    file_data = next(item for item in files if item["path"] == "references.bib")
    path = file_data["path"]
    first = client.put(
        f"/api/projects/{project_id}/files",
        json={"path": path, "content": "first edit", "base_version": file_data["version"]},
    )
    assert first.status_code == 200, first.text
    assert first.json()["version"] == 2
    conflict = client.put(
        f"/api/projects/{project_id}/files",
        json={"path": path, "content": "stale overwrite", "base_version": 1},
    )
    assert conflict.status_code == 409

    draft = save_revision(client,project_id,{"target_kind": "file", "target_path": path, "content": "checkpointed edit", "base_version": 2})
    assert draft.status_code == 200
    checkpoint = client.post(
        f"/api/projects/{project_id}/human-revision/checkpoint",
        json={"target_kind": "file", "target_path": path, "base_version": 2,"expected_draft_updated_at":draft.json()['saved_at']},
    )
    assert checkpoint.status_code == 200
    assert checkpoint.json()["version"] == 3
    assert client.get(f"/api/projects/{project_id}/human-revision/drafts").json() == []

    undo = client.post(
        f"/api/projects/{project_id}/versions/restore",
        json={"target_kind": "file", "target_path": path, "target_version": 2, "base_version": 3, "action": "undo"},
    )
    assert undo.status_code == 200, undo.text
    redo = client.post(
        f"/api/projects/{project_id}/versions/restore",
        json={"target_kind": "file", "target_path": path, "target_version": 3, "base_version": 4, "action": "redo"},
    )
    assert redo.status_code == 200, redo.text
    assert next(item for item in client.get(f"/api/projects/{project_id}").json()["files"] if item["path"] == path)["content"] == "checkpointed edit"

    deleted = client.delete(f"/api/projects/{project_id}/files/{path}?base_version=5")
    assert deleted.status_code == 200, deleted.text
    assert path not in {item["path"] for item in client.get(f"/api/projects/{project_id}").json()["files"]}
    restored = client.post(
        f"/api/projects/{project_id}/versions/restore",
        json={"target_kind": "file", "target_path": path, "target_version": 5, "base_version": 6, "action": "undo"},
    )
    assert restored.status_code == 200, restored.text
    reopened = client.get(f"/api/projects/{project_id}").json()
    assert next(item for item in reopened["files"] if item["path"] == path)["content"] == "checkpointed edit"


def test_ai_candidate_is_reviewed_idempotent_and_does_not_advance_workflow(client, monkeypatch):
    project_id = create_project(client)["project"]["id"]
    before = enter_revision(client, project_id)
    created, request = make_candidate(client, monkeypatch, project_id, request_id="job-one")
    job_id = created["job"]["id"]
    job = client.get(f"/api/projects/{project_id}/ai-jobs/{job_id}").json()
    assert job["job"]["status"] == "succeeded"
    candidate = job["candidate"]
    assert candidate["status"] == "candidate_ready"
    assert candidate["context_sources"]
    current = client.get(f"/api/projects/{project_id}").json()
    assert current["files"][0]["content"] != candidate["patches"][0]["content"]
    assert client.get(f"/api/projects/{project_id}/workflow").json()["version"] == before["version"]
    replay = client.post(f"/api/projects/{project_id}/ai-jobs", json=request)
    assert replay.status_code == 202
    assert replay.json()["created"] is False
    assert replay.json()["job"]["id"] == job_id
    changed = dict(request, prompt="different prompt")
    conflict = client.post(f"/api/projects/{project_id}/ai-jobs", json=changed)
    assert conflict.status_code == 409

    decision = {
        "decision": "accept", "request_id": "decision-one",
        "base_file_versions": candidate["base_file_versions"],
        "allow_warnings": True,
    }
    applied = client.post(f"/api/projects/{project_id}/candidates/{candidate['id']}/decision", json=decision)
    assert applied.status_code == 200, applied.text
    assert applied.json()["content_versions"]["main.tex"] == 2
    version_count = len(client.get(f"/api/projects/{project_id}/versions?target_kind=file&target_path=main.tex").json())
    replayed_decision = client.post(f"/api/projects/{project_id}/candidates/{candidate['id']}/decision", json=decision)
    assert replayed_decision.status_code == 200
    assert replayed_decision.json()["idempotent_replay"] is True
    assert len(client.get(f"/api/projects/{project_id}/versions?target_kind=file&target_path=main.tex").json()) == version_count
    assert client.get(f"/api/projects/{project_id}/workflow").json()["version"] == before["version"]


def test_multi_target_candidate_apply_rolls_back_when_a_later_target_write_fails(client, monkeypatch):
    project_id = create_project(client)["project"]["id"]
    enter_revision(client, project_id)
    files = {item["path"]: item for item in client.get(f"/api/projects/{project_id}").json()["files"]}
    replacements = {
        "main.tex": files["main.tex"]["content"].replace("Introduction", "Revised introduction"),
        "references.bib": "@article{rollbackcheck, title={Rollback check}}\n",
    }
    monkeypatch.setattr(
        app,
        "call_ai",
        lambda *args, **kwargs: {"ok": True, "content": json.dumps(replacements, ensure_ascii=False)},
    )
    targets = ["main.tex", "references.bib"]
    preflight = client.post(
        f"/api/projects/{project_id}/ai-context",
        json={
            "action": "rewrite", "prompt": "Rewrite the introduction and update references.",
            "targets": [
                {"kind": "file", "path": path, "content": files[path]["content"]}
                for path in targets
            ],
        },
    )
    assert preflight.status_code == 200, preflight.text
    created = client.post(
        f"/api/projects/{project_id}/ai-jobs",
        json={
            "action": "rewrite",
            "request_id": "bundle-rollback",
            "prompt": "Rewrite the introduction and update references.",
            "scope": {
                "targets": [
                    {
                        "kind": "file", "path": path, "start": 0,
                        "end": len(files[path]["content"]),
                    }
                    for path in targets
                ]
            },
            "base_file_versions": {path: files[path]["version"] for path in targets},
            "context_version": preflight.json()["version"],
        },
    )
    assert created.status_code == 202, created.text
    job = client.get(f"/api/projects/{project_id}/ai-jobs/{created.json()['job']['id']}").json()
    assert job["job"]["status"] == "succeeded"
    candidate = job["candidate"]

    conn = app.db()
    conn.execute(
        "CREATE TRIGGER fail_references_write BEFORE UPDATE ON files "
        "WHEN NEW.path='references.bib' BEGIN SELECT RAISE(ABORT, 'injected write failure'); END"
    )
    conn.commit()
    conn.close()

    failed=client.post(
            f"/api/projects/{project_id}/candidates/{candidate['id']}/decision",
            json={
                "decision": "accept", "request_id": "bundle-rollback-decision",
                "base_file_versions": candidate["base_file_versions"],
            },
        )
    assert failed.status_code==503 and failed.json()['detail']['code']=='storage_unavailable'

    current = {item["path"]: item for item in client.get(f"/api/projects/{project_id}").json()["files"]}
    assert {path: current[path]["content"] for path in targets} == {
        path: files[path]["content"] for path in targets
    }
    assert {path: current[path]["version"] for path in targets} == {
        path: files[path]["version"] for path in targets
    }
    unchanged_candidate = client.get(
        f"/api/projects/{project_id}/candidates/{candidate['id']}"
    ).json()
    assert unchanged_candidate["status"] == "candidate_ready"


def test_human_revision_exposes_confirmed_assist_operation_catalog(client, monkeypatch):
    project_id = create_project(client)["project"]["id"]
    enter_revision(client, project_id)
    monkeypatch.setattr(app, "call_ai", lambda *args, **kwargs: {"ok": True, "content": "A reviewed suggestion"})
    file_data = client.get(f"/api/projects/{project_id}").json()["files"][0]
    start = file_data["content"].index("Introduction")
    operations = (
        "rewrite", "expand_or_condense", "style_or_format", "term_logic_check",
        "citation_compare", "explain_or_suggest", "follow_write",
    )
    for index, action in enumerate(operations):
        preview = client.post(
            f"/api/projects/{project_id}/ai-context",
            json={"action": action, "target_kind": "file", "target_path": file_data["path"], "content": file_data["content"], "selection": "Introduction"},
        )
        assert preview.status_code == 200, preview.text
        response = client.post(
            f"/api/projects/{project_id}/ai-jobs",
            json={
                "action": action, "request_id": f"assist-{index}",
                "content": file_data["content"], "selection": "Introduction",
                "scope": {"targets": [{"kind": "file", "path": file_data["path"], "start": start, "end": start + len("Introduction")}]},
                "base_file_versions": {file_data["path"]: file_data["version"]},
                "context_version": preview.json()["version"],
            },
        )
        assert response.status_code == 202, response.text
    jobs = client.get(f"/api/projects/{project_id}/ai-jobs?limit=20").json()
    persisted_actions = {item["job"]["action"] for item in jobs if item["job"]["request_id"].startswith("assist-")}
    assert persisted_actions == set(operations)
    assert all(item["candidate"] and item["candidate"]["status"] == "candidate_ready" for item in jobs)


def test_ai_worker_sends_persisted_relevant_context_and_excludes_unrelated_material(client, monkeypatch):
    project_id = create_project(client)["project"]["id"]
    workflow = enter_revision(client, project_id)
    file_data = client.get(f"/api/projects/{project_id}").json()["files"][0]
    body = file_data["content"].replace("Introduction", "Graphene interface stability")
    saved = client.put(
        f"/api/projects/{project_id}/files",
        json={"path": file_data["path"], "content": body, "base_version": file_data["version"]},
    )
    assert saved.status_code == 200
    current = next(item for item in client.get(f"/api/projects/{project_id}").json()["files"] if item["path"] == file_data["path"])
    client.post(f"/api/projects/{project_id}/terms", json={"term": "graphene interface", "definition": "interfacial stability"})
    client.post(f"/api/projects/{project_id}/terms", json={"term": "Bayesian prior", "definition": "unrelated marker"})
    client.post(
        f"/api/projects/{project_id}/corpus",
        json={"title": "Graphene interface experiment", "content": "RELEVANT_CORPUS_MARKER: measured interface stability."},
    )
    client.post(
        f"/api/projects/{project_id}/corpus",
        json={"title": "Unrelated meteorology", "content": "EXCLUDED_CORPUS_MARKER: atmospheric observation."},
    )
    client.post(
        f"/api/projects/{project_id}/refs",
        json={"cite_key": "graphene2025", "title": "Graphene Interface Stability", "authors": "A. Researcher", "year": "2025"},
    )
    client.post(
        f"/api/projects/{project_id}/refs",
        json={"cite_key": "weather2025", "title": "Weather Forecasting", "year": "2025"},
    )
    selection = "Graphene interface stability"
    start = current["content"].index(selection)
    end = start + len(selection)
    capture: dict[str, str] = {}

    def provider(action, prompt, content, selected, model, base_url=None, context="", max_tokens=None):
        capture.update(action=action, prompt=prompt, content=content, selected=selected, context=context, model=model or "", max_tokens=str(max_tokens))
        return {"ok": True, "content": "Revised interface", "usage": {"prompt_tokens": 321, "completion_tokens": 8, "total_tokens": 329}}

    monkeypatch.setattr(app, "call_ai", provider)
    preview = client.post(
        f"/api/projects/{project_id}/ai-context",
        json={
            "action": "follow_write", "target_kind": "file", "target_path": current["path"],
            "content": current["content"], "selection": selection, "prompt": "Compare graphene interface evidence.",
        },
    )
    assert preview.status_code == 200, preview.text
    before_workflow_version = client.get(f"/api/projects/{project_id}/workflow").json()["version"]
    response = client.post(
        f"/api/projects/{project_id}/ai-jobs",
        json={
            "action": "follow_write", "request_id": "context-provenance-job",
            "prompt": "Compare graphene interface evidence.", "content": current["content"], "selection": selection,
            "scope": {"targets": [{"kind": "file", "path": current["path"], "start": start, "end": end}]},
            "base_file_versions": {current["path"]: current["version"]},
            "context_version": preview.json()["version"],
        },
    )
    assert response.status_code == 202, response.text
    result = client.get(f"/api/projects/{project_id}/ai-jobs/{response.json()['job']['id']}").json()
    assert result["job"]["status"] == "succeeded"
    assert capture["action"] == "follow_write"
    assert capture["selected"] == selection
    assert "大纲（完整）" in capture["context"]
    assert "全局逻辑链路（完整）" in capture["context"]
    assert "相关语料（不可信引用资料" in capture["context"]
    assert "RELEVANT_CORPUS_MARKER" in capture["context"]
    assert "graphene2025=Graphene Interface Stability" in capture["context"]
    assert "EXCLUDED_CORPUS_MARKER" not in capture["context"]
    assert "weather2025=Weather Forecasting" not in capture["context"]
    conn = app.db()
    snapshot = conn.execute(
        "SELECT context_text,context_version,source_manifest_json FROM context_snapshots WHERE id=?",
        (result["job"]["context_snapshot_id"],),
    ).fetchone()
    conn.close()
    assert snapshot["context_text"] == capture["context"]
    assert snapshot["context_version"] == preview.json()["version"]
    manifest = json.loads(snapshot["source_manifest_json"])
    assert any(item.get("label") == "Graphene interface experiment" for item in manifest)
    assert not any(item.get("label") == "Unrelated meteorology" for item in manifest)
    assert client.get(f"/api/projects/{project_id}/workflow").json()["version"] == before_workflow_version == workflow["version"]


def test_stale_candidate_exposes_three_way_state_and_cannot_overwrite(client, monkeypatch):
    project_id = create_project(client)["project"]["id"]
    enter_revision(client, project_id)
    created, _ = make_candidate(client, monkeypatch, project_id, request_id="job-stale", content="AI text")
    candidate = created["candidate"]
    file_data = client.get(f"/api/projects/{project_id}").json()["files"][0]
    changed = client.put(
        f"/api/projects/{project_id}/files",
        json={"path": file_data["path"], "content": "human edit", "base_version": file_data["version"]},
    )
    assert changed.status_code == 200
    stale = client.get(f"/api/projects/{project_id}/candidates/{candidate['id']}").json()
    assert stale["status"] == "stale"
    assert stale["stale"] is True
    assert stale["patches"][0]["base_content"] == file_data["content"]
    assert stale["patches"][0]["current_content"] == "human edit"
    decision = client.post(
        f"/api/projects/{project_id}/candidates/{candidate['id']}/decision",
        json={"decision": "accept", "request_id": "stale-decision", "base_file_versions": candidate["base_file_versions"], "allow_warnings": True},
    )
    assert decision.status_code == 409
    assert client.get(f"/api/projects/{project_id}").json()["files"][0]["content"] == "human edit"


def test_candidate_preapply_validation_blocks_structure_and_requires_warning_override(client, monkeypatch):
    project_id = create_project(client)["project"]["id"]
    enter_revision(client, project_id)
    broken, _ = make_candidate(client, monkeypatch, project_id, request_id="job-broken-tex", content="Broken{")
    broken_candidate = broken["candidate"]
    assert broken_candidate["validation"]["hard_errors"]
    original = client.get(f"/api/projects/{project_id}").json()["files"][0]["content"]
    blocked = client.post(
        f"/api/projects/{project_id}/candidates/{broken_candidate['id']}/decision",
        json={"decision": "accept", "request_id": "broken-apply", "base_file_versions": broken_candidate["base_file_versions"]},
    )
    assert blocked.status_code == 422
    assert blocked.json()["detail"]["code"] == "candidate_validation_failed"
    assert client.get(f"/api/projects/{project_id}").json()["files"][0]["content"] == original

    with_warning, _ = make_candidate(
        client, monkeypatch, project_id, request_id="job-missing-citation",
        content=r"\cite{missing-key}",
    )
    warning_candidate = with_warning["candidate"]
    assert warning_candidate["validation"]["warnings"][0]["code"] == "missing_citations"
    not_overridden = client.post(
        f"/api/projects/{project_id}/candidates/{warning_candidate['id']}/decision",
        json={"decision": "accept", "request_id": "warning-block", "base_file_versions": warning_candidate["base_file_versions"]},
    )
    assert not_overridden.status_code == 409
    assert not_overridden.json()["detail"]["code"] == "candidate_validation_warning"
    applied = client.post(
        f"/api/projects/{project_id}/candidates/{warning_candidate['id']}/decision",
        json={
            "decision": "accept", "request_id": "warning-override",
            "base_file_versions": warning_candidate["base_file_versions"], "allow_warnings": True,
        },
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["validation"]["warnings"]
    events = client.get(f"/api/projects/{project_id}/human-revision/events").json()
    decision_event = next(item for item in events if item.get("request_id") == "warning-override")
    assert decision_event["scope"]["allow_warnings"] is True
    assert decision_event["scope"]["validation_warning_codes"] == ["missing_citations"]


def test_job_cancel_restart_recovery_and_retry(client, monkeypatch):
    project_id = create_project(client)["project"]["id"]
    enter_revision(client, project_id)
    original_worker = app.process_ai_job
    monkeypatch.setattr(app, "process_ai_job", lambda *_args: None)
    file_data = client.get(f"/api/projects/{project_id}").json()["files"][0]
    context = client.post(
        f"/api/projects/{project_id}/ai-context",
        json={
            "action": "edit", "target_kind": "file", "target_path": file_data["path"],
            "content": file_data["content"], "selection": file_data["content"][:12],
        },
    ).json()
    body = {
        "action": "edit", "request_id": "job-cancel", "scope": {"targets": [{"kind": "file", "path": file_data["path"], "start": 0, "end": 12}]},
        "base_file_versions": {file_data["path"]: file_data["version"]}, "context_version": context["version"],
        "selection": file_data["content"][:12],
    }
    queued = client.post(f"/api/projects/{project_id}/ai-jobs", json=body)
    assert queued.status_code == 202
    job_id = queued.json()["job"]["id"]
    cancelled = client.post(f"/api/projects/{project_id}/ai-jobs/{job_id}/cancel")
    assert cancelled.status_code == 200
    monkeypatch.setattr(app, "process_ai_job", original_worker)
    original_worker(project_id, job_id)
    assert client.get(f"/api/projects/{project_id}/ai-jobs/{job_id}").json()["job"]["status"] == "cancelled"

    monkeypatch.setattr(app, "process_ai_job", lambda *_args: None)
    body["request_id"] = "job-interrupted"
    running = client.post(f"/api/projects/{project_id}/ai-jobs", json=body)
    assert running.status_code == 202
    interrupted_id = running.json()["job"]["id"]
    app.init_db()
    assert client.get(f"/api/projects/{project_id}/ai-jobs/{interrupted_id}").json()["job"]["status"] == "running"
    conn = app.db()
    with conn:
        conn.execute("UPDATE ai_jobs SET owner_token='old-process-birth' WHERE id=?", (interrupted_id,))
    conn.close()
    app.init_db()
    recovered = client.get(f"/api/projects/{project_id}/ai-jobs/{interrupted_id}").json()["job"]
    assert recovered["status"] == "failed"
    assert recovered["error_code"] == "interrupted"

    monkeypatch.setattr(app, "process_ai_job", original_worker)
    monkeypatch.setattr(app, "call_ai", lambda *args, **kwargs: {"ok": True, "content": "retried"})
    retry = client.post(
        f"/api/projects/{project_id}/ai-jobs/{interrupted_id}/retry",
        json={"request_id": "job-interrupted-retry"},
    )
    assert retry.status_code == 202, retry.text
    retried_job = client.get(f"/api/projects/{project_id}/ai-jobs/{retry.json()['job']['id']}").json()
    assert retried_job["job"]["status"] == "succeeded"
    assert retried_job["candidate"]["status"] == "candidate_ready"


def test_context_snapshot_uses_relevant_sources_and_redacts_secrets(client):
    project_id = create_project(client)["project"]["id"]
    client.post(f"/api/projects/{project_id}/terms", json={"term": "graphene", "definition": "two-dimensional material"})
    client.post(f"/api/projects/{project_id}/terms", json={"term": "Bayes", "definition": "statistical method"})
    client.post(f"/api/projects/{project_id}/corpus", json={"title": "Graphene notes", "content": "api_key=sk-abcdefghijklmnopqrstuvwxyz0123456789"})
    preview = client.post(
        f"/api/projects/{project_id}/ai-context",
        json={"action": "edit", "target_kind": "file", "target_path": "main.tex", "prompt": "graphene stability"},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert "graphene=two-dimensional material" in body["preview"]
    assert "Bayes=statistical method" not in body["preview"]
    assert "[REDACTED]" in body["preview"]
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in body["preview"]
    assert body["redactions"] == 1
    assert body["estimated_input_tokens"] > 0
    assert body["cost_status"] == "unknown"

    enter_revision(client, project_id)
    file_data = client.get(f"/api/projects/{project_id}").json()["files"][0]
    request = {
        "action": "edit", "request_id": "redacted-input",
        "prompt": "rewrite this", "selection": "api_key=sk-abcdefghijklmnopqrstuvwxyz0123456789",
        "scope": {"targets": [{"kind": "file", "path": file_data["path"], "start": 0, "end": 12}]},
        "base_file_versions": {file_data["path"]: file_data["version"]},
    }
    preflight = client.post(
        f"/api/projects/{project_id}/ai-context",
        json={
            "action": request["action"], "target_kind": "file", "target_path": file_data["path"],
            "prompt": request["prompt"], "content": file_data["content"], "selection": request["selection"],
        },
    )
    assert preflight.status_code == 200, preflight.text
    request["context_version"] = preflight.json()["version"]
    created = client.post(f"/api/projects/{project_id}/ai-jobs", json=request)
    assert created.status_code == 202, created.text
    conn = app.db()
    stored = conn.execute("SELECT input_json FROM ai_jobs WHERE id=?", (created.json()["job"]["id"],)).fetchone()
    conn.close()
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in stored["input_json"]
    assert "[REDACTED]" in stored["input_json"]


def test_request_hard_token_limit_blocks_before_worker_or_provider(client, monkeypatch):
    project_id = create_project(client)["project"]["id"]
    enter_revision(client, project_id)
    file_data = client.get(f"/api/projects/{project_id}").json()["files"][0]
    preview = client.post(
        f"/api/projects/{project_id}/ai-context",
        json={"action": "edit", "target_kind": "file", "target_path": file_data["path"]},
    ).json()
    monkeypatch.setenv("PWA_AI_MAX_JOB_TOKENS", "5")
    response = client.post(
        f"/api/projects/{project_id}/ai-jobs",
        json={
            "action": "edit", "request_id": "over-hard-limit",
            "scope": {"targets": [{"kind": "file", "path": file_data["path"], "start": 0, "end": 10}]},
            "base_file_versions": {file_data["path"]: file_data["version"]},
            "context_version": preview["version"],
        },
    )
    assert response.status_code == 413
    assert response.json()["detail"]["code"] == "budget_exceeded"
    assert client.get(f"/api/projects/{project_id}/ai-jobs?limit=20").json() == []


def test_provider_output_cap_and_usage_are_enforced_and_audited(client, monkeypatch):
    project_id = create_project(client)["project"]["id"]
    enter_revision(client, project_id)
    file_data = client.get(f"/api/projects/{project_id}").json()["files"][0]
    selected = "Introduction"
    start = file_data["content"].index(selected)
    end = start + len(selected)
    preview = client.post(
        f"/api/projects/{project_id}/ai-context",
        json={
            "action": "edit", "target_kind": "file", "target_path": file_data["path"],
            "content": file_data["content"], "selection": selected,
        },
    ).json()
    captured = {}

    def within_limit(*args, **kwargs):
        captured["max_tokens"] = kwargs["max_tokens"]
        return {"ok": True, "content": "Introduction revised", "usage": {"prompt_tokens": 120, "completion_tokens": 9, "total_tokens": 129}}

    monkeypatch.setattr(app, "call_ai", within_limit)
    request = {
        "action": "edit", "request_id": "usage-within-limit", "content": file_data["content"],
        "selection": selected, "prompt": "", "context_version": preview["version"],
        "scope": {"targets": [{"kind": "file", "path": file_data["path"], "start": start, "end": end}]},
        "base_file_versions": {file_data["path"]: file_data["version"]},
    }
    response = client.post(f"/api/projects/{project_id}/ai-jobs", json=request)
    assert response.status_code == 202
    assert captured["max_tokens"] == 4000
    job = client.get(f"/api/projects/{project_id}/ai-jobs/{response.json()['job']['id']}").json()
    assert job["job"]["usage"]["prompt_tokens"] == 120
    assert job["candidate"]["cost"]["status"] == "unknown"

    def exceeds_limit(*args, **kwargs):
        return {"ok": True, "content": "small text", "usage": {"prompt_tokens": 120, "completion_tokens": 5000, "total_tokens": 5120}}

    monkeypatch.setattr(app, "call_ai", exceeds_limit)
    request["request_id"] = "usage-over-limit"
    blocked = client.post(f"/api/projects/{project_id}/ai-jobs", json=request)
    assert blocked.status_code == 202
    failed = client.get(f"/api/projects/{project_id}/ai-jobs/{blocked.json()['job']['id']}").json()
    assert failed["job"]["status"] == "failed"
    assert failed["job"]["error_code"] == "output_limit_exceeded"
    assert failed["job"]["usage"]["completion_tokens"] == 5000
    assert failed["candidate"] is None
    current = client.get(f"/api/projects/{project_id}").json()
    assert next(item for item in current["files"] if item["path"] == file_data["path"])["content"] == file_data["content"]
