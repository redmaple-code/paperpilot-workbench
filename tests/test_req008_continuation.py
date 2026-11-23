from __future__ import annotations

import app
from tests.provider_fixtures import authoring_preflight_payload
from tests.planning_helper import complete_planning


def _project(client) -> str:
    response = client.post("/api/projects", json={"name": "续写批次", "domain": "材料科学"})
    assert response.status_code == 200, response.text
    return response.json()["project"]["id"]


def _revision(client, project_id: str) -> None:
    complete_planning(client, project_id)
    workflow = client.get(f"/api/projects/{project_id}/workflow").json()
    started = client.post(
        f"/api/projects/{project_id}/workflow/commands",
        json={"command": "start", "request_id": "continuation-start", "base_version": workflow["version"]},
    )
    assert started.status_code == 200, started.text
    entered = client.post(
        f"/api/projects/{project_id}/human-revision",
        json={"workflow_version": started.json()["version"]},
    )
    assert entered.status_code == 200, entered.text


def _batch_request(client, project_id: str, **overrides):
    project = client.get(f"/api/projects/{project_id}").json()
    file_data = next(item for item in project["files"] if item["path"] == "main.tex")
    graph = client.get(f"/api/projects/{project_id}/outline/structured").json()
    if not graph["nodes"]:
        created = client.post(f"/api/projects/{project_id}/outline/nodes", json={
            "command": "create", "request_id": "continuation-node", "base_version": graph["version"],
            "node": {"id": "continuation-node", "title": "研究背景"},
        })
        assert created.status_code == 200, created.text
        graph = created.json()
    body = {
        "request_id": "continuation-once",
        "target_kind": "file", "target_path": "main.tex",
        "base_version": file_data["version"], "anchor_start": 0, "anchor_end": 0,
        "goal": "补写研究背景", "prompt": "保持学术语气",
        "segment_count": 2, "outline_node_id": graph["nodes"][0]["id"], "outline_version": graph["version"],
    }
    body.update(overrides)
    body=authoring_preflight_payload(client,project_id,body)
    response = client.post(f"/api/projects/{project_id}/continuations", json=body)
    assert response.status_code == 202, response.text
    return response.json()["batch"]


def test_continuation_batch_persists_segments_and_supports_idempotent_replay(client, monkeypatch):
    monkeypatch.setattr(app, "call_ai", lambda *args, **kwargs: {"ok": True, "content": "续写候选"})
    project_id = _project(client)
    _revision(client, project_id)
    batch = _batch_request(client, project_id)
    assert batch["segment_count"] == 2
    assert len(batch["segments"]) == 2
    assert batch["outline_node_id"] == "continuation-node" and batch["outline_version"] == 1
    replay = _batch_request(client, project_id)
    assert replay["id"] == batch["id"]
    assert replay["idempotent_replay"] is True
    current = client.get(f"/api/projects/{project_id}/continuations/{batch['id']}")
    assert current.status_code == 200
    assert current.json()["progress"]["total"] == 2
    assert all(segment["candidate"] for segment in current.json()["segments"])


