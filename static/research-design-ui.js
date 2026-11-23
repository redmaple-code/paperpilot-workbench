/* Research-design guidance for planning stages (scholar-apprentice E stage).
   Candidate-only: nothing here writes a field until the author picks one card. */
(() => {
  const pp = window.paperpilot;
  const $ = (selector, root = document) => root.querySelector(selector);
  if (!pp || !$("#researchDesign")) return;

  const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const base = pid => `/api/projects/${pid}/workflow/research-design`;
  const agentBase = pid => `/api/projects/${pid}/agent`;

  // Only stages with fields this module may fill.
  const STAGE_FIELDS = {};

  let view = null;          // startup projection
  let meta = null;          // {operators, red_team}
  let preview = null;
  let candidates = [];
  let busy = false;
  let projectId = "";

  const message = e => { try { return JSON.parse(e.message).detail?.message || e.message; } catch { return e.message; } };
  const status = (text, error = false) => {
    const node = $("#researchDesignStatus");
    node.textContent = text || "";
    node.dataset.error = error ? "true" : "false";
  };

  function currentStageId() {
    const index = Number(view?.plan?.current_index || 0);
    return pp.selectedStartupStage || view?.definitions?.[index]?.id || "";
  }

  function selectedOperators() {
    return [...document.querySelectorAll("#researchDesignOps input:checked")].map(el => el.value);
  }

  function selectedResources(){return [...document.querySelectorAll("#rdResourceInputs input:checked")].map(input=>({kind:"corpus",id:input.value,version:Number(input.dataset.version)}));}

  function renderOperators() {
    $("#researchDesignOps").hidden = currentStageId() !== "STAGE-01";
    const host = $("#researchDesignOps");
    if (!host || !meta) return;
    if (host.childElementCount) return;   // build once per page load
    host.innerHTML = meta.operators.map(op => `
      <label class="rd-op">
        <input type="checkbox" value="${esc(op.id)}" checked>
        <span>${esc(op.label)}<small>${esc(op.prompt)}</small></span>
      </label>`).join("");
    host.addEventListener("change", () => { $("#rdGenerate").disabled = true; });
  }

  function renderCards() {
    const host = $("#researchDesignCards");
    if (!host || !pp.agentResearch) return;
    host.replaceChildren();
    if (!candidates.length) { host.textContent = "当前阶段尚无候选。先预览本次材料，再授权生成。"; return; }
    const latest = candidates[0];
    latest.project_id = latest.project_id || projectId;
    for (const card of latest.cards) host.append(pp.agentResearch.render(latest, card, view.definitions));
    document.dispatchEvent(new CustomEvent("paperpilot:research-candidates", {detail:{candidate:latest, definitions:view.definitions}}));
  }

  async function loadMeta() {
    if (meta) return meta;
    const pid = pp.current?.project?.id;
    if (!pid) return null;
    const result = await pp.api(base(pid));
    const resources = await pp.api(base(pid)+"/resources");
    if(pp.current?.project?.id!==pid)return null;
    let area=$("#rdResourceInputs");
    if(!area){area=document.createElement("fieldset");area.id="rdResourceInputs";$("#researchDesignOps").after(area);}
    area.innerHTML='<legend>选择本次发送的已授权语料（可不选，每项最多4000字）</legend>'+resources.resources.map(r=>`<label><input type="checkbox" value="${esc(r.id)}" data-version="${r.version}">${esc(r.title)} · v${r.version} · ${r.characters}字</label>`).join("");
    meta = { operators: result.operators || [], red_team: result.red_team || [] };
    return meta;
  }

  async function loadCandidates() {
    const pid = pp.current?.project?.id;
    if (!pid) return;
    const result = await pp.api(base(pid) + `?stage_id=${encodeURIComponent(currentStageId())}`);
    if (pp.current?.project?.id !== pid) return;
    candidates = result.candidates || [];
  }

  async function doPreview() {
    const pid = pp.current?.project?.id;
    if (!pid) return;
    await loadMeta();
    const operators = selectedOperators();
    if (currentStageId() === "STAGE-01" && !operators.length) throw new Error("请至少选择一个创新算子。");
    preview = await pp.api(base(pid) + "/preview", {
      method: "POST",
      body: JSON.stringify({ stage_id: currentStageId(), operators, instruction: $("#researchDesignInstruction").value, resource_refs:selectedResources() })
    });
    $("#researchDesignOutbound").textContent = JSON.stringify(preview.outbound_preview, null, 1);
    $("#researchDesignPreview").hidden = false;
    $("#rdConsent").checked = false;
    $("#rdGenerate").disabled = true;
    const admission = $("#researchDesignAdmission");
    admission.dataset.ok = preview.generation_allowed ? "true" : "false";
    admission.textContent = preview.generation_allowed
      ? `接收方：${preview.provider?.name || "已登记模型"} · ${preview.provider?.model || ""}。生成会产生费用，仅本次有效。`
      : `当前无法生成：${preview.provider_error || "模型未就绪"}。请先在「论文设置 → AI 模型」完成登记与测试。`;
    status("已生成预览，请核对上方内容后勾选授权。");
  }

  async function doGenerate() {
    const pid = pp.current?.project?.id;
    if (!pid || !preview) throw new Error("请先生成预览。");
    if (!$("#rdConsent").checked) throw new Error("请先勾选授权发送。");
    const created = await pp.api(base(pid), {
      method: "POST",
      body: JSON.stringify({
        request_id: (globalThis.crypto?.randomUUID?.() || String(Date.now())),
        stage_id: currentStageId(),
        operators: selectedOperators(),
        instruction: $("#researchDesignInstruction").value,
        resource_refs:selectedResources(),
        preview_fingerprint: preview.preview_fingerprint,
        consent_to_send: true,
        possible_cost_confirmed: true
      })
    });
    if (pp.current?.project?.id !== pid || currentStageId() !== created.stage_id) return;
    candidates = [created, ...candidates];
    renderCards();
    status(`已生成 ${created.cards.length} 条候选。它们没有写入任何字段，请逐条审阅。`);
  }

  async function applyCard(candidate, cardId, field) {
    const pid = pp.current?.project?.id;
    if (!pid) return;
    const ok = window.confirm("把这条候选写入当前阶段的草稿字段？\n\n它仍然是未确认、未查重的草稿，你可以继续修改。");
    if (!ok) return;
    const result = await pp.api(base(pid) + "/apply", {
      method: "POST",
      body: JSON.stringify({
        request_id: (globalThis.crypto?.randomUUID?.() || String(Date.now())),
        candidate_id: candidate.id,
        card_id: cardId,
        expected_workflow_version: view.workflow_version,
        field
      })
    });
    status(result.message || "已写入草稿。");
    if (pp.refreshWorkflow) await pp.refreshWorkflow();
  }

  function perform(action) {
    if (busy) return;
    busy = true;
    $("#rdPreview").disabled = true;
    $("#rdGenerate").disabled = true;
    action().catch(error => status(message(error), true)).finally(() => {
      busy = false;
      $("#rdPreview").disabled = false;
      refreshGate();
    });
  }

  function refreshGate() {
    if (mode() === "agent") {
      const ready = Boolean(agentPreview) && $("#rdConsent").checked && agentPreview.generation_allowed && !busy;
      $("#rdAgentStart").disabled = !ready;
      $("#rdGenerate").disabled = true;
      return;
    }
    const canGenerate = Boolean(preview) && $("#rdConsent").checked && preview.generation_allowed && !busy;
    $("#rdGenerate").disabled = !canGenerate;
    $("#rdAgentStart").disabled = true;
  }


  // ---------------- Agent mode ----------------
  let agentRun = null, agentTimer = null, agentPreview = null;

  function mode() {
    return currentStageId() !== "STAGE-01" ? "quick" : document.querySelector('input[name="rdMode"]:checked')?.value || "quick";
  }

  function syncMode() {
    const isAgent = mode() === "agent";
    $("#rdAgentStart").hidden = !isAgent;
    $("#rdGenerate").hidden = isAgent;
    $("#researchDesignPreview").hidden = true;
    preview = null; agentPreview = null;
    refreshGate();
    status(isAgent
      ? "Agent 模式：会多轮检索与查重，产出候选；不会写入任何字段。"
      : "快速生成：一次成文，产出候选。");
  }

  function renderAgentRun(run) {
    const host = $("#rdAgentRun");
    if (!host) return;
    if (!run) { host.innerHTML = ""; return; }
    const state = run.status === "running" || run.status === "created" ? "running" : run.status;
    const label = { running: "运行中", succeeded: "已完成", blocked: "已停止（预算/上下文）", failed: "失败", cancelled: "已取消" }[state] || state;
    const steps = (run.steps || []).map(step => `
      <li data-ok="${step.ok ? "true" : "false"}">
        <b>${esc(step.tool || "think")}</b>
        <span>${esc((step.thought || step.error_code || "").slice(0, 120))}</span>
      </li>`).join("");
    host.innerHTML = `
      <div class="rd-run">
        <div class="rd-run-head">
          <span class="rd-run-status" data-state="${esc(state)}">${esc(label)}</span>
          <span class="rd-progress">步 ${run.supersteps_used}/${run.max_supersteps} · token ${run.tokens_used}</span>
          ${state === "running" ? '<button type="button" id="rdAgentCancel">取消</button>' : ""}
        </div>
        ${run.error_code ? `<p class="rd-warn">停止原因：${esc(run.error_code)}${run.blocked_reason ? " · " + esc(run.blocked_reason) : ""}</p>` : ""}
        <ul class="rd-trace">${steps}</ul>
      </div>`;
    const cancel = $("#rdAgentCancel");
    if (cancel) cancel.onclick = () => cancelAgent();
  }

  async function previewAgent() {
    const pid = pp.current?.project?.id;
    if (!pid) return;
    agentPreview = await pp.api(agentBase(pid) + "/preview", {
      method: "POST",
      body: JSON.stringify({ stage_id: currentStageId(), operators: selectedOperators(),
                             instruction: $("#researchDesignInstruction").value })
    });
    const admission = $("#researchDesignAdmission");
    $("#researchDesignOutbound").textContent = JSON.stringify({
      tools: agentPreview.tools.map(t => t.name),
      memory: agentPreview.memory_preview,
      max_supersteps: agentPreview.max_supersteps,
      max_tokens: agentPreview.max_tokens,
      approval_mode: agentPreview.approval_mode
    }, null, 1);
    $("#researchDesignPreview").hidden = false;
    $("#rdConsent").checked = false;
    admission.dataset.ok = agentPreview.generation_allowed ? "true" : "false";
    admission.textContent = agentPreview.generation_allowed
      ? `接收方：${agentPreview.provider?.name || "已登记模型"} · ${agentPreview.provider?.model || ""}。多轮调用会产生费用。`
      : `当前无法运行：${agentPreview.provider_error || "模型未就绪"}。`;
    status("已生成 Agent 预览，请核对工具与材料后勾选授权。");
  }

  async function startAgent() {
    const pid = pp.current?.project?.id;
    if (!pid || !agentPreview) throw new Error("请先生成预览。");
    if (!$("#rdConsent").checked) throw new Error("请先勾选授权发送。");
    const run = await pp.api(agentBase(pid) + "/runs", {
      method: "POST",
      body: JSON.stringify({
        request_id: (globalThis.crypto?.randomUUID?.() || String(Date.now())),
        stage_id: currentStageId(), operators: selectedOperators(),
        instruction: $("#researchDesignInstruction").value,
        preview_fingerprint: agentPreview.preview_fingerprint,
        consent_to_send: true, possible_cost_confirmed: true
      })
    });
    agentRun = run;
    renderAgentRun(run);
    status("Agent 已启动，正在多轮检索；可在下方查看轨迹。");
    pollAgent(run.id);
  }

  function pollAgent(runId) {
    clearInterval(agentTimer);
    const pid = pp.current?.project?.id;
    agentTimer = setInterval(async () => {
      if (!pid || pp.current?.project?.id !== pid) { clearInterval(agentTimer); return; }
      try {
        const run = await pp.api(agentBase(pid) + `/runs/${runId}`);
        agentRun = run;
        renderAgentRun(run);
        if (!["running", "created"].includes(run.status)) {
          clearInterval(agentTimer);
          if ((run.result?.cards || []).length) {
            candidates = [{ id: run.id, stage_id: run.stage_id, cards: run.result.cards,
                            created_at: run.updated_at, notes: run.result }, ...candidates];
            renderCards();
          }
          status(run.status === "succeeded"
            ? `Agent 完成，产出 ${(run.result?.cards || []).length} 条候选（未写入任何字段）。`
            : `Agent ${run.status}：${run.error_code || run.blocked_reason || ""}`, run.status === "failed");
        }
      } catch (error) { clearInterval(agentTimer); status(message(error), true); }
    }, 4000);
  }

  async function cancelAgent() {
    const pid = pp.current?.project?.id;
    if (!pid || !agentRun) return;
    await pp.api(agentBase(pid) + `/runs/${agentRun.id}/cancel`, { method: "POST", body: "{}" });
    status("已请求取消；当前步结束后停止。");
  }

  document.querySelectorAll('input[name="rdMode"]').forEach(input => { input.onchange = syncMode; });
  $("#rdAgentStart").onclick = () => perform(async () => {
    if (agentPreview && $("#rdConsent").checked) await startAgent();
    else { await previewAgent(); refreshGate(); }
  });

  $("#rdPreview").onclick = () => perform(mode() === "agent" ? previewAgent : doPreview);
  $("#rdGenerate").onclick = () => perform(doGenerate);
  $("#rdConsent").onchange = refreshGate;

  // The panel follows the single workflow projection.
  pp.renderResearchDesign = async startup => {
    view = startup;
    const panel = $("#researchDesign");
    const pid = pp.current?.project?.id || "";
    if (!pid || !startup) { panel.hidden = true; return; }
    for (const definition of startup.definitions) STAGE_FIELDS[definition.id] = Object.keys(definition.fields);
    const stageId = currentStageId();
    $("#researchDesignOps").hidden = stageId !== "STAGE-01";
    const supported = Boolean(STAGE_FIELDS[stageId]);
    const editable = !pp.current?.project?.archived && pp.current?.project?.lifecycle_state !== "published";
    panel.hidden = !supported || !editable;
    if (panel.hidden) return;

    if (pid !== projectId || panel.dataset.stageId !== stageId) {
      panel.dataset.stageId = stageId;
      projectId = pid;
      meta = null;
      preview = null;
      candidates = [];
      $("#researchDesignCards").innerHTML = "";
      $("#researchDesignPreview").hidden = true;
      $("#rdConsent").checked = false;
      $("#rdGenerate").disabled = true;
      $("#researchDesignInstruction").value = "";
      status(stageId === "STAGE-01" ? "选择算子后先预览，再授权生成。" : "按当前阶段目标预览材料，再授权生成候选。");
    }
    await loadMeta();
    renderOperators();
    await loadCandidates();
    renderCards();
  };

  document.addEventListener("paperpilot:project-changed", () => {
    projectId = "";
    preview = null;
    candidates = [];
    const panel = $("#researchDesign");
    if (panel) panel.hidden = true;
  });
})();
