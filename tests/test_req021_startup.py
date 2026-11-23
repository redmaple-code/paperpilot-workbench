from __future__ import annotations

import concurrent.futures
import copy

import app
from paperpilot.services import startup_workflow as startup
from tests.test_req022_assets import project


def base(pid):
    return f"/api/projects/{pid}/workflow/startup"


def read(client, pid):
    response = client.get(base(pid))
    assert response.status_code == 200, response.text
    return response.json()


def command(client, pid, action, view=None, request_id=None, **extra):
    view = view or read(client, pid)
    payload = {"command": action, "request_id": request_id or action+"-"+str(view["workflow_version"]),
        "expected_workflow_version": view["workflow_version"], "stage_id": view["current_stage"], **extra}
    return client.post(base(pid)+"/commands", json=payload)


def review_and_confirm(client, pid, marker="confirmed"):
    view = read(client, pid)
    definition = next(s for s in view["definitions"] if s["id"] == view["current_stage"])
    fields = {key: marker+"-"+key for key in definition["fields"]}
    reviewed = command(client, pid, "review", view, fields=fields, source_refs=[{"label": "作者研究记录", "version": "v1", "location": "研究笔记"}])
    assert reviewed.status_code == 200, reviewed.text
    assert reviewed.json()["plan"]["stages"][view["plan"]["current_index"]]["status"] == "review"
    confirmed = command(client, pid, "confirm", reviewed.json())
    assert confirmed.status_code == 200, confirmed.text
    return confirmed.json()


def test_five_stages_are_real_outputs_and_only_confirmed_prefix_enters_context(client):
    pid = project(client)
    initial = read(client, pid)
    # A new paper opens inside planning, not in a "not started" limbo.
    assert len(initial["definitions"]) == 5
    assert initial["plan"]["status"] == "active" and initial["plan"]["branch_id"]
    before = client.get(f"/api/projects/{pid}").json()["files"]
    incomplete = command(client, pid, "review", initial, fields={"research_question": "private-draft-not-sent"})
    assert incomplete.status_code == 200
    draft = incomplete.json()
    assert draft["plan"]["stages"][0]["status"] == "draft"
    assert [field["key"] for field in draft["missing_fields"]] == ["scope"]
    assert "草稿已保存" in draft["message"]
    context = client.get(f"/api/projects/{pid}/ai-context").json()
    assert "private-draft-not-sent" not in context["preview"]
    assert command(client, pid, "confirm", draft).status_code == 409
    confirmed = review_and_confirm(client, pid)
    assert confirmed["current_stage"] == "STAGE-02" and len(confirmed["confirmed_context"]) == 1
    context = client.get(f"/api/projects/{pid}/ai-context").json()["preview"]
    assert "confirmed-research_question" in context and "private-draft-not-sent" not in context
    for _ in range(4):
        confirmed = review_and_confirm(client, pid)
    assert confirmed["plan"]["status"] == "complete" and len(confirmed["confirmed_context"]) == 5
    assert client.get(f"/api/projects/{pid}").json()["files"] == before


def test_conflict_idempotency_and_noncurrent_stage_never_overwrite(client):
    pid = project(client)
    initial = read(client, pid)
    first = command(client, pid, "save", initial, "save-once", fields={"research_question": "one"})
    assert first.status_code == 200
    replay = command(client, pid, "save", initial, "save-once", fields={"research_question": "one"})
    assert replay.status_code == 200 and replay.json()["idempotent_replay"]
    assert command(client, pid, "save", initial, "different-payload", fields={"research_question": "two"}).status_code == 409
    assert command(client, pid, "save", initial, "save-once", fields={"research_question": "different"}).json()["detail"]["code"] == "idempotency_key_reused"
    view = read(client, pid)
    saved = command(client, pid, "save", view, stage_id="STAGE-03", fields={"structure": "other"})
    assert saved.status_code == 200
    assert saved.json()["plan"]["current_index"] == view["plan"]["current_index"]
    assert saved.json()["plan"]["stages"][2]["fields"]["structure"] == "other"
    assert saved.json()["confirmed_context"] == view["confirmed_context"]
    assert read(client, pid)["plan"]["stages"][0]["fields"]["research_question"] == "one"


