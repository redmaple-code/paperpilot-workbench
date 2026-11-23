"""Atomic outline/workflow/Traceify application through the existing action service."""
import copy
import uuid

from paperpilot.services import innovation_points, outline, workflow, agent_actions, agent_domain_adapters
from paperpilot.services import structure_workspaces as store, structure_graph as graph
from paperpilot.services.plan_state import plan_from_snapshot, derived_position


def proposal_payload(conn, pid, wid, draft_id=None, replace_outline=False):
    current = store.read(conn, pid, wid)
    draft = store.read_draft(conn, pid, wid, draft_id) if draft_id else None
    if draft and draft["base_revision"] != current["version"]:
        graph.fail("结构草稿基线已变化，请比较后重新生成。", "workspace_stale", 409)
    refs = innovation_points.usage_references(conn, pid, "structure_draft", draft_id, draft["version"]) if draft else (
        innovation_points.usage_references(conn, pid, "structure_workspace", wid, current["version"]))
    if not innovation_points.references_current(conn, pid, refs):
        graph.fail("论文创新点已变化，请重新生成结构建议。", "innovation_context_stale", 409)
    value = draft["workspace"] if draft else current["workspace"]
    tree = outline.read_outline(conn, pid)
    sections = value.get("paper_outline", [])
    if not sections:
        graph.fail("暂无章节提案。请先让AI搭建大纲，或添加章节节点。")
    ids = {s["section_id"]: s["section_id"] if s["section_id"] in {n["id"] for n in tree["nodes"]} else uuid.uuid5(uuid.NAMESPACE_URL, wid + ":section:" + s["section_id"]).hex for s in sections}
    operations, by_id = [], {n["id"]: n for n in tree["nodes"]}
    for section in sections:
        node = {"id": ids[section["section_id"]], "title": section["title"], "purpose": section.get("purpose", ""),
                "scope": section.get("scope", ""), "parent_id": ids.get(section.get("parent_id")), "position": section.get("position", 0), "status": "draft"}
        if section.get("parent_id") and section["parent_id"] not in ids:
            graph.fail("章节父节点不存在。", "dependency_missing")
        if node["id"] in by_id:
            operations.append({"op": "update_node", "id": node["id"], "changes": {k: v for k, v in node.items() if k != "id"}})
        else:
            operations.append({"op": "add_node", "node": node})
    if replace_outline:
        operations += [{"op": "delete_node", "id": n["id"]} for n in tree["nodes"] if n["id"] not in ids.values()]
    return {"workspace_id": wid, "draft_id": draft_id, "workspace_revision": current["version"],
            "outline_version": tree["version"], "workflow_version": workflow.current_snapshot(conn, pid)["version"],
            "source_hash": value["source"]["content_hash"], "operations": operations, "sections": sections,
            "section_ids": ids, "replace_outline": replace_outline, **({"innovation_refs": refs} if refs else {})}


def preview(conn, pid, payload, version, selection):
    current = store.read(conn, pid, payload["workspace_id"])
    if current["host"]["context_changed"] or current["version"] != payload["workspace_revision"]:
        graph.fail("结构或构思已变化，请重新预览。", "workspace_stale", 409)
    if workflow.current_snapshot(conn, pid)["version"] != payload["workflow_version"]:
        graph.fail("阶段草稿已变化，请重新预览。", "workflow_conflict", 409)
    canonical = proposal_payload(conn, pid, payload["workspace_id"], payload.get("draft_id"), payload.get("replace_outline", False))
    if canonical != payload:
        graph.fail("章节建议或来源已变化。", "preview_stale", 409)
    result = agent_domain_adapters.outline_preview(conn, pid, payload, version, selection)
    plan = plan_from_snapshot(workflow.current_snapshot(conn, pid))
    result["stage_revision_required"] = plan["stages"][1]["status"] in {"confirmed", "skipped"}
    result["effect"] = "写入所选章节、结构绑定和STAGE-02草稿。已确认阶段需明确接受修订，旧记录保留。"
    return result