def test_continuation_segment_decision_reuses_candidate_apply_contract(client, monkeypatch):
    monkeypatch.setattr(app, "call_ai", lambda *args, **kwargs: {"ok": True, "content": "续写候选"})
    project_id = _project(client)
    _revision(client, project_id)
    batch = _batch_request(client, project_id, request_id="continuation-decisions", segment_count=2)
    batch = client.get(f"/api/projects/{project_id}/continuations/{batch['id']}").json()
    segment = next(item for item in batch["segments"] if item["candidate"])
    candidate = segment["candidate"]
    rejected = client.post(
        f"/api/projects/{project_id}/continuations/{batch['id']}/segments/{segment['id']}/decision",
        json={"decision": "reject", "request_id": "continuation-reject", "base_file_versions": candidate["base_file_versions"]},
    )
    assert rejected.status_code == 200, rejected.text
    assert next(item for item in rejected.json()["batch"]["segments"] if item["id"] == segment["id"])["status"] == "rejected"
    other = next(item for item in rejected.json()["batch"]["segments"] if item["candidate"] and item["id"] != segment["id"])
    applied = client.post(
        f"/api/projects/{project_id}/continuations/{batch['id']}/segments/{other['id']}/decision",
        json={"decision": "accept", "request_id": "continuation-accept", "base_file_versions": other["candidate"]["base_file_versions"], "allow_warnings": True},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["batch"]["status"] == "completed"
    main = next(item for item in client.get(f"/api/projects/{project_id}").json()["files"] if item["path"] == "main.tex")
    assert "续写候选" in main["content"]


def test_continuation_budget_pause_requires_explicit_override_and_preserves_source(client, monkeypatch):
    monkeypatch.setattr(app, "call_ai", lambda *args, **kwargs: {"ok": True, "content": "should not run"})
    project_id = _project(client)
    _revision(client, project_id)
    before = client.get(f"/api/projects/{project_id}").json()["files"][0]["content"]
    batch = _batch_request(client, project_id, request_id="continuation-budget", hard_budget_tokens=1)
    assert batch["status"] == "paused"
    assert batch["pause_reason"] == "budget_exceeded"
    blocked = client.post(
        f"/api/projects/{project_id}/continuations/{batch['id']}/resume",
        json={"request_id": "continuation-resume-blocked"},
    )
    assert blocked.status_code == 409
    resumed = client.post(
        f"/api/projects/{project_id}/continuations/{batch['id']}/resume",
        json={"request_id": "continuation-resume-override", "budget_override": True},
    )
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["batch"]["status"] in {"running", "review"}
    assert before == client.get(f"/api/projects/{project_id}").json()["files"][0]["content"]


def test_continuation_requires_current_local_outline_node_before_creating_jobs(client, monkeypatch):
    calls = []
    monkeypatch.setattr(app, "call_ai", lambda *args, **kwargs: calls.append(1))
    project_id = _project(client)
    other = _project(client)
    _revision(client, project_id)
    for pid, node in ((project_id, "local-node"), (other, "foreign-node")):
        created = client.post(f"/api/projects/{pid}/outline/nodes", json={
            "command": "create", "request_id": "node-create", "base_version": 0,
            "node": {"id": node, "title": node},
        })
        assert created.status_code == 200, created.text
    project = client.get(f"/api/projects/{project_id}").json()
    body = authoring_preflight_payload(client, project_id, {
        "request_id": "invalid-node", "target_kind": "file", "target_path": "main.tex",
        "base_version": next(f["version"] for f in project["files"] if f["path"] == "main.tex"),
        "goal": "研究背景", "anchor_start": 0, "anchor_end": 0,
    })
    for fields, status, code in [
        ({}, 422, "outline_selection_required"),
        ({"outline_node_id": "foreign-node", "outline_version": 1}, 422, "outline_node_missing"),
        ({"outline_node_id": "absent", "outline_version": 1}, 422, "outline_node_missing"),
        ({"outline_node_id": "local-node", "outline_version": 2}, 409, "outline_version_conflict"),
    ]:
        response = client.post(f"/api/projects/{project_id}/continuations", json=body | fields)
        assert response.status_code == status, response.text
        assert response.json()["detail"]["code"] == code
    changed = client.post(f"/api/projects/{project_id}/outline/nodes", json={
        "command": "create", "request_id": "new-version", "base_version": 1,
        "node": {"id": "second-node", "title": "第二章"},
    })
    assert changed.status_code == 200, changed.text
    stale = client.post(f"/api/projects/{project_id}/continuations", json=body | {
        "outline_node_id": "local-node", "outline_version": 1,
    })
    assert stale.status_code == 409, stale.text
    assert client.get(f"/api/projects/{project_id}/continuations").json() == []
    assert client.get(f"/api/projects/{project_id}/ai-jobs").json() == []
    assert calls == []
