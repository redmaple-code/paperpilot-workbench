from __future__ import annotations

import app
from tests.provider_fixtures import authoring_preflight_payload
from tests.planning_helper import complete_planning


def _post_modification(client,project_id,json):
    return client.post(f'/api/projects/{project_id}/modifications',json=authoring_preflight_payload(client,project_id,json))


def _revision(client):
    project = client.post("/api/projects", json={"name": "指定修改", "domain": "材料科学"}).json()
    project_id = project["project"]["id"]
    complete_planning(client, project_id)
    workflow = client.get(f"/api/projects/{project_id}/workflow").json()
    started = client.post(f"/api/projects/{project_id}/workflow/commands", json={"command": "start", "request_id": "mod-start", "base_version": workflow["version"]})
    assert started.status_code == 200
    entered = client.post(f"/api/projects/{project_id}/human-revision", json={"workflow_version": started.json()["version"]})
    assert entered.status_code == 200
    return project_id


def test_modification_request_persists_target_snapshot_and_reuses_candidate_apply(client, monkeypatch):
    monkeypatch.setattr(app, "call_ai", lambda *args, **kwargs: {"ok": True, "content": "Rewritten introduction"})
    project_id = _revision(client)
    file_data = next(item for item in client.get(f"/api/projects/{project_id}").json()["files"] if item["path"] == "main.tex")
    text = "Introduction"
    start = file_data["content"].index(text)
    response = _post_modification(client,project_id, json={"request_id": "mod-one", "action": "rewrite", "target_kind": "file", "target_path": "main.tex", "base_version": file_data["version"], "start_offset": start, "end_offset": start + len(text), "instruction": "重写引言"})
    assert response.status_code == 202, response.text
    item = client.get(f"/api/projects/{project_id}/modifications/mod-one").json()
    assert item["snapshot"]["base_version"] == file_data["version"]
    assert item["candidate"]
    replay = _post_modification(client,project_id, json={"request_id": "mod-one", "action": "rewrite", "target_kind": "file", "target_path": "main.tex", "base_version": file_data["version"], "start_offset": start, "end_offset": start + len(text), "instruction": "重写引言"})
    assert replay.status_code == 202
    assert replay.json()["modification"]["idempotent_replay"] is True
    decision = client.post(f"/api/projects/{project_id}/modifications/mod-one/decision", json={"decision": "accept", "request_id": "mod-decision", "base_file_versions": item["candidate"]["base_file_versions"], "allow_warnings": True})
    assert decision.status_code == 200, decision.text
    assert decision.json()["modification"]["status"] == "applied"
    main = next(item for item in client.get(f"/api/projects/{project_id}").json()["files"] if item["path"] == "main.tex")
    assert "Rewritten introduction" in main["content"]


def test_modification_rejects_stale_base_and_preserves_content(client, monkeypatch):
    monkeypatch.setattr(app, "call_ai", lambda *args, **kwargs: {"ok": True, "content": "new text"})
    project_id = _revision(client)
    file_data = next(item for item in client.get(f"/api/projects/{project_id}").json()["files"] if item["path"] == "main.tex")
    start = file_data["content"].index("Introduction")
    created = _post_modification(client,project_id, json={"request_id": "mod-stale", "target_kind": "file", "target_path": "main.tex", "base_version": file_data["version"], "start_offset": start, "end_offset": start + 12, "instruction": "修改"})
    assert created.status_code == 202
    changed = client.put(f"/api/projects/{project_id}/files", json={"path": "main.tex", "content": "human change", "base_version": file_data["version"]})
    assert changed.status_code == 200
    item = client.get(f"/api/projects/{project_id}/modifications/mod-stale").json()
    assert item["candidate"]["stale"] is True
    decision = client.post(f"/api/projects/{project_id}/modifications/mod-stale/decision", json={"decision": "accept", "request_id": "mod-stale-decision", "base_file_versions": item["candidate"]["base_file_versions"]})
    assert decision.status_code == 409
    assert client.get(f"/api/projects/{project_id}").json()["files"][0]["content"] == "human change"


def test_cleanup_requires_preview_confirmation_and_rejects_stale_or_conflicting_replay(client, monkeypatch):
    monkeypatch.setattr(app, "call_ai", lambda *args, **kwargs: {"ok": True, "content": "candidate"})
    project_id = _revision(client)
    file_data = next(item for item in client.get(f"/api/projects/{project_id}").json()["files"] if item["path"] == "main.tex")

    def create(request_id):
        return _post_modification(
            client,project_id,
            json={
                "request_id": request_id, "target_kind": "file", "target_path": "main.tex",
                "base_version": file_data["version"], "start_offset": 0, "end_offset": 5,
                "instruction": "修改",
            },
        )

    assert create("cleanup-one").status_code == 202
    first = client.post(
        f"/api/projects/{project_id}/modifications/cleanup/preview",
        json={"scope": "project", "request_id": "cleanup-preview-1"},
    )
    assert first.status_code == 200, first.text
    assert len(first.json()["eligible"]) == 1

    assert create("cleanup-two").status_code == 202
    stale = client.post(
        f"/api/projects/{project_id}/modifications/cleanup",
        json={
            "scope": "project", "request_id": "cleanup-confirm-stale", "confirm": True,
            "preview_fingerprint": first.json()["preview_fingerprint"],
        },
    )
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "cleanup_preview_stale"

    preview = client.post(
        f"/api/projects/{project_id}/modifications/cleanup/preview",
        json={"scope": "project", "request_id": "cleanup-preview-2"},
    ).json()
    missing_confirmation = client.post(
        f"/api/projects/{project_id}/modifications/cleanup",
        json={"scope": "project", "request_id": "cleanup-confirm-missing"},
    )
    assert missing_confirmation.status_code == 422
    payload = {
        "scope": "project", "request_id": "cleanup-confirm-1", "confirm": True,
        "preview_fingerprint": preview["preview_fingerprint"],
    }
    cleaned = client.post(f"/api/projects/{project_id}/modifications/cleanup", json=payload)
    assert cleaned.status_code == 200, cleaned.text
    assert cleaned.json()["purged_count"] == 2
    replay = client.post(f"/api/projects/{project_id}/modifications/cleanup", json=payload)
    assert replay.status_code == 200 and replay.json()["idempotent_replay"] is True
    conflict = client.post(
        f"/api/projects/{project_id}/modifications/cleanup",
        json={**payload, "scope": "operation", "operation_id": "cleanup-one"},
    )
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "cleanup_idempotency_conflict"
