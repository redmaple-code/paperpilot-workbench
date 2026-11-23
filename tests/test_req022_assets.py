from __future__ import annotations

import base64
import io
import os
import time
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfWriter

import app
from paperpilot.services import visual_assets


def project(client, name="资产论文"):
    r = client.post("/api/projects", json={"name": name, "domain": "材料", "style": "严谨"})
    assert r.status_code == 200, r.text
    return r.json()["project"]["id"]


def png(color="red"):
    stream = io.BytesIO()
    Image.new("RGB", (30, 20), color).save(stream, "PNG")
    return stream.getvalue()


def upload(client, pid, content=None, filename="sample.png", request_id="upload", **extra):
    return client.post(f"/api/projects/{pid}/drawing/assets/import", json={"filename": filename,
        "content_base64": base64.b64encode(png() if content is None else content).decode(), "request_id": request_id, **extra})


def test_managed_upload_preview_and_project_scoped_idempotency(client):
    a, b = project(client), project(client, "第二篇")
    first = upload(client, a)
    assert first.status_code == 200, first.text
    body = first.json()
    v = body["version"]
    assert v["version_no"] == 1 and v["source_sha256"]
    assert "managed_path" not in v and "preview_path" not in v
    preview = client.get(v["preview_url"])
    assert preview.status_code == 200 and preview.content.startswith(b"\x89PNG")
    replay = upload(client, a).json()
    assert replay["idempotent_replay"] and replay["version"]["id"] == v["id"]
    assert upload(client, a, png("blue")).status_code == 409
    assert upload(client, b).status_code == 200  # Same request_id is scoped to a project.
    assert client.get(f"/api/projects/{b}/drawing/assets/{body['asset']['id']}").status_code == 404
    assert client.get(v["preview_url"].replace(a, b)).status_code == 404


def test_replacement_conflict_archive_and_historical_preview(client):
    pid = project(client)
    first = upload(client, pid).json()
    aid, old = first["asset"]["id"], first["version"]
    payload = {"filename": "blue.png", "content_base64": base64.b64encode(png("blue")).decode(), "request_id": "replace", "expected_version": 1}
    replaced = client.post(f"/api/projects/{pid}/drawing/assets/{aid}/versions", json=payload)
    assert replaced.status_code == 200, replaced.text
    assert replaced.json()["version"]["version_no"] == 2
    payload["request_id"] = "stale-replace"
    assert client.post(f"/api/projects/{pid}/drawing/assets/{aid}/versions", json=payload).status_code == 409
    assert client.get(old["preview_url"]).content == client.get(first["version"]["preview_url"]).content
    archived = client.post(f"/api/projects/{pid}/drawing/assets/{aid}/archive", json={"request_id": "archive", "expected_revision": 2})
    assert archived.status_code == 200, archived.text
    assert client.get(f"/api/projects/{pid}/drawing/assets").json()["assets"] == []
    assert client.get(old["preview_url"]).status_code == 200
    restored = client.post(f"/api/projects/{pid}/drawing/assets/{aid}/restore", json={"request_id": "restore", "expected_revision": 3})
    assert restored.status_code == 200 and restored.json()["asset"]["status"] == "active"


def test_explicit_copy_creates_independent_lineage(client):
    a, b = project(client), project(client, "目标论文")
    original = upload(client, a).json()
    copied = client.post(f"/api/projects/{b}/drawing/assets/copy", json={"source_project_id": a,
        "source_version_id": original["version"]["id"], "request_id": "copy"})
    assert copied.status_code == 200, copied.text
    body = copied.json()
    assert body["asset"]["id"] != original["asset"]["id"]
    assert body["version"]["source_asset_version"] == original["version"]["id"]
    assert body["version"]["origin_type"] == "copy"
    changed = client.post(f"/api/projects/{a}/drawing/assets/{original['asset']['id']}/versions", json={
        "filename": "new.png", "content_base64": base64.b64encode(png("blue")).decode(), "request_id": "source-change", "expected_version": 1})
    assert changed.status_code == 200
    target = client.get(f"/api/projects/{b}/drawing/assets/{body['asset']['id']}").json()
    assert target["current_version"]["sha256"] == original["version"]["sha256"]


def test_pdf_requires_explicit_page_and_renders_only_selected_page(client):
    pid = project(client)
    stream = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=80)
    writer.add_blank_page(width=200, height=120)
    writer.write(stream)
    content = stream.getvalue()
    assert upload(client, pid, content, "paper.pdf").json()["detail"]["code"] == "page_selection_required"
    selected = upload(client, pid, content, "paper.pdf", "page-2", page_no=2)
    assert selected.status_code == 200, selected.text
    v = selected.json()["version"]
    assert v["page_no"] == 2 and v["page_count"] == 2 and v["width"] > 300
    assert client.get(v["preview_url"]).status_code == 200
    assert upload(client, pid, content, "paper.pdf", "bad-page", page_no=3).status_code == 422


