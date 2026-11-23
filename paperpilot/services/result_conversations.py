"""Local result tasks and explicit author decisions in a paper conversation."""
from __future__ import annotations

import json
import re
import uuid

from paperpilot.domain.visual_schemas import AssetInsertIn
from paperpilot.services import (
    agent_actions, agent_conversations as chat, result_charts, result_collections, result_library,
    results, visual_assets, visual_insertion,
)
from paperpilot.services.content_versions import current_version
from paperpilot.services.project_errors import ProjectServiceError

CASES = {
    "sod": "Sod", "lax": "Lax", "einfeldt": "Einfeldt", "shu_osher": "Shu–Osher", "blast": "Woodward–Colella",
}
METHODS = {"wenoas": "weno-as", "wenozplus": "weno-z+", "wenoz": "weno-z", "weno5": "weno-js"}
NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"


def initialize_schema(conn):
    conn.executescript("""
      CREATE TABLE IF NOT EXISTS result_chat_cards(
          id TEXT PRIMARY KEY,
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          conversation_id TEXT NOT NULL REFERENCES agent_conversations(id) ON DELETE CASCADE,
          kind TEXT NOT NULL, state TEXT NOT NULL, payload TEXT NOT NULL,
          receipt TEXT, svg BLOB
      );
      CREATE INDEX IF NOT EXISTS idx_result_chat_scope ON result_chat_cards(project_id,conversation_id);
      CREATE TABLE IF NOT EXISTS result_chat_requests(
          project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
          conversation_id TEXT NOT NULL REFERENCES agent_conversations(id) ON DELETE CASCADE, request_id TEXT NOT NULL,
          fingerprint TEXT NOT NULL, response TEXT NOT NULL,
          PRIMARY KEY(project_id, conversation_id, request_id)
      );
    """)


def card_row(conn, pid, cid, card_id):
    row = conn.execute("""SELECT * FROM result_chat_cards WHERE project_id=? AND conversation_id=? AND id=?""",
                       (pid, cid, card_id)).fetchone()
    if not row:
        raise ProjectServiceError(404, "result_card_missing", "此结果不属于当前论文会话。")
    return row


def public_card(row):
    return {"id": row["id"], "project_id": row["project_id"], "conversation_id": row["conversation_id"],
            "kind": row["kind"], "state": row["state"], **json.loads(row["payload"]),
            "receipt": json.loads(row["receipt"] or "null")}


def make_card(conn, pid, cid, kind, payload, svg=None):
    card_id = uuid.uuid4().hex
    state = "choose" if kind == "choice" else "ready"
    conn.execute("INSERT INTO result_chat_cards VALUES(?,?,?,?,?,?,NULL,?)",
                 (card_id, pid, cid, kind, state, chat.compact(payload), svg))
    return read_card(conn, pid, cid, card_id)


def read_card(conn, pid, cid, card_id):
    return public_card(card_row(conn, pid, cid, card_id))


def _stable_pointer(item):
    return {k: item[k] for k in ("dataset_id", "version_id", "title")}


def _case_key(item):
    source = item.get("key", "")
    match = re.fullmatch(r"(?:csv-)?(sod|lax|einfeldt|shu_osher|blast)[_-](\d+)(?:_wenoas)?", source)
    if not match:
        return None
    return match[1], int(match[2])


def _collection_entries(conn, pid):
    items = []
    for collection in result_collections.list_collections(conn, pid):
        for group in collection["groups"]:
            for entry in group["datasets"]:
                case = _case_key(entry)
                label = f"{CASES[case[0]]} · N={case[1]}" if case else group["title"]
                items.append({**entry, "case": list(case) if case else None, "group": label,
                              "paper_figure": group.get("figure"), "collection_id": collection["id"],
                              "collection_group": group["id"],
                              "figure_label": ("按论文图 5" if case and case[0] in {"sod", "lax", "einfeldt"}
                                               and case[1] == 200 and group.get("figure") else ""),
                              "origin": "CSV 导出" if entry["key"].startswith("csv-") else
                              ("论文 NPZ" if case else "实验原件")})
    return items


def library(conn, pid, query=""):
    return result_library.library(conn, pid, _collection_entries(conn, pid), query)


def grouping_requested(instruction):
    return bool(re.search(r"分组|归组|归类|分类|整理.{0,5}结果库", instruction) and
                re.search(r"结果库|结果与图表|实验数据|数据文件|数据集|结果条目|图表", instruction) and
                not re.search(r"按.{1,18}分组.{0,4}(?:统计|计算|求和|均值|平均)", instruction))


