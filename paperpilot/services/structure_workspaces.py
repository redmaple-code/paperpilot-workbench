"""Project-scoped Traceify sources, drafts, semantic history and layout CAS."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from datetime import datetime, timezone

from paperpilot.services import outline, workflow, logic_graph, structure_graph as graph
from paperpilot.services.agent_actions import require_writable
from paperpilot.services.plan_state import plan_from_snapshot
from paperpilot.services.project_errors import ProjectServiceError


def now():
    return datetime.now(timezone.utc).isoformat()


def initialize_schema(conn):
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS structure_workspaces(
      id TEXT PRIMARY KEY,project_id TEXT NOT NULL,entry_mode TEXT NOT NULL,revision INTEGER NOT NULL,
      workspace_json TEXT NOT NULL,manifest_json TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS structure_project ON structure_workspaces(project_id,updated_at);
    CREATE TABLE IF NOT EXISTS structure_workspace_versions(
      project_id TEXT NOT NULL,workspace_id TEXT NOT NULL,revision INTEGER NOT NULL,workspace_json TEXT NOT NULL,
      operation TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(project_id,workspace_id,revision));
    CREATE TABLE IF NOT EXISTS structure_drafts(
      id TEXT PRIMARY KEY,project_id TEXT NOT NULL,workspace_id TEXT NOT NULL,run_id TEXT NOT NULL,
      revision INTEGER NOT NULL,base_revision INTEGER NOT NULL,workspace_json TEXT NOT NULL,
      errors_json TEXT NOT NULL,state TEXT NOT NULL,updated_at TEXT NOT NULL,UNIQUE(workspace_id,run_id));
    CREATE TABLE IF NOT EXISTS canvas_layouts(
      project_id TEXT NOT NULL,workspace_id TEXT NOT NULL,graph_kind TEXT NOT NULL,graph_branch TEXT NOT NULL,
      revision INTEGER NOT NULL,positions_json TEXT NOT NULL,PRIMARY KEY(project_id,workspace_id,graph_kind,graph_branch));
    CREATE TABLE IF NOT EXISTS structure_request_receipts(
      project_id TEXT NOT NULL,workspace_id TEXT NOT NULL,operation TEXT NOT NULL,request_id TEXT NOT NULL,
      input_hash TEXT NOT NULL,result_json TEXT NOT NULL,PRIMARY KEY(project_id,workspace_id,operation,request_id));
    """)


def replay(conn, pid, wid, operation, data):
    request_id = data.get("request_id")
    if not isinstance(request_id, str) or not 1 <= len(request_id) <= 160:
        graph.fail("需要有效请求编号。")
    digest = graph.fingerprint(data)
    row = conn.execute("SELECT * FROM structure_request_receipts WHERE project_id=? AND workspace_id=? AND operation=? AND request_id=?",
                       (pid, wid, operation, request_id)).fetchone()
    if row:
        if row["input_hash"] != digest:
            graph.fail("请求编号已经用于其他内容。", "idempotency_key_reused", 409)
        return digest, json.loads(row["result_json"])
    return digest, None


def receipt(conn, pid, wid, operation, data, digest, result):
    conn.execute("INSERT INTO structure_request_receipts VALUES(?,?,?,?,?,?)",
                 (pid, wid, operation, data["request_id"], digest, graph.compact(result)))
    return result


