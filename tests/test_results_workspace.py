import base64
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256

import app
from tests.test_req022_assets import project


def payload(content: bytes, request_id: str, filename="measurements.csv", **extra):
    return {"filename": filename, "content_base64": base64.b64encode(content).decode("ascii"),
            "request_id": request_id, **extra}


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def versions_url(root, version):
    return f"{root}/{version['dataset_id']}/versions"


def test_raw_import_download_isolation_and_concurrent_append(client):
    a, b = project(client), project(client, "第二篇")
    root = f"/api/projects/{a}/results"
    raw = b'\xef\xbb\xbfx\ty\tnote\r\n1\t3\t"First\tcell"\r\n'
    data = payload(raw, "raw", filename="原件.tsv", title="测量数据", note="运行 1")
    first = ok(client.post(root, json=data))
    assert first["sha256"] == sha256(raw).hexdigest()
    assert first["note"] == "运行 1" and first["version_no"] == 1
    original = client.get(first["download_url"])
    assert original.content == raw
    assert "UTF-8''" in original.headers["Content-Disposition"]
    url = versions_url(root, first)
    assert ok(client.get(f"{url}/{first['id']}"))["rows"] == [["1", "3", "First\tcell"]]
    assert ok(client.post(root, json=data))["id"] == first["id"]
    assert client.post(root, json=data | {"note": "different"}).status_code == 409
    assert ok(client.get(root.replace(a, b)))["datasets"] == []
    assert client.get(first["download_url"].replace(a, b)).status_code == 404
    assert client.post(url.replace(a, b), json=payload(raw, "other", "raw.tsv")).status_code == 404

    def append(index):
        incoming = payload(f"x,y\n{index},0\n".encode(), f"append-{index}")
        return ok(client.post(url, json=incoming))["version_no"]

    with ThreadPoolExecutor(max_workers=3) as pool:
        ordinals = list(pool.map(append, range(3)))
    assert sorted(ordinals) == [2, 3, 4]
    assert client.get(first["download_url"]).content == raw


def test_transform_preserves_parent_and_records_recipe(client):
    pid = project(client)
    root = f"/api/projects/{pid}/results"
    source = ok(client.post(root, json=payload(b"group,x,extra\nB,2,\nA,1,a\nA,1,a\nA,3,b\nB,5,c\n", "raw")))
    base = versions_url(root, source)

    def derive(parent, operation, request_id=None):
        return client.post(f"{base}/{parent['id']}/derive", json={
            "operation": operation, "note": "处理记录", "request_id": request_id or operation["kind"],
        })

    trimmed = ok(derive(source, {"kind": "select", "columns": ["group", "x"]}))
    assert trimmed["parent_version_id"] == source["id"]
    assert trimmed["note"] == "处理记录"
    assert trimmed["operation"] == {"kind": "select", "version": 1, "columns": ["group", "x"]}
    assert ok(derive(source, {"kind": "drop_blank"}))["row_count"] == 4
    dedup = ok(derive(trimmed, {"kind": "deduplicate"}))
    assert dedup["row_count"] == 4
    selected = ok(derive(dedup, {"kind": "filter", "column": "x", "minimum": "1", "maximum": "3"}))
    assert selected["row_count"] == 3
    recipe = {"kind": "summarize", "group_by": "group", "column": "x"}
    summarized = ok(derive(selected, recipe))
    assert ok(derive(selected, recipe))["id"] == summarized["id"]
    summary = ok(client.get(f"{base}/{summarized['id']}"))
    assert summary["rows"] == [["B", "1", "2", "2", "2"], ["A", "2", "2", "1", "3"]]
    assert summary["columns"] == ["group", "count", "mean", "min", "max"]
    assert ok(client.get(f"{base}/{source['id']}"))["row_count"] == 5
    assert derive(source, {"kind": "filter", "column": "x", "minimum": "9"}, "empty").status_code == 422
    assert derive(source, {"kind": "summarize", "group_by": "group", "column": "extra"}, "bad").status_code == 422
    assert len(ok(client.get(f"{root}/{source['dataset_id']}"))["versions"]) == 6


