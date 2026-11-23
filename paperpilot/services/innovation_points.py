"""Author-confirmed, project-local innovation claims and their provenance."""
from __future__ import annotations

from hashlib import sha256
import json
import uuid
from urllib.parse import quote

from paperpilot.services import provider_runtime
from paperpilot.services.project_errors import ProjectServiceError


def initialize_schema(conn):
    statements = (
        """CREATE TABLE IF NOT EXISTS innovation_sets (
            project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            revision INTEGER NOT NULL, primary_id TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS innovation_points (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            id TEXT NOT NULL, draft_revision INTEGER NOT NULL, active_version INTEGER,
            draft_json TEXT, first_confirmed_at TEXT, created_at TEXT NOT NULL,
            PRIMARY KEY (project_id, id)
        )""",
        """CREATE TABLE IF NOT EXISTS innovation_versions (
            project_id TEXT NOT NULL, point_id TEXT NOT NULL, version INTEGER NOT NULL,
            parent_version INTEGER, body_json TEXT NOT NULL, confirmed_at TEXT NOT NULL,
            PRIMARY KEY (project_id, point_id, version),
            FOREIGN KEY (project_id, point_id) REFERENCES innovation_points(project_id, id) ON DELETE CASCADE
        )""",
        """CREATE TABLE IF NOT EXISTS innovation_commands (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, result_json TEXT NOT NULL,
            PRIMARY KEY (project_id, request_id)
        )""",
        """CREATE TABLE IF NOT EXISTS innovation_usages (
            project_id TEXT NOT NULL, point_id TEXT NOT NULL, version INTEGER NOT NULL,
            role TEXT NOT NULL, set_revision INTEGER, kind TEXT NOT NULL, object_id TEXT NOT NULL,
            object_version TEXT NOT NULL, label TEXT NOT NULL,
            PRIMARY KEY (project_id, point_id, version, role, kind, object_id, object_version),
            FOREIGN KEY (project_id, point_id, version)
                REFERENCES innovation_versions(project_id, point_id, version) ON DELETE CASCADE
        )""",
    )
    for statement in statements:
        conn.execute(statement)


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def fingerprint(value):
    return sha256(dump(value).encode()).hexdigest()


def fail(status, code, message):
    raise ProjectServiceError(status, code, message)


def require_writable(conn, project_id):
    row = conn.execute("SELECT lifecycle_state FROM projects WHERE id=?", (project_id,)).fetchone()
    if row is None:
        fail(404, "project_not_found", "论文不存在。")
    if row["lifecycle_state"] == "published":
        fail(409, "project_published_read_only", "已发布论文为只读状态。")


def active_set(conn, project_id):
    row = conn.execute("SELECT revision,primary_id FROM innovation_sets WHERE project_id=?", (project_id,)).fetchone()
    return (row["revision"], row["primary_id"]) if row else (0, None)


def point_row(conn, project_id, point_id):
    row = conn.execute("SELECT * FROM innovation_points WHERE project_id=? AND id=?", (project_id, point_id)).fetchone()
    if row is None:
        fail(404, "innovation_not_found", "当前论文中找不到这条创新点。")
    return row


def _version(conn, project_id, point_id, version):
    row = conn.execute("""SELECT * FROM innovation_versions
                          WHERE project_id=? AND point_id=? AND version=?""", (project_id, point_id, version)).fetchone()
    if row is None:
        fail(404, "innovation_version_not_found", "找不到这条创新点的指定版本。")
    return json.loads(row["body_json"]) | {
        "version": row["version"], "parent_version": row["parent_version"], "confirmed_at": row["confirmed_at"],
    }


def _summary(conn, project_id, row, primary):
    active = row["active_version"]
    return {
        "point_id": row["id"], "draft_revision": row["draft_revision"],
        "draft": json.loads(row["draft_json"]) if row["draft_json"] is not None else None,
        "active": _version(conn, project_id, row["id"], active) if active else None,
        "role": ("primary" if row["id"] == primary else "secondary") if active else None,
    }


def list_points(conn, project_id):
    revision, primary = active_set(conn, project_id)
    rows = conn.execute("""SELECT * FROM innovation_points WHERE project_id=?
                           ORDER BY first_confirmed_at,created_at,id""", (project_id,)).fetchall()
    values = [_summary(conn, project_id, row, primary) for row in rows]
    return {
        "project_id": project_id, "active_revision": revision,
        "primary": next((item for item in values if item["role"] == "primary"), None),
        "secondary": [item for item in values if item["role"] == "secondary"],
        "drafts": [item for item in values if item["role"] is None],
    }


