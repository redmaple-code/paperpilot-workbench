"""Traceify contract validation and linked projections, independent of rendering."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import uuid

from paperpilot.services.project_errors import ProjectServiceError

KINDS = {"concept", "claim", "entity", "section", "evidence", "other"}
PROVENANCE = {"extracted", "ai_suggestion", "user_edited"}
STATUSES = {"confirmed", "pending", "deleted", "broken"}


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def fingerprint(value):
    return hashlib.sha256(compact(value).encode()).hexdigest()


def fail(message, code="invalid_input", status=422):
    raise ProjectServiceError(status, code, message)


def identifier(prefix):
    return prefix + "-" + uuid.uuid4().hex


def reference(workspace, segment_id):
    segment = next((s for s in workspace["source"]["segments"] if s["id"] == segment_id), None)
    if segment is None:
        fail("来源段落不存在。", "invalid_reference")
    return {"source_id": workspace["source"]["id"], "granularity": "paragraph", "locator": segment_id,
            "excerpt": segment["text"], "overview": segment["text"][:160]}


def validate(workspace):
    """Validate complete snapshots, including retained tombstones and suggestions."""
    if not isinstance(workspace, dict) or not isinstance(workspace.get("source"), dict):
        fail("工作区缺少来源。")
    if len(compact(workspace)) > 4000000:
        fail("工作区超过4MB，请减少单图范围。")
    source = workspace["source"]
    layout = workspace.get("layout", {})
    if layout.get("grouping_mode", "auto") not in {"auto", "manual"}:
        fail("模块布局模式无效。")
    if source.get("kind") not in {"text", "markdown", "webpage"} or source.get("status") not in {"complete", "partial", "failed"}:
        fail("来源类型或读取状态无效。")
    segments = source.get("segments", [])
    if not isinstance(segments, list) or len(segments) > 5000:
        fail("来源段落格式或数量无效。")
    by_segment = {}
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get("id"), str) or not isinstance(segment.get("text"), str):
            fail("段落需要稳定 ID 和原文。")
        if segment["id"] in by_segment:
            fail("来源段落 ID 重复。")
        by_segment[segment["id"]] = segment["text"]
    graph = workspace.get("graph", {})
    nodes, edges, groups = graph.get("nodes", []), graph.get("edges", []), graph.get("groups", [])
    if not all(isinstance(items, list) for items in (nodes, edges, groups)) or len(nodes) > 500 or len(edges) > 2000 or len(groups) > 500:
        fail("图谱上限为500个节点、2000条关系和500个模块，含历史对象。")
    ids = set()
    for kind, items in (("node", nodes), ("edge", edges)):
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str) or not 1 <= len(item["id"]) <= 160 or item["id"] in ids:
                fail("对象需要唯一稳定 ID。")
            ids.add(item["id"])
            label = item.get("label" if kind == "node" else "relation")
            if not isinstance(label, str) or not 1 <= len(label.strip()) <= 180:
                fail("名称须为1到180字。")
            if kind == "node" and item.get("kind", "other") not in KINDS:
                fail("节点类型无效。")
            if item.get("status", "confirmed") not in STATUSES or item.get("provenance", "user_edited") not in PROVENANCE:
                fail("对象状态或来源类型无效。")
            confidence = item.get("confidence")
            if confidence is not None and (isinstance(confidence, bool) or not isinstance(confidence, (int, float)) or not math.isfinite(confidence) or not 0 <= confidence <= 1):
                fail("置信度应为0到1或null。")
            for field in ("summary", "content", "relation_basis"):
                if field in item and (not isinstance(item[field], str) or len(item[field]) > 30000):
                    fail("内容字段无效或过长。")
            if not isinstance(item.get("locked_fields", []), list) or any(not isinstance(f, str) for f in item.get("locked_fields", [])):
                fail("锁定字段格式无效。")
            refs = item.get("source_refs", [])
            if not isinstance(refs, list):
                fail("来源引用须为数组。")
            for ref in refs:
                if not isinstance(ref, dict) or ref.get("source_id") != source.get("id") or ref.get("locator") not in by_segment:
                    fail("来源须指向当前快照段落。", "invalid_reference")
                if ref.get("excerpt") != by_segment[ref["locator"]]:
                    fail("来源摘录与原文不一致。", "invalid_reference")
    node_ids = {n["id"] for n in nodes}
    for edge in edges:
        if edge.get("from") not in node_ids or edge.get("to") not in node_ids or edge.get("from") == edge.get("to"):
            fail("关系需要两个有效的不同端点。")
    by_group = {}
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("id"), str) or group["id"] in by_group:
            fail("模块 ID 无效或重复。")
        if not isinstance(group.get("label"), str) or not group["label"].strip():
            fail("模块需要名称。")
        if not isinstance(group.get("node_ids"), list) or any(n not in node_ids for n in group["node_ids"]):
            fail("模块引用了不存在的节点。")
        by_group[group["id"]] = group
    for group in groups:
        parent, visited = group.get("parent_id"), {group["id"]}
        while parent is not None:
            if parent not in by_group or parent in visited or len(visited) >= 32:
                fail("模块层级必须无环且不超过32层。")
            visited.add(parent)
            parent = by_group[parent].get("parent_id")
    suggestions = workspace.get("pending_suggestions", [])
    if not isinstance(suggestions, list) or len(suggestions) > 2000:
        fail("建议格式或数量无效。")
    seen = set()
    for suggestion in suggestions:
        if not isinstance(suggestion, dict) or not suggestion.get("id") or suggestion["id"] in seen or suggestion.get("status") not in {"pending", "accepted", "rejected"}:
            fail("建议 ID 或状态无效。")
        seen.add(suggestion["id"])
    sections = workspace.get("paper_outline", [])
    from paperpilot.services import outline
    if sections:
        outline._validate_nodes([{**s, "id": s.get("section_id", s.get("id")), "title": s.get("title", "")} for s in sections])
        for section in sections:
            if not isinstance(section.get("node_ids", []), list) or any(n not in node_ids for n in section.get("node_ids", [])):
                fail("章节引用了不存在的图谱节点。", "invalid_reference")
    return derive(workspace)


def derive(workspace):
    """Rebuild indexes and bounded multi-edge evidence paths without losing history."""
    all_nodes = {n["id"]: n for n in workspace["graph"]["nodes"]}
    nodes = {k: n for k, n in all_nodes.items() if n.get("status") != "deleted"}
    edges = []
    for edge in workspace["graph"]["edges"]:
        if edge.get("status") != "deleted" and (edge["from"] not in nodes or edge["to"] not in nodes):
            edge["status"] = "broken"
        if edge.get("status") not in {"deleted", "broken"} and (workspace.get("preview_mode") or (edge.get("status") != "pending" and all(nodes.get(k, {}).get("status") != "pending" for k in (edge["from"], edge["to"])) )):
            edges.append(edge)
    confirmed = {k: n for k, n in nodes.items() if workspace.get("preview_mode") or n.get("status") != "pending"}
    mappings = {}
    for items, key in ((list(confirmed.values()), "node_ids"), (edges, "edge_ids")):
        for item in items:
            for ref in item.get("source_refs", []):
                entry = mappings.setdefault(ref["locator"], {"source_ref": ref, "node_ids": [], "edge_ids": []})
                entry[key].append(item["id"])
    workspace["mappings"] = list(mappings.values())
    outgoing = {n: [] for n in confirmed}
    for edge in edges:
        outgoing[edge["from"]].append(edge)
    incoming = {e["to"] for e in edges}
    order, seen = [], set()
    def visit(node_id, depth):
        if node_id in seen:
            return
        seen.add(node_id)
        order.append({"node_id": node_id, "depth": depth})
        for edge in outgoing[node_id]:
            visit(edge["to"], depth + 1)
    for node_id in list(k for k in confirmed if k not in incoming) + list(confirmed):
        visit(node_id, 0)
    workspace["outline"] = order
    previous = {c["id"]: c for c in workspace.get("evidence_chains", [])}
    paths = []
    stack = [([node_id], [], []) for node_id in confirmed if node_id not in incoming]
    if not stack:
        stack = [([node_id], [], []) for node_id in list(confirmed)[:1]]
    while stack and len(paths) < 500:
        node_path, edge_path, refs = stack.pop()
        current = confirmed[node_path[-1]]
        refs = refs + current.get("source_refs", [])
        children = [e for e in outgoing[current["id"]] if e["to"] not in node_path]
        if not children or len(node_path) >= 32:
            if edge_path or refs:
                unique = {r["locator"]: r for r in refs}
                key = "chain-" + fingerprint([node_path, edge_path])[:24]
                paths.append({"id": key, "node_ids": node_path, "edge_ids": edge_path,
                              "source_refs": list(unique.values()), "status": "complete" if refs else "unsubstantiated",
                              "truncated": bool(children)})
        else:
            for edge in children:
                stack.append((node_path + [edge["to"]], edge_path + [edge["id"]], refs + edge.get("source_refs", [])))
    edge_ids = {e["id"] for e in edges}
    for key, chain in previous.items():
        if any(n not in confirmed for n in chain.get("node_ids", [])) or any(e not in edge_ids for e in chain.get("edge_ids", [])):
            chain["status"] = "broken"
            paths.append(chain)
    workspace["evidence_chains"] = paths[:1000]
    workspace["chain_expansion_truncated"] = bool(stack)
    return workspace


def apply_commands(workspace, commands):
    result = copy.deepcopy(workspace)
    for command in commands:
        op = command.get("op")
        if op == "replace_graph":
            value = copy.deepcopy(command["workspace"])
            if value.get("source") != result.get("source"):
                fail("更换来源需要建立新工作区，旧依据保留。", "source_context_stale", 409)
            old_by_id = {i["id"]: i for k in ("nodes", "edges") for i in result["graph"][k]}
            new_by_id = {i["id"]: i for k in ("nodes", "edges") for i in value["graph"][k]}
            for key, old in old_by_id.items():
                if key not in new_by_id:
                    deleted = copy.deepcopy(old)
                    deleted["status"] = "deleted"
                    collection = "nodes" if key in {n["id"] for n in result["graph"]["nodes"]} else "edges"
                    value["graph"][collection].append(deleted)
                    new_by_id[key] = deleted
                new = new_by_id[key]
                locked = set(old.get("locked_fields", [])) | set(new.get("locked_fields", []))
                for field in ("label", "relation", "summary", "content", "relation_basis", "source_refs", "from", "to"):
                    if new.get(field) != old.get(field):
                        if new.get("field_provenance", {}).get(field) in {"model", "source"}:
                            if old.get(field) or field in old.get("locked_fields", []):
                                fail("自动补充不能覆盖已有或锁定字段。", "workspace_stale", 409)
                        else:
                            locked.add(field)
                new["locked_fields"] = sorted(locked)
                new["versions"] = {**old.get("versions", {}), **new.get("versions", {}), "user": compact({f: new.get(f) for f in locked})}
                new["field_provenance"] = {**old.get("field_provenance", {}), **{f: "user" for f in locked}}
            for field in ("graph", "pending_suggestions", "paper_outline"):
                if field in value:
                    result[field] = copy.deepcopy(value[field])
            for field in ("grouping_mode", "module_layout_version"):
                if field in value.get("layout", {}):
                    result.setdefault("layout", {})[field] = value["layout"][field]
        elif op == "edit":
            items = result["graph"]["nodes" if command["kind"] == "node" else "edges"]
            item = next((x for x in items if x["id"] == command["id"]), None)
            if item is None:
                fail("对象不存在。", "resource_not_found", 404)
            fields = set(command.get("edited_fields", [])) | set(command.get("explicit_clear_fields", []))
            patch = command.get("fields", {})
            if fields - {"label", "relation", "summary", "content", "relation_basis", "source_refs", "from", "to", "status"}:
                fail("字段不允许修改。")
            for field in fields:
                item[field] = "" if field in command.get("explicit_clear_fields", []) else patch.get(field, item.get(field))
            item["locked_fields"] = sorted(set(item.get("locked_fields", [])) | fields)
            item.setdefault("field_provenance", {}).update({f: "user" for f in fields})
            item.setdefault("versions", {})["user"] = compact(patch)
            item["provenance"] = "user_edited"
        elif op == "suggestions":
            selected = command.get("ids", [])
            if not selected:
                fail("请明确选择建议。")
            by_id = {s["id"]: s for s in result["pending_suggestions"]}
            if any(k not in by_id or by_id[k]["status"] != "pending" for k in selected):
                fail("建议已变化。", "workspace_stale", 409)
            for key in selected:
                suggestion = by_id[key]
                if set(suggestion.get("dependency_ids", [])) - set(selected) - {k for k, s in by_id.items() if s["status"] == "accepted"}:
                    fail("所选建议缺少依赖项。", "dependency_missing")
                decision = command.get("decision")
                if decision == "reject":
                    suggestion["status"] = "rejected"
                elif decision == "accept":
                    proposed = copy.deepcopy(command.get("edited", {}).get(key, suggestion.get("proposed_fields", suggestion)))
                    kind = suggestion.get("target_kind", "edge")
                    items = result["graph"]["nodes" if kind == "node" else "edges"]
                    target = next((x for x in items if x["id"] == suggestion.get("target_id")), None)
                    if suggestion.get("suggested_action", "add") == "add":
                        proposed.update(id=identifier("n" if kind == "node" else "e"), status="confirmed", provenance="ai_suggestion", review_state="pending")
                        items.append(proposed)
                    elif target:
                        target.update(proposed)
                    else:
                        fail("建议目标不存在。")
                    suggestion["status"] = "accepted"
                elif decision == "edit":
                    suggestion["proposed_fields"] = command["edited"][key]
                else:
                    fail("建议动作无效。")
        else:
            fail("未知结构命令。")
    return validate(result)
