"""Business providers for Agent. Read/search or propose only, never apply."""
from __future__ import annotations
import json
import threading
import uuid
from paperpilot.services import agent_actions, academic_search
from paperpilot.services.project_errors import ProjectServiceError

_dependencies = {}


def configure(*, adapters, search, search_cards):
    _dependencies.update(adapters=adapters, search=search, search_cards=search_cards)


def keyword_search(query):
    try:
        provider = academic_search.provider_from_env()
        result = provider.search(query, {}, None, 8)
        items = [academic_search.normalize_candidate(raw, raw.get("__provider") or provider.name) for raw in result.get("results", []) if isinstance(raw, dict)]
        # This is retrieval, never a semantic novelty judgement.
        words = set(query.casefold().split())
        for item in items:
            title = set(item.get("title", "").casefold().split())
            ratio = len(words & title) / max(1, len(words))
            item["level"] = "high" if ratio >= .7 else "medium" if ratio >= .35 else "low"
        return {"ok": True, "basis": "keyword_only", "sources": [{k: i.get(k, "") for k in ("id", "title", "doi", "year", "provider", "level")} for i in items],
                "level": max((i["level"] for i in items), key=lambda level: {"low": 1, "medium": 2, "high": 3}[level]) if items else "unknown"}
    except academic_search.AcademicSearchError as exc:
        return {"ok": False, "basis": "keyword_only_unavailable", "sources": [], "level": "unknown", "error_code": exc.code, "retryable": exc.retryable}
    except Exception:
        return {"ok": False, "basis": "keyword_only_unavailable", "sources": [], "level": "unknown", "error_code": "provider_unavailable", "retryable": True}


def search(args, context):
    response = _dependencies["search"](context["project_id"], args["query"], context.get("conversation_id", ""))
    pointers = []
    for card in response.get("cards", []):
        pointers.append({**card, "summary": "临时检索候选；内容未保存，过期后请重新检索。",
                         "metadata": {**{k: card["metadata"].get(k, "") for k in ("year", "doi", "provider", "access_status")}, "has_oa": bool(card["metadata"].get("oa_url"))}})
    # The runtime may persist tool exchanges. Return reference pointers, not the
    # transient provider response or abstracts, so that does not promote a search.
    return {"ok": response.get("ok", False), "status": response.get("status"), "cards": pointers,
            "items": [{"id": c["payload"]["candidate_id"], "search_id": c["payload"]["search_id"], "title": c["title"], **c["metadata"]} for c in pointers],
            "error": response.get("error"), "note": "检索内容按临时会话规则保留，引用指针可用于追问。"}


def business_context(args, context):
    from paperpilot.services import outline, logic_graph, workflow
    conn, pid = context["conn"], context["project_id"]
    project = agent_actions.require_writable(conn, pid)
    return {"project_revision": project["project_revision"], "outline_version": outline.current_version(conn, pid),
            "logic_version": logic_graph.read_graph(conn, pid)["version"], "workflow_version": workflow.current_snapshot(conn, pid)["version"]}


def propose(operation, args, context):
    conn, pid = context["conn"], context["project_id"]
    data = {"request_id": args.get("request_id") or uuid.uuid4().hex, "operation": operation,
            "base_version": args["base_version"], "payload": args["payload"], "source_refs": args.get("source_refs", []),
            "conversation_id": context.get("conversation_id", ""), "run_id": context.get("run_id", (context.get("state") or {}).get("run_id", ""))}
    with conn:
        if not conn.in_transaction:
            conn.execute("BEGIN IMMEDIATE")
        result = agent_actions.create(conn, pid, data, _dependencies["adapters"])
    return {"ok": True, "card": result, "proposal": result, "note": "仅创建提案，尚未修改领域内容。"}


def propose_outline(args, context):
    if "payload" not in args:
        command = args.get("command")
        op = {"create": "add_node", "update": "update_node", "delete": "delete_node"}.get(command)
        if not op:
            raise ProjectServiceError(422, "invalid_operation", "请选择有效的结构操作。")
        operation = {"op": op}
        if command == "create":
            operation["node"] = {"id": uuid.uuid4().hex, **(args.get("node") or {})}
        elif command == "update":
            operation.update(id=args.get("node_id"), changes=args.get("node") or {})
        else:
            operation["id"] = args.get("node_id")
        args = {**args, "payload": {"operations": [operation]}}
    return propose("outline", args, context)


