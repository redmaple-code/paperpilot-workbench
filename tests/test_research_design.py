"""Research-design guidance: validation, consent gating and draft application.

The model call itself is verified only against the real provider (see the
delivery notes). Nothing here fakes an AI response: candidates are inserted as
already-stored rows so the surrounding rules can be tested on their own.
"""
from __future__ import annotations

import json
import uuid

import app
from paperpilot.services.research_stage_policies import POLICY_VERSION
from tests.test_req022_assets import project


def base(pid):
    return f"/api/projects/{pid}/workflow/research-design"


def read_startup(client, pid):
    return client.get(f"/api/projects/{pid}/workflow/startup").json()


def save_stage(client, pid, fields):
    view = read_startup(client, pid)
    return client.post(f"/api/projects/{pid}/workflow/startup/commands", json={
        "command": "save", "request_id": uuid.uuid4().hex,
        "expected_workflow_version": view["workflow_version"],
        "stage_id": view["current_stage"], "fields": fields,
    })


def test_preview_requires_known_operators_and_returns_no_cards(client):
    pid = project(client)
    bad = client.post(base(pid) + "/preview", json={"stage_id": "STAGE-01", "operators": ["not_an_operator"]})
    assert bad.status_code == 422 and bad.json()["detail"]["code"] == "invalid_operators"

    empty = client.post(base(pid) + "/preview", json={"stage_id": "STAGE-01", "operators": []})
    assert empty.status_code == 422

    ok = client.post(base(pid) + "/preview", json={"stage_id": "STAGE-01", "operators": ["limitation_aggregation"]})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["operators"] == ["limitation_aggregation"]
    assert body["preview_fingerprint"]
    assert len(body["operator_definitions"]) == 9
    assert len(body["red_team_definitions"]) == 6
    assert "cards" not in body
    assert body["outbound_preview"]["研究问题"] == ""
    # A preview alone never creates a candidate.
    assert client.get(base(pid)).json()["candidates"] == []


def test_generate_requires_consent_and_exact_fingerprint(client):
    pid = project(client)
    preview = client.post(base(pid) + "/preview",
                          json={"stage_id": "STAGE-01", "operators": ["matrix_gap"]}).json()
    payload = {
        "request_id": uuid.uuid4().hex, "stage_id": "STAGE-01",
        "operators": ["matrix_gap"], "preview_fingerprint": preview["preview_fingerprint"],
    }
    no_consent = client.post(base(pid), json={**payload, "consent_to_send": False})
    assert no_consent.status_code == 409 and no_consent.json()["detail"]["code"] == "consent_required"

    stale = client.post(base(pid), json={**payload, "preview_fingerprint": "0" * 64, "consent_to_send": True})
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "research_design_preview_stale"

    # Changing the stage after preview invalidates the fingerprint.
    save_stage(client, pid, {"research_question": "改了研究问题"})
    drifted = client.post(base(pid), json={**payload, "consent_to_send": True, "possible_cost_confirmed": True})
    assert drifted.status_code == 409


def test_apply_writes_only_a_draft_and_records_provenance(client):
    pid = project(client)
    card = {
        "policy_version": POLICY_VERSION, "stage_id": "STAGE-01",
        "id": "card-1", "operator": "局限聚合",
        "gap": "单指标评估无法处理证据冲突", "idea": "多指标证据聚合协议",
        "hypothesis": "聚合后可复现性提高", "falsifiable_prediction": "跨设置分级一致率提升",
        "verification_plan": "两套设置重复计算", "resources": "既有小规模算例",
        "red_team": {key: "已自评" for key in
                     ("done_before", "premise", "feasibility", "importance", "measurement", "increment")},
        "nearest_neighbors": [{"work": "稳定性判据综述", "relation": "相邻", "crash_level": "中"}],
        "search_queries": ["2D material stability criteria"], "limitations": "未查重",
    }
    conn = app.db()
    with conn:
        conn.execute(
            "INSERT INTO research_design_candidates(id,project_id,stage_id,request_id,operators_json,cards_json,"
            "notes_json,origin_json,status,applied_card_json,base_workflow_version,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("cand-1", pid, "STAGE-01", "seed-1", json.dumps(["limitation_aggregation"]),
             json.dumps([card], ensure_ascii=False), "{}", "{}", "candidate_ready", "{}", 1,
             "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
        )
    conn.close()

    before = read_startup(client, pid)
    applied = client.post(base(pid) + "/apply", json={
        "request_id": uuid.uuid4().hex, "candidate_id": "cand-1", "card_id": "card-1",
        "expected_workflow_version": before["workflow_version"], "field": "contribution",
    })
    assert applied.status_code == 200, applied.text

    after = read_startup(client, pid)
    stage = after["plan"]["stages"][0]
    assert stage["status"] == "draft"                      # written, still unconfirmed
    assert after["plan"]["status"] == "active"
    assert "多指标证据聚合协议" in stage["fields"]["contribution"]
    assert "尚未查重" in stage["fields"]["contribution"] or "待核验" in stage["fields"]["contribution"]
    assert any("scholar-apprentice" in (ref.get("label") or "") for ref in stage["source_refs"])
    assert after["confirmed_context"] == []                # nothing enters confirmed context

    listed = client.get(base(pid)).json()["candidates"]
    assert listed[0]["status"] == "applied"


def test_apply_rejects_unknown_card_and_closed_stage(client):
    pid = project(client)
    view = read_startup(client, pid)
    missing = client.post(base(pid) + "/apply", json={
        "request_id": uuid.uuid4().hex, "candidate_id": "nope", "card_id": "nope",
        "expected_workflow_version": view["workflow_version"], "field": "contribution",
    })
    assert missing.status_code == 404

    # Generation is independent of confirmation; preview alone cannot modify review state.
    definition = next(d for d in view["definitions"] if d["id"] == view["current_stage"])
    fields = {key: "内容" for key in definition["fields"]}
    review = client.post(f"/api/projects/{pid}/workflow/startup/commands", json={
        "command": "review", "request_id": uuid.uuid4().hex,
        "expected_workflow_version": view["workflow_version"],
        "stage_id": view["current_stage"], "fields": fields,
    })
    assert review.status_code == 200, review.text
    blocked = client.post(base(pid) + "/preview", json={"stage_id": "STAGE-01", "operators": ["matrix_gap"]})
    assert blocked.status_code == 200
    assert read_startup(client, pid)["plan"]["stages"][0]["status"] == "review"
