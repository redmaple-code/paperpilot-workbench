import base64
import json
import app
from paperpilot.services import agent_conversations, agent_materials, result_conversations, results
from paperpilot.services.project_errors import ProjectServiceError
from tests.test_conversation_product import workspace
from tests.test_agent_conversations import payload as run_payload, wait_run
from tests.test_req022_assets import upload
from tests.test_results_workspace import ok, payload


def endpoints(client):
    pid, cid = workspace(client)
    root = f"/api/projects/{pid}"
    prefix = f"{root}/results/conversations/{cid}"

    def turn(text, key, **reference):
        return ok(client.post(prefix + "/turn", json={"instruction": text, "request_id": key, **reference}))

    def decide(card, action, key, **choice):
        return client.post(f"{prefix}/cards/{card['id']}/decision", json={
            "action": action, "request_id": key, **choice,
        })

    return pid, cid, root, prefix, turn, decide


def test_result_candidate_archive_replay_and_private_context(client):
    pid, cid, root, _, turn, decide = endpoints(client)
    raw = b"\xef\xbb\xbfx\ty\r\n0\t4\r\n0\t4\r\n1\t3\r\n"
    attachments = f"{root}/agent/conversations/{cid}/attachments"
    data = {"filename": "run.tsv", "request_id": "attach", "content_base64": base64.b64encode(raw).decode()}
    source = ok(client.post(attachments, json=data))
    assert ok(client.get(attachments + "/" + source["id"]))["row_count"] == 3
    assert client.get(attachments + "/" + source["id"] + "/download").content == raw
    assert not turn("整理参考文献列表", "other")["handled"]
    instruction = "把 run.tsv 去重后入库，命名为 试验数据"
    candidate = turn(instruction, "prepare")["card"]
    assert candidate["kind"] == "upload"
    assert candidate["row_count"] == 2
    assert turn(instruction, "prepare")["card"]["id"] == candidate["id"]
    assert ok(client.get(root + "/results"))["datasets"] == []
    with app.db() as conn:
        assert agent_conversations.history(conn, pid, cid) == []
        materials = agent_materials.build(conn, pid, {"conversation_id": cid})
        assert "run.tsv" not in materials["text"]
        assert not materials["manifest"]["attachments"]

    saved = ok(decide(candidate, "confirm", "commit"))
    assert ok(decide(candidate, "confirm", "commit")) == saved
    assert client.get(saved["receipt"]["original_download_url"]).content == raw
    assert saved["receipt"]["version_no"] == 2
    assert ok(client.get(root + "/results/library"))["groups"][0]["items"][0]["version_id"] == saved["receipt"]["version_id"]


def test_chart_uses_pinned_source_and_can_recover_from_empty_range(client):
    pid, cid, root, prefix, turn, decide = endpoints(client)
    data = ok(client.post(root + "/results", json=payload(b"x,loss\n0,4\n1,3\n2,2\n", "v1", title="训练误差")))
    chart = turn("用训练误差数据画折线，Y 轴用 loss", "chart")["card"]
    assert chart["version"]["id"] == data["id"]
    assert len(ok(client.get(root + "/drawing/assets"))["assets"]) == 0
    preview_url = f"{prefix}/cards/{chart['id']}/preview"
    assert client.get(preview_url).headers["content-type"].startswith("image/svg+xml")
    assert "sandbox" in client.get(preview_url).headers["content-security-policy"]
    failed = turn("只保留 x 在 8 到 9 的范围", "empty", reference_id=chart["id"])
    assert "为空" in failed["message"]
    assert failed["card"]["reference_id"] == chart["id"]
    refined = turn("只保留 x 在 0 到 1 的范围", "filter", reference_id=chart["id"])["card"]
    assert refined["row_count"] == 2
    first = ok(decide(refined, "confirm", "commit"))
    second = ok(decide(refined, "confirm", "retry"))
    assert second["receipt"] == first["receipt"]
    assert len(ok(client.get(root + "/drawing/assets"))["assets"]) == 1
    alternate = ok(client.post(f"{root}/agent/conversations", json={"title": "Independent"}))
    url = f"{root}/results/conversations/{alternate['id']}/cards/{chart['id']}"
    assert client.get(url).status_code == 404


def test_result_pipeline_commit_rolls_back_on_late_error(client, monkeypatch):
    pid, cid, root, _, turn, _ = endpoints(client)
    attachment = payload(b"x,y\n1,2\n1,2\n", "upload", "raw.csv")
    ok(client.post(f"{root}/agent/conversations/{cid}/attachments", json=attachment))
    draft = turn("把 raw.csv 去重后入库", "candidate")["card"]

    def fail(*args, **kwargs):
        raise ProjectServiceError(422, "result_too_large", "处理后的数据超过上限。")

    monkeypatch.setattr(results, "derive", fail)
    reply = turn("保存", "save", reference_id=draft["id"])
    assert reply["card"]["kind"] == "retry"
    assert ok(client.get(root + "/results"))["datasets"] == []


