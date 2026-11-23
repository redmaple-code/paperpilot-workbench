"""Three independent input contracts and deterministic, versioned style compilation."""
from __future__ import annotations

import html
import json
import re
import sqlite3
from pydantic import ValidationError

from paperpilot.domain.drawing_schemas import ImageSpec, SceneSpec, StructuredSpec
from paperpilot.services.project_errors import ProjectServiceError

TYPES = ("structured_svg", "scene_graph", "ai_image")
DEFAULTS = {
    "structured_svg": {"fill": "#edf2f0", "stroke": "#243e37", "text": "#162b26", "font_size": 16},
    "scene_graph": {"fill": "#f3eee6", "stroke": "#7e512e", "text": "#30261e", "font_size": 16},
    "ai_image": {"palette": "muted", "background": "white", "description": "academic illustration"},
}


def suggest(intent: str) -> dict:
    kind = "ai_image" if any(x in intent.lower() for x in ("照片", "写实", "纹理", "photo", "illustration")) else "scene_graph" if any(x in intent for x in ("自由", "坐标", "布局", "编辑")) else "structured_svg"
    return {"suggested_type": kind, "reason": "根据描述中的图像用途与编辑需求给出本地规则建议。", "confidence": 0.65,
            "assumptions": ["作者会核对图中文字、数据与学术含义"], "alternatives": [x for x in TYPES if x != kind],
            "risks": ["AI 图像可能产生错误文字或结构，保存前需要审阅。"] if kind == "ai_image" else ["自动布局可能需要调整；不会自动写入正文。"], "method": "local_rules_v1"}


def validate_spec(kind: str, raw: dict) -> dict:
    model = {"structured_svg": StructuredSpec, "scene_graph": SceneSpec, "ai_image": ImageSpec}.get(kind)
    if model is None:
        raise ProjectServiceError(422, "invalid_tool", "请选择三种绘图方式之一。")
    try:
        spec = model.model_validate(raw).model_dump()
    except ValidationError as exc:
        # Do not echo prompts or asset content in diagnostics.
        fields = ", ".join(".".join(str(p) for p in e["loc"]) for e in exc.errors()[:8])
        raise ProjectServiceError(422, "invalid_spec", f"绘图输入不符合所选工具要求，请检查：{fields}。") from exc
    if kind != "ai_image":
        nodes = spec["nodes"]
        ids = {n["id"] for n in nodes}
        if len(ids) != len(nodes) or any(e["source"] not in ids or e["target"] not in ids for e in spec["edges"]):
            raise ProjectServiceError(422, "invalid_spec", "节点编号必须唯一，每条连线必须指向已存在节点。")
        if kind == "scene_graph" and any(n["x"] + n["width"] > spec["width"] or n["y"] + n["height"] > spec["height"] for n in nodes):
            raise ProjectServiceError(422, "invalid_spec", "图形超出画布；请调整坐标或扩大画布。")
    elif spec.get("mask_version") and not spec.get("input_asset_version"):
        raise ProjectServiceError(422, "invalid_spec", "使用遮罩时必须选择原始图像版本。")
    return spec


