import base64
import json
import uuid
from concurrent.futures import ThreadPoolExecutor

import app
from paperpilot.services import innovation_points, outline_suggestions, structure_tools
from paperpilot.services.context import build_ai_context_snapshot, context_snapshot_current
from paperpilot.services.workflow import workflow_projection
from tests.test_manuscript_import import URL as IMPORT_URL, new as new_import, upload
from tests.test_agent_conversations import payload as agent_payload, wait_run


def paper(client, name="创新点隔离测试"):
    return client.post("/api/projects", json={"name": name}).json()["project"]["id"]


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def path(project_id):
    return f"/api/projects/{project_id}/innovations"


def command(client, project, action, **values):
    return client.post(path(project) + "/commands", json={"request_id": uuid.uuid4().hex, "action": action, **values})


def draft(client, project, claim, **extra):
    return ok(command(client, project, "create_draft", body={"claim": claim, **extra}))


def confirm(client, project, item, active_revision):
    return command(client, project, "confirm", point_id=item["point_id"], draft_revision=item["draft_revision"],
                   active_revision=active_revision)


def test_confirmation_primary_revision_history_and_retry(client):
    project = paper(client)
    a = draft(client, project, "第一条可证伪创新")
    b = draft(client, project, "独立的副创新")
    assert ok(client.get(path(project)))["primary"] is None
    payload = {"request_id": "confirm_a", "action": "confirm", "point_id": a["point_id"],
               "draft_revision": a["draft_revision"], "active_revision": 0}
    first = ok(client.post(path(project) + "/commands", json=payload))
    assert first == ok(client.post(path(project) + "/commands", json=payload))
    assert first["role"] == "primary" and first["active"]["version"] == 1
    assert confirm(client, project, b, 0).status_code == 409
    second = ok(confirm(client, project, b, 1))
    assert second["role"] == "secondary"
    revised = ok(command(client, project, "save_draft", point_id=first["point_id"], draft_revision=first["draft_revision"],
                         body={"claim": "修订后的精确主张"}))
    assert revised["active"]["claim"] == "第一条可证伪创新"
    assert ok(confirm(client, project, revised, 2))["active"]["version"] == 2
    prior = ok(client.get(path(project) + "/" + a["point_id"] + "?version=1"))
    assert prior["selected"]["claim"] == "第一条可证伪创新"
    switched = ok(command(client, project, "set_primary", point_id=b["point_id"], active_revision=3))
    assert switched["role"] == "primary"
    overview = ok(client.get(path(project)))
    assert overview["primary"]["point_id"] == b["point_id"]
    assert [item["point_id"] for item in overview["secondary"]] == [a["point_id"]]
    c = draft(client, project, "第三条贡献")
    ok(confirm(client, project, c, overview["active_revision"]))
    assert {item["point_id"] for item in ok(client.get(path(project)))["secondary"]} == {a["point_id"], c["point_id"]}


def test_concurrent_confirmation_and_cross_project_reads(client):
    project, other = paper(client), paper(client, "另一篇论文")
    items = [draft(client, project, claim) for claim in ("甲", "乙")]
    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(lambda item: confirm(client, project, item, 0), items))
    assert sorted(response.status_code for response in responses) == [200, 409]
    saved = ok(client.get(path(project)))
    assert saved["primary"] and len(saved["drafts"]) == 1 and not saved["secondary"]
    assert client.get(path(other) + "/" + saved["primary"]["point_id"]).status_code == 404


def test_old_paper_extraction_source_change_and_relocation(client):
    project, other = paper(client), paper(client, "另一个稿件")
    filename = "main.tex"
    body = "\\section{Introduction}\n本文提出一种可复核的匹配方法。"
    version = ok(client.put(f"/api/projects/{project}/files", json={"path": filename, "content": body, "base_version": 1}))["version"]
    preview_path = path(project) + "/extractions/preview"
    source = {"kind": "file", "id": filename}
    old = ok(client.post(preview_path, json=source))
    assert old["candidates"][0]["claim"].startswith("本文提出")
    payload = {**source, "fingerprint": old["fingerprint"], "keys": [old["candidates"][0]["key"]], "request_id": "extract"}
    created = ok(client.post(path(project) + "/extractions", json=payload))
    assert created == ok(client.post(path(project) + "/extractions", json=payload))
    point_id = created["point_ids"][0]
    assert ok(client.get(path(project)))["primary"] is None
    assert client.post(path(other) + "/extractions", json=payload).status_code == 409
    item = ok(client.get(path(project) + "/" + point_id))
    saved = ok(command(client, project, "save_draft", point_id=point_id, draft_revision=item["draft_revision"],
                       body={"claim": "作者改写了范围"}))
    ok(client.put(f"/api/projects/{project}/files", json={"path": filename, "content": body.replace("匹配", "插值"),
                                                       "base_version": version}))
    assert confirm(client, project, saved, 0).status_code == 409
    refreshed = ok(client.post(preview_path, json=source))
    ok(client.post(path(project) + "/extractions", json={**source, "fingerprint": refreshed["fingerprint"],
        "keys": [refreshed["candidates"][0]["key"]], "request_id": "relocate", "point_id": point_id, "draft_revision": saved["draft_revision"]}))
    updated = ok(client.get(path(project) + "/" + point_id))
    assert updated["draft"]["claim"] == "作者改写了范围" and "插值" in updated["draft"]["source"]["excerpt"]
    assert ok(confirm(client, project, updated, 0))["active"]["version"] == 1