def test_chart_preview_save_and_both_links_pin_the_exact_result(client):
    pid = project(client)
    root = f"/api/projects/{pid}/results"
    first = ok(client.post(root, json=payload(b"x,loss\n2,4\n1,9\n3,1\n", "initial")))
    base = versions_url(root, first)
    chart = {"kind": "line", "x_column": "x", "y_column": "loss", "title": "Loss < trace"}
    chart_path = f"{base}/{first['id']}/chart"
    preview = ok(client.post(f"{chart_path}-preview", json=chart))
    assert "<polyline" in preview["svg"] and "Loss &lt; trace" in preview["svg"]
    assert ok(client.post(f"{chart_path}-preview", json=chart))["sha256"] == preview["sha256"]
    saved_input = {"chart": chart, "preview_sha256": preview["sha256"], "request_id": "chart"}
    assert client.post(chart_path, json=saved_input | {"chart": chart | {"kind": "scatter"}}).status_code == 409
    assert ok(client.get(f"/api/projects/{pid}/drawing/assets"))["assets"] == []
    second = ok(client.post(base, json=payload(b"x,loss\n1,2\n2,3\n", "different")))
    saved = ok(client.post(chart_path, json=saved_input))
    figure = saved["version"]
    assert figure["origin_type"] == "result_chart"
    assert figure["result_source"]["version_id"] == first["id"]
    assert figure["result_source"]["sha256"] == first["sha256"]
    assert figure["source_sha256"] == preview["sha256"]
    assert client.get(figure["preview_url"]).content.startswith(b"\x89PNG")
    assert client.get(figure["download_url"]).content == preview["svg"].encode("utf-8")
    assert ok(client.get(f"{base}/{first['id']}"))["images"][0]["version_id"] == figure["id"]
    assert ok(client.get(f"{base}/{second['id']}"))["images"] == []
    assert ok(client.post(chart_path, json=saved_input))["version"]["id"] == figure["id"]
    assert len(ok(client.get(f"/api/projects/{pid}/drawing/assets"))["assets"]) == 1
    files = ok(client.get(f"/api/projects/{pid}"))["files"]
    main = next(file for file in files if file["path"] == "main.tex")
    inserted = ok(client.post(f"/api/projects/{pid}/drawing/assets/{saved['asset']['id']}/insert-into-editor", json={
        "version_id": figure["id"], "request_id": "insert-chart", "target_path": main["path"],
        "base_file_version": main["version"], "caption": "Loss by step",
    }))
    assert inserted["asset_version_id"] == figure["id"]
    written = next(file for file in ok(client.get(f"/api/projects/{pid}"))["files"] if file["path"] == main["path"])
    assert figure["build_name"] in written["content"]


def test_reject_malformed_data_and_gate_published_storage(client):
    pid = project(client)
    root = f"/api/projects/{pid}/results"
    for raw in (b"x,x\n1,2\n", b"x,y\n1\n", b"x,y\n", b"x,\xff\n0,0\n"):
        assert client.post(root, json=payload(raw, "bad")).status_code == 422
    assert ok(client.get(root))["datasets"] == []
    source = ok(client.post(root, json=payload(b"x,y\n1,NaN\n", "created")))
    base = versions_url(root, source)
    path = f"{base}/{source['id']}"
    bad_chart = {"kind": "scatter", "x_column": "x", "y_column": "y"}
    assert client.post(f"{path}/chart-preview", json=bad_chart).json()["detail"]["code"] == "invalid_numeric_data"
    with app.db() as conn:
        conn.execute("UPDATE projects SET lifecycle_state='published' WHERE id=?", (pid,))
    assert client.get(path).status_code == 200
    assert client.post(base, json=payload(b"x,y\n1,2\n", "new")).status_code == 409
    assert client.post(f"{path}/derive", json={"operation": {"kind": "deduplicate"}, "request_id": "no-write"}).status_code == 409
    assert client.post(f"{path}/chart", json={"chart": bad_chart, "request_id": "no-image", "preview_sha256": "a" * 64}).status_code == 409