def read_point(conn, project_id, point_id, version=None):
    revision, primary = active_set(conn, project_id)
    row = point_row(conn, project_id, point_id)
    item = _summary(conn, project_id, row, primary)
    history = conn.execute("""SELECT version,parent_version,confirmed_at FROM innovation_versions
                             WHERE project_id=? AND point_id=? ORDER BY version DESC""", (project_id, point_id))
    usages = [dict(record) for record in conn.execute(
        """SELECT version,role,set_revision,kind,object_id,object_version,label FROM innovation_usages
           WHERE project_id=? AND point_id=? ORDER BY version DESC,kind,label,object_id""", (project_id, point_id))]
    for usage in usages:
        usage["needs_review"] = (
            usage["version"] != row["active_version"] or usage["role"] != item["role"]
            or (usage["set_revision"] is not None and usage["set_revision"] != revision)
        )
        _usage_location(conn, project_id, usage)
    from paperpilot.services.innovation_sources import source_status, support_options
    if item["draft"] and item["draft"].get("source"):
        item["draft"]["source_status"] = source_status(conn, project_id, item["draft"]["source"])
    selected = _version(conn, project_id, point_id, version) if version is not None else item["active"]
    bodies = [body for body in (selected, item["draft"]) if body and body["support"]]
    options = {(option["kind"], option["id"], option["version"]): option
               for option in support_options(conn, project_id)} if any(
                   support["refs"] for body in bodies for support in body["support"]) else {}
    for body in bodies:
        for support in body["support"]:
            support["targets"] = [
                options.get((ref["kind"], ref["id"], ref["version"]), ref | {"status": "historical"})
                for ref in support["refs"]
            ]
    return item | {
        "active_revision": revision, "selected": selected,
        "history": [dict(record) for record in history], "usages": usages,
    }


def _usage_location(conn, project_id, usage):
    paper = quote(project_id, safe="")
    identifier = quote(usage["object_id"], safe="")
    version = quote(usage["object_version"], safe="")
    kind = usage["kind"]
    if kind == "result_card":
        usage["url"] = f"/?project={paper}#chat/visual/result-detail/card/{version}/{identifier}"
    elif kind in {"outline", "outline_candidate", "structure_workspace", "structure_draft"}:
        usage["url"] = f"/outline?project={paper}#outline/nodes"
        if kind == "outline":
            usage["version_url"] = f"/api/projects/{paper}/outline/structured?version={version}"
        elif kind == "structure_draft":
            row = conn.execute("""SELECT d.workspace_id FROM structure_drafts d JOIN structure_workspaces w
                                  ON d.workspace_id=w.id WHERE d.id=? AND w.project_id=?""",
                               (usage["object_id"], project_id)).fetchone()
            if row:
                usage["version_url"] = (f"/api/projects/{paper}/structure-workspaces/"
                                        f"{quote(row['workspace_id'], safe='')}/drafts/{identifier}")
    elif kind == "writing_candidate":
        usage["url"] = f"/?project={paper}#chat/history/ai"
        usage["version_url"] = f"/api/projects/{paper}/candidates/{identifier}"
    elif kind in {"logic", "logic_candidate"}:
        usage["url"] = f"/?project={paper}#chat/knowledge/graph"
        if kind == "logic":
            usage["version_url"] = f"/api/projects/{paper}/logic/graph?version={version}"
    elif kind in {"planning", "planning_candidate"}:
        usage["url"] = f"/?project={paper}#chat/planning/startup"


def new_draft(conn, project_id, body):
    point_id = uuid.uuid4().hex
    conn.execute("""INSERT INTO innovation_points
                    (project_id,id,draft_revision,draft_json,created_at) VALUES (?,?,1,?,?)""",
                 (project_id, point_id, dump(body), provider_runtime.stamp()))
    return point_id


def replay(conn, project_id, request_id, identity):
    row = conn.execute("SELECT * FROM innovation_commands WHERE project_id=? AND request_id=?",
                       (project_id, request_id)).fetchone()
    if row:
        if row["fingerprint"] != fingerprint(identity):
            fail(409, "innovation_request_conflict", "请求编号已用于另一项操作。")
        return json.loads(row["result_json"])
    return None


def remember(conn, project_id, request_id, identity, result):
    conn.execute("INSERT INTO innovation_commands VALUES (?,?,?,?)",
                 (project_id, request_id, fingerprint(identity), dump(result)))
    return result