def group_library_tool(_args, context):
    conn, pid, cid = context["conn"], context["project_id"], context["conversation_id"]
    if not cid or not context.get("call_plain") or not grouping_requested(context["state"].get("instruction", "")):
        raise ProjectServiceError(422, "result_group_task_required", "请在论文对话中提出结果库分组任务。")
    agent_actions.require_writable(conn, pid)
    items = result_library.grouping_inventory(conn, pid, _collection_entries(conn, pid))[:64]
    if not items:
        raise ProjectServiceError(422, "result_group_items_missing", "当前没有可整理的结果条目。")
    descriptors = list(result_library.grouping_descriptors(items))
    ids = {desc["id"]: result_library.item_key(item) for desc, item in zip(descriptors, items)}
    types = {key: result_library.CATEGORIES[key] for key in result_library.DATA_CATEGORIES}
    prompt = ("你在整理科研结果库的折叠目录。按数据类型分组，在类型内将共享原件、同算例跨网格、"
              "列结构或图表关系相近的条目合并为有意义的中文子组。共享来源只是一项线索。"
              "尽量避免每条一个分组。可以略过证据不足的条目。只输出 JSON 数组，"
              '元素为 {"type":"类别键","name":"简短组名","items":["1","2"]}。每个编号最多出现一次。')
    result = context["call_plain"]([
        {"role": "system", "content": prompt},
        {"role": "user", "content": chat.compact({"types": types, "items": descriptors})},
    ], max_tokens=2000)
    if not result.get("ok"):
        raise ProjectServiceError(503, "result_group_model_failed", "本轮模型未能完成结果分组。")
    try:
        content = re.sub(r"^\x60{3}(?:json)?\s*|\s*\x60{3}$", "", result["content"].strip())
        groups = json.loads(content)
        if not isinstance(groups, list):
            raise ValueError()
        groups = [{**group, "items": [ids[str(key)] for key in group["items"]]} for group in groups]
    except (ValueError, AttributeError, KeyError, TypeError) as exc:
        raise ProjectServiceError(422, "invalid_result_groups", "模型返回的分组方案格式有误。") from exc
    with conn:
        conn.execute("BEGIN IMMEDIATE")
        current = {result_library.item_key(item) for item in result_library.grouping_inventory(conn, pid, _collection_entries(conn, pid))}
        count = result_library.write_layout(conn, pid, groups, current & set(ids.values()))
        card = make_card(conn, pid, cid, "library", {"title": "已整理结果分组", "query": "", "grouped_count": count})
    return {"cards": [{"kind": "result_work", "id": card["id"], "project_id": pid, "conversation_id": cid,
                       "title": "结果分组"}], "complete": True}


def label_library_item(conn, pid, kind, item_id, title, group):
    originals = set()
    if kind == "figure":
        originals = {entry["paper_figure"]["asset_id"] for entry in _collection_entries(conn, pid) if entry["paper_figure"]}
    return result_library.save_label(conn, pid, kind, item_id, title, group, originals)


def _labels_for(entries, source):
    return next((e for e in entries if e["dataset_id"] == source["dataset_id"]), {})


def _case_grid(text):
    lower = text.casefold()
    aliases = lower.replace("–", "-").replace("_", "-").replace(" ", "-")
    case = next((c for c, label in CASES.items()
                 if re.search(r"(?<![a-z])" + re.escape(c.replace("_", "-")) + r"(?![a-z])", aliases)
                 or label.casefold().replace("–", "-") in aliases), "")
    size = re.search(r"(?i)(?<![a-z])N\s*[=＝]?\s*(\d+)|(\d+)\s*(?:个)?网格", text)
    return case, int(size[1] or size[2]) if size else None