def test_adopted_manuscript_and_research_candidate_stay_in_review(client):
    batch_id = new_import(client, "innovation-adopted-import")
    ok(upload(client, batch_id, "main.md", b"# Abstract\nWe propose a new reproducible comparison method.\n"))
    preview = ok(client.get(f"{IMPORT_URL}/{batch_id}/preview"))
    applied = ok(client.post(f"{IMPORT_URL}/{batch_id}/apply", json={"request_id": "innovation-adoption",
        "fingerprint": preview["fingerprint"], "decisions": {item["id"]: "add" for item in preview["items"] if item["suggested"]}}))
    project = applied["project_id"]
    source = {"kind": "import", "id": batch_id}
    candidates = ok(client.post(path(project) + "/extractions/preview", json=source))
    assert candidates["candidates"][0]["source"]["anchor"]
    another = paper(client, "另一篇已写论文")
    assert client.post(path(another) + "/extractions/preview", json=source).status_code == 404
    with app.db() as conn:
        card = innovation_points._propose({"claim": "来自研究讨论的候选"}, {
            "conn": conn, "project_id": project, "state": {"instruction": "梳理论文创新点候选"}})["cards"][0]
    before = ok(client.get(path(project)))
    assert before["primary"] is None and before["drafts"][0]["point_id"] == card["id"]
    assert not ok(client.get(path(project) + "/" + card["id"]))["active"]


def test_support_references_render_status_and_reject_cross_project(client):
    project, other = paper(client), paper(client, "隔离工作项")
    ok(client.put(f"/api/projects/{project}/logic/graph", json={"base_version": 0, "request_id": "conflicting-evidence",
        "nodes": [{"id": "claim", "kind": "claim", "label": "论文主张"}, {"id": "observation", "kind": "evidence", "label": "反例"}],
        "edges": [{"id": "conflict", "from_node_id": "observation", "to_node_id": "claim", "edge_type": "contradicts"}]}))
    ok(client.post(f"/api/projects/{project}/results", json={"request_id": "support-data", "filename": "comparison.csv",
        "content_base64": base64.b64encode(b"case,error\n1,0.2\n").decode()}))
    targets = ok(client.get(path(project) + "/options"))["support"]
    pending = next(item for item in targets if item["kind"] == "work")
    observation = next(item for item in targets if item["kind"] == "logic" and item["id"] == "observation")
    result = next(item for item in targets if item["kind"] == "result")
    refs = [{key: entry[key] for key in ("kind", "id", "version")} for entry in (pending, observation, result)]
    body = {"claim": "研究主张", "support": [{"description": "完成对照试验", "refs": refs}]}
    assert command(client, other, "create_draft", body=body).status_code == 422
    candidate = ok(command(client, project, "create_draft", body=body))
    ok(confirm(client, project, candidate, 0))
    current = ok(client.get(path(project) + "/" + candidate["point_id"]))
    matches = {target["kind"]: target for target in current["selected"]["support"][0]["targets"]}
    assert matches["work"]["id"] == pending["id"] and matches["work"]["status"] == "pending"
    assert matches["logic"]["status"] == "conflict"
    assert client.get(matches["result"]["url"]).content == b"case,error\n1,0.2\n"


def test_paper_chat_proposes_an_author_reviewable_point(client, monkeypatch):
    project = paper(client)
    root = f"/api/projects/{project}/agent"
    conversation = ok(client.post(root + "/workspace", json={}))["root"]["id"]

    def model(*args, **kwargs):
        if any(item.get("role") == "tool" for item in kwargs["messages"]):
            return {"ok": True, "content": "候选已保存，请回看原文与适用范围。", "usage": {"total_tokens": 80}}
        return {"ok": True, "content": "", "tool_calls": [{
            "id": "innovation-proposal", "type": "function", "function": {
                "name": "propose_innovation", "arguments": json.dumps({"claim": "可复查的研究创新主张", "scope": "给定输入范围"})
            }}], "usage": {"total_tokens": 80}}

    monkeypatch.setattr(app, "call_ai", model)
    response = client.post(root + "/runs", json=agent_payload(client, project, conversation, "梳理这篇论文的创新点候选"))
    assert response.is_success, response.text
    finished = wait_run(client, project, response.json()["id"])
    assert finished["status"] == "succeeded", finished
    overview = ok(client.get(path(project)))
    assert overview["primary"] is None and len(overview["drafts"]) == 1
    entry = overview["drafts"][0]
    assert entry["draft"]["claim"] == "可复查的研究创新主张"
    assert ok(confirm(client, project, entry, 0))["active"]["version"] == 1


