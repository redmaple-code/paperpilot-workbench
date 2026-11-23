"""Project-scoped result versions and reproducible tabular transformations."""
from __future__ import annotations

import base64
import csv
import io
import json
import re
import sqlite3
import uuid
from collections import defaultdict
from decimal import Decimal, localcontext
from pathlib import PurePosixPath

from paperpilot.services import result_collections, visual_assets
from paperpilot.services.project_errors import ProjectServiceError

MAX_BYTES = 5 * 1024 * 1024
MAX_ROWS = 50_000
MAX_COLUMNS = 100
PREVIEW_ROWS = 30
NUMERIC = re.compile(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]{1,4})?")
FORMAT_ERROR = "请使用带唯一列名的 UTF-8 CSV 或 TSV，每行的列数须一致。"


def initialize_schema(conn: sqlite3.Connection) -> None:
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS result_datasets (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            title TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_result_datasets_project ON result_datasets(project_id);
        CREATE TABLE IF NOT EXISTS result_versions (
            id TEXT PRIMARY KEY, dataset_id TEXT NOT NULL REFERENCES result_datasets(id) ON DELETE CASCADE,
            version_no INTEGER NOT NULL, parent_version_id TEXT REFERENCES result_versions(id),
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            filename TEXT NOT NULL, sha256 TEXT NOT NULL, content BLOB NOT NULL,
            columns_json TEXT NOT NULL, row_count INTEGER NOT NULL,
            operation_json TEXT, note TEXT NOT NULL, created_at TEXT NOT NULL,
            request_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
            UNIQUE(dataset_id, version_no), UNIQUE(project_id, request_id)
        );
        CREATE INDEX IF NOT EXISTS idx_figures_result_source
        ON figure_asset_versions(project_id, origin_ref) WHERE origin_type='result_chart';
    """)
    result_collections.initialize_schema(conn)
    from paperpilot.services import result_library
    result_library.initialize_schema(conn)


def decode_upload(value: str) -> bytes:
    if len(value) > ((MAX_BYTES + 2) // 3) * 4:
        raise ProjectServiceError(413, "result_too_large", "数据文件最大为 5 MiB。")
    try:
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise ProjectServiceError(422, "invalid_result", FORMAT_ERROR) from exc


def filename_only(filename: str) -> str:
    name = PurePosixPath(filename.replace("\\", "/")).name
    if PurePosixPath(name).suffix.lower() not in {".csv", ".tsv"} or any(ord(ch) < 32 for ch in name):
        raise ProjectServiceError(422, "invalid_result", FORMAT_ERROR)
    return name


def parse(content: bytes, filename: str) -> tuple[list[str], list[list[str]]]:
    if len(content) > MAX_BYTES:
        raise ProjectServiceError(413, "result_too_large", "数据文件最大为 5 MiB。")
    delimiter = "\t" if filename.lower().endswith(".tsv") else ","
    try:
        text = content.decode("utf-8-sig")
        if "\x00" in text:
            raise ValueError()
        reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
        columns = [value.strip() for value in next(reader, [])]
        if (not 1 <= len(columns) <= MAX_COLUMNS or len(set(columns)) != len(columns)
                or any(not value or len(value) > 120 or any(ord(ch) < 32 for ch in value) for value in columns)):
            raise ValueError()
        rows = []
        for row in reader:
            if not row:
                continue
            if len(row) != len(columns):
                raise ProjectServiceError(422, "invalid_result", f"数据在文件第 {reader.line_num} 行附近的列数与表头不符。")
            if len(rows) >= MAX_ROWS:
                raise ProjectServiceError(413, "result_too_large", "数据文件最多包含 50,000 行。")
            rows.append(row)
        if not rows:
            raise ProjectServiceError(422, "empty_result", "表头下至少需要一行数据。")
        return columns, rows
    except (UnicodeError, csv.Error, ValueError) as exc:
        raise ProjectServiceError(422, "invalid_result", FORMAT_ERROR) from exc


VERSION_FIELDS = """id, dataset_id, version_no, parent_version_id, project_id, filename, sha256,
    length(content) AS byte_size, columns_json, row_count, operation_json, note, created_at"""


def dataset(conn, project_id: str, dataset_id: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM result_datasets WHERE project_id=? AND id=?", (project_id, dataset_id)).fetchone()
    if row is None:
        raise ProjectServiceError(404, "resource_missing", "数据集不存在于当前论文。")
    return row


def version(conn, project_id: str, dataset_id: str, version_id: str, *, with_content=False) -> sqlite3.Row:
    fields = VERSION_FIELDS + (", content" if with_content else "")
    row = conn.execute(f"SELECT {fields} FROM result_versions WHERE project_id=? AND dataset_id=? AND id=?",
                       (project_id, dataset_id, version_id)).fetchone()
    if row is None:
        raise ProjectServiceError(404, "resource_missing", "数据版本不存在于当前论文和数据集。")
    return row


def public_version(row: sqlite3.Row) -> dict:
    result = dict(row)
    result.pop("content", None)
    result["columns"] = json.loads(result.pop("columns_json"))
    result["operation"] = json.loads(result.pop("operation_json") or "null")
    result["download_url"] = (f"/api/projects/{row['project_id']}/results/{row['dataset_id']}"
                              f"/versions/{row['id']}/download")
    return result


def list_datasets(conn, project_id: str) -> list[dict]:
    return [dict(row) for row in conn.execute("""
        SELECT d.id, d.title, v.id AS version_id, v.version_no, v.row_count, v.created_at AS updated_at
        FROM result_datasets d JOIN result_versions v ON v.dataset_id=d.id AND v.version_no=(
            SELECT MAX(version_no) FROM result_versions WHERE dataset_id=d.id
        )
        WHERE d.project_id=? ORDER BY v.created_at DESC, d.id
    """, (project_id,))]


def read_dataset(conn, project_id: str, dataset_id: str) -> dict:
    item = dataset(conn, project_id, dataset_id)
    versions = conn.execute(f"SELECT {VERSION_FIELDS} FROM result_versions WHERE dataset_id=? ORDER BY version_no DESC", (dataset_id,))
    return dict(item) | {"versions": [public_version(row) for row in versions]}


def checked_content(row: sqlite3.Row) -> bytes:
    content = row["content"]
    if visual_assets.digest(content) != row["sha256"]:
        raise ProjectServiceError(409, "result_content_mismatch", "数据版本的校验值不符，无法读取。")
    return content


def read_version(conn, project_id: str, dataset_id: str, version_id: str) -> dict:
    row = version(conn, project_id, dataset_id, version_id, with_content=True)
    _, rows = parse(checked_content(row), row["filename"])
    linked_images = conn.execute("""
        SELECT a.id AS asset_id, a.title, a.status AS asset_status, v.id AS version_id, v.version_no
        FROM figure_asset_versions v JOIN figure_assets a ON v.asset_id=a.id
        WHERE v.project_id=? AND v.origin_type='result_chart' AND v.origin_ref=?
        ORDER BY v.created_at DESC
    """, (project_id, f"result:{version_id}"))
    preview = [[cell if len(cell) <= 250 else cell[:250] + "…" for cell in values] for values in rows[:PREVIEW_ROWS]]
    return public_version(row) | {"rows": preview, "images": [dict(image) for image in linked_images]}


def _replay(conn, project_id: str, request_id: str, fingerprint: str) -> dict | None:
    record = conn.execute("SELECT id,dataset_id,fingerprint FROM result_versions WHERE project_id=? AND request_id=?",
                          (project_id, request_id)).fetchone()
    if record is None:
        return None
    if record["fingerprint"] != fingerprint:
        raise ProjectServiceError(409, "idempotency_key_reused", "该请求编号已用于另一项结果数据操作。")
    return public_version(version(conn, project_id, record["dataset_id"], record["id"]))


def require_writable(conn, project_id: str) -> None:
    lifecycle = conn.execute("SELECT lifecycle_state FROM projects WHERE id=?", (project_id,)).fetchone()
    if lifecycle is None:
        raise ProjectServiceError(404, "resource_missing", "论文不存在。")
    if lifecycle["lifecycle_state"] == "published":
        raise ProjectServiceError(409, "project_published_read_only", "已发布论文为只读状态；撤回发布后才能编辑。")

def _append(conn, project_id: str, *, dataset_id: str | None, title: str, filename: str, content: bytes,
            columns: list[str], row_count: int, note: str, request_id: str, fingerprint: str,
            parent_version_id: str | None = None, operation: dict | None = None) -> dict:
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    prior = _replay(conn, project_id, request_id, fingerprint)
    if prior is not None:
        return prior
    require_writable(conn, project_id)
    created_at = visual_assets.stamp()
    if dataset_id is None:
        dataset_id = uuid.uuid4().hex
        conn.execute("INSERT INTO result_datasets VALUES(?,?,?,?)", (dataset_id, project_id, title, created_at))
    dataset(conn, project_id, dataset_id)
    version_no = conn.execute("SELECT COALESCE(MAX(version_no),0)+1 FROM result_versions WHERE dataset_id=?", (dataset_id,)).fetchone()[0]
    version_id = uuid.uuid4().hex
    conn.execute("""INSERT INTO result_versions (id,dataset_id,version_no,parent_version_id,project_id,filename,
        sha256,content,columns_json,row_count,operation_json,note,created_at,request_id,fingerprint)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (version_id, dataset_id, version_no, parent_version_id, project_id, filename, visual_assets.digest(content),
         content, visual_assets.compact(columns), row_count, visual_assets.compact(operation) if operation else None,
         note, created_at, request_id, fingerprint))
    return public_version(version(conn, project_id, dataset_id, version_id))