def idea_context(conn, pid, *, include_innovation_candidates=False):
    snapshot = workflow.current_snapshot(conn, pid)
    plan = plan_from_snapshot(snapshot)
    stage = plan["stages"][0]
    snapshots = conn.execute("SELECT version,created_at,stage_outputs_json FROM workflow_snapshots WHERE project_id=? ORDER BY version", (pid,)).fetchall()
    branches = {s["version"]: json.loads(s["stage_outputs_json"]).get("startup_plan", {}).get("branch_id") for s in snapshots}

    def message_in_branch(message):
        context = json.loads(message["context_json"])
        if context.get("action_id") or context.get("stage_id", "STAGE-01") != "STAGE-01":
            return False
        branch = context.get("branch_id")
        if branch is None:
            created = message["run_created_at"] or message["created_at"]
            prior = [s for s in snapshots if s["created_at"] <= created]
            branch = branches[prior[-1]["version"]] if prior else None
        return branch == plan.get("branch_id")
    pieces = []
    for field, value in stage["fields"].items():
        if value and (field != "contribution" or include_innovation_candidates):
            pieces.append({"text": field + "\n" + value, "origin_locator": {"kind": "stage_field", "stage_id": "STAGE-01", "field": field,
                          "status": stage["status"], "workflow_version": snapshot["version"]}})
    messages = conn.execute("""SELECT m.*,r.created_at AS run_created_at FROM agent_messages m LEFT JOIN agent_runs r ON r.id=m.run_id
        WHERE m.project_id=? AND m.role IN ('user','assistant') AND (r.stage_id='STAGE-01' OR m.run_id='')
        ORDER BY m.created_at,m.conversation_id,m.seq""", (pid,)).fetchall()
    messages = [m for m in messages if message_in_branch(m)]
    for message in messages:
        if message["text"]:
            pieces.append({"text": message["role"] + "\n" + message["text"], "origin_locator": {"kind": "message", "id": message["id"],
                          "conversation_id": message["conversation_id"], "seq": message["seq"]}})
    candidates = conn.execute("SELECT * FROM research_design_candidates WHERE project_id=? AND stage_id='STAGE-01' ORDER BY created_at", (pid,)).fetchall()
    candidates = [c for c in candidates if branches.get(c["base_workflow_version"]) == plan.get("branch_id")]
    if include_innovation_candidates:
        for candidate in candidates:
            pieces.append({"text": "创意候选（" + candidate["status"] + "）\n" + candidate["cards_json"] + "\n采用记录\n" + candidate["applied_card_json"], "origin_locator": {"kind": "candidate", "id": candidate["id"],
                          "status": candidate["status"]}})
    # Content identity excludes unrelated workflow position changes.
    identity = [{"text": p["text"], "origin_locator": {k: v for k, v in p["origin_locator"].items() if k != "workflow_version"}} for p in pieces]
    manifest = {"branch_id": plan.get("branch_id"), "workflow_version": snapshot["version"], "stage_status": stage["status"],
                "message_ids": [m["id"] for m in messages], "candidate_ids": [c["id"] for c in candidates],
                "context_hash": graph.fingerprint([plan.get("branch_id"), identity]), "omitted": []}
    return pieces, manifest


def make_source(pieces, title, kind="markdown", locator="构思交接"):
    segments = []
    for piece_index, piece in enumerate(pieces):
        for index, text in enumerate(re.split(r"\n\s*\n", piece["text"])):
            if text.strip():
                origin = piece.get("origin_locator", {"kind": "paragraph", "index": index})
                stable = graph.fingerprint([origin.get("id", origin.get("field", piece_index)), index, text])[:24]
                segments.append({"id": "seg-" + stable, "text": text, "origin_locator": origin})
    content = "\n\n".join(s["text"] for s in segments)
    if len(content) > 1000000 or len(segments) > 5000:
        graph.fail("输入超过100万字或5000段，请选择较小范围。")
    return {"id": graph.identifier("s"), "kind": kind, "title": title, "locator": locator,
            "content_hash": hashlib.sha256(content.encode()).hexdigest(), "status": "complete", "segments": segments}


def row(conn, pid, wid):
    result = conn.execute("SELECT * FROM structure_workspaces WHERE id=? AND project_id=?", (wid, pid)).fetchone()
    if result is None:
        graph.fail("工作区不属于当前论文。", "resource_not_found", 404)
    return result


def read(conn, pid, wid):
    saved = row(conn, pid, wid)
    workspace = json.loads(saved["workspace_json"])
    layout = read_layout(conn, pid, wid, {})
    ids = {n["id"] for n in workspace["graph"]["nodes"]}
    workspace["layout"] = {**workspace.get("layout", {}), "positions": {key: point for key, point in layout["positions"].items() if key in ids}}
    manifest = json.loads(saved["manifest_json"])
    changed = saved["entry_mode"] == "idea_from_stage01" and idea_context(
        conn, pid, include_innovation_candidates=not manifest.get("separate_innovation_context"))[1]["context_hash"] != manifest["context_hash"]
    return {"version": saved["revision"], "workspace": workspace, "host": {"project_id": pid, "workspace_id": wid,
            "entry_mode": saved["entry_mode"], "context_manifest": manifest, "context_changed": changed,
            "layout_revision": layout["revision"], "outline_version": outline.current_version(conn, pid),
            "workflow_version": workflow.current_snapshot(conn, pid)["version"]}}


