"""06 wiring for existing search/OA services, with transient unselected results."""
from __future__ import annotations
import threading
import uuid
from fastapi import HTTPException
from paperpilot.domain.schemas import AcademicSearchIn, AcademicHandoffIn, ImportIn
from paperpilot.services import agent_actions, agent_business_tools, agent_domain_adapters
from paperpilot.services.search_sessions import SearchSessionError
from paperpilot.services.project_errors import ProjectServiceError


def configure(app_module):
    a = app_module
    lookups = {}
    owners = {}
    tokens = {}
    lock = threading.RLock()

    def candidate(pid, payload):
        if set(payload) - {"search_id", "candidate_id"}:
            raise ProjectServiceError(422, "invalid_input", "文献交接只能引用当前检索候选，不能执行任意URL。")
        sid = payload.get("search_id")
        with lock:
            token = lookups.get((str(a.DB_PATH), pid, sid))
        if not token:
            raise ProjectServiceError(409, "search_expired", "检索会话已过期；原记录保留，请重新检索。")
        try:
            snapshot = a._search_session_store.get(a._search_session_key(token), pid, sid)
        except SearchSessionError as exc:
            raise ProjectServiceError(409, exc.code, "检索会话已失效，请重新检索。") from exc
        item = next((c for c in snapshot["candidates"] if c["id"] == payload.get("candidate_id")), None)
        if item is None:
            raise ProjectServiceError(404, "resource_not_found", "所选稳定候选编号不存在，未改选其他结果。")
        return snapshot, item, token

    def search(pid, query, conversation_id=""):
        if conversation_id:
            conn = a.db()
            try:
                exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_conversations'").fetchone()
                owner = conn.execute("SELECT project_id FROM agent_conversations WHERE id=?", (conversation_id,)).fetchone() if exists else None
                if not owner or owner[0] != pid:
                    raise HTTPException(404, detail={"code": "resource_not_found", "message": "检索会话不属于当前论文。"})
            finally:
                conn.close()
        with lock:
            key = (str(a.DB_PATH), pid, conversation_id)
            token = tokens.setdefault(key, uuid.uuid4().hex)
            while len(tokens) > 32:
                tokens.pop(next(iter(tokens)))
        response = a.session_academic_search(pid, AcademicSearchIn(query=query, request_id=uuid.uuid4().hex), token)
        sid = response.get("search", {}).get("id") or response.get("search_id")
        if sid:
            with lock:
                lookups[(str(a.DB_PATH), pid, sid)] = token
                owners[(str(a.DB_PATH), pid, sid)] = conversation_id
                while len(lookups) > 100:
                    old = next(iter(lookups));lookups.pop(old);owners.pop(old, None)
        return {**response, "cards": cards(pid, only=sid) if sid else []}

    def cards(pid, only=None, conversation_id=None):
        output = []
        with lock:
            entries = list(lookups.items())
        for (db_path, owner, sid), token in entries:
            if owner != pid or db_path != str(a.DB_PATH) or (only and only != sid):
                continue
            cid = owners.get((db_path, owner, sid), "")
            if conversation_id is not None and cid != conversation_id:
                continue
            try:
                snapshot = a._search_session_store.get(a._search_session_key(token), pid, sid)
            except SearchSessionError:
                with lock:
                    lookups.pop((db_path, owner, sid), None)
                    owners.pop((db_path, owner, sid), None)
                continue
            for item in snapshot["candidates"]:
                output.append({"schema_version": 1, "kind": "search_result", "id": sid+":"+item["id"], "project_id": pid,
                               "conversation_id": cid, "run_id": "", "status": "proposed", "title": item.get("title", ""),
                               "summary": item.get("abstract", "")[:400], "source_refs": [{"provider": item.get("provider"), "query": snapshot["query"], "retrieved_at": snapshot["created_at"]}],
                               "target": {"kind": "reference"}, "base_version": None, "limitations": ["尚未保存；元数据不是全文证据。"],
                               "metadata": {k: item.get(k, "") for k in ("authors", "year", "doi", "provider", "access_status", "oa_url")},
                               "payload": {"search_id": sid, "candidate_id": item["id"]}, "actions": ["reference", "oa"]})
        return output

    def search_preview(kind, conn, pid, payload, version, selection):
        agent_domain_adapters.check_version(conn.execute("SELECT project_revision FROM projects WHERE id=?", (pid,)).fetchone()[0], version)
        _, item, _ = candidate(pid, payload)
        if kind == "oa" and not item.get("oa_url"):
            raise ProjectServiceError(422, "oa_unavailable", "此条目没有已知开放全文；可先加入参考文献草稿。")
        return {"candidate_id": item["id"], "title": item.get("title"), "doi": item.get("doi"), "access_status": item.get("access_status"),
                "effect": "创建公开全文导入请求；仍需获取、预览和确认，尚未入库。" if kind == "oa" else "加入参考文献草稿；仍需核对后确认，不作为已核验证据。"}

    def search_apply(kind, conn, pid, payload, version, selection, request_id):
        _, item, token = candidate(pid, payload)
        with a._search_session_store.locked_snapshot(a._search_session_key(token), pid, payload["search_id"]) as snapshot:
            result = apply_snapshot(kind, conn, pid, payload, request_id, snapshot, item)
            stamp = conn.execute("SELECT updated_at FROM academic_searches WHERE id=? AND project_id=?", (payload["search_id"], pid)).fetchone()[0]
            return result | {"search_id": payload["search_id"], "durable_version": stamp}

    def apply_snapshot(kind, conn, pid, payload, request_id, snapshot, item):
        # Promote only the explicitly selected candidate, inside the action transaction.
        a._promote_session_candidate(conn, pid, payload["search_id"], item["id"], snapshot, status="candidate")
        if kind == "reference":
            return a._handoff_academic_candidate(conn, pid, AcademicHandoffIn(candidate_id=item["id"], idempotency_key=pid+":"+item["id"]), summary_override=snapshot.get("summary"))
        request = ImportIn(candidate_id=item["id"], idempotency_key=pid+":"+item["id"], defer_fetch=True)
        fp = a._oa_request_fingerprint(pid, request)
        prior = conn.execute("SELECT * FROM oa_imports WHERE idempotency_key=?", (request.idempotency_key,)).fetchone()
        a._oa_check_existing(prior, pid, request, fp)
        if prior:
            import_id = prior["id"]
        else:
            import_id = a._oa_write_import_record(conn, pid, request, item, request.idempotency_key, fp, None)
        return {"import_id": import_id, "status": "created", "message": "已创建全文导入请求；请在文献导入中获取、预览并确认，尚未入库。"}

    def after_commit(response):
        if response.get("idempotent_replay"):
            return
        pid = response["event"]["project_id"]
        sid = response["result"]["search_id"]
        with lock:
            token = lookups.get((str(a.DB_PATH), pid, sid))
        if token:
            try:
                a._search_session_store.mark_durable_version(a._search_session_key(token), pid, sid, response["result"]["durable_version"])
            except SearchSessionError:
                response["warning"] = "临时检索会话已失效；已提交的交接结果仍可在文献页面查看。"

    adapters = agent_domain_adapters.adapters()
    def validate_scope(pid, payload, cid):
        candidate(pid, payload)
        if owners.get((str(a.DB_PATH), pid, payload["search_id"]), "") != cid:
            raise ProjectServiceError(409, "target_changed", "检索候选属于另一会话，请回到原会话后交接。")
    for kind in ["reference", "oa"]:
        adapters[kind] = {"preview": lambda *args, kind=kind: search_preview(kind, *args), "apply": lambda *args, kind=kind: search_apply(kind, *args), "after_commit": after_commit, "validate_scope": validate_scope}
    original_logic = adapters["logic"]["apply"]

    def apply_logic(conn, pid, payload, version, selection, request_id):
        result = original_logic(conn, pid, payload, version, selection, request_id)
        a._sync_legacy_logic_projection(conn, pid, result["nodes"], result["edges"])
        return result
    adapters["logic"]["apply"] = apply_logic
    agent_business_tools.configure(adapters=adapters, search=search, search_cards=cards)
    agent_business_tools.install_legacy_bridge()
    return adapters, search, cards