def test_back_restart_cancel_resume_and_optional_skip_keep_history(client):
    pid = project(client)
    initial = read(client, pid)
    assert command(client, pid, "skip", initial, reason="cannot skip required").status_code == 409
    for i in range(4):
        view = review_and_confirm(client, pid, f"stage-{i}")
    assert view["current_stage"] == "STAGE-05"
    assert command(client, pid, "skip", view).json()["detail"]["code"] == "skip_reason_required"
    skipped = command(client, pid, "skip", view, reason="先完成研究问题，稍后独立估算").json()
    assert skipped["plan"]["status"] == "complete"
    old_version = skipped["workflow_version"]
    back = command(client, pid, "back", skipped, stage_id="STAGE-02").json()
    assert len(back["confirmed_context"]) == 1
    assert all(s["status"] == "draft" for s in back["plan"]["stages"][1:])
    assert back["plan"]["stages"][2]["fields"]["structure"]
    historical = client.get(base(pid)+f"/history/{old_version}").json()
    assert historical["plan"]["stages"][2]["status"] == "confirmed"
    cancelled = command(client, pid, "cancel", back).json()
    assert cancelled["plan"]["status"] == "cancelled" and cancelled["confirmed_context"] == []
    resumed = command(client, pid, "resume", cancelled).json()
    assert resumed["plan"]["stages"][1]["fields"] == back["plan"]["stages"][1]["fields"]
    restarted = command(client, pid, "restart", resumed).json()
    assert restarted["plan"]["branch_id"] != resumed["plan"]["branch_id"]
    assert restarted["plan"]["parent_branch"]["workflow_version"] == resumed["workflow_version"]
    assert not restarted["confirmed_context"] and not restarted["plan"]["stages"][0]["fields"]["research_question"]


def test_explicit_copy_is_target_owned_unconfirmed_and_independent(client):
    source, target = project(client), project(client, "target")
    source_view = review_and_confirm(client, source)
    target_view = read(client, target)
    assert target_view["plan"]["stages"][0]["fields"]["research_question"] == ""
    payload = {"request_id": "copy", "expected_workflow_version": target_view["workflow_version"], "source_project_id": source, "source_version": source_view["workflow_version"]}
    copied = client.post(base(target)+"/copy", json=payload)
    assert copied.status_code == 200, copied.text
    view = copied.json()
    assert view["plan"]["project_id"] == target and view["plan"]["branch_id"] != source_view["plan"]["branch_id"]
    assert not view["confirmed_context"] and all(s["status"] == "draft" for s in view["plan"]["stages"])
    assert view["plan"]["lineage"]["source_version"] == source_view["workflow_version"]
    assert client.post(base(target)+"/copy", json=payload).json()["idempotent_replay"]
    assert client.get(base(source)+f"/history/{source_view['workflow_version']}").status_code == 200
    assert client.get(base(target)+f"/history/{source_view['workflow_version']}").status_code == 404
    command(client, target, "save", view, fields={"research_question": "target-only"})
    assert read(client, source)["plan"]["stages"][0]["fields"]["research_question"] == "confirmed-research_question"
    assert client.get(base(target)+f"/history/{read(client, target)['workflow_version']}").json()["plan"]["stages"][0]["fields"]["research_question"] == "target-only"


def test_two_concurrent_writers_have_one_commit_and_one_visible_conflict(client):
    pid = project(client)
    view = read(client, pid)
    def writer(n):
        conn = app.db()
        try:
            with conn:
                result = startup.command(conn, pid, {"request_id": f"writer-{n}", "expected_workflow_version": view["workflow_version"], "command": "save", "stage_id": "STAGE-01", "fields": {"research_question": str(n)}, "source_refs": None, "reason": ""})
            return result["workflow_version"]
        except Exception as exc:
            return getattr(exc, "code", type(exc).__name__)
        finally:
            conn.close()
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        results = list(pool.map(writer, [1, 2]))
    assert sorted(map(str, results)) == ["2", "workflow_conflict"]
    assert read(client, pid)["workflow_version"] == 2


def test_planning_is_advisory_and_never_blocks_writing(client):
    pid = project(client)
    saved = command(client, pid, "save", fields={"research_question": "planning"}).json()
    wf = client.get(f"/api/projects/{pid}/workflow").json()
    assert wf["startup"]["workflow_version"] == saved["workflow_version"]
    assert wf["phase"] == "planning" and wf["current_stage"] == "STAGE-01"
    # Incomplete planning only produces reminders; switching to writing is free.
    assert wf["ai_allowed"] is True
    assert any(item["code"] == "planning_incomplete" for item in wf["warnings"])
    started = client.post(f"/api/projects/{pid}/workflow/commands", json={"command": "start", "base_version": wf["version"], "request_id": "start"})
    assert started.status_code == 200, started.text
    assert client.post(f"/api/projects/{pid}/human-revision", json={"workflow_version": started.json()["version"]}).status_code == 200
    # Confirming every planning stage moves the workflow position itself.
    for i in range(5):
        review_and_confirm(client, pid, f"gate-{i}")
    done = client.get(f"/api/projects/{pid}/workflow").json()
    assert done["startup"]["planning_complete"] is True
    # Completion parks the workflow position at human_revision; no second switch needed.
    assert done["current_stage"] == "human_revision" and done["phase"] == "writing"
    assert done["warnings"] == []
    # A stale planning edit cannot roll the workflow back into planning.
    assert command(client, pid, "save", saved, fields={"research_question": "old-input"}).status_code == 409
    assert read(client, pid)["plan"]["stages"][0]["fields"]["research_question"] == "gate-0-research_question"