def create(conn, pid, data, manuscript=None):
    require_writable(conn, pid)
    digest, prior = replay(conn, pid, "", "create", data)
    if prior is not None:
        return prior
    mode = data.get("entry_mode", "idea_from_stage01")
    if mode == "idea_from_stage01":
        pieces, manifest = idea_context(conn, pid)
        manifest["separate_innovation_context"] = True
        title, locator = "论文创意与构思", "STAGE-01"
    elif mode == "paper_import":
        content = data.get("content", "")
        if not isinstance(content, str) or (not content.strip() and not (data.get("source_status") == "failed" and data.get("locator"))):
            graph.fail("请导入论文文本。")
        title, locator = str(data.get("title") or "导入论文")[:180], str(data.get("locator") or "手动导入")[:500]
        pieces = data.get("pages") or [{"text": content, "origin_locator": {"kind": "paper", "locator": locator}}]
        if any(not isinstance(piece, dict) or not isinstance(piece.get("text"), str) for piece in pieces) or "\n\n".join(p["text"] for p in pieces) != content:
            graph.fail("论文页与原文内容不一致。")
        manifest = {"context_hash": graph.fingerprint(content), "workflow_version": workflow.current_snapshot(conn, pid)["version"], "omitted": []}
    else:
        graph.fail("入口类型无效。")
    source = make_source(pieces, title, kind="webpage" if locator.startswith(("https://", "http://")) else "markdown", locator=locator)
    source["status"] = data.get("source_status", "complete")
    source["warnings"] = data.get("source_warnings", [])
    if manuscript:
        manifest["manuscript_import"] = manuscript["identity"]
    wid = graph.identifier("ws")
    current = outline.read_outline(conn, pid)
    nodes = [{"id": "n-" + n["id"], "label": n["title"], "kind": "section", "summary": n.get("purpose", ""),
              "content": n.get("scope", ""), "domain_node_id": n["id"], "provenance": "user_edited", "confidence": None,
              "source_refs": [], "status": "confirmed", "locked_fields": [], "versions": {"ai": None, "user": None}} for n in current["nodes"]]
    edges = [{"id": "e-" + n["id"], "from": "n-" + n["parent_id"], "to": "n-" + n["id"], "relation": "章节归属",
              "relation_basis": "已有章节树", "provenance": "user_edited", "confidence": None, "source_refs": [], "status": "confirmed"}
             for n in current["nodes"] if n.get("parent_id")]
    sections = [{**n, "section_id": n["id"], "node_ids": ["n-" + n["id"]]} for n in current["nodes"]]
    logic = logic_graph.read_graph(conn, pid)
    logic_kinds = {"claim": "claim", "evidence": "evidence", "reasoning": "claim", "conclusion": "claim", "gap": "other"}
    for node in logic["nodes"]:
        nodes.append({"id": "logic-" + node["id"], "label": node["label"], "kind": logic_kinds.get(node["kind"], "other"),
                      "summary": node.get("details", ""), "content": "", "domain_node_id": node["id"], "domain_kind": "logic",
                      "domain_metadata": copy.deepcopy(node), "source_refs": [], "confidence": None, "status": "confirmed",
                      "provenance": "user_edited", "locked_fields": [], "versions": {"ai": None, "user": None}})
    relations = {"supports": "支持", "contradicts": "矛盾", "provides_evidence": "提供证据", "evidence_for": "作为依据"}
    for edge in logic["edges"]:
        edges.append({"id": "logic-edge-" + edge["id"], "from": "logic-" + edge["from_node_id"], "to": "logic-" + edge["to_node_id"],
                      "relation": relations.get(edge["edge_type"], edge["edge_type"]), "relation_basis": "既有逻辑图关系，来源状态见原图记录",
                      "domain_metadata": copy.deepcopy(edge), "source_refs": [], "confidence": None,
                      "status": "confirmed", "provenance": "user_edited"})
    workspace = {"schema_version": 2, "id": wid, "source": source, "graph": {"nodes": nodes, "edges": edges, "groups": []},
                 "layout": {"positions": {}, "grouping_mode": "auto", "module_layout_version": 0}, "paper_outline": sections,
                 "outline": [], "mappings": [], "evidence_chains": [], "pending_suggestions": [],
                 "sync": {"state": "synced"}, "outline_bindings": {}, "domain_versions": {"outline": current["version"], "logic": logic["version"]},
                 "domain_snapshots": {"outline": current, "logic": logic}}
    if manuscript:
        attach_import_evidence(workspace, manuscript["links"])
    graph.validate(workspace)
    stamp = now()
    conn.execute("INSERT INTO structure_workspaces VALUES(?,?,?,?,?,?,?,?)", (wid, pid, mode, 1, graph.compact(workspace), graph.compact(manifest), stamp, stamp))
    conn.execute("INSERT INTO structure_workspace_versions VALUES(?,?,?,?,?,?)", (pid, wid, 1, graph.compact(workspace), "create", stamp))
    positions = {"n-" + n["id"]: {"x": max(0, min(100000, n["canvas_x"])), "y": max(0, min(100000, n["canvas_y"]))}
                 for n in current["nodes"] if all(isinstance(n.get(k), (int, float)) for k in ("canvas_x", "canvas_y"))}
    if positions:
        save_layout(conn, pid, wid, {"request_id": "migrate:" + wid, "expected_layout_revision": 0, "positions": positions})
    result = read(conn, pid, wid)
    return receipt(conn, pid, "", "create", data, digest, result)