def _save_body(conn, project_id, body, previous=None):
    from paperpilot.services.innovation_sources import support_options
    if body is None:
        fail(422, "innovation_body_required", "请填写创新点草稿。")
    if not any(support["refs"] for support in body["support"]):
        return dict(body)
    allowed = {(option["kind"], option["id"], option["version"]) for option in support_options(conn, project_id)}
    if previous:
        allowed.update((ref["kind"], ref["id"], ref["version"]) for support in previous["support"] for ref in support["refs"])
    for support in body["support"]:
        for ref in support["refs"]:
            if (ref["kind"], ref["id"], ref["version"]) not in allowed:
                fail(422, "innovation_support_unavailable", "支撑来源不属于当前论文或已发生变化。")
    return dict(body)


def command(conn, project_id, data):
    """Caller owns the enclosing immediate transaction."""
    require_writable(conn, project_id)
    request_id = data["request_id"]
    if (prior := replay(conn, project_id, request_id, data)) is not None:
        return prior
    action = data["action"]
    if data.get("body") is not None and action not in {"create_draft", "save_draft"}:
        fail(422, "innovation_unreviewed_changes", "请先保存并审阅草稿，再确认。")
    revision, primary = active_set(conn, project_id)
    if action == "create_draft":
        point_id = new_draft(conn, project_id, _save_body(conn, project_id, data.get("body")))
    else:
        point_id = data.get("point_id")
        row = point_row(conn, project_id, point_id)
        if action in {"save_draft", "confirm"} and data.get("draft_revision") != row["draft_revision"]:
            fail(409, "innovation_draft_stale", "草稿已更新，请对照新版再提交。")
        previous = json.loads(row["draft_json"]) if row["draft_json"] else (
            _version(conn, project_id, point_id, row["active_version"]) if row["active_version"] else None)
        if action == "save_draft":
            body = _save_body(conn, project_id, data.get("body"), previous)
            if previous and previous.get("source") and not data.get("detach_source"):
                body["source"] = previous["source"]
            conn.execute("""UPDATE innovation_points SET draft_json=?, draft_revision=draft_revision+1
                            WHERE project_id=? AND id=?""", (dump(body), project_id, point_id))
        else:
            if data.get("active_revision") != revision:
                fail(409, "innovation_set_stale", "生效版本或主创新已变化，请刷新核对。")
            if action == "confirm":
                _confirm(conn, project_id, row, previous)
                primary = primary or point_id
            elif action == "set_primary":
                if row["active_version"] is None:
                    fail(422, "innovation_not_confirmed", "请先确认这一条创新点。")
                primary = point_id
            if action == "confirm" or primary != active_set(conn, project_id)[1]:
                conn.execute("""INSERT INTO innovation_sets VALUES (?,?,?)
                                ON CONFLICT(project_id) DO UPDATE SET revision=excluded.revision,primary_id=excluded.primary_id""",
                             (project_id, revision + 1, primary))
    result = _summary(conn, project_id, point_row(conn, project_id, point_id), primary)
    result["active_revision"] = active_set(conn, project_id)[0]
    return remember(conn, project_id, request_id, data, result)


def _confirm(conn, project_id, row, body):
    from paperpilot.services.innovation_sources import source_status
    if not row["draft_json"] or not body["claim"].strip():
        fail(422, "innovation_claim_required", "请先保存待确认的创新主张。")
    if body.get("source") and source_status(conn, project_id, body["source"]) != "current":
        fail(409, "innovation_source_changed", "原文已变化，请重新选取来源或以作者录入的主张保存。")
    version = (row["active_version"] or 0) + 1
    stamp = provider_runtime.stamp()
    conn.execute("INSERT INTO innovation_versions VALUES (?,?,?,?,?,?)",
                 (project_id, row["id"], version, row["active_version"], dump(body), stamp))
    conn.execute("""UPDATE innovation_points SET active_version=?,draft_json=NULL,draft_revision=draft_revision+1,
                    first_confirmed_at=COALESCE(first_confirmed_at,?) WHERE project_id=? AND id=?""",
                 (version, stamp, project_id, row["id"]))


def context(conn, project_id, point_ids=None):
    """Omit the entire block when the current task has no confirmed claims."""
    rows = conn.execute("""SELECT p.id,v.version,v.body_json,s.revision,s.primary_id
                           FROM innovation_points p JOIN innovation_versions v
                           ON v.project_id=p.project_id AND v.point_id=p.id AND v.version=p.active_version
                           JOIN innovation_sets s ON s.project_id=p.project_id
                           WHERE p.project_id=? ORDER BY p.id<>s.primary_id,p.first_confirmed_at,p.id""", (project_id,)).fetchall()
    items = []
    for row in rows:
        if point_ids is not None and row["id"] not in point_ids:
            continue
        body = json.loads(row["body_json"])
        items.append({"point_id": row["id"], "version": row["version"], "role": "primary" if row["id"] == row["primary_id"] else "secondary",
                      **{field: body[field] for field in ("claim", "difference", "scope")},
                      "support": [{"description": item["description"], "expected_output": item["expected_output"]} for item in body["support"]]})
    if not items:
        return None
    return {"items": items, "set_revision": rows[0]["revision"] if point_ids is None else None}


