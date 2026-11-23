from __future__ import annotations

import json
import uuid

import app
from tests.provider_fixtures import logic_preview_payload
from tests.planning_helper import complete_planning


def create_project(client, name: str = "需求验收论文") -> str:
    response = client.post("/api/projects", json={"name": name, "domain": "材料科学"})
    assert response.status_code == 200, response.text
    return response.json()["project"]["id"]


def enter_revision(client, project_id: str) -> None:
    complete_planning(client, project_id)
    workflow = client.get(f"/api/projects/{project_id}/workflow").json()
    started = client.post(
        f"/api/projects/{project_id}/workflow/commands",
        json={"command": "start", "request_id": f"start-{uuid.uuid4().hex}", "base_version": workflow["version"]},
    )
    assert started.status_code == 200, started.text
    entered = client.post(
        f"/api/projects/{project_id}/human-revision",
        json={"workflow_version": started.json()["version"]},
    )
    assert entered.status_code == 200, entered.text


def test_logic_graph_projects_gaps_versions_and_selected_ai_changes(client, monkeypatch):
    project_id = create_project(client)
    claim = {"id": "claim-1", "kind": "claim", "label": "核心论点", "details": ""}
    evidence = {"id": "evidence-1", "kind": "evidence", "label": "实验结果", "details": ""}
    first = client.put(
        f"/api/projects/{project_id}/logic/graph",
        json={"request_id": "graph-v1", "base_version": 0, "nodes": [claim], "edges": []},
    )
    assert first.status_code == 200, first.text
    assert first.json()["version"] == 1
    assert [gap["node_id"] for gap in first.json()["gaps"]] == ["claim-1"]

    linked = client.put(
        f"/api/projects/{project_id}/logic/graph",
        json={
            "request_id": "graph-v2", "base_version": 1,
            "nodes": [claim, evidence],
            "edges": [{"id": "edge-1", "from_node_id": "evidence-1", "to_node_id": "claim-1", "edge_type": "supports"}],
        },
    )
    assert linked.status_code == 200, linked.text
    assert linked.json()["gaps"] == []
    assert client.get(f"/api/projects/{project_id}/logic/graph?version=0").json()["nodes"] == []
    legacy_rows = client.get(f"/api/projects/{project_id}/logic").json()
    assert {row["id"] for row in legacy_rows} == {"claim-1", "evidence-1"}

    cycle = client.put(
        f"/api/projects/{project_id}/logic/graph",
        json={
            "request_id": "graph-cycle", "base_version": 2,
            "nodes": [claim, evidence],
            "edges": [
                {"id": "edge-a", "from_node_id": "claim-1", "to_node_id": "evidence-1", "edge_type": "supports"},
                {"id": "edge-b", "from_node_id": "evidence-1", "to_node_id": "claim-1", "edge_type": "supports"},
            ],
        },
    )
    assert cycle.status_code == 422
    stale = client.put(
        f"/api/projects/{project_id}/logic/graph",
        json={"request_id": "graph-stale", "base_version": 1, "nodes": [claim], "edges": []},
    )
    assert stale.status_code == 409
    assert client.get(f"/api/projects/{project_id}/logic/graph").json()["version"] == 2

    operations = [
        {"op": "add_node", "node": {"id": "evidence-2", "kind": "evidence", "label": "复现实验"}},
        {"op": "add_edge", "edge": {"id": "edge-2", "from_node_id": "evidence-2", "to_node_id": "claim-1", "edge_type": "supports"}},
    ]
    monkeypatch.setattr(app, "call_ai", lambda *_args, **_kwargs: {"ok": True, "content": json.dumps({"operations": operations})})
    suggestion = client.post(
        f"/api/projects/{project_id}/logic/suggestions",
        json=logic_preview_payload(client,project_id,{"request_id": "suggestion-partial", "prompt": "补充一条证据"}),
    )
    assert suggestion.status_code == 200, suggestion.text
    body = suggestion.json()
    reused = client.post(
        f"/api/projects/{project_id}/logic/suggestions",
        json={"request_id": "suggestion-partial", "prompt": "不同的建议请求"},
    )
    assert reused.status_code == 409
    assert reused.json()["detail"]["code"] == "idempotency_key_reused"
    applied = client.post(
        f"/api/projects/{project_id}/logic/suggestions/{body['id']}/decision",
        json={"request_id": "decision-partial", "decision": "partial_apply", "selected_indices": [0], "base_version": 2},
    )
    assert applied.status_code == 200, applied.text
    graph = client.get(f"/api/projects/{project_id}/logic/graph").json()
    assert graph["version"] == 3
    assert "evidence-2" in {node["id"] for node in graph["nodes"]}
    assert "edge-2" not in {edge["id"] for edge in graph["edges"]}

    rejected_operations = [
        {"op": "add_node", "node": {"id": "evidence-3", "kind": "evidence", "label": "第三方复核"}},
    ]
    monkeypatch.setattr(app, "call_ai", lambda *_args, **_kwargs: {"ok": True, "content": json.dumps({"operations": rejected_operations})})
    rejected_suggestion = client.post(
        f"/api/projects/{project_id}/logic/suggestions",
        json=logic_preview_payload(client,project_id,{"request_id": "suggestion-reject", "prompt": "再检查一次"}),
    ).json()
    rejected = client.post(
        f"/api/projects/{project_id}/logic/suggestions/{rejected_suggestion['id']}/decision",
        json={"request_id": "decision-reject", "decision": "reject", "base_version": 3},
    )
    assert rejected.status_code == 200
    assert client.get(f"/api/projects/{project_id}/logic/graph").json()["version"] == 3