def validate_style(kind: str, values: dict) -> dict:
    if not isinstance(values, dict) or set(values) - set(DEFAULTS[kind]):
        raise ProjectServiceError(422, "invalid_style", "风格参数不属于所选工具。")
    for key, value in values.items():
        if key in {"fill", "stroke", "text"} and (not isinstance(value, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", value)):
            raise ProjectServiceError(422, "invalid_style", "颜色请使用六位十六进制色值。")
        if key == "font_size" and (isinstance(value, bool) or not isinstance(value, (int, float)) or not 8 <= value <= 72):
            raise ProjectServiceError(422, "invalid_style", "字号应在 8 到 72 之间。")
        if key in {"palette", "background", "description"} and (not isinstance(value, str) or len(value) > 500):
            raise ProjectServiceError(422, "invalid_style", "图片风格描述不能超过 500 字。")
    return values


def compile_style(conn: sqlite3.Connection, project_id: str, kind: str, constraints: dict) -> dict:
    values = dict(DEFAULTS[kind])
    sources = [{"source": "global_default", "version": 1, "values": dict(values)}]
    project = conn.execute("SELECT domain_id FROM projects WHERE id=?", (project_id,)).fetchone()
    # Rules are separate from prose style settings; candidates never enter effective plans.
    for scope, scope_id in [("domain", project["domain_id"]), ("project", project_id)]:
        for row in conn.execute("SELECT * FROM visual_style_rules WHERE scope=? AND scope_id=? AND kind=? AND status='approved' ORDER BY version,id", (scope, scope_id, kind)):
            provenance = conn.execute("SELECT report_id FROM visual_style_candidates WHERE rule_id=?", (row["id"],)).fetchone()
            if provenance:
                # Validate source identity without recursively compiling the report's rule plan.
                frozen = json.loads(conn.execute("SELECT snapshot_json FROM visual_reports WHERE id=?", (provenance["report_id"],)).fetchone()[0])
                from paperpilot.services.visual_evidence import asset_snapshot, source_snapshot
                try:
                    if asset_snapshot(conn, project_id, frozen["source_asset"]["version_id"]) != frozen["source_asset"] or source_snapshot(conn, project_id, frozen["source"]) != frozen["source"]:
                        continue
                except ProjectServiceError:
                    continue
            rule = validate_style(kind, json.loads(row["values_json"]))
            values.update(rule)
            sources.append({"source": scope, "id": row["id"], "version": row["version"], "approval": "approved", "values": rule})
    values.update(validate_style(kind, constraints))
    if constraints:
        sources.append({"source": "explicit_request", "version": 1, "values": constraints})
    return {"compiler_version": "visual-style-v1", "kind": kind, "values": values, "sources": sources}


def render(kind: str, spec: dict, plan: dict) -> bytes:
    if kind == "ai_image":
        raise ProjectServiceError(422, "external_adapter_required", "AI 图片必须通过独立图像模型适配器执行。")
    style = plan["values"]
    if kind == "structured_svg":
        count = len(spec["nodes"])
        columns = min(count, 5)
        width, height = columns * 180 + 40, ((count + columns - 1) // columns) * 110 + 40
        nodes = [dict(n, x=30 + (i % columns) * 180, y=30 + (i // columns) * 110, width=140, height=60, shape="rect") for i, n in enumerate(spec["nodes"])]
    else:
        width, height, nodes = spec["width"], spec["height"], spec["nodes"]
    elements = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">', '<rect width="100%" height="100%" fill="#ffffff"/>']
    index = {n["id"]: n for n in nodes}
    for e in spec["edges"]:
        a, b = index[e["source"]], index[e["target"]]
        x1, y1, x2, y2 = a["x"] + a["width"] / 2, a["y"] + a["height"] / 2, b["x"] + b["width"] / 2, b["y"] + b["height"] / 2
        elements.append(f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{style["stroke"]}" stroke-width="2"/>')
    for n in nodes:
        x, y, w, h = n["x"], n["y"], n["width"], n["height"]
        attrs = f'fill="{style["fill"]}" stroke="{style["stroke"]}" stroke-width="2"'
        if n["shape"] == "ellipse":
            elements.append(f'<ellipse cx="{x+w/2}" cy="{y+h/2}" rx="{w/2}" ry="{h/2}" {attrs}/>')
        elif n["shape"] == "rect":
            elements.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="6" {attrs}/>')
        elements.append(f'<text x="{x+w/2}" y="{y+h/2+style["font_size"]/3}" text-anchor="middle" font-family="Helvetica" font-size="{style["font_size"]}" fill="{style["text"]}">{html.escape(n["label"])}</text>')
    elements.append("</svg>")
    return "".join(elements).encode("utf-8")