def _dataset_options(entries, datasets, text, fixed):
    lower = text.casefold()
    case, grid = _case_grid(text)
    figure = re.search(r"图\s*(\d+)", text)
    origin = "CSV 导出" if re.search(r"(?i)\bCSV\b|导出", text) else (
        "论文 NPZ" if re.search(r"(?i)\bNPZ\b|论文.*图\s*[34567]|原图", text) else None)
    named = [e for e in entries if e["title"].casefold() in lower or e["key"].replace("-", " ").casefold() in lower]
    specific = [d for d in datasets if d["title"].casefold() in lower]
    if fixed and not (case or grid or figure or origin or named or specific):
        return [fixed]
    if fixed and fixed.get("case") and not (named or specific):
        former_case, former_grid = fixed["case"]
        if not case or case == former_case:
            case, grid = case or former_case, grid or former_grid
    if named and (not case or any(entry["origin"] == "CSV 导出" for entry in named)):
        return named
    if case or grid or figure:
        matching = [e for e in entries if (not case or (e.get("case") and e["case"][0] == case))
                    and (grid is None or (e.get("case") and e["case"][1] == grid))]
        if grid is None and figure and figure[1] == "5":
            matching = [e for e in matching if e.get("case") and e["case"][0] in {"sod", "lax", "einfeldt"}]
            matching = [e for e in matching if e["case"][1] == 200]
        elif grid is None and figure and figure[1] == "6":
            matching = [e for e in matching if e.get("case") == ["shu_osher", 400]]
        elif figure and figure[1] != "5":
            matching = [e for e in matching if re.search(fr"图\s*{figure[1]}\b", (e.get("paper_figure") or {}).get("title", ""))]
        if origin == "CSV 导出":
            matching = [e for e in matching if e["origin"] == origin]
        elif origin:
            matching = [e for e in matching if e["origin"] != "CSV 导出"]
        elif re.search(r"weno-?(?:js|z(?:\+|plus)?)", lower):
            matching = [e for e in matching if e["origin"] != "CSV 导出"]
        return matching
    if origin and fixed:
        return [fixed] if fixed.get("origin") == origin else []
    exact = [d for d in datasets if d["title"].casefold() == lower]
    picked = exact or specific or datasets
    return [{**v, "dataset_id": v["id"]} for v in picked]


def _column(columns, text, semantic, *, preferred=None):
    if preferred and preferred in columns:
        return preferred
    folded = text.casefold()
    explicit = [c for c in columns if re.search(r"(?<![a-z0-9_])" + re.escape(c.casefold()) + r"(?![a-z0-9_])", folded, re.I) or
                (len(c) >= 2 and any(ord(ch) > 127 for ch in c) and c in text)]
    if semantic:
        prefix = next((method for method, label in METHODS.items() if label in folded or method in folded), "wenoas")
        for candidate in (f"{prefix}_{semantic}", semantic):
            if candidate in columns:
                return candidate
    return explicit[0] if len(explicit) == 1 else None


def _operations(columns, text):
    operations = []
    if re.search("去重|删除重复|去掉重复", text):
        operations.append({"kind": "deduplicate"})
    if re.search("去掉空值|删除空值|去除缺失|删除缺失|去掉缺失|删除空行|删除含空值|去掉.*空", text):
        operations.append({"kind": "drop_blank"})
    span = re.search(rf"({NUMBER})\s*(?:到|至|～|~|,|，)\s*({NUMBER})", text)
    if span:
        name = _column(columns, text, "", preferred="x" if re.search(r"\bx\b|水平坐标", text, re.I) else None)
        if not name:
            raise ProjectServiceError(422, "missing_filter_column", "请说明要筛选的列。")
        operations.append({"kind": "filter", "column": name, "minimum": span[1], "maximum": span[2]})
    elif re.search("筛选|区间", text):
        raise ProjectServiceError(422, "missing_range", "请说明筛选列和数值范围，例如「x 在 0.15 到 0.35」。")
    if "按" in text and re.search("分组|统计|均值|平均", text):
        end = text.find("按") + 1
        named = [(text.find(col, end), col) for col in columns if text.find(col, end) >= 0]
        named.sort()
        if len(named) < 2:
            raise ProjectServiceError(422, "missing_group_columns", "请说明分组列和统计的数值列。")
        operations.append({"kind": "summarize", "group_by": named[0][1], "column": named[1][1]})
    if re.search("只保留.*列|选取.*列|选取列|选列", text) and not span:
        names = [c for c in columns if _column([c], text, "")]
        if not names:
            raise ProjectServiceError(422, "missing_selection", "请说明要保留哪些列。")
        operations.append({"kind": "select", "columns": names})
    return operations


def _chart_params(columns, text, label, inherited=None, plot=None):
    inherited = inherited or {}
    semantic = "rho" if "密度" in text or "ρ" in text else "p" if "压力" in text else "u" if "速度" in text else ""
    y = _column(columns, text, semantic) if semantic else inherited.get("y_column")
    if y and y not in columns and not semantic and plot and plot.get("y") in columns:
        suffix = y.rsplit("_", 1)[-1]
        y = next((candidate for candidate in (suffix, "wenoas_" + suffix) if candidate in columns), None)
    axis = re.search(r"[Yy]\s*轴\s*(?:用|为|取|：|:)?\s*([a-zA-Z0-9_.-]+)", text)
    if axis:
        y = axis[1]
    x_axis = re.search(r"[Xx]\s*轴\s*(?:用|为|取|：|:)?\s*([a-zA-Z0-9_.-]+)", text)
    x = x_axis[1] if x_axis else inherited.get("x_column") or (plot or {}).get("x") or next(
        (name for name in ("x", "cells", "seconds") if name in columns), columns[0])
    if not y:
        y = _column([c for c in columns if c != x], text, "") or (plot or {}).get("y") or (
            columns[1] if len(columns) == 2 and x == columns[0] else None)
    if not y or x == y:
        raise ProjectServiceError(422, "missing_chart_column", "请说明要画的指标，例如「密度剖面」或「Y 轴用 loss」。")
    return {"kind": "scatter" if "散点" in text else inherited.get("kind", "line"),
            "x_column": x, "y_column": y, "x_label": x, "y_label": y, "title": label[:72]}