def test_release_gate_requires_current_compile_and_resolved_citations(client, monkeypatch):
    project_id = create_project(client)
    project = client.get(f"/api/projects/{project_id}").json()["project"]
    submitted = client.post(
        f"/api/projects/{project_id}/lifecycle",
        json={"command": "submit_review", "request_id": "submit-review", "base_version": project["project_revision"]},
    )
    assert submitted.status_code == 200, submitted.text

    no_check = client.post(
        f"/api/projects/{project_id}/lifecycle",
        json={"command": "publish", "request_id": "publish-no-check", "base_version": project["project_revision"]},
    )
    assert no_check.status_code == 409
    assert no_check.json()["detail"]["code"] == "release_check_required"

    conn = app.db()
    main = conn.execute("SELECT content FROM files WHERE project_id=? AND path='main.tex'", (project_id,)).fetchone()["content"]
    conn.execute("UPDATE files SET content=? WHERE project_id=? AND path='main.tex'", (main + "\\cite{missing-source}\n", project_id))
    app.touch_project(conn, project_id)
    conn.commit()
    conn.close()

    def successful_compile(files, project_dir, clean_path, work_dir, **kwargs):
        from pathlib import Path
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        (Path(work_dir) / "main.pdf").write_bytes(b"mock-pdf-artifact")
        return {"ok": True, "log": "", "pdf_url": None}
    monkeypatch.setattr(app, "compile_project_files", successful_compile)
    checked = client.post(f"/api/projects/{project_id}/compile", json={"purpose": "submitted"})
    assert checked.status_code == 200, checked.text
    release = client.get(f"/api/projects/{project_id}/lifecycle").json()["release_check"]
    assert release["status"] == "blocked"
    assert release["missing_citations"] == ["missing-source"]
    project = client.get(f"/api/projects/{project_id}").json()["project"]
    blocked = client.post(
        f"/api/projects/{project_id}/lifecycle",
        json={"command": "publish", "request_id": "publish-missing-citation", "base_version": project["project_revision"]},
    )
    assert blocked.status_code == 409
    assert blocked.json()["detail"]["code"] == "unresolved_citations"

    added_ref = client.post(f"/api/projects/{project_id}/refs", json={"cite_key": "missing-source", "title": "已确认来源"})
    assert added_ref.status_code == 200
    current_project = client.get(f"/api/projects/{project_id}").json()["project"]
    stale_publish = client.post(
        f"/api/projects/{project_id}/lifecycle",
        json={"command": "publish", "request_id": "publish-stale-check", "base_version": current_project["project_revision"]},
    )
    assert stale_publish.status_code == 409
    assert stale_publish.json()["detail"]["code"] == "release_check_required"
    # A library entry alone is not a compilable BibTeX entry. Explicitly write
    # the confirmed citation into this paper's bibliography before publishing.
    complete_planning(client, project_id)
    workflow = client.get(f"/api/projects/{project_id}/workflow").json()
    started = client.post(f"/api/projects/{project_id}/workflow/commands", json={
        "command": "start", "request_id": "bibliography-start", "base_version": workflow["version"],
    }).json()
    entered = client.post(f"/api/projects/{project_id}/human-revision", json={"workflow_version": started["version"]})
    assert entered.status_code == 200, entered.text
    bib = next(item for item in client.get(f"/api/projects/{project_id}").json()["files"] if item["path"] == "references.bib")
    saved_bib = client.put(f"/api/projects/{project_id}/editor/save", json={
        "path": "references.bib", "content": bib["content"] + "\n@article{missing-source,title={Confirmed source},year={2024}}\n",
        "base_version": bib["version"], "request_id": "confirm-source-bibliography",
    })
    assert saved_bib.status_code == 200, saved_bib.text
    checked = client.post(f"/api/projects/{project_id}/compile", json={"purpose": "submitted"})
    assert client.get(f"/api/projects/{project_id}/lifecycle").json()["release_check"]["status"] == "passed"
    current_project = client.get(f"/api/projects/{project_id}").json()["project"]
    published = client.post(
        f"/api/projects/{project_id}/lifecycle",
        json={"command": "publish", "request_id": "publish-after-check", "base_version": current_project["project_revision"]},
    )
    assert published.status_code == 200, published.text
    assert published.json()["lifecycle_state"] == "published"
    assert client.put(f"/api/projects/{project_id}", json={"domain": "禁止修改"}).status_code == 409
    assert client.put(
        f"/api/projects/{project_id}/files",
        json={"path": "main.tex", "content": "tamper", "base_version": 1},
    ).status_code == 409
    assert client.post(
        f"/api/projects/{project_id}/terms", json={"term": "locked", "definition": "must reject"}
    ).status_code == 409
    replay = client.post(
        f"/api/projects/{project_id}/lifecycle",
        json={"command": "publish", "request_id": "publish-after-check", "base_version": current_project["project_revision"], "continue_reason": "不同理由"},
    )
    assert replay.status_code == 409
    assert replay.json()["detail"]["code"] == "idempotency_key_reused"


