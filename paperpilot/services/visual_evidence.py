"""Version-bound, immutable visual evidence with separate append-only human reviews."""
from __future__ import annotations

import io
import json
import sqlite3
import uuid
import time

from PIL import Image

from paperpilot.services import visual_assets as assets, visual_generation as generation, visual_jobs as jobs, visual_provider
from paperpilot.services.project_errors import ProjectServiceError


def initialize_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS visual_check_runs (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id), request_id TEXT NOT NULL,
      fingerprint TEXT NOT NULL, snapshot_json TEXT NOT NULL, request_json TEXT NOT NULL, gate_json TEXT NOT NULL,
      status TEXT NOT NULL, error_code TEXT, usage_json TEXT NOT NULL DEFAULT '{}',
      created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(project_id,request_id)
    );
    CREATE TABLE IF NOT EXISTS visual_reports (
      id TEXT PRIMARY KEY, run_id TEXT NOT NULL UNIQUE REFERENCES visual_check_runs(id),
      project_id TEXT NOT NULL, version INTEGER NOT NULL, snapshot_json TEXT NOT NULL,
      summary TEXT NOT NULL, limitations_json TEXT NOT NULL, created_at TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS visual_findings (
      id TEXT PRIMARY KEY, report_id TEXT NOT NULL REFERENCES visual_reports(id),
      check_type TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL, location_json TEXT NOT NULL,
      rule_version TEXT NOT NULL, limitations TEXT NOT NULL, method TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS visual_review_events (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, report_id TEXT NOT NULL REFERENCES visual_reports(id),
      report_version INTEGER NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
      decision TEXT NOT NULL, reason TEXT NOT NULL, evidence_json TEXT NOT NULL, reviewer TEXT NOT NULL,
      created_at TEXT NOT NULL, UNIQUE(project_id,request_id)
    );
    CREATE TABLE IF NOT EXISTS visual_style_candidates (
      id TEXT PRIMARY KEY, project_id TEXT NOT NULL, report_id TEXT NOT NULL,
      source_version_id TEXT NOT NULL, location_json TEXT NOT NULL, values_json TEXT NOT NULL,
      rule_id TEXT NOT NULL, method TEXT NOT NULL, created_at TEXT NOT NULL,
      UNIQUE(project_id,report_id)
    );
    CREATE TRIGGER IF NOT EXISTS visual_reports_immutable BEFORE UPDATE ON visual_reports BEGIN SELECT RAISE(ABORT,'immutable report'); END;
    CREATE TRIGGER IF NOT EXISTS visual_findings_immutable BEFORE UPDATE ON visual_findings BEGIN SELECT RAISE(ABORT,'immutable finding'); END;
    CREATE TRIGGER IF NOT EXISTS visual_reviews_append_only BEFORE UPDATE ON visual_review_events BEGIN SELECT RAISE(ABORT,'append-only review'); END;
    """)


def source_snapshot(conn, project_id, ref):
    if not ref:
        return None
    source = conn.execute("SELECT s.* FROM corpus_source_versions s JOIN corpus c ON c.id=s.corpus_id WHERE c.project_id=? AND s.corpus_id=? AND s.version=?", (project_id, ref["corpus_id"], ref["version"])).fetchone()
    current = conn.execute("SELECT MAX(version) FROM corpus_source_versions WHERE corpus_id=?", (ref["corpus_id"],)).fetchone()[0]
    if not source or source["permission_state"] != "authorized" or source["status"] != "active" or current != ref["version"]:
        raise ProjectServiceError(409, "source_unavailable", "所选原文版本不可用、已更新或授权已撤回，请重新选择来源。")
    return {"corpus_id": ref["corpus_id"], "version": ref["version"], "sha256": assets.digest(source["content"].encode()), "permission_state": source["permission_state"], "status": source["status"]}


def asset_snapshot(conn, project_id, vid):
    v = assets.version(conn, project_id, vid)
    a = assets.asset(conn, project_id, v["asset_id"])
    if a["status"] != "active" or v["status"] != "ready" or a["current_version_id"] != vid:
        raise ProjectServiceError(409, "stale_input", "图像已更新、归档或不可用，请重新选择当前版本。")
    return {"asset_id": a["id"], "version_id": vid, "sha256": v["sha256"], "revision": a["revision"], "kind": a["kind"], "run_id": v["run_id"], "page_no": v["page_no"], "source_sha256": v["source_sha256"]}


def snapshot(conn, project_id, payload):
    target = asset_snapshot(conn, project_id, payload["asset_version_id"])
    kind = target["kind"] if target["kind"] in generation.TYPES else "structured_svg"
    return {"asset": target, "source": source_snapshot(conn, project_id, payload.get("source")),
            "source_asset": asset_snapshot(conn, project_id, payload["source_asset_version"]) if payload.get("source_asset_version") else None,
            "region": payload.get("region"), "rule_plan": generation.compile_style(conn, project_id, kind, {}), "checks": sorted(set(payload["check_types"])), "checker_version": "visual-evidence-v1"}


def stale_reasons(conn, project_id, frozen, admission=None):
    request = {"asset_version_id": frozen["asset"]["version_id"], "source": frozen["source"], "source_asset_version": frozen["source_asset"]["version_id"] if frozen["source_asset"] else None, "region": frozen["region"], "check_types": frozen["checks"]}
    try:
        if snapshot(conn, project_id, request) != frozen:
            return ["图像、原文或已批准规则版本发生变化"]
        if admission and admission.get("profile_id"):
            visual_provider.verify_gate(conn, project_id, admission)
    except ProjectServiceError as exc:
        return [exc.message]
    return []


def capacity_preview(conn, storage_root, project_id, payload):
    """Read local previews/configuration only; no credentials, job or HTTP call."""
    from paperpilot.services import provider_runtime, vision_capacity
    frozen = snapshot(conn, project_id, payload)
    binding = visual_provider.binding(conn, project_id, 'vision')
    repair = {'profile_id': binding['ai_profile_id'] if binding else None, 'purpose': 'vision'}
    try:
        if not binding or not binding['ai_profile_id']:
            raise ProjectServiceError(409, 'provider_binding_required', '请先登记、测试、启用并绑定视觉用途模型，再预览容量；未发送。')
        config = provider_runtime.require_enabled(conn, binding['ai_profile_id'], binding['ai_config_revision'], binding['ai_model_id'], 'vision_analysis', capacity=True)
        repair['model_id'] = binding['ai_model_id']
        images = [image_bytes(conn, storage_root, project_id, frozen['asset']['version_id'])]
        if frozen['source_asset'] and frozen['region']:
            images.append(image_bytes(conn, storage_root, project_id, frozen['source_asset']['version_id'], frozen['region']))
        checks = [c for c in frozen['checks'] if c in {'semantic', 'source_consistency', 'readability', 'style'}]
        capacity = vision_capacity.admit({**config, 'model': binding['ai_model_id']}, images, checks)
        result = {'generation_allowed': True, 'capacity': capacity, 'provider': {'profile_id': binding['ai_profile_id'],
            'config_revision': binding['ai_config_revision'], 'model': binding['ai_model_id'], 'name': config['name'], 'base_url': config['base_url']},
            'snapshot': frozen, 'repair': repair, 'external_calls': 0}
        result['preview_hash'] = assets.digest(assets.compact(result).encode())
        return result
    except ProjectServiceError as exc:
        return {'generation_allowed': False, 'error_code': exc.code, 'message': exc.message, 'repair': repair, 'external_calls': 0}


def create(conn, project_id, payload,temporary_key=None,*,storage_root):
    jobs.begin(conn)
    fp = assets.digest(assets.compact(payload).encode())
    existing = conn.execute("SELECT * FROM visual_check_runs WHERE project_id=? AND request_id=?", (project_id, payload["request_id"])).fetchone()
    if existing:
        if existing["fingerprint"] != fp:
            raise ProjectServiceError(409, "idempotency_key_reused", "请求编号已用于不同视觉核验输入。")
        return read_run(conn, project_id, existing["id"]) | {"idempotent_replay": True}
    frozen = snapshot(conn, project_id, payload)
    admission = {}
    if payload["cloud"]:
        preview = capacity_preview(conn, storage_root, project_id, payload)
        if not preview['generation_allowed']:
            raise ProjectServiceError(422, preview['error_code'], preview['message'])
        if payload.get('capacity_preview_hash') and payload['capacity_preview_hash'] != preview['preview_hash']:
            raise ProjectServiceError(409, 'vision_preview_stale', '容量预览的图像、接收方或配置已变化；请重新预览并明确授权，本次未发送。')
        admission = visual_provider.gate(conn, {"project_id": project_id, "selected_type": "vision_analysis", "spec_json": "{}"}, payload,temporary_key=temporary_key)
        admission['capacity'] = preview['capacity']
    run_id = uuid.uuid4().hex
    if admission:
        from paperpilot.services import provider_runtime
        provider_runtime.begin_audit(conn,run_id,project_id,payload['request_id'],admission,'vision_analysis',fp,{'asset_version_id':payload['asset_version_id'],'input_hash':fp})
    conn.execute("INSERT INTO visual_check_runs(id,project_id,request_id,fingerprint,snapshot_json,request_json,gate_json,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (run_id, project_id, payload["request_id"], fp, assets.compact(frozen), assets.compact(payload), assets.compact(admission), "queued", assets.stamp(), assets.stamp()))
    return read_run(conn, project_id, run_id)


def get_run(conn, project_id, run_id):
    row = conn.execute("SELECT * FROM visual_check_runs WHERE id=? AND project_id=?", (run_id, project_id)).fetchone()
    if not row:
        raise ProjectServiceError(404, "resource_missing", "核验任务不存在于当前论文。")
    return row


def read_run(conn, project_id, run_id):
    run = get_run(conn, project_id, run_id)
    report = conn.execute("SELECT id FROM visual_reports WHERE run_id=?", (run_id,)).fetchone()
    return {"id": run_id, "status": run["status"], "error_code": run["error_code"], "snapshot": json.loads(run["snapshot_json"]), "report_id": report["id"] if report else None,
        "usage": json.loads(run["usage_json"]), "capacity": json.loads(run['gate_json']).get('capacity'), "created_at": run["created_at"], "stale_reasons": stale_reasons(conn, project_id, json.loads(run["snapshot_json"]), json.loads(run["gate_json"]))}


def finding(check, status, message, frozen, limitation="", method="local_deterministic"):
    return {"check": check, "status": status, "message": message, "location": {"asset_version_id": frozen["asset"]["version_id"], "page_no": frozen["asset"]["page_no"], "region": frozen["region"]}, "limitations": limitation, "method": method}


def image_bytes(conn, storage_root, project_id, vid, region=None):
    # Validate immutable originals/builds before exposing the managed preview to a model.
    assets.checked_file(conn, storage_root, project_id, vid, "original")
    assets.checked_file(conn, storage_root, project_id, vid, "build")
    path = assets.checked_file(conn, storage_root, project_id, vid, "preview")
    with Image.open(path) as image:
        image.load()
        image = image.convert("RGB")
        if region:
            w, h = image.size
            box = (int(region["x"]*w), int(region["y"]*h), int((region["x"]+region["width"])*w), int((region["y"]+region["height"])*h))
            if box[2] <= box[0] or box[3] <= box[1]:
                raise ProjectServiceError(422, "invalid_region", "所选区域小于一个像素，请扩大范围。")
            image = image.crop(box)
        image.thumbnail((1600, 1600))
        out = io.BytesIO()
        image.save(out, "PNG")
        return out.getvalue()


def local_check(conn, storage_root, project_id, frozen):
    v = assets.version(conn, project_id, frozen["asset"]["version_id"])
    findings = []
    images = []
    integrity_error = None
    try:
        images.append(image_bytes(conn, storage_root, project_id, v["id"]))
    except (ProjectServiceError, OSError, ValueError) as exc:
        integrity_error = exc.code if isinstance(exc, ProjectServiceError) else "invalid_preview"
    for check in frozen["checks"]:
        if check == "file_integrity":
            findings.append(finding(check, "failed" if integrity_error else "pass", "受管文件缺失、损坏或哈希不符。" if integrity_error else "原件和构建表示哈希匹配，预览可解析。", frozen, "仅验证文件完整性，不证明学术内容真实。"))
        elif check == "readability":
            findings.append(finding(check, "failed" if integrity_error else "warning" if min(v["width"],v["height"]) < 300 else "needs_human_review", "无法解析图像。" if integrity_error else "图像像素较低，缩印时可能难以阅读。" if min(v["width"],v["height"]) < 300 else "图像可解析；文字、字号与缩印效果仍需人工检查。", frozen, "本地尺寸检查不包含 OCR 或科学语义判断。"))
        elif check == "provenance":
            findings.append(finding(check, "needs_human_review" if not v["origin_ref"] and not v["run_id"] else "pass", "生成运行或导入来源链已记录。" if v["origin_ref"] or v["run_id"] else "没有可定位的外部来源，请补充来源后核验。", frozen, "有来源记录不等于来源真实性或使用权已核实。"))
        elif check == "source_consistency":
            located = bool(frozen["source_asset"] and frozen["region"])
            findings.append(finding(check, "needs_human_review" if located else "unknown", "已有固定参考图区域，可进行人工或显式授权模型对照。" if located else "缺少可定位的参考图区域，无法核验图文或数据一致性。", frozen, "题录或文本原文不能替代视觉页证据。"))
        elif check == "semantic":
            findings.append(finding(check, "unknown", "科学含义、数据与图中关系尚未核验。", frozen, "本地检查不能证明科学真实性；未执行云模型核验。"))
        elif check == "style":
            findings.append(finding(check, "needs_human_review" if frozen["source_asset"] and frozen["region"] else "unknown", "有定位区域时可提取颜色候选，候选仍需批准。" if frozen["source_asset"] and frozen["region"] else "缺少图像页和区域，风格提取不可用。", frozen, "颜色提取不推断期刊风格或科学含义。"))
    if images and frozen["source_asset"] and frozen["region"]:
        try:
            images.append(image_bytes(conn, storage_root, project_id, frozen["source_asset"]["version_id"], frozen["region"]))
        except (ProjectServiceError, OSError, ValueError):
            findings.append(finding("source_consistency", "failed", "参考图区域无法读取或解析。", frozen, "没有发送不可解析的参考图。"))
            images = []
    return findings, images


def execute(database, storage_root, project_write, project_id, run_id,temporary_key=None):
    started=time.perf_counter()
    from paperpilot.adapters.visual_vision import analyze
    conn = database()
    findings, usage, error_code, result_state = [], {}, None, "completed"
    try:
        with conn:
            jobs.begin(conn)
            run = get_run(conn, project_id, run_id)
            if run["status"] != "queued":
                return
            project_write(project_id)
            frozen, admission = json.loads(run["snapshot_json"]), json.loads(run["gate_json"])
            if stale_reasons(conn, project_id, frozen, admission):
                raise ProjectServiceError(409, "stale_input", "输入已变化，请创建新核验任务。")
            conn.execute("UPDATE visual_check_runs SET status='running',updated_at=? WHERE id=?", (assets.stamp(), run_id))
        findings, images = local_check(conn, storage_root, project_id, frozen)
        cloud_checks = [c for c in frozen["checks"] if c in {"semantic", "source_consistency", "readability", "style"}]
        if admission and cloud_checks:
            # Recheck permissions after local rendering and immediately before egress.
            project_write(project_id)
            if stale_reasons(conn, project_id, frozen, admission):
                raise ProjectServiceError(409, "stale_input", "输入或授权发生变化，未发出云端请求。")
            if not images:
                findings.append(finding("semantic", "failed", "图像不可解析，未执行云端核验。", frozen))
            else:
                from paperpilot.services.provider_config import resolve_credentials
                from paperpilot.services.credential_vault import scoped
                runtime=resolve_credentials(conn,admission['profile_id'],admission['profile_version'],temporary_key=temporary_key)
                with scoped(runtime):
                    cloud = analyze(admission, images, cloud_checks)
                findings.extend(cloud["findings"])
                usage = cloud["usage"]
        elif not admission:
            findings.append(finding("cloud_review", "unknown", "未授权云端视觉核验；仅执行本地检查。", frozen, "外部调用次数为零。", "not_executed"))
    except Exception as exc:
        conn.rollback()
        error_code = exc.code if isinstance(exc, ProjectServiceError) else "check_failed"
        result_state = "unknown" if error_code == "provider_unknown" else "failed"
        run = get_run(conn, project_id, run_id)
        frozen = json.loads(run["snapshot_json"])
        failure_message = exc.message if isinstance(exc, ProjectServiceError) else '核验未完成；请检查文件、权限或模型配置后创建新任务。'
        findings.append(finding("execution", "unknown" if result_state == "unknown" else "failed", "核验结果不明，请先核对提供方记录，不会自动重发。" if result_state == "unknown" else failure_message, frozen, error_code))
    finally:
        if findings:
            with conn:
                jobs.begin(conn)
                if not conn.execute("SELECT 1 FROM visual_reports WHERE run_id=?", (run_id,)).fetchone():
                    report_id = uuid.uuid4().hex
                    statuses = {f["status"] for f in findings}
                    summary = next((s for s in ("failed", "unknown", "needs_human_review", "warning") if s in statuses), "pass")
                    conn.execute("INSERT INTO visual_reports VALUES(?,?,?,?,?,?,?,?)", (report_id, run_id, project_id, 1, assets.compact(frozen), summary, assets.compact(["自动检查不能替代人工核验或科学真实性验证。"]), assets.stamp()))
                    for f in findings:
                        conn.execute("INSERT INTO visual_findings VALUES(?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, report_id, f["check"], f["status"], f["message"], assets.compact(f["location"]), assets.digest(assets.compact(frozen["rule_plan"]).encode()), f["limitations"], f["method"]))
                    conn.execute("UPDATE visual_check_runs SET status=?,error_code=?,usage_json=?,updated_at=? WHERE id=?", (result_state, error_code, assets.compact(usage), assets.stamp(), run_id))
        from paperpilot.services import provider_runtime
        audit_run=conn.execute('SELECT status,error_code,usage_json FROM visual_check_runs WHERE id=?',(run_id,)).fetchone()
        if audit_run:
            with conn:provider_runtime.finish_audit(conn,run_id,audit_run['status'],audit_run['error_code'],json.loads(audit_run['usage_json']),(time.perf_counter()-started)*1000)
        temporary_key=None
        conn.close()


def report(conn, project_id, report_id):
    r = conn.execute("SELECT * FROM visual_reports WHERE id=? AND project_id=?", (report_id, project_id)).fetchone()
    if not r:
        raise ProjectServiceError(404, "resource_missing", "核验报告不存在于当前论文。")
    frozen = json.loads(r["snapshot_json"])
    run = get_run(conn, project_id, r["run_id"])
    reasons = stale_reasons(conn, project_id, frozen, json.loads(run["gate_json"]))
    reviews = [dict(e) | {"evidence_refs": json.loads(e["evidence_json"]), "stale": bool(reasons)} for e in conn.execute("SELECT * FROM visual_review_events WHERE report_id=? ORDER BY created_at,id", (report_id,))]
    return {"id": r["id"], "run_id": r["run_id"], "version": r["version"], "snapshot": frozen, "summary": r["summary"], "stale": bool(reasons), "stale_reasons": reasons,
        "limitations": json.loads(r["limitations_json"]), "created_at": r["created_at"],
        "findings": [dict(f) | {"location": json.loads(f["location_json"])} for f in conn.execute("SELECT * FROM visual_findings WHERE report_id=?", (report_id,))],
        "reviews": reviews, "read_only": True, "blocks_build": False, "body_written": False}


def review(conn, project_id, report_id, payload):
    jobs.begin(conn)
    fp = assets.digest(assets.compact({"report_id": report_id, **payload}).encode())
    existing = conn.execute("SELECT * FROM visual_review_events WHERE project_id=? AND request_id=?", (project_id, payload["request_id"])).fetchone()
    if existing:
        if existing["fingerprint"] != fp:
            raise ProjectServiceError(409, "idempotency_key_reused", "此请求编号已用于其他人工复核。")
        return report(conn, project_id, report_id) | {"idempotent_replay": True}
    r = report(conn, project_id, report_id)
    if r["version"] != payload["report_version"] or len(r["reviews"]) != payload["expected_review_count"]:
        raise ProjectServiceError(409, "review_conflict", "报告或复核记录已变化；请刷新后重试，复核草稿保留。")
    if r["stale"]:
        raise ProjectServiceError(409, "report_stale", "报告输入已经变化；历史报告仍可查看，请基于当前版本重新核验。")
    if not payload["reason"].strip() or not payload["reviewer"].strip():
        raise ProjectServiceError(422, "review_reason_required", "请填写复核者和具体理由。")
    conn.execute("INSERT INTO visual_review_events VALUES(?,?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, project_id, report_id, r["version"], payload["request_id"], fp, payload["decision"], payload["reason"], assets.compact(payload["evidence_refs"]), payload["reviewer"], assets.stamp()))
    return report(conn, project_id, report_id)


def current(conn, project_id, asset_version_id, purpose="review"):
    if purpose not in {"review", "build", "citation"}:
        raise ProjectServiceError(422, "invalid_purpose", "不支持此报告用途。")
    assets.version(conn, project_id, asset_version_id)
    for r in conn.execute("SELECT id FROM visual_reports WHERE project_id=? ORDER BY created_at DESC,id", (project_id,)):
        view = report(conn, project_id, r["id"])
        if view["snapshot"]["asset"]["version_id"] == asset_version_id and not view["stale"]:
            return {"status": "current", "report_id": view["id"], "report_version": view["version"], "summary": view["summary"], "reviews": view["reviews"], "blocks_build": False, "read_only": True}
    return {"status": "unavailable", "report_id": None, "reason": "没有匹配当前版本、有效来源与规则的报告。", "blocks_build": False, "read_only": True}


def extract_style(conn, storage_root, project_id, report_id):
    r = report(conn, project_id, report_id)
    frozen = r["snapshot"]
    if r["stale"]:
        raise ProjectServiceError(409, "report_stale", "报告已过期，请基于当前图像重新核验。")
    if not frozen["source_asset"] or not frozen["region"]:
        return {"status": "unavailable", "reason": "需要可定位的参考图像页和区域；题录或原文文本不足以提取视觉风格。", "published": False}
    content = image_bytes(conn, storage_root, project_id, frozen["source_asset"]["version_id"], frozen["region"])
    with Image.open(io.BytesIO(content)) as im:
        quantized = im.convert("RGB").quantize(colors=5)
        palette = quantized.getpalette()
        counts = sorted(quantized.getcolors(), reverse=True)
        color = palette[counts[0][1]*3:counts[0][1]*3+3]
        values = {"fill": "#" + "".join(f"{c:02x}" for c in color)}
    jobs.begin(conn)
    existing = conn.execute("SELECT * FROM visual_style_candidates WHERE project_id=? AND report_id=?", (project_id, report_id)).fetchone()
    if existing:
        return {"id": existing["id"], "rule_id": existing["rule_id"], "values": json.loads(existing["values_json"]), "status": "candidate", "published": False, "idempotent_replay": True}
    # Recheck permissions and versions after reading the fixed reference region.
    if report(conn, project_id, report_id)["stale"]:
        raise ProjectServiceError(409, "report_stale", "参考来源已变化，未保存风格候选。")
    rid, cid = uuid.uuid4().hex, uuid.uuid4().hex
    kind = "structured_svg"
    version = conn.execute("SELECT COALESCE(MAX(version),0)+1 FROM visual_style_rules WHERE scope='project' AND scope_id=? AND kind=?", (project_id, kind)).fetchone()[0]
    conn.execute("INSERT INTO visual_style_rules VALUES(?,?,?,?,?,?,?,?)", (rid, "project", project_id, kind, version, "candidate", assets.compact(values), assets.stamp()))
    conn.execute("INSERT INTO visual_style_candidates VALUES(?,?,?,?,?,?,?,?,?)", (cid, project_id, report_id, frozen["source_asset"]["version_id"], assets.compact(frozen["region"]), assets.compact(values), rid, "local_dominant_color_v1", assets.stamp()))
    return {"id": cid, "rule_id": rid, "status": "candidate", "values": values, "source_version_id": frozen["source_asset"]["version_id"], "region": frozen["region"], "limitations": "仅统计所选区域主色，不推断期刊规范。", "published": False}


def projections_for_files(conn, project_id, files):
    """Advisory consumption must not add an asset or report gate to citation review."""
    names = {name for f in files for name in assets.GRAPHICS.findall(str(f.get("content", "")))}
    projections = []
    for name in sorted(names):
        v = conn.execute("SELECT id FROM figure_asset_versions WHERE project_id=? AND build_name=?", (project_id, name)).fetchone()
        if v:
            projections.append(current(conn, project_id, v["id"], "citation"))
    return projections
