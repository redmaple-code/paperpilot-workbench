"""Project-local source selection and extractive candidate previews."""
import json
from pathlib import PurePosixPath
import re
from urllib.parse import quote

from paperpilot.services import content_versions, innovation_points as points, logic_graph
from paperpilot.services import manuscript_import, manuscript_sources, outline, plan_state, structure_workspaces, workflow

CLAIM = re.compile(r"创新点|主要贡献|本文提出|本研究提出|我们提出|本文的贡献|首次|新方法|\b(?:contributions?|novel|we propose|we introduce|we develop|this paper proposes)\b", re.I)
RELEVANT_HEADING = re.compile(r"摘要|引言|方法|贡献|创新|结论|讨论|abstract|introduction|method|contribut|conclusion|discussion", re.I)
SUPPORTED_FILES = {".tex", ".md", ".txt"}


def _latest_plan(conn, project_id):
    row = conn.execute("SELECT * FROM workflow_snapshots WHERE project_id=? ORDER BY version DESC LIMIT 1",
                       (project_id,)).fetchone()
    if row:
        snapshot = workflow.parse_snapshot(row)
        return snapshot["version"], plan_state.plan_from_snapshot(snapshot)["stages"][0]["fields"].get("contribution", "")
    return 0, ""


def sources(conn, project_id):
    files = [{"kind": "file", "id": row["path"], "label": row["path"]}
             for row in conn.execute("SELECT path FROM files WHERE project_id=? ORDER BY path", (project_id,))
             if PurePosixPath(row["path"]).suffix.lower() in SUPPORTED_FILES]
    imports = [{"kind": "import", "id": row["id"], "label": "导入主稿 · " + row["name"]}
               for row in structure_workspaces.imported_manuscripts(conn, project_id)]
    _, legacy = _latest_plan(conn, project_id)
    history = [{"kind": "planning", "id": "STAGE-01", "label": "启动规划中的创新表述"}] if legacy.strip() else []
    research = [{"kind": "message", "id": row["id"], "label": "论文对话 · " + " ".join(row["text"].split())[:76]}
                for row in conn.execute("""SELECT id,text FROM agent_messages WHERE project_id=?
                    AND role IN ('user','assistant') AND text<>'' ORDER BY created_at DESC,seq DESC LIMIT 25""",
                                        (project_id,))]
    return files + imports + history + research


def _material(conn, project_id, kind, object_id):
    if kind == "file":
        row = conn.execute("SELECT content FROM files WHERE project_id=? AND path=?", (project_id, object_id)).fetchone()
        suffix = PurePosixPath(object_id).suffix.lower()
        if row is not None and suffix in SUPPORTED_FILES:
            text = row["content"]
            return content_versions.current_version(conn, project_id, "file", object_id), points.fingerprint(text), [
                {**part, "id": str(index)} for index, part in enumerate(manuscript_sources._sections(text, suffix))
            ]
    if kind == "import" and any(row["id"] == object_id for row in structure_workspaces.imported_manuscripts(conn, project_id)):
        batch = manuscript_import.batch(conn, object_id)
        materials = manuscript_import.sources(conn, object_id)
        selected = manuscript_import.primary(batch, materials)
        source = next((row for row in materials if row["id"] == selected), None)
        if source:
            return batch["revision"], points.fingerprint([selected, source["fragments"]]), source["fragments"]
    if kind == "planning" and object_id == "STAGE-01":
        version, text = _latest_plan(conn, project_id)
        if text.strip():
            return version, points.fingerprint(text), [{"id": "contribution", "location": "研究问题与创新点", "text": text}]
    if kind == "message":
        row = conn.execute("SELECT seq,text FROM agent_messages WHERE project_id=? AND id=? AND role IN ('user','assistant')",
                           (project_id, object_id)).fetchone()
        if row and row["text"].strip():
            return row["seq"], points.fingerprint(row["text"]), [{"id": object_id, "location": f"对话消息 {row['seq']}",
                                                                    "text": row["text"]}]
    points.fail(404, "innovation_source_unavailable", "当前论文中找不到可用的来源。")


def source_status(conn, project_id, source):
    from paperpilot.services.project_errors import ProjectServiceError
    try:
        _, digest, _ = _material(conn, project_id, source["kind"], source["id"])
    except ProjectServiceError as error:
        if error.status_code != 404:
            raise
        return "unavailable"
    return "current" if digest == source["fingerprint"] else "changed"


def _excerpt_options(parts, kind):
    matches, possible = [], []
    for fragment in parts:
        heading = fragment.get("heading", "").strip()
        paragraphs = re.split(r"\n\s*\n", fragment["text"])
        for paragraph in paragraphs:
            value = " ".join(paragraph.split()).strip()
            if len(value) < 12 or (value.startswith(("%", "\\documentclass", "\\usepackage", "\\bibliography")) and not CLAIM.search(value)):
                continue
            match = CLAIM.search(value)
            if match:
                start = max(value.rfind("。", 0, match.start()), value.rfind(". ", 0, match.start())) + 1
                value = value[start:].strip()
            target = matches if match or kind == "planning" else possible
            if target is possible and heading and not RELEVANT_HEADING.search(heading):
                continue
            if len(target) < 12:
                target.append((fragment, value[:850]))
        if len(matches) >= 12:
            break
    return matches or possible[:7]


