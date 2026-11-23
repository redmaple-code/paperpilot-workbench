"""Build and commit an explicitly reviewed figure insertion."""
import re

from paperpilot.services import editor, visual_assets as assets
from paperpilot.services.content_versions import current_version
from paperpilot.services.project_errors import ProjectServiceError

HEADINGS = {"section": 1, "subsection": 2, "subsubsection": 3, "paragraph": 4}
HEADING = re.compile(r"(?m)^[ \t]*\\(section|subsection|subsubsection|paragraph)\*?(?:\[[^\]\n]*\])?\{([^{}\n]+)\}")


def section_end(text: str, title: str) -> int:
    document_end = text.rfind(r"\end{document}")
    stop = document_end if document_end >= 0 else len(text)
    if not title:
        return stop
    headings = list(HEADING.finditer(text, 0, stop))
    positions = [i for i, m in enumerate(headings) if m[2].strip() == title]
    if len(positions) != 1:
        raise ProjectServiceError(409, "section_changed", "指定小节已变化或有同名小节，请重新预览。")
    index = positions[0]
    rank = HEADINGS[headings[index][1]]
    return next((m.start() for m in headings[index + 1:] if HEADINGS[m[1]] <= rank), stop)


def insert(conn, storage_root, project_id, asset_id, payload, *, section=""):
    data = payload.model_dump()
    if section:
        data["section"] = section
    request_fp = assets.fingerprint({"op": "insert", **data, "asset_id": asset_id})
    prior = assets.replay(conn, project_id, payload.request_id, request_fp)
    if prior:
        return prior
    row = assets.version(conn, project_id, payload.version_id)
    if row["asset_id"] != asset_id or assets.asset(conn, project_id, asset_id)["status"] != "active":
        raise ProjectServiceError(409, "asset_unavailable", "所选资产不可用于新的正文引用。")
    assets.checked_file(conn, storage_root, project_id, row["id"])
    path = payload.target_path
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    prior = assets.replay(conn, project_id, payload.request_id, request_fp)
    if prior:
        return prior
    if conn.execute("SELECT 1 FROM revision_drafts WHERE project_id=? AND target_kind='file' AND target_path=?", (project_id, path)).fetchone():
        raise ProjectServiceError(409, "draft_checkpoint_required", "该文件有待审草稿，请先处理草稿。")
    current = conn.execute("SELECT content FROM files WHERE project_id=? AND path=?", (project_id, path)).fetchone()
    if current is None:
        raise ProjectServiceError(404, "file_missing", "目标正文文件不存在。")
    if current_version(conn, project_id, "file", path) != payload.base_file_version:
        raise ProjectServiceError(409, "editor_version_conflict", "正文已变化，请重新预览后插入。")
    replacements = {"\\": r"\textbackslash{}", "{": r"\{", "}": r"\}", "$": r"\$", "&": r"\&", "#": r"\#", "%": r"\%", "_": r"\_", "^": r"\textasciicircum{}", "~": r"\textasciitilde{}"}
    snippet = "\n\\begin{figure}[htbp]\n\\centering\n\\includegraphics[width=\\linewidth]{" + row["build_name"] + "}\n"
    if payload.caption:
        caption = re.sub(r"[\\{}$&#%_^~]", lambda m: replacements[m.group()], payload.caption)
        snippet += "\\caption{" + caption + "}\n"
    snippet += "\\end{figure}\n"
    text = str(current["content"])
    where = section_end(text, section)
    content = text[:where] + snippet + text[where:]
    if not re.search(r"\\usepackage(?:\[[^\]]*\])?\{[^}]*\bgraphicx\b[^}]*\}", content):
        marker = content.find("\\begin{document}")
        if marker >= 0:
            content = content[:marker] + "\\usepackage{graphicx}\n" + content[marker:]
    result = editor.save(conn, project_id, path, content, payload.base_file_version, "asset:" + payload.request_id)
    assets.add_reference(conn, project_id, row["id"], "editor", path)
    conn.execute("UPDATE projects SET project_revision=project_revision+1 WHERE id=?", (project_id,))
    return assets.remember(conn, project_id, payload.request_id, "insert", request_fp,
                           {**result, "asset_version_id": row["id"], "inserted": True})