def upload(conn, project_id: str, *, dataset_id: str | None, filename: str, title: str,
           content: bytes, note: str, request_id: str, source: dict | None = None) -> dict:
    filename = filename_only(filename)
    title = title.strip() or PurePosixPath(filename).stem
    identity = {"action": "upload", "dataset_id": dataset_id, "filename": filename,
                "title": title, "sha256": visual_assets.digest(content), "note": note}
    operation = {"kind": "source_table", "version": 1, **source} if source is not None else None
    if operation is not None:
        identity["operation"] = operation
    fingerprint = visual_assets.fingerprint(identity)
    prior = _replay(conn, project_id, request_id, fingerprint)
    if prior is not None:
        return prior
    if dataset_id is not None:
        dataset(conn, project_id, dataset_id)
    columns, rows = parse(content, filename)
    return _append(conn, project_id, dataset_id=dataset_id, title=title, filename=filename, content=content,
                   columns=columns, row_count=len(rows), note=note, request_id=request_id,
                   fingerprint=fingerprint, operation=operation)


def numeric(value: str) -> Decimal | None:
    value = value.strip()
    if len(value) > 64 or not NUMERIC.fullmatch(value):
        return None
    number = Decimal(value)
    return number if -1000 <= number.adjusted() <= 1000 else None


