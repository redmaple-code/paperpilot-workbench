from __future__ import annotations

from paperpilot.services.conversion import svg_escape


def flow_svg(title: str, labels: list[str]) -> str:
    labels = [str(x)[:80] for x in (labels or ["问题", "方法", "证据", "结论"])[:8]]
    width, height = 220 * max(1, len(labels)), 220
    chunks = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="#f8faff"/><text x="20" y="30" font-family="sans-serif" font-size="18" fill="#172033">{svg_escape(title[:80])}</text>'
    ]
    for i, label in enumerate(labels):
        x = 20 + i * 220
        if i:
            chunks.append(f'<path d="M{x-28} 120 L{x} 120" stroke="#315efb" stroke-width="3" marker-end="url(#arrow)"/>')
        chunks.append(f'<rect x="{x}" y="75" width="170" height="90" rx="12" fill="#fff" stroke="#315efb"/><text x="{x+85}" y="125" text-anchor="middle" font-family="sans-serif" font-size="14" fill="#172033">{svg_escape(label)}</text>')
    chunks.insert(1, '<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#315efb"/></marker></defs>')
    return "".join(chunks) + "</svg>"