def _prepare_result(conn, pid, cid, source, text, mode, inherited=None):
    row = results.version(conn, pid, source["dataset_id"], source["version_id"], with_content=True)
    columns, rows = results.parse(results.checked_content(row), row["filename"])
    available = row["row_count"]
    previous_ops = inherited.get("operations", []) if inherited else []
    prefix_columns, _ = results.apply_operations(columns, rows, previous_ops)
    operations = previous_ops + _operations(prefix_columns, text)
    columns, result_rows = results.apply_operations(columns, rows, operations)
    version = results.public_version(row)
    title = source["title"]
    group = source.get("group")
    if mode == "chart" and source.get("case"):
        suffix = next((name + "剖面" for name in ("密度", "速度", "压力") if name in text), None)
        if not suffix and inherited and inherited.get("chart"):
            suffix = inherited["chart"]["title"].rsplit(" · ", 1)[-1]
        title = f'{group} · {suffix or "数值剖面"}'
    payload = {"title": title, "version": version, "source": _stable_pointer(source),
               "origin": source.get("origin", "项目数据"), "group": group, "operations": operations,
               "collection_id": source.get("collection_id"),
               "paper_figure": source.get("paper_figure"), "input_rows": available,
               "row_count": len(result_rows), "columns": columns}
    if source.get("case"):
        relevant = _collection_entries(conn, pid)
        other = next((entry for entry in relevant if entry["case"] == source["case"]
                      and entry["dataset_id"] != source["dataset_id"]), None)
        if other:
            payload["other_source"] = _stable_pointer(other) | {"origin": other["origin"]}
            comparison = next((entry for entry in relevant if entry["key"] == "riemann-source-comparison"
                               and entry["collection_id"] == source.get("collection_id")), None)
            if comparison:
                payload["comparison"] = _stable_pointer(comparison)
            payload["paper_figure"] = payload["paper_figure"] or other.get("paper_figure")
    if mode == "chart":
        config = _chart_params(columns, text, title, inherited.get("chart") if inherited else None, source.get("plot"))
        svg, source_metadata = result_charts.render_version(conn, pid, row["dataset_id"], row["id"],
                                                           config, operations=operations)
        return make_card(conn, pid, cid, "chart", payload | {"chart": config, "provenance": source_metadata,
                         "preview_sha256": visual_assets.digest(svg)}, svg)
    payload["rows"] = [[str(c)[:250] for c in r] for r in result_rows[:results.PREVIEW_ROWS]]
    return make_card(conn, pid, cid, "derive" if operations else "table", payload)


def _prepare_upload(conn, pid, cid, attach, text, datasets):
    content = attach["result_bytes"]
    columns, rows = results.parse(content, attach["filename"])
    operations = _operations(columns, text)
    new_columns, revised = results.apply_operations(columns, rows, operations)
    named = re.search(r"(?:命名为|名为)\s*[「“]?(.*?)[」”。\n，,]*$", text)
    title = named[1].strip()[:160] if named else attach["filename"].rsplit(".", 1)[0]
    target = None
    if re.search("追加|更新.*版本|新版本", text):
        matching = [d for d in datasets if d["title"].casefold() in text.casefold()]
        if len(matching) != 1:
            raise ProjectServiceError(422, "missing_target_dataset", "请在对话中指明要追加到的数据集名称。")
        target, title = matching[0]["id"], matching[0]["title"]
    case, grid = _case_grid(text)
    return make_card(conn, pid, cid, "upload", {
        "title": title, "attachment_id": attach["id"], "filename": attach["filename"], "sha256": attach["sha256"],
        "target_dataset": target, "operations": operations, "columns": new_columns, "input_rows": len(rows),
        "row_count": len(revised), "rows": [[str(c)[:250] for c in r] for r in revised[:results.PREVIEW_ROWS]],
        "origin": "对话附件",
        "group": f"{CASES[case]} · N={grid}" if case and grid else None,
    })


