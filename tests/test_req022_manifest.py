from __future__ import annotations

import base64
import io
from pathlib import Path

from PIL import Image

import app
from paperpilot.services import visual_assets
from tests.planning_helper import complete_planning


def test_editor_selection_is_separate_and_snapshot_keeps_fixed_version(client, monkeypatch):
    pid = client.post("/api/projects", json={"name": "固定资产版本论文"}).json()["project"]["id"]
    stream = io.BytesIO(); Image.new("RGB", (40, 30), "red").save(stream, "PNG")
    body = client.post(f"/api/projects/{pid}/drawing/assets/import", json={"filename": "figure.png", "request_id": "asset", "content_base64": base64.b64encode(stream.getvalue()).decode()}).json()
    aid, v = body["asset"]["id"], body["version"]
    selected = client.post(f"/api/projects/{pid}/drawing/assets/{aid}/select-for-editor", json={"version_id": v["id"]})
    assert selected.status_code == 200 and not selected.json()["body_written"]
    complete_planning(client, pid)
    workflow = client.get(f"/api/projects/{pid}/workflow").json()
    started = client.post(f"/api/projects/{pid}/workflow/commands", json={"command": "start", "request_id": "start", "base_version": workflow["version"]}).json()
    assert client.post(f"/api/projects/{pid}/human-revision", json={"workflow_version": started["version"]}).status_code == 200
    main = next(f for f in client.get(f"/api/projects/{pid}").json()["files"] if f["path"] == "main.tex")
    assert v["build_name"] not in main["content"]
    inserted = client.post(f"/api/projects/{pid}/drawing/assets/{aid}/insert-into-editor", json={
        "version_id": v["id"], "request_id": "insert", "target_path": "main.tex", "base_file_version": main["version"], "caption": "图像结果"})
    assert inserted.status_code == 200, inserted.text
    snapshot = client.post(f"/api/projects/{pid}/build-snapshots", json={"request_id": "fixed-build"})
    assert snapshot.status_code == 200, snapshot.text
    snap = snapshot.json()
    assert snap["manifest"]["assets"][0]["version_id"] == v["id"]
    stream = io.BytesIO(); Image.new("RGB", (40, 30), "blue").save(stream, "PNG")
    replacement = client.post(f"/api/projects/{pid}/drawing/assets/{aid}/versions", json={"filename": "figure.png", "request_id": "new-version", "expected_version": 1, "content_base64": base64.b64encode(stream.getvalue()).decode()})
    assert replacement.status_code == 200
    read = client.get(f"/api/projects/{pid}/build-snapshots/{snap['id']}").json()
    assert read["manifest"]["assets"] == snap["manifest"]["assets"]
    assert client.delete(f"/api/projects/{pid}/drawing/assets/{aid}/versions/{v['id']}").json()["detail"]["code"] == "asset_referenced"
    conn = app.db()
    path = visual_assets.checked_file(conn, app.PROJECTS_DIR, pid, v["id"])
    conn.close()
    original_hash = visual_assets.digest(path.read_bytes())
    rendered = []
    def fake_compile(files, project_dir, clean_path, work_dir, **kwargs):
        rendered.extend(files)
        Path(work_dir).mkdir(parents=True, exist_ok=True)
        (Path(work_dir) / "main.pdf").write_bytes(b"pdf")
        return {"ok": True, "log": ""}
    monkeypatch.setattr(app, "compile_project_files", fake_compile)
    compiled = client.post(f"/api/projects/{pid}/compile", json={"snapshot_id": snap["id"], "request_id": "compile-fixed"})
    assert compiled.status_code == 200, compiled.text
    image_row = next(f for f in rendered if f["path"] == v["build_name"])
    assert visual_assets.digest(image_row["content_bytes"]) == original_hash
    path.write_bytes(b"tampered")
    rendered.clear()
    again = client.post(f"/api/projects/{pid}/compile", json={"snapshot_id": snap["id"], "request_id": "compile-corrupt"})
    assert again.status_code == 200, again.text
    assert not rendered  # Integrity failure prevents compiler execution.
    finished = client.get(f"/api/projects/{pid}/builds/{again.json()['build_job']['id']}").json()
    assert finished["status"] == "failed"
    assert finished["diagnostics"][0]["code"] == "asset_content_missing"
