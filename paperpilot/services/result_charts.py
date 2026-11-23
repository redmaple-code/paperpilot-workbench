"""Deterministic numerical figures from an exact result version."""
from __future__ import annotations

import html
import textwrap
from decimal import Decimal, localcontext

from paperpilot.services import results, visual_assets
from paperpilot.services.project_errors import ProjectServiceError

MAX_POINTS = 3000
TICK_STEPS = 5
INK, GRID, SERIES = "#24323c", "#dce4e9", "#205878"


def _label(value: str, x: int, y: int, size: int, *, anchor="middle", transform="") -> str:
    text = html.escape(value.replace("\ufffe", "").replace("\uffff", ""))
    rotation = f' transform="{transform}"' if transform else ""
    return (f'<text x="{x}" y="{y}" font-family="Helvetica" font-size="{size}" '
            f'fill="{INK}" text-anchor="{anchor}"{rotation}>{text}</text>')


def _scale(values: list[Decimal]):
    lower, upper = min(values), max(values)
    if lower == upper:
        margin = abs(lower) * Decimal("0.05") if lower else Decimal(1)
        lower, upper = lower - margin, upper + margin
    span = upper - lower
    ticks = [lower + span * i / TICK_STEPS for i in range(TICK_STEPS + 1)]
    labels = [format(tick.normalize(), ".5g") for tick in ticks]
    offset = ""
    if len(set(labels)) != len(labels):
        labels = [f"+{(tick - lower).normalize():.5g}" if tick != lower else "0" for tick in ticks]
        exact = str(lower.normalize())
        offset = f" + ({exact})"
    return lower, span, labels, offset


def _render(points: list[tuple[Decimal, Decimal]], chart: dict) -> bytes:
    # More precision than any accepted input mantissa, including narrow, offset axes.
    with localcontext() as context:
        context.prec = 80
        xmin, xspan, xticks, xoffset = _scale([x for x, _ in points])
        ymin, yspan, yticks, yoffset = _scale([y for _, y in points])
        left, right, top, bottom = 136, 870, 90, 420
        marks = [(float(left + (x - xmin) / xspan * (right - left)),
                  float(bottom - (y - ymin) / yspan * (bottom - top))) for x, y in points]

    svg = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="960" height="520" viewBox="0 0 960 520">',
        '<rect width="960" height="520" fill="#ffffff"/>',
    ]
    for index, line in enumerate(textwrap.wrap(chart["title"], width=36)):
        svg.append(_label(line, 480, 32 + 22 * index, 19))
    for index, (xtext, ytext) in enumerate(zip(xticks, yticks)):
        x = left + (right - left) * index / TICK_STEPS
        y = bottom - (bottom - top) * index / TICK_STEPS
        svg.extend([
            f'<path d="M{x} {top}V{bottom} M{left} {y}H{right}" fill="none" stroke="{GRID}"/>',
            _label(xtext, x, bottom + 23, 12),
            _label(ytext, left - 10, y + 4, 12, anchor="end"),
        ])
    svg.append(f'<path d="M{left} {top}V{bottom}H{right}" fill="none" stroke="{INK}"/>')
    axis_center = (left + right) // 2
    svg.append(_label(chart["x_label"], axis_center, 473, 15))
    if xoffset:
        svg.append(_label(xoffset, axis_center, 495, 11))
    svg.append(_label(chart["y_label"], 32, 255, 15, transform="rotate(-90 32 255)"))
    if yoffset:
        svg.append(_label(yoffset, 53, 255, 11, transform="rotate(-90 53 255)"))
    if chart["kind"] == "line":
        coords = " ".join(f"{x:.2f},{y:.2f}" for x, y in marks)
        svg.append(f'<polyline points="{coords}" fill="none" stroke="{SERIES}" stroke-width="2"/>')
    if chart["kind"] == "scatter" or len(marks) <= 60:
        svg.extend(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="2.8" fill="{SERIES}"/>' for x, y in marks)
    svg.append("</svg>")
    return "".join(svg).encode("utf-8")


def render_version(conn, project_id: str, dataset_id: str, version_id: str, config: dict,
                   *, operations=()) -> tuple[bytes, dict]:
    source = results.version(conn, project_id, dataset_id, version_id, with_content=True)
    columns, rows = results.parse(results.checked_content(source), source["filename"])
    columns, rows = results.apply_operations(columns, rows, operations)
    if len(rows) > MAX_POINTS:
        raise ProjectServiceError(422, "too_many_points", "单张图最多绘制 3,000 个数据点，请先筛选或汇总。")
    x_col, y_col = config["x_column"], config["y_column"]
    xi, yi = results.column_index(columns, x_col), results.column_index(columns, y_col)
    if xi == yi:
        raise ProjectServiceError(422, "invalid_column", "X、Y 轴请选择不同的数据列。")
    points = []
    for index, row in enumerate(rows, start=1):
        x, y = results.numeric(row[xi]), results.numeric(row[yi])
        if x is None or y is None:
            raise ProjectServiceError(422, "invalid_numeric_data", f"第 {index} 条数据的 X 或 Y 值无效，请先处理数据。")
        points.append((x, y))
    if config["kind"] == "line":
        points.sort(key=lambda point: point[0])
    chart = config | {
        "title": config["title"].strip() or "Result",
        "x_label": config["x_label"].strip() or x_col,
        "y_label": config["y_label"].strip() or y_col,
    }
    provenance = {
        "dataset_id": dataset_id, "version_id": version_id, "version_no": source["version_no"],
        "sha256": source["sha256"], "chart": chart, "renderer": "result-svg-v1", "point_count": len(points),
    }
    if operations:
        provenance["operations"] = list(operations)
    return _render(points, chart), provenance