def imported_manuscripts(conn, pid):
    return [dict(row) for row in conn.execute(
        """SELECT id, name FROM manuscript_batches AS batch
           WHERE project_id=? AND EXISTS (
               SELECT 1 FROM manuscript_applied WHERE batch_id=batch.id AND module='manuscript')
           ORDER BY updated_at DESC""", (pid,))]


def create_from_manuscript(conn, pid, batch_id):
    from paperpilot.services import manuscript_import

    if not any(batch["id"] == batch_id for batch in imported_manuscripts(conn, pid)):
        graph.fail("当前论文没有这份已采用的导入稿。", "resource_not_found", 404)
    batch = manuscript_import.batch(conn, batch_id)
    materials = manuscript_import.sources(conn, batch_id)
    source_id = manuscript_import.primary(batch, materials)
    if not source_id:
        graph.fail("导入稿没有可读取的正文主源。", "manuscript_primary_required", 422)
    source = next(row for row in materials if row["id"] == source_id)
    pieces = []
    for fragment in source["fragments"]:
        text = fragment["text"]
        if not text.strip():
            continue
        locator = {"id": fragment["id"], "location": fragment["location"], "kind": "paper"}
        if fragment.get("page"):
            locator.update(kind="paper_page", page=fragment["page"])
        pieces.append({"text": text, "origin_locator": locator})
    links = {}
    for item in manuscript_import.applied(conn, batch_id):
        target = item["result"].get("id")
        if target and item["module"] in {"outline", "logic"}:
            links[item["module"], target] = [ref["fragment_id"] for ref in item["evidence"]
                                               if ref.get("source_id") == source_id and ref.get("fragment_id")]
    identity = {"batch_id": batch_id, "source_id": source_id, "sha256": source["sha256"]}
    data = {"request_id": f"manuscript:{source_id}:{graph.fingerprint(pieces)[:24]}",
            "entry_mode": "paper_import", "title": source["name"], "locator": source["name"], "pages": pieces,
            "content": "\n\n".join(piece["text"] for piece in pieces), "source_warnings": source["warnings"],
            "source_status": "partial" if source["warnings"] else "complete"}
    return create(conn, pid, data, {"identity": identity, "links": links})


