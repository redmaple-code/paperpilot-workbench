from __future__ import annotations

import copy
import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from paperpilot.services.project_errors import ProjectServiceError


NODE_KINDS = {"claim", "evidence", "reasoning", "conclusion", "gap"}
NODE_FIELDS = {"id", "kind", "label", "details", "status", "source_anchor", "source_version", "source_status", "legacy_links"}


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _projection(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    evidence_nodes = {item["id"] for item in nodes if item["kind"] == "evidence"}
    evidence_links: set[str] = set()
    conflicts: list[dict[str, Any]] = []
    for edge in edges:
        left = next(item for item in nodes if item["id"] == edge["from_node_id"])
        right = next(item for item in nodes if item["id"] == edge["to_node_id"])
        if edge["edge_type"] in {"supports", "provides_evidence", "evidence_for"}:
            if left["kind"] == "claim" and right["id"] in evidence_nodes:
                evidence_links.add(left["id"])
            if left["id"] in evidence_nodes and right["kind"] == "claim":
                evidence_links.add(right["id"])
            if right["id"] in evidence_nodes and left["kind"] == "claim":
                evidence_links.add(left["id"])
        if edge["edge_type"] == "contradicts":
            conflicts.append({"id": edge["id"], "node_ids": [left["id"], right["id"]], "status": "unresolved"})
    gaps = [
        {
            "id": f"gap:{item['id']}:evidence",
            "node_id": item["id"],
            "kind": "missing_evidence",
            "status": "open",
            "reason": "该论点尚未关联证据节点。",
        }
        for item in nodes
        if item["kind"] == "claim" and item["id"] not in evidence_links
    ]
    gaps.extend(
        {
            "id": f"gap:{item['id']}:source",
            "node_id": item["id"],
            "kind": "source_unavailable",
            "status": item.get("source_status", "stale"),
            "reason": "来源当前不可访问或锚点需要重新确认。",
        }
        for item in nodes
        if item.get("source_status") in {"stale", "inaccessible", "reanchor_required"}
    )
    return {"gaps": gaps, "conflicts": conflicts}


def validate_graph(nodes: Any, edges: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ProjectServiceError(422, "invalid_graph", "nodes 和 edges 必须是数组。")
    normalized_nodes: list[dict[str, Any]] = []
    node_ids: set[str] = set()
    for raw in nodes:
        if not isinstance(raw, dict):
            raise ProjectServiceError(422, "invalid_node", "节点必须是对象。")
        node_id = str(raw.get("id") or "").strip()
        kind = str(raw.get("kind") or "").strip()
        label = str(raw.get("label") or "").strip()
        if not node_id or not label or not kind:
            raise ProjectServiceError(422, "invalid_node", "节点必须包含稳定 ID、有效类型和非空标签。")
        if kind not in NODE_KINDS:
            raise ProjectServiceError(422, "invalid_node_kind", "节点类型无效。")
        if node_id in node_ids:
            raise ProjectServiceError(422, "duplicate_node", f"节点 ID 重复：{node_id}")
        node_ids.add(node_id)
        item = {key: copy.deepcopy(value) for key, value in raw.items() if key in NODE_FIELDS}
        item.update({"id": node_id, "kind": kind, "label": label, "details": str(raw.get("details") or "")})
        item.setdefault("status", "active")
        normalized_nodes.append(item)

    normalized_edges: list[dict[str, Any]] = []
    edge_ids: set[str] = set()
    edge_keys: set[tuple[str, str, str]] = set()
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for raw in edges:
        if not isinstance(raw, dict):
            raise ProjectServiceError(422, "invalid_edge", "关系必须是对象。")
        edge_id = str(raw.get("id") or "").strip() or uuid.uuid4().hex
        source = str(raw.get("from_node_id") or "").strip()
        target = str(raw.get("to_node_id") or "").strip()
        edge_type = str(raw.get("edge_type") or "").strip()
        if source not in node_ids or target not in node_ids or not edge_type:
            raise ProjectServiceError(422, "invalid_edge", "关系必须引用当前图中的节点并指定类型。")
        if edge_id in edge_ids:
            raise ProjectServiceError(422, "duplicate_edge", f"关系 ID 重复：{edge_id}")
        edge_ids.add(edge_id)
        if source == target:
            raise ProjectServiceError(422, "logic_cycle", "节点不能指向自身。")
        key = (source, target, edge_type)
        if key in edge_keys:
            raise ProjectServiceError(422, "duplicate_edge", "图中存在重复关系。")
        edge_keys.add(key)
        adjacency[source].append(target)
        normalized_edges.append({"id": edge_id, "from_node_id": source, "to_node_id": target, "edge_type": edge_type})

    indegree = {node_id: 0 for node_id in node_ids}
    for targets in adjacency.values():
        for target in targets:
            indegree[target] += 1
    ready = [node_id for node_id, degree in indegree.items() if degree == 0]
    visited_count = 0
    while ready:
        node_id = ready.pop()
        visited_count += 1
        for target in adjacency[node_id]:
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
    if visited_count != len(node_ids):
        raise ProjectServiceError(422, "logic_cycle", "逻辑关系不能形成有向循环。")
    return normalized_nodes, normalized_edges


def read_graph(conn: sqlite3.Connection, project_id: str, version: int | None = None) -> dict[str, Any]:
    if version is None:
        row = conn.execute(
            "SELECT * FROM logic_graph_versions WHERE project_id=? ORDER BY version DESC LIMIT 1", (project_id,)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM logic_graph_versions WHERE project_id=? AND version=?", (project_id, version)
        ).fetchone()
    if row:
        nodes = json.loads(row["nodes_json"])
        edges = json.loads(row["edges_json"])
        return {
            "version": int(row["version"]),
            "nodes": nodes,
            "edges": edges,
            "gaps": json.loads(row["gaps_json"]),
            **_projection(nodes, edges),
        }
    if version not in (None, 0):
        raise ProjectServiceError(404, "graph_version_missing", "找不到指定逻辑图版本。")
    legacy = conn.execute("SELECT * FROM logic_nodes WHERE project_id=? ORDER BY rowid", (project_id,)).fetchall()
    kind_aliases = {"论点": "claim", "证据": "evidence", "推理": "reasoning", "结论": "conclusion", "缺口": "gap"}
    nodes = []
    for row in legacy:
        raw_kind = str(row["kind"] or "claim")
        kind = raw_kind if raw_kind in NODE_KINDS else kind_aliases.get(raw_kind, "claim")
        nodes.append({
            "id": row["id"], "kind": kind, "label": row["label"],
            "details": row["details"], "status": "active", "legacy_links": "",
        })
    node_ids = {item["id"] for item in nodes}
    edges = []
    for row in legacy:
        targets = [part.strip() for part in str(row["links"] or "").split(",") if part.strip()]
        unresolved = []
        for target in targets:
            if target in node_ids and target != row["id"]:
                edges.append({
                    "id": f"legacy:{row['id']}:{target}",
                    "from_node_id": row["id"], "to_node_id": target, "edge_type": "supports",
                })
            else:
                unresolved.append(target)
        node = next(item for item in nodes if item["id"] == row["id"])
        node["legacy_links"] = ",".join(unresolved)
    outgoing: dict[str, list[str]] = {}
    for edge in edges:
        outgoing.setdefault(edge["from_node_id"], []).append(edge["to_node_id"])
    for node in nodes:
        if not node["legacy_links"]:
            node["legacy_links"] = ",".join(outgoing.get(node["id"], []))
    return {"version": 0, "nodes": nodes, "edges": edges, **_projection(nodes, edges)}


def save_graph(
    conn: sqlite3.Connection,
    project_id: str,
    *,
    base_version: int,
    request_id: str,
    nodes: Any,
    edges: Any,
) -> dict[str, Any]:
    normalized_nodes, normalized_edges = validate_graph(nodes, edges)
    existing = conn.execute(
        "SELECT * FROM logic_graph_versions WHERE project_id=? AND request_id=?", (project_id, request_id)
    ).fetchone()
    if existing:
        if existing["nodes_json"] != _dump(normalized_nodes) or existing["edges_json"] != _dump(normalized_edges):
            raise ProjectServiceError(409, "idempotency_key_reused", "request_id 已用于不同的逻辑图写入。")
        return read_graph(conn, project_id, int(existing["version"]))
    latest_version = conn.execute(
        "SELECT MAX(version) AS version FROM logic_graph_versions WHERE project_id=?", (project_id,)
    ).fetchone()
    current_version = int(latest_version["version"] or 0)
    if current_version != base_version:
        raise ProjectServiceError(409, "graph_conflict", "逻辑图版本已变化；请刷新后比较并重试。")
    latest_row = conn.execute(
        "SELECT MAX(version) AS version FROM logic_graph_versions WHERE project_id=?", (project_id,)
    ).fetchone()
    latest = int(latest_row["version"] or 0)
    if not conn.execute("SELECT 1 FROM logic_graph_versions WHERE project_id=? LIMIT 1", (project_id,)).fetchone():
        baseline = read_graph(conn, project_id)
        baseline_projection = _projection(baseline["nodes"], baseline["edges"])
        conn.execute(
            "INSERT INTO logic_graph_versions(project_id,version,request_id,nodes_json,edges_json,gaps_json,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                project_id, 0, f"graph-baseline:{uuid.uuid4().hex}",
                _dump(baseline["nodes"]), _dump(baseline["edges"]),
                _dump(baseline_projection["gaps"]), datetime.now(timezone.utc).isoformat(),
            ),
        )
        latest = 0
    if latest != base_version:
        raise ProjectServiceError(409, "graph_conflict", "逻辑图版本已变化；请刷新后比较并重试。")
    version = latest + 1
    projection = _projection(normalized_nodes, normalized_edges)
    stamp = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO logic_graph_versions(project_id,version,request_id,nodes_json,edges_json,gaps_json,created_at) VALUES(?,?,?,?,?,?,?)",
        (project_id, version, request_id, _dump(normalized_nodes), _dump(normalized_edges), _dump(projection["gaps"]), stamp),
    )
    conn.execute("UPDATE projects SET updated_at=?,project_revision=project_revision+1 WHERE id=?", (stamp, project_id))
    return {"version": version, "nodes": normalized_nodes, "edges": normalized_edges, **projection}