def apply(conn, pid, payload, version, selection, request_id):
    diff = preview(conn, pid, payload, version, selection)
    if diff["stage_revision_required"] and not selection.get("acknowledge_revision"):
        graph.fail("请明确接受修订已确认的大纲阶段。", "revision_required", 409)
    current_tree = outline.read_outline(conn, pid)
    surviving = {n["id"] for n in diff["after"]}
    links = [link for link in current_tree.get("links", []) if link.get("node_id") in surviving]
    result = outline._write_version(conn, pid, version, request_id, diff["after"], links, source="traceify_proposal")
    innovation_points.record_usage(conn, pid, payload.get("innovation_refs", []), "outline", str(result["version"]),
                                   result["version"], "已采纳 Traceify 大纲")
    snapshot = workflow.current_snapshot(conn, pid)
    plan = plan_from_snapshot(snapshot)
    stage = plan["stages"][1]
    stage["fields"].update(outline="\n".join(n["title"] for n in diff["after"]),
                           objectives="\n".join(n["title"] + " — " + n.get("purpose", "") for n in diff["after"]))
    stage["status"] = "draft"
    stage["invalidated_reason"] = "Traceify章节提案已应用，待审阅。"
    if diff["stage_revision_required"]:
        for following in plan["stages"][2:]:
            if following["status"] in {"confirmed", "skipped"}:
                following["status"] = "draft"
                following["invalidated_reason"] = "前序大纲已修订。"
        plan["current_index"], plan["status"] = 1, "active"
    position = derived_position(plan)
    new_workflow = workflow.append_snapshot(conn, pid, expected_version=payload["workflow_version"],
        current_stage=position[0] if position else snapshot["current_stage"],
        stage_status=position[1] if position else snapshot["stage_status"], stage_outputs={"startup_plan": plan}, confirmation_status="pending")
    current = store.read(conn, pid, payload["workspace_id"])
    value = copy.deepcopy(current["workspace"])
    candidate = store.read_draft(conn, pid, payload["workspace_id"], payload["draft_id"])["workspace"] if payload.get("draft_id") else value
    selected_sections = []
    selected_node_ids = set()
    operation_ids = {op.get("id", (op.get("node") or {}).get("id")) for op in diff["operations"] if op["op"] != "delete_node"}
    for section in payload["sections"]:
        domain_id = payload["section_ids"][section["section_id"]]
        if domain_id in operation_ids:
            selected_sections.append(section)
            selected_node_ids.update(section.get("node_ids", []))
            value["outline_bindings"][domain_id] = section.get("node_ids", [])
    nodes = {n["id"]: n for n in value["graph"]["nodes"]}
    for node in candidate["graph"]["nodes"]:
        if node["id"] in selected_node_ids:
            nodes[node["id"]] = {**node, "status": "confirmed"}
    value["graph"]["nodes"] = list(nodes.values())
    edges = {e["id"]: e for e in value["graph"]["edges"]}
    for edge in candidate["graph"]["edges"]:
        if edge["from"] in selected_node_ids and edge["to"] in selected_node_ids:
            edges[edge["id"]] = {**edge, "status": "confirmed"}
    value["graph"]["edges"] = list(edges.values())
    groups = {g["id"]: g for g in value["graph"]["groups"]}
    for group in candidate["graph"]["groups"]:
        if all(node_id in nodes for node_id in group["node_ids"]):
            groups[group["id"]] = copy.deepcopy(group)
    while any(g.get("parent_id") and g["parent_id"] not in groups for g in groups.values()):
        groups = {key: g for key, g in groups.items() if not g.get("parent_id") or g["parent_id"] in groups}
    value["graph"]["groups"] = list(groups.values())
    value["paper_outline"] = [{**n, "section_id": n["id"], "node_ids": value["outline_bindings"].get(n["id"], [])} for n in result["nodes"]]
    value["domain_versions"] = {"outline": result["version"], "workflow": new_workflow["version"]}
    saved = store.write(conn, pid, payload["workspace_id"], {"request_id": request_id + ":bindings", "expected_workspace_revision": current["version"],
                        "source_hash": value["source"]["content_hash"], "commands": [{"op": "replace_graph", "workspace": value}]},
                        domain_updates={"outline_bindings": value["outline_bindings"], "domain_versions": value["domain_versions"]})
    return {**result, "workspace_revision": saved["version"], "workflow_version": new_workflow["version"]}


def install(adapters):
    from paperpilot.services import agent_tools, structure_tools
    adapters["structure_outline"] = {"preview": preview, "apply": apply}
    if "traceify_context" not in agent_tools.REGISTRY:
        agent_tools.register_provider(structure_tools)
