from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor

import app


def create_project(client, name="REQ-003 论文"):
    response = client.post("/api/projects", json={"name": name, "domain": "材料科学"})
    assert response.status_code == 200, response.text
    return response.json()["project"]["id"]


def test_outline_request_ids_do_not_replay_other_projects_or_different_candidates(client):
    a=create_project(client,'甲');b=create_project(client,'乙')
    body={'request_id':'shared-preview'}
    first=client.post(f'/api/projects/{a}/outline/migration/preview',json=body);assert first.status_code==200
    assert client.post(f'/api/projects/{a}/outline/migration/preview',json=body).json()['id']==first.json()['id']
    other=client.post(f'/api/projects/{b}/outline/migration/preview',json=body)
    assert other.status_code==409 and other.json()['detail']['code']=='outline_request_id_conflict'
    assert 'Introduction' not in other.text and first.json()['id'] not in other.text
    payload={'request_id':'shared-candidate','base_version':0,'operations':[{'op':'add_node','node':{'id':'private','title':'甲论文私人内容'}}]}
    created=client.post(f'/api/projects/{a}/outline/candidates',json=payload);assert created.status_code==200
    assert client.post(f'/api/projects/{a}/outline/candidates',json=payload).json()['id']==created.json()['id']
    other=client.post(f'/api/projects/{b}/outline/candidates',json=payload)
    assert other.status_code==409 and '甲论文私人内容' not in other.text
    changed=client.post(f'/api/projects/{a}/outline/candidates',json={**payload,'operations':[]})
    assert changed.status_code==409 and changed.json()['detail']['code']=='outline_request_id_conflict'


def test_concurrent_outline_preview_id_collision_is_isolated(client):
    a=create_project(client,'并发甲');b=create_project(client,'并发乙')
    def request(pid):return client.post(f'/api/projects/{pid}/outline/migration/preview',json={'request_id':'simultaneous-preview'})
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(request,[a,b]))
    assert sorted(r.status_code for r in results)==[200,409]
    loser=next(r for r in results if r.status_code==409)
    assert loser.json()['detail']['code']=='outline_request_id_conflict' and 'nodes' not in loser.text


