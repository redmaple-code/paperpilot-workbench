from tests.planning_helper import complete_planning
def _create_project(client):
    response = client.post("/api/projects", json={"name": "REQ-015 编辑器", "domain": "材料科学"})
    assert response.status_code == 200, response.text
    project_id = response.json()["project"]["id"]
    complete_planning(client, project_id)
    workflow = client.get(f"/api/projects/{project_id}/workflow").json()
    started = client.post(
        f"/api/projects/{project_id}/workflow/commands",
        json={"command": "start", "request_id": f"start-{project_id}", "base_version": workflow["version"]},
    )
    assert started.status_code == 200, started.text
    entered = client.post(
        f"/api/projects/{project_id}/human-revision",
        json={"workflow_version": started.json()["version"]},
    )
    assert entered.status_code == 200, entered.text
    return project_id


def test_editor_save_is_versioned_and_idempotent(client):
    project_id = _create_project(client)
    file = client.get(f"/api/projects/{project_id}").json()["files"][0]
    body = {
        "path": file["path"], "content": file["content"] + "\n% editor",
        "base_version": file["version"], "request_id": "editor-save-1",
    }
    saved = client.put(f"/api/projects/{project_id}/editor/save", json=body)
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == file["version"] + 1
    replay = client.put(f"/api/projects/{project_id}/editor/save", json=body)
    assert replay.status_code == 200 and replay.json()["idempotent_replay"] is True

    changed_retry = client.put(
        f"/api/projects/{project_id}/editor/save",
        json={**body, "content": body["content"] + " changed"},
    )
    assert changed_retry.status_code == 409
    assert changed_retry.json()["detail"]["code"] == "editor_request_id_conflict"


def test_editor_draft_can_be_restored_or_discarded(client):
    project_id = _create_project(client)
    file = client.get(f"/api/projects/{project_id}").json()["files"][0]
    body = {
        "path": file["path"], "base_version": file["version"],
        "base_content": file["content"], "content": file["content"] + "\nunsaved",
    }
    saved = client.put(f"/api/projects/{project_id}/editor/draft", json=body)
    assert saved.status_code == 200, saved.text
    restored = client.get(
        f"/api/projects/{project_id}/editor/draft", params={"path": file["path"]},
    )
    assert restored.status_code == 200
    assert restored.json()["content"] == body["content"]
    assert restored.json()["stale"] is False
    deleted = client.delete(
        f"/api/projects/{project_id}/editor/draft", params={"path": file["path"]},
    )
    assert deleted.status_code == 200
    assert client.get(
        f"/api/projects/{project_id}/editor/draft", params={"path": file["path"]},
    ).json()["exists"] is False


def test_stale_editor_draft_is_preserved_for_manual_recovery(client):
    project_id = _create_project(client)
    file = client.get(f"/api/projects/{project_id}").json()["files"][0]
    draft_content = file["content"] + "\nlocal edits"
    saved = client.put(
        f"/api/projects/{project_id}/editor/draft",
        json={
            "path": file["path"], "base_version": file["version"],
            "base_content": file["content"], "content": draft_content,
        },
    )
    assert saved.status_code == 200, saved.text
    changed = client.put(
        f"/api/projects/{project_id}/editor/save",
        json={
            "path": file["path"], "content": file["content"] + "\nremote edits",
            "base_version": file["version"], "request_id": "editor-remote-save",
        },
    )
    assert changed.status_code == 200, changed.text
    restored = client.get(
        f"/api/projects/{project_id}/editor/draft", params={"path": file["path"]},
    ).json()
    assert restored["content"] == draft_content
    assert restored["stale"] is True


def test_conversion_requires_preview_and_rejects_stale_source(client):
    project_id = _create_project(client)
    file = client.get(f"/api/projects/{project_id}").json()["files"][0]
    preview = client.post(
        f"/api/projects/{project_id}/editor/conversions/preview",
        json={
            "request_id": "conversion-preview-1", "source_path": file["path"],
            "source_version": file["version"], "target_path": "main.md",
            "direction": "latex_to_markdown",
        },
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["status"] == "preview"
    assert preview.json()["target_content"]

    stale = client.put(
        f"/api/projects/{project_id}/editor/save",
        json={
            "path": file["path"], "content": file["content"] + "\n% newer",
            "base_version": file["version"], "request_id": "editor-save-newer",
        },
    )
    assert stale.status_code == 200, stale.text
    applied = client.post(
        f"/api/projects/{project_id}/editor/conversions/{preview.json()['id']}/apply",
        json={"request_id": "conversion-apply-1"},
    )
    assert applied.status_code == 409
    assert applied.json()["detail"]["code"] == "editor_conversion_stale"


def test_editor_saved_version_is_the_source_of_req011_build_snapshot(client):
    project_id = _create_project(client)
    file = client.get(f"/api/projects/{project_id}").json()["files"][0]
    content = file["content"] + "\n% snapshot handoff"
    saved = client.put(
        f"/api/projects/{project_id}/editor/save",
        json={
            "path": file["path"], "content": content,
            "base_version": file["version"], "request_id": "editor-handoff-save",
        },
    )
    assert saved.status_code == 200, saved.text
    project = client.get(f"/api/projects/{project_id}").json()["project"]
    snapshot = client.post(
        f"/api/projects/{project_id}/build-snapshots",
        json={
            "request_id": "editor-handoff-snapshot",
            "expected_project_revision": project["project_revision"],
        },
    )
    assert snapshot.status_code == 200, snapshot.text
    source = next(item for item in snapshot.json()["files"] if item["path"] == file["path"])
    assert source["content"] == content
    assert source["version"] == saved.json()["version"]
