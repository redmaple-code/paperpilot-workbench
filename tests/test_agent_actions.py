"""Action API behavior: explicit acceptance, isolation, CAS, idempotency and rollback."""
import json
import uuid
import pytest
import app
from tests.test_req022_assets import project


def create(client, pid, operation="outline", payload=None, sources=None):
    conn = app.db()
    if operation == "outline":
        from paperpilot.services.outline import current_version
        version = current_version(conn, pid)
    else:
        version = conn.execute("SELECT project_revision FROM projects WHERE id=?", (pid,)).fetchone()[0]
    conn.close()
    payload = payload or {"operations": [{"op": "add_node", "node": {"id": uuid.uuid4().hex, "title": "Reviewable proposal"}}]}
    response = client.post(f"/api/projects/{pid}/agent/actions", json={"request_id": uuid.uuid4().hex, "operation": operation,
                           "base_version": version, "payload": payload, "source_refs": sources or []})
    assert response.status_code == 200, response.text
    return response.json()


def preview(client, pid, card, **selection):
    response = client.post(f"/api/projects/{pid}/agent/actions/{card['id']}/preview", json=selection)
    assert response.status_code == 200, response.text
    return response.json()


def apply(client, pid, card, snapshot, request_id=None, **extra):
    return client.post(f"/api/projects/{pid}/agent/actions/{card['id']}/apply", json={"request_id": request_id or uuid.uuid4().hex,
                       "expected_version": snapshot["base_version"], "action_fingerprint": snapshot["action_fingerprint"], "accepted": True, **extra})


def test_proposal_and_preview_do_not_change_outline_and_explicit_acceptance_required(client):
    pid = project(client)
    before = client.get(f"/api/projects/{pid}/outline/structured").json()
    card = create(client, pid)
    snap = preview(client, pid, card)
    assert client.get(f"/api/projects/{pid}/outline/structured").json() == before
    assert apply(client, pid, card, snap, accepted=False).status_code == 422
    result = apply(client, pid, card, snap)
    assert result.status_code == 200, result.text
    assert result.json()["event"]["project_id"] == pid
    assert client.get(f"/api/projects/{pid}/outline/structured").json()["version"] == before["version"] + 1


def test_partial_selection_and_duplicate_requests(client):
    pid = project(client)
    payload = {"operations": [{"op": "add_node", "node": {"id": "chosen", "title": "Chosen"}}, {"op": "add_node", "node": {"id": "rejected", "title": "Not chosen"}}]}
    card = create(client, pid, payload=payload)
    snapshot = preview(client, pid, card, selected_indices=[0])
    first = apply(client, pid, card, snapshot, "same-request")
    assert first.status_code == 200, first.text
    replay = apply(client, pid, card, snapshot, "same-request")
    assert replay.status_code == 200 and replay.json()["idempotent_replay"]
    nodes = client.get(f"/api/projects/{pid}/outline/structured").json()["nodes"]
    assert "chosen" in {n["id"] for n in nodes} and "rejected" not in {n["id"] for n in nodes}
    assert apply(client, pid, card, snapshot, "same-request", accepted=False).status_code == 409


def test_second_preview_invalidates_first_and_empty_selection_is_not_all(client):
    pid = project(client)
    card = create(client, pid)
    first = preview(client, pid, card)
    second = preview(client, pid, card, selected_indices=[0])
    assert first["action_fingerprint"] != second["action_fingerprint"]
    assert apply(client, pid, card, first).status_code == 409
    empty = client.post(f"/api/projects/{pid}/agent/actions/{card['id']}/preview", json={"selected_indices": []})
    assert empty.status_code == 422


def test_old_version_cross_project_and_archive_preserve_candidate(client):
    pid, other = project(client), project(client, "other")
    card = create(client, pid)
    snapshot = preview(client, pid, card)
    assert client.post(f"/api/projects/{other}/agent/actions/{card['id']}/preview", json={}).status_code == 404
    conn = app.db()
    with conn:
        conn.execute("UPDATE projects SET archived=1 WHERE id=?", (pid,))
    conn.close()
    assert apply(client, pid, card, snapshot).status_code == 403
    assert client.get(f"/api/projects/{pid}/agent/actions").json()["cards"][0]["status"] == "previewed"