def test_legacy_outline_migration_preview_is_non_destructive_and_commits_version(client):
    project_id = create_project(client)
    preview = client.post(
        f"/api/projects/{project_id}/outline/migration/preview",
        json={"request_id": "outline-preview-1"},
    )
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["status"] == "preview"
    assert body["nodes"] and body["unknown_lines"] == []
    before = client.get(f"/api/projects/{project_id}").json()["outline"]
    assert before.startswith("1. Introduction")
    committed = client.post(
        f"/api/projects/{project_id}/outline/migration/commit",
        json={"draft_id": body["id"], "request_id": "outline-commit-1"},
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["version"] == 1
    structured = client.get(f"/api/projects/{project_id}/outline/structured").json()
    assert structured["version"] == 1
    assert structured["nodes"][0]["title"] == "Introduction"
    assert client.get(f"/api/projects/{project_id}").json()["outline"] == before


def test_outline_node_commands_are_versioned_and_conflicts_preserve_current_tree(client):
    project_id = create_project(client, "REQ-003 节点")
    migration = client.post(f"/api/projects/{project_id}/outline/migration/preview", json={"request_id": "migration-node"}).json()
    committed = client.post(
        f"/api/projects/{project_id}/outline/migration/commit",
        json={"draft_id": migration["id"], "request_id": "commit-node"},
    ).json()
    root = committed["nodes"][0]
    created = client.post(
        f"/api/projects/{project_id}/outline/nodes",
        json={
            "command": "create", "request_id": "node-create", "base_version": 1,
            "node": {"id": "node-extra", "title": "新增章节", "parent_id": root["id"], "position": 0},
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["version"] == 2
    stale = client.post(
        f"/api/projects/{project_id}/outline/nodes",
        json={
            "command": "update", "request_id": "node-stale", "base_version": 1,
            "node_id": root["id"], "node": {"title": "过期修改"},
        },
    )
    assert stale.status_code == 409
    current = client.get(f"/api/projects/{project_id}/outline/structured").json()
    assert all(node["title"] != "过期修改" for node in current["nodes"])


def test_outline_ai_candidate_requires_current_baseline_and_supports_partial_apply(client):
    project_id = create_project(client, "REQ-003 AI")
    migration = client.post(f"/api/projects/{project_id}/outline/migration/preview", json={"request_id": "migration-ai"}).json()
    client.post(f"/api/projects/{project_id}/outline/migration/commit", json={"draft_id": migration["id"], "request_id": "commit-ai"})
    candidate = client.post(
        f"/api/projects/{project_id}/outline/candidates",
        json={
            "request_id": "candidate-ai", "base_version": 1,
            "operations": [
                {"op": "add_node", "node": {"id": "ai-node", "title": "AI 建议"}},
                {"op": "add_node", "node": {"id": "ignored-node", "title": "不采纳"}},
            ],
        },
    )
    assert candidate.status_code == 200, candidate.text
    applied = client.post(
        f"/api/projects/{project_id}/outline/candidates/{candidate.json()['id']}/decision",
        json={"request_id": "candidate-decision", "decision": "partial_apply", "selected_indices": [0], "base_version": 1},
    )
    assert applied.status_code == 200, applied.text
    graph = client.get(f"/api/projects/{project_id}/outline/structured").json()
    assert graph["version"] == 2
    ids = {node["id"] for node in graph["nodes"]}
    assert "ai-node" in ids and "ignored-node" not in ids
    stale = client.post(
        f"/api/projects/{project_id}/outline/candidates",
        json={"request_id": "candidate-stale", "base_version": 1, "operations": []},
    )
    assert stale.status_code == 409


def test_outline_local_draft_survives_save_failure_and_is_readable(client):
    project_id = create_project(client, "REQ-003 草稿")
    saved = client.put(
        f"/api/projects/{project_id}/outline/drafts/editor",
        json={"draft_key": "editor", "base_version": 0, "content": {"nodes": [{"id": "draft", "title": "本地草稿"}]}},
    )
    assert saved.status_code == 200, saved.text
    restored = client.get(f"/api/projects/{project_id}/outline/drafts/editor")
    assert restored.status_code == 200
    assert restored.json()["content"]["nodes"][0]["title"] == "本地草稿"


def test_outline_resource_links_are_project_scoped_and_inactive_resources_project_stale(client):
    project_id = create_project(client, "REQ-003 资源归属")
    other_project_id = create_project(client, "REQ-003 其他项目")
    migration = client.post(
        f"/api/projects/{project_id}/outline/migration/preview",
        json={"request_id": "resource-migration"},
    ).json()
    committed = client.post(
        f"/api/projects/{project_id}/outline/migration/commit",
        json={"draft_id": migration["id"], "request_id": "resource-commit"},
    ).json()
    node_id = committed["nodes"][0]["id"]
    foreign_term = client.post(
        f"/api/projects/{other_project_id}/terms",
        json={"term": "foreign", "definition": "not local"},
    ).json()
    linked = client.post(
        f"/api/projects/{project_id}/outline/nodes",
        json={
            "command": "link", "request_id": "foreign-link", "base_version": 1,
            "node_id": node_id,
            "link": {"resource_type": "term", "resource_id": foreign_term["id"]},
        },
    )
    assert linked.status_code == 409

    local_term = client.post(
        f"/api/projects/{project_id}/terms",
        json={"term": "local", "definition": "owned here"},
    ).json()
    linked = client.post(
        f"/api/projects/{project_id}/outline/nodes",
        json={
            "command": "link", "request_id": "local-link", "base_version": 1,
            "node_id": node_id,
            "link": {"resource_type": "term", "resource_id": local_term["id"]},
        },
    )
    assert linked.status_code == 200, linked.text
    conn = app.db()
    conn.execute("UPDATE terms SET status='inactive' WHERE id=?", (local_term["id"],))
    conn.commit()
    conn.close()
    projected = client.get(f"/api/projects/{project_id}/outline/structured").json()
    resource_link = next(item for item in projected["links"] if item["resource_id"] == local_term["id"])
    assert resource_link["status"] == "inactive"
    assert resource_link["invalid_reason"] == "resource_inactive"