def attach_import_evidence(workspace, links):
    by_fragment = {}
    for segment in workspace["source"]["segments"]:
        by_fragment.setdefault(segment["origin_locator"]["id"], []).append(graph.reference(workspace, segment["id"]))
    sections = {section["section_id"]: section for section in workspace["paper_outline"]}
    for node in workspace["graph"]["nodes"]:
        module = "logic" if node.get("domain_kind") == "logic" else "outline"
        fragment_ids = links.get((module, node["domain_node_id"]), [])
        node["source_refs"] = list({ref["locator"]: ref for fragment in fragment_ids
                                    for ref in by_fragment.get(fragment, [])}.values())
        if module == "outline":
            sections[node["domain_node_id"]]["source_refs"] = copy.deepcopy(node["source_refs"])
    nodes = {node["id"]: node for node in workspace["graph"]["nodes"]}
    for edge in workspace["graph"]["edges"]:
        if edge["relation"] == "章节归属":
            edge["source_refs"] = copy.deepcopy(nodes[edge["to"]]["source_refs"])


def write(conn, pid, wid, data, operation="commands", domain_updates=None):
    from paperpilot.services import innovation_points
    require_writable(conn, pid)
    digest, prior = replay(conn, pid, wid, operation, data)
    if prior is not None:
        return prior
    saved = row(conn, pid, wid)
    if saved["revision"] != data.get("expected_workspace_revision"):
        graph.fail("工作区已变化，本地修改保留，请比较版本。", "workspace_stale", 409)
    current = json.loads(saved["workspace_json"])
    if data.get("source_hash") != current["source"]["content_hash"]:
        graph.fail("来源已变化。", "source_context_stale", 409)
    refs = innovation_points.usage_references(conn, pid, "structure_workspace", wid,
                                               data.get("revision") if operation == "restore" else saved["revision"])
    if operation == "restore":
        old = conn.execute("SELECT workspace_json FROM structure_workspace_versions WHERE project_id=? AND workspace_id=? AND revision=?",
                           (pid, wid, data.get("revision"))).fetchone()
        if not old:
            graph.fail("历史版本不存在。", "resource_not_found", 404)
        value = graph.validate(json.loads(old[0]))
    elif operation == "accept_draft":
        draft = read_draft(conn, pid, wid, data["draft_id"])
        if draft["state"] not in {"partial", "ready"} or draft["base_revision"] != saved["revision"]:
            graph.fail("草稿基线已过期。", "workspace_stale", 409)
        refs = draft.get("innovation_refs", [])
        if not innovation_points.references_current(conn, pid, refs):
            graph.fail("论文创新点已变化，请重新生成结构草稿。", "innovation_context_stale", 409)
        value = draft["workspace"]
        value.pop("preview_mode", None)
        for items in (value["graph"]["nodes"], value["graph"]["edges"]):
            for item in items:
                if item.get("status") == "pending":
                    item["status"] = "confirmed"
        value = graph.validate(value)
        conn.execute("UPDATE structure_drafts SET state='accepted' WHERE id=?", (data["draft_id"],))
    else:
        value = graph.apply_commands(current, data.get("commands", []))
    if domain_updates:
        value.update(copy.deepcopy(domain_updates))
        graph.validate(value)
    revision = saved["revision"] + 1
    stamp = now()
    conn.execute("UPDATE structure_workspaces SET revision=?,workspace_json=?,updated_at=? WHERE id=?", (revision, graph.compact(value), stamp, wid))
    conn.execute("INSERT INTO structure_workspace_versions VALUES(?,?,?,?,?,?)", (pid, wid, revision, graph.compact(value), operation, stamp))
    innovation_points.record_usage(conn, pid, refs, "structure_workspace", wid, revision, "已采纳的 Traceify 结构")
    return receipt(conn, pid, wid, operation, data, digest, read(conn, pid, wid))


def read_layout(conn, pid, wid, data):
    kind, branch = data.get("graph_kind", "structure"), data.get("graph_branch", "main")
    saved = conn.execute("SELECT * FROM canvas_layouts WHERE project_id=? AND workspace_id=? AND graph_kind=? AND graph_branch=?", (pid, wid, kind, branch)).fetchone()
    return {"revision": saved["revision"] if saved else 0, "positions": json.loads(saved["positions_json"]) if saved else {}}


