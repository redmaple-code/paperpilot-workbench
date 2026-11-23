from __future__ import annotations

import re
import sqlite3
from typing import Any

from paperpilot.services.conversion import bib_keys


_CITATION = re.compile(r"\\(?:cite|citep|citet|citeauthor|citeyear|nocite)(?:\[[^\]]*\]){0,2}\{([^}]+)\}")
_ENVIRONMENT = re.compile(r"\\(begin|end)\s*\{([^}]+)\}")


def _without_latex_comments(source: str) -> str:
    lines = []
    for line in source.splitlines():
        visible = []
        escaped = False
        for character in line:
            if character == "%" and not escaped:
                break
            visible.append(character)
            if character == "\\":
                escaped = not escaped
            else:
                escaped = False
        lines.append("".join(visible))
    return "\n".join(lines)


def _validate_tex(path: str, content: str) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    text = _without_latex_comments(content)
    depth = 0
    escaped = False
    for index, character in enumerate(text):
        if character == "\\":
            escaped = not escaped
            continue
        if character == "{" and not escaped:
            depth += 1
        elif character == "}" and not escaped:
            depth -= 1
            if depth < 0:
                issues.append({"code": "unmatched_closing_brace", "message": f"{path} 有多余的右花括号。"})
                depth = 0
                break
        escaped = False
    if depth > 0:
        issues.append({"code": "unclosed_brace", "message": f"{path} 有 {depth} 个未闭合的左花括号。"})

    environments: list[str] = []
    for match in _ENVIRONMENT.finditer(text):
        direction, name = match.groups()
        name = name.strip()
        if direction == "begin":
            environments.append(name)
        elif not environments or environments[-1] != name:
            issues.append({"code": "mismatched_environment", "message": f"{path} 的 LaTeX 环境 {name} 没有匹配的开始标签。"})
            break
        else:
            environments.pop()
    if environments:
        issues.append({"code": "unclosed_environment", "message": f"{path} 的环境 {environments[-1]} 未闭合。"})
    if path.replace("\\", "/").rsplit("/", 1)[-1] == "main.tex":
        if not re.search(r"\\documentclass(?:\[[^\]]*\])?\{[^}]+\}", text):
            issues.append({"code": "missing_documentclass", "message": "main.tex 缺少有效的 \\documentclass 声明。"})
        if not re.search(r"\\begin\s*\{document\}", text) or not re.search(r"\\end\s*\{document\}", text):
            issues.append({"code": "missing_document_environment", "message": "main.tex 必须包含完整的 document 环境。"})
    return issues


def validate_candidate(
    conn: sqlite3.Connection,
    project_id: str,
    patches: list[dict[str, Any]],
) -> dict[str, list[dict[str, str]]]:
    hard_errors: list[dict[str, str]] = []
    cited: set[str] = set()
    known_refs = {
        str(row["cite_key"])
        for row in conn.execute("SELECT cite_key FROM refs WHERE project_id=?", (project_id,))
    }
    proposed_files = {
        item["path"]: item["content"]
        for item in patches
        if item["kind"] == "file"
    }
    existing_bib = conn.execute("SELECT content FROM files WHERE project_id=? AND path='references.bib'", (project_id,)).fetchone()
    bib_source = proposed_files.get("references.bib", existing_bib["content"] if existing_bib else "")
    known_refs.update(bib_keys(bib_source))

    for patch in patches:
        if patch["kind"] != "file":
            continue
        path = str(patch["path"])
        content = str(patch["content"])
        if path.lower().endswith((".tex", ".ltx")):
            hard_errors.extend(_validate_tex(path, content))
            for match in _CITATION.finditer(_without_latex_comments(content)):
                cited.update(key.strip() for key in match.group(1).split(",") if key.strip() and key.strip() != "*")
    missing = sorted(cited - known_refs)
    warnings = []
    if missing:
        warnings.append({
            "code": "missing_citations",
            "message": "候选引用了当前参考文献库中不存在的 cite key：" + ", ".join(missing),
        })
    return {"hard_errors": hard_errors, "warnings": warnings}