def _saved_chart(ref):
    if ref and ref["kind"] in {"chart", "asset"} and ref.get("receipt"):
        return ref["receipt"]
    raise ProjectServiceError(422, "missing_saved_chart", "请打开已保存图表，点「在对话中继续」，再指定正文位置。")


def _prepare_insert(conn, pid, cid, ref, text, file_path):
    if ref and ref["kind"] == "insert":
        asset = ref["asset"]
        target_text = ref["target"]["section"] or ref["target"]["file_path"]
        text = f"{text} {target_text}"
    else:
        asset = _saved_chart(ref)
    hits = []
    for row in conn.execute("SELECT path,content FROM files WHERE project_id=? AND path LIKE '%.tex'", (pid,)):
        named = re.sub(r"\s+", "", text).casefold()
        headings = [m[2].strip() for m in visual_insertion.HEADING.finditer(row["content"]) if
                    re.sub(r"\s+", "", re.sub(r"^\s*\d+(?:\.\d+)*\.?\s+", "", m[2])).casefold() in named]
        hits += [(row["path"], heading) for heading in headings]
        if not headings and (row["path"] in text or ("当前文件" in text and row["path"] == file_path)):
            hits.append((row["path"], ""))
    if hits:
        longest = max(len(heading) for _, heading in hits)
        hits = list(dict.fromkeys(x for x in hits if len(x[1]) == longest))
    if len(hits) != 1:
        raise ProjectServiceError(422, "missing_insert_target", "请注明正文文件或小节的准确名称。")
    target_path, section = hits[0]
    caption = re.search(r"(?:图注|标题)\s*(?:为|用|[：:])\s*[「“]?([^」”\n]+)", text)
    previous_caption = ref["target"]["caption"] if ref and ref["kind"] == "insert" else asset["title"]
    heading = section or target_path
    target = conn.execute("SELECT content FROM files WHERE project_id=? AND path=?", (pid, target_path)).fetchone()["content"]
    where = visual_insertion.section_end(target, section)
    return make_card(conn, pid, cid, "insert", {"title": f"插入到 {heading}", "asset": asset,
        "target": {"file_path": target_path, "section": section,
                   "base_file_version": current_version(conn, pid, "file", target_path),
                   "excerpt": target[max(0, where - 230):where].strip(),
                   "caption": (caption[1].strip().rstrip("。") if caption else previous_caption)[:1000]}})


def _replay(conn, pid, cid, data):
    old = conn.execute("SELECT fingerprint,response FROM result_chat_requests WHERE project_id=? AND conversation_id=? AND request_id=?",
                       (pid, cid, data["request_id"])).fetchone()
    if old is None:
        return None
    if old["fingerprint"] != chat.fingerprint(data):
        raise ProjectServiceError(409, "result_request_conflict", "此请求已用于另一条结果操作。")
    return json.loads(old["response"])


def _remember(conn, pid, cid, data, response):
    conn.execute("INSERT INTO result_chat_requests VALUES(?,?,?,?,?)",
                 (pid, cid, data["request_id"], chat.fingerprint(data), chat.compact(response)))
    return response


def _message(conn, pid, cid, instruction, card, reply):
    if instruction:
        chat.append_message(conn, pid, cid, "user", instruction, context={"result_local": True})
    cards = [{"kind": "result_work", "id": card["id"], "project_id": pid,
              "conversation_id": cid, "title": card["title"]}] if card else []
    chat.append_message(conn, pid, cid, "assistant", reply, cards=cards, context={"result_local": True})


def _mode(text, ref, attachment):
    if grouping_requested(text):
        return ""
    if re.search("参考文献|文献列表|引文|DOI", text, re.I) and not re.search("实验数据|数据表|算例", text):
        return ""
    if re.search("插入|插到|放到|写进|写入", text) and (re.search("这张图|该图|图表|剖面|结果图", text) or ref):
        return "insert"
    if re.match(r"^(?:(?:请|帮我|把|将|确认)\s*)*(?:保存|存入|存到|采纳|这张图.*保存|这个结果.*保存|这个表.*保存)", text) and ref and ref["kind"] in {"chart", "derive", "upload"}:
        return "save"
    if (re.search("找回|查找|有哪些|打开|查看", text) and re.search("算例|图表|剖面|结果|数据|Lax|Sod|WENO", text, re.I)):
        return "library"
    if attachment and (re.search("CSV|TSV|附件|表格|数据|这份|这张表|[去删]重|空值|入库|追加.*版本", text, re.I)
                       or any(item["filename"] in text for item in attachment)) and re.search(
                       "入库|上传|导入|整理|保存|清洗|去重|删|筛选|汇总|保留|追加", text):
        return "upload"
    if re.search("画|绘图|出图|折线|散点|生成.*图", text) and not re.search("流程图|示意图|架构图|思维导图", text):
        return "chart"
    if ref and ref.get("chart") and re.search("只保留|范围|改用|显示|调整", text):
        return "chart"
    if re.search("整理|清洗|去重|筛选|汇总|分组|统计|保留.*列|选列", text) and (
        ref or re.search("实验数据|数据[表集]|算例|表格|数据列|Lax|Sod|WENO", text, re.I)
    ) and not re.search("参考文献|文献列表|引文|DOI", text, re.I):
        return "table"
    if ref and ref["kind"] == "insert" and re.search("刷新|重新|重试", text):
        return "insert"
    return ""


