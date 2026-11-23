"""Agent foundation: reducers, graph engine, tool protocol, memory gates, context.

No model is stubbed here: the AI path is verified against the real provider
(see delivery notes). These tests cover the deterministic machinery only.
"""
from __future__ import annotations

import json

import app
from paperpilot.services import agent_context, agent_graph, agent_memory, agent_runtime, agent_tools
from tests.test_req022_assets import project


def memory_sources(conn, pid, *ids):
    for source in ids:
        conn.execute("INSERT INTO agent_runs(id,project_id,request_id,status,created_at,updated_at) VALUES(?,?,?,'succeeded',?,?)",
                     (source,pid,source,agent_runtime.stamp(),agent_runtime.stamp()))


# ---------------- reducers / engine ----------------

def test_reducers_keep_semantics():
    window = agent_graph.append_window(3)
    assert window([1, 2], [3, 4]) == [2, 3, 4]

    dedupe = agent_graph.append_dedupe("text")
    assert dedupe([{"text": "a"}], [{"text": "a"}, {"text": "b"}]) == [{"text": "a"}, {"text": "b"}]

    merge = agent_graph.merge_by_id("id")
    merged = merge([{"id": "1", "v": 1}], [{"id": "1", "v": 2}, {"id": "2", "v": 3}])
    assert {item["id"]: item["v"] for item in merged} == {"1": 2, "2": 3}

    cap = agent_graph.append_cap(2, key="importance")
    kept = cap([{"importance": 0.1}], [{"importance": 0.9}, {"importance": 0.5}])
    assert [item["importance"] for item in kept] == [0.9, 0.5]


def test_engine_stops_on_loop_and_records_checkpoints():
    schema = agent_graph.StateSchema({"n": agent_graph.replace})
    checkpoints = []

    def loop_node(state, ctx):
        return {"n": (state.get("n") or 0) + 1}, None

    graph = agent_graph.Graph(
        name="t", version=1, schema=schema,
        nodes={"loop": agent_graph.Node("loop", loop_node)},
        edges=[agent_graph.Edge("loop", "loop")], entry="loop")

    ctx = agent_graph.RunContext(checkpoint=lambda state, step, node: checkpoints.append(step))
    state = agent_graph.Engine(graph, ctx).run({}, max_supersteps=10, max_node_visits=3)
    assert state["_stop"] == "loop_detected"
    assert len(checkpoints) == 3  # the 4th visit is blocked before it runs


def test_engine_honours_explicit_halt():
    schema = agent_graph.StateSchema({"x": agent_graph.replace})

    def halt_node(state, ctx):
        return {"x": 1, "_halt": "final"}, None

    graph = agent_graph.Graph(name="t2", version=1, schema=schema,
                              nodes={"n": agent_graph.Node("n", halt_node)},
                              edges=[], entry="n")
    state = agent_graph.Engine(graph, agent_graph.RunContext()).run({}, max_supersteps=5)
    assert state["_stop"] == "final" and state["x"] == 1


# ---------------- tool protocol ----------------

def test_unknown_tool_is_rejected_without_raising():
    result = agent_tools.call_tool({}, "does_not_exist", {})
    assert result["ok"] is False and result["error_code"] == "unknown_tool"


def test_rate_limit_and_idempotent_cache():
    state = {}
    args = {"query": "graphene stability"}
    first = agent_tools.call_tool(state, "search_academic", args)
    if not first["ok"]:
        # Offline is a legitimate outcome; the cache must still not fabricate.
        assert first["error_code"] in {"provider_unreachable", "timeout", "tool_failed"}
        return
    replay = agent_tools.call_tool(state, "search_academic", args)
    assert replay.get("cached") is True


def test_remember_requires_source_seq():
    state = {}
    missing = agent_tools.call_tool(state, "remember", {"kind": "finding", "text": "无来源"})
    assert missing["ok"] is False and missing["error_code"] == "invalid_args"  # schema rejects before handler
    ok = agent_tools.call_tool(state, "remember", {"kind": "finding", "text": "有来源", "source_seq": 3})
    assert ok["ok"] is True


def test_crash_breaker_opens_after_repeated_failures():
    state = {}
    original = agent_tools.REGISTRY["read_stage"].handler
    agent_tools.REGISTRY["read_stage"].handler = lambda args, ctx: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        results = [agent_tools.call_tool(state, "read_stage", {"stage_id": "STAGE-01"}) for _ in range(4)]
    finally:
        agent_tools.REGISTRY["read_stage"].handler = original
    assert all(not item["ok"] for item in results)
    assert any(item["error_code"] == "circuit_open" for item in results)