def test_agent_tool_returns_opaque_pointer_only(client):
    pid, cid, root, prefix, _, _ = endpoints(client)
    data = payload(b"x,loss\n0,42301\n1,29111\n", "orig", title="训练误差")
    ok(client.post(root + "/results", json=data))
    with app.db() as conn:
        result = result_conversations.prepare_tool({"instruction": "用训练误差数据画折线，Y 轴用 loss"},
            {"conn": conn, "project_id": pid, "conversation_id": cid, "run_id": "test-agent"})
    assert set(result["cards"][0]) == {"kind", "id", "project_id", "conversation_id", "title"}
    assert "42301" not in str(result)
    card = ok(client.get(f"{prefix}/cards/{result['cards'][0]['id']}"))
    assert card["row_count"] == 2
    assert ok(client.get(root + "/drawing/assets"))["assets"] == []


def test_model_transport_can_prepare_results_without_receiving_values(client, monkeypatch):
    pid, cid, root, prefix, _, _ = endpoints(client)
    raw = b"x,y\n1,1762339181\n2,1762339182\n"
    ok(client.post(f"{root}/agent/conversations/{cid}/attachments", json=payload(raw, "local", "draft.csv")))
    calls = []

    def model(*args, **kwargs):
        messages = kwargs["messages"]
        calls.extend(messages)
        if any(message["role"] == "tool" for message in messages):
            return {"ok": True, "content": "已备好候选，请在卡片核对。", "usage": {"total_tokens": 60}}
        return {"ok": True, "content": "", "usage": {"total_tokens": 60}, "tool_calls": [{
            "id": "local-result", "type": "function", "function": {
                "name": "prepare_result", "arguments": json.dumps({"instruction": "整理 CSV 附件并去重"})
            },
        }]}

    monkeypatch.setattr(app, "call_ai", model)
    body = run_payload(client, pid, cid, "请把刚测完的那批数据收好")
    run = ok(client.post(f"{root}/agent/runs", json=body))
    finished = wait_run(client, pid, run["id"])
    assert finished["status"] == "succeeded", finished
    pointer = finished["result"]["cards"][0]
    assert pointer["kind"] == "result_work"
    assert ok(client.get(f"{prefix}/cards/{pointer['id']}"))["row_count"] == 2
    assert "1762339181" not in json.dumps(calls)
    assert "draft.csv" not in json.dumps(calls)
    assert ok(client.get(root + "/results"))["datasets"] == []


def test_insert_rejects_stale_file_then_reuses_asset_and_caption(client):
    pid, _, root, _, turn, decide = endpoints(client)
    asset = ok(upload(client, pid))
    before = next(file for file in ok(client.get(root))["files"] if file["path"] == "main.tex")
    candidate = turn("把这张图插到 main.tex，图注为实验对照", "insert",
        asset_id=asset["asset"]["id"], version_id=asset["version"]["id"])["card"]
    assert candidate["target"]["caption"] == "实验对照"
    ok(client.put(root + "/files", json={"path": "main.tex", "base_version": before["version"],
                                      "content": before["content"] + "\n% changed"}))
    stale = decide(candidate, "confirm", "stale")
    assert stale.status_code == 409 and stale.json()["detail"]["code"] == "editor_version_conflict"
    refreshed = turn("重新预览插入位置", "updated", reference_id=candidate["id"])["card"]
    assert refreshed["target"]["caption"] == candidate["target"]["caption"]
    assert refreshed["target"]["base_file_version"] != candidate["target"]["base_file_version"]
    applied = ok(decide(refreshed, "confirm", "apply"))
    assert applied["receipt"]["version_id"] == asset["version"]["id"]
    assert ok(decide(refreshed, "confirm", "apply")) == applied
    library_url = root + "/results/library"
    adopted = ok(client.get(library_url))["groups"][0]
    assert adopted["category"] == "adopted"
    assert adopted["items"][0]["version_id"] == asset["version"]["id"]
    written = next(file for file in ok(client.get(root))["files"] if file["path"] == "main.tex")
    ok(client.put(root + "/files", json={"path": "main.tex", "base_version": written["version"], "content": before["content"]}))
    assert not ok(client.get(library_url))["groups"]