def _reference(conn, pid, cid, data):
    if data.get("reference_id"):
        return read_card(conn, pid, cid, data["reference_id"])
    if data.get("dataset_id") and data.get("version_id"):
        dataset_id, version_id = data["dataset_id"], data["version_id"]
        item = results.dataset(conn, pid, dataset_id)
        results.version(conn, pid, dataset_id, version_id)
        return {"kind": "table", "source": {"dataset_id": dataset_id, "version_id": version_id, "title": item["title"]}}
    if data.get("asset_id") and data.get("version_id"):
        asset_id, version_id = data["asset_id"], data["version_id"]
        asset = visual_assets.asset(conn, pid, asset_id)
        version = visual_assets.version(conn, pid, version_id)
        if version["asset_id"] != asset_id or asset["status"] != "active":
            raise ProjectServiceError(409, "asset_unavailable", "选定的图像版本已不可用。")
        version = visual_assets.public_version(version)
        ref = {"kind": "asset", "receipt": {"asset_id": asset_id, "version_id": version_id, "title": asset["title"],
               "version_no": version["version_no"], "preview_url": version["preview_url"]}}
        source = version.get("result_source")
        if source:
            ref.update(chart=source["chart"], operations=source.get("operations", []),
                       source={"dataset_id": source["dataset_id"], "version_id": source["version_id"], "title": asset["title"]})
        return ref
    return None


