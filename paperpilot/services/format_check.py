from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from typing import Any, Iterable

from paperpilot.services import settings
from paperpilot.services.content_versions import version_map


_CITE_COMMAND = re.compile(r"\\cite(?:[A-Za-z]*)")
_BEGIN_DOCUMENT = re.compile(r"\\begin\s*\{document\}")
_END_DOCUMENT = re.compile(r"\\end\s*\{document\}")


def _line_for_offset(content: str, offset: int) -> int:
    return content.count("\n", 0, max(0, offset)) + 1


def _warning(code: str, message: str, path: str, content: str, offset: int = 0, *, severity: str = "warning") -> dict[str, Any]:
    line = _line_for_offset(content, offset)
    return {
        "code": code,
        "message": message,
        "path": path,
        "line": line,
        "column": offset - content.rfind("\n", 0, offset),
        "severity": severity,
        "blocking": False,
    }


def _brace_warnings(path: str, content: str) -> list[dict[str, Any]]:
    # A small deterministic check that is useful even when pdflatex is not
    # installed.  Escaped braces are presentation text and do not affect the
    # balance counter.
    depth = 0
    first_unmatched_close: int | None = None
    for match in re.finditer(r"(?<!\\)[{}]", content):
        if match.group(0) == "{":
            depth += 1
        elif depth:
            depth -= 1
        elif first_unmatched_close is None:
            first_unmatched_close = match.start()
    warnings: list[dict[str, Any]] = []
    if first_unmatched_close is not None:
        warnings.append(_warning("unmatched_closing_brace", "发现未匹配的右花括号。", path, content, first_unmatched_close))
    if depth:
        warnings.append(_warning("unclosed_brace", f"有 {depth} 个左花括号未闭合。", path, content, len(content)))
    return warnings


def _file_warnings(path: str, content: str, *, is_main: bool) -> list[dict[str, Any]]:
    warnings = _brace_warnings(path, content)
    if is_main or path.lower().endswith("main.tex"):
        if not _BEGIN_DOCUMENT.search(content):
            warnings.append(_warning("missing_document_begin", "主文档缺少 \\begin{document}。", path, content))
        if not _END_DOCUMENT.search(content):
            warnings.append(_warning("missing_document_end", "主文档缺少 \\end{document}。", path, content, len(content)))
    for match in _CITE_COMMAND.finditer(content):
        tail = content[match.end():]
        if not re.match(r"\s*\{", tail):
            warnings.append(_warning("malformed_citation_command", "引用命令后缺少花括号参数。", path, content, match.start()))
    return warnings


def analyze(
    files: Iterable[dict[str, Any]],
    effective_settings: dict[str, Any],
    *,
    target_path: str | None = None,
    draft_content: str | None = None,
) -> dict[str, Any]:
    """Return location-aware, non-blocking format warnings.

    The checker deliberately has no write path and never gates saving, AI
    requests, or LaTeX compilation.  The effective settings are echoed so a
    caller can prove which paper-level configuration was used.
    """
    source_files = [dict(item) for item in files]
    if target_path:
        source_files = [item for item in source_files if item.get("path") == target_path]
        if not source_files and draft_content is not None:
            source_files = [{"path": target_path, "content": draft_content, "version": None}]
        elif source_files and draft_content is not None:
            # A supplied draft is the explicit check target; never silently
            # fall back to the persisted file contents.
            source_files = [{**source_files[0], "content": draft_content}]
    elif draft_content is not None:
        source_files = [{"path": "__draft__.tex", "content": draft_content, "version": None}]

    warnings: list[dict[str, Any]] = []
    checked_files: list[dict[str, Any]] = []
    for item in source_files:
        path = str(item.get("path") or "")
        content = str(item.get("content") or "")
        checked_files.append({"path": path, "version": item.get("version"), "content_sha256": __import__("hashlib").sha256(content.encode("utf-8")).hexdigest()})
        warnings.extend(_file_warnings(path, content, is_main=path == "main.tex" or len(source_files) == 1 and path.endswith(".tex")))

    return {
        "checked_files": checked_files,
        "warnings": warnings,
        "summary": {"file_count": len(checked_files), "warning_count": len(warnings), "blocking_count": 0},
        "policy": str(effective_settings.get("format_policy") or "warn_only"),
        "non_blocking": True,
        "effective_settings": dict(effective_settings),
    }


def dependencies(conn: sqlite3.Connection, project_id: str, target_path: str | None = None) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT path,content FROM files WHERE project_id=? ORDER BY path",
        (project_id,),
    ).fetchall()
    versions = version_map(conn, project_id)
    files = [
        {
            "path": row["path"],
            "version": versions.get(row["path"], 0),
            "sha256": hashlib.sha256(str(row["content"]).encode("utf-8")).hexdigest(),
        }
        for row in rows
        if target_path is None or row["path"] == target_path
    ]
    view = settings.read(conn, project_id)
    config = {
        "effective": view.get("effective", {}),
        "baseline_version": (view.get("baseline") or {}).get("version"),
        "domain_version": (view.get("baseline") or {}).get("domain_version"),
        "override_versions": {
            key: value.get("version")
            for key, value in (view.get("overrides") or {}).items()
        },
    }
    terms = [
        {
            "id": row["id"],
            "version": int(row["version"] or 0),
            "status": row["status"],
            "term_sha256": hashlib.sha256(str(row["term"]).encode("utf-8")).hexdigest(),
        }
        for row in conn.execute(
            "SELECT id,version,status,term FROM terms WHERE project_id=? ORDER BY id",
            (project_id,),
        )
    ]
    project = conn.execute(
        "SELECT project_revision FROM projects WHERE id=?",
        (project_id,),
    ).fetchone()
    parts = {
        "files": files,
        "settings": config,
        "terms": terms,
    }
    fingerprint = hashlib.sha256(
        json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "fingerprint": fingerprint,
        **parts,
        "project_revision": int(project["project_revision"]) if project else 0,
    }
