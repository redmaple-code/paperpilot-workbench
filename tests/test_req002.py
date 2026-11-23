from __future__ import annotations

import uuid


def make_project(client, name="REQ-002 论文"):
    response = client.post("/api/projects", json={"name": name, "domain": ""})
    assert response.status_code == 200, response.text
    return response.json()


def bind_domain(client, project_id, domain_id, revision):
    response = client.put(
        f"/api/projects/{project_id}/domain",
        json={"domain_id": domain_id, "base_project_revision": revision},
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_domain_term_import_is_versioned_snapshot(client):
    domain = client.post("/api/domains", json={"name": "REQ002 材料科学"}).json()
    resource = client.post(
        f"/api/domains/{domain['id']}/resources",
        json={"resource_type": "term", "title": "石墨烯", "content": "二维碳材料"},
    )
    assert resource.status_code == 200, resource.text
    terms = client.get(f"/api/domains/{domain['id']}/terms").json()
    assert len(terms) == 1 and terms[0]["term"] == "石墨烯"
    project = make_project(client)
    bound = bind_domain(client, project["project"]["id"], domain["id"], project["project"]["project_revision"])
    request_id = str(uuid.uuid4())
    imported = client.post(
        f"/api/projects/{project['project']['id']}/terms/import",
        json={
            "domain_term_id": terms[0]["id"],
            "source_domain_version": 1,
            "base_project_revision": bound["project_revision"],
            "request_id": request_id,
        },
    )
    assert imported.status_code == 200, imported.text
    imported_term = imported.json()["term"]
    assert imported_term["source_domain_version"] == 1
    latest_revision = client.get(f"/api/projects/{project['project']['id']}").json()["project"]["project_revision"]
    replay = client.post(
        f"/api/projects/{project['project']['id']}/terms/import",
        json={
            "domain_term_id": terms[0]["id"],
            "source_domain_version": 1,
            "base_project_revision": latest_revision,
            "request_id": request_id,
        },
    )
    assert replay.status_code == 200 and replay.json()["idempotent_replay"] is True


def test_promotion_preview_commit_and_conflict_are_explicit(client):
    domain = client.post("/api/domains", json={"name": "REQ002 领域"}).json()
    project = make_project(client, "REQ002 沉淀论文")
    bound = bind_domain(client, project["project"]["id"], domain["id"], project["project"]["project_revision"])
    added = client.post(
        f"/api/projects/{project['project']['id']}/terms",
        json={"term": "术语甲", "definition": "论文定义", "aliases": "甲"},
    )
    assert added.status_code == 200, added.text
    project_data = client.get(f"/api/projects/{project['project']['id']}").json()
    preview = client.post(
        f"/api/projects/{project['project']['id']}/terms/promote/preview",
        json={
            "domain_id": domain["id"],
            "term_ids": [added.json()["id"]],
            "source_project_revision": project_data["project"]["project_revision"],
            "request_id": str(uuid.uuid4()),
        },
    )
    assert preview.status_code == 200, preview.text
    assert preview.json()["rows"][0]["outcome"] == "new"
    committed = client.post(
        f"/api/projects/{project['project']['id']}/terms/promote/commit",
        json={"preview_id": preview.json()["id"], "request_id": str(uuid.uuid4())},
    )
    assert committed.status_code == 200, committed.text
    assert committed.json()["domain_version"] == 2
    assert client.get(f"/api/domains/{domain['id']}/terms").json()[0]["term"] == "术语甲"

    # A different definition with the same normalized term is visible as a conflict.
    changed = client.post(
        f"/api/projects/{project['project']['id']}/terms",
        json={"term": "术语甲", "definition": "不同定义"},
    )
    assert changed.status_code == 200
    latest = client.get(f"/api/projects/{project['project']['id']}").json()
    conflict_preview = client.post(
        f"/api/projects/{project['project']['id']}/terms/promote/preview",
        json={
            "domain_id": domain["id"],
            "term_ids": [changed.json()["id"]],
            "source_project_revision": latest["project"]["project_revision"],
            "request_id": str(uuid.uuid4()),
        },
    )
    assert conflict_preview.status_code == 200
    assert conflict_preview.json()["conflicts"][0]["reason"] == "domain_term_conflict"


def test_ai_classification_never_publishes_before_human_decision(client):
    domain = client.post("/api/domains", json={"name": "REQ002 复核领域"}).json()
    project = make_project(client, "REQ002 复核论文")
    bind_domain(client, project["project"]["id"], domain["id"], project["project"]["project_revision"])
    term = client.post(
        f"/api/projects/{project['project']['id']}/terms",
        json={"term": "待分类", "definition": "论文定义"},
    ).json()
    review = client.post(
        f"/api/projects/{project['project']['id']}/terminology/reviews",
        json={
            "term_id": term["id"], "domain_id": domain["id"],
            "suggested_term": "待分类", "suggested_definition": "建议定义",
            "confidence": 0.42, "basis": "低置信度测试", "request_id": str(uuid.uuid4()),
        },
    )
    assert review.status_code == 200, review.text
    assert review.json()["status"] == "pending_manual"
    assert client.get(f"/api/domains/{domain['id']}/terms").json() == []
    decided = client.post(
        f"/api/projects/{project['project']['id']}/terminology/reviews/{review.json()['id']}/decision",
        json={"decision": "edit", "definition": "人工确认定义", "request_id": str(uuid.uuid4())},
    )
    assert decided.status_code == 200, decided.text
    assert decided.json()["status"] == "accepted"
    terms = client.get(f"/api/domains/{domain['id']}/terms").json()
    assert terms and terms[0]["definition"] == "人工确认定义"