def turn(conn, root, pid, cid, data, *, candidate_only=False):
    chat.get(conn, pid, cid)
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")
    prior = _replay(conn, pid, cid, data)
    if prior:
        return prior
    text = data["instruction"].strip()
    ref = _reference(conn, pid, cid, data)
    attaches = conn.execute("""SELECT id, filename, sha256, result_bytes FROM agent_attachments
        WHERE project_id=? AND conversation_id=? AND status='active' AND result_bytes IS NOT NULL ORDER BY created_at DESC""",
                            (pid, cid)).fetchall()
    mode = _mode(text, ref, attaches)
    datasets = results.list_datasets(conn, pid) if mode else []
    if not mode or (candidate_only and mode in {"save", "insert"}) or (
        mode == "chart" and not datasets and not ref and not re.search("数据|剖面|Lax|WENO|坐标轴", text, re.I)
    ):
        return {"handled": False}
    if mode == "chart" and not ref and not re.search("数据|剖面|Lax|Sod|WENO|坐标|曲线|散点|折线|收敛|误差|NPZ|CSV", text, re.I) and not any(
        d["title"].casefold() in text.casefold() for d in datasets
    ):
        return {"handled": False}
    agent_actions.require_writable(conn, pid)
    entries = _collection_entries(conn, pid)
    card = None
    conn.execute("SAVEPOINT result_turn")
    try:
        if mode == "save":
            _confirm(conn, root, pid, cid, card_row(conn, pid, cid, ref["id"]))
            card = read_card(conn, pid, cid, ref["id"])
        elif mode == "insert":
            card = _prepare_insert(conn, pid, cid, ref, text, data.get("file_path", ""))
        elif mode == "library":
            case, grid = _case_grid(text)
            figure = re.search(r"图\s*(\d+)", text)
            terms = [CASES.get(case), f"N={grid}" if grid else None, f"图 {figure[1]}" if figure else None]
            if not any(terms):
                terms = [d["title"] for d in datasets if d["title"].casefold() in text.casefold()][:1]
            card = make_card(conn, pid, cid, "library", {"title": "结果与图表", "query": " ".join(filter(None, terms))})
        elif mode == "upload":
            matching = [row for row in attaches if row["filename"] in text]
            selected = matching or attaches
            if len(selected) != 1:
                card = make_card(conn, pid, cid, "choice", {"title": "选择要整理的附件", "instruction": text,
                                 "mode": mode, "options": [{"attachment_id": a["id"], "title": a["filename"]} for a in selected]})
            else:
                card = _prepare_upload(conn, pid, cid, selected[0], text, datasets)
        else:
            fixed = None
            if ref and ref.get("source"):
                fixed = {**_labels_for(entries, ref["source"]), **ref["source"]}
            if ref and ref.get("receipt", {}) and ref["receipt"].get("dataset_id") and ref["kind"] in {"upload", "derive"}:
                fixed = {**_labels_for(entries, ref["receipt"]), **_stable_pointer(ref["receipt"])}
            options = _dataset_options(entries, datasets, text, fixed)
            if not options:
                raise ProjectServiceError(422, "missing_result_source", "当前论文没有匹配的数据，请补充算例或添加附件。")
            if len(options) > 1:
                same_case = options[0].get("case")
                difference = next((m for c in result_collections.list_collections(conn, pid) for m in c.get("mismatches", [])
                    if same_case and all(o.get("case") == same_case for o in options)
                    and m["case"] == CASES[same_case[0]] and m["cells"] == same_case[1]
                    and m["method"] in {"wenoas", "weno-as"}), None)
                options.sort(key=lambda item: (item.get("group", ""), item.get("origin") == "CSV 导出"))
                card = make_card(conn, pid, cid, "choice", {"title": "选择数据来源", "instruction": text,
                                 "mode": mode, "difference": difference, "reference": ref, "options": options[:30]})
            else:
                card = _prepare_result(conn, pid, cid, options[0], text, mode, ref)
        reply = ""
    except ProjectServiceError as exc:
        conn.execute("ROLLBACK TO result_turn")
        card = make_card(conn, pid, cid, "retry", {"title": "结果未生成", "instruction": text,
                         "error": exc.message, "reference_id": data.get("reference_id"),
                         "dataset_id": data.get("dataset_id"), "asset_id": data.get("asset_id"),
                         "version_id": data.get("version_id")})
        reply = exc.message
    conn.execute("RELEASE result_turn")
    if card and card["kind"] in {"chart", "table", "derive"} and re.search(r"创新|主张|贡献|novelty|contribution", text, re.I):
        from paperpilot.services import innovation_points
        claims = innovation_points.context(conn, pid)
        if claims:
            stored = card_row(conn, pid, cid, card["id"])
            payload = json.loads(stored["payload"]) | {"innovation_claims": claims["items"]}
            conn.execute("UPDATE result_chat_cards SET payload=? WHERE id=? AND project_id=? AND conversation_id=?",
                         (chat.compact(payload), card["id"], pid, cid))
            innovation_points.record_usage(conn, pid, innovation_points.references(claims), "result_card", card["id"], cid, card["title"])
            card = read_card(conn, pid, cid, card["id"])
    if not candidate_only:
        _message(conn, pid, cid, text, card, reply)
    return _remember(conn, pid, cid, data, {"handled": True, "card": card, "message": reply})


def prepare_tool(args, context):
    """The model can request a local candidate and receives only its opaque pointer."""
    conn, pid, cid = context["conn"], context["project_id"], context["conversation_id"]
    instruction = args["instruction"].strip()
    if not cid or not instruction:
        return {"ok": False, "error_code": "result_task_missing", "error": "需要论文会话和明确的结果任务。"}
    request_id = "result-agent:" + chat.fingerprint({"run": context["run_id"], "instruction": instruction})
    try:
        result = turn(conn, None, pid, cid, {"instruction": instruction, "request_id": request_id}, candidate_only=True)
    except ProjectServiceError as exc:
        return {"ok": False, "error_code": exc.code, "error": "暂时无法准备结果。请检查当前论文的使用权限。"}
    if not result["handled"]:
        return {"ok": False, "error_code": "result_task_unclear", "error": "请明确组织、检索或绘图目标，或请作者补充。"}
    card = result["card"]
    return {"cards": [{"kind": "result_work", "id": card["id"], "project_id": pid, "conversation_id": cid,
                       "title": "本地结果"}], "complete": True}


