"""Durable drawing runs. An attempt is reserved before I/O and never blindly replayed."""
from __future__ import annotations

import json
import sqlite3
import uuid
import time
from pathlib import Path

from paperpilot.services import visual_assets as assets, visual_generation as generation
from paperpilot.services.project_errors import ProjectServiceError

compact, digest, stamp = assets.compact, assets.digest, assets.stamp


def initialize_schema(conn: sqlite3.Connection):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS drawing_runs (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
      request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, title TEXT NOT NULL, intent TEXT NOT NULL,
      status TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, recommendation_json TEXT NOT NULL,
      selected_type TEXT, spec_json TEXT NOT NULL DEFAULT '{}', plan_json TEXT NOT NULL DEFAULT '{}',
      context_json TEXT NOT NULL DEFAULT '{}', input_asset_version TEXT,
      history_json TEXT NOT NULL DEFAULT '[]', history_cursor INTEGER NOT NULL DEFAULT -1,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(project_id,request_id)
    );
    CREATE TABLE IF NOT EXISTS drawing_attempts (
      id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES drawing_runs(id), project_id TEXT NOT NULL,
      status TEXT NOT NULL, input_hash TEXT NOT NULL, snapshot_json TEXT NOT NULL,
      gate_json TEXT NOT NULL, usage_json TEXT NOT NULL DEFAULT '{}', error_code TEXT,
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS drawing_candidates (
      id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES drawing_runs(id), attempt_id TEXT NOT NULL UNIQUE,
      project_id TEXT NOT NULL, input_hash TEXT NOT NULL, content BLOB NOT NULL, filename TEXT NOT NULL,
      sha256 TEXT NOT NULL, status TEXT NOT NULL, saved_json TEXT, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS drawing_events (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, run_id TEXT NOT NULL, event TEXT NOT NULL,
      details_json TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS drawing_operations (
      project_id TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, result_json TEXT NOT NULL,
      PRIMARY KEY(project_id,request_id)
    );
    CREATE TABLE IF NOT EXISTS drawing_scene_versions (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, run_id TEXT NOT NULL, spec_json TEXT NOT NULL,
      selected_type TEXT NOT NULL, constraints_json TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS visual_style_rules (
      id TEXT PRIMARY KEY, scope TEXT NOT NULL, scope_id TEXT NOT NULL, kind TEXT NOT NULL,
      version INTEGER NOT NULL, status TEXT NOT NULL, values_json TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS project_visual_provider_bindings (
      project_id TEXT NOT NULL REFERENCES projects(id), purpose TEXT NOT NULL,
      profile_id TEXT NOT NULL REFERENCES ai_provider_profiles(id), model_id TEXT NOT NULL,
      profile_version INTEGER NOT NULL, revision INTEGER NOT NULL,
      PRIMARY KEY(project_id,purpose)
    );
    CREATE INDEX IF NOT EXISTS idx_drawing_runs_project ON drawing_runs(project_id,created_at);
    """)


def begin(conn):
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def event(conn, run, name, details=None):
    conn.execute("INSERT INTO drawing_events VALUES(?,?,?,?,?,?)", (uuid.uuid4().hex, run["project_id"], run["id"], name, compact(details or {}), stamp()))


def operation(conn, project_id, request_id, payload):
    fp = digest(compact(payload).encode())
    old = conn.execute("SELECT * FROM drawing_operations WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
    if old:
        if old["fingerprint"] != fp:
            raise ProjectServiceError(409, "idempotency_key_reused", "此请求编号已用于不同绘图操作；请刷新后重试。")
        return fp, json.loads(old["result_json"]) | {"idempotent_replay": True}
    return fp, None


def remember(conn, project_id, request_id, fp, result):
    conn.execute("INSERT INTO drawing_operations VALUES(?,?,?,?)", (project_id, request_id, fp, compact(result)))
    return result


def row(conn, project_id, run_id):
    run = conn.execute("SELECT * FROM drawing_runs WHERE project_id=? AND id=?", (project_id, run_id)).fetchone()
    if run is None:
        raise ProjectServiceError(404, "resource_missing", "绘图任务不存在于当前论文。")
    return run


def check_revision(run, revision):
    if run["revision"] != revision:
        raise ProjectServiceError(409, "version_conflict", "绘图任务已变化，请刷新后再操作；你的输入仍可保留。")


def context(conn, project_id, spec):
    p = conn.execute("SELECT project_revision,ai_profile_id,ai_model_id,ai_config_revision,domain_id FROM projects WHERE id=?", (project_id,)).fetchone()
    result = dict(p)
    for key in ("input_asset_version", "mask_version"):
        vid = spec.get(key)
        if vid:
            v = assets.version(conn, project_id, vid)
            a = assets.asset(conn, project_id, v["asset_id"])
            if a["status"] != "active" or a["current_version_id"] != vid or v["status"] != "ready":
                raise ProjectServiceError(409, "stale_input", "所选输入图像已更新、归档或不可用，请重新选择版本。")
            result[key] = {"id": vid, "sha256": v["sha256"], "asset_revision": a["revision"]}
    return result


def input_hash(run):
    return digest(compact({k: run[k] for k in ("selected_type", "spec_json", "plan_json", "context_json")}).encode())


def stale_reasons(conn, run):
    if not run["selected_type"]:
        return []
    spec = json.loads(run["spec_json"])
    try:
        if context(conn, run["project_id"], spec) != json.loads(run["context_json"]):
            return ["论文配置或输入图像版本发生变化"]
        old_plan = json.loads(run["plan_json"])
        constraints = next((s["values"] for s in old_plan["sources"] if s["source"] == "explicit_request"), {})
        if generation.compile_style(conn, run["project_id"], run["selected_type"], constraints) != old_plan:
            return ["已批准风格规则发生变化"]
    except ProjectServiceError:
        return ["输入图像已更新、归档或不可读取"]
    return []


def read(conn, project_id, run_id):
    run = row(conn, project_id, run_id)
    result = {k: run[k] for k in ("id", "project_id", "title", "intent", "status", "revision", "selected_type", "created_at", "updated_at")}
    for key in ("recommendation", "spec", "plan", "context", "history"):
        result[key] = json.loads(run[key + "_json"])
    result["history_cursor"] = run["history_cursor"]
    result["stale_reasons"] = stale_reasons(conn, run)
    result["candidates"] = [candidate(conn, project_id, c["id"]) for c in conn.execute("SELECT id FROM drawing_candidates WHERE run_id=? ORDER BY created_at", (run_id,))]
    result["attempts"] = [dict(a) for a in conn.execute("SELECT id,status,error_code,usage_json,created_at,updated_at FROM drawing_attempts WHERE run_id=?", (run_id,))]
    result["events"] = [dict(e) for e in conn.execute("SELECT event,details_json,created_at FROM drawing_events WHERE run_id=? ORDER BY created_at", (run_id,))]
    return result


def create(conn, project_id, payload):
    begin(conn)
    fp, prior = operation(conn, project_id, payload["request_id"], {"action": "create", **payload})
    if prior:
        return read(conn, project_id, prior["id"]) | {"idempotent_replay": True}
    spec = {"input_asset_version": payload.get("input_asset_version")}
    context(conn, project_id, spec)
    run_id = uuid.uuid4().hex
    conn.execute("INSERT INTO drawing_runs(id,project_id,request_id,fingerprint,title,intent,status,recommendation_json,input_asset_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                 (run_id, project_id, payload["request_id"], fp, payload["title"], payload["intent"], "awaiting_confirmation", compact(generation.suggest(payload["intent"])), payload.get("input_asset_version"), stamp(), stamp()))
    event(conn, row(conn, project_id, run_id), "recommendation_ready")
    return remember(conn, project_id, payload["request_id"], fp, read(conn, project_id, run_id))


def decide(conn, project_id, run_id, payload):
    begin(conn)
    fp, prior = operation(conn, project_id, payload["request_id"], {"action": "decision", "run_id": run_id, **payload})
    if prior:
        return prior
    run = row(conn, project_id, run_id)
    check_revision(run, payload["expected_revision"])
    if run["status"] == "cancelled":
        raise ProjectServiceError(409, "run_cancelled", "任务已取消；请复制为新任务后重新确认。")
    if conn.execute("SELECT 1 FROM drawing_attempts WHERE run_id=?", (run_id,)).fetchone():
        raise ProjectServiceError(409, "attempt_exists", "本任务已有执行记录；请复制为新任务修改输入，避免重复收费或覆盖候选。")
    kind = payload["selected_type"]
    spec = generation.validate_spec(kind, payload["spec"])
    if run["input_asset_version"] and not spec.get("input_asset_version"):
        if kind != "ai_image":
            raise ProjectServiceError(422, "invalid_spec", "引用图像编辑请选择 AI 图片工具或新建无参考图任务。")
        spec["input_asset_version"] = run["input_asset_version"]
    generation.validate_spec(kind, spec)
    ctx = context(conn, project_id, spec)
    plan = generation.compile_style(conn, project_id, kind, payload["constraints"])
    hid = uuid.uuid4().hex
    conn.execute("INSERT INTO drawing_scene_versions VALUES(?,?,?,?,?,?,?)", (hid, project_id, run_id, compact(spec), kind, compact(payload["constraints"]), stamp()))
    history = json.loads(run["history_json"])[:run["history_cursor"] + 1] + [hid]
    conn.execute("UPDATE drawing_runs SET selected_type=?,spec_json=?,plan_json=?,context_json=?,status='tool_ready',revision=revision+1,history_json=?,history_cursor=?,updated_at=? WHERE id=?",
                 (kind, compact(spec), compact(plan), compact(ctx), compact(history), len(history)-1, stamp(), run_id))
    event(conn, run, "tool_confirmed", {"kind": kind, "snapshot": hid})
    return remember(conn, project_id, payload["request_id"], fp, read(conn, project_id, run_id))


def history_command(conn, project_id, run_id, payload, direction):
    begin(conn)
    fp, prior = operation(conn, project_id, payload["request_id"], {"action": direction, "run_id": run_id, **payload})
    if prior:
        return prior
    run = row(conn, project_id, run_id)
    check_revision(run, payload["expected_revision"])
    if conn.execute("SELECT 1 FROM drawing_attempts WHERE run_id=?", (run_id,)).fetchone():
        raise ProjectServiceError(409, "attempt_exists", "已执行任务保持固定输入；请复制任务后编辑。")
    history = json.loads(run["history_json"])
    cursor = run["history_cursor"] + (-1 if direction == "undo" else 1)
    if not 0 <= cursor < len(history):
        raise ProjectServiceError(409, "history_boundary", "没有更多可撤销或重做的编辑。")
    v = conn.execute("SELECT * FROM drawing_scene_versions WHERE id=?", (history[cursor],)).fetchone()
    plan = generation.compile_style(conn, project_id, v["selected_type"], json.loads(v["constraints_json"]))
    ctx = context(conn, project_id, json.loads(v["spec_json"]))
    conn.execute("UPDATE drawing_runs SET selected_type=?,spec_json=?,plan_json=?,context_json=?,history_cursor=?,revision=revision+1,updated_at=? WHERE id=?", (v["selected_type"], v["spec_json"], compact(plan), compact(ctx), cursor, stamp(), run_id))
    event(conn, run, direction, {"snapshot": v["id"]})
    return remember(conn, project_id, payload["request_id"], fp, read(conn, project_id, run_id))


def reserve(conn, project_id, run_id, payload,temporary_key=None):
    from paperpilot.services.visual_provider import gate
    begin(conn)
    fp, prior = operation(conn, project_id, payload["request_id"], {"action": "dispatch", "run_id": run_id, **payload})
    if prior:
        return prior
    run = row(conn, project_id, run_id)
    check_revision(run, payload["expected_revision"])
    if run["status"] != "tool_ready":
        raise ProjectServiceError(409, "tool_not_ready", "请先确认绘图类型与输入；已有任务需先查看或对账，不能重复派发。")
    reasons = stale_reasons(conn, run)
    if reasons:
        raise ProjectServiceError(409, "stale_input", "；".join(reasons) + "。请重新确认输入。")
    admission = gate(conn, run, payload,temporary_key=temporary_key) if run["selected_type"] == "ai_image" else {"adapter": "local_svg_v1", "estimated_cost": 0, "currency": "USD"}
    aid = uuid.uuid4().hex
    if run['selected_type']=='ai_image':
        from paperpilot.services import provider_runtime
        provider_runtime.begin_audit(conn,aid,project_id,payload['request_id'],admission,admission['mode'],input_hash(run),{'run_id':run_id,'input_hash':input_hash(run)})
    snapshot = {"kind": run["selected_type"], "spec": json.loads(run["spec_json"]), "plan": json.loads(run["plan_json"]), "context": json.loads(run["context_json"])}
    conn.execute("INSERT INTO drawing_attempts(id,run_id,project_id,status,input_hash,snapshot_json,gate_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (aid, run_id, project_id, "queued", input_hash(run), compact(snapshot), compact(admission), stamp(), stamp()))
    conn.execute("UPDATE drawing_runs SET status='queued',revision=revision+1,updated_at=? WHERE id=?", (stamp(), run_id))
    event(conn, run, "queued", {"attempt_id": aid, "adapter": admission["adapter"], "estimated_cost": admission.get("estimated_cost")})
    return remember(conn, project_id, payload["request_id"], fp, {"run_id": run_id, "attempt_id": aid, "status": "queued", "revision": run["revision"] + 1, "gate": admission})


def execute(database, storage_root: Path, project_write, project_id, run_id, attempt_id,temporary_key=None):
    started=time.perf_counter()
    from paperpilot.adapters.visual_image import generate
    from paperpilot.services.visual_provider import verify_gate
    conn = database()
    try:
        with conn:
            begin(conn)
            run = row(conn, project_id, run_id)
            attempt = conn.execute("SELECT * FROM drawing_attempts WHERE id=? AND run_id=?", (attempt_id, run_id)).fetchone()
            if attempt is None or attempt["status"] != "queued" or run["status"] == "cancelled":
                return
            project_write(project_id)
            if stale_reasons(conn, run):
                raise ProjectServiceError(409, "stale_input", "任务输入已变化。")
            snapshot, gate = json.loads(attempt["snapshot_json"]), json.loads(attempt["gate_json"])
            reference = mask = None
            if snapshot["kind"] == "ai_image":
                verify_gate(conn, project_id, gate)
                for key in ("input_asset_version", "mask_version"):
                    vid = snapshot["spec"].get(key)
                    if vid:
                        version = assets.version(conn, project_id, vid)
                        if version["mime_type"] not in {"image/png", "image/jpeg", "image/webp"}:
                            raise ProjectServiceError(422, "invalid_reference", "AI 图片参考图和遮罩请选择 PNG、JPEG 或 WebP 资产。")
                        data = assets.checked_file(conn, storage_root, project_id, vid, "original").read_bytes()
                        item = (version["filename"], data, version["mime_type"])
                        if key == "mask_version":
                            mask = item
                        else:
                            reference = item
            conn.execute("UPDATE drawing_attempts SET status='running',updated_at=? WHERE id=?", (stamp(), attempt_id))
            conn.execute("UPDATE drawing_runs SET status='running',revision=revision+1,updated_at=? WHERE id=?", (stamp(), run_id))
            event(conn, run, "running", {"attempt_id": attempt_id})
        # No database transaction is held across network or rendering calls.
        if snapshot["kind"] == "ai_image":
            from paperpilot.services.provider_config import resolve_credentials
            from paperpilot.services.credential_vault import scoped
            runtime=resolve_credentials(conn,gate['profile_id'],gate['profile_version'],temporary_key=temporary_key)
            with scoped(runtime):
                output = generate(gate, snapshot["spec"], snapshot["plan"], reference=reference, mask=mask)
            content, filename, usage = output["content"], output["filename"], output["usage"]
        else:
            content = generation.render(snapshot["kind"], snapshot["spec"], snapshot["plan"])
            filename, usage = "drawing.svg", {"external_calls": 0}
        assets.prepare(content, filename, None)  # Invalid output never becomes a successful candidate.
        with conn:
            begin(conn)
            run = row(conn, project_id, run_id)
            current_attempt = conn.execute("SELECT status FROM drawing_attempts WHERE id=?", (attempt_id,)).fetchone()
            if current_attempt["status"] not in {"running", "unknown"}:
                return
            cid = uuid.uuid4().hex
            cancelled = run["status"] == "cancelled"
            status = "cancelled" if cancelled else "stale" if stale_reasons(conn, run) else "ready"
            conn.execute("INSERT OR IGNORE INTO drawing_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?)", (cid, run_id, attempt_id, project_id, attempt["input_hash"], content, filename, digest(content), status, None, stamp()))
            conn.execute("UPDATE drawing_attempts SET status='completed',usage_json=?,updated_at=? WHERE id=?", (compact(usage), stamp(), attempt_id))
            if not cancelled:
                conn.execute("UPDATE drawing_runs SET status=?,revision=revision+1,updated_at=? WHERE id=?", ("preview_ready" if status == "ready" else "stale", stamp(), run_id))
            event(conn, run, "candidate_ready", {"attempt_id": attempt_id, "candidate_id": cid, "status": status})
    except Exception as exc:
        # The provider may have accepted a timed-out request. Never retry that attempt.
        conn.rollback()
        code = exc.code if isinstance(exc, ProjectServiceError) else "tool_failed"
        status = "unknown" if code in {"provider_unknown", "tool_failed"} else "failed"
        with conn:
            begin(conn)
            conn.execute("UPDATE drawing_attempts SET status=?,error_code=?,updated_at=? WHERE id=? AND status IN ('queued','running','unknown')", (status, code, stamp(), attempt_id))
            conn.execute("UPDATE drawing_runs SET status=?,revision=revision+1,updated_at=? WHERE id=? AND status!='cancelled'", (status, stamp(), run_id))
            event(conn, row(conn, project_id, run_id), status, {"attempt_id": attempt_id, "code": code})
    finally:
        from paperpilot.services import provider_runtime
        attempt=conn.execute('SELECT status,error_code,usage_json FROM drawing_attempts WHERE id=?',(attempt_id,)).fetchone()
        if attempt:
            with conn:provider_runtime.finish_audit(conn,attempt_id,attempt['status'],attempt['error_code'],json.loads(attempt['usage_json']),(time.perf_counter()-started)*1000)
        temporary_key=None
        conn.close()


def command(conn, project_id, run_id, payload, action):
    begin(conn)
    fp, prior = operation(conn, project_id, payload["request_id"], {"action": action, "run_id": run_id, **payload})
    if prior:
        return prior
    run = row(conn, project_id, run_id)
    check_revision(run, payload["expected_revision"])
    if action == "cancel":
        if run["status"] == "saved":
            raise ProjectServiceError(409, "already_saved", "候选已保存为资产；请在资产库中归档。")
        conn.execute("UPDATE drawing_runs SET status='cancelled',revision=revision+1,updated_at=? WHERE id=?", (stamp(), run_id))
        conn.execute("UPDATE drawing_candidates SET status='cancelled' WHERE run_id=? AND saved_json IS NULL", (run_id,))
        conn.execute("UPDATE drawing_attempts SET status='cancelled',updated_at=? WHERE run_id=? AND status='queued'", (stamp(), run_id))
        details = {"message": "任务已取消；已发送请求可能仍计费，迟到结果仅供历史查看。"}
    else:
        attempt = conn.execute("SELECT * FROM drawing_attempts WHERE run_id=?", (run_id,)).fetchone()
        details = {"message": "没有待对账的外部请求。"}
        if attempt and attempt["status"] in {"running", "queued", "unknown"}:
            # This adapter has no trustworthy provider query endpoint. Keep uncertainty explicit.
            details = {"message": "提供方没有可用查询接口，结果仍不明。请凭任务编号核对提供方账单；本系统不会自动重发。", "attempt_id": attempt["id"]}
            if run["status"] != "cancelled":
                conn.execute("UPDATE drawing_runs SET status='unknown',revision=revision+1,updated_at=? WHERE id=?", (stamp(), run_id))
            conn.execute("UPDATE drawing_attempts SET status='unknown',updated_at=? WHERE id=?", (stamp(), attempt["id"]))
    event(conn, run, action, details)
    return remember(conn, project_id, payload["request_id"], fp, read(conn, project_id, run_id) | details)


def candidate(conn, project_id, candidate_id):
    c = conn.execute("SELECT * FROM drawing_candidates WHERE id=? AND project_id=?", (candidate_id, project_id)).fetchone()
    if not c:
        raise ProjectServiceError(404, "resource_missing", "候选不存在于当前论文。")
    run = row(conn, project_id, c["run_id"])
    reasons = stale_reasons(conn, run)
    return {k: c[k] for k in ("id", "run_id", "attempt_id", "filename", "sha256", "created_at")} | {
        "status": "stale" if reasons or c["input_hash"] != input_hash(run) else c["status"],
        "stale_reasons": reasons, "saved": json.loads(c["saved_json"]) if c["saved_json"] else None,
        "preview_url": f"/api/projects/{project_id}/drawing/candidates/{candidate_id}/preview"}


def save_candidate(conn, storage_root, project_id, candidate_id, payload):
    # Conversion occurs in assets.save_asset before it starts its write transaction.
    fp, prior = operation(conn, project_id, payload["request_id"], {"action": "save", "candidate_id": candidate_id, **payload})
    if prior:
        return prior
    c = candidate(conn, project_id, candidate_id)
    run = row(conn, project_id, c["run_id"])
    check_revision(run, payload["expected_run_revision"])
    if c["saved"]:
        return c["saved"] | {"idempotent_replay": True}
    if c["status"] != "ready" or run["status"] != "preview_ready":
        raise ProjectServiceError(409, "stale_input", "候选已过期、取消或尚不可保存。请核对输入差异并创建新任务。")
    raw = conn.execute("SELECT content,filename FROM drawing_candidates WHERE id=?", (candidate_id,)).fetchone()
    # Recheck under an immediate transaction after deterministic file preparation.
    prepared = assets.prepare(raw["content"], raw["filename"], None)
    begin(conn)
    fp, prior = operation(conn, project_id, payload["request_id"], {"action": "save", "candidate_id": candidate_id, **payload})
    if prior:
        return prior
    run = row(conn, project_id, c["run_id"])
    check_revision(run, payload["expected_run_revision"])
    if candidate(conn, project_id, candidate_id)["status"] != "ready" or run["status"] != "preview_ready":
        raise ProjectServiceError(409, "stale_input", "候选状态已变化，请刷新后查看。")
    result = assets.save_asset(conn, storage_root, project_id, content=raw["content"], filename=raw["filename"], title=run["title"], request_id="drawing:" + candidate_id,
                               kind=run["selected_type"], run_id=run["id"], editable_source=run["spec_json"], source_asset_version=json.loads(run["spec_json"]).get("input_asset_version"), _prepared=prepared)
    conn.execute("UPDATE drawing_candidates SET status='saved',saved_json=? WHERE id=?", (compact(result), candidate_id))
    conn.execute("UPDATE drawing_runs SET status='saved',revision=revision+1,updated_at=? WHERE id=?", (stamp(), run["id"]))
    event(conn, run, "saved", {"candidate_id": candidate_id, "version_id": result["version"]["id"], "body_written": False})
    return remember(conn, project_id, payload["request_id"], fp, result | {"body_written": False})