@pytest.mark.parametrize("svg", [
    b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><rect onload="x()"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><image href="https://example.com/x"/></svg>',
    b'<svg xmlns="http://www.w3.org/2000/svg"><style>@import "https://example.com/x";</style></svg>',
    b'<!DOCTYPE svg [<!ENTITY x SYSTEM "file:///secret">]><svg>&x;</svg>',
])
def test_unsafe_svg_rejected_without_creating_asset(client, svg):
    pid = project(client)
    r = upload(client, pid, svg, "unsafe.svg")
    assert r.status_code == 422, r.text
    assert client.get(f"/api/projects/{pid}/drawing/assets").json()["assets"] == []


def test_svg_has_real_png_preview_and_pdf_build_representation(client):
    pid = project(client)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80"><rect width="120" height="80" fill="red"/></svg>'
    r = upload(client, pid, svg, "safe.svg")
    assert r.status_code == 200, r.text
    v = r.json()["version"]
    assert v["build_name"].endswith(".pdf")
    image = Image.open(io.BytesIO(client.get(v["preview_url"]).content))
    red, green, blue = image.convert("RGB").getpixel((image.width // 2, image.height // 2))
    assert red > 200 and green < 50 and blue < 50


def test_chinese_svg_labels_are_embedded_in_build_pdf(client):
    pid = project(client)
    svg = '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="100"><text x="20" y="55" font-family="Helvetica" font-size="24">方法与结果</text></svg>'.encode()
    response = upload(client, pid, svg, "chinese.svg")
    assert response.status_code == 200, response.text
    conn = app.db()
    try:
        path = visual_assets.checked_file(conn, app.PROJECTS_DIR, pid, response.json()["version"]["id"])
        from pypdf import PdfReader
        pdf = PdfReader(path)
        assert "方法与结果" in pdf.pages[0].extract_text()
        fonts = pdf.pages[0]["/Resources"]["/Font"]
        assert any(font.get_object().get("/FontDescriptor", {}).get("/FontFile2") for font in fonts.values())
    finally:
        conn.close()


def test_file_failure_never_advances_asset_pointer(client, monkeypatch):
    pid = project(client)
    old = upload(client, pid).json()
    original = Path.write_bytes
    def failing_write(path, data):
        if path.name == "preview.png.tmp":
            raise OSError("disk full")
        return original(path, data)
    monkeypatch.setattr(Path, "write_bytes", failing_write)
    failed = client.post(f"/api/projects/{pid}/drawing/assets/{old['asset']['id']}/versions", json={"filename": "next.png",
            "content_base64": base64.b64encode(png("blue")).decode(), "expected_version": 1, "request_id": "failed-save"})
    assert failed.status_code == 503 and failed.json()["detail"]["code"] == "asset_storage_failed"
    current = client.get(f"/api/projects/{pid}/drawing/assets/{old['asset']['id']}").json()
    assert current["current_version_id"] == old["version"]["id"] and len(current["versions"]) == 1


def test_fake_extension_and_corrupt_image_rejected(client):
    pid = project(client)
    assert upload(client, pid, png(), "fake.svg").status_code == 422
    assert upload(client, pid, b"not-an-image", "fake.png").status_code == 422


def test_recovery_cleans_only_old_orphans_and_reports_missing_fixed_version(client):
    pid = project(client)
    body = upload(client, pid).json()
    conn = app.db()
    path = visual_assets.checked_file(conn, app.PROJECTS_DIR, pid, body["version"]["id"])
    conn.close()
    orphan = app.PROJECTS_DIR / pid / "assets" / "interrupted" / "unknown-version" / "source.png.tmp"
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"incomplete")
    old = time.time() - 7200
    os.utime(orphan, (old, old))
    young = orphan.parent / "active-upload.tmp"
    young.write_bytes(b"writing")
    path.unlink()
    report = client.get(f"/api/projects/{pid}/drawing/storage-check")
    assert report.status_code == 200 and report.json()["damaged"]
    assert orphan.exists()
    recovered = client.post(f"/api/projects/{pid}/drawing/storage-recovery")
    assert recovered.status_code == 200, recovered.text
    assert not orphan.exists() and young.exists()
    assert not recovered.json()["current_versions_changed"]
    current = client.get(f"/api/projects/{pid}/drawing/assets/{body['asset']['id']}").json()
    assert current["current_version_id"] == body["version"]["id"]
