from __future__ import annotations

import uuid

import app
from tests.provider_fixtures import seed_generation_fixture


def project(client, name="配置隔离论文"):
    response = client.post("/api/projects", json={"name": name, "domain": ""})
    assert response.status_code == 200, response.text
    return response.json()


def test_provider_profile_is_versioned_safe_and_binds_to_project(client):
    created = project(client)
    profile = client.post("/api/ai/profiles", json={
        "name": "本地模型",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_mode": "api_key",
        "models": ["local-model"],
        "api_key": "secret-value",
        "request_id": uuid.uuid4().hex,
    })
    assert profile.status_code == 200, profile.text
    body = profile.json()
    assert body["credential_state"] == "configured"
    assert "secret-value" not in profile.text
    seed_generation_fixture(body['id'],'local-model')
    enabled = client.put(f"/api/ai/profiles/{body['id']}", json={"base_version": 1, "changes": {"status": "enabled"}})
    assert enabled.status_code == 200, enabled.text
    bound = client.put(f"/api/projects/{created['project']['id']}/ai-binding", json={
        "profile_id": body["id"], "model_id": "local-model", "base_project_revision": created["project"]["project_revision"],
    })
    assert bound.status_code == 200, bound.text
    assert bound.json()["binding"]["model_id"] == "local-model"
    project_data = client.get(f"/api/projects/{created['project']['id']}").json()
    assert project_data["ai_binding"]["profile_id"] == body["id"]
    stale = client.put(f"/api/projects/{created['project']['id']}/ai-binding", json={
        "profile_id": body["id"], "model_id": "local-model", "base_project_revision": created["project"]["project_revision"],
    })
    assert stale.status_code == 409


def test_provider_profile_test_does_not_send_external_request(client):
    response = client.post("/api/ai/profiles", json={
        "name": "免密钥本地",
        "base_url": "http://127.0.0.1:8000/v1",
        "auth_mode": "none",
        "models": ["model-a"],
        "request_id": uuid.uuid4().hex,
    })
    assert response.status_code == 200
    checked = client.post(f"/api/ai/profiles/{response.json()['id']}/test")
    assert checked.status_code == 200
    assert checked.json()["status"] == "configuration_valid_only"
    assert checked.json()['external_calls']==0
    assert client.put(f"/api/ai/profiles/{response.json()['id']}",json={'base_version':1,'changes':{'status':'enabled'}}).status_code==409


def test_domain_resources_are_isolated_and_copied_as_project_snapshot(client):
    first = client.post("/api/domains", json={"name": "材料科学"})
    second = client.post("/api/domains", json={"name": "历史学"})
    assert first.status_code == second.status_code == 200
    domain_a = first.json()["id"]
    domain_b = second.json()["id"]
    resource = client.post(f"/api/domains/{domain_a}/resources", json={
        "resource_type": "term", "title": "石墨烯", "content": "二维碳材料"
    })
    assert resource.status_code == 200
    p = project(client, "领域论文")
    assigned = client.put(f"/api/projects/{p['project']['id']}/domain", json={
        "domain_id": domain_a, "base_project_revision": p["project"]["project_revision"]
    })
    assert assigned.status_code == 200
    copied = client.post(f"/api/projects/{p['project']['id']}/domain-resources/copy", json={
        "resource_id": resource.json()["id"], "base_project_revision": assigned.json()["project_revision"]
    })
    assert copied.status_code == 200
    data = client.get(f"/api/projects/{p['project']['id']}").json()
    assert any(item["term"] == "石墨烯" and item["source_resource_id"] == resource.json()["id"] for item in data["terms"])
    other = client.post(f"/api/domains/{domain_b}/resources", json={"resource_type": "term", "title": "史料", "content": "档案"})
    assert other.status_code == 200
    forbidden = client.post(f"/api/projects/{p['project']['id']}/domain-resources/copy", json={
        "resource_id": other.json()["id"], "base_project_revision": copied.json()["project_revision"]
    })
    assert forbidden.status_code == 403


def test_existing_domain_cannot_be_switched_and_legacy_first_binding_preserves_baseline(client):
    first = client.post('/api/domains', json={'name': '归属一'}).json()
    second = client.post('/api/domains', json={'name': '归属二'}).json()
    created = client.post('/api/projects', json={'name': '旧无领域论文', 'domain': '', 'style': '原有风格'}).json()
    pid = created['project']['id']
    prior = client.get(f'/api/projects/{pid}/settings').json()
    revision = client.get(f'/api/projects/{pid}').json()['project']['project_revision']
    bound = client.put(f'/api/projects/{pid}/domain', json={'domain_id': first['id'], 'base_project_revision': revision})
    assert bound.status_code == 200, bound.text
    current = client.get(f'/api/projects/{pid}/settings').json()
    assert current['baseline']['domain_id'] == first['id']
    assert current['baseline']['domain_version'] == 0
    assert current['effective']['style'] == prior['effective']['style'] == '原有风格'
    assert current['sources']['style']['source'] == 'project_baseline'
    before_project = client.get(f'/api/projects/{pid}').json()['project']
    before_settings = current
    rejected = client.put(f'/api/projects/{pid}/domain', json={'domain_id': second['id'], 'base_project_revision': before_project['project_revision']})
    assert rejected.status_code == 409 and rejected.json()['detail']['code'] == 'domain_migration_unavailable'
    assert client.get(f'/api/projects/{pid}').json()['project'] == before_project
    assert client.get(f'/api/projects/{pid}/settings').json() == before_settings
    same = client.put(f'/api/projects/{pid}/domain', json={'domain_id': first['id'], 'base_project_revision': before_project['project_revision']})
    assert same.status_code == 200 and same.json()['unchanged'] and same.json()['project_revision'] == before_project['project_revision']
    stale = client.put(f'/api/projects/{pid}/domain', json={'domain_id': first['id'], 'base_project_revision': revision})
    assert stale.status_code == 409


def test_create_project_with_domain_uses_domain_identity_and_missing_domain_is_rejected(client):
    domain = client.post('/api/domains', json={'name': '创建时选择领域'}).json()
    result = client.post('/api/projects', json={'name': '新领域论文', 'domain_id': domain['id'], 'domain': '过期的文字'})
    assert result.status_code == 200, result.text
    project = result.json()['project']
    assert project['domain_id'] == domain['id'] and project['domain'] == domain['name']
    settings = client.get(f"/api/projects/{project['id']}/settings").json()
    assert settings['baseline']['domain_id'] == domain['id'] and settings['effective']['domain'] == domain['name']
    before = len(client.get('/api/projects').json())
    missing = client.post('/api/projects', json={'name': '不存在的领域', 'domain_id': 'missing-domain'})
    assert missing.status_code == 404 and missing.json()['detail']['code'] == 'domain_missing'
    assert len(client.get('/api/projects').json()) == before