def references(block):
    if not block:
        return []
    return [{"point_id": item["point_id"], "version": item["version"], "role": item["role"],
             "set_revision": block["set_revision"]} for item in block["items"]]


def attach(payload, block):
    if not block:
        return payload
    return payload | {"innovations": block["items"]}


def focus(block, task_text):
    """Local writing gets the primary claim and explicitly relevant secondary claims."""
    if not block:
        return None
    text = task_text.casefold()
    selected = []
    for item in block["items"]:
        claim = item["claim"].strip().casefold()
        if item["role"] == "primary" or item["point_id"] in text or (claim and claim in text):
            selected.append(item)
    return {"items": selected, "set_revision": None} if selected else None


def manifest_references(items):
    return [item["ref"] for item in items if item.get("kind") == "innovation"]


def references_current(conn, project_id, refs):
    if not refs:
        return True
    revision, primary = active_set(conn, project_id)
    for ref in refs:
        row = conn.execute("SELECT active_version FROM innovation_points WHERE project_id=? AND id=?",
                           (project_id, ref["point_id"])).fetchone()
        role = "primary" if ref["point_id"] == primary else "secondary"
        if not row or row["active_version"] != ref["version"] or ref["role"] != role:
            return False
        if ref.get("set_revision") is not None and ref["set_revision"] != revision:
            return False
    return True


def record_usage(conn, project_id, refs, kind, object_id, version, label):
    for ref in refs:
        conn.execute("""INSERT INTO innovation_usages VALUES (?,?,?,?,?,?,?,?,?)
                        ON CONFLICT DO NOTHING""",
                     (project_id, ref["point_id"], ref["version"], ref["role"], ref.get("set_revision"),
                      kind, object_id, str(version), label))


def usage_references(conn, project_id, kind, object_id, object_version=None):
    query = """SELECT point_id,version,role,set_revision FROM innovation_usages
               WHERE project_id=? AND kind=? AND object_id=?"""
    args = (project_id, kind, object_id)
    if object_version is not None:
        query += " AND object_version=?"
        args += (str(object_version),)
    return [dict(row) for row in conn.execute(query, args)]


def discussion_requested(instruction):
    return any(word in instruction.casefold() for word in ("创新", "贡献", "novelty", "contribution"))


def _propose(args, runtime):
    conn, project_id = runtime["conn"], runtime["project_id"]
    if not discussion_requested(runtime.get("state", {}).get("instruction", "")):
        fail(422, "innovation_proposal_not_requested", "请在对话中明确要求梳理或保存创新点候选。")
    owns_transaction = not conn.in_transaction
    if owns_transaction:
        conn.execute("BEGIN IMMEDIATE")
    try:
        require_writable(conn, project_id)
        claim = args["claim"].strip()
        if not claim:
            fail(422, "innovation_claim_required", "请提供一条可审阅主张。")
        body = {"claim": claim, "difference": args.get("difference", ""), "scope": args.get("scope", ""), "support": []}
        if runtime.get("run_id"):
            message = conn.execute("""SELECT id,seq,text FROM agent_messages WHERE project_id=? AND run_id=?
                                      AND role='user' ORDER BY seq DESC LIMIT 1""",
                                   (project_id, runtime["run_id"])).fetchone()
            if message:
                body["source"] = {"kind": "message", "id": message["id"], "version": message["seq"],
                                  "anchor": message["id"], "location": f"对话消息 {message['seq']}",
                                  "excerpt": message["text"][:850], "fingerprint": fingerprint(message["text"])}
        point_id = new_draft(conn, project_id, body)
        if owns_transaction:
            conn.commit()
    except BaseException:
        if owns_transaction:
            conn.rollback()
        raise
    return {"cards": [{"kind": "innovation_draft", "id": point_id, "project_id": project_id,
                       "title": "审阅创新点候选", "summary": claim[:320]}]}


def get_tools():
    from paperpilot.services.agent_tools import Tool, WRITE_CANDIDATE
    properties = {name: {"type": "string", "maxLength": 4000} for name in ("claim", "difference", "scope")}
    return [Tool("propose_innovation", "作者明确要求梳理或保存创新点时，保存一条可审阅候选。提交真实主张和边界，作者在详情确认。",
                 {"type": "object", "properties": properties, "required": ["claim"], "additionalProperties": False},
                 _propose, side_effect=WRITE_CANDIDATE, max_calls_per_run=6, intents=("research", "scholar"))]