def test_action_event_failure_rolls_back_domain_write_and_receipt(client):
    pid = project(client)
    card = create(client, pid)
    snapshot = preview(client, pid, card)
    before = client.get(f"/api/projects/{pid}/outline/structured").json()
    conn = app.db()
    with conn:
        conn.execute("CREATE TRIGGER fail_action_event BEFORE INSERT ON agent_action_events BEGIN SELECT RAISE(ABORT,'event unavailable'); END")
    conn.close()
    failed = apply(client, pid, card, snapshot, "retry-after-failure")
    assert failed.status_code == 503
    assert client.get(f"/api/projects/{pid}/outline/structured").json() == before
    conn = app.db()
    with conn:
        conn.execute("DROP TRIGGER fail_action_event")
    assert conn.execute("SELECT count(*) FROM agent_action_receipts WHERE project_id=?", (pid,)).fetchone()[0] == 0
    conn.close()
    assert apply(client, pid, card, snapshot, "retry-after-failure").status_code == 200


def test_same_name_term_preview_keeps_old_definition(client):
    pid = project(client)
    original = client.post(f"/api/projects/{pid}/terms", json={"term": "CAS", "definition": "Original"}).json()
    card = create(client, pid, "term", {"term": "CAS", "definition": "Alternative"})
    snapshot = preview(client, pid, card)
    assert snapshot["diff"]["existing_same_name"][0]["definition"] == "Original"
    assert apply(client, pid, card, snapshot).status_code == 200
    conn = app.db()
    assert conn.execute("SELECT definition FROM terms WHERE id=?", (original["id"],)).fetchone()[0] == "Original"
    assert conn.execute("SELECT count(*) FROM terms WHERE project_id=? AND term='CAS'", (pid,)).fetchone()[0] == 2
    conn.close()


def test_source_revocation_prevents_application(client):
    pid = project(client)
    corpus = client.post(f"/api/projects/{pid}/corpus", json={"title": "Authorized input", "content": "A term and definition"}).json()
    card = create(client, pid, "term", {"term": "Term", "definition": "Candidate"}, [{"kind": "corpus", "id": corpus["id"], "version": 1}])
    snapshot = preview(client, pid, card)
    conn = app.db()
    with conn:
        conn.execute("UPDATE corpus_source_versions SET permission_state='revoked' WHERE corpus_id=?", (corpus["id"],))
    conn.close()
    assert apply(client, pid, card, snapshot).status_code == 403


def test_unknown_operation_is_rejected_and_rejection_has_no_domain_effect(client):
    pid = project(client)
    bad = client.post(f"/api/projects/{pid}/agent/actions", json={"request_id": "bad", "operation": "http://arbitrary/", "base_version": 0, "payload": {}})
    assert bad.status_code == 422
    card = create(client, pid)
    assert client.post(f"/api/projects/{pid}/agent/actions/{card['id']}/reject").json()["status"] == "rejected"
    assert client.post(f"/api/projects/{pid}/agent/actions/{card['id']}/preview", json={}).status_code == 409


def test_manual_structure_edit_makes_old_action_stale(client):
    pid = project(client)
    card = create(client, pid)
    snap = preview(client, pid, card)
    changed = client.post(f"/api/projects/{pid}/outline/nodes", json={"command":"create", "base_version":card["base_version"],
                        "request_id":"manual-edit", "node":{"title":"Manual new version"}})
    assert changed.status_code == 200, changed.text
    result = apply(client, pid, card, snap)
    assert result.status_code == 409
    nodes = client.get(f"/api/projects/{pid}/outline/structured").json()["nodes"]
    assert "Manual new version" in [n["title"] for n in nodes] and "Reviewable proposal" not in [n["title"] for n in nodes]