def apply_operations(graph: dict[str, Any], operations: list[dict[str, Any]], selected: set[int] | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    nodes = copy.deepcopy(graph["nodes"])
    edges = copy.deepcopy(graph["edges"])
    for index, operation in enumerate(operations):
        if selected is not None and index not in selected:
            continue
        if not isinstance(operation, dict):
            raise ProjectServiceError(422, "invalid_suggestion", "AI 建议操作格式无效。")
        kind = operation.get("op")
        if kind == "add_node":
            nodes.append(copy.deepcopy(operation.get("node") or {}))
        elif kind == "update_node":
            target = operation.get("id")
            node = next((item for item in nodes if item["id"] == target), None)
            if node is None:
                raise ProjectServiceError(422, "invalid_suggestion", "AI 建议引用了不存在的节点。")
            changes = operation.get("changes")
            if not isinstance(changes, dict):
                raise ProjectServiceError(422, "invalid_suggestion", "节点修改必须是字段对象。")
            node.update({key: copy.deepcopy(value) for key, value in changes.items() if key in NODE_FIELDS - {"id"}})
        elif kind == "add_edge":
            edges.append(copy.deepcopy(operation.get("edge") or {}))
        elif kind == "remove_edge":
            edge_id = operation.get("id")
            before = len(edges)
            edges = [item for item in edges if item["id"] != edge_id]
            if before == len(edges):
                raise ProjectServiceError(422, "invalid_suggestion", "AI 建议引用了不存在的关系。")
        else:
            raise ProjectServiceError(422, "invalid_suggestion", "AI 建议仅可新增/编辑节点或新增/解除关系。")
    return validate_graph(nodes, edges)