def preview(conn, project_id, kind, object_id):
    version, digest, fragments = _material(conn, project_id, kind, object_id)
    items = []
    for fragment, excerpt in _excerpt_options(fragments, kind):
        source = {"kind": kind, "id": object_id, "version": version, "fingerprint": digest,
                  "anchor": fragment["id"], "location": fragment.get("location", object_id), "excerpt": excerpt}
        key = points.fingerprint(source)
        if not any(item["key"] == key for item in items):
            items.append({"key": key, "claim": excerpt, "difference": "", "scope": "", "support": [], "source": source})
    return {"kind": kind, "id": object_id, "version": version, "fingerprint": digest, "candidates": items}


def adopt(conn, project_id, data):
    points.require_writable(conn, project_id)
    identity = {"operation": "extract", **data}
    request_id = data["request_id"]
    if (prior := points.replay(conn, project_id, request_id, identity)) is not None:
        return prior
    current = preview(conn, project_id, data["kind"], data["id"])
    if current["fingerprint"] != data["fingerprint"]:
        points.fail(409, "innovation_source_changed", "来源已变化，请重新预览。")
    lookup = {candidate["key"]: candidate for candidate in current["candidates"]}
    selected = list(dict.fromkeys(data["keys"]))
    if not selected or any(key not in lookup for key in selected):
        points.fail(422, "innovation_selection_invalid", "请选择当前预览中的原文。")
    if data.get("point_id"):
        row = points.point_row(conn, project_id, data["point_id"])
        if len(selected) != 1 or not row["draft_json"]:
            points.fail(422, "innovation_relocation_invalid", "请选择一段原文重新定位当前草稿。")
        if row["draft_revision"] != data.get("draft_revision"):
            points.fail(409, "innovation_draft_stale", "草稿有更新，请刷新后重新定位。")
        body = json.loads(row["draft_json"])
        body["source"] = lookup[selected[0]]["source"]
        conn.execute("""UPDATE innovation_points SET draft_json=?,draft_revision=draft_revision+1
                        WHERE project_id=? AND id=?""", (points.dump(body), project_id, row["id"]))
        created = [row["id"]]
    else:
        existing = {}
        for row in conn.execute("""SELECT p.id,p.draft_json,v.body_json FROM innovation_points p
                                  LEFT JOIN innovation_versions v ON v.project_id=p.project_id AND v.point_id=p.id
                                  AND v.version=p.active_version WHERE p.project_id=?""", (project_id,)):
            for value in (row["draft_json"], row["body_json"]):
                source = json.loads(value).get("source") if value else None
                if source:
                    existing[points.fingerprint(source)] = row["id"]
        created = [existing.get(key) or points.new_draft(conn, project_id, {k: v for k, v in lookup[key].items() if k != "key"})
                   for key in selected]
    return points.remember(conn, project_id, request_id, identity, {"point_ids": created})


def support_options(conn, project_id):
    paper = quote(project_id, safe="")
    targets = [{"kind": "work", "id": row["id"], "version": "1", "label": row["title"], "status": row["status"],
                "url": f"/?project={paper}#chat/planning/resources"}
               for row in conn.execute("SELECT * FROM workflow_work_items WHERE project_id=? ORDER BY created_at,id",
                                       (project_id,))]
    graph = logic_graph.read_graph(conn, project_id)
    gaps = {item["node_id"] for item in graph.get("gaps", [])}
    conflicts = {node_id for item in graph.get("conflicts", []) for node_id in item["node_ids"]}
    targets.extend({"kind": "logic", "id": node["id"], "version": str(graph["version"]),
                    "label": node["label"], "status": "conflict" if node["id"] in conflicts else "gap" if node["id"] in gaps else "linked",
                    "url": f"/?project={paper}#chat/knowledge/graph"}
                   for node in graph["nodes"])
    graph = outline.read_outline(conn, project_id)
    targets.extend({"kind": "outline", "id": node["id"], "version": str(graph["version"]),
                    "label": node["title"], "status": "linked", "url": f"/outline?project={paper}#outline/nodes"}
                   for node in graph["nodes"])
    targets.extend({"kind": "result", "id": row["dataset_id"], "version": row["id"],
                    "label": f"{row['title']} · v{row['version_no']}", "status": "linked",
                    "url": f"/api/projects/{paper}/results/{quote(row['dataset_id'], safe='')}/versions/{quote(row['id'], safe='')}/download"}
                   for row in conn.execute("""SELECT v.id,v.dataset_id,v.version_no,d.title FROM result_versions v
                        JOIN result_datasets d ON d.id=v.dataset_id WHERE v.project_id=? AND d.project_id=?
                        ORDER BY d.title,v.version_no DESC""", (project_id, project_id)))
    targets.extend({"kind": "asset", "id": row["asset_id"], "version": row["id"],
                    "label": f"{row['title']} · 图像 v{row['version_no']}", "status": "linked",
                    "url": f"/api/projects/{paper}/drawing/assets/{quote(row['asset_id'], safe='')}/versions/{quote(row['id'], safe='')}/preview"}
                   for row in conn.execute("""SELECT v.id,v.asset_id,v.version_no,a.title FROM figure_asset_versions v
                        JOIN figure_assets a ON a.id=v.asset_id WHERE v.project_id=? AND a.project_id=?
                        AND v.status='ready' AND a.status='active' ORDER BY a.title,v.version_no DESC""", (project_id, project_id)))
    return targets
