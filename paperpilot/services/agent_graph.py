"""Graph execution: state reducers, nodes, edges, supersteps, checkpoints.

Deliberately small: one node per superstep, parallelism lives inside a node.
That keeps routing deterministic and replay exact without a scheduler.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

END = "__end__"


# ---------- reducers: (current, delta) -> new ----------

def replace(current, delta):
    return delta


def append_window(max_items: int):
    def apply(current, delta):
        merged = list(current or []) + list(delta or [])
        return merged[-max_items:]
    return apply


def append_cap(max_items: int, key: str = ""):
    """Append and keep the highest-importance items when over capacity."""
    def apply(current, delta):
        merged = list(current or []) + list(delta or [])
        if len(merged) <= max_items:
            return merged
        if not key:
            return merged[-max_items:]
        indexed = list(enumerate(merged))
        indexed.sort(key=lambda pair: (float(pair[1].get(key, 0) or 0), pair[0]))
        keep = {index for index, _ in indexed[-max_items:]}
        return [item for index, item in enumerate(merged) if index in keep]
    return apply


def append_dedupe(key: str):
    def apply(current, delta):
        seen = {str(item.get(key, "")) for item in (current or []) if isinstance(item, dict)}
        merged = list(current or [])
        for item in (delta or []):
            if not isinstance(item, dict):
                continue
            marker = str(item.get(key, ""))
            if marker in seen:
                continue
            seen.add(marker)
            merged.append(item)
        return merged
    return apply


def merge_by_id(key: str = "id"):
    def apply(current, delta):
        merged = {str(item.get(key)): item for item in (current or []) if isinstance(item, dict)}
        for item in (delta or []):
            if isinstance(item, dict):
                merged[str(item.get(key))] = item
        return list(merged.values())
    return apply


def merge_lru(max_items: int):
    def apply(current, delta):
        merged = dict(current or {})
        merged.update(delta or {})
        if len(merged) > max_items:
            for stale in list(merged.keys())[:-max_items]:
                merged.pop(stale, None)
        return merged
    return apply


def sum_counters(keys: tuple[str, ...]):
    def apply(current, delta):
        merged = dict(current or {})
        for key in keys:
            merged[key] = float(merged.get(key, 0) or 0) + float((delta or {}).get(key, 0) or 0)
        return merged
    return apply


class StateSchema:
    def __init__(self, fields: dict[str, Callable]):
        self.fields = fields

    def apply(self, state: dict, delta: dict) -> dict:
        out = dict(state)
        for key, value in (delta or {}).items():
            reducer = self.fields.get(key)
            out[key] = reducer(out.get(key), value) if reducer else value
        return out


@dataclass
class Node:
    name: str
    fn: Callable[[dict, Any], tuple[dict, str | None]]
    kind: str = "llm"


@dataclass
class Edge:
    src: str
    dst: str
    when: Callable[[dict], bool] | None = None


@dataclass
class Graph:
    name: str
    version: int
    schema: StateSchema
    nodes: dict[str, Node]
    edges: list[Edge]
    entry: str

    def route(self, node_name: str, state: dict) -> str:
        candidates = [edge for edge in self.edges if edge.src == node_name]
        for edge in candidates:
            if edge.when is None or edge.when(state):
                return edge.dst
        return END

    def definition(self) -> dict:
        return {
            "name": self.name,
            "version": self.version,
            "entry": self.entry,
            "nodes": [{"name": node.name, "kind": node.kind} for node in self.nodes.values()],
            "edges": [{"src": edge.src, "dst": edge.dst, "conditional": edge.when is not None} for edge in self.edges],
        }


@dataclass
class RunContext:
    """Everything a node needs that is not part of the reducible state."""
    run_id: str = ""
    project_id: str = ""
    stage_id: str = ""
    limits: dict = field(default_factory=dict)
    checkpoint: Callable[[dict, int, str], None] | None = None
    emit: Callable[[str, dict], None] | None = None

    def log(self, kind: str, payload: dict) -> None:
        if self.emit:
            self.emit(kind, payload)


class Engine:
    """Runs a graph one node per superstep, checkpointing after each."""

    def __init__(self, graph: Graph, context: RunContext):
        self.graph = graph
        self.context = context

    def run(self, state: dict, max_supersteps: int = 12, max_node_visits: int = 6) -> dict:
        identity = {"name": self.graph.name, "version": self.graph.version}
        if state.get("_graph") and state["_graph"] != identity:
            raise ValueError("checkpoint_graph_incompatible")
        state["_graph"] = identity
        node_name = state.get("_next_node", self.graph.entry)
        visits = dict(state.get("_visits") or {})
        state.pop("_halt", None)
        state.pop("_stop", None)
        for superstep in range(int(state.get("_supersteps", 0)), max_supersteps):
            if self.context.limits.get("check"):
                self.context.limits["check"]()
            if node_name == END or node_name not in self.graph.nodes:
                state["_stop"] = "final"
                return state
            visits[node_name] = visits.get(node_name, 0) + 1
            if visits[node_name] > max_node_visits:
                state["_stop"] = "loop_detected"
                return state

            node = self.graph.nodes[node_name]
            self.context.log("step_started", {"superstep": superstep, "node": node_name})
            delta, override = node.fn(state, self.context)
            if self.context.limits.get("check"):
                self.context.limits["check"]()
            state = self.graph.schema.apply(state, delta or {})
            state["_supersteps"] = superstep + 1
            state["_visits"] = visits
            if state.get('_halt') and state['_halt'] != 'final':
                state['_resume_node'] = node_name
            state["_next_node"] = END if state.get("_halt") else override or self.graph.route(node_name, state)
            self.context.log("step_finished", {"superstep": superstep, "node": node_name})

            if self.context.checkpoint:
                self.context.checkpoint(state, superstep, state["_next_node"])
            if state.get("_halt"):
                state["_stop"] = state.get("_halt")
                return state
            node_name = state["_next_node"]
        state["_stop"] = "max_supersteps"
        return state


def public_state(state: dict) -> dict:
    """State minus internal markers, safe to persist or return."""
    return {key: value for key, value in state.items() if not key.startswith("_")}


def dump(state: dict) -> str:
    return json.dumps(state, ensure_ascii=False, default=str)