# ---------------- context engineering ----------------

def test_context_keeps_recent_observations_and_folds_old_ones():
    steps = [{"superstep": i, "thought": f"t{i}", "tool": "search_academic",
              "arguments": {"query": f"q{i}"}, "observation": f"obs{i}"} for i in range(8)]
    assembled = agent_context.assemble(system="S", task="T", knowledge=[], memory_items=[],
                                       working={}, steps=steps, max_tokens=4000)
    assert assembled["compressed"] is True
    assert "obs7" in assembled["text"] and "obs6" in assembled["text"]
    assert any(item.get("reason") == "folded" for item in assembled["dropped"])


def test_context_overflow_is_reported_not_hidden():
    assembled = agent_context.assemble(system="S" * 5000, task="T" * 5000, knowledge=[],
                                       memory_items=[], working={}, steps=[], max_tokens=200)
    assert assembled["overflow"] is True


# ---------------- memory ----------------

def test_long_term_memory_requires_source_and_dedupes(client):
    pid = project(client)
    conn = app.db()
    try:
        with conn:memory_sources(conn,pid,'run-1','run-2')
        with conn:
            rejected = agent_memory.write(conn, pid, {
                "kind": "semantic", "title": "无来源", "content": {"idea": "x"},
                "source_kind": "", "source_id": ""})
            assert rejected["ok"] is False and rejected["gate"]["gate"] == "G2"

        with conn:
            first = agent_memory.write(conn, pid, {
                "kind": "semantic", "title": "有来源", "content": {"idea": "证据聚合协议"},
                "source_kind": "agent_run", "source_id": "run-1", "importance": 0.6})
            assert first["ok"] is True

        with conn:
            duplicate = agent_memory.write(conn, pid, {
                "kind": "semantic", "title": "有来源", "content": {"idea": "证据聚合协议"},
                "source_kind": "agent_run", "source_id": "run-2", "importance": 0.6})
            assert duplicate["ok"] is False and duplicate["gate"]["gate"] == "G1"

        assert agent_memory.recall(conn,pid,'证据聚合')['items']==[]  # model memories await explicit adoption
        with conn:assert agent_memory.decide(conn,pid,first['id'],'approve')['ok']
        recalled = agent_memory.recall(conn, pid, "证据聚合")
        assert recalled["items"]
        listed = agent_memory.list_memories(conn, pid, "semantic")
        assert listed[0]["source_kind"] == "agent_run"
    finally:
        conn.close()


def test_memory_conflict_is_recorded_not_overwritten(client):
    pid = project(client)
    conn = app.db()
    try:
        with conn:memory_sources(conn,pid,'r1','r2')
        with conn:
            a = agent_memory.write(conn, pid, {
                "kind": "semantic", "title": "A", "content": {"idea": "方法甲可以解决稳定性问题"},
                "source_kind": "agent_run", "source_id": "r1", "importance": 0.6})
        with conn:
            b = agent_memory.write(conn, pid, {
                "kind": "semantic", "title": "B", "content": {"idea": "方法甲可以解决稳定性问题的反例"},
                "source_kind": "agent_run", "source_id": "r2", "importance": 0.6})
        assert a["ok"] and b["ok"]
        groups = [row["conflict_group"] for row in conn.execute(
            "SELECT conflict_group FROM agent_memories WHERE project_id=? AND conflict_group!=''", (pid,))]
        assert groups, "conflict should be recorded, not silently overwritten"
    finally:
        conn.close()


def test_memory_delete_takes_effect(client):
    pid = project(client)
    conn = app.db()
    try:
        with conn:memory_sources(conn,pid,'r3')
        with conn:
            created = agent_memory.write(conn, pid, {
                "kind": "semantic", "title": "待删", "content": {"idea": "会被删除的记忆条目"},
                "source_kind": "agent_run", "source_id": "r3", "importance": 0.6})
        with conn:
            assert agent_memory.decide(conn, pid, created["id"], "delete")["ok"] is True
        assert agent_memory.list_memories(conn, pid, "semantic") == []
    finally:
        conn.close()


# ---------------- card quality ----------------

def test_placeholder_cards_are_rejected():
    assert agent_runtime.accept_card({"idea": "候选占位，待补充研究构思"}) is False
    assert agent_runtime.accept_card({"idea": "短"}) is False
    assert agent_runtime.accept_card({"idea": "用弱证据投票合成分级并标注冲突位点"}) is True
