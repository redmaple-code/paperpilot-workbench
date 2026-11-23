"""Reviewable actions. Domain writes, receipts and events share the caller's transaction."""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from paperpilot.services.project_errors import ProjectServiceError

OPERATIONS = {"outline", "logic", "term", "corpus", "reference", "oa", "research", "planning", "structure_outline"}
KINDS = {"outline": "outline_proposal", "logic": "logic_proposal", "term": "term_proposal",
         "corpus": "term_proposal", "reference": "reference_candidate", "oa": "reference_candidate", "research": "research_candidate", "planning": "planning_proposal"}
TITLES = {"outline": "大纲修改提案", "logic": "逻辑图修改提案", "term": "术语候选", "corpus": "语料候选",
          "reference": "参考文献交接", "oa": "公开全文导入", "research": "阶段研究候选", "planning": "待讨论的论文方案"}


KINDS["structure_outline"] = "outline_proposal"
TITLES["structure_outline"] = "Traceify 章节与目标提案"


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(compact(value).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def initialize_schema(conn):
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS agent_actions(
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, conversation_id TEXT NOT NULL, run_id TEXT NOT NULL,
        operation TEXT NOT NULL, base_version INTEGER NOT NULL, payload_json TEXT NOT NULL, sources_json TEXT NOT NULL,
        status TEXT NOT NULL, request_id TEXT NOT NULL, input_fingerprint TEXT NOT NULL,
        preview_json TEXT NOT NULL DEFAULT '{}', preview_fingerprint TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(project_id,request_id),
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
      );
      CREATE TABLE IF NOT EXISTS agent_action_receipts(
        project_id TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, result_json TEXT NOT NULL,
        PRIMARY KEY(project_id,request_id), FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
      );
      CREATE TABLE IF NOT EXISTS agent_action_events(
        seq INTEGER PRIMARY KEY AUTOINCREMENT, project_id TEXT NOT NULL, action_id TEXT NOT NULL,
        event_type TEXT NOT NULL, payload_json TEXT NOT NULL, created_at TEXT NOT NULL,
        FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE,
        FOREIGN KEY(action_id) REFERENCES agent_actions(id) ON DELETE CASCADE
      );
      CREATE INDEX IF NOT EXISTS idx_agent_action_project ON agent_actions(project_id,created_at);
    ''')


def require_writable(conn, project_id):
    row = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
    if not row:
        raise ProjectServiceError(404, "resource_not_found", "论文不存在。")
    if row["archived"] or row["lifecycle_state"] == "published":
        raise ProjectServiceError(403, "read_only", "论文已归档或发布，只能查看；请恢复后再应用。")
    return row


def sources_current(conn, project_id, sources):
    for source in sources:
        if not isinstance(source, dict) or source.get("kind") != "corpus" or not source.get("id"):
            raise ProjectServiceError(422, "invalid_source", "来源必须是当前论文中已授权语料的稳定编号。")
        row = conn.execute("SELECT s.* FROM corpus_source_versions s JOIN corpus c ON c.id=s.corpus_id WHERE c.id=? AND c.project_id=? ORDER BY s.version DESC LIMIT 1",
                           (source["id"], project_id)).fetchone()
        if not row or row["permission_state"] != "authorized" or row["status"] != "active":
            raise ProjectServiceError(403, "access_denied", "来源不存在或已撤权，原候选保留，请重新选择来源。")
        if source.get("version") != row["version"]:
            raise ProjectServiceError(409, "stale_version", "来源已更新，请核对后重新生成提案。")


def read(conn, project_id, action_id):
    row = conn.execute("SELECT * FROM agent_actions WHERE id=? AND project_id=?", (action_id, project_id)).fetchone()
    if row is None:
        raise ProjectServiceError(404, "resource_not_found", "候选不属于当前论文或已不存在。")
    return dict(row)


def card(row):
    value = {"schema_version": 1, "id": row["id"], "kind": KINDS[row["operation"]], "project_id": row["project_id"],
            "conversation_id": row["conversation_id"], "run_id": row["run_id"], "status": row["status"],
            "title": TITLES[row["operation"]], "summary": "请审阅目标和差异，再明确应用。",
            "source_refs": json.loads(row["sources_json"]), "target": {"kind": row["operation"]},
            "base_version": row["base_version"], "limitations": ["候选不等于已确认事实；正文插入不等于文件保存。"],
            "actions": ["preview", "apply", "reject"], "payload": json.loads(row["payload_json"])}
    value['selection'] = json.loads(row['preview_json']).get('selection', {})
    if row['operation'] == 'planning':
        value['limitations'] = []
        value['summary'] = '核对这份方案，采纳后继续讨论。'
        value['reviewed_plan'] = json.loads(row['preview_json']).get('diff', {})
        if row['status'] == 'applied':
            confirmed = value['payload'].get('mode') == 'confirm' and value['selection'].get('selected_indices') is None
            value['title'] = '已确认的论文方案' if confirmed else '已保存的讨论草稿'
            value['summary'] = ''
    return value


def conversation_note(conn, row, text, cards=None):
    if not row.get("conversation_id"):
        return
    from paperpilot.services import agent_conversations
    agent_conversations.append_message(conn, row["project_id"], row["conversation_id"], "assistant", text,
                                       cards=cards, run_id=row.get("run_id", ""), context={"action_id": row["id"]})


def create(conn, project_id, data, adapters):
    from paperpilot.services import innovation_points
    require_writable(conn, project_id)
    for key, table in [("run_id", "agent_runs"), ("conversation_id", "agent_conversations")]:
        if not data.get(key):
            continue
        exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        owner = conn.execute("SELECT project_id FROM "+table+" WHERE id=?", (data[key],)).fetchone() if exists else None
        if not owner or owner[0] != project_id:
            raise ProjectServiceError(404, "resource_not_found", "运行或会话不属于当前论文。")
    if data.get("run_id"):
        run = conn.execute("SELECT conversation_id,input_json FROM agent_runs WHERE id=?", (data["run_id"],)).fetchone()
        if (run["conversation_id"] or "") != data.get("conversation_id", ""):
            raise ProjectServiceError(409, "target_changed", "运行与提案会话不一致；未创建提案，请返回原会话。")
    op = data["operation"]
    if op not in OPERATIONS or op not in adapters:
        raise ProjectServiceError(422, "invalid_operation", "此操作未注册，不能执行任意地址或命令。")
    if len(compact(data).encode()) > 200000:
        raise ProjectServiceError(422, "invalid_input", "提案过大，请缩小修改范围。")
    sources_current(conn, project_id, data.get("source_refs", []))
    if adapters[op].get("validate_scope"):
        adapters[op]["validate_scope"](project_id, data["payload"], data.get("conversation_id", ""))
    fp = digest(data)
    prior = conn.execute("SELECT * FROM agent_actions WHERE project_id=? AND request_id=?", (project_id, data["request_id"])).fetchone()
    if prior:
        if prior["input_fingerprint"] != fp:
            raise ProjectServiceError(409, "idempotency_key_reused", "请求编号已用于另一份提案。")
        return card(dict(prior))
    # Validate payload and version using a pure domain preview before recording a proposal.
    adapters[op]["preview"](conn, project_id, data["payload"], data["base_version"], {})
    action_id = uuid.uuid4().hex
    conn.execute("INSERT INTO agent_actions(id,project_id,conversation_id,run_id,operation,base_version,payload_json,sources_json,status,request_id,input_fingerprint,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                 (action_id, project_id, data.get("conversation_id", ""), data.get("run_id", ""), op, data["base_version"], compact(data["payload"]), compact(data.get("source_refs", [])), "proposed", data["request_id"], fp, now(), now()))
    if data.get("run_id") and op in {"outline", "logic", "planning"}:
        refs = json.loads(run["input_json"]).get("_context_manifest", {}).get("innovation_refs", [])
        if not innovation_points.references_current(conn, project_id, refs):
            raise ProjectServiceError(409, "innovation_context_stale", "创新点已修订，请重新生成此提案。")
        innovation_points.record_usage(conn, project_id, refs, "agent_action", action_id, data["base_version"], TITLES[op])
    stored = read(conn, project_id, action_id)
    result = card(stored)
    conversation_note(conn, stored, "已创建可审阅提案；尚未修改领域内容。", [result])
    return result


def preview(conn, project_id, action_id, selection, adapters):
    from paperpilot.services import innovation_points
    require_writable(conn, project_id)
    row = read(conn, project_id, action_id)
    if row["status"] in {"applied", "rejected"}:
        raise ProjectServiceError(409, "action_closed", "此候选已有明确处理结果。")
    sources_current(conn, project_id, json.loads(row["sources_json"]))
    refs = innovation_points.usage_references(conn, project_id, "agent_action", action_id)
    if not innovation_points.references_current(conn, project_id, refs):
        raise ProjectServiceError(409, "innovation_context_stale", "创新点已修订，请重新生成此提案。")
    result = adapters[row["operation"]]["preview"](conn, project_id, json.loads(row["payload_json"]), row["base_version"], selection)
    value = {"action_id": action_id, "target": row["operation"], "base_version": row["base_version"],
             "diff": result, "selection": selection, "cost": "无额外模型调用"}
    fp = digest({"action": row["input_fingerprint"], "preview": value})
    conn.execute("UPDATE agent_actions SET status='previewed',preview_json=?,preview_fingerprint=?,updated_at=? WHERE id=?",
                 (compact(value), fp, now(), action_id))
    return value | {"action_fingerprint": fp}


def apply(conn, project_id, action_id, data, adapters):
    from paperpilot.services import innovation_points
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    require_writable(conn, project_id)
    fp = digest({"action_id": action_id, **data})
    previous = conn.execute("SELECT * FROM agent_action_receipts WHERE project_id=? AND request_id=?", (project_id, data["request_id"])).fetchone()
    if previous:
        if previous["fingerprint"] != fp:
            raise ProjectServiceError(409, "idempotency_key_reused", "应用请求编号已用于其他操作。")
        return json.loads(previous["result_json"]) | {"idempotent_replay": True}
    row = read(conn, project_id, action_id)
    if not data.get("accepted"):
        raise ProjectServiceError(422, "acceptance_required", "请先审阅差异并明确采纳。")
    if row["status"] != "previewed" or data["action_fingerprint"] != row["preview_fingerprint"]:
        raise ProjectServiceError(409, "preview_stale", "预览已变化，请重新核对目标与差异。")
    if data["expected_version"] != row["base_version"]:
        raise ProjectServiceError(409, "stale_version", "应用版本与候选不一致。")
    sources_current(conn, project_id, json.loads(row["sources_json"]))
    refs = innovation_points.usage_references(conn, project_id, "agent_action", action_id)
    if not innovation_points.references_current(conn, project_id, refs):
        raise ProjectServiceError(409, "innovation_context_stale", "创新点已修订，请重新生成此提案。")
    payload = json.loads(row["payload_json"])
    saved_preview = json.loads(row["preview_json"])
    selection = saved_preview["selection"]
    adapter = adapters[row["operation"]]
    current_diff = adapter["preview"](conn, project_id, payload, row["base_version"], selection)
    current_preview = {**saved_preview, "diff": current_diff}
    if digest({"action": row["input_fingerprint"], "preview": current_preview}) != row["preview_fingerprint"]:
        raise ProjectServiceError(409, "preview_stale", "来源或差异在预览后发生变化，请重新查看预览；原候选保留。")
    result = adapter["apply"](conn, project_id, payload, row["base_version"], selection, data["request_id"])
    kind = "outline" if row["operation"] == "structure_outline" else row["operation"]
    if result.get("version") is not None:
        innovation_points.record_usage(conn, project_id, refs, kind, str(result["version"]), result["version"], TITLES[row["operation"]])
    event = {"project_id": project_id, "kind": kind, "version": result.get("version"), "origin": "agent_action", "request_id": data["request_id"]}
    response = {"ok": True, "status": "applied", "action_id": action_id, "result": result, "event": event}
    conn.execute("UPDATE agent_actions SET status='applied',updated_at=? WHERE id=?", (now(), action_id))
    conn.execute("INSERT INTO agent_action_events(project_id,action_id,event_type,payload_json,created_at) VALUES(?,?,?,?,?)", (project_id, action_id, "action_applied", compact(response), now()))
    conn.execute("INSERT INTO agent_action_receipts VALUES(?,?,?,?)", (project_id, data["request_id"], fp, compact(response)))
    note = result.get('message', '') if row['operation'] == 'planning' else "应用回执：" + TITLES[row["operation"]] + "；action_id=" + action_id + "；领域版本=" + str(result.get("version", "待领域流程确认")) + "。" + result.get("message", "阶段确认与正文保存仍需单独执行。")
    conversation_note(conn, row, note)
    return response


def reject(conn, project_id, action_id):
    require_writable(conn, project_id)
    row = read(conn, project_id, action_id)
    if row["status"] == "applied":
        raise ProjectServiceError(409, "action_closed", "已应用内容请通过领域历史恢复。")
    conn.execute("UPDATE agent_actions SET status='rejected',updated_at=? WHERE id=?", (now(), action_id))
    if row["status"] != "rejected":
        conversation_note(conn, row, "已拒绝提案 " + action_id + "，领域内容未改变。")
    return {"status": "rejected", "action_id": action_id}