def test_library_search_scope_and_reference_versions_are_explicit(client):
    project_id = create_project(client, "参考文献测试论文")
    other_id = create_project(client, "另一篇论文")
    current = client.get(f"/api/library/projects?scope=current&current_project_id={project_id}&query=参考文献").json()
    assert [item["id"] for item in current["items"]] == [project_id]
    global_results = client.get("/api/library/projects?scope=global&query=论文").json()
    assert {item["id"] for item in global_results["items"]} == {project_id, other_id}

    record = client.post("/api/references", json={
        "title": "Robust Graph Evidence", "authors": "A. Author", "year": "2025",
        "doi": "https://doi.org/10.1000/robust-graph", "notes": "原始笔记",
    })
    assert record.status_code == 200, record.text
    reference = record.json()
    assert reference["confidence"] == "lowered"
    link = client.post(
        f"/api/projects/{project_id}/references/{reference['id']}",
        json={"request_id": "link-reference", "reference_version": 1, "cite_key": "robustGraph2025"},
    )
    assert link.status_code == 200, link.text

    revised = client.put(
        f"/api/references/{reference['id']}",
        json={"base_version": 1, "changes": {"title": "Robust Graph Evidence Revised", "notes": "原始笔记"}},
    )
    assert revised.status_code == 200, revised.text
    project_refs = client.get(f"/api/projects/{project_id}").json()["refs"]
    pinned = next(item for item in project_refs if item["id"] == reference["id"])
    assert pinned["title"] == "Robust Graph Evidence"
    assert pinned["reference_version"] == 1
    assert pinned["link_status"] == "update_available"

    duplicate = client.post("/api/references", json={
        "title": "Robust Graph Evidence Revised", "authors": "A. Author", "year": "2025",
        "doi": "10.1000/robust-graph", "notes": "补充笔记",
    })
    assert duplicate.status_code == 200
    assert reference["id"] in {item["id"] for item in duplicate.json()["possible_duplicates"]}
    merged = client.post(
        f"/api/references/{duplicate.json()['id']}/merge",
        json={"request_id": "merge-duplicate", "target_reference_id": reference["id"], "base_version": 2},
    )
    assert merged.status_code == 200, merged.text
    reused_merge_key = client.post(
        f"/api/references/{reference['id']}/merge",
        json={"request_id": "merge-duplicate", "target_reference_id": duplicate.json()["id"], "base_version": 2},
    )
    assert reused_merge_key.status_code == 409
    assert reused_merge_key.json()["detail"]["code"] == "idempotency_key_reused"
    target = client.get(f"/api/references/{reference['id']}").json()
    assert target["current_version"] == 3
    assert "原始笔记" in target["notes"] and "补充笔记" in target["notes"]
    assert len(target["versions"]) == 3

    refresh = client.post(
        f"/api/projects/{project_id}/references/{reference['id']}/refresh",
        json={"request_id": "refresh-reference", "reference_version": 3, "base_version": 1},
    )
    assert refresh.status_code == 200, refresh.text
    refreshed = next(item for item in client.get(f"/api/projects/{project_id}").json()["refs"] if item["id"] == reference["id"])
    assert refreshed["reference_version"] == 3
    assert refreshed["title"] == "Robust Graph Evidence Revised"


