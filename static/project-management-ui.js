(() => {
  const $ = selector => document.querySelector(selector);
  const pp = window.paperpilot;
  if (!pp) return;

  const state = {
    projectId: "",
    lastData: null,
    graph: null,
    graphView: "graph",
    editingNodeId: null,
    suggestion: null,
    searchTimer: null,
    searchToken: 0,
    syncBusy: false
  };
  const loadProjectOriginal = pp.loadProject.bind(pp);

  function uid() {
    return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, character => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    })[character]);
  }

  function errorText(error) {
    try {
      const body = JSON.parse(error.message);
      const detail = body.detail || body;
      return detail.message || detail.code || error.message;
    } catch {
      return error.message || "请求失败";
    }
  }

  function projectId() {
    return pp.data?.project?.id || pp.current?.project?.id || "";
  }

  function selectedFile() {
    return pp.data?.files?.find(file => file.path === $("#fileSelect")?.value) || null;
  }

  function dirtyState() {
    const data = pp.data;
    if (!data?.project) return {};
    const file = selectedFile();
    return {
      file: Boolean(file && $("#editor").value !== file.content),
      outline: $("#outlineText").value !== (data.outline || ""),
      settings: $("#projectName").value !== (data.project.name || "")
        || $("#domain").value !== (data.project.domain || "")
        || $("#style").value !== (data.project.style || "")
        || $("#model").value !== (data.project.model || "")
        || $("#aiBaseUrl").value !== (data.project.ai_base_url || "")
    };
  }

  function dirtyLabels(dirty) {
    return [dirty.file && "当前文件", dirty.outline && "大纲", dirty.settings && "论文配置"].filter(Boolean);
  }

  async function persistSettings(values) {
    await pp.api(`/api/projects/${projectId()}`, {
      method: "PUT",
      body: JSON.stringify(values)
    });
  }

  async function saveEdits() {
    const id = projectId();
    const data = pp.data;
    const dirty = dirtyState();
    const settings = {
      name: $("#projectName").value,
      domain: $("#domain").value,
      style: $("#style").value,
      model: $("#model").value,
      ai_base_url: $("#aiBaseUrl").value.trim()
    };
    if (data.workflow_enabled) {
      if (dirty.file && !(await pp.saveCurrentEdit?.("file"))) throw new Error("当前文件尚未保存，请先处理编辑器的保存提示。");
      if (dirty.outline && !(await pp.saveCurrentEdit?.("outline"))) throw new Error("大纲尚未保存，请核对版本后重试。");
    } else {
      if (dirty.file) {
        const file = selectedFile();
        await pp.api(`/api/projects/${id}/files`, {
          method: "PUT", body: JSON.stringify({ path: file.path, content: $("#editor").value })
        });
      }
      if (dirty.outline) {
        await pp.api(`/api/projects/${id}/outline`, {
          method: "PUT", body: JSON.stringify({ content: $("#outlineText").value, base_version: data.outline_version || 0 })
        });
      }
    }
    if (dirty.settings) await persistSettings(settings);
    await pp.reload();
  }

  async function keepDraftAndSwitch() {
    const dirty = dirtyState();
    const settings = {
      name: $("#projectName").value,
      domain: $("#domain").value,
      style: $("#style").value,
      model: $("#model").value,
      ai_base_url: $("#aiBaseUrl").value.trim()
    };
    if (dirty.file && !(await pp.keepCurrentDraft?.("file"))) throw new Error("当前文件草稿没有保存。");
    if (dirty.outline && !(await pp.keepCurrentDraft?.("outline"))) throw new Error("大纲草稿没有保存。");
    if (dirty.settings) await persistSettings(settings);
    await pp.reload();
  }

  function requestSwitch(targetId, dirty) {
    const dialog = $("#projectSwitchDialog");
    const description = $("#projectSwitchDescription");
    const host = $("#projectSwitchActions");
    const labels = dirtyLabels(dirty).join("、");
    const workflowEnabled = Boolean(pp.data?.workflow_enabled);
    description.textContent = `当前${labels}尚未保存，请处理后切换论文。`;
    host.replaceChildren();
    return new Promise(resolve => {
      let finished = false;
      const finish = value => {
        if (finished) return;
        finished = true;
        dialog.removeEventListener("cancel", onCancel);
        dialog.close();
        resolve(value);
      };
      const onCancel = event => {
        event.preventDefault();
        finish(false);
      };
      dialog.addEventListener("cancel", onCancel);
      const addAction = (label, action, run) => {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = label;
        if (action === "save") button.className = "primary";
        button.addEventListener("click", async () => {
          button.disabled = true;
          try {
            if (run) await run();
            if (action === "discard") pp.flash(dirty.outline ? "已放弃本页更改；如大纲草稿已自动保存，仍可恢复" : "已放弃本页未保存更改");
            finish(action !== "stay");
            if (action !== "stay") await loadProjectOriginal(targetId);
          } catch (error) {
            description.textContent = errorText(error);
            button.disabled = false;
          }
        });
        host.append(button);
      };
      addAction("保存后切换", "save", saveEdits);
      if (workflowEnabled && pp.isRevisionActive?.()) addAction("保留为草稿并切换", "keep", keepDraftAndSwitch);
      if ((!workflowEnabled || !pp.isRevisionActive?.()) && dirty.outline && !dirty.file && !dirty.settings)
        addAction("保留大纲草稿并切换", "keep", async () => {
          if (!(await pp.keepOutlineTextDraft?.())) throw new Error("大纲草稿未保存，请重试。");
        });
      if (!workflowEnabled || !pp.isRevisionActive?.()) addAction("放弃本页更改并切换", "discard", null);
      addAction("继续编辑", "stay", null);
      dialog.showModal();
    });
  }

  pp.loadProject = async projectIdToLoad => {
    const currentId = pp.current?.project?.id;
    if (currentId && currentId !== projectIdToLoad) {
      let dirty = dirtyState();
      if (dirty.file && pp.flushEditorChanges) {
        await pp.flushEditorChanges();
        if (pp.current?.project?.id !== currentId) return;
        dirty = dirtyState();
      }
      if (dirtyLabels(dirty).length && !(await requestSwitch(projectIdToLoad, dirty))) return;
      if (dirtyLabels(dirty).length) return;
    }
    await loadProjectOriginal(projectIdToLoad);
  };

  function renderProjectItems(items) {
    const host = $("#projects");
    host.replaceChildren();
    if (!items.length) {
      host.textContent = "没有匹配的论文";
      return;
    }
    for (const item of items) {
      const row = document.createElement("div");
      row.className = `project-item ${projectId() === item.id ? "selected" : ""}`;
      row.dataset.id = item.id;
      row.tabIndex = 0;
      row.setAttribute("role", "button");
      row.setAttribute("aria-current", projectId() === item.id ? "page" : "false");
      const name = document.createElement("strong");
      name.textContent = item.name;
      const meta = document.createElement("small");
      meta.textContent = [item.domain || "未设置领域", item.archived ? "已归档" : ""].filter(Boolean).join(" · ");
      row.append(name, meta);
      row.addEventListener("click", () => pp.loadProject(item.id));
      row.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          pp.loadProject(item.id);
        }
      });
      host.append(row);
    }
  }

  async function renderScopedProjects() {
    const token = ++state.searchToken;
    const query = $("#projectSearch").value.trim();
    const scope = $("#projectSearchScope").value;
    try {
      let items;
      if (!query && scope === "current") {
        items = (await pp.api("/api/projects")).filter(item => !item.archived);
      } else {
        const params = new URLSearchParams({ scope, query });
        if (projectId()) params.set("current_project_id", projectId());
        const result = await pp.api(`/api/library/projects?${params}`);
        items = result.items || [];
      }
      if (token === state.searchToken) renderProjectItems(items);
    } catch (error) {
      if (token === state.searchToken) $("#projects").textContent = errorText(error);
    }
  }

  async function renderCopySources() {
    if (!$("#copyProject")) return;
    try {
      const projects = await pp.api("/api/projects");
      const published = projects.filter(item => item.lifecycle_state === "published");
      const select = $("#copyProject");
      const previous = select.value;
      select.replaceChildren();
      for (const item of published) {
        const option = document.createElement("option");
        option.value = item.id;
        option.textContent = `${item.name} · ${lifecycleLabel(item.lifecycle_state)} · v${item.project_revision}${item.archived ? " · 已归档" : ""}`;
        select.append(option);
      }
      if ([...select.options].some(option => option.value === previous)) select.value = previous;
      if (!published.length) {
        const option = document.createElement("option");
        option.value = "";
        option.textContent = "没有已发布的源论文";
        select.append(option);
      }
      window.refreshCopyResources?.();
    } catch (error) {
      setMessage("#copyStatus", errorText(error), true);
    }
  }

  function renderResourceOrigins() {
    const data = pp.data;
    if (!data) return;
    for (const [selector, resources] of [
      ["#termList", data.terms || []], ["#corpusList", data.corpus || []], ["#refList", data.refs || []]
    ]) {
      const list = $(selector);
      [...list.children].forEach((item, index) => {
        const resource = resources[index];
        if (!resource?.source_project_id) return;
        const source = item.querySelector("small") || document.createElement("small");
        source.textContent = `来源 ${resource.source_project_id} · ${resource.source_project_state || "来源状态未知"} · v${resource.source_project_revision || 0}${resource.source_project_archived ? " · 已归档" : ""}`;
        if (!source.parentElement) item.append(source);
      });
    }
  }

  async function copyPublishedResource() {
    const dirty = dirtyLabels(dirtyState());
    if (dirty.length) {
      setMessage("#copyStatus", "先保存或切换到已保存状态，再复制资源。", true);
      return;
    }
    const sourceId = $("#copyProject").value;
    const resourceId = $("#copyId").value;
    if (!sourceId || !resourceId) return setMessage("#copyStatus", "请选择已发布论文中的资源。", true);
    try {
      const result = await pp.api(`/api/projects/${projectId()}/copy-resource`, {
        method: "POST",
        body: JSON.stringify({
          source_project_id: sourceId, resource_type: $("#copyType").value, resource_id: resourceId
        })
      });
      await pp.reload();
      const source = result.source;
      setMessage("#copyStatus", `资源已复制为独立快照 · 来源 ${source.project_id} · ${lifecycleLabel(source.lifecycle_state)} v${source.project_revision}${source.archived ? " · 已归档" : ""}`);
      renderResourceOrigins();
    } catch (error) {
      setMessage("#copyStatus", errorText(error), true);
    }
  }

  function setMessage(selector, message, error = false) {
    const element = $(selector);
    if (!element) return;
    element.textContent = message || "";
    element.dataset.kind = error ? "error" : "info";
    if (error) {
      if (!element.hasAttribute("tabindex")) element.tabIndex = -1;
      element.focus();
    }
  }

  function lifecycleLabel(value) {
    return ({ draft: "草稿", in_review: "审阅中", published: "已发布" })[value] || value || "未知";
  }

  function renderLifecycle(result) {
    const project = pp.data?.project;
    if (!project) return;
    const release = result?.release_check;
    $("#projectLifecycleState").textContent = `${lifecycleLabel(project.lifecycle_state)} · v${project.project_revision}${project.archived ? " · 已归档" : ""}`;
    $("#projectReleaseSummary").textContent = release
      ? `最近检查：${release.current ? "当前版本" : "已过期"} · ${release.compile_ok ? "编译通过" : "编译未通过"} · 缺失引用 ${release.missing_citations?.length || 0} · 逻辑缺口 ${release.logic_gaps?.length || 0} · 冲突 ${release.logic_conflicts?.length || 0}`
      : "尚无当前版本的发布检查";
    $("#lifecycleContinueField").hidden = !release || !(release.logic_gaps?.length || release.logic_conflicts?.length);
    const actions = $("#lifecycleActions");
    actions.replaceChildren();
    const commands = project.lifecycle_state === "draft"
      ? [["submit_review", "送审"]]
      : project.lifecycle_state === "in_review"
        ? [["return_to_draft", "退回草稿"], ["publish", "发布"]]
        : [["withdraw_publication", "撤回发布"]];
    for (const [command, label] of commands) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = label;
      button.dataset.lifecycleCommand = command;
      button.className = command === "publish" ? "primary" : "";
      button.addEventListener("click", () => runLifecycleCommand(command));
      actions.append(button);
    }
  }

  async function refreshLifecycle() {
    if (!projectId()) return;
    try {
      const result = await pp.api(`/api/projects/${projectId()}/lifecycle`);
      renderLifecycle(result);
    } catch (error) {
      setMessage("#lifecycleMessage", errorText(error), true);
    }
  }

  async function runLifecycleCommand(command) {
    const dirty = dirtyState();
    if (dirtyLabels(dirty).length) {
      setMessage("#lifecycleMessage", "请先保存当前更改，再继续论文状态转换。", true);
      $(dirty.file ? "#saveFile" : dirty.outline ? "#saveOutline" : "#saveSettings").focus();
      return;
    }
    try {
      const project = pp.data.project;
      const result = await pp.api(`/api/projects/${projectId()}/lifecycle`, {
        method: "POST",
        body: JSON.stringify({
          command,
          request_id: uid(),
          base_version: Number(project.project_revision),
          continue_reason: $("#lifecycleContinueReason").value.trim()
        })
      });
      setMessage("#lifecycleMessage", `状态已更新：${lifecycleLabel(result.lifecycle_state)}`);
      await pp.reload();
      await refreshLifecycle();
      await renderScopedProjects();
    } catch (error) {
      setMessage("#lifecycleMessage", errorText(error), true);
    }
  }

  async function runReleaseCheck() {
    if (dirtyLabels(dirtyState()).length) {
      setMessage("#lifecycleMessage", "请先保存正文和大纲，再运行发布检查。", true);
      return;
    }
    const button = $("#runReleaseCheck");
    button.disabled = true;
    button.textContent = "正在编译和检查…";
    try {
      const pid = projectId();
      let result = await pp.api(`/api/projects/${pid}/compile`, { method: "POST", body: JSON.stringify({request_id: uid(), purpose: "submitted"}) });
      result = result.build_job || result;
      const jobId = result.id || result.job_id;
      const terminal = ["succeeded", "failed", "cancelled", "superseded", "interrupted"];
      while (!terminal.includes(result.status)) {
        if (pid !== projectId()) return;
        await new Promise(resolve => setTimeout(resolve, 700));
        if (pid !== projectId()) return;
        result = await pp.api(`/api/projects/${pid}/builds/${jobId}`);
      }
      if (pid !== projectId()) return;
      const lifecycle = await pp.api(`/api/projects/${pid}/lifecycle`);
      if (pid !== projectId()) return;
      const check = lifecycle.release_check;
      const passed = result.status === "succeeded" && check?.current && check?.compile_ok && !(check?.missing_citations?.length);
      setMessage("#lifecycleMessage", passed ? "提交版编译通过。请核对逻辑缺口、冲突和送审状态后发布。" : `发布检查未通过：${(result.diagnostics || []).map(item => item.message || item.code).join("；") || result.log || "请查看构建诊断、引用或版本状态。"}`, !passed);
      if (result.status === "succeeded") pp.openBuildReader?.(pid, jobId);
      renderLifecycle(lifecycle);
    } catch (error) {
      setMessage("#lifecycleMessage", errorText(error), true);
    } finally {
      button.disabled = false;
      button.textContent = "编译并检查发布条件";
    }
  }

  const edgeLabels = {
    supports: "支持", provides_evidence: "提供证据", evidence_for: "提供证据", cites: "引用",
    derived_from: "推导自", contradicts: "反驳"
  };
  const kindLabels = {
    claim: "论点", evidence: "证据", reasoning: "推理", conclusion: "结论", gap: "缺口"
  };

  function renderGraph() {
    const graph = state.graph;
    if (!graph) return;
    const nodesHost = $("#logicGraphNodes");
    const edgesHost = $("#logicGraphEdges");
    nodesHost.replaceChildren();
    edgesHost.replaceChildren();
    const narrowView = window.matchMedia("(max-width: 560px)").matches;
    const effectiveView = narrowView ? "list" : state.graphView;
    $("#logicGraphView").classList.toggle("active", effectiveView === "graph");
    $("#logicGraphView").setAttribute("aria-pressed", String(effectiveView === "graph"));
    $("#logicListView").classList.toggle("active", effectiveView === "list");
    $("#logicListView").setAttribute("aria-pressed", String(effectiveView === "list"));
    $("#logicGraphStatus").textContent = `图版本 v${graph.version} · ${graph.nodes.length} 个节点 · ${graph.edges.length} 条关系`;
    nodesHost.classList.toggle("list-mode", effectiveView === "list");
    const nodeById = new Map(graph.nodes.map(node => [node.id, node]));
    for (const node of graph.nodes) {
      const card = document.createElement("article");
      card.className = "logic-node";
      const header = document.createElement("div");
      header.className = "logic-node-heading";
      const title = document.createElement("strong");
      title.textContent = node.label;
      const kind = document.createElement("span");
      kind.className = `logic-kind logic-kind-${node.kind}`;
      kind.textContent = kindLabels[node.kind] || node.kind;
      header.append(title, kind);
      card.append(header);
      if (node.details) {
        const detail = document.createElement("p");
        detail.textContent = node.details;
        card.append(detail);
      }
      if (node.source_anchor) {
        const source = document.createElement("small");
        source.textContent = `来源 ${node.source_anchor}${node.source_version ? ` · v${node.source_version}` : ""} · ${node.source_status || "未确认"}`;
        card.append(source);
      }
      const outgoing = graph.edges.filter(edge => edge.from_node_id === node.id);
      const relations = document.createElement("div");
      relations.className = "logic-node-relations";
      const relationText = outgoing.map(edge => `${edgeLabels[edge.edge_type] || edge.edge_type} → ${nodeById.get(edge.to_node_id)?.label || edge.to_node_id}`);
      if (node.legacy_links) relationText.push(`旧版关联（待确认）：${node.legacy_links}`);
      relations.textContent = relationText.length ? relationText.join("；") : "暂无外出关系";
      card.append(relations);
      const actions = document.createElement("div");
      actions.className = "logic-node-actions";
      const edit = document.createElement("button");
      edit.type = "button";
      edit.textContent = "编辑";
      edit.addEventListener("click", () => editNode(node));
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "删除";
      remove.addEventListener("click", () => removeNode(node));
      actions.append(edit, remove);
      card.append(actions);
      nodesHost.append(card);
    }
    for (const edge of graph.edges) {
      const from = nodeById.get(edge.from_node_id);
      const to = nodeById.get(edge.to_node_id);
      const row = document.createElement("div");
      row.className = "logic-edge-row";
      row.textContent = `${from?.label || edge.from_node_id} · ${edgeLabels[edge.edge_type] || edge.edge_type} → ${to?.label || edge.to_node_id}`;
      edgesHost.append(row);
    }
    const issues = $("#logicGraphIssues");
    issues.replaceChildren();
    const warnings = [
      ...(graph.gaps || []).map(item => `缺口：${item.reason}`),
      ...(graph.conflicts || []).map(item => `冲突：${item.id}`)
    ];
    issues.textContent = warnings.length ? warnings.join("\n") : "未发现逻辑缺口或冲突。";
    updateGraphSelectors();
    $("#logicUndo").disabled = graph.version < 1;
  }

  function updateGraphSelectors() {
    const graph = state.graph;
    for (const selector of ["#logicEdgeFrom", "#logicEdgeTo"]) {
      const select = $(selector);
      const old = select.value;
      select.replaceChildren();
      for (const node of graph?.nodes || []) {
        const option = document.createElement("option");
        option.value = node.id;
        option.textContent = `${kindLabels[node.kind] || node.kind} · ${node.label}`;
        select.append(option);
      }
      if ([...select.options].some(option => option.value === old)) select.value = old;
      select.disabled = (graph?.nodes.length || 0) < 2;
    }
    $("#logicEdgeForm button").disabled = (graph?.nodes.length || 0) < 2;
  }

  async function refreshGraph() {
    if (!projectId()) return;
    try {
      state.graph = await pp.api(`/api/projects/${projectId()}/logic/graph`);
      renderGraph();
    } catch (error) {
      setMessage("#logicGraphStatus", errorText(error), true);
    }
  }

  async function saveGraph(nodes, edges) {
    const current = state.graph;
    const graphNodes = nodes.map(({ legacy_links: _legacyLinks, ...node }) => node);
    try {
      const saved = await pp.api(`/api/projects/${projectId()}/logic/graph`, {
        method: "PUT",
        body: JSON.stringify({
          request_id: uid(), base_version: current.version, nodes: graphNodes, edges
        })
      });
      state.graph = saved;
      renderGraph();
      await pp.reload();
      return true;
    } catch (error) {
      setMessage("#logicGraphStatus", errorText(error), true);
      return false;
    }
  }

  function editNode(node) {
    state.editingNodeId = node.id;
    $("#logicNodeFormTitle").textContent = "编辑节点";
    $("#logicSaveNode").textContent = "保存节点";
    $("#logicCancelEdit").hidden = false;
    $("#logicNodeKind").value = node.kind;
    $("#logicNodeLabel").value = node.label;
    $("#logicNodeDetails").value = node.details || "";
    $("#logicNodeSource").value = node.source_anchor || "";
    $("#logicNodeSourceVersion").value = node.source_version || "";
    $("#logicNodeSourceStatus").value = node.source_status || "current";
    $("#logicNodeLabel").focus();
  }

  function resetNodeForm() {
    state.editingNodeId = null;
    $("#logicNodeForm").reset();
    $("#logicNodeFormTitle").textContent = "添加节点";
    $("#logicSaveNode").textContent = "添加节点";
    $("#logicCancelEdit").hidden = true;
  }

  async function removeNode(node) {
    const incident = state.graph.edges.filter(edge => edge.from_node_id === node.id || edge.to_node_id === node.id);
    const affected = incident.map(edge => {
      const otherId = edge.from_node_id === node.id ? edge.to_node_id : edge.from_node_id;
      const other = state.graph.nodes.find(item => item.id === otherId);
      return `${edgeLabels[edge.edge_type] || edge.edge_type}: ${other?.label || otherId}`;
    });
    const impact = affected.length ? `\n将同时解除：\n${affected.join("\n")}` : "\n该节点没有关联关系。";
    if (!window.confirm(`删除“${node.label}”？${impact}\n可通过撤销恢复上一个图版本。`)) return;
    const nodes = state.graph.nodes.filter(item => item.id !== node.id);
    const edges = state.graph.edges.filter(edge => edge.from_node_id !== node.id && edge.to_node_id !== node.id);
    await saveGraph(nodes, edges);
  }

  function renderSuggestion() {
    const host = $("#logicSuggestionReview");
    host.replaceChildren();
    const suggestion = state.suggestion;
    if (!suggestion) return;
    const title = document.createElement("p");
    title.textContent = `待审建议 · 图版本 v${suggestion.base_version}`;
    host.append(title);
    suggestion.operations.forEach((operation, index) => {
      const label = document.createElement("label");
      label.className = "logic-suggestion-item";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = true;
      checkbox.dataset.suggestionIndex = String(index);
      const text = document.createElement("code");
      text.textContent = JSON.stringify(operation);
      label.append(checkbox, text);
      host.append(label);
    });
    const actions = document.createElement("div");
    actions.className = "logic-form-actions";
    const apply = document.createElement("button");
    apply.type = "button";
    apply.className = "primary";
    apply.textContent = "应用所选";
    apply.addEventListener("click", () => decideSuggestion("selected"));
    const reject = document.createElement("button");
    reject.type = "button";
    reject.textContent = "拒绝建议";
    reject.addEventListener("click", () => decideSuggestion("reject"));
    actions.append(apply, reject);
    host.append(actions);
  }

  async function createSuggestion() {
    setMessage("#logicGraphStatus", "正在本地准备待发预览，尚未调用模型…");
    try {
      const id=projectId(),payload={request_id:uid(),prompt:$('#logicSuggestionPrompt').value.trim()};
      const preview=await pp.api(`/api/projects/${id}/logic/suggestions/preview`,{
        method:'POST',body:JSON.stringify(payload)
      });
      if(id!==projectId())return;
      $('#logicAIPreflight').hidden=false;$('#logicAIPreflight').open=true;$('#logicAIConsent').checked=false;
      $('#logicAIRecipient').textContent=`接收方：${preview.provider.name} · ${preview.provider.base_url} · 模型 ${preview.provider.model} · 配置 v${preview.provider.config_revision}；预计输入 ${preview.estimated_input_tokens}，输出预留 ${preview.max_output_tokens} tokens；费用未知。`;
      $('#logicAIOutbound').textContent=preview.outbound_preview;$('#logicAIConfirm').disabled=!preview.generation_allowed;
      $('#logicAIConfigure').onclick=()=>pp.openProviderSettings?.();
      $('#logicAIConfirm').onclick=async()=>{
        if(id!==projectId()||!$('#logicAIConsent').checked){setMessage('#logicGraphStatus','请核对当前论文与待发范围并勾选本次授权。',true);return;}
        $('#logicAIConfirm').disabled=true;
        try{
      state.suggestion = await pp.api(`/api/projects/${id}/logic/suggestions`, {
        method: "POST",
        body: JSON.stringify({ ...payload,prompt:$('#logicSuggestionPrompt').value.trim(),preview_fingerprint:preview.preview_fingerprint,consent_to_send:true,temporary_key:pp.takeTemporaryCredential?.(preview.provider.profile_id)||'' })
      });
      if(id!==projectId())return;
      renderSuggestion();
      setMessage("#logicGraphStatus", `收到 ${state.suggestion.operations.length} 项建议；图未改变。`);
      $('#logicAIPreflight').hidden=true;
        }catch(error){setMessage('#logicGraphStatus',errorText(error)+' 请先核对模型、授权或可能费用，再创建新预览；不会自动重发。',true);}finally{$('#logicAIConsent').checked=false;}
      };
      setMessage('#logicGraphStatus','预览已就绪；明确授权后才发送，不会直接改变逻辑图。');
    } catch (error) {
      setMessage("#logicGraphStatus", errorText(error), true);
    }
  }

  async function decideSuggestion(mode) {
    const suggestion = state.suggestion;
    if (!suggestion) return;
    const selected = [...document.querySelectorAll("[data-suggestion-index]:checked")]
      .map(input => Number(input.dataset.suggestionIndex));
    if (mode === "selected" && !selected.length) {
      setMessage("#logicGraphStatus", "至少选择一项建议，或拒绝整组建议。", true);
      return;
    }
    const decision = mode === "reject" ? "reject" : selected.length === suggestion.operations.length ? "accept" : "partial_apply";
    try {
      await pp.api(`/api/projects/${projectId()}/logic/suggestions/${suggestion.id}/decision`, {
        method: "POST",
        body: JSON.stringify({
          request_id: uid(), decision,
          selected_indices: decision === "partial_apply" ? selected : null,
          base_version: suggestion.base_version
        })
      });
      state.suggestion = null;
      renderSuggestion();
      await pp.reload();
      await refreshGraph();
      setMessage("#logicGraphStatus", decision === "reject" ? "建议已拒绝；逻辑图保持不变。" : "所选建议已写入新图版本。");
    } catch (error) {
      setMessage("#logicGraphStatus", errorText(error), true);
    }
  }

  async function undoGraph() {
    if (!state.graph || state.graph.version < 1) return;
    try {
      const previous = await pp.api(`/api/projects/${projectId()}/logic/graph?version=${state.graph.version - 1}`);
      await saveGraph(previous.nodes, previous.edges);
    } catch (error) {
      setMessage("#logicGraphStatus", errorText(error), true);
    }
  }

  function referenceLabel(item) {
    return `${item.title || "无题名"}${item.authors ? ` · ${item.authors}` : ""}${item.year ? ` · ${item.year}` : ""}`;
  }

  function fullTextLabel(value) {
    return ({
      metadata_only: "仅元数据 · 原文对照不可用",
      abstract_only: "仅摘要 · 原文对照不可用",
      full_text_available: "已关联全文证据",
      parse_review: "原文覆盖待核对",
      unavailable: "全文不可用 · 原文对照不可用"
    })[value] || value || "全文状态未知";
  }

  function renderPossibleDuplicates(container, created, duplicates) {
    if (!duplicates?.length) return;
    const notice = document.createElement("p");
    notice.textContent = "发现可能重复记录。选择目标后合并，笔记和历史会保留。";
    container.append(notice);
    for (const duplicate of duplicates) {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = `合并到：${referenceLabel(duplicate)}`;
      button.addEventListener("click", async () => {
        try {
          await pp.api(`/api/references/${created.id}/merge`, {
            method: "POST",
            body: JSON.stringify({
              request_id: uid(), target_reference_id: duplicate.id,
              base_version: duplicate.current_version
            })
          });
          setMessage("#referenceLibraryStatus", "已合并记录；目标笔记和历史已保留。");
          await refreshReferences();
        } catch (error) {
          setMessage("#referenceLibraryStatus", errorText(error), true);
        }
      });
      container.append(button);
    }
  }

  async function refreshReferences() {
    const query = $("#referenceLibrarySearch").value.trim();
    const host = $("#referenceLibraryList");
    try {
      const result = await pp.api(`/api/references?query=${encodeURIComponent(query)}`);
      host.replaceChildren();
      for (const item of result.items || []) {
        const card = document.createElement("article");
        card.className = "reference-item";
        const title = document.createElement("h4");
        title.textContent = item.title;
        const meta = document.createElement("p");
        meta.textContent = [item.authors, item.year, item.venue].filter(Boolean).join(" · ") || "作者和出版信息未提供";
        const trace = item.source_trace || {};
        const source = document.createElement("small");
        source.textContent = `v${item.current_version} · ${fullTextLabel(item.full_text_presence)} · 置信度 ${item.confidence || "lowered"} · 来源 ${trace.provider || trace.kind || "手工录入"}`;
        card.append(title, meta, source);
        if (["metadata_only", "abstract_only", "unavailable"].includes(item.full_text_presence)) {
          const guidance = document.createElement("p");
          guidance.textContent = "可关联当前论文中已确认的全文解析证据；低置信度元数据不会作为全文依据。";
          card.append(guidance);
        }
        if (item.doi) {
          const doi = document.createElement("p");
          doi.textContent = `DOI ${item.doi}`;
          card.append(doi);
        }
        if (item.abstract) {
          const abstract = document.createElement("p");
          abstract.textContent = item.abstract.slice(0, 260);
          card.append(abstract);
        }
        if (item.notes) {
          const notes = document.createElement("p");
          notes.textContent = `笔记：${item.notes}`;
          card.append(notes);
        }
        const link = pp.data?.refs?.find(ref => ref.id === item.id);
        const actions = document.createElement("div");
        actions.className = "reference-actions";
        const terms = document.createElement("button");
        terms.type = "button";
        terms.textContent = "术语";
        terms.addEventListener("click", () => pp.openReferenceTerms?.(item.id, link?.reference_version || item.current_version));
        actions.append(terms);
        if (link?.link_status === "update_available") {
          const refresh = document.createElement("button");
          refresh.type = "button";
          refresh.textContent = `刷新论文引用到 v${item.current_version}`;
          refresh.addEventListener("click", () => refreshReferenceLink(item, link));
          actions.append(refresh);
        } else if (!link || ["unlinked", "merged_source"].includes(link.link_status)) {
          const citeKey = document.createElement("input");
          citeKey.value = `ref${item.id.slice(0, 8)}`;
          citeKey.maxLength = 120;
          citeKey.setAttribute("aria-label", `${item.title} 的引用键`);
          const add = document.createElement("button");
          add.type = "button";
          add.textContent = "关联到当前论文";
          add.addEventListener("click", () => linkReference(item, citeKey.value));
          actions.append(citeKey, add);
        } else {
          const status = document.createElement("span");
          status.textContent = `当前论文已关联 · ${link.cite_key} · v${link.reference_version}`;
          actions.append(status);
        }
        const evidenceId = document.createElement("input");
        if (link && !["unlinked", "merged_source"].includes(link.link_status)) {
          const copy = document.createElement("button");
          copy.type = "button";
          copy.textContent = "加入或更新工程 bib";
          copy.addEventListener("click", () => previewBibliography(item, card));
          actions.append(copy);
        }
        evidenceId.placeholder = "已确认全文证据 ID（可选）";
        evidenceId.maxLength = 80;
        evidenceId.setAttribute("aria-label", `${item.title} 的已确认全文证据 ID`);
        const attach = document.createElement("button");
        attach.type = "button";
        attach.textContent = "关联全文证据";
        attach.addEventListener("click", () => attachReferenceEvidence(item, evidenceId.value));
        actions.append(evidenceId, attach);
        card.append(actions);
        host.append(card);
      }
      if (!host.childElementCount) host.textContent = "暂无参考文献";
    } catch (error) {
      host.textContent = errorText(error);
    }
  }

  async function linkReference(item, citeKey) {
    if (!citeKey.trim()) return setMessage("#referenceLibraryStatus", "请填写引用键。", true);
    try {
      await pp.api(`/api/projects/${projectId()}/references/${item.id}`, {
        method: "POST",
        body: JSON.stringify({ request_id: uid(), reference_version: item.current_version, cite_key: citeKey.trim() })
      });
      await pp.reload();
      await refreshReferences();
      setMessage("#referenceLibraryStatus", "参考文献已按固定版本关联到当前论文。更新后可显式刷新。");
    } catch (error) {
      setMessage("#referenceLibraryStatus", errorText(error), true);
    }
  }

  async function previewBibliography(item, card) {
    const pid = projectId();
    card.querySelector(".bib-copy-review")?.remove();
    const panel = document.createElement("section");
    panel.className = "bib-copy-review";
    panel.setAttribute("aria-label", "工程参考文献差异");
    const label = document.createElement("label");
    label.textContent = "工程 bib 目标文件 ";
    const select = document.createElement("select");
    select.setAttribute("aria-label", "工程 bib 目标文件");
    for (const file of pp.data?.files || []) {
      if (!file.path.endsWith(".bib")) continue;
      const option = document.createElement("option"); option.value = file.path; option.textContent = file.path; select.append(option);
    }
    label.append(select);
    const inspect = document.createElement("button"); inspect.type = "button"; inspect.textContent = "预览工程差异";
    const status = document.createElement("p"); status.setAttribute("role", "status");
    const detail = document.createElement("pre"); detail.style.cssText = "white-space:pre-wrap;overflow-wrap:anywhere;max-height:22rem;overflow:auto";
    const commit = document.createElement("button"); commit.type = "button"; commit.textContent = "确认写入工程 bib"; commit.disabled = true;
    const cancel = document.createElement("button"); cancel.type = "button"; cancel.textContent = "取消并保留工程文件";
    cancel.addEventListener("click", () => panel.remove());
    panel.append(label, inspect, status, detail, commit, cancel); card.append(panel);
    let preview = null; let requestId = null;
    const current = () => projectId() === pid && panel.isConnected;
    select.addEventListener("change", () => { preview = null; commit.disabled = true; status.textContent = "目标已变化，请重新预览。"; });
    inspect.addEventListener("click", async () => {
      preview = null; commit.disabled = true; inspect.disabled = true; select.disabled = true;
      try {
        const result = await pp.api(`/api/projects/${pid}/bibliography/preview`, { method: "POST", body: JSON.stringify({reference_id:item.id,target_path:select.value}) });
        if (!current()) return;
        preview = result; requestId = uid(); detail.textContent = result.diff || "条目内容未变化";
        status.textContent = `来源固定为论文关联 v${result.reference_version}；工程 ${result.target_path} v${result.base_version}。${result.conflict ? "同键条目已存在；确认后仅替换该条目，其他手写内容保留。" : "确认后新增条目；取消不会写入。"} 后续文献库更新不会自动同步。`;
        commit.textContent = result.conflict ? "明确替换同键条目" : "确认写入工程 bib"; commit.disabled = false;
      } catch (error) { if (current()) status.textContent = errorText(error); }
      finally { inspect.disabled = false; select.disabled = false; }
    });
    commit.addEventListener("click", async () => {
      if (!preview || !current()) return;
      commit.disabled = true;
      try {
        const result = await pp.api(`/api/projects/${pid}/bibliography/commit`, {method:"POST",body:JSON.stringify({reference_id:item.id,target_path:preview.target_path,
          request_id:requestId,preview_fingerprint:preview.preview_fingerprint,confirm:true,conflict_resolution:preview.conflict?"replace":"none"})});
        if (!current()) return;
        await pp.reload(); await refreshReferences();
        if (projectId()===pid) setMessage("#referenceLibraryStatus",`工程 bib 已保存为 v${result.version}；请显式重新编译检查引用。`);
      } catch (error) { if (current()) { status.textContent = errorText(error); commit.disabled = false; } }
    });
    inspect.focus();
  }

  async function refreshReferenceLink(item, link) {
    try {
      await pp.api(`/api/projects/${projectId()}/references/${item.id}/refresh`, {
        method: "POST",
        body: JSON.stringify({
          request_id: uid(), reference_version: item.current_version,
          base_version: link.reference_version
        })
      });
      await pp.reload();
      await refreshReferences();
      setMessage("#referenceLibraryStatus", "论文引用已显式刷新到最新版本。");
    } catch (error) {
      setMessage("#referenceLibraryStatus", errorText(error), true);
    }
  }

  async function attachReferenceEvidence(item, evidenceId) {
    if (!evidenceId.trim()) return setMessage("#referenceLibraryStatus", "请填写项目中已确认的全文证据 ID。", true);
    try {
      await pp.api(`/api/projects/${projectId()}/references/${item.id}/attachments`, {
        method: "POST",
        body: JSON.stringify({ project_id: projectId(), external_evidence_id: evidenceId.trim(), base_version: item.current_version })
      });
      await pp.reload();
      await refreshReferences();
      setMessage("#referenceLibraryStatus", "全文证据已关联；现有论文引用版本未被改写。更新提示已保留。");
    } catch (error) {
      setMessage("#referenceLibraryStatus", errorText(error), true);
    }
  }

  async function loadDrafts() {
    if (!projectId()) return;
    const host = $("#referenceDrafts");
    try {
      const drafts = await pp.api(`/api/projects/${projectId()}/reference-drafts`);
      host.replaceChildren();
      const pending = drafts.filter(draft => draft.status === "pending_confirmation");
      for (const draft of pending) {
        const details = await pp.api(`/api/references/drafts/${draft.id}`);
        const card = document.createElement("article");
        card.className = "reference-draft";
        const title = document.createElement("strong");
        title.textContent = details.metadata?.title || "待确认参考文献";
        const trace = details.source_trace || {};
        const source = document.createElement("p");
        source.textContent = `来源 ${trace.provider || "搜索交接"} · ${trace.doi || "无 DOI"} · ${details.full_text_presence || "metadata_only"} · 置信度 ${details.confidence || "lowered"}`;
        card.append(title, source);
        const confirm = (decision, mergeIntoId) => async () => {
          try {
            await pp.api(`/api/references/drafts/${draft.id}/confirm`, {
              method: "POST",
              body: JSON.stringify({ request_id: uid(), decision, merge_into_id: mergeIntoId || null })
            });
            setMessage("#referenceLibraryStatus", decision === "merge" ? "已按确认合并参考文献。" : "参考文献已确认入库。既有全文预览仍由导入流程管理。");
            await Promise.all([refreshReferences(), loadDrafts()]);
          } catch (error) {
            setMessage("#referenceLibraryStatus", errorText(error), true);
          }
        };
        if (details.possible_duplicates?.length) {
          for (const duplicate of details.possible_duplicates) {
            const merge = document.createElement("button");
            merge.type = "button";
            merge.textContent = `确认合并到：${referenceLabel(duplicate)}`;
            merge.addEventListener("click", confirm("merge", duplicate.id));
            card.append(merge);
          }
        }
        const create = document.createElement("button");
        create.type = "button";
        create.textContent = details.possible_duplicates?.length ? "仍作为新记录入库" : "确认入库";
        create.addEventListener("click", confirm("create"));
        card.append(create);
        host.append(card);
      }
      if (!host.childElementCount) host.textContent = "没有待确认导入";
    } catch (error) {
      host.textContent = errorText(error);
    }
  }

  function renderCreatedDuplicates(created) {
    const host = $("#referenceLibraryStatus");
    host.replaceChildren();
    const note = document.createElement("p");
    const duplicates = created.possible_duplicates || [];
    note.textContent = duplicates.length
      ? `记录已保存为 v${created.version}；发现 ${duplicates.length} 条 DOI/题名候选，尚未合并。`
      : `记录已保存为 v${created.version}。`;
    host.append(note);
    renderPossibleDuplicates(host, created, duplicates);
  }

  async function createReference(event) {
    event.preventDefault();
    const payload = {
      title: $("#libraryReferenceTitle").value.trim(),
      authors: $("#libraryReferenceAuthors").value.trim(),
      year: $("#libraryReferenceYear").value.trim(),
      venue: $("#libraryReferenceVenue").value.trim(),
      doi: $("#libraryReferenceDoi").value.trim(),
      canonical_url: $("#libraryReferenceUrl").value.trim(),
      abstract: $("#libraryReferenceAbstract").value.trim(),
      notes: $("#libraryReferenceNotes").value.trim(),
      tags: []
    };
    try {
      const created = await pp.api("/api/references", { method: "POST", body: JSON.stringify(payload) });
      renderCreatedDuplicates(created);
      $("#referenceCreateForm").reset();
      await refreshReferences();
    } catch (error) {
      setMessage("#referenceLibraryStatus", errorText(error), true);
    }
  }

  function addHandoffButtons() {
    for (const card of document.querySelectorAll("#academicCandidates .candidate-card")) {
      if (card.querySelector("[data-reference-handoff]")) continue;
      const candidateButton = card.querySelector("[data-candidate]");
      const actions = card.querySelector(".candidate-actions");
      if (!candidateButton || !actions) continue;
      const button = document.createElement("button");
      button.type = "button";
      button.dataset.referenceHandoff = candidateButton.dataset.candidate;
      button.textContent = "转入参考文献库";
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          const result = await pp.academicHandoff(projectId(), button.dataset.referenceHandoff, uid());
          setMessage("#referenceLibraryStatus", result.next_action || "已创建待确认参考文献记录。");
          await loadDrafts();
        } catch (error) {
          setMessage("#referenceLibraryStatus", errorText(error), true);
          button.disabled = false;
        }
      });
      actions.append(button);
    }
  }

  async function syncProject() {
    if (state.syncBusy || !pp.data?.project || state.lastData === pp.data) return;
    state.syncBusy = true;
    const nextProjectId = projectId();
    const projectChanged = state.projectId !== nextProjectId;
    state.lastData = pp.data;
    if (projectChanged) {
      state.projectId = nextProjectId;
      state.graph = null;
      state.suggestion = null;
      resetNodeForm();
      $('#logicAIPreflight').hidden=true;$('#logicAIOutbound').textContent='';$('#logicAIConsent').checked=false;
    }
    try {
      await Promise.all([refreshLifecycle(), refreshGraph(), refreshReferences(), loadDrafts(), renderScopedProjects(), renderCopySources()]);
      renderResourceOrigins();
    } finally {
      state.syncBusy = false;
    }
  }

  $("#projectSearch").addEventListener("input", event => {
    event.stopImmediatePropagation();
    renderScopedProjects();
  }, true);
  $("#projectSearchScope").addEventListener("change", renderScopedProjects);
  $("#runReleaseCheck").addEventListener("click", runReleaseCheck);
  $("#referenceCreateForm").addEventListener("submit", createReference);
  document.addEventListener("click", event => {
    const button = event.target.closest("#copyResource");
    if (!button) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    copyPublishedResource();
  }, true);
  $("#referenceLibrarySearch").addEventListener("input", () => {
    clearTimeout(state.searchTimer);
    state.searchTimer = setTimeout(refreshReferences, 180);
  });

  $("#logicGraphView").addEventListener("click", () => {
    state.graphView = "graph";
    $("#logicGraphView").classList.add("active");
    $("#logicGraphView").setAttribute("aria-pressed", "true");
    $("#logicListView").classList.remove("active");
    $("#logicListView").setAttribute("aria-pressed", "false");
    renderGraph();
  });
  $("#logicListView").addEventListener("click", () => {
    state.graphView = "list";
    $("#logicListView").classList.add("active");
    $("#logicListView").setAttribute("aria-pressed", "true");
    $("#logicGraphView").classList.remove("active");
    $("#logicGraphView").setAttribute("aria-pressed", "false");
    renderGraph();
  });
  $("#logicNodeForm").addEventListener("submit", async event => {
    event.preventDefault();
    if (!state.graph) return;
    const old = state.graph.nodes.find(node => node.id === state.editingNodeId);
    const node = {
      ...(old || {}), id: old?.id || uid(), kind: $("#logicNodeKind").value,
      label: $("#logicNodeLabel").value.trim(), details: $("#logicNodeDetails").value.trim(),
      status: old?.status || "active", source_anchor: $("#logicNodeSource").value.trim(),
      source_version: $("#logicNodeSourceVersion").value.trim(), source_status: $("#logicNodeSourceStatus").value
    };
    const nodes = old
      ? state.graph.nodes.map(item => item.id === old.id ? node : item)
      : [...state.graph.nodes, node];
    if (await saveGraph(nodes, state.graph.edges)) resetNodeForm();
  });
  $("#logicCancelEdit").addEventListener("click", resetNodeForm);
  $("#logicEdgeForm").addEventListener("submit", async event => {
    event.preventDefault();
    if (!state.graph) return;
    const from = $("#logicEdgeFrom").value;
    const to = $("#logicEdgeTo").value;
    if (!from || !to) return setMessage("#logicGraphStatus", "关系需要起点和终点。", true);
    const edge = { id: uid(), from_node_id: from, to_node_id: to, edge_type: $("#logicEdgeType").value };
    await saveGraph(state.graph.nodes, [...state.graph.edges, edge]);
  });
  $("#logicSuggest").addEventListener("click", createSuggestion);
  $("#logicUndo").addEventListener("click", undoGraph);
  window.addEventListener("resize", () => {
    if (state.graph) renderGraph();
  });

  document.addEventListener("paperpilot:resource-updated", event => {
    if(event.detail?.project_id!==projectId())return;
    if(event.detail.kind==='reference'||event.detail.kind==='oa') {
      Promise.all([refreshReferences(),loadDrafts()]).catch(error=>setMessage('#status',errorText(error),true));
    }
    if(event.detail.kind==='logic') {
      // Keep graph CAS and pending form values. The old base must conflict if saved.
      const pending=state.editingNodeId||$('#logicNodeLabel').value.trim()||$('#logicNodeDetails').value.trim();
      if(pending)setMessage('#logicGraphStatus','逻辑图已有外部修改；当前输入已保留，请先复制输入并取消编辑，再刷新图。',true);
      else refreshGraph();
    }
  });
  $('#logicCancelEdit').addEventListener('click',()=>refreshGraph());

  const candidateHost = $("#academicCandidates");
  new MutationObserver(addHandoffButtons).observe(candidateHost, { childList: true, subtree: true });
  addHandoffButtons();
  setInterval(() => syncProject().catch(error => setMessage("#status", errorText(error), true)), 250);
})();
