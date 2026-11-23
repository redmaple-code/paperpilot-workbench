"""Whitelisted adapters; every mutation delegates to its domain service."""
import json
import copy
from paperpilot.services import outline, logic_graph, workflow, research_design, project_resources
from paperpilot.services.project_errors import ProjectServiceError


def check_version(actual, expected):
    if actual != expected:
        raise ProjectServiceError(409, "stale_version", "目标版本已变化；原候选保留，请比较后重建提案。")


def selection_indices(payload, selection):
    operations = payload.get("operations")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 500:
        raise ProjectServiceError(422, "invalid_input", "请提供1到500条结构操作。")
    indices = selection.get("selected_indices")
    if indices is None:
        indices = list(range(len(operations)))
    if not indices or len(set(indices)) != len(indices) or any(type(i) is not int or not 0 <= i < len(operations) for i in indices):
        raise ProjectServiceError(422, "invalid_selection", "请选择有效的修改范围；空选择不会应用全部。")
    return indices


def outline_preview(conn, pid, payload, version, selection):
    graph = outline.read_outline(conn, pid)
    check_version(graph["version"], version)
    indices = selection_indices(payload, selection)
    if any(not isinstance(op, dict) or op.get("op") not in {"add_node", "update_node", "delete_node"} for op in payload["operations"]):
        raise ProjectServiceError(422, "invalid_operation", "大纲提案包含未知操作。")
    nodes = copy.deepcopy(graph["nodes"])
    for index in indices:
        operation = payload["operations"][index]
        if operation["op"] == "add_node":
            nodes.append(copy.deepcopy(operation.get("node") or {}))
        elif operation["op"] == "update_node":
            target = next((n for n in nodes if n["id"] == operation.get("id")), None)
            if target is None:
                raise ProjectServiceError(422, "invalid_input", "提案引用了不存在的大纲节点。")
            target.update({k: v for k, v in (operation.get("changes") or {}).items() if k != "id"})
        else:
            nodes = [n for n in nodes if n["id"] != operation.get("id")]
    after = outline._validate_nodes(nodes)
    return {"before": graph["nodes"], "after": after, "operations": [payload["operations"][i] for i in indices], "selected_indices": indices,
            "effect": "仅应用所选结构修改；纯画布布局不受影响。删除后若留下无父节点引用，必须调整选择。"}


def outline_apply(conn, pid, payload, version, selection, request_id):
    indices = selection_indices(payload, selection)
    candidate = outline.create_candidate(conn, pid, {"request_id": request_id+":candidate", "base_version": version, "operations": payload["operations"]})
    return outline.decide_candidate(conn, pid, candidate["id"], {"request_id": request_id, "base_version": version, "decision": "partial_apply", "selected_indices": indices})


def logic_preview(conn, pid, payload, version, selection):
    graph = logic_graph.read_graph(conn, pid)
    check_version(graph["version"], version)
    indices = selection_indices(payload, selection)
    nodes, edges = logic_graph.apply_operations(graph, payload["operations"], set(indices))
    return {"before": {"nodes": graph["nodes"], "edges": graph["edges"]}, "after": {"nodes": nodes, "edges": edges}, "selected_indices": indices}


def logic_apply(conn, pid, payload, version, selection, request_id):
    diff = logic_preview(conn, pid, payload, version, selection)
    return logic_graph.save_graph(conn, pid, base_version=version, request_id=request_id, **diff["after"])


def resource_preview(kind, conn, pid, payload, version, selection):
    row = conn.execute("SELECT project_revision FROM projects WHERE id=?", (pid,)).fetchone()
    check_version(row[0], version)
    keys = {"term", "definition", "aliases"} if kind == "term" else {"title", "content", "source"}
    required = "term" if kind == "term" else "title"
    if set(payload) - keys or not isinstance(payload.get(required), str) or not payload[required].strip():
        raise ProjectServiceError(422, "invalid_input", "候选缺少名称或包含不允许的字段。")
    if any(not isinstance(v, str) or len(v) > 16000 for v in payload.values()):
        raise ProjectServiceError(422, "invalid_input", "资源内容格式或长度无效。")
    duplicates = []
    if kind == "term":
        duplicates = [dict(r) for r in conn.execute("SELECT id,term,definition,version FROM terms WHERE project_id=? AND lower(trim(term))=lower(trim(?)) AND status='active'", (pid, payload["term"]))]
    return {"new_resource": payload, "existing_same_name": duplicates, "effect": "新增独立资源；已有同名定义保留，不覆盖。"}


def research_preview(conn, pid, payload, version, selection):
    check_version(workflow.current_snapshot(conn, pid)["version"], version)
    row = conn.execute("SELECT * FROM research_design_candidates WHERE id=? AND project_id=?", (payload.get("candidate_id"), pid)).fetchone()
    if row is None:
        raise ProjectServiceError(404, "resource_not_found", "阶段候选不存在。")
    check_version(row["base_workflow_version"], version)
    cards = json.loads(row["cards_json"])
    card = next((c for c in cards if c["id"] == payload.get("card_id")), None)
    if card is None or payload.get("field") not in research_design.STAGE_FIELDS.get(row["stage_id"], ()):
        raise ProjectServiceError(422, "invalid_target_field", "字段或候选不属于目标阶段。")
    if card.get("status") in {"rejected", "applied"}:
        raise ProjectServiceError(409, "action_closed", "此候选已有明确处理结果，请基于当前版本重新生成。")
    stage = next(s for s in research_design.plan_from_snapshot(workflow.current_snapshot(conn, pid))["stages"] if s["id"] == row["stage_id"])
    text = selection.get("edited_text")
    if text is None:
        text = (card.get("payload") or {}).get(payload["field"]) if card.get("payload") else research_design.compose_contribution(card, (payload["field"],))
    return {"stage_id": row["stage_id"], "field": payload["field"], "before": stage["fields"].get(payload["field"], ""),
            "after": text, "dedup_report": card.get("dedup_report"), "revision_required": stage["status"] in {"confirmed", "skipped"},
            "effect": "仅写阶段草稿；已确认阶段修订会使该阶段及下游重新待审，旧确认保留历史。"}


def research_apply(conn, pid, payload, version, selection, request_id):
    return research_design.apply_card(conn, pid, {**payload, **{k: selection.get(k) for k in ["edited_text", "review_reason", "acknowledge_revision"]},
                                                 "request_id": request_id, "expected_workflow_version": version})


def adapters():
    from paperpilot.services import agent_planning
    result = {"outline": {"preview": outline_preview, "apply": outline_apply},
              "logic": {"preview": logic_preview, "apply": logic_apply},
              "research": {"preview": research_preview, "apply": research_apply},
              "planning": {"preview": agent_planning.preview, "apply": agent_planning.apply}}
    for kind in ["term", "corpus"]:
        result[kind] = {"preview": lambda conn, pid, payload, version, selection, kind=kind: resource_preview(kind, conn, pid, payload, version, selection),
                        "apply": lambda conn, pid, payload, version, selection, request_id, kind=kind: getattr(project_resources, "add_"+kind)(conn, pid, payload)}
    return result