def test_downstream_omits_draft_and_old_plan_and_binds_confirmed_version(client):
    project = paper(client)
    startup = f"/api/projects/{project}/workflow/startup"
    state = ok(client.get(startup))
    stage_url = startup + "/commands"
    for operation in ("save", "review", "confirm"):
        payload = {"request_id": uuid.uuid4().hex, "expected_workflow_version": state["workflow_version"],
                   "command": operation, "stage_id": "STAGE-01"}
        if operation == "save":
            payload["fields"] = {"research_question": "问题", "scope": "范围", "contribution": "OLD-NOT-VALID"}
        state = ok(client.post(stage_url, json=payload))
    a = draft(client, project, "DRAFT-NOT-VALID")
    with app.db() as conn:
        bundle = app.project_payload(project)
        empty = build_ai_context_snapshot(bundle["project"], bundle, workflow=workflow_projection(conn, project))
        request = outline_suggestions.preview(conn, project, {"instruction": "建立正式大纲"})
        assert "innovations" not in request["outbound_preview"]
        assert "OLD-NOT-VALID" not in empty["text"] and "DRAFT-NOT-VALID" not in empty["text"]
    active = ok(confirm(client, project, a, 0))
    with app.db() as conn:
        bundle = app.project_payload(project)
        snapshot = build_ai_context_snapshot(bundle["project"], bundle, workflow=workflow_projection(conn, project))
        request = outline_suggestions.preview(conn, project, {"instruction": "建立正式大纲"})
        assert request["outbound_preview"]["innovations"][0]["version"] == 1
        refs = innovation_points.manifest_references(snapshot["sources"])
        assert refs[0]["point_id"] == a["point_id"]
        innovation_points.record_usage(conn, project, refs, "outline", "example", "1", "实验大纲")
        assert context_snapshot_current(conn, project, snapshot["sources"])
    revised = ok(command(client, project, "save_draft", point_id=a["point_id"], draft_revision=active["draft_revision"], body={"claim": "第二版主张"}))
    with app.db() as conn:
        assert context_snapshot_current(conn, project, snapshot["sources"])
    ok(confirm(client, project, revised, 1))
    with app.db() as conn:
        assert not context_snapshot_current(conn, project, snapshot["sources"])
    point = ok(client.get(path(project) + "/" + a["point_id"]))
    assert point["usages"][0]["needs_review"]


def test_formal_outline_generation_works_with_empty_context_and_rechecks_confirmed_version(client, monkeypatch):
    project = paper(client)
    api = f"/api/projects/{project}/outline"
    requests = []

    def model(*args, **kwargs):
        requests.append(str(args) + str(kwargs))
        return {"ok": True, "content": json.dumps({"operations": [{"op": "add_node",
            "node": {"id": f"section-{len(requests)}", "title": "方法", "purpose": "解释验证边界"}}]})}

    def generate(request_id):
        payload = {"request_id": request_id, "instruction": "生成正式大纲"}
        preview = ok(client.post(api + "/suggestion-preview", json=payload))
        result = ok(client.post(api + "/suggestions", json={**payload, "consent_to_send": True,
                   "preview_fingerprint": preview["preview_fingerprint"]}))
        return result

    monkeypatch.setattr(app, "call_ai", model)
    candidate = draft(client, project, "DRAFT-OUTLINE-CONTEXT")
    empty = generate("no-confirmed-innovations")
    assert empty["status"] == "candidate" and "DRAFT-OUTLINE-CONTEXT" not in requests[-1]
    active = ok(confirm(client, project, candidate, 0))
    suggestion = generate("with-confirmed-innovations")
    assert "DRAFT-OUTLINE-CONTEXT" in requests[-1]
    assert suggestion["innovation_refs"][0]["point_id"] == candidate["point_id"]
    revised = ok(command(client, project, "save_draft", point_id=candidate["point_id"],
                         draft_revision=active["draft_revision"], body={"claim": "新版创新范围"}))
    ok(confirm(client, project, revised, 1))
    stale = client.post(api + f"/candidates/{suggestion['id']}/decision", json={"decision": "accept",
        "base_version": suggestion["base_version"], "request_id": "apply-stale-outline"})
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "innovation_context_stale"


def test_traceify_binds_confirmed_innovations_and_preserves_direct_source(client):
    project = paper(client)
    endpoint = f"/api/projects/{project}/structure-workspaces"
    value = ok(client.post(endpoint, json={"request_id": "innovation-traceify", "entry_mode": "paper_import",
                                           "content": "本文采用可复现的实验方法。"}))
    wid = value["workspace"]["id"]
    item = draft(client, project, "TRACEIFY-REVIEW-MARKER")
    with app.db() as conn:
        assert "innovations" not in structure_tools.profile(conn, project, wid)["manifest"]
    ok(confirm(client, project, item, 0))
    with app.db() as conn:
        profile = structure_tools.profile(conn, project, wid)["manifest"]
    ref = profile["innovation_refs"][0]
    assert ref["point_id"] == item["point_id"] and ref["version"] == 1
    assert profile["source_hash"] == value["workspace"]["source"]["content_hash"]