def save_layout(conn, pid, wid, data):
    require_writable(conn, pid)
    digest, prior = replay(conn, pid, wid, "layout", data)
    if prior is not None:
        return prior
    value = row(conn, pid, wid)
    current = read_layout(conn, pid, wid, data)
    if data.get("expected_layout_revision") != current["revision"]:
        graph.fail("布局已被另一页面修改。", "layout_stale", 409)
    positions = data.get("positions", {})
    ids = {n["id"] for n in json.loads(value["workspace_json"])["graph"]["nodes"]}
    if data.get("draft_id"):
        ids |= {n["id"] for n in read_draft(conn, pid, wid, data["draft_id"])["workspace"]["graph"]["nodes"]}
    if not isinstance(positions, dict) or set(positions) - ids:
        graph.fail("布局引用了不存在的节点。")
    import math
    for position in positions.values():
        if not isinstance(position, dict) or any(isinstance(position.get(k), bool) or not isinstance(position.get(k), (int, float)) or not math.isfinite(position[k]) or not 0 <= position[k] <= 100000 for k in ("x", "y")):
            graph.fail("节点坐标无效。")
    if data.get("graph_kind", "structure") not in {"structure", "outline", "logic"}:
        graph.fail("图类型无效。")
    result = {"revision": current["revision"] + 1, "positions": {key: point for key, point in {**current["positions"], **positions}.items() if key in ids}}
    conn.execute("INSERT INTO canvas_layouts VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,workspace_id,graph_kind,graph_branch) DO UPDATE SET revision=excluded.revision,positions_json=excluded.positions_json",
                 (pid, wid, data.get("graph_kind", "structure"), data.get("graph_branch", "main"), result["revision"], graph.compact(result["positions"])))
    return receipt(conn, pid, wid, "layout", data, digest, result)


def read_draft(conn, pid, wid, did):
    from paperpilot.services import innovation_points
    row(conn, pid, wid)
    saved = conn.execute("SELECT * FROM structure_drafts WHERE id=? AND project_id=? AND workspace_id=?", (did, pid, wid)).fetchone()
    if saved is None:
        graph.fail("草稿不存在。", "resource_not_found", 404)
    workspace = json.loads(saved["workspace_json"])
    ids = {n["id"] for n in workspace["graph"]["nodes"]}
    workspace["layout"]["positions"].update({key: point for key, point in read_layout(conn, pid, wid, {})["positions"].items() if key in ids})
    value = {"id": did, "version": saved["revision"], "base_revision": saved["base_revision"], "run_id": saved["run_id"],
             "workspace": workspace, "errors": json.loads(saved["errors_json"]), "state": saved["state"]}
    refs = innovation_points.usage_references(conn, pid, "structure_draft", did, saved["revision"])
    if refs:
        value["innovation_refs"] = refs
        value["innovation_review_required"] = not innovation_points.references_current(conn, pid, refs)
    return value


