from __future__ import annotations

import json
import sqlite3

import pytest

import app
from paperpilot.services import visual_assets, visual_evidence, visual_jobs
from tests.test_req022_assets import png, project, upload
from tests.test_req023_jobs import bind_image_model


def path(pid, suffix):
    return f"/api/projects/{pid}" + suffix


def check(client, pid, vid, request_id="check", **extra):
    created = client.post(path(pid, "/visual-checks"), json={"request_id": request_id, "asset_version_id": vid, **extra})
    assert created.status_code == 200, created.text
    run = client.get(path(pid, f"/visual-checks/{created.json()['id']}")).json()
    assert run["report_id"], run
    report = client.get(path(pid, f"/visual-reports/{run['report_id']}"))
    assert report.status_code == 200, report.text
    return report.json()


def review_payload(report, **extra):
    return {"report_version": 1, "expected_review_count": len(report["reviews"]), "request_id": "review", "decision": "reject", "reason": "坐标标签与来源不一致", "reviewer": "作者", "evidence_refs": ["Figure 2 axis"], **extra}


def test_no_cloud_consent_runs_local_and_never_false_pass(client, monkeypatch):
    from paperpilot.adapters import visual_vision
    calls = []
    monkeypatch.setattr(visual_vision, "analyze", lambda *a, **k: calls.append(1))
    pid = project(client)
    vid = upload(client, pid).json()["version"]["id"]
    before = client.get(path(pid, "")).json()["files"]
    r = check(client, pid, vid)
    findings = {f["check_type"]: f for f in r["findings"]}
    assert findings["file_integrity"]["status"] == "pass"
    assert findings["semantic"]["status"] == "unknown" and findings["source_consistency"]["status"] == "unknown"
    assert findings["cloud_review"]["method"] == "not_executed" and calls == []
    assert r["summary"] != "pass" and not r["blocks_build"]
    assert client.get(path(pid, "")).json()["files"] == before
    assert check(client, pid, vid)["id"] == r["id"]


def test_immutable_automatic_findings_and_append_only_reviews(client):
    pid = project(client)
    vid = upload(client, pid).json()["version"]["id"]
    r = check(client, pid, vid)
    response = client.post(path(pid, f"/visual-reports/{r['id']}/reviews"), json=review_payload(r))
    assert response.status_code == 200, response.text
    updated = response.json()
    assert updated["findings"] == r["findings"] and updated["reviews"][0]["decision"] == "reject"
    conflict = client.post(path(pid, f"/visual-reports/{r['id']}/reviews"), json=review_payload(r, request_id="conflict"))
    assert conflict.status_code == 409 and conflict.json()["detail"]["code"] == "review_conflict"
    conn = app.db()
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE visual_findings SET status='pass' WHERE report_id=?", (r["id"],))
        conn.rollback()
    finally:
        conn.close()


def test_asset_update_preserves_stale_report_and_review_history(client):
    pid = project(client)
    saved = upload(client, pid).json()
    r = check(client, pid, saved["version"]["id"])
    client.post(path(pid, f"/visual-reports/{r['id']}/reviews"), json=review_payload(r))
    import base64
    changed = client.post(path(pid, f"/drawing/assets/{saved['asset']['id']}/versions"), json={"filename": "new.png", "content_base64": base64.b64encode(png("blue")).decode(), "request_id": "replace", "expected_version": 1})
    assert changed.status_code == 200
    historical = client.get(path(pid, f"/visual-reports/{r['id']}")).json()
    assert historical["stale"] and historical["reviews"][0]["stale"]
    current = client.get(path(pid, f"/visual-reports/current?asset_version_id={saved['version']['id']}&purpose=build")).json()
    assert current["status"] == "unavailable" and not current["blocks_build"]
    assert client.post(path(pid, f"/visual-reports/{r['id']}/reviews"), json=review_payload(historical, request_id="new-review")).status_code == 409


def test_corpus_permission_withdrawal_and_rule_updates_invalidate_report(client):
    pid = project(client)
    vid = upload(client, pid).json()["version"]["id"]
    conn = app.db()
    with conn:
        conn.execute("INSERT INTO corpus(id,project_id,title,content) VALUES(?,?,?,?)", ("source", pid, "来源", "reference facts"))
        conn.execute("INSERT INTO corpus_source_versions(corpus_id,version,content,created_at) VALUES(?,?,?,?)", ("source", 1, "reference facts", visual_assets.stamp()))
    conn.close()
    r = check(client, pid, vid, source={"corpus_id": "source", "version": 1})
    conn = app.db()
    with conn:
        conn.execute("UPDATE corpus_source_versions SET permission_state='revoked' WHERE corpus_id='source'")
    conn.close()
    assert client.get(path(pid, f"/visual-reports/{r['id']}")).json()["stale"]
    denied = client.post(path(pid, "/visual-checks"), json={"asset_version_id": vid, "request_id": "revoked", "source": {"corpus_id": "source", "version": 1}})
    assert denied.status_code == 409
    another = check(client, pid, vid, "without-source")
    rule = client.post(path(pid, "/drawing/style-rules/structured_svg"), json={"request_id": "rule", "values": {"fill": "#010203"}}).json()
    client.post(path(pid, f"/drawing/style-rules/{rule['id']}/approve"))
    assert client.get(path(pid, f"/visual-reports/{another['id']}")).json()["stale"]


