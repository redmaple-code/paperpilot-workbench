from __future__ import annotations


def _project(client) -> str:
    response = client.post("/api/projects", json={"name": "语料生命周期", "domain": "材料科学"})
    assert response.status_code == 200, response.text
    return response.json()["project"]["id"]


def test_corpus_original_version_and_manual_curation_are_separate(client):
    project_id = _project(client)
    created = client.post(f"/api/projects/{project_id}/corpus", json={"title": "实验记录", "content": "原始文本", "source": "local"})
    assert created.status_code == 200, created.text
    corpus_id = created.json()["id"]
    assert created.json()["source_version"] == 1
    curated = client.post(
        f"/api/projects/{project_id}/corpus/{corpus_id}/curations",
        json={"base_source_version": 1, "content": "人工校正文本", "notes": "修正 OCR", "kind": "manual"},
    )
    assert curated.status_code == 200, curated.text
    lineage = client.get(f"/api/projects/{project_id}/corpus/{corpus_id}/lineage").json()
    assert lineage["current_source_version"]["version"] == 1
    assert lineage["current_source_version"]["content"] == "原始文本"
    assert lineage["curation_versions"][0]["content"] == "人工校正文本"


def test_refresh_requires_explicit_decision_and_preserves_rejected_candidate(client):
    project_id = _project(client)
    created = client.post(f"/api/projects/{project_id}/corpus", json={"title": "外部资料", "content": "v1", "source": "url:v1"}).json()
    corpus_id = created["id"]
    candidate = client.post(
        f"/api/projects/{project_id}/corpus/{corpus_id}/refresh",
        json={"base_source_version": 1, "content": "v2", "source": "url:v2", "request_id": "refresh-1"},
    )
    assert candidate.status_code == 200, candidate.text
    replay = client.post(
        f"/api/projects/{project_id}/corpus/{corpus_id}/refresh",
        json={"base_source_version": 1, "content": "v2", "source": "url:v2", "request_id": "refresh-1"},
    ).json()
    assert replay["idempotent_replay"] is True
    rejected = client.post(
        f"/api/projects/{project_id}/corpus/{corpus_id}/refresh/{candidate.json()['id']}/decision",
        json={"decision": "reject", "request_id": "refresh-decision-1"},
    )
    assert rejected.status_code == 200, rejected.text
    lineage = client.get(f"/api/projects/{project_id}/corpus/{corpus_id}/lineage").json()
    assert lineage["current_source_version"]["version"] == 1
    assert lineage["refresh_candidates"][0]["status"] == "rejected"


def test_refresh_accept_creates_new_source_version_and_old_candidate_conflicts(client):
    project_id = _project(client)
    created = client.post(f"/api/projects/{project_id}/corpus", json={"title": "资料", "content": "v1"}).json()
    corpus_id = created["id"]
    candidate = client.post(
        f"/api/projects/{project_id}/corpus/{corpus_id}/refresh",
        json={"base_source_version": 1, "content": "v2", "source": "url:v2", "request_id": "refresh-accept"},
    ).json()
    accepted = client.post(
        f"/api/projects/{project_id}/corpus/{corpus_id}/refresh/{candidate['id']}/decision",
        json={"decision": "accept", "request_id": "refresh-accept-decision"},
    )
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["source_version"] == 2
    old = client.post(
        f"/api/projects/{project_id}/corpus/{corpus_id}/refresh",
        json={"base_source_version": 1, "content": "stale", "request_id": "refresh-stale"},
    )
    assert old.status_code == 409
    lineage = client.get(f"/api/projects/{project_id}/corpus/{corpus_id}/lineage").json()
    assert lineage["current_source_version"]["version"] == 2
    assert lineage["current_source_version"]["content"] == "v2"


def test_corpus_index_search_returns_source_version_and_excludes_revoked_sources(client):
    project_id = _project(client)
    first = client.post(f"/api/projects/{project_id}/corpus", json={"title": "石墨烯实验", "content": "界面稳定性来自石墨烯层间作用。"}).json()
    second = client.post(f"/api/projects/{project_id}/corpus", json={"title": "受限资料", "content": "石墨烯数据不可用"}).json()
    candidate = client.post(
        f"/api/projects/{project_id}/corpus/{second['id']}/refresh",
        json={"base_source_version": 1, "content": "石墨烯数据不可用", "permission_state": "revoked", "request_id": "revoke-index"},
    ).json()
    accepted = client.post(
        f"/api/projects/{project_id}/corpus/{second['id']}/refresh/{candidate['id']}/decision",
        json={"decision": "accept", "request_id": "revoke-index-decision"},
    )
    assert accepted.status_code == 409
    search = client.get(f"/api/projects/{project_id}/corpus/search", params={"query": "石墨烯"})
    assert search.status_code == 200, search.text
    body = search.json()
    assert body["authorized_only"] is True
    assert any(item["corpus_id"] == first["id"] and item["source_version"] == 1 for item in body["results"])
