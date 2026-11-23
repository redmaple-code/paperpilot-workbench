"""Group saved results from their source structure and optional author labels."""
from __future__ import annotations

import json
import re
from pathlib import PurePosixPath

from paperpilot.services import agent_actions, results, visual_assets
from paperpilot.services.project_errors import ProjectServiceError

CATEGORIES = {
    "adopted": "论文已采纳",
    "profile": "曲线与剖面",
    "convergence": "收敛性与误差",
    "performance": "耗时与运行指标",
    "comparison": "数据核对与参数试验",
    "series": "时间序列",
    "table": "其他数据表",
    "figure": "其他结果图",
    "manual": "手工分组",
}
DATA_CATEGORIES = tuple(key for key in CATEGORIES if key not in {"adopted", "manual"})
FIELD_LABELS = {
    "rho": "密度", "u": "速度", "p": "压力", "x": "位置", "y": "纵坐标",
    "time": "时间", "t": "时间", "seconds": "耗时", "cells": "网格数",
    "l1": "L1 误差", "linf": "L∞ 误差", "loss": "损失", "temperature": "温度",
    "density": "密度", "velocity": "速度", "pressure": "压力",
}
METHOD_LABELS = {"wenoas": "WENO-AS", "weno5": "WENO-JS", "wenoz": "WENO-Z", "wenozplus": "WENO-Z+"}
SOURCE_TITLES = {
    "riemann-source-comparison": "黎曼算例 · CSV 与 NPZ 数值差异",
    "euler-ablation": "权重组合消融 · 密度误差与运行指标",
    "zplus_gap_euler": "Euler · WENO-Z+ 误差、阶数与耗时对照",
    "zplus_gap_scalar": "线性平流 · WENO-Z+ 误差、阶数与耗时对照",
    "threshold-critical2": "二阶临界点 · 阈值检验的 L1 与 L∞ 误差",
    "threshold-composite": "复合初值 · 阈值检验的误差与总变差",
}