def test_style_requires_located_visual_region_and_only_creates_candidate(client):
    pid = project(client)
    vid = upload(client, pid).json()["version"]["id"]
    r = check(client, pid, vid)
    unavailable = client.post(path(pid, f"/visual-reports/{r['id']}/style-candidates")).json()
    assert unavailable["status"] == "unavailable" and not unavailable["published"]
    source = upload(client, pid, png("blue"), "reference.png", "reference").json()["version"]["id"]
    located = check(client, pid, vid, "located", source_asset_version=source, region={"x": 0.1, "y": 0.1, "width": 0.7, "height": 0.7}, check_types=["style"])
    candidate = client.post(path(pid, f"/visual-reports/{located['id']}/style-candidates"))
    assert candidate.status_code == 200, candidate.text
    assert candidate.json()["status"] == "candidate" and candidate.json()["values"]["fill"] == "#0000ff"
    conn = app.db()
    assert conn.execute("SELECT status FROM visual_style_rules WHERE id=?", (candidate.json()["rule_id"],)).fetchone()[0] == "candidate"
    assert visual_evidence.current(conn, pid, vid)["status"] == "current"
    conn.close()


def test_missing_or_corrupt_asset_is_failed_instead_of_fake_pass(client):
    pid = project(client)
    saved = upload(client, pid).json()
    conn = app.db()
    original = visual_assets.checked_file(conn, app.PROJECTS_DIR, pid, saved["version"]["id"], "original")
    conn.close()
    original.write_bytes(b"corrupt")
    r = check(client, pid, saved["version"]["id"])
    assert next(f for f in r["findings"] if f["check_type"] == "file_integrity")["status"] == "failed"


def test_cloud_requires_capability_consent_budget_and_unknown_is_durable(client, monkeypatch):
    from paperpilot.adapters import visual_vision
    calls = []
    pid = project(client)
    bind_image_model(client, pid, {"vision_analysis": True})
    vid = upload(client, pid).json()["version"]["id"]
    payload = {"request_id": "cloud", "asset_version_id": vid, "cloud": True, "external_consent": True, "unknown_price_confirmed": True, "hard_limit": 1}
    denied = client.post(path(pid, "/visual-checks"), json=payload)
    assert denied.status_code == 422 and denied.json()["detail"]["code"] == "budget_unverifiable"
    payload["hard_limit"] = None
    payload["external_consent"] = False
    assert client.post(path(pid, "/visual-checks"), json=payload).json()["detail"]["code"] == "external_consent_required"
    def unknown(*a, **k):
        from paperpilot.services.project_errors import ProjectServiceError
        calls.append(1)
        raise ProjectServiceError(503, "provider_unknown", "unknown")
    monkeypatch.setattr(visual_vision, "analyze", unknown)
    payload["external_consent"] = True
    created = client.post(path(pid, "/visual-checks"), json=payload).json()
    run = client.get(path(pid, f"/visual-checks/{created['id']}")).json()
    assert run["status"] == "unknown" and run["report_id"]
    client.post(path(pid, "/visual-checks"), json=payload)
    client.post(path(pid, f"/visual-checks/{created['id']}/reconcile"))
    assert len(calls) == 1


def test_cross_project_reports_hidden_and_nonblocking_read_only_build_projection(client):
    pid, other = project(client), project(client, "other")
    saved = upload(client, pid).json()
    r = check(client, pid, saved["version"]["id"])
    assert client.get(path(other, f"/visual-reports/{r['id']}")).status_code == 404
    conn = app.db()
    try:
        with conn:
            conn.execute("UPDATE files SET content=content||? WHERE project_id=? AND path='main.tex'", ("\n\\includegraphics{" + saved["version"]["build_name"] + "}", pid))
        from paperpilot.services import build_snapshots
        with conn:
            build = build_snapshots.create(conn, pid, "build", asset_storage_root=app.PROJECTS_DIR)
        assert build["manifest"]["visual_reports"][0]["report_id"] == r["id"]
        assert not build["manifest"]["visual_reports"][0]["blocks_build"]
        assert visual_evidence.projections_for_files(conn, pid, [{"content": "\\includegraphics{figures/asset_missing.pdf}"}]) == []
    finally:
        conn.close()
