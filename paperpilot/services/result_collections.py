"""Project-scoped indexes of result tables, source originals and paper figures."""
from __future__ import annotations

import json
import sqlite3

from paperpilot.services import visual_assets
from paperpilot.services.project_errors import ProjectServiceError


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS result_collections (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            id TEXT NOT NULL, manifest TEXT NOT NULL,
            PRIMARY KEY(project_id, id)
        );
        CREATE TABLE IF NOT EXISTS result_collection_sources (
            project_id TEXT NOT NULL, collection_id TEXT NOT NULL, id TEXT NOT NULL,
            filename TEXT NOT NULL, sha256 TEXT NOT NULL, content BLOB NOT NULL,
            PRIMARY KEY(project_id, collection_id, id),
            FOREIGN KEY(project_id, collection_id) REFERENCES result_collections(project_id, id) ON DELETE CASCADE
        );
    """)


def list_collections(conn, project_id: str) -> list[dict]:
    rows = conn.execute("SELECT id,manifest FROM result_collections WHERE project_id=? ORDER BY id", (project_id,))
    return [json.loads(row["manifest"]) | {"id": row["id"]} for row in rows]


def read_source(conn, project_id: str, collection_id: str, source_id: str) -> tuple[str, bytes]:
    row = conn.execute(
        "SELECT filename,sha256,content FROM result_collection_sources WHERE project_id=? AND collection_id=? AND id=?",
        (project_id, collection_id, source_id),
    ).fetchone()
    if row is None:
        raise ProjectServiceError(404, "resource_missing", "原始文件不存在于当前论文的结果集合。")
    if visual_assets.digest(row["content"]) != row["sha256"]:
        raise ProjectServiceError(409, "result_content_mismatch", "原始文件的校验值不符，无法读取。")
    return row["filename"], row["content"]