def initialize_schema(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS result_library_labels (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            item_kind TEXT NOT NULL CHECK(item_kind IN ('table', 'figure')),
            item_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            group_name TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(project_id, item_kind, item_id)
        );
        CREATE TABLE IF NOT EXISTS result_library_layout (
            project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
            layout_json TEXT NOT NULL
        );
    """)


def _shape(columns, meta):
    names = {re.sub(r"[\s-]+", "_", column.strip().casefold()) for column in columns}
    group = meta.get("collection_group")
    key = meta.get("key", "")
    if (group == "verification" and not meta.get("case")) or re.search(r"ablation|zplus_gap|threshold", key):
        return "comparison", "核对或参数试验"
    if group in {"scalar", "supplement"} and key.startswith(("scalar-", "euler-smooth-")):
        return "convergence", "按网格记录误差"
    if names & {"different_values", "max_abs_difference"}:
        return "comparison", "来源差异"
    if names & {"time", "t", "timestamp", "时间", "日期"}:
        return "series", "按时间记录"
    if names & {"x", "position", "distance", "坐标", "位置"}:
        variables = {column.rsplit("_", 1)[-1] for column in names}
        return "profile", "空间剖面" if {"rho", "u", "p"} <= variables else "按位置记录"
    if names & {"seconds", "runtime", "elapsed", "cpu_time", "耗时"}:
        return "performance", "耗时与指标"
    if names & {"cells", "grid", "mesh", "网格数"} and any(
        re.search(r"(?:^|_)(?:l1|linf|l2|error|order)(?:_|$)|误差", name) for name in names
    ):
        return "convergence", "按网格记录误差"
    if {"count", "mean", "min", "max"} <= names:
        return "table", "分组统计表"
    return "table", "数据表"


def _measurement(column):
    name = column.casefold()
    method, sep, field = name.partition("_")
    if sep and method in METHOD_LABELS:
        return f"{METHOD_LABELS[method]} {FIELD_LABELS.get(field, field)}"
    return FIELD_LABELS.get(name, column)


def _table_title(title, columns, source_filename, meta, category):
    if meta.get("case"):
        case, grid = meta["group"].split(" · ", 1)
        source = "WENO-AS CSV 导出" if meta["origin"] == "CSV 导出" else "论文 NPZ"
        return f"{case} {grid} · 密度、速度与压力（{source}）"
    if category == "convergence" and meta.get("collection_group") in {"scalar", "supplement"}:
        return title + " · 网格收敛误差"
    if meta.get("key") in SOURCE_TITLES:
        return SOURCE_TITLES[meta["key"]]
    stem = PurePosixPath(source_filename).stem
    generic = re.fullmatch(r"(?:data|result[s]?|output|run|table|数据|结果|表格)(?:[\s_.-]*\d+)?", title, re.I)
    file_slug = title == stem and re.fullmatch(r"[a-z0-9]+(?:[_-][a-z0-9]+)+", title, re.I)
    if generic or file_slug:
        metrics = "、".join(_measurement(column) for column in columns[:3])
        return f"{metrics} · {stem}"
    return title


def _chart_title(title, source):
    chart = source.get("chart") or {}
    y = chart.get("y_column", "")
    x = chart.get("x_column", "")
    metric = _measurement(y)
    if x == "x" and y and re.search(r"剖面$", title):
        return re.sub(r"(?:密度|速度|压力|数值)剖面$", lambda _: f"{metric}剖面", title)
    if y and (not title or title == "Result"):
        return f"{metric}随{_measurement(x)}变化"
    return title


def _manuscript_graphics(conn, pid):
    graphics = set()
    for row in conn.execute("SELECT content FROM files WHERE project_id=? AND path LIKE '%.tex'", (pid,)):
        text = re.sub(r"(?m)(?<!\\)%[^\n]*", "", row["content"])
        graphics.update(visual_assets.GRAPHICS.findall(text))
    return graphics


def _items(conn, pid, entries, labels):
    collection_tables = {entry["dataset_id"]: entry for entry in entries}
    indexed = {}
    paper_figures = {}
    for row in conn.execute("""
        SELECT d.id AS dataset_id,d.title,v.id AS version_id,v.version_no,v.row_count,
               v.filename,v.columns_json,raw.filename AS raw_filename,raw.columns_json AS raw_columns,
               raw.operation_json AS raw_operation
        FROM result_datasets d
        JOIN result_versions raw ON raw.dataset_id=d.id AND raw.version_no=1
        JOIN result_versions v ON v.dataset_id=d.id AND v.version_no=(
            SELECT MAX(last.version_no) FROM result_versions last WHERE last.dataset_id=d.id
        )
        WHERE d.project_id=? ORDER BY v.created_at DESC,d.id
    """, (pid,)):
        meta = collection_tables.get(row["dataset_id"], {})
        columns = json.loads(row["raw_columns"])
        operation = json.loads(row["raw_operation"] or "{}")
        category, structure = _shape(columns, meta)
        stored_group = operation.get("group")
        if meta.get("case"):
            subgroup = meta["group"]
        elif category == "convergence":
            subgroup = row["title"].split(" · ", 1)[0] if meta else ""
        elif category == "performance":
            subgroup = row["title"].split(" · ", 1)[0] if meta else ""
        else:
            subgroup = {"verification": "原件一致性核对", "supplement": "补充试验",
                        "composite": "复合初值线性平流"}.get(meta.get("collection_group"), "")
        original = meta.get("paper_figure")
        figure_label = re.match(r"图\s*\d+", (original or {}).get("title", ""))
        item = {
            "kind": "table", "id": row["dataset_id"], "dataset_id": row["dataset_id"], "title": row["title"],
            "display_title": _table_title(row["title"], columns, row["raw_filename"], meta, category),
            "version_id": row["version_id"], "version_no": row["version_no"], "row_count": row["row_count"],
            "column_count": len(json.loads(row["columns_json"])), "structure": structure,
            "origin": meta.get("origin", "项目数据"), "collection_id": meta.get("collection_id"),
            "paper_figure": original, "figures": [], "_category": category, "_group": subgroup or stored_group or "",
            "_identity": row["dataset_id"],
            "_schema": columns, "_case": meta.get("case"),
            "_sources": [file["id"] for file in operation.get("files", []) if file.get("id")],
            "_keywords": [
                row["filename"], row["raw_filename"], *columns, *[file["path"] for file in operation.get("files", [])],
                figure_label[0] if figure_label else "",
            ],
        }
        indexed[item["dataset_id"]] = item
        if original:
            key = original["asset_id"], original["version_id"]
            collected = paper_figures.setdefault(key, (original, []))
            group = meta["collection_group"]
            if (group == "scalar" and meta["key"] != "scalar-critical2_center") or (
                group == "composite" and meta["key"] == "scalar-composite"
            ) or (group == "efficiency" and meta["key"].endswith("-metrics")) or meta["figure_label"] or (
                group == "shock" and meta.get("case") and meta["case"][1] == 400
            ):
                name = labels.get(("table", row["dataset_id"]), {}).get("title") or item["display_title"]
                collected[1].append({"dataset_id": row["dataset_id"], "version_id": meta["version_id"], "display_title": name})

    figures = []
    paper_versions = set()
    for (asset_id, version_id), (original, sources) in paper_figures.items():
        version = conn.execute("""SELECT v.version_no,v.width,v.height FROM figure_asset_versions v JOIN figure_assets a ON a.id=v.asset_id
            WHERE a.project_id=? AND v.asset_id=? AND v.id=? AND a.status='active'""", (pid, asset_id, version_id)).fetchone()
        if not version:
            continue
        paper_versions.add(version_id)
        figures.append({
            **original, **dict(version), "kind": "figure", "origin": "论文原图",
            "display_title": original["title"], "tables": sources, "figures": [],
            "adopted": True, "_category": "adopted", "_group": "", "_identity": asset_id,
            "_keywords": [source["display_title"] for source in sources],
        })
    used_graphics = _manuscript_graphics(conn, pid)
    for row in conn.execute("""SELECT a.id AS asset_id,a.title,a.current_version_id,a.status,v.id AS version_id,
        v.version_no,v.width,v.height,v.editable_source,v.build_name,v.origin_type
        FROM figure_assets a JOIN figure_asset_versions v ON a.id=v.asset_id
        WHERE a.project_id=? AND v.project_id=? ORDER BY a.updated_at DESC,v.version_no DESC""", (pid, pid)):
        adopted = row["build_name"] in used_graphics
        is_chart = row["origin_type"] == "result_chart"
        if row["version_id"] in paper_versions or (not adopted and (
            not is_chart or row["version_id"] != row["current_version_id"] or row["status"] != "active"
        )):
            continue
        source = json.loads(row["editable_source"]) if is_chart else {}
        chart = {key: row[key] for key in ("asset_id", "title", "version_id", "version_no", "width", "height")} | {
            "kind": "figure", "origin": "正文引用" if adopted else "已保存图表", "source": source or None,
            "display_title": _chart_title(row["title"], source), "adopted": adopted, "editable": row["status"] == "active",
        }
        table = indexed.get(source.get("dataset_id"))
        label = labels.get(("figure", row["asset_id"]), {})
        if table and not adopted and not label.get("group_name"):
            table["figures"].append(chart | {
                "item_id": row["asset_id"], "display_title": label.get("title") or chart["display_title"],
                "automatic_title": chart["display_title"],
                "manual_title": label.get("title", ""), "manual_group": "", "automatic_group": "随来源数据",
                "automatic_category": CATEGORIES[table["_category"]],
            })
        else:
            source_title = table["display_title"] if table else ""
            figures.append(chart | {
                "figures": [], "_category": "adopted" if adopted else table["_category"] if table else "figure",
                "_group": table["_group"] if table and not adopted else "", "_identity": row["asset_id"], "_keywords": [source_title],
            })
    return [*indexed.values(), *figures]


def _labels(conn, pid):
    return {(row["item_kind"], row["item_id"]): dict(row) for row in conn.execute(
        "SELECT item_kind,item_id,title,group_name FROM result_library_labels WHERE project_id=?", (pid,),
    )}


def item_key(item):
    return item["kind"] + ":" + item["_identity"]


def grouping_inventory(conn, pid, entries):
    """Descriptors for organizing results, separate from scientific values and files."""
    items = [item for item in _items(conn, pid, entries, {}) if item["_category"] != "adopted"]
    return sorted(items, key=lambda item: (item["_category"], item["display_title"], item_key(item)))


def grouping_descriptors(items):
    fields = set(FIELD_LABELS) | {
        "position", "distance", "timestamp", "case", "method", "grid", "mesh", "order", "l2",
        "error", "runtime", "elapsed", "steps", "count", "mean", "min", "max", "tv",
        "different_values", "max_abs_difference", "mass_residual", "gate",
        "位置", "坐标", "时间", "日期", "网格数", "耗时", "误差",
    }
    source_labels = {}
    shape_labels = {}
    for number, item in enumerate(items, 1):
        columns = item.get("_schema", [])
        shape = tuple(value.casefold() for value in columns)
        recognized = [column for column in shape if column in fields or re.fullmatch(
            r"(?:wenoas|weno5|wenoz|wenozplus|exact|reference)_(?:rho|u|p|l1|linf|seconds)", column
        )]
        yield {
            "id": str(number), "type_hint": item["_category"], "case_grid": item.get("_case"),
            "fields": recognized[:24], "shape_id": shape_labels.setdefault(shape, len(shape_labels) + 1),
            "columns": len(columns), "rows": item.get("row_count"), "chart_count": len(item["figures"]),
            "shared_sources": [source_labels.setdefault(value, len(source_labels) + 1) for value in item.get("_sources", [])[:12]],
        }


def write_layout(conn, pid, groups, available):
    layout = {}
    if not isinstance(groups, list) or not 1 <= len(groups) <= len(available):
        raise ProjectServiceError(422, "invalid_result_groups", "分组应包含当前论文中可整理的结果条目。")
    for group in groups:
        if not isinstance(group, dict):
            raise ProjectServiceError(422, "invalid_result_groups", "无法读取分组方案。")
        category, title, members = group.get("type"), group.get("name"), group.get("items")
        if (not isinstance(category, str) or category not in DATA_CATEGORIES or not isinstance(title, str)
                or not 1 <= len(title.strip()) <= 80 or not title.strip().isprintable()
                or not isinstance(members, list) or not members):
            raise ProjectServiceError(422, "invalid_result_groups", "分组类型、名称或成员有误。")
        for key in members:
            if not isinstance(key, str) or key not in available or key in layout:
                raise ProjectServiceError(422, "invalid_result_groups", "分组方案中有重复或未知的条目。")
            layout[key] = {"type": category, "name": title.strip()}
    agent_actions.require_writable(conn, pid)
    prior = conn.execute("SELECT layout_json FROM result_library_layout WHERE project_id=?", (pid,)).fetchone()
    merged = json.loads(prior["layout_json"]) if prior else {}
    merged.update(layout)
    conn.execute("""INSERT INTO result_library_layout(project_id,layout_json) VALUES(?,?)
        ON CONFLICT(project_id) DO UPDATE SET layout_json=excluded.layout_json""",
        (pid, json.dumps(merged, ensure_ascii=False)))
    return len(layout)


def library(conn, pid, entries, query):
    labels = _labels(conn, pid)
    stored = conn.execute("SELECT layout_json FROM result_library_layout WHERE project_id=?", (pid,)).fetchone()
    layout = json.loads(stored["layout_json"]) if stored else {}
    groups = {}
    terms = re.sub(r"(?i)\bN\s*[=＝]\s*(\d+)", r"N=\1", query).casefold().split()
    for item in _items(conn, pid, entries, labels):
        key = item["kind"], item["_identity"]
        saved = labels.get(key, {})
        title = saved.get("title") or item["display_title"]
        adopted = item["_category"] == "adopted"
        proposed = layout.get(item_key(item), {}) if not adopted else {}
        auto_category = proposed.get("type", item["_category"])
        group = saved.get("group_name", "") if not adopted else ""
        category = "manual" if group else auto_category
        auto_group = proposed.get("name") or item["_group"] or CATEGORIES[auto_category]
        group_title = group or auto_group
        haystack = " ".join([
            title, item["display_title"], item["title"], auto_group, group_title, CATEGORIES[category], item.get("structure", ""),
            *item["_keywords"], *[figure["display_title"] + " " + figure["title"] for figure in item["figures"]],
        ]).casefold()
        if not all(term in haystack for term in terms):
            continue
        container = groups.setdefault((category, group_title), {"category": category, "category_title": CATEGORIES[category],
            "title": group_title, "items": []})
        public = {key: value for key, value in item.items() if not key.startswith("_")}
        public.update(item_id=item["_identity"], display_title=title, automatic_title=item["display_title"],
                      manual_title=saved.get("title", ""),
                      manual_group=group, automatic_group=auto_group, automatic_category=CATEGORIES[auto_category])
        container["items"].append(public)
    ordered = sorted(groups.values(), key=lambda group: (
        tuple(CATEGORIES).index(group["category"]),
        re.sub(r"\d+", lambda match: match[0].zfill(8), group["title"].casefold()),
    ))
    return {"groups": ordered, "manual_groups": sorted(
        {row["group_name"] for row in labels.values() if row["group_name"]}, key=str.casefold,
    )}


def save_label(conn, pid, kind, item_id, title, group, originals):
    conn.execute("BEGIN IMMEDIATE")
    agent_actions.require_writable(conn, pid)
    if kind == "table":
        results.dataset(conn, pid, item_id)
    else:
        asset = visual_assets.asset(conn, pid, item_id)
        version = visual_assets.version(conn, pid, asset["current_version_id"])
        if asset["status"] != "active" or (version["origin_type"] != "result_chart" and item_id not in originals):
            raise ProjectServiceError(404, "resource_missing", "当前论文中没有可整理的这张图。")
    values = title.strip(), group.strip()
    if any(ord(char) < 32 or ord(char) == 127 for value in values for char in value):
        raise ProjectServiceError(422, "invalid_library_label", "名称和分组须各占一行。")
    title, group = values
    if not title and not group:
        conn.execute("DELETE FROM result_library_labels WHERE project_id=? AND item_kind=? AND item_id=?",
                     (pid, kind, item_id))
    else:
        if group:
            group = next((row["group_name"] for row in _labels(conn, pid).values()
                          if row["group_name"].casefold() == group.casefold()), group)
        conn.execute("""INSERT INTO result_library_labels VALUES(?,?,?,?,?)
            ON CONFLICT(project_id,item_kind,item_id) DO UPDATE SET title=excluded.title,group_name=excluded.group_name""",
            (pid, kind, item_id, title, group))
    return {"manual_title": title, "manual_group": group}