def column_index(columns: list[str], name: str) -> int:
    try:
        return columns.index(name)
    except ValueError as exc:
        raise ProjectServiceError(422, "invalid_column", "所选列不存在于该数据版本。") from exc


def transform(columns: list[str], rows: list[list[str]], operation: dict) -> tuple[list[str], list[list[str]]]:
    kind = operation["kind"]
    if kind == "select":
        selected = operation["columns"]
        if len(set(selected)) != len(selected):
            raise ProjectServiceError(422, "invalid_column", "保留列必须唯一。")
        indexes = [column_index(columns, name) for name in selected]
        return selected, [[row[i] for i in indexes] for row in rows]
    if kind == "drop_blank":
        return columns, [row for row in rows if all(cell.strip() for cell in row)]
    if kind == "deduplicate":
        return columns, [list(row) for row in dict.fromkeys(map(tuple, rows))]
    if kind == "filter":
        index = column_index(columns, operation["column"])
        minimum = numeric(operation["minimum"] or "")
        maximum = numeric(operation["maximum"] or "")
        if ((minimum is None and maximum is None)
                or (operation["minimum"] and minimum is None) or (operation["maximum"] and maximum is None)
                or (minimum is not None and maximum is not None and minimum > maximum)):
            raise ProjectServiceError(422, "invalid_range", "请填写有效的数值范围，且下限不得高于上限。")
        filtered = []
        for row in rows:
            value = numeric(row[index])
            if value is not None and (minimum is None or value >= minimum) and (maximum is None or value <= maximum):
                filtered.append(row)
        return columns, filtered

    group_column, metric = operation["group_by"], operation["column"]
    group_index, metric_index = column_index(columns, group_column), column_index(columns, metric)
    if group_index == metric_index:
        raise ProjectServiceError(422, "invalid_column", "分组列和统计数值列须分别选择。")
    groups = defaultdict(list)
    for index, row in enumerate(rows, start=1):
        value = numeric(row[metric_index])
        if value is None:
            raise ProjectServiceError(422, "invalid_numeric_data", f"第 {index} 条数据缺少有效统计数值，请先清理或筛选。")
        groups[row[group_index]].append(value)
    headers = [group_column] + [f"{name}_value" if name == group_column else name for name in ("count", "mean", "min", "max")]
    with localcontext() as context:
        # Cover the accepted exponent range during summation, then round only the mean.
        context.prec = 2200
        totals = {group: sum(values) for group, values in groups.items()}
        context.prec = 24
        summary = [[group, str(len(values)), str(totals[group] / len(values)),
                    str(min(values)), str(max(values))] for group, values in groups.items()]
    return headers, summary


def apply_operations(columns, rows, operations):
    for operation in operations:
        columns, rows = transform(columns, rows, operation)
        if not rows:
            raise ProjectServiceError(422, "empty_result", "处理结果为空，请调整条件或选择其他版本。")
    return columns, rows


def derive(conn, project_id: str, dataset_id: str, version_id: str, *, operation: dict, note: str, request_id: str) -> dict:
    fingerprint = visual_assets.fingerprint({"action": "derive", "dataset_id": dataset_id,
                                            "parent": version_id, "operation": operation, "note": note})
    prior = _replay(conn, project_id, request_id, fingerprint)
    if prior is not None:
        return prior
    source = version(conn, project_id, dataset_id, version_id, with_content=True)
    columns, rows = parse(checked_content(source), source["filename"])
    operations = operation["steps"] if operation["kind"] == "pipeline" else [operation]
    new_columns, new_rows = apply_operations(columns, rows, operations)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(new_columns)
    writer.writerows(new_rows)
    content = output.getvalue().encode("utf-8-sig")
    if len(content) > MAX_BYTES:
        raise ProjectServiceError(413, "result_too_large", "处理后的数据超过 5 MiB，请缩小处理范围。")
    return _append(conn, project_id, dataset_id=dataset_id, title="", filename=f"result-from-v{source['version_no']}.csv",
                   content=content, columns=new_columns, row_count=len(new_rows), note=note, request_id=request_id,
                   fingerprint=fingerprint, parent_version_id=version_id, operation={"version": 1, **operation})
