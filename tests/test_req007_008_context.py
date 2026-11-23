from __future__ import annotations


def test_follow_and_continue_context_exposes_global_constraints_and_omissions(client):
    created = client.post("/api/projects", json={"name": "REQ007 上下文", "domain": "材料科学"})
    assert created.status_code == 200
    project_id = created.json()["project"]["id"]
    for index in range(45):
        assert client.post(
            f"/api/projects/{project_id}/terms",
            json={"term": f"term-{index}", "definition": "context"},
        ).status_code == 200
    preview = client.post(
        f"/api/projects/{project_id}/ai-context",
        json={
            "action": "follow_write", "target_kind": "file", "target_path": "main.tex",
            "content": client.get(f"/api/projects/{project_id}").json()["files"][0]["content"],
            "prompt": "继续写作",
        },
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["global_context_complete"] is True
    assert body["degradation_mode"] == "partial_with_omission_report"
    assert body["context_policy"]
    assert any(item["kind"] == "术语" for item in body["omissions"])


def test_context_snapshot_version_changes_when_selected_source_changes(client):
    created = client.post("/api/projects", json={"name": "REQ008 快照", "domain": "材料科学"})
    project_id = created.json()["project"]["id"]
    project = client.get(f"/api/projects/{project_id}").json()
    request = {
        "action": "continue", "target_kind": "file", "target_path": "main.tex",
        "content": project["files"][0]["content"], "prompt": "续写一段",
    }
    first = client.post(f"/api/projects/{project_id}/ai-context", json=request)
    assert first.status_code == 200
    assert first.json()["version"]
    assert all("version" in source for source in first.json()["sources"])
    assert "工作流约束" in first.json()["context_policy"]