def _confirm(conn, root, pid, cid, row):
    card = public_card(row)
    if row["state"] == "saved":
        return card["receipt"]
    if row["state"] != "ready":
        raise ProjectServiceError(409, "result_not_ready", "请先查看或重新生成候选。")
    kind, request_id = row["kind"], f'result-chat:{row["id"]}'
    if kind == "chart":
        content = row["svg"]
        if not content or visual_assets.digest(content) != card["preview_sha256"]:
            raise ProjectServiceError(409, "chart_preview_changed", "请重新生成图表预览。")
        version = card["version"]
        source = results.version(conn, pid, version["dataset_id"], version["id"], with_content=True)
        results.checked_content(source)
        asset = visual_assets.save_asset(conn, root, pid, content=content, filename="result-chart.svg",
            title=card["title"], request_id=request_id, kind="result_chart", origin_ref=f'result:{version["id"]}',
            editable_source=visual_assets.compact(card["provenance"]),
            _before_write=lambda *_: agent_actions.require_writable(conn, pid))
        receipt = {"asset_id": asset["asset"]["id"], "version_id": asset["version"]["id"],
                   "preview_url": asset["version"]["preview_url"], "version_no": asset["version"]["version_no"],
                   "title": asset["asset"]["title"]}
    elif kind in {"upload", "derive"}:
        if kind == "upload":
            attach = conn.execute("""SELECT * FROM agent_attachments WHERE project_id=? AND conversation_id=?
                AND id=? AND status='active'""", (pid, cid, card["attachment_id"])).fetchone()
            if not attach or attach["sha256"] != card["sha256"] or visual_assets.digest(attach["result_bytes"]) != card["sha256"]:
                raise ProjectServiceError(409, "attachment_changed", "附件已移除或变化，请重新预览。")
            version = results.upload(conn, pid, dataset_id=card["target_dataset"], filename=attach["filename"],
                                     title=card["title"], content=attach["result_bytes"], note="从论文对话归档",
                                     request_id=request_id, source={"group": card.get("group"), "attachment_id": attach["id"]})
            original = version
        else:
            version = card["version"]
            original = version
        if card["operations"]:
            version = results.derive(conn, pid, version["dataset_id"], version["id"],
                operation={"kind":"pipeline", "steps":card["operations"]}, note="从论文对话整理", request_id=request_id+":derived")
        receipt = {"title": card["title"], "dataset_id": version["dataset_id"], "version_id": version["id"],
                   "version_no": version["version_no"], "download_url": version["download_url"],
                   "original_version_id": original["id"], "original_download_url": original["download_url"]}
    elif kind == "insert":
        target, asset = card["target"], card["asset"]
        payload = AssetInsertIn(version_id=asset["version_id"], request_id=request_id,
                                target_path=target["file_path"], base_file_version=target["base_file_version"],
                                caption=target["caption"])
        visual_insertion.insert(conn, root, pid, asset["asset_id"], payload, section=target["section"])
        receipt = {"file_path": target["file_path"], "section": target["section"], "title": card["title"],
                   "asset_id": asset["asset_id"], "version_id": asset["version_id"], "version_no": asset["version_no"]}
    else:
        raise ProjectServiceError(422, "nothing_to_save", "此结果无需保存。")
    conn.execute("UPDATE result_chat_cards SET state='saved',receipt=? WHERE id=?", (chat.compact(receipt), row["id"]))
    return receipt


def decide(conn, root, pid, cid, card_id, data):
    conn.execute("BEGIN IMMEDIATE")
    prior = _replay(conn, pid, cid, data | {"card_id": card_id})
    if prior:
        return prior
    agent_actions.require_writable(conn, pid)
    row = card_row(conn, pid, cid, card_id)
    card = public_card(row)
    action = data["action"]
    if action == "cancel":
        if card["state"] == "choose":
            conn.execute("UPDATE result_chat_cards SET state='cancelled' WHERE id=?", (card_id,))
        response = read_card(conn, pid, cid, card_id)
    elif action == "choose" and card["state"] == "choose":
        index = data["option"]
        if index is None or not 0 <= index < len(card["options"]):
            raise ProjectServiceError(422, "missing_choice", "请选择一个可用来源。")
        item = card["options"][index]
        if card["mode"] == "upload":
            attach = conn.execute("""SELECT * FROM agent_attachments WHERE project_id=? AND conversation_id=?
                AND id=? AND status='active' AND result_bytes IS NOT NULL""", (pid, cid, item["attachment_id"])).fetchone()
            if attach is None:
                raise ProjectServiceError(409, "attachment_changed", "附件已移除，请重选。")
            response = _prepare_upload(conn, pid, cid, attach, card["instruction"], results.list_datasets(conn, pid))
        else:
            response = _prepare_result(conn, pid, cid, item, card["instruction"], card["mode"], card.get("reference"))
        conn.execute("UPDATE result_chat_cards SET state='resolved',receipt=? WHERE id=?",
                     (chat.compact({"card_id": response["id"]}), card_id))
        _message(conn, pid, cid, "", response, "")
    elif action == "confirm":
        _confirm(conn, root, pid, cid, row)
        response = read_card(conn, pid, cid, card_id)
    else:
        raise ProjectServiceError(409, "result_choice_changed", "此选择已经处理，请打开最新结果。")
    return _remember(conn, pid, cid, data | {"card_id": card_id}, response)
