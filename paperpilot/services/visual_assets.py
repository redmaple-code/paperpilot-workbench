from __future__ import annotations

import base64
import hashlib
import io
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import uuid
import warnings
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from PIL import Image, UnidentifiedImageError
from pypdf import PdfReader

from paperpilot.services.project_errors import ProjectServiceError

MAX_BYTES = 20 * 1024 * 1024
MAX_PIXELS = 16_000_000
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
GENERATED_KINDS = {"structured_svg", "scene_graph", "ai_image"}
GRAPHICS = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}")


def stamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS figure_assets (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id),
            title TEXT NOT NULL, kind TEXT NOT NULL, current_version_id TEXT,
            status TEXT NOT NULL DEFAULT 'active', revision INTEGER NOT NULL DEFAULT 1,
            source_asset_version TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS figure_asset_versions (
            id TEXT PRIMARY KEY, asset_id TEXT NOT NULL REFERENCES figure_assets(id),
            project_id TEXT NOT NULL REFERENCES projects(id), version_no INTEGER NOT NULL,
            sha256 TEXT NOT NULL, source_sha256 TEXT NOT NULL, mime_type TEXT NOT NULL,
            filename TEXT NOT NULL, managed_path TEXT NOT NULL, preview_path TEXT NOT NULL,
            build_path TEXT NOT NULL, build_name TEXT NOT NULL, build_sha256 TEXT NOT NULL,
            width INTEGER NOT NULL, height INTEGER NOT NULL, byte_size INTEGER NOT NULL,
            origin_type TEXT NOT NULL, origin_ref TEXT NOT NULL, page_no INTEGER,
            page_count INTEGER, run_id TEXT, editable_source TEXT NOT NULL DEFAULT '',
            source_asset_version TEXT, status TEXT NOT NULL DEFAULT 'ready', created_at TEXT NOT NULL,
            UNIQUE(asset_id, version_no), UNIQUE(asset_id, sha256)
        );
        CREATE TABLE IF NOT EXISTS asset_operations (
            project_id TEXT NOT NULL, request_id TEXT NOT NULL, operation TEXT NOT NULL,
            fingerprint TEXT NOT NULL, result_json TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(project_id, request_id)
        );
        CREATE TABLE IF NOT EXISTS asset_references (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, asset_id TEXT NOT NULL,
            version_id TEXT NOT NULL REFERENCES figure_asset_versions(id),
            consumer_kind TEXT NOT NULL, consumer_id TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(project_id,version_id,consumer_kind,consumer_id)
        );
        CREATE INDEX IF NOT EXISTS idx_figure_assets_project ON figure_assets(project_id, status);
        CREATE INDEX IF NOT EXISTS idx_figure_versions_asset ON figure_asset_versions(asset_id,version_no);
    """)


def validate_svg(content: bytes) -> None:
    if re.search(br"<!\s*(?:DOCTYPE|ENTITY)|<\?xml-stylesheet", content, re.I):
        raise ProjectServiceError(422, "unsafe_svg", "SVG 不允许文档类型或实体声明。")
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError) as exc:
        raise ProjectServiceError(422, "invalid_format", "SVG 内容无法解析。") from exc
    if root.tag.rsplit("}", 1)[-1].lower() != "svg":
        raise ProjectServiceError(422, "invalid_format", "文件不是有效 SVG。")
    nodes = list(root.iter())
    if len(nodes) > 5000:
        raise ProjectServiceError(422, "image_too_large", "SVG 节点过多，请简化图形。")
    blocked = {"script", "foreignobject", "iframe", "object", "embed", "image", "audio", "video", "animate", "set"}
    for node in nodes:
        if node.tag.rsplit("}", 1)[-1].lower() in blocked:
            raise ProjectServiceError(422, "unsafe_svg", "SVG 包含脚本、外部图像或不支持的活动内容。")
        for key, value in node.attrib.items():
            name = key.rsplit("}", 1)[-1].lower()
            if name.startswith("on") or (name in {"href", "src"} and not value.startswith("#")):
                raise ProjectServiceError(422, "unsafe_svg", "SVG 不能包含事件处理器或外部引用。")
            urls = re.findall(r"url\s*\((.*?)\)", value, flags=re.I)
            if any(not url.strip(" \t\r\n'\"").startswith("#") for url in urls) or re.search(r"javascript:|@import|expression\s*\(", value, re.I):
                raise ProjectServiceError(422, "unsafe_svg", "SVG 样式包含不安全资源。")
        if node.tag.rsplit("}", 1)[-1].lower() == "style":
            css = node.text or ""
            if re.search(r"@import|javascript:|expression\s*\(", css, re.I) or any(
                not url.strip(" \t\r\n'\"").startswith("#") for url in re.findall(r"url\s*\((.*?)\)", css, re.I)
            ):
                raise ProjectServiceError(422, "unsafe_svg", "SVG 样式不能加载外部资源。")


def decode_upload(value: str) -> bytes:
    if len(value) > ((MAX_BYTES + 2) // 3) * 4 + 8:
        raise ProjectServiceError(413, "file_too_large", "图像文件不能超过 20 MB。")
    try:
        result = base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ProjectServiceError(422, "invalid_format", "文件编码无效。") from exc
    if not result or len(result) > MAX_BYTES:
        raise ProjectServiceError(413, "file_too_large", "文件为空或超过 20 MB。")
    return result


def prepare(content: bytes, filename: str, page_no: int | None) -> dict[str, Any]:
    """Validate and render before acquiring a database write transaction."""
    if not content or len(content) > MAX_BYTES:
        raise ProjectServiceError(413, "file_too_large", "文件为空或超过 20 MB。")
    is_pdf = content.startswith(b"%PDF-")
    is_svg = content.lstrip().startswith((b"<svg", b"<?xml", b"<!--"))
    suffix = Path(filename).suffix.lower()
    if (is_pdf and suffix != ".pdf") or (is_svg and suffix != ".svg"):
        raise ProjectServiceError(422, "invalid_format", "文件扩展名与实际内容不一致。")
    if is_svg:
        validate_svg(content)
    if is_pdf or is_svg:
        page_count = 1
        if is_pdf:
            try:
                reader = PdfReader(io.BytesIO(content))
                if reader.is_encrypted:
                    raise ValueError("encrypted")
                page_count = len(reader.pages)
            except Exception as exc:
                raise ProjectServiceError(422, "invalid_format", "PDF 损坏或受密码保护。") from exc
            if not 1 <= page_count <= 200:
                raise ProjectServiceError(422, "invalid_page", "PDF 页数必须在 1～200 页。")
            if page_count > 1 and page_no is None:
                raise ProjectServiceError(422, "page_selection_required", f"PDF 有 {page_count} 页，请明确选择页码。")
            if not 1 <= (page_no or 1) <= page_count:
                raise ProjectServiceError(422, "invalid_page", "所选 PDF 页码不存在。")
        elif page_no is not None:
            raise ProjectServiceError(422, "invalid_page", "只有 PDF 需要选择页码。")
        with tempfile.TemporaryDirectory(prefix="paperpilot-visual-") as temporary:
            folder = Path(temporary)
            source, pdf, preview = folder / "source.bin", folder / "page.pdf", folder / "preview.png"
            source.write_bytes(content)
            command = [sys.executable, "-X", "utf8", "-m", "paperpilot.adapters.visual_document", "pdf" if is_pdf else "svg", str(source), str(pdf), str(preview), "--page", str(page_no or 1)]
            try:
                result = subprocess.run(command, capture_output=True, timeout=20, cwd=Path(__file__).resolve().parents[2], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ProjectServiceError(503, "conversion_unavailable", "图像转换超时或不可用；原资产保持不变。") from exc
            if result.returncode or not pdf.exists() or not preview.exists():
                if b"cjk_font_unavailable" in (result.stderr or b""):
                    raise ProjectServiceError(503, "conversion_unavailable", "当前机器缺少可嵌入的中文字体；请安装中文 TrueType 字体或配置 PWA_VISUAL_CJK_FONT 后重试，原资产保持不变。")
                raise ProjectServiceError(503, "conversion_unavailable", "图像转换未完成；请检查文件或转换环境。")
            with Image.open(preview) as image:
                width, height = image.size
            return {"original": content, "content": pdf.read_bytes() if is_pdf else content,
                    "build": pdf.read_bytes(), "preview": preview.read_bytes(), "mime": "application/pdf" if is_pdf else "image/svg+xml",
                    "extension": ".pdf" if is_pdf else ".svg", "build_extension": ".pdf", "width": width, "height": height,
                    "page_no": (page_no or 1) if is_pdf else None, "page_count": page_count if is_pdf else None,
                    "kind": "pdf_page" if is_pdf else "imported_image"}
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(content)) as image:
                if image.format not in {"PNG", "JPEG", "WEBP"} or image.width * image.height > MAX_PIXELS:
                    raise ValueError("format_or_pixels")
                if suffix not in {"PNG": {".png"}, "JPEG": {".jpg", ".jpeg"}, "WEBP": {".webp"}}[image.format]:
                    raise ValueError("extension_mismatch")
                image.verify()
            with Image.open(io.BytesIO(content)) as image:
                width, height = image.size
                if page_no is not None:
                    raise ProjectServiceError(422, "invalid_page", "只有 PDF 需要选择页码。")
                build_io, preview_io = io.BytesIO(), io.BytesIO()
                image.convert("RGBA").save(build_io, "PNG")
                image.thumbnail((1600, 1600))
                image.convert("RGBA").save(preview_io, "PNG")
        return {"original": content, "content": content, "build": build_io.getvalue(), "preview": preview_io.getvalue(),
                "mime": Image.MIME.get(image.format, "image/png"), "extension": {"JPEG": ".jpg", "WEBP": ".webp"}.get(image.format, ".png"),
                "build_extension": ".png", "width": width, "height": height, "page_no": None, "page_count": None, "kind": "imported_image"}
    except ProjectServiceError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise ProjectServiceError(422, "invalid_format", "文件无效或过大；支持 PNG、JPEG、WebP、安全 SVG 和 PDF。") from exc


def asset(conn: sqlite3.Connection, project_id: str, asset_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM figure_assets WHERE id=? AND project_id=?", (asset_id, project_id)).fetchone()
    if row is None:
        raise ProjectServiceError(404, "resource_missing", "图像资产不存在或不属于当前论文。")
    return row


def version(conn: sqlite3.Connection, project_id: str, version_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM figure_asset_versions WHERE id=? AND project_id=?", (version_id, project_id)).fetchone()
    if row is None:
        raise ProjectServiceError(404, "resource_missing", "图像版本不存在或不属于当前论文。")
    return row


def public_version(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
    result = dict(row)
    for key in ("managed_path", "preview_path", "build_path"):
        result.pop(key, None)
    result["preview_url"] = f"/api/projects/{result['project_id']}/drawing/assets/{result['asset_id']}/versions/{result['id']}/preview"
    result["download_url"] = f"/api/projects/{result['project_id']}/drawing/assets/{result['asset_id']}/versions/{result['id']}/download"
    if result["origin_type"] == "result_chart":
        result["result_source"] = json.loads(result["editable_source"])
    return result


def read(conn: sqlite3.Connection, project_id: str, asset_id: str) -> dict[str, Any]:
    row = asset(conn, project_id, asset_id)
    versions = [public_version(v) for v in conn.execute("SELECT * FROM figure_asset_versions WHERE asset_id=? AND project_id=? ORDER BY version_no DESC", (asset_id, project_id))]
    return {**dict(row), "versions": versions, "current_version": next((v for v in versions if v["id"] == row["current_version_id"]), None)}


def list_assets(conn: sqlite3.Connection, project_id: str, include_archived: bool = False) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT id FROM figure_assets WHERE project_id=? AND (? OR status='active') ORDER BY updated_at DESC", (project_id, include_archived)).fetchall()
    return [read(conn, project_id, r["id"]) for r in rows]


def fingerprint(payload: dict[str, Any]) -> str:
    return digest(compact(payload).encode("utf-8"))


def replay(conn: sqlite3.Connection, project_id: str, request_id: str, fp: str) -> dict[str, Any] | None:
    if not request_id or len(request_id) > 120:
        raise ProjectServiceError(422, "request_id_required", "需要有效 request_id。")
    old = conn.execute("SELECT * FROM asset_operations WHERE project_id=? AND request_id=?", (project_id, request_id)).fetchone()
    if old:
        if old["fingerprint"] != fp:
            raise ProjectServiceError(409, "idempotency_key_reused", "同一 request_id 不能用于不同操作或输入。")
        return json.loads(old["result_json"]) | {"idempotent_replay": True}
    return None


def remember(conn: sqlite3.Connection, project_id: str, request_id: str, operation: str, fp: str, result: dict) -> dict:
    conn.execute("INSERT INTO asset_operations VALUES(?,?,?,?,?,?)", (project_id, request_id, operation, fp, compact(result), stamp()))
    return result


def managed_path(storage_root: Path, project_id: str, relative: str) -> Path:
    base = (storage_root / project_id / "assets").resolve()
    target = (base / relative).resolve()
    if not target.is_relative_to(base):
        raise ProjectServiceError(422, "invalid_path", "资产路径无效。")
    return target


def save_asset(conn: sqlite3.Connection, storage_root: Path, project_id: str, *, content: bytes, filename: str,
               title: str, request_id: str, asset_id: str | None = None, expected_version: int | None = None,
               page_no: int | None = None, origin_ref: str = "", run_id: str | None = None,
               kind: str | None = None, editable_source: str = "", source_asset_version: str | None = None,
               _prepared: dict[str, Any] | None = None, _before_write=None) -> dict[str, Any]:
    if kind in GENERATED_KINDS and not run_id:
        raise ProjectServiceError(422, "run_required", "生成图像必须关联绘图运行。")
    fp = fingerprint({"op": "replace" if asset_id else "import", "content": digest(content), "filename": filename,
                      "title": title, "asset_id": asset_id, "expected_version": expected_version, "page_no": page_no,
                      "origin_ref": origin_ref, "run_id": run_id, "kind": kind, "editable_source": editable_source,
                      "source_asset_version": source_asset_version})
    prior = replay(conn, project_id, request_id, fp)
    if prior:
        return prior
    prepared = _prepared if _prepared is not None else prepare(content, filename, page_no)
    # PDF/image conversion above is outside the SQLite write transaction.
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    prior = replay(conn, project_id, request_id, fp)
    if prior:
        return prior
    sha = digest(prepared["content"])
    now = stamp()
    if asset_id:
        old = asset(conn, project_id, asset_id)
        current = version(conn, project_id, old["current_version_id"])
        if old["status"] != "active":
            raise ProjectServiceError(409, "asset_archived", "请先恢复归档图像。")
        if expected_version is None or current["version_no"] != expected_version:
            raise ProjectServiceError(409, "version_conflict", "资产版本已变化，请刷新后重试。")
        same = conn.execute("SELECT * FROM figure_asset_versions WHERE asset_id=? AND sha256=?", (asset_id, sha)).fetchone()
        if same:
            if old["current_version_id"] != same["id"]:
                conn.execute("UPDATE figure_assets SET current_version_id=?,revision=revision+1,updated_at=? WHERE id=?", (same["id"], now, asset_id))
            result = {"ok": True, "asset": read(conn, project_id, asset_id), "version": public_version(same), "content_reused": True, "new_version": False}
            return remember(conn, project_id, request_id, "replace", fp, result)
        ordinal = conn.execute("SELECT COALESCE(MAX(version_no),0)+1 FROM figure_asset_versions WHERE asset_id=?", (asset_id,)).fetchone()[0]
    else:
        asset_id = uuid.uuid4().hex
        ordinal = 1
        conn.execute("INSERT INTO figure_assets(id,project_id,title,kind,status,source_asset_version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                     (asset_id, project_id, title or Path(filename).stem, kind or prepared["kind"], "active", source_asset_version, now, now))
    vid = uuid.uuid4().hex
    prefix = f"{asset_id}/{vid}"
    paths = {"source": prefix + "/source" + prepared["extension"], "build": prefix + "/build" + prepared["build_extension"], "preview": prefix + "/preview.png"}
    created = []
    try:
        if _before_write is not None:
            _before_write(vid, paths)
        for key, data in [("source", prepared["original"]), ("build", prepared["build"]), ("preview", prepared["preview"])]:
            target = managed_path(storage_root, project_id, paths[key])
            target.parent.mkdir(parents=True, exist_ok=True)
            pending = target.with_suffix(target.suffix + ".tmp")
            pending.write_bytes(data)
            pending.replace(target)
            created.append(target)
        build_name = f"figures/asset_{asset_id}_{vid}{prepared['build_extension']}"
        origin_type = "result_chart" if kind == "result_chart" else (
            "generated" if run_id else "copy" if source_asset_version else "import"
        )
        conn.execute("""INSERT INTO figure_asset_versions(id,asset_id,project_id,version_no,sha256,source_sha256,mime_type,filename,
            managed_path,preview_path,build_path,build_name,build_sha256,width,height,byte_size,origin_type,origin_ref,page_no,page_count,
            run_id,editable_source,source_asset_version,status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (vid, asset_id, project_id, ordinal, sha, digest(content), prepared["mime"], Path(filename).name,
             paths["source"], paths["preview"], paths["build"], build_name, digest(prepared["build"]), prepared["width"], prepared["height"], len(content),
             origin_type, origin_ref, prepared["page_no"], prepared["page_count"], run_id,
             editable_source, source_asset_version, "ready", now))
        conn.execute("UPDATE figure_assets SET current_version_id=?,updated_at=?,revision=revision+? WHERE id=?", (vid, now, int(ordinal > 1), asset_id))
        result = {"ok": True, "asset": read(conn, project_id, asset_id), "version": public_version(version(conn, project_id, vid)), "new_version": True}
        return remember(conn, project_id, request_id, "replace" if expected_version is not None else "import", fp, result)
    except Exception:
        for path in created:
            path.unlink(missing_ok=True)
        raise