def get_tools():
    from paperpilot.services.agent_tools import Tool, WRITE_CANDIDATE
    schema = {"type": "object", "properties": {"base_version": {"type": "integer"}, "payload": {"type": "object"}, "source_refs": {"type": "array", "items": {"type": "object"}}}, "required": ["base_version", "payload"]}
    tools = [Tool("business_search", "检索真实文献并返回有来源与访问状态的卡片；不会永久保存未选结果。",
                  {"type": "object", "properties": {"query": {"type": "string", "maxLength": 240}}, "required": ["query"]}, search),
             Tool("business_context", "读取领域版本；资源/文献用project_revision，结构用对应版本，阶段候选用workflow_version。", {"type": "object", "properties": {}}, business_context),
             Tool("propose_business", "创建业务提案，先用business_context读取版本。operation=planning时用workflow_version，payload为{stage_id,fields,mode:'draft'或'confirm',revise:布尔}，通过对话形成方案供作者采纳；不能自动确认。",
                  {**schema, "properties": {**schema["properties"], "operation": {"type": "string", "enum": sorted(agent_actions.OPERATIONS)}}, "required": ["operation", "base_version", "payload"]},
                  lambda args, context: propose(args["operation"], args, context), side_effect=WRITE_CANDIDATE),
             Tool("propose_outline", "提出大纲修改，作者预览并明确采纳后才应用。", schema, propose_outline, side_effect=WRITE_CANDIDATE)]
    for kind in ["logic", "term", "corpus", "research"]:
        tools.append(Tool("propose_"+kind, "创建"+kind+"候选，仅提案，不自动应用。base_version来自对应领域版本。", schema,
                          lambda args, context, kind=kind: propose(kind, args, context), side_effect=WRITE_CANDIDATE))
    return tools


def install_legacy_bridge():
    """06 compatibility wiring until 01's provider registration is delivered.

    No runtime/panel source edits. Replace only the known unsafe outline binding;
    search uses the existing name, so old intent selection can discover it.
    """
    from paperpilot.services import agent_tools
    if getattr(agent_tools, "_business_bridge_installed", False):
        return
    old = agent_tools.REGISTRY.get("edit_outline_nodes")
    if old:
        from dataclasses import replace
        agent_tools.REGISTRY["edit_outline_nodes"] = replace(old, handler=propose_outline,
            description="创建大纲修改提案，必须由作者预览、明确应用后才改结构。")
    old_search = agent_tools.REGISTRY.get("search_academic")
    if old_search:
        from dataclasses import replace
        agent_tools.REGISTRY["search_academic"] = replace(old_search, handler=search)
    if hasattr(agent_tools, "register_provider"):
        import sys
        agent_tools.register_provider(sys.modules[__name__])
    else:
        for tool in get_tools():
            if tool.name in agent_tools.REGISTRY:
                raise RuntimeError("Duplicate business tool: " + tool.name)
            agent_tools.register(tool)
    original = agent_tools.tool_schemas

    def schemas(intent="", **kwargs):
        # Select from the full eligible registry before applying the provider cap.
        # Otherwise this provider can hide extensions registered after startup.
        existing = original(intent, **{**kwargs, "max_tools": len(agent_tools.REGISTRY)})
        relevant = {
            "writing": {"write_draft", "read_outline", "edit_outline_nodes", "read_stage", "remember", "write_candidate"},
            "structure": {"read_outline", "edit_outline_nodes", "read_stage", "remember", "write_draft"},
            "research": {"search_academic", "read_knowledge", "read_stage", "check_duplication", "remember", "write_candidate"},
        }
        relevant['scholar'] = relevant['research']
        own_names = {tool.name for tool in get_tools()}
        extensions = [tool.name for tool in agent_tools.REGISTRY.values()
                      if getattr(tool, "extension", False) and tool.name not in own_names
                      and (not tool.intents or intent in tool.intents)]
        if intent in relevant:
            existing = [s for s in existing if s["function"]["name"] in relevant[intent] or s["function"]["name"] in extensions]
        else:
            existing = [s for s in existing if s["function"]["name"] not in own_names]
        names = {s["function"]["name"] for s in existing}
        wanted = ["propose_business", "business_context"] if intent != "writing" else []
        # Preserve original required outlet tools. Capability filtering is handled by 01.
        for name in wanted:
            if name not in names:
                existing.append(agent_tools.REGISTRY[name].schema())
        cap = kwargs.get("max_tools") or agent_tools.MAX_TOOLS_PER_CALL
        if intent == 'scholar':
            priority = ['scholar_research', 'search_academic', 'read_knowledge', 'read_stage', 'check_duplication']
            priority += extensions + ['business_context', 'propose_business', 'write_candidate', 'remember']
            names = list(dict.fromkeys(priority))
            return [agent_tools.REGISTRY[name].schema() for name in names if name in agent_tools.REGISTRY][:cap]
        if wanted:
            # Keep the final answer outlet and expose one general proposal tool
            # rather than spending the gateway's budget on every resource type.
            priority = wanted + extensions + [s["function"]["name"] for s in existing if s["function"]["name"] not in wanted]
            unique = list(dict.fromkeys(priority))
            if "write_candidate" in unique and unique.index("write_candidate") >= cap:
                unique.remove("write_candidate");unique.insert(cap-1,"write_candidate")
            return [agent_tools.REGISTRY[name].schema() for name in unique[:cap]]
        return existing[:cap]
    agent_tools.tool_schemas = schemas
    agent_tools._business_bridge_installed = True
