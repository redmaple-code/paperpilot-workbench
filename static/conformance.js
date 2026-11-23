(function () {
  "use strict";

  const VERDICT_LABELS = { correct: "正确", wrong: "错误", partial: "部分符合", unverified: "待核" };
  const AUTO_LABELS = {
    supported: "有可核对入口",
    partial: "入口不完整",
    unsupported: "入口缺失",
    unverified: "无可核对入口",
  };
  const CAUSE_LABELS = {
    requirement_ambiguity: "需求表述模糊/不可观察",
    confirmation_gap: "未经人工确认即实现",
    design_drift: "工程设计偏离需求",
    implementation_gap: "实现未按设计落地",
    ui_entry_gap: "能力存在但缺少入口/形态不符",
    integration_seam: "跨模块接缝断裂",
    evidence_gap: "证据不足或不可复核",
    scope_creep: "实现超出确认范围",
    unknown: "未定位",
  };

  const state = { matrix: null, chain: null, catalog: null, filters: { req: "", verdict: "", auto: "", text: "" }, openReqs: new Set() };

  const STAGE_LABELS = {
    function: "需求与 AC",
    product: "形态存在",
    product_confirmed: "形态已确认",
    engineering_package: "工程包",
    package_bound: "形态版本绑定",
    code: "代码文件",
    tests: "测试入口",
    product_acceptance: "产品验收",
  };

  const $ = (id) => document.getElementById(id);

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function verdictClass(verdict) {
    if (verdict === "correct") return "ok";
    if (verdict === "wrong") return "bad";
    if (verdict === "partial") return "warn";
    return "";
  }

  async function api(path, options) {
    const response = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, options || {}));
    if (!response.ok) {
      let message = response.statusText;
      try {
        const body = await response.json();
        message = (body.detail && (body.detail.message || body.detail)) || message;
      } catch (error) {
        /* ignore */
      }
      throw new Error(String(message));
    }
    return response;
  }

  async function load(options) {
    const opts = options || {};
    if (opts.scan) {
      $("cfGenerated").textContent = "正在扫描真实仓库…";
      await api("/api/conformance/scan", { method: "POST" });
    }
    const response = await api("/api/conformance");
    state.matrix = await response.json();
    const chainResponse = await api("/api/conformance/chain");
    state.chain = await chainResponse.json();
    render();
  }

  function render() {
    const matrix = state.matrix;
    if (!matrix) return;
    const summary = matrix.summary;
    $("cfReqCount").textContent = summary.requirements;
    $("cfAcCount").textContent = summary.acceptance_criteria;
    $("cfCorrect").textContent = summary.ac_verdicts.correct || 0;
    $("cfWrong").textContent = summary.ac_verdicts.wrong || 0;
    $("cfPartial").textContent = summary.ac_verdicts.partial || 0;
    $("cfUnverified").textContent = summary.ac_verdicts.unverified || 0;
    $("cfGenerated").textContent =
      "生成时间 " + matrix.generated_at + "　｜　自动核对只证明仓库里还有可复核入口，不证明产品正确；正确/错误仅来自人工判定。";

    renderRequestFilter();
    renderCauses();
    renderChain();
    renderList();
  }

  function renderChain() {
    const chain = state.chain;
    const stagesNode = $("cfStages");
    const bodyNode = $("cfChainBody");
    stagesNode.replaceChildren();
    bodyNode.replaceChildren();
    if (!chain) {
      $("cfChainSummary").textContent = "链路数据不可用";
      return;
    }
    const summary = chain.summary;
    $("cfChainSummary").textContent =
      "链路完整 " + summary.chain_complete + "/" + summary.requirements +
      "　形态已确认 " + summary.forms.confirmed +
      "　诊断门禁条件 " + summary.gate_blocked + " 条（仅诊断，不代表系统实际阻断）";

    formsStageSummary(summary).forEach((stage) => {
      const card = el("div", "cf-stage" + (stage.count === summary.requirements ? " full" : stage.count === 0 ? " none" : ""));
      card.appendChild(el("span", "", stage.label));
      card.appendChild(el("b", "", stage.count + "/" + summary.requirements));
      stagesNode.appendChild(card);
    });

    const table = el("table", "cf-chain-table");
    const thead = el("thead");
    const headRow = el("tr");
    ["需求", "就绪度", "产品形态", "工程包", "形态绑定", "AC 映射", "缺失环节", "诊断门禁条件"].forEach((label) => {
      headRow.appendChild(el("th", "", label));
    });
    thead.appendChild(headRow);
    table.appendChild(thead);
    const tbody = el("tbody");
    chain.requirements.forEach((item) => {
      const row = el("tr", item.gate_blocked ? "blocked" : "");
      row.appendChild(el("td", "", item.requirement_id + " " + item.title));
      row.appendChild(el("td", "", item.readiness));
      row.appendChild(el("td", "", item.form_status + (item.form_version ? " v" + item.form_version : "")));
      row.appendChild(el("td", "", item.package_present ? "有" : "无"));
      row.appendChild(el("td", "", item.stages.package_bound ? "是" : "否"));
      row.appendChild(el("td", "", item.ac_mapped + "/" + item.ac_count));
      row.appendChild(el("td", "", item.missing_stages.map((stage) => STAGE_LABELS[stage] || stage).join("、") || "—"));
      row.appendChild(el("td", "", item.gate_blocked ? "条件命中" : "未命中"));
      tbody.appendChild(row);
    });
    table.appendChild(tbody);
    bodyNode.appendChild(table);
  }

  function formsStageSummary(summary) {
    const stages = summary.stages || {};
    const total = summary.requirements;
    return ["product", "product_confirmed", "engineering_package", "package_bound", "code", "tests", "product_acceptance"].map((stage) => ({
      label: STAGE_LABELS[stage] || stage,
      count: stages[stage] || 0,
      total,
    }));
  }

  function chainByRequirement() {
    const map = {};
    if (!state.chain) return map;
    state.chain.requirements.forEach((item) => {
      map[item.requirement_id] = item;
    });
    return map;
  }

  function renderRequestFilter() {
    const select = $("cfFilterReq");
    const current = select.value;
    select.replaceChildren(el("option", "", "全部"));
    select.firstChild.value = "";
    state.matrix.requirements.forEach((req) => {
      const option = el("option", "", req.requirement_id + " " + req.title);
      option.value = req.requirement_id;
      select.appendChild(option);
    });
    select.value = current && state.matrix.requirements.some((r) => r.requirement_id === current) ? current : "";
  }

  function renderCauses() {
    const container = $("cfCauses");
    container.replaceChildren();
    const causes = state.matrix.summary.root_causes || {};
    const entries = Object.entries(causes).sort((a, b) => b[1] - a[1]);
    if (!entries.length) {
      container.appendChild(el("span", "cf-cause-chip", "根因分布：尚无人工判定"));
      return;
    }
    entries.forEach(([cause, count]) => {
      const chip = el("span", "cf-cause-chip", CAUSE_LABELS[cause] || cause);
      chip.appendChild(el("b", "", String(count)));
      container.appendChild(chip);
    });
  }

  function matches(req, ac) {
    const filters = state.filters;
    if (filters.req && req.requirement_id !== filters.req) return false;
    if (filters.verdict && ac.verdict !== filters.verdict) return false;
    if (filters.auto && ac.auto_verdict !== filters.auto) return false;
    if (filters.text) {
      const haystack = [req.requirement_id, req.title, ac.ac_id, ac.given, ac.when, ac.then, ac.verification]
        .join(" ")
        .toLowerCase();
      if (!haystack.includes(filters.text.toLowerCase())) return false;
    }
    return true;
  }

  function renderList() {
    const list = $("cfList");
    list.replaceChildren();
    let visible = 0;
    state.matrix.requirements.forEach((req) => {
      const acs = req.acceptance_criteria.filter((ac) => matches(req, ac));
      if (!acs.length) return;
      visible += 1;
      list.appendChild(renderRequirement(req, acs));
    });
    if (!visible) list.appendChild(el("div", "cf-empty", "没有符合筛选条件的验收条件。"));
  }

  function renderRequirement(req, acs) {
    const wrap = el("article", "cf-req");
    if (state.openReqs.has(req.requirement_id)) wrap.classList.add("open");

    const head = el("div", "cf-req-head");
    head.setAttribute("role", "button");
    head.setAttribute("tabindex", "0");
    head.addEventListener("click", () => toggleReq(wrap, req.requirement_id));
    head.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        toggleReq(wrap, req.requirement_id);
      }
    });

    const title = el("div", "cf-req-title", req.requirement_id + " " + req.title);
    title.appendChild(el("small", "", "v" + req.version + " · " + req.readiness));
    head.appendChild(title);

    const meta = el("div", "cf-req-meta");
    const verdictBadge = el("span", "cf-badge " + verdictClass(req.verdict), VERDICT_LABELS[req.verdict] || req.verdict);
    meta.appendChild(verdictBadge);
    meta.appendChild(el("span", "cf-badge auto-" + req.auto_verdict, "自动：" + (AUTO_LABELS[req.auto_verdict] || req.auto_verdict)));
    meta.appendChild(el("span", "cf-badge", "AC " + acs.length));
    meta.appendChild(el("span", "cf-badge", "证据 " + req.evidence_passed + "/" + req.evidence_count));
    const chainItem = chainByRequirement()[req.requirement_id];
    if (chainItem) {
      const formClass = chainItem.form_status === "confirmed" ? "ok" : chainItem.form_status === "none" ? "bad" : "warn";
      meta.appendChild(el("span", "cf-badge " + formClass, "形态 " + chainItem.form_status));
    }
    if (req.confirmation_lock !== "locked") meta.appendChild(el("span", "cf-badge warn", "未锁定"));
    head.appendChild(meta);
    wrap.appendChild(head);

    const body = el("div", "cf-req-body");
    body.appendChild(renderReqJudgement(req));
    if (req.checks.length) body.appendChild(renderChecks(req.checks));
    acs.forEach((ac) => body.appendChild(renderAc(req, ac)));
    wrap.appendChild(body);
    return wrap;
  }

  function toggleReq(wrap, reqId) {
    wrap.classList.toggle("open");
    if (wrap.classList.contains("open")) state.openReqs.add(reqId);
    else state.openReqs.delete(reqId);
  }

  function renderChecks(checks) {
    const list = el("ul", "cf-checks");
    checks.forEach((check) => {
      list.appendChild(el("li", check.severity, "[" + check.severity + "] " + check.label + "：" + check.message));
    });
    return list;
  }

  function renderReqJudgement(req) {
    return renderJudgeForm("整条需求判定（应用到未单独判定的 AC）", req.requirement_id + "::*", req.judgement, req.requirement_id + " " + req.title);
  }

  function renderAc(req, ac) {
    const wrap = el("div", "cf-ac");
    const head = el("div", "cf-ac-head");
    head.addEventListener("click", () => wrap.classList.toggle("open"));
    head.appendChild(el("span", "cf-ac-id", ac.ac_id));
    head.appendChild(el("span", "cf-ac-then", ac.then || "（缺少 then 描述）"));
    head.appendChild(el("span", "cf-badge " + verdictClass(ac.verdict), VERDICT_LABELS[ac.verdict] || ac.verdict));
    head.appendChild(el("span", "cf-badge auto-" + ac.auto_verdict, AUTO_LABELS[ac.auto_verdict] || ac.auto_verdict));
    wrap.appendChild(head);

    const body = el("div", "cf-ac-body");
    if (ac.given) body.appendChild(field("Given", ac.given));
    if (ac.when) body.appendChild(field("When", ac.when));
    if (ac.then) body.appendChild(field("Then", ac.then));
    if (ac.verification) body.appendChild(field("验证", ac.verification));
    if (ac.checks.length) body.appendChild(renderChecks(ac.checks));
    body.appendChild(
      renderJudgeForm("本条验收条件判定", req.requirement_id + "::" + ac.ac_id, ac.judgement, ac.ac_id + " " + (ac.then || ""))
    );
    wrap.appendChild(body);
    return wrap;
  }

  function field(label, value) {
    const row = el("div", "cf-field");
    row.appendChild(el("b", "", label));
    row.appendChild(document.createTextNode(value));
    return row;
  }

  function renderJudgeForm(title, target, judgement, contextLabel) {
    const wrapper = el("div", "cf-judge");
    wrapper.setAttribute("data-target", target);

    const verdictLabel = el("label", "", "判定");
    const verdictSelect = el("select");
    Object.entries(VERDICT_LABELS).forEach(([value, label]) => {
      const option = el("option", "", label);
      option.value = value;
      verdictSelect.appendChild(option);
    });
    verdictSelect.value = (judgement && judgement.verdict) || "unverified";
    verdictLabel.appendChild(verdictSelect);
    wrapper.appendChild(verdictLabel);

    const causeLabel = el("label", "", "根因");
    const causeSelect = el("select");
    Object.entries(CAUSE_LABELS).forEach(([value, label]) => {
      const option = el("option", "", label);
      option.value = value;
      causeSelect.appendChild(option);
    });
    causeSelect.value = (judgement && judgement.cause) || "unknown";
    causeLabel.appendChild(causeSelect);
    wrapper.appendChild(causeLabel);

    const evidenceLabel = el("label", "", "证据 / 复现路径");
    const evidenceInput = el("input");
    evidenceInput.type = "text";
    evidenceInput.placeholder = "截图、测试名、文件行号或操作步骤";
    evidenceInput.value = (judgement && judgement.evidence) || "";
    evidenceLabel.appendChild(evidenceInput);
    wrapper.appendChild(evidenceLabel);

    const observedLabel = el("label", "cf-span-3", "实际表现（产品实际做了什么）");
    const observedArea = el("textarea");
    observedArea.value = (judgement && judgement.observed) || "";
    observedLabel.appendChild(observedArea);
    wrapper.appendChild(observedLabel);

    const noteLabel = el("label", "cf-span-3", "备注（期望差异、影响范围）");
    const noteArea = el("textarea");
    noteArea.value = (judgement && judgement.note) || "";
    noteLabel.appendChild(noteArea);
    wrapper.appendChild(noteLabel);

    const actions = el("div", "cf-judge-actions");
    const save = el("button", "primary", "保存判定");
    save.type = "button";
    const status = el("span", "cf-saved", "");
    actions.appendChild(save);
    actions.appendChild(status);
    wrapper.appendChild(actions);

    save.addEventListener("click", async () => {
      save.disabled = true;
      status.textContent = "保存中…";
      try {
        await api("/api/conformance/judgements", {
          method: "POST",
          body: JSON.stringify({
            target: target,
            verdict: verdictSelect.value,
            cause: causeSelect.value,
            observed: observedArea.value,
            note: noteArea.value,
            evidence: evidenceInput.value,
          }),
        });
        status.textContent = "已保存";
        await load();
      } catch (error) {
        status.textContent = "保存失败：" + error.message;
      } finally {
        save.disabled = false;
      }
    });

    const hint = el("div", "cf-note", title + " · " + contextLabel);
    const container = el("div");
    container.appendChild(hint);
    container.appendChild(wrapper);
    return container;
  }

  function bindFilters() {
    $("cfFilterReq").addEventListener("change", (event) => {
      state.filters.req = event.target.value;
      renderList();
    });
    $("cfFilterVerdict").addEventListener("change", (event) => {
      state.filters.verdict = event.target.value;
      renderList();
    });
    $("cfFilterAuto").addEventListener("change", (event) => {
      state.filters.auto = event.target.value;
      renderList();
    });
    $("cfSearch").addEventListener("input", (event) => {
      state.filters.text = event.target.value.trim();
      renderList();
    });
  }

  function bindActions() {
    $("cfChainScan").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      $("cfChainSummary").textContent = "正在校验链路…";
      try {
        await api("/api/conformance/chain/scan", { method: "POST" });
        await load();
      } catch (error) {
        $("cfChainSummary").textContent = "链路校验失败：" + error.message;
      } finally {
        button.disabled = false;
      }
    });
    $("cfScan").addEventListener("click", async (event) => {
      event.target.disabled = true;
      try {
        await load({ scan: true });
      } catch (error) {
        $("cfGenerated").textContent = "扫描失败：" + error.message;
      } finally {
        event.target.disabled = false;
      }
    });
    $("cfReport").addEventListener("click", async () => {
      try {
        const response = await api("/api/conformance/report");
        $("cfReportBody").textContent = await response.text();
        $("cfReportDialog").showModal();
      } catch (error) {
        $("cfReportBody").textContent = "读取报告失败：" + error.message;
        $("cfReportDialog").showModal();
      }
    });
    $("cfReportClose").addEventListener("click", () => $("cfReportDialog").close());
  }

  bindFilters();
  bindActions();
  load().catch((error) => {
    $("cfGenerated").textContent = "加载失败：" + error.message;
  });
})();
