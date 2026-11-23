from __future__ import annotations

import json
import uuid

import app
from tests.provider_fixtures import logic_preview_payload
from paperpilot.services.context import build_ai_context_snapshot
from paperpilot.services.style_profile import (
    DisabledStyleProfileProvider,
    StyleProfile,
    profile_context,
    resolve_style_profile,
)


def create_project(client, name: str = "边界测试论文") -> str:
    response = client.post("/api/projects", json={"name": name, "domain": "材料科学"})
    assert response.status_code == 200, response.text
    return response.json()["project"]["id"]


def test_req020_extension_is_default_off_and_read_only(client):
    project_id = create_project(client)
    before = client.get(f"/api/projects/{project_id}").json()["project"]
    status = client.get(f"/api/projects/{project_id}/extensions/style-profile")
    assert status.status_code == 200
    assert status.json() == {
        "module_id": "EXT-001", "contract_version": "1", "project_id": project_id,
        "status": "disabled", "opt_in": False, "profile_version": None,
        "source_versions": [], "learning_backend_available": True,
        "message": "默认关闭。选择学习范围和期限并明确授权后开始。",
    }
    assert resolve_style_profile(project_id) == StyleProfile(project_id=project_id)
    assert isinstance(DisabledStyleProfileProvider().effective_profile(project_id), StyleProfile)
    assert profile_context(StyleProfile(project_id=project_id)) == ""
    after = client.get(f"/api/projects/{project_id}").json()["project"]
    assert after["project_revision"] == before["project_revision"]


def test_req020_active_profile_requires_opt_in_and_provenance():
    class Provider:
        def effective_profile(self, project_id):
            return StyleProfile(
                project_id=project_id, version="sp-2", instructions=("使用短句。",),
                source_versions=("edit-v4",), status="active",
            )

    project_id = "profile-project"
    assert resolve_style_profile(project_id, provider=Provider(), opt_in=False).status == "disabled"
    profile = resolve_style_profile(project_id, provider=Provider(), opt_in=True)
    assert profile.status == "active"
    assert "vsp-2" in profile_context(profile)

    class InvalidProvider:
        def effective_profile(self, project_id):
            return StyleProfile(project_id=project_id, status="active", instructions=("缺少版本",))

    assert resolve_style_profile(project_id, provider=InvalidProvider(), opt_in=True).status == "failed"


def test_style_profile_is_absent_from_default_context_snapshot(client):
    project_id = create_project(client)
    project = client.get(f"/api/projects/{project_id}").json()["project"]
    bundle = {
        "outline": "",
        "terms": [], "corpus": [], "refs": [], "logic": [],
    }
    snapshot = build_ai_context_snapshot(project, bundle)
    assert "授权文风经验" not in snapshot["text"]
    assert snapshot["redactions"] == 0


def test_logic_suggestion_reject_remains_available_after_graph_changes(client, monkeypatch):
    project_id = create_project(client)
    claim = {"id": "claim-edge", "kind": "claim", "label": "主张"}
    first = client.put(
        f"/api/projects/{project_id}/logic/graph",
        json={"request_id": uuid.uuid4().hex, "base_version": 0, "nodes": [claim], "edges": []},
    )
    assert first.status_code == 200
    monkeypatch.setattr(
        app, "call_ai", lambda *_args, **_kwargs: {
            "ok": True, "content": json.dumps({"operations": [
                {"op": "add_node", "node": {"id": "suggested-edge", "kind": "evidence", "label": "证据"}},
            ]}),
        },
    )
    suggestion = client.post(
        f"/api/projects/{project_id}/logic/suggestions",
        json=logic_preview_payload(client,project_id,{"request_id": uuid.uuid4().hex, "prompt": "补充证据"}),
    ).json()
    changed = client.put(
        f"/api/projects/{project_id}/logic/graph",
        json={
            "request_id": uuid.uuid4().hex, "base_version": 1,
            "nodes": [claim, {"id": "server-change", "kind": "evidence", "label": "现有证据"}],
            "edges": [],
        },
    )
    assert changed.status_code == 200
    rejected = client.post(
        f"/api/projects/{project_id}/logic/suggestions/{suggestion['id']}/decision",
        json={"request_id": uuid.uuid4().hex, "decision": "reject", "base_version": 1},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["graph"]["version"] == 2
    assert "suggested-edge" not in {node["id"] for node in rejected.json()["graph"]["nodes"]}


def test_logic_suggestion_non_object_response_is_rejected(client, monkeypatch):
    project_id = create_project(client)
    monkeypatch.setattr(app, "call_ai", lambda *_args, **_kwargs: {"ok": True, "content": "[]"})
    response = client.post(
        f"/api/projects/{project_id}/logic/suggestions",
        json=logic_preview_payload(client,project_id,{"request_id": uuid.uuid4().hex, "prompt": "检查"}),
    )
    assert response.status_code == 502
    assert response.json()["detail"]["code"] == "invalid_logic_suggestion"

    monkeypatch.setattr(app, "call_ai", lambda *_args, **_kwargs: [])
    provider_response = client.post(
        f"/api/projects/{project_id}/logic/suggestions",
        json=logic_preview_payload(client,project_id,{"request_id": uuid.uuid4().hex, "prompt": "检查"}),
    )
    assert provider_response.status_code == 502
    assert provider_response.json()["detail"]["code"] == "logic_suggestion_failed"