def set_archived(conn: sqlite3.Connection, project_id: str, asset_id: str, archived: bool, expected_revision: int, request_id: str) -> dict:
    fp = fingerprint({"op": "archive" if archived else "restore", "id": asset_id, "revision": expected_revision})
    prior = replay(conn, project_id, request_id, fp)
    if prior:
        return prior
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    prior = replay(conn, project_id, request_id, fp)
    if prior:
        return prior
    row = asset(conn, project_id, asset_id)
    if row["revision"] != expected_revision:
        raise ProjectServiceError(409, "version_conflict", "资产状态已变化，请刷新后重试。")
    conn.execute("UPDATE figure_assets SET status=?,revision=revision+1,updated_at=? WHERE id=?", ("archived" if archived else "active", stamp(), asset_id))
    return remember(conn, project_id, request_id, "archive" if archived else "restore", fp, {"ok": True, "asset": read(conn, project_id, asset_id)})


def checked_file(conn: sqlite3.Connection, storage_root: Path, project_id: str, version_id: str, representation: str = "build") -> Path:
    row = version(conn, project_id, version_id)
    key = {"build": "build_path", "preview": "preview_path", "original": "managed_path"}[representation]
    path = managed_path(storage_root, project_id, row[key])
    expected = row["build_sha256"] if representation == "build" else row["source_sha256"] if representation == "original" else None
    if not path.is_file() or (expected and digest(path.read_bytes()) != expected):
        raise ProjectServiceError(409, "asset_content_missing", "受管资产缺失或哈希不符；不会替换为其他版本。")
    return path