def test_search_handoff_stays_pending_until_user_confirms(client):
    project_id = create_project(client)
    stamp = app.now()
    search_id = uuid.uuid4().hex
    candidate_id = uuid.uuid4().hex
    conn = app.db()
    conn.execute(
        "INSERT INTO academic_searches(id,project_id,query,filters_json,provider,rule_version,request_id,generation,next_cursor,status,saved,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (search_id, project_id, "graph evidence", "{}", "crossref", "v1", "search-request", 1, None, "completed", 0, stamp, stamp),
    )
    conn.execute(
        "INSERT INTO academic_candidates(id,search_id,project_id,title,authors,year,venue,doi,canonical_url,abstract,provider,access_status,oa_url,match_reasons_json,sources_json,dedupe_key,rank_score,status,raw_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (candidate_id, search_id, project_id, "A Search Result", "B. Author", "2024", "Journal", "10.1000/search", "https://example.invalid/paper", "Abstract", "crossref", "metadata_only", "", "[]", "[]", "dedupe-search", 1.0, "candidate", "{}", stamp),
    )
    conn.commit()
    conn.close()

    handoff = client.post(
        f"/api/projects/{project_id}/search/handoff",
        json={"candidate_id": candidate_id, "idempotency_key": "handoff-search-candidate"},
    )
    assert handoff.status_code == 200, handoff.text
    draft = handoff.json()["reference_draft"]
    assert draft["status"] == "pending_confirmation"
    conn = app.db()
    active_count = conn.execute("SELECT COUNT(*) FROM reference_records WHERE doi='10.1000/search'").fetchone()[0]
    conn.close()
    assert active_count == 0

    details = client.get(f"/api/references/drafts/{draft['id']}").json()
    assert details["source_trace"]["candidate_id"] == candidate_id
    confirmed = client.post(
        f"/api/references/drafts/{draft['id']}/confirm",
        json={"request_id": "confirm-search-draft", "decision": "create"},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["reference"]["title"] == "A Search Result"
    changed_confirmation = client.post(
        f"/api/references/drafts/{draft['id']}/confirm",
        json={"request_id": "different-confirmation", "decision": "create"},
    )
    assert changed_confirmation.status_code == 409
    assert changed_confirmation.json()["detail"]["code"] == "reference_draft_already_confirmed"


def test_ai_preflight_binds_context_task_model_and_project_revision(client):
    project_id = create_project(client)
    enter_revision(client, project_id)
    file_data = client.get(f"/api/projects/{project_id}").json()["files"][0]
    request_context = {
        "action": "edit", "target_kind": "file", "target_path": file_data["path"],
        "prompt": "Revise the selected sentence", "content": file_data["content"],
        "selection": "Introduction", "model": "local-model-a",
    }
    preview = client.post(f"/api/projects/{project_id}/ai-context", json=request_context)
    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["model"] == "local-model-a"
    assert all("version" in source for source in body["sources"])

    changed_prompt = client.post(
        f"/api/projects/{project_id}/ai-context",
        json={**request_context, "prompt": "Use a different instruction"},
    )
    changed_model = client.post(
        f"/api/projects/{project_id}/ai-context",
        json={**request_context, "model": "local-model-b"},
    )
    assert changed_prompt.json()["version"] != body["version"]
    assert changed_model.json()["version"] != body["version"]

    base_request = {
        "action": "edit", "request_id": "preflight-required",
        "scope": {"targets": [{"kind": "file", "path": file_data["path"], "start": 0, "end": 12}]},
        "base_file_versions": {file_data["path"]: file_data["version"]},
    }
    missing = client.post(f"/api/projects/{project_id}/ai-jobs", json=base_request)
    assert missing.status_code == 422
    assert missing.json()["detail"]["code"] == "preflight_required"

    assert client.post(f"/api/projects/{project_id}/terms", json={"term": "graph evidence", "definition": "new context"}).status_code == 200
    stale_request = {
        **base_request,
        "request_id": "stale-preflight",
        "prompt": request_context["prompt"],
        "content": file_data["content"],
        "selection": request_context["selection"],
        "model": request_context["model"],
        "context_version": body["version"],
    }
    stale = client.post(f"/api/projects/{project_id}/ai-jobs", json=stale_request)
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "context_conflict"
    conn = app.db()
    assert conn.execute("SELECT COUNT(*) FROM ai_jobs WHERE project_id=?", (project_id,)).fetchone()[0] == 0
    conn.close()


def test_req006_candidate_rejection_preserves_original_file(client, monkeypatch):
    project_id = create_project(client)
    enter_revision(client, project_id)
    file_data = client.get(f"/api/projects/{project_id}").json()["files"][0]
    selected = "Introduction"
    start = file_data["content"].index(selected)
    preview = client.post(
        f"/api/projects/{project_id}/ai-context",
        json={
            "action": "edit", "target_kind": "file", "target_path": file_data["path"],
            "prompt": "Rewrite the selected heading", "content": file_data["content"], "selection": selected,
        },
    )
    assert preview.status_code == 200, preview.text
    monkeypatch.setattr(app, "call_ai", lambda *_args, **_kwargs: {"ok": True, "content": "Introduction revised"})
    created = client.post(
        f"/api/projects/{project_id}/ai-jobs",
        json={
            "action": "edit", "request_id": "req006-reject-candidate",
            "prompt": "Rewrite the selected heading", "content": file_data["content"], "selection": selected,
            "scope": {"targets": [{"kind": "file", "path": file_data["path"], "start": start, "end": start + len(selected)}]},
            "base_file_versions": {file_data["path"]: file_data["version"]},
            "context_version": preview.json()["version"],
        },
    )
    assert created.status_code == 202, created.text
    completed = client.get(f"/api/projects/{project_id}/ai-jobs/{created.json()['job']['id']}").json()
    assert completed["job"]["status"] == "succeeded"
    candidate = completed["candidate"]
    before = client.get(f"/api/projects/{project_id}").json()["files"][0]["content"]
    rejected = client.post(
        f"/api/projects/{project_id}/candidates/{candidate['id']}/decision",
        json={
            "decision": "reject", "request_id": "req006-reject-decision",
            "base_file_versions": candidate["base_file_versions"],
        },
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["decision"] == "reject"
    after = client.get(f"/api/projects/{project_id}").json()["files"][0]["content"]
    assert after == before == file_data["content"]