def test_library_groups_by_structure_and_persists_author_labels(client):
    pid, _, root, _, turn, decide = endpoints(client)
    data_url = root + "/results"
    library_url = data_url + "/library"
    raw = b"x,rho,u,p\n0,1,2,3\n1,2,3,4\n"
    profile = ok(client.post(data_url, json=payload(raw, "curve", "result.csv", title="result")))
    series = ok(client.post(data_url, json=payload(b"time,temperature\n0,23\n1,24\n", "series", "sensor.csv", title="传感器")))
    convergence = ok(client.post(data_url, json=payload(b"cells,l1\n100,0.12\n200,0.04\n", "conv", "mesh.csv", title="网格误差")))
    groups = ok(client.get(library_url))["groups"]
    by_category = {group["category"]: group["items"][0] for group in groups}
    assert by_category["profile"]["dataset_id"] == profile["dataset_id"]
    assert "密度" in by_category["profile"]["display_title"]
    assert by_category["series"]["dataset_id"] == series["dataset_id"]
    assert by_category["convergence"]["dataset_id"] == convergence["dataset_id"]
    assert all("rows" not in item for item in by_category.values())

    card = turn("用 result 数据画密度剖面", "draw", dataset_id=profile["dataset_id"], version_id=profile["id"])["card"]
    figure = ok(decide(card, "confirm", "save"))["receipt"]
    curve_url = f"{library_url}/items/table/{profile['dataset_id']}"
    fig_url = f"{library_url}/items/figure/{figure['asset_id']}"
    assert ok(client.put(curve_url, json={"title": "运行 A · 空间剖面", "group": "主文结果"}))["manual_group"] == "主文结果"
    manually_grouped = ok(client.get(library_url, params={"q": "运行 A"}))["groups"][0]
    assert manually_grouped["title"] == "主文结果"
    assert manually_grouped["items"][0]["figures"][0]["version_id"] == figure["version_id"]
    assert ok(client.put(fig_url, json={"title": "主文密度图", "group": "图表待审"}))["manual_title"] == "主文密度图"
    separated = ok(client.get(library_url, params={"q": "主文密度图"}))["groups"][0]
    assert separated["title"] == "图表待审"
    assert separated["items"][0]["source"]["version_id"] == profile["id"]
    assert client.get(profile["download_url"]).content == raw
    assert next(item for item in ok(client.get(data_url))["datasets"] if item["id"] == profile["dataset_id"])["title"] == "result"

    outside, _ = workspace(client)
    assert client.put(curve_url.replace(pid, outside), json={"title": "跨论文", "group": ""}).status_code == 404
    assert client.put(fig_url.replace(pid, outside), json={"title": "", "group": "跨论文"}).status_code == 404
    assert ok(client.put(fig_url, json={"title": "", "group": ""}))["manual_group"] == ""
    assert ok(client.put(curve_url, json={"title": "", "group": ""}))["manual_title"] == ""
    restored = ok(client.get(library_url))
    assert restored["manual_groups"] == []
    assert next(group for group in restored["groups"] if group["category"] == "profile")["items"][0]["figures"]
    with app.db() as conn:
        conn.execute("UPDATE projects SET archived=1 WHERE id=?", (pid,))
    assert client.put(curve_url, json={"title": "只读", "group": ""}).status_code == 403


def test_agent_result_grouping_uses_anonymous_descriptors_and_respects_manual_labels(client, monkeypatch):
    pid, cid, root, _, turn, _ = endpoints(client)
    result = root + "/results"
    first = ok(client.post(result, json=payload(b"x,rho,very_private_name\n0,12972931,hi\n", "a.csv", title="private_result_name")))
    second = ok(client.post(result, json=payload(b"x,rho,very_private_name\n1,12972932,hi\n", "b.csv", title="a-second")))
    ok(client.put(f"{result}/library/items/table/{first['dataset_id']}", json={"title": "", "group": "手工定组"}))
    task = "请给结果库按类型和关联重新分组"
    assert not turn(task, "agent-handles")["handled"]
    calls = []

    def model(*args, **kwargs):
        messages = kwargs["messages"]
        calls.extend(messages)
        if "你在整理科研结果库" in messages[0]["content"]:
            descriptors = json.loads(messages[1]["content"])["items"]
            assert len(descriptors) == 2 and descriptors[0]["shape_id"] == descriptors[1]["shape_id"]
            assert all(item["fields"] == ["x", "rho"] for item in descriptors)
            content = json.dumps([{"type": "profile", "name": "同字段剖面", "items": [item["id"] for item in descriptors]}])
            return {"ok": True, "content": content, "usage": {"total_tokens": 60}}
        if any(message["role"] == "tool" for message in messages):
            return {"ok": True, "content": "已建立分组。", "usage": {"total_tokens": 60}}
        return {"ok": True, "content": "", "usage": {"total_tokens": 60}, "tool_calls": [{
            "id": "group-library", "type": "function",
            "function": {"name": "group_result_library", "arguments": "{}"},
        }]}

    monkeypatch.setattr(app, "call_ai", model)
    body = run_payload(client, pid, cid, task)
    run = ok(client.post(f"{root}/agent/runs", json=body))
    finished = wait_run(client, pid, run["id"])
    assert finished["status"] == "succeeded" and finished["result"]["cards"], finished
    assert finished["result"]["context_manifest"]["scope"] == "result-library"
    groups = {group["title"]: group for group in ok(client.get(result + "/library"))["groups"]}
    assert groups["同字段剖面"]["items"][0]["dataset_id"] == second["dataset_id"]
    assert groups["手工定组"]["items"][0]["dataset_id"] == first["dataset_id"]
    assert not any(marker in str(calls) for marker in ("private_result_name", "very_private_name", "12972931"))
