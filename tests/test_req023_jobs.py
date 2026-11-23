from __future__ import annotations

import json
import threading
from contextlib import contextmanager

import app
from paperpilot.services import visual_generation, visual_jobs
from paperpilot.services.project_errors import ProjectServiceError
from tests.test_req022_assets import project, upload
from tests.provider_fixtures import seed_generation_fixture


def url(pid, suffix=""):
    return f"/api/projects/{pid}/drawing" + suffix


def create(client, pid, request_id="new"):
    r = client.post(url(pid, "/runs"), json={"intent": "方法流程图", "title": "Method", "request_id": request_id})
    assert r.status_code == 200, r.text
    return r.json()


def decision(client, pid, run, kind="structured_svg", spec=None, request_id="choose"):
    return client.post(url(pid, f"/runs/{run['id']}/decision"), json={"expected_revision": run["revision"], "request_id": request_id,
        "selected_type": kind, "spec": spec or {"nodes": [{"id": "a", "label": "Input"}, {"id": "b", "label": "Output"}], "edges": [{"source": "a", "target": "b"}]}})


def read(client, pid, run):
    return client.get(url(pid, f"/runs/{run['id']}")).json()


def dispatch(client, pid, run, **extra):
    return client.post(url(pid, f"/runs/{run['id']}/dispatch"), json={"expected_revision": run["revision"], "request_id": "dispatch", **extra})


