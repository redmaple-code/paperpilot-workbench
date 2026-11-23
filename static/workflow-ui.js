(() => {
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const pp = window.paperpilot;
  const state = { project: null, workflow: null, drafts: [], jobs: [], events: [], continuations: [], pendingAction: null, pollToken: 0 };
  let watchedProject = null;
  const draftTimers = new Map();
  const draftQueues = new Map();
  const pendingDrafts = new Map();
  const observedDrafts = new Map(), pageCopies = new Map(), reviewedShared = new Map();
  let recoveryCopies = [];
  let copyCursor=null, copyPageRequest=0, copyPreviewRequest=0;
  const revisionKey = (id,kind,path) => JSON.stringify([id,kind,path]);
  function revisionCopyId(key) { if(!pageCopies.has(key))pageCopies.set(key,crypto.randomUUID()); return pageCopies.get(key); }
  let activeEditorPath = "";
  let workspaceLoad=0,workflowRefresh=0;
  let workspaceReady=Promise.resolve();
  const isCurrentPaper=id=>projectId()===id && pp.current?.project?.id===id;

  const labels = {
    created: "已创建",
    human_revision: "人工修订",
    stage_confirmed: "已确认",
    context_ready: "待开始",
    stage_in_progress: "进行中",
    stage_review: "待审阅",
    cancelled: "已取消",
    running: "运行中",
    cancel_requested: "正在取消",
    succeeded: "已完成",
    failed: "失败",
    cancelled_job: "已取消",
    candidate_ready: "待审候选",
    stale: "基线已过期",
    applied: "已应用",
    partially_applied: "部分应用",
    rejected: "已拒绝",
    STAGE_01: "研究问题与创新点",
    STAGE_02: "写作大纲",
    STAGE_03: "文章结构",
    STAGE_04: "术语与证据准备",
    STAGE_05: "工作量与资源计划",
    planning_start: "待填写",
    planning_draft: "草稿",
    planning_review: "待审阅",
    planning_confirmed: "已确认",
    planning_skipped: "已跳过",
    planning_cancelled: "规划已暂停"
  };
  const phaseLabels = {
    planning: "启动规划",
    planning_cancelled: "规划已暂停",
    writing: "论文写作",
    confirming: "阶段确认",
    legacy: "旧论文"
  };
  const actionLabels = {
    write: "自动写作", companion: "自动伴写", follow: "跟写",
    follow_write: "跟写候选", continue: "续写", edit: "修改选中", outline: "生成大纲",
    rewrite: "局部重写", expand_or_condense: "扩写 / 压缩",
    style_or_format: "语气 / 格式调整", term_logic_check: "术语 / 逻辑检查",
    citation_compare: "引用对照建议", explain_or_suggest: "解释 / 修改建议"
  };
  const commandLabels = {
    start: "进入人工修订阶段", review: "提交当前阶段审阅",
    confirm: "确认当前阶段", back: "回退一步", retry: "重新开始阶段",
    cancel: "取消当前阶段"
  };

  function requestId() {
    return globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  async function api(path, options = {}) {
    const response = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(options.headers || {}) },
      ...options
    });
    if (!response.ok) {
      const raw = await response.text();
      let message = raw || response.statusText;
      let detail = null;
      try {
        const body = JSON.parse(raw);
        detail = body.detail || body;
        message = typeof detail === "string" ? detail : detail.message || message;
      } catch {}
      const error = new Error(message);
      error.status = response.status;
      error.detail = detail;
      throw error;
    }
    return response.json();
  }

  function announce(message, error = false) {
    const node = $("#workflowAnnouncement");
    node.textContent = message || "";
    node.dataset.kind = error ? "error" : "info";
    if (message) pp.flash(message);
  }

  function projectId() {
    return state.project?.project?.id || "";
  }

  function activeMode() {
    return state.workflow?.human_revision_mode || null;
  }

  function targetInfo(action) {
    const outline = action === "outline" || $("#outline").classList.contains("active");
    const kind = outline ? "outline" : "file";
    const field = $(outline ? "#outlineText" : "#editor");
    const file = outline ? null : pp.data?.files?.find(item => item.path === $("#fileSelect").value);
    const value = field.value;
    let start = field.selectionStart ?? 0;
    let end = field.selectionEnd ?? 0;
    if (start === end && !["follow", "continue", "companion"].includes(action)) {
      start = 0;
      end = value.length;
    }
    return {
      kind,
      path: file?.path || "",
      field,
      content: value,
      selection: value.slice(start, end),
      start,
      end,
      baseVersion: outline ? pp.data?.outline_version : file?.version
    };
  }

  function prettyTime(value) {
    if (!value) return "";
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
  }

  function stageLabel(workflow) {
    const status = labels[workflow?.stage_status] || workflow?.stage_status || "未知状态";
    const stage = labels[workflow?.current_stage] || workflow?.current_stage || "未知阶段";
    return `${stage} · ${status}`;
  }

  function renderSteps() {
    const current = state.workflow;
    const host = $("#workflowSteps");
    if (!current) {
      host.replaceChildren();
      return;
    }
    // The top bar and the sidebar guide already carry the stage list; repeating
    // it inside the view would be a third copy of the same projection.
    $("#workflowStatus").textContent = phaseLabel(current, current.startup || {});
    $("#workflowVersion").textContent = `工作流 v${current.version}`;
    const globalProgress = $("#wfProgress");
    if (globalProgress && !globalProgress.hidden) {
      host.replaceChildren();
      host.hidden = true;
      return;
    }
    host.hidden = false;
    // Planning stages are the first five steps of one pipeline, not a separate
    // panel: the workflow position and the plan position are the same thing.
    const startup = current.startup || {};
    const steps = mergedStepList(current, startup);
    host.innerHTML = steps.map(step => {
      const classes = ["workflow-step"];
      if (step.current) classes.push("current");
      if (step.done) classes.push("done");
      return `<li class="${classes.join(" ")}" ${step.current ? 'aria-current="step"' : ""} data-phase="${esc(step.phase)}" data-status="${esc(step.status || "")}"><span class="step-mark"></span><span>${esc(step.label)}</span></li>`;
    }).join("");
    $("#workflowStatus").textContent = phaseLabel(current, startup);
    $("#workflowVersion").textContent = `工作流 v${current.version}`;
  }

  function mergedStepList(current, startup) {
    const plan = startup.plan || {};
    const definitions = startup.definitions || [];
    const index = Number(plan.current_index || 0);
    const planning = !!plan.status && plan.status !== "not_started";
    const completed = plan.status === "complete";
    const steps = definitions.map((definition, i) => {
      const status = plan.stages?.[i]?.status || "start";
      return {
        label: definition.title,
        phase: "planning",
        status,
        current: planning && !completed && current.current_stage === definition.id,
        done: completed || status === "confirmed" || status === "skipped" || (planning && i < index)
      };
    });
    steps.push({
      label: "人工修订", phase: "writing", status: current.stage_status,
      current: current.current_stage === "human_revision",
      done: current.current_stage === "stage_confirmed"
    });
    steps.push({
      label: "阶段确认", phase: "confirming", status: current.stage_status,
      current: current.current_stage === "stage_confirmed",
      done: false
    });
    return steps;
  }

  function phaseLabel(current, startup) {
    const plan = startup?.plan || {};
    if (plan.status === "not_started") {
      return `${labels[current.stage_status] || current.stage_status} · 旧论文，未嵌入启动规划`;
    }
    const stage = current.current_stage === "human_revision"
      ? "人工修订"
      : (startup?.definitions || []).find(item => item.id === current.current_stage)?.title || labels[current.current_stage] || current.current_stage;
    const status = labels[current.stage_status] || current.stage_status;
    return `${stage} · ${status} · ${phaseLabels[current.phase] || current.phase || ""}`;
  }

  function renderControls() {
    const current = state.workflow;
    const host = $("#workflowControls");
    host.replaceChildren();
    for (const command of current?.allowed_actions || []) {
      if (command === "redo") continue;
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = commandLabels[command] || command;
      if (command === "start" || command === "confirm") button.classList.add("primary");
      button.addEventListener("click", () => sendWorkflowCommand(command));
      host.append(button);
    }
    if ((current?.history || []).length > 1) {
      const select = document.createElement("select");
      select.setAttribute("aria-label", "选择工作流历史版本");
      select.innerHTML = `<option value="">重做到历史版本…</option>${current.history.map(item => `<option value="${item.version}">v${item.version} · ${labels[item.current_stage] || item.current_stage} / ${labels[item.stage_status] || item.stage_status}</option>`).join("")}`;
      select.addEventListener("change", () => {
        if (select.value) sendWorkflowCommand("redo", Number(select.value));
        select.value = "";
      });
      host.append(select);
    }
  }

  function renderWorkItems() {
    const host = $("#workflowWorkItems");
    host.replaceChildren();
    for (const item of state.workflow?.work_items || []) {
      const label = document.createElement("label");
      label.className = "workflow-work-item";
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = item.status === "done";
      checkbox.dataset.workItem = item.id;
      checkbox.setAttribute("aria-label", item.title);
      const text = document.createElement("span");
      text.textContent = item.title;
      label.append(checkbox, text);
      host.append(label);
    }
    if (!host.childElementCount) host.textContent = "暂无工作项";
  }

  function renderMode() {
    const current = state.workflow;
    const mode = activeMode();
    const stageAvailable = current?.current_stage === "human_revision" && ["stage_in_progress", "stage_review"].includes(current.stage_status);
    $("#workflowModeState").textContent = mode
      ? `已进入 · v${mode.mode_version} · ${prettyTime(mode.created_at)}`
      : stageAvailable ? "当前阶段可进入" : "进入人工修订阶段后可用";
    const host = $("#workflowModeControls");
    host.replaceChildren();
    $("#workflowAssistControls").hidden = !mode;
    if (mode) {
      const button = document.createElement("button");
      button.textContent = "退出人工修订模式";
      button.addEventListener("click", exitRevisionMode);
      host.append(button);
    } else if (stageAvailable) {
      const button = document.createElement("button");
      button.className = "primary";
      button.textContent = "进入人工修订模式";
      button.addEventListener("click", enterRevisionMode);
      host.append(button);
    }
  }

  async function loadWorkspace(project) {
    if (!project) return;
    const token=++workspaceLoad,previousId=projectId(),id=project.project.id;
    state.project = project;
    if(previousId!==id){state.workflow=null;state.pendingAction=null;state.drafts=[];recoveryCopies=[];copyCursor=null;++copyPageRequest;++copyPreviewRequest;reviewedShared.clear();$('#continuationOutlineNode').replaceChildren(new Option('请选择大纲节点',''));$('#continuationOutlineNode').dataset.version='';$('#continuationOutlineStatus').textContent='正在读取当前论文的大纲节点。';$('#revisionCopies').replaceChildren(new Option('请选择恢复副本',''));$('#revisionCopiesMore').hidden=true;$('#revisionCopyPreview').value='';$('#revisionSharedPreview').value='';$('#revisionCopyStatus').textContent='';$('#workflowPreview').textContent='正在加载所选论文的上下文。';$('#workflowSources').replaceChildren();$('#workflowPreflightControls').replaceChildren();}
    activeEditorPath = $("#fileSelect").value || project.files?.[0]?.path || "";
    if (!project.workflow_enabled) {
      $("#workflowStatus").textContent = "该旧论文没有工作流快照，不会自动迁移。";
      $("#workflowControls").replaceChildren();
      $("#workflowModeControls").replaceChildren();
      $("#workflowWorkItems").textContent = "旧论文未进入 REQ-001 工作流。";
      $("#workflowCandidate").replaceChildren();
      $("#workflowJobHistory").replaceChildren();
      return;
    }
    await refreshWorkflow();
    if(token!==workspaceLoad||id!==projectId())return;
    const drafts = await api(`/api/projects/${id}/human-revision/drafts`);
    if(token!==workspaceLoad||id!==projectId())return;
    state.drafts = drafts;
    restoreOpenDraft();
    await Promise.all([refreshJobs(), refreshEvents(), refreshContentHistory(), refreshContinuations(), refreshContinuationOutline()]);
  }

  async function refreshWorkflow() {
    if (!projectId() || !state.project?.workflow_enabled) return;
    const id=projectId(),request=++workflowRefresh;
    const workflow = await api(`/api/projects/${id}/workflow`);
    if(id!==projectId()||request!==workflowRefresh)return;
    state.workflow = workflow;
    // Render the global surfaces first so the in-view step list can detect it is
    // a duplicate and stand down.
    if (pp.renderWorkflowNav) pp.renderWorkflowNav(workflow);
    renderSteps();
    renderControls();
    renderWorkItems();
    renderMode();
    // Single refresh rhythm for the whole pipeline: the startup panel and the
    // global progress/sidebar guide render from this projection.
    if (pp.renderStartup) pp.renderStartup(workflow.startup, workflow);
    document.dispatchEvent(new CustomEvent("paperpilot:workflow-refreshed", {detail:{projectId:projectId(),startup:workflow.startup}}));
  }

  function leavesHumanRevision(command, targetVersion) {
    const current = state.workflow;
    if (!current || current.current_stage !== "human_revision") return false;
    if (command === "confirm" || command === "cancel") return true;
    if (command === "advance" && current.stage_status === "stage_review") return true;
    if (command === "redo") {
      const target = current.history.find(item => item.version === targetVersion);
      return !!target && target.current_stage !== "human_revision";
    }
    return false;
  }

  async function chooseDraftAction(title, description) {
    if (!state.drafts.length) return null;
    const dialog = $("#workflowDecisionDialog");
    $("#workflowDecisionTitle").textContent = title;
    $("#workflowDecisionDescription").textContent = description + "\n\n待处理草稿：\n" + state.drafts.map(d=>`${d.target_path||'大纲'} · ${d.updated_at} · ${d.content.slice(0,100)}`).join("\n");
    const host = $("#workflowDecisionActions");
    host.replaceChildren();
    return new Promise(resolve => {
      let complete = false;
      const finish = value => {
        if (complete) return;
        complete = true;
        dialog.removeEventListener("cancel", onCancel);
        dialog.close();
        resolve(value);
      };
      const onCancel = event => {
        event.preventDefault();
        finish(null);
      };
      dialog.addEventListener("cancel", onCancel);
      const choices = [
        ["commit", "提交检查点"], ["keep", "保留为草稿"], ["discard", "放弃草稿"], [null, "返回"]
      ];
      for (const [value, text] of choices) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = text;
        if (value === "commit") button.className = "primary";
        button.addEventListener("click", () => finish(value));
        host.append(button);
      }
      dialog.showModal();
    });
  }

  async function sendWorkflowCommand(command, targetVersion = null) {
    if (!state.workflow) return;
    const originId=projectId();
    if(!isCurrentPaper(originId))return;
    try {
      let draftAction = null;
      let expectedDrafts = null;
      if (leavesHumanRevision(command, targetVersion)) {
        await flushPendingDrafts();
        if(!isCurrentPaper(originId))return;
        expectedDrafts=state.drafts.map(({target_kind,target_path,updated_at,base_version,mode_id,content})=>({target_kind,target_path,updated_at,base_version,mode_id,content}));
        draftAction = await chooseDraftAction("离开人工修订阶段", "先处理未提交草稿；提交会生成内容版本，保留会让草稿可恢复，放弃会删除未提交内容。");
        if (expectedDrafts.length && !draftAction) return;
        if(!isCurrentPaper(originId))return;
      }
      const next = await api(`/api/projects/${originId}/workflow/commands`, {
        method: "POST",
        body: JSON.stringify({ command, request_id: requestId(), base_version: state.workflow.version, target_version: targetVersion, draft_action: draftAction, expected_drafts:expectedDrafts })
      });
      if(!isCurrentPaper(originId))return;
      state.workflow = next;
      const latestDrafts=await api(`/api/projects/${originId}/human-revision/drafts`);
      if(!isCurrentPaper(originId))return true;
      state.drafts=latestDrafts;
      renderSteps();
      renderControls();
      renderWorkItems();
      renderMode();
      renderDraftStatus();
      await refreshJobs();
      if(isCurrentPaper(originId))announce(`工作流已更新：${stageLabel(next)}`);
    } catch (error) {
      if(!isCurrentPaper(originId))return;
      announce(error.message, true);
      if(error.detail?.code==='draft_set_changed'){const drafts=await api(`/api/projects/${originId}/human-revision/drafts`);if(!isCurrentPaper(originId))return;state.drafts=drafts;}
      await refreshWorkflow();
    }
  }

  async function enterRevisionMode() {
    try {
      const result = await api(`/api/projects/${projectId()}/human-revision`, {
        method: "POST",
        body: JSON.stringify({ workflow_version: state.workflow.version })
      });
      state.workflow = result.workflow;
      state.drafts = await api(`/api/projects/${projectId()}/human-revision/drafts`);
      restoreOpenDraft();
      renderMode();
      announce("已进入人工修订模式");
    } catch (error) {
      announce(error.message, true);
    }
  }

  async function exitRevisionMode() {
    const originId=projectId();
    if(!isCurrentPaper(originId))return;
    try {
      await flushPendingDrafts();
      if(!isCurrentPaper(originId))return;
      const expectedDrafts=state.drafts.map(({target_kind,target_path,updated_at,base_version,mode_id,content})=>({target_kind,target_path,updated_at,base_version,mode_id,content}));
      const draftAction = await chooseDraftAction("退出人工修订模式", "选择如何处理未提交草稿。");
      if (expectedDrafts.length && !draftAction) return;
      if(!isCurrentPaper(originId))return;
      await api(`/api/projects/${originId}/human-revision/exit`, {
        method: "POST", body: JSON.stringify({ draft_action: draftAction,expected_drafts:expectedDrafts })
      });
      if(!isCurrentPaper(originId))return;
      const [workflow,drafts]=await Promise.all([api(`/api/projects/${originId}/workflow`),api(`/api/projects/${originId}/human-revision/drafts`)]);
      if(!isCurrentPaper(originId))return;
      state.workflow=workflow;state.drafts=drafts;
      renderMode();
      renderDraftStatus();
      announce("已退出人工修订模式");
    } catch (error) {
      if(!isCurrentPaper(originId))return;
      announce(error.message, true);
      if(error.detail?.code==='draft_set_changed'){const drafts=await api(`/api/projects/${originId}/human-revision/drafts`);if(isCurrentPaper(originId))state.drafts=drafts;}
    }
  }

  async function refreshJobs() {
    if (!projectId() || !state.project?.workflow_enabled) return;
    const id=projectId(),token=workspaceLoad,jobs=await api(`/api/projects/${id}/ai-jobs?limit=20`);
    if(id!==projectId()||token!==workspaceLoad)return;
    state.jobs=jobs;
    renderJobs();
  }

  async function refreshContinuations() {
    if (!projectId() || !state.project?.workflow_enabled) return;
    const id=projectId(),token=workspaceLoad,items=await api(`/api/projects/${id}/continuations?limit=10`);
    if(id!==projectId()||token!==workspaceLoad)return;
    state.continuations=items;
    renderContinuations();
  }

  function renderContinuations() {
    const host = $("#continuationList");
    if (!host) return;
    host.replaceChildren();
    const status = $("#continuationStatus");
    status.textContent = state.continuations.length ? `已保存 ${state.continuations.length} 个续写批次；候选必须人工审阅后才会进入正文。` : "暂无续写批次";
    for (const batch of state.continuations) {
      const article = document.createElement("article");
      article.className = "continuation-batch";
      const header = document.createElement("header");
      const title = document.createElement("strong");
      title.textContent = `${batch.goal} · ${labels[batch.status] || batch.status}`;
      const progress = document.createElement("span");
      progress.className = "muted";
      progress.textContent = `${batch.progress?.counts?.candidate_ready || 0}/${batch.progress?.total || 0} 个候选待审`;
      header.append(title, progress);
      article.append(header);
      const meta = document.createElement("div");
      meta.className = "muted";
      const pauseMessage = batch.pause_reason === 'segment_failed' ? '有分段失败或结果不明；已保留候选，请核对后恢复未完成项' : batch.pause_reason;
      meta.textContent = `目标：${batch.target_path || "大纲"} · 分段 ${batch.segment_count} · 预算 ${batch.budget?.cost_status === "unknown" ? "费用未知" : `${batch.budget.estimated_cost} ${batch.budget.currency}（来源：${batch.budget.pricing_source}，用户提供未核验）`} · 整批token上限 ${batch.budget?.max_job_tokens}，已用/预留 ${batch.budget?.usage?.tokens || 0}${pauseMessage ? ` · ${pauseMessage}` : ""}${batch.outline_stale_reasons?.length ? ` · ${batch.outline_stale_reasons.join('；')}` : ''}`;
      article.append(meta);
      const actions = document.createElement("div");
      actions.className = "continuation-actions";
      const unknown=(batch.segments||[]).some(s=>s.job?.error_code==='provider_result_unknown'||(s.job?.error_code==='interrupted'&&s.job?.egress_state==='unknown'));
      const duplicate=document.createElement('input');duplicate.type='checkbox';
      if(unknown){const label=document.createElement('label');label.append(duplicate,document.createTextNode('已核对远端记录，确认恢复可能重复执行/收费'));actions.append(label);}
      const budget=document.createElement('input');budget.type='checkbox';
      if(batch.pause_reason==='budget_exceeded'){const label=document.createElement('label');label.append(budget,document.createTextNode(`确认将整批token上限提高到 ${Math.max(batch.budget.max_job_tokens,16000*batch.segment_count)}；未知费用预留仍累计，单次调用硬上限仍生效`));actions.append(label);}
      for (const [command, text] of [["pause", "暂停"], ["resume", "恢复"], ["cancel", "取消"]]) {
        if ((command === "pause" && !["running", "review"].includes(batch.status)) || (command === "resume" && batch.status !== "paused") || (command === "cancel" && ["completed", "cancelled"].includes(batch.status))) continue;
        const button = document.createElement("button");
        button.textContent = text;
        button.addEventListener("click", () => {if(command==='resume'&&unknown&&!duplicate.checked)return announce('请核对远端记录并明确确认可能重复费用。',true);commandContinuation(batch, command,duplicate.checked,budget.checked);});
        actions.append(button);
      }
      article.append(actions);
      for (const segment of batch.segments || []) {
        const item = document.createElement("div");
        item.className = "continuation-segment";
        const summary = document.createElement("div");
        summary.textContent = `分段 ${segment.sequence + 1} · ${segment.goal || batch.goal} · 章节 ${segment.outline_node_id || batch.outline_node_id} · ${labels[segment.status] || segment.status}`;
        item.append(summary);
        if (segment.candidate) {
          const patch = segment.candidate.patches?.[0];
          const preview = document.createElement("pre");
          preview.textContent = patch?.current_content === patch?.content ? "候选与当前正文相同" : (patch?.content || "");
          item.append(preview);
          const segmentActions = document.createElement("div");
          segmentActions.className = "continuation-segment-actions";
          if (["candidate_ready"].includes(segment.status) && !segment.candidate.stale) {
            for (const [decision, text] of [["accept", "应用候选"], ["reject", "拒绝候选"]]) {
              const button = document.createElement("button");
              button.textContent = text;
              button.addEventListener("click", () => decideContinuation(batch, segment, decision));
              segmentActions.append(button);
            }
          }
          item.append(segmentActions);
        }
        article.append(item);
      }
      host.append(article);
    }
  }

  let continuationOutlineLoad = 0;
  async function refreshContinuationOutline() {
    const id=projectId(), token=++continuationOutlineLoad, workspace=workspaceLoad;
    if(!id)return;
    const select=$('#continuationOutlineNode'), old=select.value;
    try {
      const graph=await api(`/api/projects/${id}/outline/structured`);
      if(id!==projectId()||token!==continuationOutlineLoad||workspace!==workspaceLoad)return;
      select.replaceChildren(new Option('请选择大纲节点',''));
      for(const node of graph.nodes)select.add(new Option(node.title,node.id));
      if(graph.nodes.some(node=>node.id===old))select.value=old;
      select.dataset.version=String(graph.version);
      $('#continuationOutlineStatus').textContent=graph.nodes.length ? `结构化大纲 v${graph.version}；请选择节点。` : '暂无结构化大纲节点，请到大纲页创建节点或迁移现有大纲，然后刷新。';
    } catch(error) {
      if(id===projectId()&&token===continuationOutlineLoad){select.replaceChildren(new Option('请选择大纲节点',''));select.dataset.version='';$('#continuationOutlineStatus').textContent=`读取大纲失败，请刷新：${error.message}`;}
    }
  }

  async function startContinuation() {
    // Revision mode is no longer required; the view is the context.
    const target = targetInfo("continue");
    const goal = $("#continuationGoal").value.trim();
    if (!goal) return announce("请先填写续写目标", true);
    const node=$('#continuationOutlineNode').value, outlineVersion=Number($('#continuationOutlineNode').dataset.version||0);
    if(!node||!outlineVersion)return announce('请选择大纲节点；如无节点，请到大纲页创建或迁移后刷新。',true);
    try {
      const id=projectId(),count=Number($('#continuationSegments').value||1),prompt=$('#workflowAssistPrompt').value.trim();
      const plan=pp.continuationPlanSnapshot?.()||{subgoals:[]};
      const effectivePrompt=pp.continuationRequestPrompt?.(prompt||goal,plan.subgoals)||prompt||goal;
      await previewContext('continue',effectivePrompt,{...target,batchPlan:{...plan,count}},async(preview,request)=>{
      if(id!==projectId()||$('#continuationGoal').value.trim()!==goal||Number($('#continuationSegments').value||1)!==count||$('#continuationOutlineNode').value!==node||Number($('#continuationOutlineNode').dataset.version||0)!==outlineVersion)throw new Error('续写目标、次数或大纲节点已变化，请重新预览。');
      if(JSON.stringify(plan)!==JSON.stringify(pp.continuationPlanSnapshot?.()||{subgoals:[]}))throw new Error('逐段目标或整批预算已变化，请重新预览。');
      await api(`/api/projects/${id}/continuations`, {
        method: "POST",
        body: JSON.stringify({
          request_id: request, target_kind: target.kind, target_path: target.path,
          base_version: target.baseVersion, anchor_start: target.start, anchor_end: target.end,
          goal, segment_count:count,prompt,context_version:preview.version,outline_node_id:node,outline_version:outlineVersion,
          subgoals:plan.subgoals,hard_budget_tokens:plan.hard_budget_tokens,hard_budget_amount:plan.hard_budget_amount,
          temporary_key:pp.takeTemporaryCredential?.(preview.provider?.profile_id)||'',
        })
      });
      await refreshContinuations();
      announce("续写批次已创建；原稿保持不变，候选生成后逐段审阅。 ");
      },`按此范围生成 ${count} 个续写候选（最多 ${count} 次模型调用，费用未知）`);
    } catch (error) { announce(error.message, true); }
  }

  async function commandContinuation(batch, command,duplicateConfirmed=false,budgetOverride=false) {
    try {
      await api(`/api/projects/${projectId()}/continuations/${batch.id}/${command}`, { method: "POST", body: JSON.stringify({ request_id: requestId(),duplicate_cost_confirmed:duplicateConfirmed,budget_override:budgetOverride,hard_budget_tokens:budgetOverride?Math.max(batch.budget?.max_job_tokens||0,16000*batch.segment_count):null,temporary_key:command==='resume'?(pp.takeTemporaryCredential?.((batch.segments||[]).find(s=>s.job?.provider_snapshot)?.job.provider_snapshot.profile_id)||''):'' }) });
      await refreshContinuations();
      announce(`续写批次已${command === "pause" ? "暂停" : command === "resume" ? "恢复" : "取消"}`);
    } catch (error) { announce(error.message, true); }
  }

  async function decideContinuation(batch, segment, decision) {
    try {
      await api(`/api/projects/${projectId()}/continuations/${batch.id}/segments/${segment.id}/decision`, {
        method: "POST",
        body: JSON.stringify({ decision, request_id: requestId(), base_file_versions: segment.candidate.base_file_versions, allow_warnings: false })
      });
      await pp.reload();
      await Promise.all([refreshContinuations(), refreshContentHistory(), refreshJobs()]);
      announce(decision === "accept" ? "续写候选已应用" : "续写候选已拒绝");
    } catch (error) { announce(error.message, true); }
  }

  function renderJobs() {
    const latest = state.jobs[0];
    const status = $("#workflowJobStatus");
    status.textContent = latest
      ? `最近任务：${actionLabels[latest.job.action] || latest.job.action} · ${labels[latest.job.status] || latest.job.status} · ${latest.job.model || "模型未确定"} · 预计输入 ${latest.job.scope.estimated_input_tokens ?? "未知"} tokens · 输出上限 ${latest.job.scope.max_output_tokens ?? "未知"} · 费用未知${latest.job.usage?.prompt_tokens != null ? ` · 实际输入 ${latest.job.usage.prompt_tokens} tokens` : ""}${latest.job.error_code ? ` · ${latest.job.error_code}` : ""}`
      : "暂无 AI 任务";
    const host = $("#workflowJobHistory");
    host.replaceChildren();
    for (const item of state.jobs) {
      const row = document.createElement("div");
      row.className = "workflow-job-row";
      const summary = document.createElement("span");
      const targetNames = (item.job.scope.targets || []).map(target => target.path || "大纲").join(", ");
      summary.textContent = `${actionLabels[item.job.action] || item.job.action} · ${labels[item.job.status] || item.job.status} · ${item.job.model || "模型未知"} · 输入估算 ${item.job.scope.estimated_input_tokens ?? "未知"} tokens · 费用未知${item.job.usage?.completion_tokens != null ? ` · 输出 ${item.job.usage.completion_tokens} tokens` : ""} · ${targetNames} · ${prettyTime(item.job.created_at)}`;
      row.append(summary);
      if (["running", "cancel_requested"].includes(item.job.status)) {
        const cancel = document.createElement("button");
        cancel.textContent = "取消";
        cancel.addEventListener("click", () => cancelJob(item.job.id));
        row.append(cancel);
        if (['unknown','dead'].includes(item.job.recovery_state)) {
          const note = document.createElement('p');
          note.textContent = '无法确认原执行进程仍在工作。请先检查其他应用窗口和提供方记录；若确认原进程已停止，可标记中断。之后重试可能重复执行或收费。';
          const recover = document.createElement('button'); recover.type='button'; recover.textContent='核对后标记中断';
          recover.addEventListener('click', async () => {
            if (!window.confirm('请确认原执行进程已经停止，并已核对提供方记录。标记中断不会自动重发；之后重试可能重复收费。')) return;
            const id=projectId(); recover.disabled=true;
            try { await api(`/api/projects/${id}/ai-jobs/${item.job.id}/recover`, {method:'POST',body:JSON.stringify({confirmed_stopped:true})}); if(id===projectId()){await refreshJobs();announce('任务已标记中断；请核对远端结果后决定是否重试。');} }
            catch(error){if(id===projectId()){recover.disabled=false;announce(error.message,true);}}
          });
          row.append(note,recover);
        }
      }
      if (item.job.retryable) {
        const unknown=item.job.error_code==='provider_result_unknown'||(item.job.error_code==='interrupted'&&item.job.scope?.egress_state==='unknown');
        const consent=document.createElement('input');consent.type='checkbox';
        if(unknown){const label=document.createElement('label');label.append(consent,document.createTextNode('已核对远端记录，确认再次发送可能重复执行或收费'));row.append(label);}
        const retry = document.createElement("button");
        retry.textContent = "重试";
        retry.disabled=unknown;consent.onchange=()=>{retry.disabled=unknown&&!consent.checked;};
        retry.addEventListener("click", () => retryJob(item.job.id,consent.checked));
        row.append(retry);
      }
      host.append(row);
    }
    const candidate = state.jobs.find(item => item.candidate)?.candidate;
    renderCandidate(candidate || null);
  }

  async function cancelJob(jobId) {
    try {
      await api(`/api/projects/${projectId()}/ai-jobs/${jobId}/cancel`, { method: "POST", body: "{}" });
      await refreshJobs();
      announce("已请求取消 AI 任务");
    } catch (error) {
      announce(error.message, true);
    }
  }

  async function retryJob(jobId,duplicateConfirmed=false) {
    try {
      const result = await api(`/api/projects/${projectId()}/ai-jobs/${jobId}/retry`, {
        method: "POST", body: JSON.stringify({ request_id: requestId(),duplicate_cost_confirmed:duplicateConfirmed,temporary_key:pp.takeTemporaryCredential?.(state.jobs.find(j=>j.job.id===jobId)?.job.scope?.provider_snapshot?.profile_id)||'' })
      });
      await refreshJobs();
      pollJob(result.job.id);
      announce("已提交重试任务");
    } catch (error) {
      announce(error.message, true);
    }
  }

  function renderCandidate(candidate) {
    const host = $("#workflowCandidate");
    host.replaceChildren();
    if (!candidate) return;
    const stale = candidate.stale || candidate.status === "stale";
    const article = document.createElement("article");
    article.className = "workflow-candidate";
    const heading = document.createElement("div");
    heading.className = "workflow-subhead";
    const title = document.createElement("h4");
    title.textContent = `候选 ${candidate.id.slice(0, 8)} · ${labels[candidate.status] || candidate.status}${stale ? " · 基线已过期" : ""}`;
    const version = document.createElement("span");
    version.className = "muted";
    version.textContent = `上下文 ${String(candidate.context_version).slice(0, 12)}`;
    heading.append(title, version);
    article.append(heading);
    const sourceList = document.createElement("p");
    sourceList.className = "muted";
    sourceList.textContent = `上下文来源：${(candidate.context_sources || []).filter(source => source.kind !== "omission").map(source => source.label).filter(Boolean).join("、") || "无"}`;
    article.append(sourceList);
    const costLine = document.createElement("p");
    costLine.className = "candidate-cost";
    const usage = candidate.usage || {};
    costLine.textContent = `模型：${candidate.model || "未知"} · 费用：未知 · 输入 token 估算 ${candidate.cost?.estimated_input_tokens ?? usage.estimated_input_tokens ?? "未知"} · 输出上限 ${candidate.cost?.max_output_tokens ?? "未知"}${usage.prompt_tokens != null ? ` · 实际输入 ${usage.prompt_tokens}` : ""}${usage.completion_tokens != null ? ` · 实际输出 ${usage.completion_tokens}` : ""}`;
    article.append(costLine);
    const validation = candidate.validation || { hard_errors: [], warnings: [] };
    const validationBox = document.createElement("section");
    validationBox.className = "candidate-validation";
    const validationHeading = document.createElement("h5");
    validationHeading.textContent = "应用前结构与引用检查";
    validationBox.append(validationHeading);
    for (const issue of validation.hard_errors || []) {
      const line = document.createElement("p");
      line.className = "validation-error";
      line.textContent = `阻止应用 · ${issue.message}`;
      validationBox.append(line);
    }
    for (const warning of validation.warnings || []) {
      const line = document.createElement("p");
      line.className = "validation-warning";
      line.textContent = `需要明确覆盖 · ${warning.message}`;
      validationBox.append(line);
    }
    if (!(validation.hard_errors || []).length && !(validation.warnings || []).length) {
      const line = document.createElement("p");
      line.className = "validation-pass";
      line.textContent = "LaTeX 结构检查通过；没有发现缺失引用。";
      validationBox.append(line);
    }
    article.append(validationBox);
    if(candidate.status==='candidate_ready'&&!stale&&(candidate.patches||[]).some(p=>p.kind==='file')){
      const preflight=document.createElement('button');preflight.type='button';preflight.textContent='编译预检（不采纳候选）';preflight.dataset.candidatePreflight=candidate.id;
      preflight.onclick=()=>pp.preflightCandidate?.(candidate.id,candidate.project_id);article.append(preflight);
    }
    const selected = new Set();
    for (const patch of candidate.patches || []) {
      const key = patch.kind === "outline" ? "__outline__" : patch.path;
      const details = document.createElement("details");
      details.className = "candidate-target";
      details.open = true;
      const summary = document.createElement("summary");
      summary.textContent = `${patch.path || "大纲"} · 基线 v${patch.base_version} → 当前 v${patch.current_version}`;
      details.append(summary);
      const comparison = document.createElement("div");
      comparison.className = "candidate-three-way";
      for (const [label, value] of [
        ["基线", patch.base_content],
        ["当前正文", patch.current_content === null ? "文件已删除" : patch.current_content],
        ["候选", patch.content]
      ]) {
        const pane = document.createElement("section");
        const paneTitle = document.createElement("h5");
        paneTitle.textContent = label;
        const pre = document.createElement("pre");
        pre.textContent = value || "";
        pane.append(paneTitle, pre);
        comparison.append(pane);
      }
      details.append(comparison);
      article.append(details);
      if (candidate.status === "candidate_ready" && !stale) {
        const label = document.createElement("label");
        label.className = "candidate-select";
        const checkbox = document.createElement("input");
        checkbox.type = "checkbox";
        checkbox.checked = true;
        checkbox.value = key;
        checkbox.addEventListener("change", () => checkbox.checked ? selected.add(key) : selected.delete(key));
        selected.add(key);
        label.append(checkbox, document.createTextNode(`选择 ${patch.path || "大纲"}`));
        article.append(label);
      }
    }
    if (candidate.status === "candidate_ready" && !stale) {
      const hasHardErrors = (validation.hard_errors || []).length > 0;
      const hasWarnings = (validation.warnings || []).length > 0;
      const override = document.createElement("input");
      override.type = "checkbox";
      override.disabled = !hasWarnings || hasHardErrors;
      override.setAttribute("aria-label", "我已查看引用警告并明确覆盖");
      if (hasWarnings && !hasHardErrors) {
        const label = document.createElement("label");
        label.className = "validation-override";
        label.append(override, document.createTextNode("我已查看这些引用警告，仍要继续应用"));
        article.append(label);
      }
      const controls = document.createElement("div");
      controls.className = "workflow-controls";
      const accept = document.createElement("button");
      accept.className = "primary";
      accept.textContent = "审阅并应用全部";
      accept.disabled = hasHardErrors || hasWarnings;
      override.addEventListener("change", () => {
        accept.disabled = hasHardErrors || hasWarnings && !override.checked;
        partial.disabled = hasHardErrors || hasWarnings && !override.checked;
      });
      accept.addEventListener("click", () => decideCandidate(candidate, "accept", null, override.checked));
      const partial = document.createElement("button");
      partial.textContent = "应用所选目标";
      partial.disabled = hasHardErrors || hasWarnings;
      partial.addEventListener("click", () => decideCandidate(candidate, "partial_apply", [...selected], override.checked));
      const reject = document.createElement("button");
      reject.textContent = "拒绝候选";
      reject.addEventListener("click", () => decideCandidate(candidate, "reject"));
      controls.append(accept, partial, reject);
      article.append(controls);
    }
    host.append(article);
  }

  async function decideCandidate(candidate, decision, selectedPaths = null, allowWarnings = false) {
    try {
      const response = await api(`/api/projects/${projectId()}/candidates/${candidate.id}/decision`, {
        method: "POST",
        body: JSON.stringify({
          decision,
          request_id: requestId(),
          base_file_versions: candidate.base_file_versions,
          selected_paths: selectedPaths,
          allow_warnings: allowWarnings
        })
      });
      if (response.content_versions) await pp.reload();
      await Promise.all([refreshWorkflow(), refreshJobs(), refreshEvents(), refreshContentHistory()]);
      announce(decision === "reject" ? "已拒绝候选" : "候选已应用并建立新版本");
    } catch (error) {
      announce(error.message, true);
      if (error.detail?.validation) {
        candidate.validation = error.detail.validation;
        renderCandidate(candidate);
      }
      await refreshJobs();
    }
  }

  async function pollJob(jobId) {
    const token = ++state.pollToken;
    for (let attempt = 0; attempt < 150 && token === state.pollToken; attempt += 1) {
      try {
        const result = await api(`/api/projects/${projectId()}/ai-jobs/${jobId}`);
        if (!["running", "cancel_requested"].includes(result.job.status)) {
          await Promise.all([refreshJobs(), refreshEvents()]);
          if (result.job.status === "succeeded") announce("AI 候选已就绪，可比较后决定是否应用");
          else announce(`AI 任务${labels[result.job.status] || result.job.status}${result.job.error_code ? `：${result.job.error_code}` : ""}`, result.job.status === "failed");
          return;
        }
        $("#workflowJobStatus").textContent = `AI 任务${labels[result.job.status] || result.job.status} · 第 ${result.job.attempt} 次尝试`;
        await new Promise(resolve => setTimeout(resolve, 800));
      } catch (error) {
        announce(error.message, true);
        return;
      }
    }
    if (token === state.pollToken) announce("任务仍在运行；可从任务列表查看或取消。");
  }

  async function refreshEvents() {
    if (!projectId() || !state.project?.workflow_enabled) return;
    state.events = await api(`/api/projects/${projectId()}/human-revision/events`);
    const host = $("#workflowEventHistory");
    host.replaceChildren();
    for (const item of state.events.slice(0, 12)) {
      const row = document.createElement("div");
      row.className = "workflow-event-row";
      row.textContent = `${item.action_type} · ${labels[item.status] || item.status} · ${prettyTime(item.created_at)}`;
      host.append(row);
    }
    if (!host.childElementCount) host.textContent = "暂无修订记录";
  }

  function historyTarget() {
    const outline = $("#outline").classList.contains("active");
    return outline
      ? { kind: "outline", path: "", version: pp.data?.outline_version || 0 }
      : { kind: "file", path: $("#fileSelect").value, version: pp.data?.files?.find(item => item.path === $("#fileSelect").value)?.version || 0 };
  }

  async function refreshContentHistory() {
    if (!projectId() || !state.project?.workflow_enabled) return;
    const target = historyTarget();
    if (!target.version) return;
    const versions = await api(`/api/projects/${projectId()}/versions?target_kind=${target.kind}&target_path=${encodeURIComponent(target.path)}`);
    const host = $("#workflowContentHistory");
    host.replaceChildren();
    const current = versions[0];
    const controls = document.createElement("div");
    controls.className = "workflow-controls";
    const undoTarget = current?.source === "redo"
      ? versions.find(item => item.version < current.version && item.source === "undo")?.restored_from_version
      : current?.parent_version;
    const undo = document.createElement("button");
    undo.textContent = "撤销上一版本";
    undo.disabled = !undoTarget || !activeMode();
    undo.addEventListener("click", () => restoreVersion(target, undoTarget, "undo"));
    const redoTarget = current?.source === "undo" ? current.parent_version : null;
    const redo = document.createElement("button");
    redo.textContent = "重做";
    redo.disabled = !redoTarget || !activeMode();
    redo.addEventListener("click", () => restoreVersion(target, redoTarget, "redo"));
    controls.append(undo, redo);
    host.append(controls);
    for (const item of versions.slice(0, 8)) {
      const row = document.createElement("div");
      row.className = "workflow-version-row";
      const text = document.createElement("span");
      text.textContent = `v${item.version} · ${item.source} · ${prettyTime(item.created_at)}${item.version === target.version ? " · 当前" : ""}`;
      row.append(text);
      if (item.version !== target.version) {
        const restore = document.createElement("button");
        restore.textContent = "恢复";
        restore.disabled = !activeMode();
        restore.addEventListener("click", () => restoreVersion(target, item.version, "undo"));
        row.append(restore);
      }
      host.append(row);
    }
  }

  async function restoreVersion(target, version, action) {
    try {
      await api(`/api/projects/${projectId()}/versions/restore`, {
        method: "POST",
        body: JSON.stringify({
          target_kind: target.kind, target_path: target.path,
          target_version: version, base_version: target.version, action
        })
      });
      await pp.reload();
      state.drafts = await api(`/api/projects/${projectId()}/human-revision/drafts`);
      await Promise.all([refreshWorkflow(), refreshJobs(), refreshEvents(), refreshContentHistory()]);
      announce(`${action === "undo" ? "已恢复版本" : "已重做到版本"} v${version}`);
    } catch (error) {
      announce(error.message, true);
    }
  }

  async function previewContext(action, prompt = "",targetOverride=null,customSubmit=null,caption='使用此上下文生成候选') {
    if (!activeMode()) {
      // Revision mode is no longer required.
      return;
    }
    const target = targetOverride||targetInfo(action),id=projectId();
    if (!target.path && target.kind === "file") {
      announce("请先选择目标文件", true);
      return;
    }
    try {
      const preview = await api(`/api/projects/${projectId()}/ai-context`, {
        method: "POST",
        body: JSON.stringify({
          action, target_kind: target.kind, target_path: target.path,
          prompt, content: target.content, selection: target.selection, context_source_ids: []
        })
      });
      if(id!==projectId())return;
      state.pendingAction = { ...target, action, prompt, context: preview,customSubmit,requestId:requestId(),submitting:false };
      renderContext(preview);
      $("#workflowContext").open = true;
      $('#workflowContext').scrollIntoView({block:'center'});
      const controls = $("#workflowPreflightControls");
      controls.replaceChildren();
      const generate = document.createElement("button");
      generate.className = "primary";
      generate.textContent = caption;
      generate.disabled=preview.generation_allowed===false;
      generate.addEventListener("click", createJobFromPreflight);
      const cancel = document.createElement("button");
      cancel.textContent = "取消";
      cancel.addEventListener("click", () => {
        state.pendingAction = null;
        controls.replaceChildren();
      });
      controls.append(generate, cancel);
      if(target.batchPlan){const plan=document.createElement('pre');const pricing=preview.provider?.model_limits?.[preview.model]?.pricing;plan.textContent=`整批token硬上限：${target.batchPlan.hard_budget_tokens||preview.max_job_tokens}；金额硬上限：${target.batchPlan.hard_budget_amount??'未设置'}；${pricing?`费率来源：${pricing.source}（${pricing.currency}，用户提供未核验）`:'费率未知，设置金额硬上限会暂停'}\n`+(target.batchPlan.subgoals||[]).map((g,i)=>`${i+1}. ${g.goal}；章节${g.outline_node_id}；锚点${g.anchor_start??target.start}～${g.anchor_end??target.end}`).join('\n');controls.append(plan);}
      if(preview.generation_allowed===false){const fix=document.createElement('button');fix.textContent='打开模型配置并修复';fix.onclick=()=>pp.openProviderSettings?.();controls.append(fix);announce(preview.provider_error,true);return;}
      announce(`上下文预览已就绪 · ${String(preview.version).slice(0, 12)}`);
    } catch (error) {
      announce(error.message, true);
    }
  }

  function renderContext(preview) {
    const action = state.pendingAction ? actionLabels[state.pendingAction.action] || state.pendingAction.action : "AI 操作";
    $("#workflowContextStatus").textContent = `${action} · 模型 ${preview.model || "未设置"} · 上下文版本 ${preview.version} · ${preview.sources.length} 个来源 · 预计输入 ${preview.estimated_input_tokens || "未知"} tokens · 输出上限 ${preview.max_output_tokens || "未知"} · 硬上限 ${preview.max_job_tokens || "未知"} · 费用${preview.cost_status === "unknown" ? "未知" : "可估算"} · ${preview.redactions} 项敏感值将被脱敏`;
    if(preview.provider)$('#workflowContextStatus').textContent+=` · 接收方 ${preview.provider.name}（${preview.provider.base_url}）· 配置 v${preview.provider.config_revision} · 容量来源 ${preview.capacity?.capacity_source||'未提供'}`;
    const sources = $("#workflowSources");
    sources.replaceChildren();
    for (const source of preview.sources) {
      const item = document.createElement("li");
      const kind={outline:'大纲',term:'术语',corpus:'语料',reference:'文献',logic:'逻辑',provider:'模型提供方',materials_fingerprint:'资料与授权校验',style_profile:'已批准文风经验'}[source.kind]||source.label||'来源';
      item.textContent = `${kind} · ${source.label || kind} · 来源版本 v${source.version ?? "?"}`;
      sources.append(item);
    }
    const omissions = $("#workflowOmissions");
    omissions.replaceChildren();
    for (const item of preview.omissions || []) {
      const row = document.createElement("li");
      row.textContent = `省略 ${item.kind} ${item.omitted} 项 · ${item.reason}`;
      omissions.append(row);
    }
    $("#workflowPreview").textContent = preview.preview;
  }

  async function createJobFromPreflight() {
    const pending = state.pendingAction;
    if (!pending||pending.submitting) return;
    const saved = pending.kind === "outline"
      ? pp.data?.outline || ""
      : pp.data?.files?.find(item => item.path === pending.path)?.content || "";
    if (saved !== pending.content) {
      announce("正文版本已更新，请重新预览本轮上下文。", true);
      return;
    }
    const key = pending.kind === "outline" ? "__outline__" : pending.path;
    if (!pending.baseVersion) {
      announce("目标内容没有可用版本，请先保存。", true);
      return;
    }
    const estimatedTokens = pending.context.estimated_input_tokens || 0;
    const outputLimit = pending.context.max_output_tokens || 0;
    const hardLimit = pending.context.max_job_tokens || 0;
    if (estimatedTokens + outputLimit > hardLimit) {
      announce(`未发送：预计输入 ${estimatedTokens} 加输出上限 ${outputLimit} 超出任务硬上限 ${hardLimit} tokens。请缩小选择范围或精简上下文。`, true);
      return;
    }
    try {
      pending.submitting=true;const button=$('#workflowPreflightControls .primary');if(button)button.disabled=true;
      if(pending.customSubmit){await pending.customSubmit(pending.context,pending.requestId);state.pendingAction=null;$('#workflowPreflightControls').replaceChildren();return;}
      const result = await api(`/api/projects/${projectId()}/ai-jobs`, {
        method: "POST",
        body: JSON.stringify({
          action: pending.action,
          request_id: pending.requestId,
          prompt: pending.prompt,
          content: pending.content,
          selection: pending.selection,
          scope: {
            targets: [{ kind: pending.kind, path: pending.path, start: pending.start, end: pending.end }],
            context_source_ids: []
          },
          base_file_versions: { [key]: pending.baseVersion },
          context_version: pending.context.version
          ,temporary_key:pp.takeTemporaryCredential?.(pending.context.provider?.profile_id)||''
        })
      });
      state.pendingAction = null;
      $("#workflowPreflightControls").replaceChildren();
      await refreshJobs();
      pollJob(result.job.id);
      announce("AI 任务已加入队列；原稿保持不变");
    } catch (error) {
      announce(error.message, true);
    } finally {
      pending.submitting=false;const button=$('#workflowPreflightControls .primary');if(button)button.disabled=false;
    }
  }

  async function flushDraft(target, content = target.field.value) {
    if (!target.projectId && !activeMode()) return;
    const originId=target.projectId||projectId();
    const savedVersion = target.baseVersion ?? (target.kind === "outline"
      ? pp.data?.outline_version || 0
      : pp.data?.files?.find(item => item.path === target.path)?.version || 0);
    const savedContent=target.savedContent ?? (target.kind==='outline'?pp.data?.outline:pp.data?.files?.find(item=>item.path===target.path)?.content);
    target={...target,projectId:originId,baseVersion:savedVersion,savedContent};
    const existing=state.drafts.find(item=>item.target_kind===target.kind&&item.target_path===target.path);
    const draftBase=originId===projectId()?(existing?.base_version??savedVersion):savedVersion;
    const key = revisionKey(originId,target.kind,target.path);
    if (savedVersion < 1) return;
    pendingDrafts.set(key, { target, content, baseVersion: draftBase });
    const previous = draftQueues.get(key) || Promise.resolve();
    const queued = previous.catch(() => {}).then(async () => {
      const latest = pendingDrafts.get(key);
      if (!latest || latest.target.projectId!==originId) return;
      if (latest.content === latest.target.savedContent) {
        if (latest) pendingDrafts.delete(key);
        return;
      }
      const baseVersion = latest.baseVersion;
      const saved = await api(`/api/projects/${originId}/human-revision/drafts`, {
        method: "POST",
        body: JSON.stringify({
          target_kind: latest.target.kind, target_path: latest.target.path,
          content: latest.content, base_version: baseVersion,
          expected_draft_updated_at:observedDrafts.get(key) ?? null, copy_id:revisionCopyId(key)
        })
      });
      observedDrafts.set(key,saved.saved_at);
      const restoredDrafts = await api(`/api/projects/${originId}/human-revision/drafts`);
      if(isCurrentPaper(originId))state.drafts=restoredDrafts;
      if (pendingDrafts.get(key) === latest) pendingDrafts.delete(key);
      if(isCurrentPaper(originId))renderDraftStatus();
      return saved;
    });
    draftQueues.set(key, queued);
    try { return await queued; }
    finally { if (draftQueues.get(key) === queued) draftQueues.delete(key); }
  }

  async function flushPendingDrafts() {
    const originId=projectId();
    for (const [key, timer] of draftTimers) clearTimeout(timer);
    draftTimers.clear();
    const pending = [...pendingDrafts.values()];
    for (const item of pending) await flushDraft(item.target, item.content);
    await Promise.all([...draftQueues.values()]);
    const stored=await api(`/api/projects/${originId}/human-revision/drafts`);
    if(isCurrentPaper(originId)){state.drafts=stored;renderDraftStatus();}
  }

  function scheduleDraft(target) {
    if (!activeMode()) return;
    target={...target,projectId:projectId(),baseVersion:target.kind==='outline'?pp.data?.outline_version||0:pp.data?.files?.find(item=>item.path===target.path)?.version||0,
      savedContent:target.kind==='outline'?pp.data?.outline:pp.data?.files?.find(item=>item.path===target.path)?.content};
    const key = revisionKey(target.projectId,target.kind,target.path);
    pendingDrafts.set(key, {
      target,
      content: target.field.value,
      baseVersion: target.kind === "outline"
        ? pp.data?.outline_version || 0
        : pp.data?.files?.find(item => item.path === target.path)?.version || 0,
    });
    clearTimeout(draftTimers.get(key));
    draftTimers.set(key, setTimeout(() => {
      draftTimers.delete(key);
      const latest = pendingDrafts.get(key);
      if (latest) flushDraft(target, latest.content).catch(error => {if(isCurrentPaper(target.projectId))announce(`草稿保存失败，输入仍保留：${error.message}`, true);});
    }, 700));
    $("#workflowModeState").textContent = "正在保存可恢复草稿…";
  }

  function renderDraftStatus() {
    if (!activeMode()) return;
    const count = state.drafts.length;
    $("#workflowModeState").textContent = count ? `已进入 · ${count} 项可恢复草稿` : "已进入 · 无未提交草稿";
  }

  async function refreshRevisionCopies(reset=true) {
    const id=projectId();if(!id||!isCurrentPaper(id))return;
    const request=++copyPageRequest,select=$('#revisionCopies'),more=$('#revisionCopiesMore');
    if(reset){copyCursor=null;recoveryCopies=[];++copyPreviewRequest;select.replaceChildren(new Option('请选择恢复副本',''));$('#revisionCopyPreview').value='';more.hidden=true;}
    else if(!copyCursor)return;
    const params=new URLSearchParams({limit:'30'});if(copyCursor){params.set('before_updated_at',copyCursor.updated_at);params.set('before_copy_id',copyCursor.copy_id);}
    more.disabled=true;
    try {
      const page=await api(`/api/projects/${id}/human-revision/draft-copies-page?${params}`);
      if(!isCurrentPaper(id)||request!==copyPageRequest)return;
      const known=new Set(recoveryCopies.map(x=>x.copy_id));
      for(const item of page.copies){if(known.has(item.copy_id))continue;known.add(item.copy_id);recoveryCopies.push(item);select.add(new Option(`${item.target_path||'大纲'} · ${item.updated_at} · ${item.preview.slice(0,50)}`,item.copy_id));}
      copyCursor=page.next_cursor;more.hidden=!copyCursor;$('#revisionCopyStatus').textContent=`已加载 ${recoveryCopies.length} 份副本${copyCursor?'，可继续加载':''}；其他页面更新后可刷新。`;
    } finally {if(request===copyPageRequest)more.disabled=false;}
  }
  async function readRevisionCopy(id,item) {
    const copy=await api(`/api/projects/${id}/human-revision/draft-copies/${encodeURIComponent(item.copy_id)}`);
    if(!isCurrentPaper(id)||$('#revisionCopies').value!==item.copy_id)throw new Error('论文或副本选择已变化，请重新选择。');
    if(copy.updated_at!==item.updated_at)throw new Error('副本已变化，请刷新并重新选择。');
    return copy;
  }
  async function restoreRevisionCopy() {
    const id=projectId(), item=recoveryCopies.find(x=>x.copy_id===$('#revisionCopies').value);
    if(!item||!activeMode())throw new Error('请先进入人工修订模式并选择恢复副本。');
    const copy=await readRevisionCopy(id,item),kind=item.target_kind, path=item.target_path;
    if((kind==='file'&&(!$('#write').classList.contains('active')||$('#fileSelect').value!==path))||(kind==='outline'&&!$('#outline').classList.contains('active')))throw new Error('请先打开副本对应的文件或大纲。');
    const field=$(kind==='file'?'#editor':'#outlineText'), current=kind==='file'?pp.data?.files?.find(x=>x.path===path):null;
    const version=kind==='file'?current?.version:pp.data?.outline_version;
    if(version!==item.base_version)throw new Error('该副本基线已过期；可从下方复制内容，与当前正式内容比较后手动合并。');
    if(!confirm('将所选副本载入编辑器？保存时将尝试更新当前共享草稿；如有新变化会再次阻止覆盖。'))return;
    const disabled=field.disabled;field.disabled=true;
    try {
      const key=revisionKey(id,kind,path), pending=pendingDrafts.get(key);
      if(pending){clearTimeout(draftTimers.get(key));if(pending.content!==copy.content){if(pageCopies.get(key)===item.copy_id)pageCopies.set(key,crypto.randomUUID());try{await flushDraft(pending.target,pending.content);}catch(e){if(!e.detail?.copy_id)throw e;}}}
      const [fresh,shared]=await Promise.all([readRevisionCopy(id,item),api(`/api/projects/${id}/human-revision/drafts`)]);
      if(id!==projectId()||(kind==='file'&&$('#fileSelect').value!==path))throw new Error('目标已切换，请重新选择。');
      const currentShared=shared.find(x=>x.target_kind===kind&&x.target_path===path), sharedToken=currentShared?.updated_at ?? null;
      $('#revisionSharedPreview').value=currentShared?.content ?? '';
      if(sharedToken!==(observedDrafts.get(key) ?? null)&&reviewedShared.get(key)!==sharedToken){reviewedShared.set(key,sharedToken);$('#revisionCopyStatus').textContent='共享草稿已变化；请比较下方内容，再次选择恢复。';return;}
      observedDrafts.set(key,sharedToken);reviewedShared.delete(key);
      pageCopies.set(key,crypto.randomUUID());pendingDrafts.delete(key);field.value=fresh.content;$('#revisionCopyStatus').textContent='副本已载入，尚未提交；确认内容后点击保存。';
    } finally {field.disabled=disabled;}
  }

  function restoreOpenDraft() {
    if (!activeMode()) return;
    const outline = $("#outline").classList.contains("active");
    if (!outline) return;
    const key=revisionKey(projectId(),outline?'outline':'file',outline?'':$('#fileSelect').value);
    const pending=pendingDrafts.get(key);
    if(pending){$(outline ? "#outlineText" : "#editor").value=pending.content;announce('本页有未保存的输入，请检查后重试保存。',true);return;}
    if(draftQueues.has(key))return;
    const target = outline
      ? state.drafts.find(item => item.target_kind === "outline")
      : state.drafts.find(item => item.target_kind === "file" && item.target_path === $("#fileSelect").value);
    if (target) {
      observedDrafts.set(key,target.updated_at);
      $(outline ? "#outlineText" : "#editor").value = target.content;
      announce(`已恢复 ${target.target_path || "大纲"} 的未提交草稿`);
    } else observedDrafts.set(key,null);
    renderDraftStatus();
  }

  async function saveTarget(kind) {
    if (kind === 'file' && pp.flushEditorChanges) return pp.flushEditorChanges();
    if (!projectId()) return false;
    if(!isCurrentPaper(projectId())||!state.workflow){announce('论文正在切换或加载，请等待完成后再保存。',true);return false;}
    if (!state.project.workflow_enabled) {
      announce("该旧论文没有工作流版本管理。", true);
      return false;
    }
    const target = kind === "outline"
      ? { kind: "outline", path: "", field: $("#outlineText") }
      : { kind: "file", path: $("#fileSelect").value, field: $("#editor") };
    const key = target.kind === "outline" ? "__outline__" : target.path;
    const version = target.kind === "outline"
      ? pp.data?.outline_version || 0
      : pp.data?.files?.find(item => item.path === target.path)?.version || 0;
    const originId=projectId(), wasDisabled=target.field.disabled;
    try {
      if (activeMode()) {
        target.field.disabled=true;
        const timerKey = revisionKey(originId,target.kind,target.path);
        clearTimeout(draftTimers.get(timerKey));
        const saved=await flushDraft(target, target.field.value);
        if(!isCurrentPaper(originId))throw new Error("论文已切换，请在原论文重新检查草稿后提交。");
        if(!saved?.saved_at)throw new Error("没有新保存的草稿；请先修改内容或恢复草稿。");
        const result = await api(`/api/projects/${originId}/human-revision/checkpoint`, {
          method: "POST",
          body: JSON.stringify({ target_kind: target.kind, target_path: target.path, base_version: version, expected_draft_updated_at:saved.saved_at })
        });
        observedDrafts.set(revisionKey(originId,target.kind,target.path),null);
        if(!isCurrentPaper(originId))return true;
        announce(`大纲已保存 v${result.version}`);
      } else {
        const result = await api(`/api/projects/${originId}/outline`, {
          method: 'PUT', body: JSON.stringify({content: target.field.value, base_version: version})
        });
        if (isCurrentPaper(originId)) { pp.data.outline = target.field.value; pp.data.outline_version = result.version; }
      }
      await pp.reload();
      if(!isCurrentPaper(originId))return true;
      const latestDrafts=await api(`/api/projects/${originId}/human-revision/drafts`);
      if(!isCurrentPaper(originId))return true;
      state.drafts=latestDrafts;
      await Promise.all([refreshWorkflow(), refreshJobs(), refreshContentHistory(), refreshEvents()]);
      return true;
    } catch (error) {
      if(isCurrentPaper(originId))announce(error.message, true);
      return false;
    } finally {
      target.field.disabled=wasDisabled;
    }
  }

  async function addFile() {
    if (!projectId()) return;
    const path = $("#newFilePath").value.trim();
    if (!path) return;
    try {
      await api(`/api/projects/${projectId()}/files`, {
        method: "PUT", body: JSON.stringify({ path, content: "", base_version: 0 })
      });
      await pp.reload();
      $("#fileSelect").value = path;
      $("#editor").value = "";
      $("#newFilePath").value = "";
      await refreshContentHistory();
      announce("已创建文件 v1");
    } catch (error) {
      announce(error.message, true);
    }
  }

  async function deleteFile() {
    if (!projectId()) return;
    const file = pp.data?.files?.find(item => item.path === $("#fileSelect").value);
    if (!file || file.path === "main.tex") return announce("main.tex 不能删除", true);
    try {
      await api(`/api/projects/${projectId()}/files/${encodeURIComponent(file.path)}?base_version=${file.version}`, { method: "DELETE" });
      await pp.reload();
      await refreshContentHistory();
      announce("文件已删除；历史版本仍可恢复");
    } catch (error) {
      announce(error.message, true);
    }
  }

  async function createProject(event) {
    event.preventDefault();
    if (!$("#newName").reportValidity()) return;
    const button = $("#createProject"), status = $("#newProjectStatus");
    if (button.disabled) return;
    const input = {name: $("#newName").value, domain: $("#newDomain").value,
      domain_id: $("#newDomainSelect").value || "", style: $("#newStyle").value};
    const signature = JSON.stringify(input);
    if (window.pendingProjectSignature !== signature) {
      window.pendingProjectRequestId = null;
      window.pendingProjectSignature = signature;
    }
    if (!window.pendingProjectRequestId) window.pendingProjectRequestId = requestId();
    button.disabled = true;
    try {
      const result = await api("/api/projects", {
        method: "POST",
        body: JSON.stringify({
          ...input,
          request_id: window.pendingProjectRequestId
        })
      });
      window.pendingProjectRequestId = null;
      window.pendingProjectSignature = null;
      $("#newDialog").close();
      await pp.loadProject(result.project.id);
      await watchProject();
      if (pp.current?.project.id === result.project.id) pp.openFirstPlanningStage?.();
      announce("论文工作区、工作流和资源待办已创建");
    } catch (error) {
      if (status) status.textContent = error.message;
      announce(error.message, true);
    } finally {
      button.disabled = false;
    }
  }

  function bindIntercept(selector, handler, force = false) {
    document.addEventListener("click", event => {
      const button = event.target.closest(selector);
      if (!button) return;
      if (!force && !state.project?.workflow_enabled) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      handler(event, button);
    }, true);
  }

  function bindUI() {
    $('#revisionCopiesRefresh').addEventListener('click',()=>{const id=projectId();refreshRevisionCopies().catch(e=>{if(isCurrentPaper(id))$('#revisionCopyStatus').textContent=e.message;});});
    $('#revisionCopiesMore').addEventListener('click',()=>{const id=projectId();refreshRevisionCopies(false).catch(e=>{if(isCurrentPaper(id))$('#revisionCopyStatus').textContent=e.message;});});
    $('#revisionCopies').addEventListener('change',async()=>{
      const id=projectId(),item=recoveryCopies.find(x=>x.copy_id===$('#revisionCopies').value),request=++copyPreviewRequest;
      $('#revisionCopyPreview').value='';if(!item)return;
      try {const copy=await readRevisionCopy(id,item);if(request===copyPreviewRequest)$('#revisionCopyPreview').value=copy.content;}
      catch(e){if(request===copyPreviewRequest && isCurrentPaper(id))$('#revisionCopyStatus').textContent=e.message;}
    });
    $('#revisionCopyRestore').addEventListener('click',()=>{const id=projectId();restoreRevisionCopy().catch(e=>{if(isCurrentPaper(id))$('#revisionCopyStatus').textContent=e.message;});});
    $('#revisionCopyDelete').addEventListener('click',async()=>{
      const id=projectId(),item=recoveryCopies.find(x=>x.copy_id===$('#revisionCopies').value);
      if(!item){$('#revisionCopyStatus').textContent='请先选择恢复副本。';return;}
      if(!confirm('删除所选恢复副本？共享草稿和正式内容不会删除。'))return;
      try { await api(`/api/projects/${id}/human-revision/draft-copies/${encodeURIComponent(item.copy_id)}?updated_at=${encodeURIComponent(item.updated_at)}`,{method:'DELETE'});if(projectId()===id)await refreshRevisionCopies(); }
      catch(e){if(projectId()===id)$('#revisionCopyStatus').textContent=e.message;}
    });
    bindIntercept("[data-action]", (_event, button) => previewContext(button.dataset.action));
    // Author file saves belong to the live editor controller.
    bindIntercept("#saveOutline", () => saveTarget("outline"));
    bindIntercept("#addFile", addFile);
    bindIntercept("#deleteFile", deleteFile);
    bindIntercept("#createProject", createProject, true);
    $("#refreshWorkflow").addEventListener("click", () => loadWorkspace(pp.current));
    $("#refreshContinuations")?.addEventListener("click", () => refreshContinuations());
    $("#continuationOutlineRefresh")?.addEventListener("click", () => refreshContinuationOutline());
    $("#continuationStart")?.addEventListener("click", startContinuation);
    $("#workflowAssistGenerate").addEventListener("click", () => {
      previewContext($("#workflowAssistAction").value, $("#workflowAssistPrompt").value.trim());
    });
    const scheduleInput = action => {
      const id=pp.current?.project?.id,field=$(action==='outline'?'#outlineText':'#editor'),value=field.value,path=action==='outline'?'':$('#fileSelect').value;
      if(isCurrentPaper(id)&&state.workflow){scheduleDraft(targetInfo(action));return;}
      if(!id)return;
      const kind=action==='outline'?'outline':'file',file=kind==='file'?pp.data?.files?.find(x=>x.path===path):null;
      const version=kind==='outline'?pp.data?.outline_version||0:file?.version||0, savedContent=kind==='outline'?pp.data?.outline:file?.content;
      const key=revisionKey(id,kind,path), provisional={target:{kind,path,field,projectId:id,baseVersion:version,savedContent},content:value,baseVersion:version};
      pendingDrafts.set(key,provisional);
      watchProject().then(()=>{if(!isCurrentPaper(id))return;if(!activeMode()&&pendingDrafts.get(key)===provisional){pendingDrafts.delete(key);return;}if(field.value===value&&(action==='outline'||$('#fileSelect').value===path))scheduleDraft(targetInfo(action));}).catch(error=>{if(isCurrentPaper(id))announce(`草稿暂未保存：${error.message}`,true);});
    };
    // The author editor persists independently of internal AI revision drafts.
    $("#outlineText").addEventListener("input", () => scheduleInput("outline"));
    document.addEventListener("change", event => {
      if (event.target?.id !== "fileSelect" || !activeMode()) return;
      const oldPath = activeEditorPath;
      const oldKey = revisionKey(projectId(),'file',oldPath);
      clearTimeout(draftTimers.get(oldKey));
      draftTimers.delete(oldKey);
      const oldTarget = { kind: "file", path: oldPath, field: $("#editor") };
      const originId=projectId();
      if (oldPath) flushDraft(oldTarget, oldTarget.field.value).catch(error => {if(isCurrentPaper(originId))announce(`草稿保存失败，请返回检查：${error.message}`, true);});
      activeEditorPath = event.target.value;
    }, true);
    $("#fileSelect").addEventListener("change", () => setTimeout(() => {
      restoreOpenDraft();
      refreshContentHistory();
    }, 0));
    $$(".tabs button").forEach(button => button.addEventListener("click", () => setTimeout(() => {
      restoreOpenDraft();
      refreshContentHistory();
    }, 0)));
    document.addEventListener("change", async event => {
      const checkbox = event.target.closest("[data-work-item]");
      if (!checkbox || !state.workflow) return;
      try {
        await api(`/api/projects/${projectId()}/workflow/items/${checkbox.dataset.workItem}`, {
          method: "PUT", body: JSON.stringify({ status: checkbox.checked ? "done" : "pending" })
        });
        await refreshWorkflow();
      } catch (error) {
        announce(error.message, true);
        checkbox.checked = !checkbox.checked;
      }
    });
  }

  async function watchProject() {
    const project = pp.current;
    if (project && project !== watchedProject) {
      watchedProject = project;
      workspaceReady=loadWorkspace(project);
    }
    await workspaceReady;
  }

  pp.saveCurrentEdit = saveTarget;
  pp.reviewBeforeSend=previewContext;
  pp.isRevisionActive = () => Boolean(activeMode());
  pp.refreshWorkflow = refreshWorkflow;
  pp.flushRevisionDraftsForBuild=async expectedProjectId=>{
    if(!isCurrentPaper(expectedProjectId))throw new Error('论文已切换，请重新预览当前论文。');
    await flushPendingDrafts();
    await Promise.all([...draftQueues.values()]);
    if(!isCurrentPaper(expectedProjectId))throw new Error('论文已切换，原论文草稿已保留；请重新预览当前论文。');
  };
  pp.keepCurrentDraft = async (kind, expectedProjectId=projectId()) => {
    if(!isCurrentPaper(expectedProjectId))throw new Error('论文正在切换，请等待加载完成后重试。');
    if (!activeMode() || !projectId()) return false;
    const target = kind === "outline"
      ? { kind: "outline", path: "", field: $("#outlineText") }
      : { kind: "file", path: $("#fileSelect").value, field: $("#editor") };
    clearTimeout(draftTimers.get(revisionKey(projectId(),target.kind,target.path)));
    await flushDraft(target, target.field.value);
    if(!isCurrentPaper(expectedProjectId))throw new Error('论文已切换，原论文草稿已保存；请重新预览当前论文。');
    return true;
  };
  bindUI();
  document.addEventListener('paperpilot:style-learning-changed', event => {
    if(event.detail.projectId!==projectId())return;
    state.pendingAction=null;
    $('#workflowPreview').textContent='学习档案或授权已变化，请重新预览当前上下文。';
    $('#workflowSources').replaceChildren();
    $('#workflowPreflightControls').replaceChildren();
    refreshWorkflow().catch(error=>announce(error.message,true));
  });
  setInterval(() => watchProject().catch(error => announce(error.message, true)), 250);
})();
