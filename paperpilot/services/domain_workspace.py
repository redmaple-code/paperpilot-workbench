from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from typing import Any

from paperpilot.services.project_errors import ProjectServiceError


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def list_domains(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT d.*,COUNT(DISTINCT p.id) AS project_count FROM domains d LEFT JOIN projects p ON p.domain_id=d.id GROUP BY d.id ORDER BY d.name,d.id").fetchall()
    return [dict(row) for row in rows]


def create_domain(conn: sqlite3.Connection, name: str, description: str = "") -> dict[str, Any]:
    name = str(name or "").strip()
    if not name:
        raise ProjectServiceError(422, "domain_name_required", "领域名称不能为空。")
    if conn.execute("SELECT 1 FROM domains WHERE lower(name)=lower(?)", (name,)).fetchone():
        raise ProjectServiceError(409, "domain_exists", "该领域已存在。")
    stamp = _now(); domain_id = uuid.uuid4().hex
    conn.execute("INSERT INTO domains(id,name,description,version,created_at,updated_at) VALUES(?,?,?,?,?,?)", (domain_id,name,description,1,stamp,stamp))
    conn.execute("INSERT INTO domain_versions(domain_id,version,status,batch_id,created_at) VALUES(?,?,?,?,?)", (domain_id, 1, "active", "", stamp))
    return dict(conn.execute("SELECT * FROM domains WHERE id=?", (domain_id,)).fetchone())


def assign_project_domain(conn: sqlite3.Connection, project_id: str, domain_id: str, base_revision: int) -> dict[str, Any]:
    project = conn.execute("SELECT project_revision,domain_id FROM projects WHERE id=?", (project_id,)).fetchone()
    domain = conn.execute("SELECT * FROM domains WHERE id=?", (domain_id,)).fetchone()
    if project is None or domain is None:
        raise ProjectServiceError(404, "domain_binding_missing", "论文或领域不存在。")
    if int(project["project_revision"]) != base_revision:
        raise ProjectServiceError(409, "project_version_conflict", "论文已变化，请刷新后重试。")
    current_domain = str(project["domain_id"] or "")
    if current_domain == domain_id:
        return {"project_id": project_id, "domain_id": domain_id, "domain": domain["name"], "project_revision": int(project["project_revision"]), "unchanged": True}
    if current_domain:
        raise ProjectServiceError(409, "domain_migration_unavailable", "已有论文暂不支持跨领域迁移；当前领域、配置和资料均未改变。可在目标领域新建论文，再按需显式复制资料。")
    stamp = _now()
    # A legacy unbound paper keeps its existing values. Point its baseline at
    # the new domain so any later defaults are compared and applied explicitly.
    baseline = conn.execute("SELECT settings_json,sources_json FROM project_setting_baselines WHERE project_id=?", (project_id,)).fetchone()
    if baseline:
        values = json.loads(baseline["settings_json"] or "{}")
        sources = json.loads(baseline["sources_json"] or "{}")
        values["domain"] = domain["name"]
        sources["domain"] = {"source": "project_baseline", "domain_version": None}
        conn.execute("UPDATE project_setting_baselines SET domain_id=?,domain_version=0,version=version+1,settings_json=?,sources_json=?,updated_at=? WHERE project_id=?", (domain_id, json.dumps(values, ensure_ascii=False, sort_keys=True), json.dumps(sources, ensure_ascii=False, sort_keys=True), stamp, project_id))
    conn.execute("UPDATE projects SET domain_id=?,domain=?,project_revision=project_revision+1,updated_at=? WHERE id=?", (domain_id, domain["name"], stamp, project_id))
    return {"project_id": project_id, "domain_id": domain_id, "domain": domain["name"], "project_revision": int(project["project_revision"])+1}


def add_domain_resource(conn: sqlite3.Connection, domain_id: str, resource_type: str, title: str, content: str,
                        source_project_id: str = "", *, add_term: bool = True) -> dict[str, Any]:
    if conn.execute("SELECT 1 FROM domains WHERE id=?", (domain_id,)).fetchone() is None:
        raise ProjectServiceError(404, "domain_missing", "领域不存在。")
    if resource_type not in {"term", "corpus", "reference"}:
        raise ProjectServiceError(422, "invalid_domain_resource_type", "领域资源类型无效。")
    stamp = _now(); resource_id = uuid.uuid4().hex
    conn.execute("INSERT INTO domain_resources(id,domain_id,resource_type,title,content,source_project_id,version,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (resource_id,domain_id,resource_type,title,content,source_project_id,1,"active",stamp,stamp))
    if resource_type == "term" and add_term:
        version = _current_domain_version(conn, domain_id)
        conn.execute(
            "INSERT INTO domain_terms(id,domain_id,version,term,definition,aliases,status,source_project_id,source_term_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (uuid.uuid4().hex, domain_id, version, title.strip(), content, "", "active", source_project_id, "", stamp),
        )
    return dict(conn.execute("SELECT * FROM domain_resources WHERE id=?", (resource_id,)).fetchone())


def list_domain_resources(conn: sqlite3.Connection, domain_id: str) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute("SELECT * FROM domain_resources WHERE domain_id=? AND status='active' ORDER BY resource_type,title", (domain_id,))]


def copy_domain_resource(conn: sqlite3.Connection, project_id: str, resource_id: str, base_revision: int) -> dict[str, Any]:
    project = conn.execute("SELECT project_revision,domain_id FROM projects WHERE id=?", (project_id,)).fetchone()
    resource = conn.execute("SELECT * FROM domain_resources WHERE id=? AND status='active'", (resource_id,)).fetchone()
    if project is None or resource is None:
        raise ProjectServiceError(404, "domain_resource_missing", "论文或领域资源不存在。")
    if int(project["project_revision"]) != base_revision:
        raise ProjectServiceError(409, "project_version_conflict", "论文已变化，请刷新后重试。")
    if project["domain_id"] != resource["domain_id"]:
        raise ProjectServiceError(403, "domain_resource_forbidden", "跨领域资源默认不可见。")
    new_id = uuid.uuid4().hex; stamp = _now()
    table = {"term": "terms", "corpus": "corpus", "reference": "refs"}[resource["resource_type"]]
    if table == "terms":
        conn.execute("INSERT INTO terms(id,project_id,term,definition,source_project_id,source_resource_id) VALUES(?,?,?,?,?,?)", (new_id,project_id,resource["title"],resource["content"],resource["source_project_id"],resource_id))
    elif table == "corpus":
        conn.execute("INSERT INTO corpus(id,project_id,title,content,source,source_project_id,source_resource_id) VALUES(?,?,?,?,?,?,?)", (new_id,project_id,resource["title"],resource["content"],"domain",resource["source_project_id"],resource_id))
    else:
        conn.execute("INSERT INTO refs(id,project_id,cite_key,title,bibtex,source_project_id,source_resource_id) VALUES(?,?,?,?,?,?,?)", (new_id,project_id,"domain_"+new_id[:8],resource["title"],resource["content"],resource["source_project_id"],resource_id))
    conn.execute("UPDATE projects SET project_revision=project_revision+1,updated_at=? WHERE id=?", (stamp,project_id))
    return {"project_id": project_id, "resource_id": new_id, "source_domain_id": resource["domain_id"], "source_resource_id": resource_id, "resource_type": resource["resource_type"], "project_revision": int(project["project_revision"])+1}


def _current_domain_version(conn: sqlite3.Connection, domain_id: str) -> int:
    row = conn.execute("SELECT version FROM domains WHERE id=?", (domain_id,)).fetchone()
    if row is None:
        raise ProjectServiceError(404, "domain_missing", "领域不存在。")
    version = int(row["version"] or 1)
    conn.execute(
        "INSERT OR IGNORE INTO domain_versions(domain_id,version,status,batch_id,created_at) VALUES(?,?,?,?,?)",
        (domain_id, version, "active", "", _now()),
    )
    return version


def list_domain_terms(conn: sqlite3.Connection, domain_id: str, version: int | None = None) -> list[dict[str, Any]]:
    current = _current_domain_version(conn, domain_id)
    selected = current if version is None else int(version)
    if not conn.execute("SELECT 1 FROM domain_versions WHERE domain_id=? AND version=?", (domain_id, selected)).fetchone():
        raise ProjectServiceError(404, "domain_version_missing", "领域术语版本不存在。")
    rows = conn.execute(
        "SELECT * FROM domain_terms WHERE domain_id=? AND version=? AND status='active' ORDER BY term,id",
        (domain_id, selected),
    ).fetchall()
    return [dict(row) for row in rows]


def publish_term_version(conn, domain_id, version, batch_id, changes, replaced_ids=()):
    """Publish an immutable domain glossary snapshot within the caller's transaction."""
    new_version = version + 1
    stamp = _now()
    replaced = set(replaced_ids)
    previous = list_domain_terms(conn, domain_id, version)
    conn.execute("UPDATE domain_versions SET status='archived' WHERE domain_id=? AND version=?", (domain_id, version))
    conn.execute("INSERT INTO domain_versions VALUES(?,?,?,?,?)", (domain_id, new_version, "active", batch_id, stamp))
    for term in [*(term for term in previous if term['id'] not in replaced), *changes]:
        conn.execute(
            """INSERT INTO domain_terms(id,domain_id,version,term,definition,aliases,status,source_project_id,source_term_id,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (uuid.uuid4().hex, domain_id, new_version, term['term'], term['definition'], term['aliases'], 'active',
             term.get('source_project_id', ''), term.get('source_term_id', ''), term.get('created_at', stamp)),
        )
    conn.execute("UPDATE domains SET version=?,updated_at=? WHERE id=?", (new_version, stamp, domain_id))
    return new_version


def import_domain_term(conn: sqlite3.Connection, project_id: str, domain_term_id: str, source_domain_version: int, base_revision: int, request_id: str) -> dict[str, Any]:
    project = conn.execute("SELECT project_revision,domain_id FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        raise ProjectServiceError(404, "project_missing", "论文不存在。")
    existing = conn.execute("SELECT * FROM domain_term_imports WHERE request_id=?", (request_id,)).fetchone()
    if existing:
        if existing['project_id'] != project_id or existing['domain_term_id'] != domain_term_id or existing['source_domain_version'] != source_domain_version:
            raise ProjectServiceError(409, 'idempotency_key_reused', '此请求编号已用于不同论文、术语或来源版本。')
        row = conn.execute("SELECT * FROM terms WHERE id=?", (existing["created_term_id"],)).fetchone()
        return {"id": existing["created_term_id"], "term": dict(row) if row else None, "idempotent_replay": True}
    if int(project["project_revision"]) != int(base_revision):
        raise ProjectServiceError(409, "project_version_conflict", "论文已变化，请刷新后重试。")
    domain_term = conn.execute(
        "SELECT * FROM domain_terms WHERE id=? AND domain_id=? AND version=? AND status='active'",
        (domain_term_id, project["domain_id"], int(source_domain_version)),
    ).fetchone()
    if domain_term is None:
        raise ProjectServiceError(409, "domain_term_version_conflict", "领域术语来源版本已变化，请刷新后重试。")
    new_id = uuid.uuid4().hex[:12]
    stamp = _now()
    conn.execute(
        "INSERT INTO terms(id,project_id,term,definition,aliases,version,status,source_domain_id,source_domain_term_id,source_domain_version,source_project_id,source_resource_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (new_id, project_id, domain_term["term"], domain_term["definition"], domain_term["aliases"], 1, "active", project["domain_id"], domain_term_id, int(source_domain_version), "", ""),
    )
    conn.execute(
        "INSERT INTO domain_term_imports(request_id,project_id,domain_term_id,source_domain_version,created_term_id,created_at) VALUES(?,?,?,?,?,?)",
        (request_id, project_id, domain_term_id, int(source_domain_version), new_id, stamp),
    )
    conn.execute("UPDATE projects SET project_revision=project_revision+1,updated_at=? WHERE id=?", (stamp, project_id))
    return {"id": new_id, "term": dict(conn.execute("SELECT * FROM terms WHERE id=?", (new_id,)).fetchone()), "idempotent_replay": False}


def _term_key(term: str) -> str:
    return " ".join(str(term or "").strip().lower().split())


def _promote_rows(conn: sqlite3.Connection, project_id: str, domain_id: str, term_ids: list[str]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    params: list[Any] = [project_id]
    sql = "SELECT * FROM terms WHERE project_id=? AND status='active'"
    if term_ids:
        placeholders = ",".join("?" for _ in term_ids)
        sql += f" AND id IN ({placeholders})"
        params.extend(term_ids)
    source = [dict(row) for row in conn.execute(sql + " ORDER BY term,id", params)]
    version = _current_domain_version(conn, domain_id)
    existing = {
        _term_key(row["term"]): dict(row)
        for row in conn.execute("SELECT * FROM domain_terms WHERE domain_id=? AND version=? AND status='active'", (domain_id, version))
    }
    rows: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for item in source:
        prior = existing.get(_term_key(item["term"]))
        if prior and (prior["definition"] != item["definition"] or prior["aliases"] != item["aliases"]):
            conflicts.append({"term_id": item["id"], "term": item["term"], "reason": "domain_term_conflict", "existing": prior, "incoming": item})
        elif prior:
            rows.append({"term_id": item["id"], "term": item["term"], "outcome": "unchanged", "existing_domain_term_id": prior["id"]})
        else:
            rows.append({"term_id": item["id"], "term": item["term"], "outcome": "new", "incoming": item})
    return rows, conflicts


def preview_term_promotion(conn: sqlite3.Connection, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    import hashlib
    input_fingerprint = hashlib.sha256(json.dumps({k: payload.get(k) for k in ('domain_id', 'term_ids', 'source_project_revision', 'target_domain_version')}, sort_keys=True).encode()).hexdigest()
    domain_id = payload["domain_id"]
    project = conn.execute("SELECT project_revision,domain_id FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        raise ProjectServiceError(404, "project_missing", "论文不存在。")
    if project["domain_id"] != domain_id:
        raise ProjectServiceError(403, "domain_scope_conflict", "只能向论文绑定的领域沉淀术语。")
    if int(project["project_revision"]) != int(payload["source_project_revision"]):
        raise ProjectServiceError(409, "project_version_conflict", "论文已变化，请刷新后重试。")
    replay = conn.execute("SELECT * FROM term_promote_batches WHERE request_id=?", (payload["request_id"],)).fetchone()
    if replay:
        previous = json.loads(replay['preview_json'] or '{}')
        if replay['project_id'] != project_id or replay['domain_id'] != domain_id or (previous.get('input_fingerprint') and previous['input_fingerprint'] != input_fingerprint):
            raise ProjectServiceError(409, 'idempotency_key_reused', '请求编号已用于不同论文或沉淀范围。')
        return _batch_payload(replay)
    version = _current_domain_version(conn, domain_id)
    requested_version = payload.get("target_domain_version")
    if requested_version is not None and int(requested_version) != version:
        raise ProjectServiceError(409, "domain_version_conflict", "领域已更新，请重新生成沉淀预览。")
    rows, conflicts = _promote_rows(conn, project_id, domain_id, payload.get("term_ids") or [])
    batch_id = uuid.uuid4().hex
    preview = {"rows": rows, "conflicts": conflicts, "domain_version": version, "source_project_revision": int(project["project_revision"]), 'input_fingerprint': input_fingerprint}
    stamp = _now()
    conn.execute(
        "INSERT INTO term_promote_batches(id,project_id,domain_id,source_project_revision,target_domain_version,status,request_id,preview_json,results_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (batch_id, project_id, domain_id, int(project["project_revision"]), version, "preview", payload["request_id"], json.dumps(preview, ensure_ascii=False), "{}", stamp, stamp),
    )
    return {"id": batch_id, "status": "preview", **preview}


def _batch_payload(row: sqlite3.Row) -> dict[str, Any]:
    preview = json.loads(row["preview_json"] or "{}")
    result = json.loads(row["results_json"] or "{}")
    return {"id": row["id"], "status": row["status"], **preview, **result}


def commit_term_promotion(conn: sqlite3.Connection, project_id: str, preview_id: str, request_id: str,
                          *, selected_term_ids: list[str] | None = None, conflict_decisions: dict[str, str] | None = None) -> dict[str, Any]:
    import hashlib
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    conflict_decisions = conflict_decisions or {}
    decision_hash = hashlib.sha256(json.dumps({'selected': selected_term_ids, 'conflicts': conflict_decisions}, sort_keys=True).encode()).hexdigest()
    batch = conn.execute("SELECT * FROM term_promote_batches WHERE id=? AND project_id=?", (preview_id, project_id)).fetchone()
    if batch is None:
        raise ProjectServiceError(404, "promotion_preview_missing", "沉淀预览不存在。")
    if batch["status"] == "committed":
        previous = json.loads(batch['results_json'] or '{}')
        if previous.get('decision_fingerprint') and previous['decision_fingerprint'] != decision_hash:
            raise ProjectServiceError(409, 'promotion_decision_conflict', '此批次已按其他选择提交；请核对原结果，不能重复覆盖。')
        return {**_batch_payload(batch), "idempotent_replay": True}
    project = conn.execute("SELECT project_revision,domain_id FROM projects WHERE id=?", (project_id,)).fetchone()
    current_version = _current_domain_version(conn, batch["domain_id"])
    if project["domain_id"] != batch["domain_id"] or int(project["project_revision"]) != int(batch["source_project_revision"]):
        raise ProjectServiceError(409, "promotion_stale", "论文已变化，请重新生成沉淀预览。")
    if current_version != int(batch["target_domain_version"]):
        raise ProjectServiceError(409, "domain_version_conflict", "领域已更新，请重新生成沉淀预览。")
    preview = json.loads(batch["preview_json"] or "{}")
    available = {item['term_id'] for item in preview.get('rows', []) + preview.get('conflicts', [])}
    selected = available if selected_term_ids is None else set(selected_term_ids)
    if not selected <= available or not set(conflict_decisions) <= {i['term_id'] for i in preview.get('conflicts', [])}:
        raise ProjectServiceError(422, 'promotion_selection_invalid', '选择项不属于本次预览。')
    if any(value not in {'keep_domain', 'use_paper'} for value in conflict_decisions.values()):
        raise ProjectServiceError(422, 'promotion_resolution_invalid', '请选择保留领域值或显式使用论文值。')
    imported = [item for item in preview.get("rows", []) if item.get("outcome") == "new" and item['term_id'] in selected]
    conflicts = preview.get("conflicts", [])
    unchanged = [item for item in preview.get("rows", []) if item.get("outcome") == "unchanged"]
    replacements = [i for i in conflicts if i['term_id'] in selected and conflict_decisions.get(i['term_id']) == 'use_paper']
    changed = imported + replacements
    incoming_keys = [_term_key(item['term']) for item in changed]
    if len(set(incoming_keys)) != len(incoming_keys):
        raise ProjectServiceError(409, 'promotion_duplicate_terms', '多条论文术语指向同一领域术语，请每个术语只选择一个值。')
    replacement_keys = {_term_key(item['term']) for item in replacements}
    new_version = current_version
    if changed:
        new_version = current_version + 1
        stamp = _now()
        conn.execute("UPDATE domain_versions SET status='archived' WHERE domain_id=? AND version=?", (batch["domain_id"], current_version))
        conn.execute("INSERT INTO domain_versions(domain_id,version,status,batch_id,created_at) VALUES(?,?,?,?,?)", (batch["domain_id"], new_version, "active", batch["id"], stamp))
        for prior in conn.execute("SELECT * FROM domain_terms WHERE domain_id=? AND version=? AND status='active'", (batch["domain_id"], current_version)).fetchall():
            if _term_key(prior['term']) in replacement_keys:
                continue
            conn.execute("INSERT INTO domain_terms(id,domain_id,version,term,definition,aliases,status,source_project_id,source_term_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, batch["domain_id"], new_version, prior["term"], prior["definition"], prior["aliases"], prior["status"], prior["source_project_id"], prior["source_term_id"], prior["created_at"]))
        for item in changed:
            term = conn.execute("SELECT * FROM terms WHERE id=? AND project_id=?", (item["term_id"], project_id)).fetchone()
            if term is None:
                continue
            conn.execute("INSERT INTO domain_terms(id,domain_id,version,term,definition,aliases,status,source_project_id,source_term_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (uuid.uuid4().hex, batch["domain_id"], new_version, term["term"], term["definition"], term["aliases"], "active", project_id, term["id"], _now()))
        conn.execute("UPDATE domains SET version=?,updated_at=? WHERE id=?", (new_version, stamp, batch["domain_id"]))
    results = {"imported": imported, "replaced": replacements, "unchanged": unchanged, "conflicts": conflicts,
               "domain_version": new_version, "decision_fingerprint": decision_hash,
               "selected_term_ids": sorted(selected), "conflict_decisions": conflict_decisions}
    stamp = _now()
    conn.execute("UPDATE term_promote_batches SET status='committed',results_json=?,updated_at=? WHERE id=?", (json.dumps(results, ensure_ascii=False), stamp, batch["id"]))
    return {"id": batch["id"], "status": "committed", **results, "idempotent_replay": False}


def create_classification_review(conn: sqlite3.Connection, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    project = conn.execute("SELECT domain_id FROM projects WHERE id=?", (project_id,)).fetchone()
    if project is None:
        raise ProjectServiceError(404, "project_missing", "论文不存在。")
    if project["domain_id"] != payload["domain_id"]:
        raise ProjectServiceError(403, "domain_scope_conflict", "分类复核只能使用论文绑定的领域。")
    term = conn.execute("SELECT id FROM terms WHERE id=? AND project_id=? AND status='active'", (payload["term_id"], project_id)).fetchone()
    if term is None:
        raise ProjectServiceError(404, "term_missing", "论文术语不存在。")
    existing = conn.execute("SELECT * FROM terminology_reviews WHERE request_id=?", (payload["request_id"],)).fetchone()
    if existing:
        if existing['project_id'] != project_id or existing['term_id'] != payload['term_id'] or existing['domain_id'] != payload['domain_id']:
            raise ProjectServiceError(409, 'idempotency_key_reused', '请求编号已用于另一条术语复核。')
        return dict(existing) | {"idempotent_replay": True}
    review_id = uuid.uuid4().hex; stamp = _now()
    conn.execute("INSERT INTO terminology_reviews(id,project_id,term_id,domain_id,suggested_term,suggested_definition,suggested_aliases,confidence,basis,classifier_version,status,decision_json,request_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (review_id, project_id, payload["term_id"], payload["domain_id"], payload["suggested_term"], payload.get("suggested_definition", ""), payload.get("suggested_aliases", ""), float(payload["confidence"]), payload.get("basis", ""), payload.get("classifier_version", "manual-or-local"), "pending_manual", "", payload["request_id"], stamp, stamp))
    if payload.get('input_fingerprint'):
        conn.execute('UPDATE terminology_reviews SET input_fingerprint=? WHERE id=?', (payload['input_fingerprint'], review_id))
    return dict(conn.execute("SELECT * FROM terminology_reviews WHERE id=?", (review_id,)).fetchone()) | {"idempotent_replay": False}


def list_classification_reviews(conn: sqlite3.Connection, project_id: str, status: str | None = None) -> list[dict[str, Any]]:
    if status:
        rows = conn.execute("SELECT * FROM terminology_reviews WHERE project_id=? AND status=? ORDER BY created_at DESC", (project_id, status)).fetchall()
    else:
        rows = conn.execute("SELECT * FROM terminology_reviews WHERE project_id=? ORDER BY created_at DESC", (project_id,)).fetchall()
    return [dict(row) for row in rows]


def decide_classification_review(conn: sqlite3.Connection, project_id: str, review_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')
    review = conn.execute("SELECT * FROM terminology_reviews WHERE id=? AND project_id=?", (review_id, project_id)).fetchone()
    if review is None:
        raise ProjectServiceError(404, "review_missing", "分类复核不存在。")
    if review["status"] != "pending_manual":
        return dict(review) | {"idempotent_replay": True}
    decision = payload["decision"]
    values = {"term": payload.get("term") or review["suggested_term"], "definition": payload.get("definition") if payload.get("definition") is not None else review["suggested_definition"], "aliases": payload.get("aliases") if payload.get("aliases") is not None else review["suggested_aliases"]}
    if decision == "reject":
        status = "rejected"
        result = {"status": status, "decision": decision}
    else:
        version = _current_domain_version(conn, review["domain_id"])
        prior = conn.execute("SELECT * FROM domain_terms WHERE domain_id=? AND version=? AND lower(term)=lower(?) AND status='active'", (review["domain_id"], version, values["term"])).fetchone()
        replacing = bool(prior and (prior['definition'] != values['definition'] or prior['aliases'] != values['aliases']))
        if replacing and not payload.get('replace_conflict'):
            raise ProjectServiceError(409, "domain_term_conflict", "人工确认的术语与领域现有值冲突，请先处理冲突。")
        if replacing and payload.get('expected_domain_version') != version:
            raise ProjectServiceError(409, 'domain_version_conflict', '领域版本已变化，请刷新并核对要替换的原值。')
        changes = [values | {'source_project_id': project_id, 'source_term_id': review['term_id']}] if not prior or replacing else []
        new_version = publish_term_version(conn, review['domain_id'], version, 'review:' + review_id, changes,
                                           [prior['id']] if replacing else [])
        status = "accepted"
        result = {"status": status, "decision": decision, "domain_version": new_version, "published": not bool(prior) or replacing}
    decision_json = json.dumps({"request_id": payload["request_id"], **result, "values": values}, ensure_ascii=False)
    conn.execute("UPDATE terminology_reviews SET status=?,decision_json=?,updated_at=? WHERE id=?", (status, decision_json, _now(), review_id))
    return dict(conn.execute("SELECT * FROM terminology_reviews WHERE id=?", (review_id,)).fetchone()) | {"idempotent_replay": False}
