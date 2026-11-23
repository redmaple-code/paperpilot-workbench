"""Context assembly: layered budgets, compression, fingerprint.

Order is fixed (S -> T -> K -> M -> H) so the static prefix stays stable and
provider prompt caching keeps working across long runs.
"""
from __future__ import annotations

import hashlib
import json

# Share of the context budget per layer.
LAYERS = (("static", 0.10), ("task", 0.15), ("knowledge", 0.40), ("memory", 0.15), ("trace", 0.20))
# Conservative chars-per-token for mixed Chinese/English content.
CHARS_PER_TOKEN = 1.6


def _budget_chars(max_tokens: int) -> dict[str, int]:
    return {name: max(200, int(max_tokens * share * CHARS_PER_TOKEN)) for name, share in LAYERS}


def _clip(text: str, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[: max(0, limit - 20)] + "\n…[已裁剪]", True


def _wrap_untrusted(source: str, body: str) -> str:
    return f'<untrusted source="{source}">\n{body}\n</untrusted>'


def render_knowledge(items: list[dict]) -> str:
    blocks = []
    for item in items or []:
        kind = item.get("kind", "material")
        if kind == "corpus":
            body = f"{item.get('title', '')}\n{item.get('content', '')}"
        elif kind == "term":
            body = f"{item.get('term', '')}：{item.get('definition', '')}"
        elif kind == "ref":
            body = f"{item.get('title', '')} ({item.get('authors', '')}, {item.get('year', '')})"
        elif kind == "search":
            body = f"{item.get('title', '')} ({item.get('year', '')}) {item.get('abstract', '')}"
        elif kind == "outline":
            body = str(item.get("content", ""))
        elif kind == "logic":
            body = f"{item.get('label', '')}：{item.get('details', '')}"
        else:
            body = json.dumps(item, ensure_ascii=False)
        blocks.append(_wrap_untrusted(kind, body))
    return "\n\n".join(blocks)


def render_memory(items: list[dict], working: dict) -> str:
    lines = []
    scratch = (working or {}).get("scratchpad") or []
    if scratch:
        lines.append("[工作记忆]")
        for note in scratch[-12:]:
            lines.append(f"- ({note.get('kind')}) {note.get('text')} [步 {note.get('source_seq')}]")
    questions = (working or {}).get("open_questions") or []
    if questions:
        lines.append("[未决问题]")
        lines.extend(f"- {text}" for text in questions[-6:])
    ledger = (working or {}).get("evidence_ledger") or []
    if ledger:
        lines.append("[已用证据]")
        lines.extend(f"- {item.get('title', '')} ({item.get('year', '')})" for item in ledger[-10:])
    if items:
        lines.append("[长期记忆]")
        for item in items:
            marker = " · 存在冲突待裁决" if item.get("conflict_group") else ""
            lines.append(f"- ({item.get('kind')}) {item.get('title', '')}{marker}")
    return "\n".join(lines)


def render_trace(steps: list[dict]) -> str:
    lines = []
    for step in steps:
        lines.append(f"[步 {step.get('superstep')}] 已记录的操作")
        if step.get("tool"):
            lines.append(f"  行动：{step['tool']}({json.dumps(step.get('arguments') or {}, ensure_ascii=False)[:200]})")
            lines.append(f"  观察：{step.get('observation', '')[:600]}")
    return "\n".join(lines)


def fold_trace(steps: list[dict], keep_recent: int = 2) -> tuple[str, list[dict]]:
    """Fold everything except the most recent steps into one summary line.

    The newest observations are never folded: losing them is the most common
    cause of the model "forgetting" what it just retrieved.
    """
    if len(steps) <= keep_recent:
        return render_trace(steps), []
    old, recent = steps[:-keep_recent], steps[-keep_recent:]
    tools = [step.get("tool") for step in old if step.get("tool")]
    summary = (f"[历史摘要] 已完成 {len(old)} 步；调用工具 {len(tools)} 次"
               f"（{', '.join(sorted(set(tools)))}）")
    return summary + "\n" + render_trace(recent), old


def assemble(*, system: str, task: str, knowledge: list[dict], memory_items: list[dict],
             working: dict, steps: list[dict], max_tokens: int) -> dict:
    budget = _budget_chars(max_tokens)
    dropped: list[dict] = []
    compressed = False

    knowledge_text = render_knowledge(knowledge)
    knowledge_text, cut = _clip(knowledge_text, budget["knowledge"])
    if cut:
        compressed = True
        dropped.append({"layer": "knowledge", "reason": "budget"})

    memory_text = render_memory(memory_items, working)
    memory_text, cut = _clip(memory_text, budget["memory"])
    if cut:
        compressed = True
        dropped.append({"layer": "memory", "reason": "budget"})

    trace_text, folded = fold_trace(steps)
    if folded:
        compressed = True
        dropped.append({"layer": "trace", "reason": "folded", "count": len(folded)})
    trace_text, cut = _clip(trace_text, budget["trace"])
    if cut:
        compressed = True
        dropped.append({"layer": "trace", "reason": "budget"})

    static_text, task_text = system, task

    sections = [
        ("static", static_text), ("task", task_text),
        ("knowledge", knowledge_text), ("memory", memory_text), ("trace", trace_text),
    ]
    text = "\n\n".join(f"### {name}\n{body}" for name, body in sections if body.strip())
    tokens = int(len(text) / CHARS_PER_TOKEN)
    overflow = tokens > max_tokens
    return {
        "text": text,
        "tokens_estimated": tokens,
        "layers": {name: len(body) for name, body in sections},
        "dropped": dropped,
        "compressed": compressed,
        "overflow": overflow,
    }


def fingerprint(*parts) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