def copy_asset(conn: sqlite3.Connection, storage_root: Path, project_id: str, source_project_id: str, source_version_id: str, title: str, request_id: str) -> dict:
    row = version(conn, source_project_id, source_version_id)
    # The API verifies both source read and target write permission before reaching here.
    source = checked_file(conn, storage_root, source_project_id, source_version_id, "original").read_bytes()
    return save_asset(conn, storage_root, project_id, content=source, filename=row["filename"], title=title or row["filename"], request_id=request_id,
                      page_no=row["page_no"], origin_ref=f"project:{source_project_id}/version:{source_version_id}", source_asset_version=source_version_id)


def add_reference(conn: sqlite3.Connection, project_id: str, version_id: str, consumer_kind: str, consumer_id: str) -> None:
    row = version(conn, project_id, version_id)
    conn.execute("INSERT OR IGNORE INTO asset_references VALUES(?,?,?,?,?,?,?)", (uuid.uuid4().hex, project_id, row["asset_id"], version_id, consumer_kind, consumer_id, stamp()))


def capture_manifest(conn: sqlite3.Connection, project_id: str, files: list[dict]) -> list[dict]:
    names = {match for f in files for match in GRAPHICS.findall(str(f.get("content", "")))}
    manifest = []
    for name in sorted(names):
        row = conn.execute("SELECT * FROM figure_asset_versions WHERE project_id=? AND build_name=?", (project_id, name)).fetchone()
        if row is None:
            if name.startswith("figures/asset_"):
                raise ProjectServiceError(409, "asset_reference_missing", "正文引用的固定资产版本不存在。")
            continue  # Legacy figure paths keep their existing behavior.
        manifest.append({"asset_id": row["asset_id"], "version_id": row["id"], "sha256": row["build_sha256"], "path": row["build_name"], "representation": Path(row["build_name"]).suffix.lstrip(".")})
    return manifest