def test_initial_plan_is_active_and_start_action_is_the_only_entry(client):
    pid = project(client)
    initial = read(client, pid)
    assert initial["plan"]["status"] == "active"
    assert initial["allowed_actions"] == ["save", "review", "cancel", "back", "restart"]
    wf = client.get(f"/api/projects/{pid}/workflow").json()
    assert wf["phase"] == "planning" and wf["current_stage"] == "STAGE-01" and wf["stage_status"] == "planning_start"


def test_legacy_snapshot_without_plan_only_offers_start(client):
    pid = project(client)
    # Simulate a snapshot written before planning was embedded in the workflow.
    conn = app.db()
    with conn:
        conn.execute("UPDATE workflow_snapshots SET stage_outputs_json='{}',current_stage='created',stage_status='context_ready' WHERE project_id=?", (pid,))
    conn.close()
    view = read(client, pid)
    assert view["plan"]["status"] == "not_started"
    assert view["allowed_actions"] == ["start"]
    wf = client.get(f"/api/projects/{pid}/workflow").json()
    assert wf["phase"] == "legacy"
    started = command(client, pid, "start", view)
    assert started.status_code == 200, started.text
    assert started.json()["plan"]["status"] == "active"


def test_terminal_skip_completes_planning_and_records_reason(client):
    pid = project(client)
    for i in range(4):
        view = review_and_confirm(client, pid, f"sk-{i}")
    assert view["current_stage"] == "STAGE-05"
    skipped = command(client, pid, "skip", view, reason="资源计划稍后单独估算").json()
    assert skipped["plan"]["status"] == "complete"
    assert skipped["plan"]["skipped_terminal"] is True
    assert skipped["plan"]["stages"][4]["skip_reason"] == "资源计划稍后单独估算"
    assert skipped["planning_complete"] is True
    wf = client.get(f"/api/projects/{pid}/workflow").json()
    assert wf["phase"] == "writing" and wf["current_stage"] == "human_revision"


def test_planning_cancel_keeps_position_in_planning(client):
    pid = project(client)
    command(client, pid, "save", fields={"research_question": "暂停前草稿"}).json()
    view = read(client, pid)
    cancelled = command(client, pid, "cancel", view).json()
    assert cancelled["plan"]["status"] == "cancelled"
    wf = client.get(f"/api/projects/{pid}/workflow").json()
    assert wf["phase"] == "planning_cancelled"
    assert wf["current_stage"] == "STAGE-01" and wf["stage_status"] == "planning_cancelled"
    resumed = command(client, pid, "resume", cancelled).json()
    assert resumed["plan"]["status"] == "active"
    assert client.get(f"/api/projects/{pid}/workflow").json()["phase"] == "planning"


def test_command_storage_failure_rolls_back_snapshot_and_keeps_retryable_draft(client):
    pid = project(client)
    saved = command(client, pid, "save", fields={"research_question": "保留的草稿"}).json()
    conn = app.db()
    with conn:
        conn.execute("CREATE TRIGGER injected_startup_failure BEFORE INSERT ON workflow_commands WHEN NEW.request_id='fail-save' BEGIN SELECT RAISE(ABORT,'injected storage failure'); END")
    conn.close()
    payload = {"request_id": "fail-save", "command": "save", "expected_workflow_version": saved["workflow_version"], "stage_id": "STAGE-01", "fields": {"research_question": "新草稿"}}
    failed = client.post(base(pid)+"/commands", json=payload)
    assert failed.status_code == 503 and failed.json()["detail"]["code"] == "workflow_storage_failed"
    unchanged = read(client, pid)
    assert unchanged["workflow_version"] == saved["workflow_version"]
    assert unchanged["plan"]["stages"][0]["fields"]["research_question"] == "保留的草稿"
    conn = app.db()
    assert conn.execute("SELECT COUNT(*) FROM workflow_commands WHERE request_id='fail-save'").fetchone()[0] == 0
    with conn:
        conn.execute("DROP TRIGGER injected_startup_failure")
    conn.close()
    retried = client.post(base(pid)+"/commands", json=payload)
    assert retried.status_code == 200
    assert retried.json()["workflow_version"] == saved["workflow_version"]+1
