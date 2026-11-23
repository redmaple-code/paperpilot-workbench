"""Host-owned Traceify routes. Model calls use the durable Agent runtime."""
from contextlib import contextmanager
import json
import sqlite3
from fastapi import APIRouter, BackgroundTasks, HTTPException, Body, Request

from paperpilot.domain.structure_schemas import WorkspaceCreateIn, WorkspaceSaveIn, LayoutIn
from paperpilot.domain.agent_conversation_schemas import ConversationPreviewIn, ConversationRunIn
from paperpilot.services import structure_workspaces as store, structure_graph as graph
from paperpilot.services import structure_integration, agent_actions, agent_runtime
from paperpilot.services.project_errors import ProjectServiceError


def create_router(database, project_read, project_write, call_ai, adapters):
    router = APIRouter(prefix="/api/projects/{project_id}/structure-workspaces", tags=["Traceify structure"])

    @contextmanager
    def connection(pid, writable=False):
        (project_write if writable else project_read)(pid)
        conn = database()
        try:
            with conn:
                if writable:
                    conn.execute("BEGIN IMMEDIATE")
                yield conn
        except ProjectServiceError as exc:
            raise HTTPException(exc.status_code, detail={"code": exc.code, "message": exc.message,
                                "retryable": exc.status_code in {409, 503}}) from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(422, detail={"code": "invalid_input", "message": "结构数据格式无效，请核对字段和来源。"}) from exc
        except sqlite3.Error as exc:
            raise HTTPException(503, detail={"code": "storage_or_provider_unavailable", "message": "结构暂时无法保存，输入已保留。"}) from exc
        finally:
            conn.close()

    @router.get("")
    def list_workspaces(project_id: str):
        with connection(project_id) as conn:
            return {"workspaces": [{"id": r["id"], "entry_mode": r["entry_mode"], "revision": r["revision"],
                    "title": json.loads(r["workspace_json"])["source"]["title"], "updated_at": r["updated_at"]}
                    for r in conn.execute("SELECT * FROM structure_workspaces WHERE project_id=? ORDER BY updated_at DESC", (project_id,))],
                    "imported_manuscripts": store.imported_manuscripts(conn, project_id)}

    @router.post("/from-manuscript/{batch_id}")
    def from_manuscript(project_id: str, batch_id: str):
        with connection(project_id, True) as conn:
            return store.create_from_manuscript(conn, project_id, batch_id)

    def import_content(content, content_type, locator):
        from paperpilot.services import imports
        warnings = []
        if content.startswith(b'%PDF-') or 'pdf' in content_type:
            try:
                text, extracted = imports._pdf_text(content)
            except imports.ImportError as exc:
                raise HTTPException(422, detail={"code": exc.code, "message": exc.message}) from exc
            pages = [{"text": text, "origin_locator": {"kind": "paper_page", "page": index + 1, "locator": locator}} for index, text in enumerate(extracted) if text]
            warnings = [f"第{index + 1}页未提取到文本" for index, text in enumerate(extracted) if not text]
            text = "\n\n".join(page['text'] for page in pages)
        elif 'html' in content_type:
            text, _ = imports._html_text(content)
            pages = []
        else:
            try:
                text = content.decode('utf-8-sig')
            except UnicodeDecodeError:
                try:
                    text = content.decode('gb18030')
                except UnicodeDecodeError as exc:
                    raise HTTPException(422, detail={"message": "文本编码无法识别，请转为UTF-8后导入。"}) from exc
            pages = []
        if not text.strip() or len(text) > 1000000:
            raise HTTPException(422, detail={"message": "没有提取到文本，或文本超过100万字。扫描PDF请先提供可提取文本。"})
        return {"content": text, "pages": pages, "locator": locator, "status": "partial" if warnings else "complete", "warnings": warnings}

    @router.post('/import-file/preview')
    async def preview_file(project_id: str, request: Request, filename: str = '导入论文'):
        project_read(project_id)
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > 20 * 1024 * 1024:
                raise HTTPException(413, detail={"message": "文件超过20MB。"})
            chunks.append(chunk)
        return import_content(b''.join(chunks), request.headers.get('content-type', ''), filename[:500])

    @router.post('/import-url/preview')
    def preview_url(project_id: str, payload: dict = Body(...)):
        from paperpilot.services import imports
        project_read(project_id)
        url = payload.get('url', '')
        if not isinstance(url, str):
            raise HTTPException(422, detail={"message": "请输入公开论文地址。"})
        try:
            content, content_type = imports._public_request(url, timeout=45, max_bytes=imports.MAX_FETCH_BYTES, accept='application/pdf,text/html,text/plain')
        except imports.ImportError as exc:
            raise HTTPException(422, detail={"code": exc.code, "message": exc.message}) from exc
        return import_content(content, content_type, url)

    @router.post("")
    def create(project_id: str, payload: WorkspaceCreateIn):
        with connection(project_id, True) as conn:
            return store.create(conn, project_id, payload.model_dump())

    @router.get("/{workspace_id}")
    def read(project_id: str, workspace_id: str):
        with connection(project_id) as conn:
            return store.read(conn, project_id, workspace_id)

    @router.get("/{workspace_id}/context")
    def context(project_id: str, workspace_id: str):
        with connection(project_id) as conn:
            value = store.read(conn, project_id, workspace_id)
            return {"source": value["workspace"]["source"], **value["host"]}

    @router.put("/{workspace_id}")
    def write(project_id: str, workspace_id: str, payload: WorkspaceSaveIn):
        with connection(project_id, True) as conn:
            return store.write(conn, project_id, workspace_id, payload.model_dump())

    @router.get("/{workspace_id}/history")
    def history(project_id: str, workspace_id: str):
        with connection(project_id) as conn:
            store.row(conn, project_id, workspace_id)
            return {"versions": [dict(r) for r in conn.execute("SELECT revision,operation,created_at FROM structure_workspace_versions WHERE project_id=? AND workspace_id=? ORDER BY revision DESC LIMIT 100", (project_id, workspace_id))]}

    @router.post("/{workspace_id}/restore")
    def restore(project_id: str, workspace_id: str, payload: dict = Body(...)):
        with connection(project_id, True) as conn:
            return store.write(conn, project_id, workspace_id, payload, "restore")

    @router.get("/{workspace_id}/layout")
    def layout(project_id: str, workspace_id: str, graph_kind: str = "structure", graph_branch: str = "main"):
        with connection(project_id) as conn:
            store.row(conn, project_id, workspace_id)
            return store.read_layout(conn, project_id, workspace_id, {"graph_kind": graph_kind, "graph_branch": graph_branch})

    @router.put("/{workspace_id}/layout")
    def save_layout(project_id: str, workspace_id: str, payload: LayoutIn):
        with connection(project_id, True) as conn:
            return store.save_layout(conn, project_id, workspace_id, payload.model_dump())

    @router.get("/{workspace_id}/settings")
    def settings(project_id: str, workspace_id: str):
        from paperpilot.services import provider_runtime
        with connection(project_id) as conn:
            store.row(conn, project_id, workspace_id)
            project = conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            admission = provider_runtime.preview_admission(conn, project_id, project["ai_model_id"] or project["model"], 2000, 2000)
            return {"configured": admission["generation_allowed"], "model": (admission.get("provider") or {}).get("model", ""), "has_key": bool(admission.get("provider")), "base_url": "宿主模型配置"}

    @router.post("/{workspace_id}/generation/preview")
    def preview_generation(project_id: str, workspace_id: str, payload: ConversationPreviewIn):
        with connection(project_id) as conn:
            data = payload.model_dump() | {"structure_workspace_id": workspace_id, "stage_id": "STAGE-02"}
            return {k: v for k, v in agent_runtime.preview(conn, project_id, data).items() if not k.startswith("_")}

    @router.post("/{workspace_id}/generations")
    def generate(project_id: str, workspace_id: str, payload: ConversationRunIn, tasks: BackgroundTasks):
        # The runtime owns the turn transaction and commits before scheduling.
        with connection(project_id) as conn:
            project_write(project_id)
            data = payload.model_dump() | {"structure_workspace_id": workspace_id, "stage_id": "STAGE-02"}
            result = agent_runtime.start(conn, project_id, data)
            if result["status"] == "queued":
                tasks.add_task(agent_runtime.schedule, conn.execute("PRAGMA database_list").fetchone()["file"], result["id"], call_ai)
            return result

    @router.get("/{workspace_id}/generations/{run_id}")
    def generation(project_id: str, workspace_id: str, run_id: str):
        with connection(project_id) as conn:
            store.row(conn, project_id, workspace_id)
            run = conn.execute("SELECT * FROM agent_runs WHERE id=? AND project_id=?", (run_id, project_id)).fetchone()
            if run is None or json.loads(run["input_json"]).get("structure_workspace_id") != workspace_id:
                graph.fail("运行不属于当前工作区。", "resource_not_found", 404)
            drafts = conn.execute("SELECT id,revision,state FROM structure_drafts WHERE project_id=? AND workspace_id=? AND run_id=?", (project_id, workspace_id, run_id)).fetchall()
            return {"run": agent_runtime._public_run(run), "drafts": [dict(r) for r in drafts]}

    @router.post("/{workspace_id}/generations/{run_id}/cancel")
    def cancel(project_id: str, workspace_id: str, run_id: str):
        generation(project_id, workspace_id, run_id)
        project_write(project_id)
        with connection(project_id) as conn:
            return agent_runtime.cancel(conn, project_id, run_id)

    @router.get("/{workspace_id}/drafts/{draft_id}")
    def draft(project_id: str, workspace_id: str, draft_id: str):
        with connection(project_id) as conn:
            return store.read_draft(conn, project_id, workspace_id, draft_id)

    @router.put("/{workspace_id}/drafts/{draft_id}")
    def save_draft(project_id: str, workspace_id: str, draft_id: str, payload: dict = Body(...)):
        from paperpilot.services import innovation_points
        with connection(project_id, True) as conn:
            digest, prior = store.replay(conn, project_id, workspace_id, "draft_edit", payload)
            if prior is not None:
                return prior
            current = store.read_draft(conn, project_id, workspace_id, draft_id)
            if current["version"] != payload.get("expected_draft_revision") or current["state"] not in {"partial", "ready"}:
                graph.fail("草稿已变化。", "workspace_stale", 409)
            value = graph.apply_commands(current["workspace"], [{"op": "replace_graph", "workspace": payload["workspace"]}])
            conn.execute("UPDATE structure_drafts SET revision=revision+1,workspace_json=?,updated_at=? WHERE id=?", (graph.compact(value), store.now(), draft_id))
            innovation_points.record_usage(conn, project_id, current.get("innovation_refs", []), "structure_draft",
                                           draft_id, current["version"] + 1, "Traceify 大纲草稿")
            return store.receipt(conn, project_id, workspace_id, "draft_edit", payload, digest, store.read_draft(conn, project_id, workspace_id, draft_id))

    @router.post("/{workspace_id}/drafts/{draft_id}/accept")
    def accept_draft(project_id: str, workspace_id: str, draft_id: str, payload: dict = Body(...)):
        with connection(project_id, True) as conn:
            return store.write(conn, project_id, workspace_id, payload | {"draft_id": draft_id}, "accept_draft")

    @router.post("/{workspace_id}/outline-proposal")
    def outline_proposal(project_id: str, workspace_id: str, payload: dict = Body(...)):
        with connection(project_id, True) as conn:
            data = structure_integration.proposal_payload(conn, project_id, workspace_id, payload.get("draft_id"), payload.get("replace_outline", False))
            return agent_actions.create(conn, project_id, {"request_id": payload["request_id"], "operation": "structure_outline", "base_version": data["outline_version"],
                "payload": data, "source_refs": [], "conversation_id": payload.get("conversation_id", ""), "run_id": ""}, adapters)

    @router.post("/{workspace_id}/conflicts/preview")
    def conflict_preview(project_id: str, workspace_id: str, payload: dict = Body(...)):
        with connection(project_id) as conn:
            remote = store.read(conn, project_id, workspace_id)
            baseline = conn.execute("SELECT workspace_json FROM structure_workspace_versions WHERE project_id=? AND workspace_id=? AND revision=?", (project_id, workspace_id, payload.get("base_revision"))).fetchone()
            if baseline is None:
                graph.fail("冲突基线不存在。")
            return {"base": json.loads(baseline[0]), "remote": remote, "local": payload["workspace"]}

    @router.post("/{workspace_id}/conflicts/resolve")
    def conflict_resolve(project_id: str, workspace_id: str, payload: WorkspaceSaveIn):
        with connection(project_id, True) as conn:
            return store.write(conn, project_id, workspace_id, payload.model_dump(), "conflict_resolve")

    return router