def materialize_manifest(conn: sqlite3.Connection, storage_root: Path, project_id: str, manifest: list[dict], output_dir: Path) -> None:
    for item in manifest:
        row = version(conn, project_id, item["version_id"])
        if row["asset_id"] != item["asset_id"] or row["build_name"] != item["path"] or row["build_sha256"] != item["sha256"]:
            raise ProjectServiceError(409, "asset_manifest_conflict", "构建资产清单与固定版本不匹配。")
        source = checked_file(conn, storage_root, project_id, item["version_id"])
        target = (output_dir / item["path"]).resolve()
        if not target.is_relative_to(output_dir.resolve()):
            raise ProjectServiceError(422, "invalid_path", "构建资产路径无效。")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())


def reconcile_storage(conn: sqlite3.Connection, storage_root: Path, project_id: str, *, cleanup: bool = False, grace_seconds: int = 3600) -> dict:
    """Report damaged immutable files and remove only old, unreferenced staging files."""
    base = managed_path(storage_root, project_id, "")
    rows = conn.execute("SELECT * FROM figure_asset_versions WHERE project_id=?", (project_id,)).fetchall()
    referenced = {managed_path(storage_root, project_id, row[field]) for row in rows for field in ("managed_path", "build_path", "preview_path")}
    damaged = []
    for row in rows:
        for representation in ("original", "build", "preview"):
            try:
                checked_file(conn, storage_root, project_id, row["id"], representation)
            except ProjectServiceError:
                damaged.append({"asset_id": row["asset_id"], "version_id": row["id"], "representation": representation, "code": "asset_content_missing"})
    cutoff = datetime.now(timezone.utc).timestamp() - max(60, grace_seconds)
    orphaned, removed = [], []
    if base.exists():
        for path in base.rglob("*"):
            if not path.is_file() or path.is_symlink():
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(base) or resolved in referenced or path.stat().st_mtime > cutoff:
                continue
            # A version directory absent from the DB is an interrupted save; never delete known-version files.
            if path.parent.name in {row["id"] for row in rows}:
                continue
            relative = path.relative_to(base).as_posix()
            orphaned.append(relative)
            if cleanup:
                path.unlink()
                removed.append(relative)
    return {"damaged": damaged, "orphaned_files": orphaned, "removed_files": removed,
            "cleanup_applied": cleanup, "current_versions_changed": False,
            "next_action": "缺失文件需从受控备份恢复对应版本；不会静默改用其他图像。" if damaged else "受管版本可用。"}