def record_result(conn, pid, wid, run_id, data, seed_draft_id=None):
    """Persist model output before returning a small tool result pointer."""
    require_writable(conn, pid)
    digest, prior = replay(conn, pid, wid, "batch", data)
    if prior is not None:
        return prior
    current = read(conn, pid, wid)
    if current["host"]["context_changed"]:
        graph.fail("构思已更新，请保留本次结果并基于新构思生成。", "source_context_stale", 409)
    saved = conn.execute("SELECT * FROM structure_drafts WHERE workspace_id=? AND run_id=?", (wid, run_id)).fetchone()
    if saved and saved["state"] not in {"partial", "ready"}:
        graph.fail("此草稿已处理，请创建新运行。", "workspace_stale", 409)
    did = saved["id"] if saved else graph.identifier("draft")
    expected = saved["revision"] if saved else 0
    if data.get("expected_draft_revision", 0) != expected:
        graph.fail("草稿版本已更新，请先读取。", "workspace_stale", 409)
    seed = read_draft(conn, pid, wid, seed_draft_id) if seed_draft_id and not saved else None
    if seed and (seed["base_revision"] != current["version"] or seed["state"] not in {"partial", "ready"}):
        graph.fail("续生成草稿基线已过期。", "workspace_stale", 409)
    value = json.loads(saved["workspace_json"]) if saved else copy.deepcopy(seed["workspace"] if seed else current["workspace"])
    value["preview_mode"] = True
    result = data.get("result", {})
    if not isinstance(result, dict):
        graph.fail("结构结果须为对象。")
    for key in ("nodes", "edges", "groups", "paper_outline", "pending_suggestions", "covered_segment_ids"):
        if key in result and not isinstance(result[key], list):
            graph.fail("结构结果中的集合须为数组。")
    source_ids = {s["id"] for s in value["source"]["segments"]}
    errors = json.loads(saved["errors_json"]) if saved else copy.deepcopy(seed["errors"] if seed else [])
    batch_errors = []
    def invalid(raw, exc):
        batch_errors.append({"object_id": raw.get("id", raw.get("section_id")) if isinstance(raw, dict) else None,
                             "message": exc.message if isinstance(exc, ProjectServiceError) else "对象格式无效。"})

    def keep(collection, item):
        """Validate before replacing any prior object, retaining valid batch siblings."""
        previous = value["graph"][collection]
        value["graph"][collection] = [old for old in previous if old["id"] != item["id"]] + [item]
        try:
            graph.validate(value)
        except (KeyError, TypeError, ValueError, ProjectServiceError):
            value["graph"][collection] = previous
            raise
    def stable(raw):
        if not isinstance(raw, str):
            graph.fail("引用 ID 须为字符串。")
        known = {x["id"] for x in value["graph"]["nodes"]}
        mapping = value.setdefault("model_ids", {})
        if raw in known:
            return raw
        return mapping.setdefault(raw, "n-" + uuid.uuid5(uuid.NAMESPACE_URL, did + ":" + raw).hex)
    for kind in ("nodes", "edges"):
        for raw in result.get(kind, []):
            try:
                item = copy.deepcopy(raw)
                if not isinstance(item, dict):
                    graph.fail("模型对象格式无效。")
                raw_id = item.get("id")
                if not isinstance(raw_id, str) or not raw_id:
                    graph.fail("模型对象缺少ID。")
                items = {n["id"]: n for n in value["graph"][kind]}
                item["id"] = stable(raw_id) if kind == "nodes" or raw_id in items else "e-" + uuid.uuid5(uuid.NAMESPACE_URL, did + ":" + raw_id).hex
                refs = item.pop("segment_ids", [])
                if not isinstance(refs, list) or any(not isinstance(s, str) or s not in source_ids for s in refs):
                    graph.fail("模型引用了未知段落。", "invalid_reference")
                item["source_refs"] = [graph.reference(value, sid) for sid in refs]
                item.update(status="pending", provenance=item.get("provenance", "ai_suggestion"), review_state="pending")
                item.setdefault("versions", {})["ai"] = graph.compact(raw)
                item.setdefault("locked_fields", [])
                if kind == "edges":
                    item["from"], item["to"] = stable(item["from"]), stable(item["to"])
                old = items.get(item["id"])
                if old:
                    proposed = {}
                    for field in set(("label", "relation", "summary", "content", "relation_basis", "source_refs", "from", "to")) | set(old.get("locked_fields", [])):
                        if field in item and item[field] != old.get(field) and (old.get(field) or field in old.get("locked_fields", [])):
                            proposed[field] = item[field]
                            item[field] = old.get(field)
                    item["locked_fields"] = old.get("locked_fields", [])
                    item["versions"]["user"] = old.get("versions", {}).get("user")
                    item = {**old, **item, "status": old.get("status", "confirmed")}
                else:
                    proposed = {}
                keep(kind, item)
                if proposed:
                    sid = "suggestion-" + uuid.uuid5(uuid.NAMESPACE_URL, did + ":update:" + item["id"]).hex
                    value["pending_suggestions"] = [s for s in value["pending_suggestions"] if s["id"] != sid] + [{
                        "id": sid, "status": "pending", "suggested_action": "update", "target_kind": "node" if kind == "nodes" else "edge",
                        "target_id": item["id"], "proposed_fields": proposed, "source_refs": item["source_refs"]}]
            except (KeyError, TypeError, ValueError, ProjectServiceError) as exc:
                invalid(raw, exc)
    # Parents are inserted before children even if the model emits reverse order.
    pending = list(result.get("groups", []))
    while pending:
        parents = {g["id"] for g in value["graph"]["groups"]}
        available = [g for g in pending if not isinstance(g, dict) or not g.get("parent_id") or g.get("parent_id") in parents]
        if not available:
            for raw in pending:
                invalid(raw, ValueError())
            break
        for raw in available:
            pending.remove(raw)
            try:
                item = copy.deepcopy(raw)
                if not isinstance(item, dict):
                    graph.fail("模块格式无效。")
                item["node_ids"] = [stable(n) for n in item.get("node_ids", [])]
                keep("groups", item)
            except (KeyError, TypeError, ValueError, ProjectServiceError) as exc:
                invalid(raw, exc)
    if "paper_outline" in result:
        previous = value["paper_outline"]
        value["paper_outline"] = []
        pending = list(result["paper_outline"])
        while pending:
            parents = {s["section_id"] for s in value["paper_outline"]}
            available = [s for s in pending if not isinstance(s, dict) or not s.get("parent_id") or s.get("parent_id") in parents]
            if not available:
                for raw in pending:
                    invalid(raw, ValueError())
                break
            for raw in available:
                pending.remove(raw)
                try:
                    item = {**raw, "section_id": raw.get("section_id", raw.get("id")), "node_ids": [stable(n) for n in raw.get("node_ids", [])]}
                    value["paper_outline"].append(item)
                    try:
                        graph.validate(value)
                    except (KeyError, TypeError, ValueError, ProjectServiceError):
                        value["paper_outline"].pop()
                        raise
                except (KeyError, TypeError, ValueError, ProjectServiceError) as exc:
                    invalid(raw, exc)
        if not value["paper_outline"] and result["paper_outline"]:
            value["paper_outline"] = previous
    for suggestion in result.get("pending_suggestions", []):
        try:
            item = copy.deepcopy(suggestion)
            if not isinstance(item, dict):
                graph.fail("建议格式无效。")
            item.update(id="suggestion-" + uuid.uuid5(uuid.NAMESPACE_URL, did + ":" + str(suggestion["id"])).hex, status="pending")
            item["source_refs"] = [graph.reference(value, sid) for sid in item.pop("segment_ids", [])]
            for fields in (item, item.get("proposed_fields", {})):
                for field in ("from", "to", "owner_node_id", "target_id"):
                    if fields.get(field):
                        fields[field] = stable(fields[field])
            item["dependency_ids"] = ["suggestion-" + uuid.uuid5(uuid.NAMESPACE_URL, did + ":" + str(s)).hex for s in item.get("dependency_ids", [])]
            previous = value["pending_suggestions"]
            value["pending_suggestions"] = [s for s in previous if s["id"] != item["id"]] + [item]
            try:
                graph.validate(value)
            except (KeyError, TypeError, ValueError, ProjectServiceError):
                value["pending_suggestions"] = previous
                raise
        except (KeyError, TypeError, ValueError, ProjectServiceError) as exc:
            invalid(suggestion, exc)
    covered = result.get("covered_segment_ids", [])
    if any(not isinstance(s, str) or s not in source_ids for s in covered):
        invalid({}, ValueError())
    value["coverage"] = sorted(set(value.get("coverage", [])) | {s for s in covered if isinstance(s, str) and s in source_ids})
    # Historical diagnostics remain visible until a clean, complete replacement batch.
    errors = [] if result.get("complete") and not batch_errors else errors
    errors.extend(batch_errors)
    graph.validate(value)
    state = "ready" if result.get("complete") and not errors and set(value["coverage"]) == source_ids and value["source"]["status"] == "complete" else "partial"
    conn.execute("INSERT INTO structure_drafts VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(workspace_id,run_id) DO UPDATE SET revision=excluded.revision,workspace_json=excluded.workspace_json,errors_json=excluded.errors_json,state=excluded.state,updated_at=excluded.updated_at",
                 (did, pid, wid, run_id, expected + 1, saved["base_revision"] if saved else current["version"], graph.compact(value), graph.compact(errors), state, now()))
    response = {"draft_id": did, "draft_revision": expected + 1, "workspace_id": wid, "state": state,
                "counts": {k: len(value["graph"][k]) for k in ("nodes", "edges", "groups")},
                "covered": len(value["coverage"]), "total_segments": len(source_ids), "validation_errors": errors[:10]}
    return receipt(conn, pid, wid, "batch", data, digest, response)