def test_confirm_preview_save_are_three_distinct_commits(client):
    pid = project(client)
    run = create(client, pid)
    assert run["status"] == "awaiting_confirmation" and run["recommendation"]["alternatives"]
    assert dispatch(client, pid, run).json()["detail"]["code"] == "tool_not_ready"
    assert not read(client, pid, run)["attempts"]
    chosen = decision(client, pid, run).json()
    assert chosen["status"] == "tool_ready"
    sent = dispatch(client, pid, chosen)
    assert sent.status_code == 200, sent.text
    ready = read(client, pid, run)
    assert ready["status"] == "preview_ready", ready
    c = ready["candidates"][0]
    assert client.get(url(pid, "/assets")).json()["assets"] == []
    before = client.get(f"/api/projects/{pid}").json()["files"]
    assert client.get(c["preview_url"]).content.startswith(b"<svg")
    payload = {"request_id": "save", "expected_run_revision": ready["revision"]}
    saved = client.post(url(pid, f"/candidates/{c['id']}/save"), json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"]["run_id"] == run["id"] and not saved.json()["body_written"]
    assert client.get(f"/api/projects/{pid}").json()["files"] == before
    again = client.post(url(pid, f"/candidates/{c['id']}/save"), json=payload)
    assert again.json()["version"]["id"] == saved.json()["version"]["id"]
    assert len(read(client, pid, run)["attempts"]) == 1
    assert dispatch(client, pid, chosen).json()["idempotent_replay"]


def test_invalid_spec_conflict_and_persistent_undo_redo(client):
    pid = project(client)
    run = create(client, pid)
    invalid = decision(client, pid, run, spec={"nodes": [{"id": "a", "label": "A"}], "edges": [{"source": "a", "target": "missing"}]})
    assert invalid.status_code == 422
    scene = {"nodes": [{"id": "a", "label": "one", "x": 10, "y": 10}]}
    first = decision(client, pid, run, "scene_graph", scene).json()
    assert decision(client, pid, run, "scene_graph", scene, "stale").status_code == 409
    scene["nodes"][0]["label"] = "two"
    second = decision(client, pid, first, "scene_graph", scene, "edit").json()
    undone = client.post(url(pid, f"/runs/{run['id']}/undo"), json={"expected_revision": second["revision"], "request_id": "undo"}).json()
    assert undone["spec"]["nodes"][0]["label"] == "one"
    redone = client.post(url(pid, f"/runs/{run['id']}/redo"), json={"expected_revision": undone["revision"], "request_id": "redo"}).json()
    assert redone["spec"]["nodes"][0]["label"] == "two"
    assert dispatch(client, pid, redone).status_code == 200
    assert read(client, pid, run)["status"] == "preview_ready"


def test_stale_candidate_and_cross_project_are_rejected(client):
    pid, other = project(client), project(client, "other")
    run = create(client, pid)
    chosen = decision(client, pid, run).json()
    dispatch(client, pid, chosen)
    ready = read(client, pid, run)
    c = ready["candidates"][0]
    assert client.get(url(other, f"/candidates/{c['id']}")).status_code == 404
    conn = app.db()
    with conn:
        conn.execute("UPDATE projects SET project_revision=project_revision+1 WHERE id=?", (pid,))
    conn.close()
    assert read(client, pid, run)["candidates"][0]["status"] == "stale"
    denied = client.post(url(pid, f"/candidates/{c['id']}/save"), json={"request_id": "save", "expected_run_revision": ready["revision"]})
    assert denied.status_code == 409
    assert not client.get(url(pid, "/assets")).json()["assets"]


def test_legacy_draw_creates_recommendation_without_writing_files(client):
    pid = project(client)
    before = client.get(f"/api/projects/{pid}").json()["files"]
    r = client.post(f"/api/projects/{pid}/draw", json={"title": "Method", "labels": ["A", "B"]})
    assert r.status_code == 200 and r.json()["run"]["status"] == "awaiting_confirmation"
    assert client.get(f"/api/projects/{pid}").json()["files"] == before


def bind_image_model(client, pid, cap):
    response = client.post("/api/ai/profiles", json={"name": "Images", "base_url": "http://127.0.0.1:9999/v1", "auth_mode": "none", "models": ["image-test"], "capabilities": {"image-test": cap},'model_limits':{'image-test':{'input_tokens':100000,'output_tokens':10000,'source':'isolated visual consumer fixture','vision':{'tokens_per_image':1000,'max_width':1600,'max_height':1600,'source':'isolated high detail consumer fixture, not a real model limit'}}}, "request_id": "profile"})
    assert response.status_code == 200, response.text
    p = response.json()
    seed_generation_fixture(p['id'],'image-test',('chat','image_generation','image_edit','vision_analysis'))
    enabled = client.put(f"/api/ai/profiles/{p['id']}", json={"base_version": 1, "changes": {"status": "enabled"}})
    assert enabled.status_code == 200, enabled.text
    revision = client.get(f"/api/projects/{pid}").json()["project"]["project_revision"]
    r = client.put(f"/api/projects/{pid}/ai-binding", json={"profile_id": p["id"], "model_id": "image-test", "base_project_revision": revision})
    assert r.status_code == 200, r.text
    return p


def test_unknown_price_hard_limit_and_missing_mask_make_zero_calls(client, monkeypatch):
    from paperpilot.adapters import visual_image
    calls = []
    monkeypatch.setattr(visual_image, "generate", lambda *a, **k: calls.append(1))
    pid = project(client)
    bind_image_model(client, pid, {"image_generation": True, "image_edit": True})
    run = create(client, pid)
    chosen = decision(client, pid, run, "ai_image", {"prompt": "scientific image"}).json()
    assert dispatch(client, pid, chosen, external_consent=True, hard_limit=1, unknown_price_confirmed=True).json()["detail"]["code"] == "budget_unverifiable"
    assert dispatch(client, pid, chosen, external_consent=True).json()["detail"]["code"] == "unknown_price_confirmation_required"
    assert calls == [] and read(client, pid, run)["attempts"] == []
    image = upload(client, pid).json()["version"]["id"]
    masked = create(client, pid, "masked")
    chosen_masked = decision(client, pid, masked, "ai_image", {"prompt": "edit", "input_asset_version": image, "mask_version": image}, "masked-choice").json()
    assert dispatch(client, pid, chosen_masked, external_consent=True, unknown_price_confirmed=True).json()["detail"]["code"] == "capability_missing"
    assert calls == []


def test_timeout_reconcile_and_cancelled_late_result_never_auto_save(client, monkeypatch):
    from paperpilot.adapters import visual_image
    pid = project(client)
    bind_image_model(client, pid, {"image_generation": True, "price_per_image": 0.02})
    calls = []
    def timeout(*a, **k):
        calls.append(1)
        raise ProjectServiceError(503, "provider_unknown", "timeout")
    monkeypatch.setattr(visual_image, "generate", timeout)
    run = create(client, pid)
    chosen = decision(client, pid, run, "ai_image", {"prompt": "example"}).json()
    assert dispatch(client, pid, chosen, external_consent=True, hard_limit=0.01).status_code == 422
    assert dispatch(client, pid, chosen, external_consent=True, hard_limit=1).status_code == 200
    unknown = read(client, pid, run)
    assert unknown["status"] == "unknown" and len(calls) == 1
    reconciled = client.post(url(pid, f"/runs/{run['id']}/reconcile"), json={"expected_revision": unknown["revision"], "request_id": "reconcile"}).json()
    assert reconciled["status"] == "unknown" and "不会自动重发" in reconciled["message"]
    denied = client.post(url(pid, f"/runs/{run['id']}/dispatch"), json={"expected_revision": reconciled["revision"], "request_id": "retry", "external_consent": True})
    assert denied.status_code == 409 and len(calls) == 1
    # Cancel in-flight work while the adapter is executing in a separate transaction.
    from tests.test_req022_assets import png
    def late(*a, **k):
        live = read(client, pid, run2)
        cancelled = client.post(url(pid, f"/runs/{run2['id']}/cancel"), json={"expected_revision": live["revision"], "request_id": "cancel"})
        assert cancelled.status_code == 200
        return {"content": png(), "filename": "late.png", "usage": {"images": 1}}
    monkeypatch.setattr(visual_image, "generate", late)
    run2 = create(client, pid, "second")
    chosen2 = decision(client, pid, run2, "ai_image", {"prompt": "late"}, "choose-second").json()
    assert client.post(url(pid, f"/runs/{run2['id']}/dispatch"), json={"expected_revision": chosen2["revision"], "request_id": "second-dispatch", "external_consent": True}).status_code == 200
    cancelled = read(client, pid, run2)
    assert cancelled["status"] == "cancelled" and cancelled["candidates"][0]["status"] == "cancelled"
    assert not client.get(url(pid, "/assets")).json()["assets"]


def test_unapproved_style_never_enters_plan(client):
    pid = project(client)
    candidate = client.post(url(pid, "/style-rules/structured_svg"), json={"request_id": "rule", "values": {"fill": "#ff0000"}}).json()
    run = create(client, pid)
    first = decision(client, pid, run).json()
    assert first["plan"]["values"]["fill"] != "#ff0000"
    assert client.post(url(pid, f"/style-rules/{candidate['id']}/approve")).status_code == 200
    assert read(client, pid, run)["stale_reasons"]
    second = client.post(url(pid, f"/runs/{run['id']}/decision"), json={"request_id": "choose2", "expected_revision": first["revision"], "selected_type": "structured_svg", "spec": first["spec"], "constraints": {"fill": "#0000ff"}}).json()
    assert second["plan"]["values"]["fill"] == "#0000ff"
    assert [s["source"] for s in second["plan"]["sources"]] == ["global_default", "project", "explicit_request"]


def test_visual_model_binding_is_separate_from_writing_binding(client):
    pid = project(client)
    profile = bind_image_model(client, pid, {"image_generation": True, "vision_analysis": True})
    writing = client.get(f"/api/projects/{pid}").json()["ai_binding"]
    other = client.post("/api/ai/profiles", json={"name": "Dedicated drawing", "base_url": "http://127.0.0.1:9998/v1", "auth_mode": "none", "models": ["draw-only"], "capabilities": {"draw-only": {"image_generation": True}}, "request_id": "separate"}).json()
    seed_generation_fixture(other['id'],'draw-only',('image_generation',))
    client.put(f"/api/ai/profiles/{other['id']}", json={"base_version": 1, "changes": {"status": "enabled"}})
    revision = client.get(f"/api/projects/{pid}").json()["project"]["project_revision"]
    payload = {"profile_id": other["id"], "model_id": "draw-only", "expected_project_revision": revision, "purpose": "drawing"}
    bound = client.put(url(pid, "/provider-binding"), json=payload)
    assert bound.status_code == 200, bound.text
    assert client.get(f"/api/projects/{pid}").json()["ai_binding"] == writing
    assert client.get(url(pid, "/capabilities")).json()["model"] == "draw-only"
    assert client.get(url(pid, "/capabilities?purpose=vision")).json()["model"] == "image-test"
    assert client.put(url(pid, "/provider-binding"), json=payload).status_code == 409
