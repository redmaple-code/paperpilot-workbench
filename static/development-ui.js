/* Development surfaces for the confirmed REQ-003/009/010/011/013/015/017
 * contracts.  They intentionally sit beside the existing UI modules: the
 * product shell remains usable when a project has no workflow or no history.
 */
(() => {
  const pp = window.paperpilot;
  if (!pp) return;
  const $ = (selector, root = document) => root.querySelector(selector);
  const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;", "'":"&#39;"}[c]));
  const uid = () => globalThis.crypto?.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const labels = {
    queued: "排队中", running: "运行中", succeeded: "成功", failed: "失败", cancelled: "已取消", interrupted: "进程重启中断", superseded: "已由更新自动预览替代",
    candidate_ready: "候选待审阅", applied: "已应用", rejected: "已拒绝", stale: "已过期",
    partially_applied: "部分应用"
  };
  const state = { projectId: "", settings: null, builds: [], modifications: [], graph: null, selectedOutlineNode: null, outlineDraft: null, outlineHistory: [], linkableResources: [], cleanupPreview: null };
  let editorDraftTimer = null;
  let automaticPreviewTimer = null;
  const automaticBuilds = new Map();
  let conversionPreview = null;
  let buildPreviewToken = 0;
  const buildRetriesPending = new Set();
  const editorDraftWrites = new Map();
  const outlineDraftQueues = new Map();
  const outlinePageId = uid();
  const outlinePageKeys = new Map();
  const outlineRecovery = { editor: {items:[],hasMore:false,request:0}, node: {items:[],hasMore:false,request:0} };
  let outlineRefreshToken = 0;
  let outlineSelectionToken = 0;
  let outlineTextDraftToken = 0;
  let outlineTextDraft = null;
  let outlineNodeDirty = false;
  const pendingOutlineMutations = new Map();
  let buildPreviewMode = 'live';
  let restorePreviewToken = 0;
  let settingsLoadToken = 0;
  let settingsProjectId = '';

  function projectId() { return pp.data?.project?.id || pp.current?.project?.id || ""; }
  function project() { return pp.data?.project || pp.current?.project || null; }
  function selectedFile() { return pp.data?.files?.find(file => file.path === $("#fileSelect")?.value) || null; }
  function activeTarget() {
    const outline = $("#outline")?.classList.contains("active");
    if (outline) return { kind: "outline", path: "", content: $("#outlineText")?.value || "", version: pp.data?.outline_version || 0 };
    const file = selectedFile();
    return { kind: "file", path: file?.path || "", content: $("#editor")?.value || "", version: file?.version || 0 };
  }
  function prettyTime(value) { return value ? new Date(value).toLocaleString() : "—"; }
  function showError(error) {
    try { const body = JSON.parse(error.message); return body.detail?.message || body.message || body.detail?.code || error.message; }
    catch { return error.message || "请求失败"; }
  }
  async function api(path, options) { return pp.api(path, options); }
  function announce(message, error = false) {
    const status = $("#status");
    if (status) { status.textContent = message; status.classList.toggle("warning", error); }
  }

  function injectPanels() {
    if (!$("#editorGovernance") && $("#editor")) {
      $("#editor").insertAdjacentHTML("afterend", `
        <section id="editorGovernance" class="editor-governance" aria-label="编辑器保存与转换">
          <div class="inline-fields"><span id="editorSaveStatus" class="muted" role="status" aria-live="polite">已保存</span><button id="restoreEditorDraft" type="button" hidden>恢复草稿</button><button id="discardEditorDraft" type="button" hidden>丢弃草稿</button></div>
          <div class="inline-fields"><label>转换目标路径<input id="editorConversionPath" maxlength="240" placeholder="例如 sections/introduction.md"></label><button id="previewEditorConversion" type="button">预览转换</button><button id="applyEditorConversion" type="button" disabled>应用到目标文件</button></div>
          <pre id="editorConversionDiff" hidden></pre>
        </section>`);
    }
    if (!$("#targetedModificationPanel") && $("#editor")) {
      $("#editor").insertAdjacentHTML("afterend", `
        <section id="targetedModificationPanel" class="result targeted-modification" aria-labelledby="targetedModificationTitle">
          <h3 id="targetedModificationTitle">指定部分修改</h3>
          <p class="muted">选中正文或大纲片段，提交修改指令。候选生成后先比较，再决定是否写入。</p>
          <div class="inline-fields"><input id="modificationInstruction" maxlength="12000" placeholder="例如：保留含义，改成更接近参考文献的学术表述"><button id="createModification" type="button">生成指定修改候选</button><button id="refreshModifications" type="button">刷新修改请求</button></div>
          <div id="modificationStatus" class="muted" role="status" aria-live="polite"></div>
          <div class="inline-fields"><label>清理范围<select id="modificationCleanupScope"><option value="project">整个项目</option><option value="operation">单个操作</option></select></label><input id="modificationCleanupOperation" placeholder="操作 ID 或 request_id" hidden><button id="previewModificationCleanup" type="button">预览清理</button></div>
          <div id="modificationCleanupPreview" aria-live="polite"></div>
          <div id="modificationList"></div>
        </section>`);
    }
    if (!$("#citationReviewPanel") && $("#referenceLibraryCard")) {
      $("#referenceLibraryCard").insertAdjacentHTML("beforeend", `
        <section id="citationReviewPanel" class="citation-review" aria-labelledby="citationReviewTitle">
          <h4 id="citationReviewTitle">论断—文献支撑审查</h4>
          <button id="runCitationReview" type="button">预览本次检查</button>
          <div id="citationReviewPreflight" hidden></div>
          <div class="inline-fields"><label>历史报告<select id="citationReviewHistory"><option value="">选择历史报告</option></select></label><button id="openCitationReviewHistory" type="button">打开</button></div>
          <div id="citationReviewStatus" class="muted" role="status" aria-live="polite"></div>
          <div id="citationReviewResult"></div>
        </section>`);
    }
    const corpusCard = $("#corpusList")?.closest(".card");
    if (corpusCard && !$("#corpusIndexPanel")) {
      corpusCard.insertAdjacentHTML("beforeend", `
        <section id="corpusIndexPanel" class="corpus-index-panel" aria-labelledby="corpusIndexTitle">
          <h4 id="corpusIndexTitle">语料原文索引与检索</h4>
          <div id="corpusIndexList"></div>
          <div class="inline-fields"><input id="corpusSearchQuery" maxlength="500" placeholder="检索已授权的当前原文"><button id="searchCorpus" type="button">检索</button></div>
          <div id="corpusSearchStatus" class="muted" role="status" aria-live="polite"></div><div id="corpusSearchResults"></div>
        </section>`);
    }
    if ($("#outlineTree") && !$("#outlineOperations")) {
      $("#outlineTree").insertAdjacentHTML("beforebegin", `
        <section id="outlineOperations" class="outline-operations" aria-labelledby="outlineOperationsTitle">
          <h4 id="outlineOperationsTitle">结构化大纲节点操作</h4>
          <div class="inline-fields"><input id="outlineNodeTitle" maxlength="600" placeholder="新节点标题" aria-label="新节点标题"><input id="outlineNodePurpose" maxlength="2000" placeholder="新节点目的" aria-label="新节点目的"><select id="outlineNodeParent" aria-label="新节点父节点"><option value="">根节点</option></select><button id="outlineNodeCreate" type="button">新增节点</button><button id="outlineNodeRefresh" type="button">刷新树</button></div>
          <fieldset id="outlineNodeDetails" hidden>
            <legend>节点详情</legend>
            <div class="inline-fields"><label>标题<input id="outlineDetailTitle" maxlength="600"></label><label>目的<input id="outlineDetailPurpose" maxlength="2000"></label></div>
            <div class="inline-fields"><label>范围<textarea id="outlineDetailScope" maxlength="4000" rows="2"></textarea></label><label>状态<select id="outlineDetailStatus"><option value="draft">草稿</option><option value="todo">待开始</option><option value="in_progress">进行中</option><option value="review">待审阅</option><option value="done">完成</option><option value="blocked">受阻</option></select></label></div>
            <div class="inline-fields"><label>父节点<select id="outlineDetailParent"><option value="">根节点</option></select></label><label>顺序<input id="outlineDetailPosition" type="number" min="0" step="1"></label></div>
            <div class="inline-fields"><label>来源锚点<input id="outlineDetailAnchor" maxlength="500"></label><span id="outlineNodeDraftStatus" class="muted" role="status" aria-live="polite"></span></div>
            <div class="inline-fields"><button id="outlineNodeSave" type="button">保存节点</button><label>可恢复节点草稿<select id="outlineNodeDraftSelect" hidden aria-label="可恢复节点草稿"></select></label><button id="outlineNodeDraftRestore" type="button" hidden>恢复节点草稿</button><button id="outlineNodeDraftDiscard" type="button" hidden>删除所选节点草稿</button><button id="outlineNodeDraftMore" type="button" hidden>更多节点草稿</button></div>
          <fieldset id="outlineLinks">
            <legend>关联资源</legend>
            <div class="inline-fields"><label>论文资源<select id="outlineResourceSelect"><option value="">选择资源</option></select></label><button id="outlineResourceLink" type="button">关联到节点</button></div>
            <div id="outlineNodeLinks" aria-live="polite"></div>
          </fieldset>
          </fieldset>
          <div class="inline-fields"><label>大纲历史<select id="outlineHistorySelect"><option value="">选择历史版本</option></select></label><button id="outlineHistoryView" type="button">查看</button><button id="outlineHistoryRestore" type="button" disabled>恢复为新版本</button></div>
          <pre id="outlineHistoryDetails" hidden></pre>
          <div id="outlineOperationStatus" class="muted" role="status" aria-live="polite"></div>
        </section>`);
    }
    if (!$("#formatCheckPanel") && $("#settingsGovernance")) {
      $("#settingsGovernance").insertAdjacentHTML("afterend", `
        <section id="formatCheckPanel" class="format-check-panel" aria-labelledby="formatCheckTitle">
          <h4 id="formatCheckTitle">格式检查</h4>
          <p class="muted">按当前论文有效配置生成可定位警告；检查不会阻止保存、AI 操作或编译。</p>
          <button id="runFormatCheck" type="button">检查当前文件</button>
          <div class="inline-fields"><label>历史检查<select id="formatCheckHistory"><option value="">选择历史结果</option></select></label><button id="openFormatCheckHistory" type="button">打开</button></div>
          <div id="formatCheckStatus" class="muted" role="status" aria-live="polite"></div>
          <div id="formatCheckResult"></div>
        </section>`);
    }
    $("#createModification")?.addEventListener("click", createModification);
    $("#refreshModifications")?.addEventListener("click", refreshModifications);
    $("#previewModificationCleanup")?.addEventListener("click", () => previewModificationCleanup());
    $("#modificationCleanupScope")?.addEventListener("change", event => {
      $("#modificationCleanupOperation").hidden = event.target.value !== "operation";
    });
    $("#runCitationReview")?.addEventListener("click", runCitationReview);
    $("#openCitationReviewHistory")?.addEventListener("click", openCitationReviewHistory);
    $("#searchCorpus")?.addEventListener("click", searchCorpus);
    $("#outlineNodeCreate")?.addEventListener("click", createOutlineNode);
    $("#outlineNodeRefresh")?.addEventListener("click", reconcileOutlineMutation);
    $("#outlineNodeSave")?.addEventListener("click", saveOutlineNode);
    $("#outlineNodeDraftRestore")?.addEventListener("click", restoreOutlineNodeDraft);
    $("#outlineNodeDraftDiscard")?.addEventListener("click", discardOutlineNodeDraft);
    if($("#outlineNodeDraftMore"))$("#outlineNodeDraftMore").onclick=()=>loadOutlineRecovery("node",false);
    if($("#outlineStructuredStatus") && !$("#outlineTextRecovery")) {
      $("#outlineStructuredStatus").insertAdjacentHTML("afterend", `<div id="outlineTextRecovery" class="inline-fields" hidden><label>可恢复大纲草稿<select id="outlineTextDraftSelect" aria-label="可恢复大纲草稿"></select></label><button id="restoreOutlineTextDraft" type="button">恢复所选草稿</button><button id="discardOutlineTextDraft" type="button">删除所选草稿</button><button id="outlineTextDraftMore" type="button" hidden>更多大纲草稿</button></div>`);
      $("#restoreOutlineTextDraft").addEventListener("click",restoreOutlineTextDraft);
      $("#discardOutlineTextDraft").addEventListener("click",()=>discardOutlineRecovery("editor"));
      $("#outlineTextDraftMore").addEventListener("click",()=>loadOutlineRecovery("editor",false));
    }
    $("#outlineResourceLink")?.addEventListener("click", linkOutlineResource);
    $("#outlineHistoryView")?.addEventListener("click", viewOutlineHistory);
    $("#outlineHistoryRestore")?.addEventListener("click", restoreOutlineHistory);
    ["#outlineDetailTitle", "#outlineDetailPurpose", "#outlineDetailScope", "#outlineDetailStatus", "#outlineDetailParent", "#outlineDetailPosition", "#outlineDetailAnchor"].forEach(selector => {
      $(selector)?.addEventListener("input", scheduleOutlineNodeDraft);
      $(selector)?.addEventListener("change", scheduleOutlineNodeDraft);
    });
    $("#outlineText")?.addEventListener("input", scheduleOutlineTextDraft);
    $("#saveOutline")?.addEventListener("click", saveOutlineText, true);
    $("#outlineSaveDraft")?.addEventListener("click", saveOutlineDraftExplicitly, true);
    $("#runFormatCheck")?.addEventListener("click", runFormatCheck);
    $("#openFormatCheckHistory")?.addEventListener("click", openFormatCheckHistory);
    $("#restoreEditorDraft")?.addEventListener("click", restoreEditorDraft);
    $("#discardEditorDraft")?.addEventListener("click", discardEditorDraft);
    $("#previewEditorConversion")?.addEventListener("click", previewEditorConversion);
    $("#applyEditorConversion")?.addEventListener("click", applyEditorConversion);
    // The live editor controller owns author saves and recovery drafts.
    $("#fileSelect")?.addEventListener("change", () => { conversionPreview = null; refreshEditorDraft(); });
    $("#saveFile")?.addEventListener("click", saveEditorFile, true);
    $("#compile")?.addEventListener("click", startBuild, true);
  }

  async function startBuild(event) {
    event.preventDefault();
    event.stopImmediatePropagation();
    const button = $("#compile");
    if (button) button.disabled = true;
    const id = projectId(), token = ++buildPreviewToken;
    const purpose = $('#buildPurpose')?.value || 'draft_preview';
    buildPreviewMode = purpose === 'draft_preview' ? 'live' : 'explicit';
    try {
      if(purpose==='draft_preview')await flushVisibleDraft(id);
      const submitted = await api(`/api/projects/${id}/compile`, {
        method: "POST",
        body: JSON.stringify({ request_id: uid(), purpose, ...(purpose==='draft_preview'?{draft_source:$('#buildDraftSource').value}:{}) })
      });
      if (projectId() !== id) return;
      $("[data-tab='preview']")?.click();
      $("#compileLog").textContent = "构建任务已排队…";
      await refreshBuilds();
      await waitBuild(id, submitted.build_job.id, token);
    } catch (error) {
      if(projectId()===id)$("#compileLog").textContent = showError(error);
    } finally { if (button) button.disabled = false; }
  }
  async function waitBuild(id, jobId, token, automatic=false){
      while (projectId() === id) {
        await new Promise(resolve => setTimeout(resolve, 900));
        if(projectId()!==id)return;
        const job = await api(`/api/projects/${id}/builds/${encodeURIComponent(jobId)}`);
        if(projectId()!==id)return;
        if (automatic && (token !== buildPreviewToken || buildPreviewMode !== 'live')) return job;
        if (!automatic) await refreshBuilds();
        if (job.status === "succeeded") {
          $("#compileLog").textContent = automatic ? '预览已更新' : `编译成功 · ${job.purpose==='submitted'?'提交版':'预览'} · 快照论文版本${job.snapshot_revision} · ${buildSourceLabel(job.source)}。${job.current?'':'此结果已过期，仅作历史查看。'}${job.purpose.endsWith('preview')?'预览不能用于发布检查。':''}`;
          if(token===buildPreviewToken&&(!automatic||(buildPreviewMode==='live'&&job.current))){if(pp.openBuildReader)pp.openBuildReader(id,jobId,{background:automatic});else $("#pdfFrame").src = `/api/projects/${id}/builds/${encodeURIComponent(jobId)}/pdf?t=${Date.now()}`;}
          return job;
        }
        if (["failed", "cancelled", "interrupted", "superseded"].includes(job.status)) {
          $("#compileLog").textContent = automatic ? ('预览暂未更新，' + ($('#pdfFrame').getAttribute('src') ? '上一版 PDF 已保留。' : '') + '详情见版本与诊断。') : (job.diagnostics || []).map(item => item.message || item.code).join("\n") || `构建${labels[job.status] || job.status}`;
          return job;
        }
      }
  }

  function editorFile() { return selectedFile(); }
  function persistDraft(id,file,content){
    const key=id+'\n'+file.path,previous=editorDraftWrites.get(key)||Promise.resolve();
    const next=previous.catch(()=>{}).then(()=>api(`/api/projects/${id}/editor/draft`,{method:'PUT',body:JSON.stringify({path:file.path,base_version:file.version||0,base_content:file.content||'',content})}));
    editorDraftWrites.set(key,next);next.finally(()=>{if(editorDraftWrites.get(key)===next)editorDraftWrites.delete(key);}).catch(()=>{});return next;
  }
  async function flushVisibleDraft(id){
    clearTimeout(editorDraftTimer);
    if (pp.flushAllEditorChanges && !(await pp.flushAllEditorChanges(id))) throw new Error('正文尚未保存，请先处理保存提示。');
    const file=editorFile(),content=$('#editor').value;
    if(id!==projectId())throw new Error('论文已切换，请在当前论文重新预览。');
    if(file&&content!==file.content)await persistDraft(id,{...file},content);
    else if(file)await editorDraftWrites.get(id+'\n'+file.path);
    if(id!==projectId())throw new Error('论文已切换，原论文草稿已保留；请重新预览当前论文。');
    await pp.flushRevisionDraftsForBuild?.(id);
    if(id!==projectId())throw new Error('论文已切换，原论文草稿已保留；请重新预览当前论文。');
  }
  pp.flushVisibleDraftForReading=flushVisibleDraft;
  function buildSourceLabel(source){return source?.kind==='candidate'?`指定候选预检（${source.entries.length}个文件）`:source?.kind==='draft'?`${{all:'已保存草稿',editor:'编辑器草稿',revision:'人工修订草稿'}[source.draft_source]}覆盖${source.entries.length}项，其余为已保存正文`:'已保存正文';}
  function scheduleAutomaticPreview(id) {
    if (id !== projectId()) return;
    ++buildPreviewToken;
    buildPreviewMode = 'live';
    clearTimeout(automaticPreviewTimer);
    automaticPreviewTimer = setTimeout(() => automaticPreview(id), 1500);
  }
  pp.scheduleLivePreview = scheduleAutomaticPreview;

  async function automaticPreview(id) {
    if (id !== projectId() || !$('#autoPreviewBuild').checked || project()?.archived || project()?.lifecycle_state === 'published') return;
    const token = buildPreviewToken;
    const previous = automaticBuilds.get(id);
    if (previous) {
      await previous;
      if (id !== projectId() || token !== buildPreviewToken) return;
    }
    const task = (async () => {
      try {
        await flushVisibleDraft(id);
        if (id !== projectId() || token !== buildPreviewToken) return;
        const result = await api(`/api/projects/${id}/compile`, {
          method: 'POST', body: JSON.stringify({ request_id: uid(), purpose: 'draft_preview', draft_source: 'editor', trigger: 'automatic' })
        });
        if (id !== projectId() || token !== buildPreviewToken) return;
        $('#compileLog').textContent = '正在更新预览…';
        await waitBuild(id, result.build_job.id, token, true);
      } catch (error) {
        if (id === projectId() && token === buildPreviewToken) $('#compileLog').textContent = '预览暂未更新，' + showError(error);
      }
    })();
    automaticBuilds.set(id, task);
    try { await task; }
    finally { if (automaticBuilds.get(id) === task) automaticBuilds.delete(id); }
  }

  pp.preflightCandidate=async(candidateId,pid=projectId())=>{
    if(pid!==projectId())return announce('候选所属论文已切换，请在原论文重新打开候选。',true);
    const token=++buildPreviewToken;buildPreviewMode='explicit';
    try{const result=await api(`/api/projects/${pid}/compile`,{method:'POST',body:JSON.stringify({request_id:uid(),purpose:'candidate_preview',candidate_id:candidateId})});if(pid!==projectId())return;$("[data-tab='preview']")?.click();$('#compileLog').textContent='候选编译预检已排队；未采纳候选，正文与提交PDF保持不变。';await refreshBuilds();await waitBuild(pid,result.build_job.id,token);}catch(error){if(pid===projectId())announce(showError(error),true);}
  };
  async function saveEditorFile(event) {
    event?.preventDefault();
    event?.stopImmediatePropagation();
    return pp.flushEditorChanges?.();
  }

  function scheduleEditorDraft() {
    pp.editorChanged?.();
  }

  async function refreshEditorDraft() {
    const file = editorFile();
    if (!file || !projectId()) return;
    try {
      const draft = await api(`/api/projects/${projectId()}/editor/draft?path=${encodeURIComponent(file.path)}`);
      const available = draft.exists !== false && draft.content !== $("#editor").value;
      $("#restoreEditorDraft").hidden = !available;
      $("#discardEditorDraft").hidden = !available;
      if (available) {
        $("#editorSaveStatus").textContent = draft.stale
          ? `草稿基线 v${draft.base_version} 已过期；当前文件 v${draft.current_version}`
          : `有可恢复草稿 · ${prettyTime(draft.updated_at)}`;
      }
    } catch {}
  }

  async function restoreEditorDraft() {
    const file = editorFile();
    if (!file) return;
    const draft = await api(`/api/projects/${projectId()}/editor/draft?path=${encodeURIComponent(file.path)}`);
    if (draft.stale && !confirm("草稿基线已过期。恢复只会载入编辑器，不会覆盖当前文件，继续吗？")) return;
    $("#editor").value = draft.content;
    scheduleEditorDraft();
    announce("已载入编辑器草稿，尚未写入论文文件");
  }

  async function discardEditorDraft() {
    const file = editorFile();
    if (!file) return;
    await api(`/api/projects/${projectId()}/editor/draft?path=${encodeURIComponent(file.path)}`, { method: "DELETE" });
    $("#restoreEditorDraft").hidden = true;
    $("#discardEditorDraft").hidden = true;
    $("#editorSaveStatus").textContent = "草稿已丢弃";
  }

  async function previewEditorConversion() {
    const file = editorFile();
    const targetPath = $("#editorConversionPath").value.trim();
    if (!file || !targetPath) return announce("请先填写转换目标路径", true);
    const direction = file.path.toLowerCase().endsWith(".md") ? "markdown_to_latex" : "latex_to_markdown";
    try {
      conversionPreview = await api(`/api/projects/${projectId()}/editor/conversions/preview`, {
        method: "POST",
        body: JSON.stringify({ request_id: uid(), source_path: file.path, source_version: file.version || 0, target_path: targetPath, direction })
      });
      $("#editorConversionDiff").textContent = `目标文件：${targetPath}（v${conversionPreview.target_version}）\n\n--- 当前目标 ---\n${conversionPreview.target_before || "（不存在）"}\n\n--- 转换结果 ---\n${conversionPreview.target_content}`;
      $("#editorConversionDiff").hidden = false;
      $("#applyEditorConversion").disabled = false;
    } catch (error) { announce(showError(error), true); }
  }

  async function applyEditorConversion() {
    if (!conversionPreview) return;
    try {
      const applied = await api(`/api/projects/${projectId()}/editor/conversions/${encodeURIComponent(conversionPreview.id)}/apply`, {
        method: "POST", body: JSON.stringify({ request_id: uid() })
      });
      await pp.reload();
      $("#applyEditorConversion").disabled = true;
      announce(`转换已保存到 ${applied.path}；当前文件未被替换`);
    } catch (error) { announce(showError(error), true); }
  }

  function renderSettings(view) {
    const host = $("#settingsGovernance");
    if (!host || !view) return;
    const keys = [
      ["citation_style", "引用格式", "input"],
      ["term_policy", "术语策略", "select"],
      ["format_policy", "格式检查策略", "select"]
    ];
    host.replaceChildren();
    keys.forEach(([key, label, kind]) => {
      const row = document.createElement("div"); row.className = "settings-row";
      const title = document.createElement("strong"); title.textContent = label; row.append(title);
      let input;
      if (kind === "select") {
        input = document.createElement("select");
        const options = key === "term_policy" ? [["project_isolated", "论文独立"], ["domain_preferred", "领域优先"]] : [["warn_only", "仅提示"], ["strict", "严格阻止"]];
        options.forEach(([value, text]) => { const option = document.createElement("option"); option.value = value; option.textContent = text; input.append(option); });
      } else { input = document.createElement("input"); input.maxLength = 200; }
      input.id = `setting-${key}`; input.value = view.effective?.[key] ?? ""; input.dataset.settingKey = key;
      const source = document.createElement("span"); source.className = "muted settings-source";
      const sourceMeta = view.sources?.[key] || {}; const sourceName = {project_override:"论文覆盖",project_baseline:"论文基线",domain_baseline:"领域基线",unknown:"旧记录来源未知"}[sourceMeta.source] || "来源未知"; source.textContent = `${sourceName} · v${sourceMeta.version || view.baseline?.version || 0}${sourceMeta.domain_version ? ` · 领域 v${sourceMeta.domain_version}` : ""}`;
      const save = document.createElement("button"); save.type = "button"; save.textContent = "保存覆盖"; save.dataset.settingSave = key;
      const restore = document.createElement("button"); restore.type = "button"; restore.textContent = "恢复基线"; restore.disabled = sourceMeta.source !== "project_override"; restore.dataset.settingRestore = key;
      row.append(input, source, save, restore); host.append(row);
    });
    host.querySelectorAll("[data-setting-save]").forEach(button => button.addEventListener("click", () => saveSetting(button.dataset.settingSave)));
    host.querySelectorAll("[data-setting-restore]").forEach(button => button.addEventListener("click", () => restoreSetting(button.dataset.settingRestore)));
    renderSettingsSync(view);
  }
  async function loadSettings(discardEdits = []) {
    const id = projectId(); if (!id) return;
    const token = ++settingsLoadToken;
    try {
      const view = await api(`/api/projects/${id}/settings`);
      if (id !== projectId() || token !== settingsLoadToken) return;
      const localEdits = settingsProjectId === id
        ? [...document.querySelectorAll('[data-setting-key]')].filter(input =>
            !discardEdits.includes(input.dataset.settingKey) &&
            input.value !== String(state.settings?.effective?.[input.dataset.settingKey] ?? ''))
        : [];
      const values = localEdits.map(input => [input.id, input.value]);
      const focused = document.activeElement;
      const focusId = focused?.matches('[data-setting-key]') ? focused.id : '';
      const selection = [focused?.selectionStart, focused?.selectionEnd];
      state.settings = view;
      settingsProjectId = id;
      renderSettings(view);
      for (const [inputId, value] of values) document.getElementById(inputId).value = value;
      if (focusId) {
        const input = document.getElementById(focusId);
        input.focus({preventScroll: true});
        if (typeof selection[0] === 'number') input.setSelectionRange(...selection);
      }
    } catch (error) {
      if (id === projectId() && token === settingsLoadToken) $("#configurationStatus").textContent = showError(error);
    }
  }
  async function saveSetting(key) {
    const id = projectId(); const p = project(); const input = $(`#setting-${key}`); if (!id || !p || !input) return;
    try {
      await api(`/api/projects/${id}/settings/${key}`, { method: "PUT", body: JSON.stringify({ value: input.value, expected_project_revision: p.project_revision, request_id: uid() }) });
      await pp.reload(); await loadSettings(); announce(`${key} 已保存为论文级覆盖`);
    } catch (error) { announce(showError(error), true); }
  }
  async function restoreSetting(key) {
    const id = projectId(); const p = project(); if (!id || !p) return;
    try {
      await api(`/api/projects/${id}/settings/${key}/restore`, { method: "POST", body: JSON.stringify({ expected_project_revision: p.project_revision, request_id: uid() }) });
      await pp.reload(); await loadSettings([key]); announce(`${key} 已恢复论文基线值`);
    } catch (error) { announce(showError(error), true); }
  }
  async function runFormatCheck() {
    const target = activeTarget();
    if (!projectId()) return;
    const saved = target.kind === "file" ? selectedFile() : null;
    if (saved && target.content !== saved.content) {
      $("#formatCheckStatus").textContent = `“${target.path}”有未保存修改；本检查只读取已保存版本。请先保存后重试。`;
      return;
    }
    try {
      $("#formatCheckStatus").textContent = "正在检查…";
      const result = await api(`/api/projects/${projectId()}/format-check`, {
        method: "POST",
        body: JSON.stringify({ target_path: target.kind === "file" ? target.path : null, request_id: uid() })
      });
      renderFormatResult(result);
      await refreshFormatHistory();
      $("#formatCheckStatus").textContent = `${result.stale ? "检查结果已过期；" : ""}检查 ${result.summary.file_count} 个文件，发现 ${result.summary.warning_count} 条非阻断警告；配置来源已冻结在本次检查结果中。`;
    } catch (error) { $("#formatCheckStatus").textContent = showError(error); }
  }
  function renderFormatResult(result) {
    const host = $("#formatCheckResult");
    if (!host) return;
    host.replaceChildren();
    if (!result.warnings?.length) {
      host.textContent = "未发现格式警告。";
      return;
    }
    result.warnings.forEach(item => {
      const row = document.createElement("div");
      row.className = "format-warning-row";
      const locate = document.createElement("button");
      locate.type = "button";
      locate.textContent = `${item.path}:${item.line}:${item.column} · ${item.message}`;
      locate.addEventListener("click", () => {
        if (selectedFile()?.path !== item.path) $("#fileSelect").value = item.path;
        const editor = $("#editor");
        const lines = editor.value.split("\n");
        const offset = lines.slice(0, Math.max(0, item.line - 1)).reduce((sum, line) => sum + line.length + 1, 0) + Math.max(0, item.column - 1);
        editor.focus(); editor.setSelectionRange(offset, offset);
      });
      row.append(locate);
      host.append(row);
    });
  }
  async function refreshFormatHistory() {
    const select = $("#formatCheckHistory");
    if (!select || !projectId()) return;
    try {
      const runs = await api(`/api/projects/${projectId()}/format-check/runs?limit=20`);
      select.innerHTML = `<option value="">选择历史结果</option>` + runs.map(run =>
        `<option value="${esc(run.run_id)}">${run.stale ? "过期 · " : ""}${esc(run.target_path || "全部文件")} · ${esc(prettyTime(run.updated_at || run.created_at))}</option>`
      ).join("");
    } catch (error) { $("#formatCheckStatus").textContent = showError(error); }
  }
  async function openFormatCheckHistory() {
    const runId = $("#formatCheckHistory")?.value;
    if (!runId) return;
    try {
      const result = await api(`/api/projects/${projectId()}/format-check/runs/${encodeURIComponent(runId)}`);
      renderFormatResult(result);
      $("#formatCheckStatus").textContent = `${result.stale ? `历史结果已过期（${result.stale_reason.join("、")}）；` : ""}保存版本 v${result.source_project_revision} · ${result.summary.warning_count} 条警告`;
    } catch (error) { $("#formatCheckStatus").textContent = showError(error); }
  }
  function renderSettingsSync(view) {
    const host = $("#settingsSync"); if (!host) return;
    host.hidden = false;
    host.replaceChildren();
    const preview = view.sync_preview;
    const button = document.createElement("button"); button.type = "button"; button.textContent = "检查领域更新";
    button.addEventListener("click", async () => { const id = projectId(); try { const latest = await api(`/api/projects/${id}/settings/sync-preview`); if (id === projectId()) renderSyncPreview(latest); } catch (error) { if (id === projectId()) announce(showError(error), true); } });
    host.append(button);
    if (preview) renderSyncPreview(preview);
  }
  function renderSyncPreview(preview) {
    const host = $("#settingsSync"); if (!host) return;
    host.querySelectorAll(".sync-diff,.sync-commit").forEach(node => node.remove());
    if (!preview.available) {
      const note = document.createElement("div"); note.className = "muted sync-diff";
      if (preview.reason === "domain_binding_mismatch" && preview.binding_repair) {
        const repair = preview.binding_repair, id = preview.project_id;
        const summary = document.createElement("p"); summary.textContent = `论文当前领域：${repair.project_domain_name}；旧配置基线领域：${repair.baseline_domain_name}。同步已暂停。`;
        const values = document.createElement("pre"); values.style.whiteSpace = "pre-wrap"; values.style.overflowWrap = "anywhere"; values.textContent = `当前生效配置：\n${JSON.stringify(repair.effective_values, null, 2)}`;
        const explanation = document.createElement("p"); explanation.textContent = "可以保留上面的配置，将基线归属对齐到论文当前领域。不会迁移论文或自动采用领域默认；原基线会记录在本地历史，之后可另行比较并选择同步。部分旧来源会标记为未知，现有编译结果可能需要重新核验。";
        const fix = document.createElement("button"); fix.type = "button"; fix.textContent = "保留配置并修复基线归属"; fix.disabled = !repair.available;
        if (!repair.available) explanation.textContent += " 当前论文只读或当前领域不存在，请先解除只读或核对领域数据。";
        const requestId = uid();
        fix.addEventListener("click", async () => {
          if (id !== projectId() || !window.confirm("确认保留当前配置，并把配置基线归属对齐到论文当前领域？不会自动同步领域默认。")) return;
          fix.disabled = true;
          try {
            await api(`/api/projects/${id}/settings/binding-repair`, {method:"POST", body:JSON.stringify({expected_project_revision:repair.project_revision, expected_baseline_version:repair.baseline_version, expected_project_domain_id:repair.project_domain_id, expected_baseline_domain_id:repair.baseline_domain_id, request_id:requestId})});
            if (id !== projectId()) return;
            await pp.reload(); await loadSettings(); announce("配置值已保留，基线归属已修复；可重新检查领域更新。");
          } catch (error) { if (id === projectId()) { announce(showError(error), true); fix.disabled = false; } }
        });
        note.append(summary, values, explanation, fix);
      } else { note.textContent = preview.reason === "project_has_no_domain" ? "未绑定领域" : "领域没有新的配置差异"; }
      host.append(note); return;
    }
    const wrap = document.createElement("div"); wrap.className = "sync-diff";
    (preview.diff || []).forEach(item => { const label = document.createElement("label"); label.className = "check-label"; label.innerHTML = `<input type="checkbox" value="${esc(item.key)}" checked> ${esc(item.key)}：${esc(item.old)} → ${esc(item.new)}`; wrap.append(label); });
    const commit = document.createElement("button"); commit.className = "sync-commit"; commit.type = "button"; commit.textContent = "按选择同步到论文基线";
    commit.addEventListener("click", async () => { const selected = [...wrap.querySelectorAll("input:checked")].map(input => input.value); if (!selected.length) { announce("请至少选择一项配置；当前配置未改变。", true); return; } try { await api(`/api/projects/${projectId()}/settings/sync`, { method: "POST", body: JSON.stringify({ expected_baseline_version: preview.baseline.version, expected_domain_version: preview.to_version, selected_keys: selected, request_id: uid() }) }); await pp.reload(); await loadSettings(); announce("领域配置已按选择同步；论文覆盖仍保持独立"); } catch (error) { announce(showError(error), true); } });
    host.append(wrap, commit);
  }

  function renderBuilds() {
    const host = $("#buildHistoryList"); if (!host) return;
    host.dataset.projectId=projectId();
    host.replaceChildren();
    const retention=document.createElement('p');retention.className='muted';retention.dataset.cacheInventory='';
    retention.textContent='正在核对构建历史保留情况…';host.append(retention);
    const cleanup=document.createElement('button');cleanup.type='button';cleanup.textContent='检查并清理可释放的构建缓存';
    cleanup.onclick=async()=>{const id=projectId();cleanup.disabled=true;try{const result=await api(`/api/projects/${id}/builds/cache-cleanup`,{method:'POST'});if(id!==projectId())return;announce(result.blocked?'引用或未完成的清理需要先检查，未继续删除。':result.pending.length?`已清理 ${result.pruned.length} 项；${result.pending.length} 项需检查存储或目录。`:`已清理 ${result.pruned.length} 项，受保护构建和历史记录保留。`,result.blocked||Boolean(result.pending.length));await refreshBuilds();}catch(error){announce(showError(error),true);}finally{cleanup.disabled=false;}};host.append(cleanup);
    const inventoryProject=projectId();
    const environment=document.createElement('p');environment.className='muted';environment.textContent='正在检查本论文编译环境…';host.append(environment);
    api(`/api/projects/${inventoryProject}/build-environment`).then(info=>{
      if(inventoryProject!==projectId()||!environment.isConnected)return;
      environment.textContent=info.overridden?`本论文使用已固定的编译环境（${info.effective.PWA_LATEX_ENGINE}）；新构建使用该设置。`:'本论文使用本机默认编译环境。';
      if(info.overridden){const reset=document.createElement('button');reset.type='button';reset.textContent='改用本机默认编译环境';reset.onclick=async()=>{reset.disabled=true;try{const revision=project()?.project_revision;await api(`/api/projects/${inventoryProject}/build-environment/reset`,{method:'POST',body:JSON.stringify({request_id:uid(),expected_project_revision:revision})});if(inventoryProject===projectId()){await pp.reload();await refreshBuilds();announce('已改用本机默认环境；已有构建保留原配置，请重新编译以生成当前环境的 PDF。');}}catch(error){announce(showError(error),true);reset.disabled=false;}};environment.append(' ',reset);}
    }).catch(()=>{if(environment.isConnected)environment.textContent='无法检查论文编译环境，请刷新后重试。';});
    api(`/api/projects/${inventoryProject}/builds/cache-inventory`).then(info=>{
      if(inventoryProject!==projectId()||!retention.isConnected)return;
      retention.textContent=`成功构建 ${info.success_count} 次；最近 ${info.recent_limit} 次及已记录的发布、恢复和阅读引用受保护。${info.unknown_release_references?'存在无法识别的发布记录，清理已暂停。':''}${info.message}`;
      for(const [jobId,count] of Object.entries(info.persistent_pin_counts||{})){
        const row=host.querySelector(`[data-build-job="${CSS.escape(jobId)}"]`);if(!row)continue;
        const release=document.createElement('button');release.type='button';release.textContent=`释放遗留阅读保护（${count}）`;
        release.onclick=async()=>{if(!window.confirm('请先关闭此构建的所有阅读页面，等待下载和定位结束。释放保护后，旧缓存可能被自动清理。确定继续吗？'))return;release.disabled=true;try{await api(`/api/projects/${inventoryProject}/builds/${encodeURIComponent(jobId)}/clear-read-pins`,{method:'POST'});if(inventoryProject===projectId())await refreshBuilds();}catch(error){announce(showError(error),true);release.disabled=false;}};row.append(release);
      }
    }).catch(()=>{if(retention.isConnected)retention.textContent='无法核对构建历史保留情况；文件未被清理，请稍后刷新。';});
    if (!state.builds.length) { host.textContent = "暂无构建历史"; return; }
    state.builds.forEach(job => {
      const row = document.createElement("article"); row.className = "build-history-row";row.dataset.buildJob=job.id;
      const summary = document.createElement("div"); summary.textContent = `${labels[job.status] || job.status} · ${{draft_preview:"草稿预览",candidate_preview:"候选预检",submitted:"当前提交版",release:"发布构建"}[job.purpose]||job.purpose} · 论文v${job.snapshot_revision} · ${job.artifact_eligibility==="stale"?"已过期，仅历史":job.artifact_eligibility==="purpose_preview"?"预览用途，非发布证据":""} · ${prettyTime(job.created_at)}`; row.append(summary);
      if (job.started_at || job.finished_at) { const timing = document.createElement("small"); timing.className = "muted"; timing.textContent = `开始 ${prettyTime(job.started_at)} · 结束 ${prettyTime(job.finished_at)}`; row.append(timing); }
      const origin=document.createElement('small');origin.style.display='block';origin.textContent=`${job.trigger_kind==='automatic'?'自动预览':'显式构建'} · ${buildSourceLabel(job.source)}${job.queue_position?` · 等待序号${job.queue_position}`:''}${job.global_capacity_wait?` · ${job.global_capacity_message||'本机构建名额已满，完成后自动继续。'}${job.global_capacity_blockers?.length?` 涉及论文：${job.global_capacity_blockers.map(item=>item.project_name).join('、')}`:''}`:''}`;row.append(origin);
      const actions = document.createElement("div"); actions.className = "build-history-actions";
      if (job.status === "succeeded") { const open = document.createElement("button"); open.type = "button"; open.textContent = job.cache_pruned ? "缓存已清理，按快照重建" : "打开此 PDF"; open.addEventListener("click", () => { if(job.cache_pruned){retryBuild(job,true).catch(()=>{});return;} ++buildPreviewToken;buildPreviewMode='history';if(pp.openBuildReader)pp.openBuildReader(projectId(),job.id);else $("#pdfFrame").src = `/api/projects/${projectId()}/builds/${encodeURIComponent(job.id)}/pdf?t=${Date.now()}`; $("[data-tab='preview']")?.click(); }); const restore = document.createElement("button"); restore.type = "button"; restore.textContent = "载入为修订草稿"; restore.addEventListener("click", () => restoreBuild(job.id)); actions.append(open, restore); }
      if (["queued", "running"].includes(job.status)) { const cancel = document.createElement("button"); cancel.type = "button"; cancel.textContent = "取消构建"; cancel.addEventListener("click", () => cancelBuild(job.id)); actions.append(cancel); }
      if (["failed","cancelled","interrupted"].includes(job.status)){const retry=document.createElement("button");retry.type="button";retry.textContent="按此快照重试";retry.dataset.buildRetry=job.id;retry.disabled=Boolean(project()?.archived);retry.title=retry.disabled?'论文已归档，恢复论文后可以按原快照重试。':'';retry.onclick=()=>retryBuild(job);actions.append(retry);}
      const snapshot = document.createElement("button"); snapshot.type = "button"; snapshot.textContent = "查看快照"; snapshot.addEventListener("click", () => readSnapshot(job.snapshot_id)); actions.append(snapshot);
      row.append(actions);
      for(const diagnostic of job.diagnostics||[]){if(diagnostic.code==='tex_source_error'){const locate=document.createElement('button');locate.type='button';locate.textContent=`查看错误源码（${diagnostic.path}:${diagnostic.line}）`;locate.onclick=()=>pp.openBuildSource?.(projectId(),job.id,diagnostic.path,diagnostic.line);row.append(locate);}}
      if ((job.diagnostics || []).length) {
        const explanation=document.createElement('p');explanation.textContent=job.diagnostics.map(d=>d.message||(d.code==='cancelled'?(d.request_id?.startsWith('archive-')?'论文归档，构建已取消；旧成功PDF保留。':'构建已取消；可以查看原快照并明确重试。'):d.code==='worker_restart'?'服务重启，任务已中断；不会自动重跑。':`构建未完成（${d.code||'未知原因'}），请核对下面的诊断。`)).join('\n');row.append(explanation);
        const details=document.createElement('details'),caption=document.createElement('summary'),diag=document.createElement('pre');caption.textContent='查看诊断编号与技术详情';diag.className='build-diagnostics';diag.textContent=JSON.stringify(job.diagnostics,null,2);details.append(caption,diag);row.append(details);
      }
      host.append(row);
    });
  }
  async function refreshBuilds() {
    const id = projectId(); if (!id) return;
    try { await api(`/api/projects/${id}/builds/reconcile?stale_after_seconds=300`, { method: "POST" }); const jobs = await api(`/api/projects/${id}/builds?limit=30`); if(id!==projectId())return;state.builds=jobs; renderBuilds();pp.refreshBuildReading?.(id,jobs); } catch (error) { $("#compileLog").textContent = showError(error); }
  }
  async function cancelBuild(jobId) {
    try { const result=await api(`/api/projects/${projectId()}/builds/${encodeURIComponent(jobId)}/cancel?request_id=${encodeURIComponent(uid())}`, { method: "POST" }); await refreshBuilds(); const pending=result.diagnostics?.find(d=>d.code==="runtime_cleanup_pending");announce(pending?.message||"已取消构建；成功 PDF 保持不变",Boolean(pending)); }
    catch (error) { announce(showError(error), true); }
  }
  async function retryBuild(job, forceRebuild=false){
    if(buildRetriesPending.has(job.id))return;
    buildRetriesPending.add(job.id);
    const id=projectId(),token=++buildPreviewToken;buildPreviewMode='explicit';
    try{const result=await api(`/api/projects/${id}/compile`,{method:"POST",body:JSON.stringify({request_id:uid(),snapshot_id:job.snapshot_id,purpose:job.purpose,...(forceRebuild?{force_rebuild:true}:{})})});if(id!==projectId())return;$('#compileLog').textContent=forceRebuild?'已按此版本重新编译；旧PDF和正文保留，结果仍按原用途和版本判断。':'已按原快照明确重试；旧快照成功后仅作历史结果。';await refreshBuilds();return await waitBuild(id,result.build_job.id,token);}catch(error){if(id===projectId())$('#compileLog').textContent=showError(error);if(forceRebuild)throw error;}finally{buildRetriesPending.delete(job.id);}
  }
  pp.rebuildBuildReader=async(project,reading)=>{if(project!==projectId())throw new Error('论文已切换，请在当前论文重新选择构建。');return retryBuild({id:reading.job_id,snapshot_id:reading.snapshot_id,purpose:reading.purpose},true);};

  async function restoreBuild(jobId) {
    await previewRestore(jobId, []);
  }
  async function previewRestore(jobId, paths, removePaths=[], settingKeys=[], restoreEnvironment=false) {
    const id=projectId(),token=++restorePreviewToken,host=$('#buildRestorePanel');
    try {
      await flushVisibleDraft(id);
      const view=await api(`/api/projects/${id}/builds/${encodeURIComponent(jobId)}/restore-preview`,{method:'POST',body:JSON.stringify({include_paths:paths,remove_paths:removePaths,setting_keys:settingKeys,restore_build_environment:restoreEnvironment})});
      if(id!==projectId()||token!==restorePreviewToken)return;
      host.hidden=false;host.replaceChildren();
      const title=document.createElement('h3');title.textContent='历史恢复：先查看差异，再确认写入';
      const note=document.createElement('p');note.textContent=view.guidance;
      const status=document.createElement('p');status.setAttribute('role','status');status.dataset.restoreStatus='';
      host.append(title,note,status);
      const choices=document.createElement('div');
      view.files.forEach(file=>{
        const details=document.createElement('details'),summary=document.createElement('summary'),label=document.createElement('label'),checkbox=document.createElement('input');
        checkbox.type='checkbox';checkbox.value=file.path;checkbox.checked=file.selected;checkbox.dataset.restorePath=file.path;
        label.append(checkbox,document.createTextNode(` ${file.path} · ${file.changed?'有差异':'内容相同'}${!file.current_exists?' · 将重新创建':''}${file.conflict?' · 有未提交草稿冲突，请先处理或取消选择':''}`));summary.textContent=`查看 ${file.path} 源码差异`;details.append(summary);
        const diff=document.createElement('pre');diff.className='build-snapshot-details';diff.textContent=file.diff||'源码内容相同';details.append(diff);const item=document.createElement('div');item.append(label,details);choices.append(item);
      });
      host.append(choices);
      if(view.current_only_files?.length){
        const heading=document.createElement('h4');heading.textContent='当前独有文件：默认保留，可逐项选择移除';choices.append(heading);
        view.current_only_files.forEach(file=>{const item=document.createElement('div'),label=document.createElement('label'),checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.value=file.path;checkbox.checked=file.selected;checkbox.dataset.restoreRemovePath='';label.append(checkbox,document.createTextNode(` 移除 ${file.path}${file.conflict?' · 有未提交草稿，不能移除':''}`));const details=document.createElement('details'),summary=document.createElement('summary'),diff=document.createElement('pre');summary.textContent=`查看 ${file.path} 将被移除的内容`;diff.className='build-snapshot-details';diff.textContent=file.diff||'空文件';details.append(summary,diff);item.append(label,details);choices.append(item);});
      }
      const settingHeading=document.createElement('h4');settingHeading.textContent='论文配置：默认保留当前值，可逐项恢复历史值';choices.append(settingHeading);
      const settingNames={domain:'领域配置',style:'论文风格',citation_style:'引用格式',term_policy:'术语策略',format_policy:'格式检查策略'};
      const shown=value=>value===null||value===undefined||value===''?'未设置':typeof value==='string'?value:JSON.stringify(value);
      for(const item of view.configuration.choices||[]){const row=document.createElement('div'),label=document.createElement('label'),checkbox=document.createElement('input');checkbox.type='checkbox';checkbox.value=item.key;checkbox.checked=item.selected;checkbox.disabled=!item.available;checkbox.dataset.restoreUnavailable=String(!item.available);checkbox.dataset.restoreSetting='';label.append(checkbox,document.createTextNode(` ${settingNames[item.key]||item.key}：当前 ${shown(item.current)} → 历史 ${shown(item.historical)}${item.changed?'':'（相同）'}`));row.append(label);if(!item.available){const reason=document.createElement('p');reason.className='muted';reason.textContent=item.reason;row.append(reason);}choices.append(row);}
      const envLabel=document.createElement('label'),envCheck=document.createElement('input'),envNote=document.createElement('p');envCheck.type='checkbox';envCheck.checked=Boolean(view.configuration.environment_review?.selected);envCheck.dataset.restoreEnvironment='';envLabel.append(envCheck,document.createTextNode(' 恢复历史编译环境（先检查本地镜像）'));envNote.className='muted';envNote.dataset.restoreEnvironmentStatus='';envNote.textContent=view.configuration.environment_review?.message||'';choices.append(envLabel,envNote);
      const resources=document.createElement('details'),resourceTitle=document.createElement('summary'),resourceBody=document.createElement('pre');resourceTitle.textContent='查看完整资源、配置及当前独有文件差异';resourceBody.className='build-snapshot-details';resourceBody.textContent=JSON.stringify({historical_resources:view.assets,current_resources:view.current_assets,current_only_paths:view.current_only_paths,configuration:view.configuration},null,2);resources.append(resourceTitle,resourceBody);host.append(resources);
      const policy=document.createElement('p');policy.textContent=view.configuration.policy;host.append(policy);
      const update=document.createElement('button'),prepare=document.createElement('button'),cancel=document.createElement('button');
      update.type=prepare.type=cancel.type='button';update.textContent='更新所选差异';prepare.textContent='按此差异生成待审恢复草稿';prepare.dataset.restorePrepare='';prepare.disabled=!view.can_prepare;cancel.textContent='取消恢复';
      const selectedFiles=()=>[...choices.querySelectorAll('input[data-restore-path]:checked')].map(input=>input.value);
      const selectedRemovals=()=>[...choices.querySelectorAll('input[data-restore-remove-path]:checked')].map(input=>input.value);
      const selectedSettings=()=>[...choices.querySelectorAll('input[data-restore-setting]:checked')].map(input=>input.value);
      choices.addEventListener('change',()=>{prepare.disabled=true;status.textContent='选择已改变，请先更新所选差异。';});
      update.onclick=async()=>{const selectedPaths=selectedFiles();if(!selectedPaths.length){status.textContent='请至少选择一个历史文件。';return;}update.disabled=true;prepare.disabled=true;status.textContent=envCheck.checked?'正在检查本地镜像与恢复差异，请稍候。':'正在更新所选恢复差异，请稍候。';try{await previewRestore(jobId,selectedPaths,selectedRemovals(),selectedSettings(),envCheck.checked);}finally{if(update.isConnected){update.disabled=false;status.textContent='差异检查未完成，请根据提示处理后重新更新；现稿未改变。';}}};
      cancel.onclick=()=>{++restorePreviewToken;host.hidden=true;status.textContent='';announce('已取消恢复；当前正文和草稿未改动。');};
      prepare.onclick=async()=>{
        prepare.disabled=true;update.disabled=true;cancel.disabled=true;choices.querySelectorAll('input').forEach(input=>input.disabled=true);
        try {
          await flushVisibleDraft(id);
          const candidate=await api(`/api/projects/${id}/builds/${encodeURIComponent(jobId)}/restore`,{method:'POST',body:JSON.stringify({request_id:uid(),include_paths:paths,remove_paths:removePaths,setting_keys:settingKeys,restore_build_environment:restoreEnvironment,preview_fingerprint:view.preview_fingerprint})});
          if(id!==projectId()||token!==restorePreviewToken)return;
          status.textContent=`已生成${candidate.drafts.length}个待审恢复草稿，选择移除${candidate.removals.length}个当前独有文件、恢复${candidate.settings_to_restore.length}项论文配置${candidate.environment_to_restore?'及历史编译环境':''}，当前正文和配置尚未改变。${candidate.environment_notice||''}确认后将原子保存新版本并重新编译；关闭此页仍保留待审草稿。`;
          prepare.hidden=true;update.hidden=true;choices.querySelectorAll('input').forEach(input=>input.disabled=true);
          cancel.disabled=false;cancel.textContent='保留草稿并关闭';cancel.onclick=()=>{++restorePreviewToken;host.hidden=true;announce('恢复候选草稿已保留，正文尚未提交。');};
          const commit=document.createElement('button');commit.type='button';commit.textContent=candidate.removals.length||candidate.settings_to_restore.length||candidate.environment_to_restore?`确认恢复（移除${candidate.removals.length}个文件、恢复${candidate.settings_to_restore.length}项配置${candidate.environment_to_restore?'和编译环境':''}）并重新编译`:'确认写入新版本并重新编译';commit.dataset.restoreCommit='';host.append(commit);
          const requestId=uid();
          commit.onclick=async()=>{
            commit.disabled=true;cancel.disabled=true;const editor=$('#editor'),wasReadOnly=editor.readOnly;editor.readOnly=true;
            try {
              await flushVisibleDraft(id);
              const result=await api(`/api/projects/${id}/human-revision/build-restores/${encodeURIComponent(candidate.restore_id)}/commit`,{method:'POST',body:JSON.stringify({request_id:requestId,candidate_fingerprint:candidate.candidate_fingerprint})});
              if(id!==projectId()||token!==restorePreviewToken)return;
              status.textContent=`全部所选文件已保存到新版本；已移除${Object.keys(result.removed_versions||{}).length}个文件，恢复${Object.keys(result.restored_settings||{}).length}项论文配置；移除的文件保留在版本历史中，重新编译已排队。`;commit.hidden=true;cancel.disabled=false;cancel.textContent='关闭恢复结果';cancel.onclick=()=>{++restorePreviewToken;host.hidden=true;};
              await pp.reload();editor.readOnly=wasReadOnly;if(id!==projectId())return;await refreshBuilds();
              const previewToken=++buildPreviewToken;buildPreviewMode='explicit';await waitBuild(id,result.build_job.id,previewToken);
            } catch(error){if(id===projectId()&&token===restorePreviewToken)status.textContent=showError(error);commit.disabled=false;cancel.disabled=false;}
            finally{editor.readOnly=wasReadOnly;}
          };
        } catch(error){if(id===projectId()&&token===restorePreviewToken)status.textContent=showError(error);prepare.disabled=false;update.disabled=false;cancel.disabled=false;choices.querySelectorAll('input').forEach(input=>input.disabled=input.dataset.restoreUnavailable==='true');}
      };
      host.append(update,prepare,cancel);
      if(!view.can_prepare)status.textContent='当前有草稿冲突、资源缺失或所选配置不可恢复；请按上方指引处理。';
      host.scrollIntoView({block:'start'});
    } catch(error){if(id===projectId())announce(showError(error),true);}
  }
  async function readSnapshot(snapshotId) {
    try { const snapshot = await api(`/api/projects/${projectId()}/build-snapshots/${encodeURIComponent(snapshotId)}`); const host = $("#buildSnapshotDetails"); host.hidden = false; host.textContent = JSON.stringify({ id: snapshot.id, purpose: snapshot.purpose, project_revision: snapshot.project_revision, manifest: snapshot.manifest, settings: snapshot.settings, files: (snapshot.files || []).map(file => ({ path: file.path, version: file.version })) }, null, 2); }
    catch (error) { announce(showError(error), true); }
  }
  async function createHandoff() {
    const p = project(); if (!p) return;
    try { const id=projectId();await flushVisibleDraft(id);const snapshot = await api(`/api/projects/${id}/build-snapshots`, { method: "POST", body: JSON.stringify({ request_id: uid(), purpose: "draft_preview", draft_source:$("#buildDraftSource").value, expected_project_revision: p.project_revision }) }); $("#buildHandoffStatus").textContent = `编辑器交接快照已创建：${snapshot.id} · 项目版本 ${snapshot.project_revision} · ${snapshot.files?.length || 0} 个文件`; await refreshBuilds(); }
    catch (error) { announce(showError(error), true); }
  }
  function ensureHandoffButton() {
    const host = $("#buildHandoffStatus"); if (!host || $("#createBuildHandoff")) return;
    const button = document.createElement("button"); button.id = "createBuildHandoff"; button.type = "button"; button.textContent = "生成编辑器交接快照"; button.addEventListener("click", createHandoff); host.before(button);
  }

  function renderModifications() {
    const host = $("#modificationList"); if (!host) return; host.replaceChildren();
    if (!state.modifications.length) { host.textContent = "暂无指定修改请求"; return; }
    state.modifications.forEach(item => {
      const card = document.createElement("article"); card.className = "modification-row";
      const title = document.createElement("strong"); title.textContent = `${labels[item.status] || item.status} · ${item.target_path || "大纲"} · ${prettyTime(item.created_at)}`; card.append(title);
      const meta = document.createElement("p"); meta.className = "muted"; meta.textContent = `指令：${item.instruction} · 基线：${JSON.stringify(item.base_file_versions)} · 上下文：${String(item.context_version || "").slice(0, 12)}`; card.append(meta);
      const snap = document.createElement("details"); const sum = document.createElement("summary"); sum.textContent = "查看目标快照"; snap.append(sum); const pre = document.createElement("pre"); pre.textContent = item.snapshot?.selection || item.snapshot?.target_content || ""; snap.append(pre); card.append(snap);
      if (item.candidate) {
        const candidate = item.candidate; const candidateBox = document.createElement("details"); candidateBox.open = true; const cs = document.createElement("summary"); cs.textContent = `候选 ${labels[candidate.status] || candidate.status}`; candidateBox.append(cs);
        (candidate.patches || []).forEach(patch => { const diff = document.createElement("pre"); diff.textContent = `--- 当前\n${patch.current_content || ""}\n+++ 候选\n${patch.content || ""}`; candidateBox.append(diff); }); card.append(candidateBox);
        if (["candidate_ready"].includes(item.status) && !candidate.stale) {
          const actions = document.createElement("div"); const accept = document.createElement("button"); accept.type = "button"; accept.textContent = "应用候选"; accept.addEventListener("click", () => decideModification(item, "accept")); const reject = document.createElement("button"); reject.type = "button"; reject.textContent = "拒绝候选"; reject.addEventListener("click", () => decideModification(item, "reject")); actions.append(accept, reject); card.append(actions);
        }
      }
      if (!["applied", "partially_applied", "purged"].includes(item.status)) {
        const cleanup = document.createElement("button");
        cleanup.type = "button";
        cleanup.textContent = "预览清除此操作";
        cleanup.addEventListener("click", () => {
          $("#modificationCleanupScope").value = "operation";
          $("#modificationCleanupOperation").hidden = false;
          $("#modificationCleanupOperation").value = item.request_id || item.id;
          previewModificationCleanup("operation", item.request_id || item.id);
        });
        card.append(cleanup);
      }
      host.append(card);
    });
  }
  async function previewModificationCleanup(scope, operationId) {
    const selectedScope = scope || $("#modificationCleanupScope")?.value || "project";
    const selectedOperation = operationId || ($("#modificationCleanupOperation")?.value.trim() || "");
    const host = $("#modificationCleanupPreview");
    try {
      host.textContent = "正在计算清理范围…";
      state.cleanupPreview = await api(`/api/projects/${projectId()}/modifications/cleanup/preview`, {
        method: "POST",
        body: JSON.stringify({ scope: selectedScope, operation_id: selectedOperation, request_id: uid() })
      });
      const preview = state.cleanupPreview;
      host.replaceChildren();
      const summary = document.createElement("p");
      summary.textContent = `可清理 ${preview.counts.operations} 个操作、${preview.counts.candidates} 个候选、${preview.counts.jobs_and_prompts} 个任务/提示；运行中阻止 ${preview.blocked_active.length} 个；已采纳保留 ${preview.retained_applied_count} 个。`;
      const warning = document.createElement("p");
      warning.className = "warning";
      warning.textContent = preview.warning;
      const list = document.createElement("ul");
      preview.eligible.forEach(item => {
        const line = document.createElement("li");
        line.textContent = `${item.request_id} · ${labels[item.status] || item.status} · ${item.target_path || "大纲"}`;
        list.append(line);
      });
      const confirm = document.createElement("button");
      confirm.type = "button";
      confirm.textContent = "确认清理所列内容";
      confirm.disabled = preview.eligible.length === 0;
      confirm.addEventListener("click", confirmModificationCleanup);
      host.append(summary, warning, list, confirm);
    } catch (error) {
      state.cleanupPreview = null;
      host.textContent = showError(error);
    }
  }
  async function confirmModificationCleanup() {
    const preview = state.cleanupPreview;
    if (!preview) return;
    const button = $("#modificationCleanupPreview button");
    if (button) button.disabled = true;
    try {
      const result = await api(`/api/projects/${projectId()}/modifications/cleanup`, {
        method: "POST",
        body: JSON.stringify({
          scope: preview.scope, operation_id: preview.operation_id,
          request_id: uid(), preview_fingerprint: preview.preview_fingerprint, confirm: true
        })
      });
      state.cleanupPreview = null;
      $("#modificationCleanupPreview").textContent = `已清理 ${result.purged_count} 个操作；保留已采纳修订 ${result.retained_applied_count} 个。`;
      await refreshModifications();
    } catch (error) {
      if (button) button.disabled = false;
      $("#modificationCleanupPreview").textContent = `清理未执行：${showError(error)}。请重新预览。`;
      state.cleanupPreview = null;
    }
  }
  async function refreshModifications() { const id = projectId(); if (!id) return; try { state.modifications = await api(`/api/projects/${id}/modifications?limit=30`); renderModifications(); } catch (error) { $("#modificationStatus").textContent = showError(error); } }
  async function createModification() {
    const target = activeTarget(); const instruction = $("#modificationInstruction")?.value.trim();
    if (!instruction) return announce("请填写指定修改指令", true);
    if (!target.path && target.kind === "file") return announce("请先选择目标文件", true);
    const field = target.kind === "outline" ? $("#outlineText") : $("#editor"); const start = field?.selectionStart ?? 0; const end = field?.selectionEnd ?? 0;
    if (start === end) return announce("请先选中需要修改的内容", true);
    try {const id=projectId();await pp.reviewBeforeSend('rewrite',instruction,{...target,content:field.value,selection:field.value.slice(start,end),start,end,baseVersion:target.version},async(preview,request)=>{
      if(id!==projectId()||$('#modificationInstruction').value.trim()!==instruction)throw new Error('论文或修改指令已变化，请重新预览。');
      await api(`/api/projects/${id}/modifications`, { method: "POST", body: JSON.stringify({ request_id:request, action: "rewrite", target_kind: target.kind, target_path: target.path, base_version: target.version, start_offset: start, end_offset: end, instruction,context_version:preview.version,temporary_key:pp.takeTemporaryCredential?.(preview.provider?.profile_id)||'' }) }); $("#modificationInstruction").value = ""; $("#modificationStatus").textContent = "请求已提交，候选将在生成后出现在这里"; await refreshModifications();
    },'授权按此范围生成指定修改候选（费用未知）');}
    catch (error) { $("#modificationStatus").textContent = showError(error); }
  }
  async function decideModification(item, decision) {
    try { await api(`/api/projects/${projectId()}/modifications/${encodeURIComponent(item.request_id || item.id)}/decision`, { method: "POST", body: JSON.stringify({ decision, request_id: uid(), base_file_versions: item.candidate.base_file_versions, allow_warnings: false }) }); await pp.reload(); await refreshModifications(); announce(decision === "accept" ? "指定修改已应用" : "指定修改候选已拒绝"); }
    catch (error) { announce(showError(error), true); }
  }

  async function runCitationReview() {
    const target = activeTarget(); if (!projectId()) return;
    try {
      $("#citationReviewStatus").textContent = "正在生成本次检查预览；不会发送数据…";
      const preview = await api(`/api/projects/${projectId()}/citation-check/preview`, {
        method: "POST",
        body: JSON.stringify({ request_id: uid(), target_path: target.kind === "file" ? target.path : null })
      });
      renderCitationPreflight(preview);
      $("#citationReviewStatus").textContent = "预览已生成。尚未向模型发送内容。";
    } catch (error) { $("#citationReviewStatus").textContent = showError(error); }
  }
  function renderCitationPreflight(preview) {
    const host = $("#citationReviewPreflight"); if (!host) return;
    host.replaceChildren(); host.hidden = false;
    const summary = document.createElement("p");
    summary.textContent = `目标 ${preview.target_path} · 论断 ${preview.claim_count} · 来源 ${preview.source_count} · 模型 ${preview.model.id || "未配置"} · ${preview.model.endpoint_type} ${preview.model.endpoint_host || ""} · 估算 ${preview.estimated_input_tokens} tokens`;
    host.append(summary);
    if ((preview.outbound_preview?.sources || []).length) {
      const scope = document.createElement("fieldset"); scope.className = "citation-source-scope";
      const legend = document.createElement("legend"); legend.textContent = "本次可用来源（默认全选）"; scope.append(legend);
      preview.outbound_preview.sources.forEach(source => {
        const label = document.createElement("label");
        const input = document.createElement("input"); input.type = "checkbox"; input.checked = preview.source_scope.includes(source.source_id);
        const title = source.title || source.cite_key || source.source_id;
        input.addEventListener("change", async () => {
          const selected = [...scope.querySelectorAll("input[type=checkbox]:checked")].map(item => item.value);
          if (!selected.length) { input.checked = true; $("#citationReviewStatus").textContent = "至少保留一个来源；如不外发可选择仅本地检查。"; return; }
          await refreshCitationPreflight(preview, selected);
        });
        input.value = source.source_id;
        label.append(input, document.createTextNode(`${title} · ${source.cite_key || "无引用键"} · ${source.full_text_presence}`));
        scope.append(label);
      });
      host.append(scope);
    }
    const details = document.createElement("details");
    const heading = document.createElement("summary"); heading.textContent = "查看完整模型指令和外发材料";
    const body = document.createElement("pre"); body.className = "citation-outbound-preview";
    body.textContent = `System:\n${preview.system_prompt_preview}\n\nUser:\n${preview.outbound_prompt_preview}`;
    details.append(heading, body); host.append(details);
    const localButton = document.createElement("button"); localButton.type = "button"; localButton.textContent = "仅本地结构检查";
    localButton.addEventListener("click", () => submitCitationReview(preview, false));
    host.append(localButton);
    const consentLabel = document.createElement("label"); consentLabel.className = "citation-consent";
    const consent = document.createElement("input"); consent.type = "checkbox"; consent.checked = false;
    consentLabel.append(consent, document.createTextNode(`我已查看材料，并同意将本次内容发送至 ${preview.model.endpoint_host || "所选提供方"} 进行语义核验`));
    host.append(consentLabel);
    const sendButton = document.createElement("button"); sendButton.type = "button"; sendButton.textContent = "确认外发并核验";
    sendButton.disabled = !preview.model.ready || preview.budget_status !== "within_limit";
    sendButton.addEventListener("click", () => {
      if (!consent.checked) { $("#citationReviewStatus").textContent = "请先勾选本次外发授权。"; return; }
      submitCitationReview(preview, true);
    });
    host.append(sendButton);
  }
  async function refreshCitationPreflight(previous, sourceIds) {
    try {
      $("#citationReviewStatus").textContent = "正在按新的来源范围重算预览；不会发送数据…";
      const preview = await api(`/api/projects/${projectId()}/citation-check/preview`, {
        method: "POST",
        body: JSON.stringify({
          request_id: previous.request_id,
          target_path: previous.target_path === "__all_tex__" ? null : previous.target_path,
          source_ids: sourceIds,
          model: previous.model.id || null
        })
      });
      renderCitationPreflight(preview);
      $("#citationReviewStatus").textContent = "来源范围已更新；尚未发送数据。";
    } catch (error) { $("#citationReviewStatus").textContent = showError(error); }
  }
  async function submitCitationReview(preview, consentToSend) {
    try {
      $("#citationReviewStatus").textContent = consentToSend ? "已授权；正在进行语义核验…" : "正在执行本地结构检查…";
      const result = await api(`/api/projects/${projectId()}/citation-check`, {
        method: "POST",
        body: JSON.stringify({
          request_id: preview.request_id, target_path: preview.target_path === "__all_tex__" ? null : preview.target_path,
          model: preview.model.id || null, source_ids: preview.source_scope,
          consent_to_send: consentToSend, preview_fingerprint: preview.preview_fingerprint,
          temporary_key:consentToSend?(pp.takeTemporaryCredential?.(preview.model.profile_id)||''):''
        })
      });
      renderCitationReport(result);
      await refreshCitationHistory();
      $("#citationReviewStatus").textContent = `${result.semantic_review?.status || "本地检查"} · ${result.stale ? "报告已过期；" : ""}版本 ${result.source_version || "项目快照"} · 结果只读，不会修改正文`;
    } catch (error) { $("#citationReviewStatus").textContent = showError(error); }
  }
  function renderCitationReport(result) {
    const host = $("#citationReviewResult"); if (!host) return;
    host.replaceChildren();
    const summary = document.createElement("p");
    summary.textContent = `论断 ${result.summary.claim_count} · 未引用 ${result.summary.uncited_count} · 弱支撑 ${result.summary.weak_support_count} · 缺失引用 ${result.summary.missing_citation_count} · 语义 ${result.semantic_review?.status || "未核验"}`;
    host.append(summary);
    (result.claims || []).slice(0, 40).forEach(claim => {
      const row = document.createElement("details"); row.className = `citation-claim ${claim.status}`;
      const title = document.createElement("summary"); title.textContent = `${claim.status === "uncited" ? "未引用" : "待人工复核"} · ${claim.text}`; row.append(title);
      (claim.supports || []).forEach(support => {
        const p = document.createElement("p");
        p.textContent = `${support.cite_key} · ${support.support_status || support.status} · 置信度 ${support.confidence} · ${(support.reasons || []).join("；")}`;
        row.append(p);
        if (support.evidence_text) { const evidence = document.createElement("blockquote"); evidence.textContent = `来源片段：${support.evidence_text}`; row.append(evidence); }
        (support.expression_comparison?.source_phrases || []).forEach(phrase => { const quote = document.createElement("blockquote"); quote.textContent = `参考表述：${phrase}`; row.append(quote); });
      });
      (claim.semantic_review?.sources || []).forEach(source => {
        const finding = document.createElement("p");
        finding.textContent = `${source.cite_key || source.source_id} · ${source.role} · ${source.relation}${source.location ? ` · ${source.location}` : ""} · ${source.rationale || ""}`;
        row.append(finding);
        if (source.quoted_text) { const quote = document.createElement("blockquote"); quote.textContent = `核验原文：${source.quoted_text}`; row.append(quote); }
      });
      (claim.semantic_review?.expression_examples || []).forEach(example => {
        const phrase = document.createElement("blockquote"); phrase.textContent = `表达范例（非支撑证据）：${example.text} · ${example.rationale || ""}`; row.append(phrase);
      });
      (claim.evidence_candidates || []).forEach(candidate => {
        const candidateLine = document.createElement("p"); candidateLine.textContent = `未引用论断的来源候选 ${candidate.cite_key} · ${candidate.relation} · ${candidate.rationale || ""}`; row.append(candidateLine);
      });
      const suggestion = claim.semantic_review?.suggestion;
      if (suggestion?.text) {
        const proposed = document.createElement("blockquote"); proposed.textContent = `保意建议（仅供审阅）：${suggestion.text}`; row.append(proposed);
        (suggestion.reasons || []).forEach(reason => { const why = document.createElement("p"); why.textContent = `建议理由：${reason}`; row.append(why); });
      }
      if (suggestion?.meaning_change_text) { const changed = document.createElement("p"); changed.textContent = `含义变化方案（需单独决定）：${suggestion.meaning_change_text}`; row.append(changed); }
      if (suggestion?.text && result.run_id && !result.stale && result.target_path && result.target_path !== "__all_tex__") {
        const handoff = document.createElement("button"); handoff.type = "button"; handoff.textContent = "送入人工修订草稿";
        handoff.title = "建立待审阅草稿，不会直接修改论文正文";
        handoff.addEventListener("click", async () => {
          if (!window.confirm("将此建议放入人工修订草稿？论文正文不会被直接修改。")) return;
          try {
            const response = await api(`/api/projects/${projectId()}/citation-check/runs/${encodeURIComponent(result.run_id)}/claims/${encodeURIComponent(claim.id)}/handoff`, {
              method: "POST", body: JSON.stringify({ request_id: uid(), base_version: result.source_version, confirm_handoff: true })
            });
            $("#citationReviewStatus").textContent = `建议已送入人工修订草稿（${response.target_path}，基线 v${response.base_version}）；请在人工修订流程中审阅和提交`;
          } catch (error) { $("#citationReviewStatus").textContent = showError(error); }
        });
        row.append(handoff);
      }
      host.append(row);
    });
  }
  async function refreshCitationHistory() {
    const select = $("#citationReviewHistory"); if (!select || !projectId()) return;
    try {
      const runs = await api(`/api/projects/${projectId()}/citation-check/runs?limit=20`);
      select.innerHTML = `<option value="">选择历史报告</option>` + runs.map(run =>
        `<option value="${esc(run.run_id)}">${run.stale ? "过期 · " : ""}v${run.source_version || "项目"} · ${esc(prettyTime(run.expires_at))}</option>`
      ).join("");
    } catch (error) { $("#citationReviewStatus").textContent = showError(error); }
  }
  async function openCitationReviewHistory() {
    const runId = $("#citationReviewHistory")?.value;
    if (!runId) return;
    try {
      const report = await api(`/api/projects/${projectId()}/citation-check/runs/${encodeURIComponent(runId)}`);
      renderCitationReport(report);
      $("#citationReviewStatus").textContent = `${report.stale ? "历史报告已过期；" : ""}当前授权状态已重新检查，报告只读`;
    } catch (error) { $("#citationReviewStatus").textContent = showError(error); }
  }

  async function refreshCorpusPanel() {
    const host = $("#corpusIndexList"); const items = pp.data?.corpus || []; if (!host) return; host.replaceChildren();
    if (!items.length) { host.textContent = "暂无语料"; return; }
    items.forEach(item => { const row = document.createElement("div"); row.className = "corpus-index-row"; const text = document.createElement("span"); text.textContent = `${item.title} · ${item.source || "手工"}`; const index = document.createElement("button"); index.type = "button"; index.textContent = "重建索引"; index.addEventListener("click", async () => { try { const result = await api(`/api/projects/${projectId()}/corpus/${item.id}/index`, { method: "POST" }); row.dataset.indexed = "true"; text.textContent = `${item.title} · 当前版本 v${result.source_version} · ${result.chunk_count} 块`; } catch (error) { $("#corpusSearchStatus").textContent = showError(error); } }); const lineage = document.createElement("button"); lineage.type = "button"; lineage.textContent = "查看血缘"; lineage.addEventListener("click", async () => { try { const result = await api(`/api/projects/${projectId()}/corpus/${item.id}/lineage`); $("#corpusSearchResults").textContent = JSON.stringify(result, null, 2); } catch (error) { $("#corpusSearchStatus").textContent = showError(error); } }); row.append(text, index, lineage); host.append(row); });
  }
  async function searchCorpus() {
    const query = $("#corpusSearchQuery")?.value.trim();
    if (!query) return;
    const id = projectId();
    try {
      const result = await api(`/api/projects/${id}/corpus/search?query=${encodeURIComponent(query)}&limit=30`);
      if (projectId() !== id) return;
      $("#corpusSearchStatus").textContent = `命中 ${result.results.length} 段；仅返回已授权当前原文版本`;
      const host = $("#corpusSearchResults");
      host.replaceChildren();
      for (const item of result.results) {
        const page = /^page:(\d+)$/.exec(item.location)?.[1];
        const row = document.createElement("article");
        row.className = "corpus-search-result";
        const position = page ? `第 ${page} 页` : `${item.start_offset}-${item.end_offset}`;
        row.textContent = `${item.title} · v${item.source_version} · ${position}\n${item.content}\n${item.match_reasons.join("；")}`;
        if (item.pdf_import_id) {
          const link = document.createElement("a");
          link.href = `/api/projects/${id}/imports/${item.pdf_import_id}/pdf${page ? `#page=${page}` : ""}`;
          link.target = "_blank";
          link.rel = "noopener noreferrer";
          link.textContent = "打开原件";
          row.append(document.createElement("br"), link);
        }
        host.append(row);
      }
    } catch (error) {
      if (projectId() === id) $("#corpusSearchStatus").textContent = showError(error);
    }
  }

  async function refreshOutline() {
    const id=projectId(), token=++outlineRefreshToken;
    if (!id) return;
    try {
      const [graph, historyItems, resources] = await Promise.all([
        api(`/api/projects/${id}/outline/structured`),
        api(`/api/projects/${id}/outline/versions?limit=20`),
        api(`/api/projects/${id}/outline/resources`)
      ]);
      if (projectId()!==id || token!==outlineRefreshToken) return;
      [state.graph, state.outlineHistory, state.linkableResources]=[graph,historyItems,resources];
      const options = `<option value="">根节点</option>` + (state.graph.nodes || []).map(node => `<option value="${esc(node.id)}">${esc(node.title)}</option>`).join("");
      if ($("#outlineNodeParent")) $("#outlineNodeParent").innerHTML = options;
      if ($("#outlineDetailParent")) $("#outlineDetailParent").innerHTML = options;
      const resourceSelect = $("#outlineResourceSelect");
      if (resourceSelect) resourceSelect.innerHTML = `<option value="">选择资源</option>` + state.linkableResources.map((resource, index) =>
        `<option value="${index}">${esc(resource.resource_type)} · ${esc(resource.label)} · v${resource.version}</option>`
      ).join("");
      const history = $("#outlineHistorySelect");
      if (history) history.innerHTML = `<option value="">选择历史版本</option>` + state.outlineHistory.map(item => `<option value="${item.version}">v${item.version} · ${esc(item.source)} · ${esc(prettyTime(item.created_at))}</option>`).join("");
      renderOutlineOperations();
      const selected = state.graph.nodes.find(node => node.id === state.selectedOutlineNode);
      if (!selected) {
        state.selectedOutlineNode = null;
        $("#outlineNodeDetails").hidden = true;
      }
      document.dispatchEvent(new CustomEvent('paperpilot:outline-refreshed', {detail:{projectId:id, graph, selectedId:state.selectedOutlineNode}}));
      if (selected && !outlineNodeDirty) await selectOutlineNode(selected);
      await refreshOutlineTextDraft();
      return projectId()===id && token===outlineRefreshToken;
    }
    catch (error) {
      if (projectId() === id && token === outlineRefreshToken) {
        const message = showError(error);
        $("#outlineOperationStatus").textContent = message;
        document.dispatchEvent(new CustomEvent('paperpilot:outline-load-failed', {detail:{projectId:id, message}}));
      }
      return false;
    }
  }
  async function reconcileOutlineMutation() {
    const id=projectId(),pending=pendingOutlineMutations.get(id);
    if(pending?.running){$("#outlineOperationStatus").textContent='上一次操作仍在进行，请稍候再刷新。';return;}
    if(await refreshOutline() && projectId()===id){if(pendingOutlineMutations.get(id)===pending)pendingOutlineMutations.delete(id);$("#outlineOperationStatus").textContent='已刷新大纲，请核对上次操作结果后继续。';}
  }
  async function outlineMutation(id, path, intent, createNode=false) {
    const signature=JSON.stringify({path,intent});let pending=pendingOutlineMutations.get(id);
    if(pending && pending.signature!==signature)throw new Error('上次操作结果尚未确认；请先刷新树并核对，再进行其他操作。');
    if(pending?.running)throw new Error('上次操作仍在进行，请稍候。');
    if(!pending){const body={...intent,request_id:uid()};if(createNode)body.node={...intent.node,id:uid()};pending={signature,path,body,running:false};pendingOutlineMutations.set(id,pending);}
    pending.running=true;
    try {const result=await api(path,{method:'POST',body:JSON.stringify(pending.body)});if(pendingOutlineMutations.get(id)===pending)pendingOutlineMutations.delete(id);return result;}
    catch(error){throw new Error(`${showError(error)}；结果未确认，可重试相同操作，或刷新树后核对。`);}
    finally {pending.running=false;}
  }
  pp.refreshStructuredOutline=refreshOutline;
  pp.structuredOutlineSnapshot = () => ({projectId:state.projectId, graph:state.graph, selectedId:state.selectedOutlineNode});
  pp.updateOutlineFromCanvas = async (nodeId, node) => {
    const id = projectId();
    if (!state.graph || !state.graph.nodes.some(item => item.id === nodeId)) return;
    await outlineMutation(id, `/api/projects/${id}/outline/nodes`, {
      command:'update', base_version:state.graph.version, node_id:nodeId, node
    });
    if (projectId() === id) await refreshOutline();
  };
  function renderOutlineOperations() {
    const tree = $("#outlineTree"); if (!tree || !state.graph) return;
    const nodes = state.graph.nodes || []; tree.replaceChildren();
    if (!nodes.length) { tree.textContent = "暂无结构化节点；可先预览迁移。"; return; }
    const byParent = {}; nodes.forEach(node => (byParent[node.parent_id || ""] ||= []).push(node)); Object.values(byParent).forEach(items => items.sort((a,b) => (a.position || 0) - (b.position || 0)));
    const walk = (parent, depth = 0) => (byParent[parent] || []).map(node => {
      const row = document.createElement("div"); row.className = "outline-tree-node outline-operation-node"; row.style.setProperty("--outline-depth", depth);
      const select = document.createElement("button"); select.type = "button"; select.className = "outline-node-select"; select.dataset.nodeId=node.id; select.textContent = node.title; select.setAttribute("aria-pressed", String(node.id === state.selectedOutlineNode)); select.addEventListener("click", () => {
        if (node.id !== state.selectedOutlineNode) selectOutlineNode(node);
        document.dispatchEvent(new CustomEvent('paperpilot:outline-node-selected', {detail:{nodeId:node.id}}));
      });
      const remove = document.createElement("button"); remove.type = "button"; remove.textContent = "删除"; remove.setAttribute("aria-label",`删除节点 ${node.title}`); remove.addEventListener("click", () => deleteOutlineNode(node));
      row.append(select, remove); tree.append(row); walk(node.id, depth + 1); return "";
    }).join(""); walk("");
  }
  async function createOutlineNode() {
    if (!state.graph) return;
    const title = $("#outlineNodeTitle")?.value.trim(); if (!title) return;
    const parentId = $("#outlineNodeParent")?.value || null;
    const siblings = (state.graph.nodes || []).filter(node => (node.parent_id || null) === parentId);
    const id=projectId(),purpose=$("#outlineNodePurpose")?.value.trim() || "";
    const node = { title, purpose, parent_id: parentId, position: siblings.length, status: "todo" };
    try {
      await outlineMutation(id,`/api/projects/${id}/outline/nodes`,{command:'create',base_version:state.graph.version,node},true);
      if(projectId()!==id)return;
      if($("#outlineNodeTitle").value.trim()===title)$("#outlineNodeTitle").value = ""; if($("#outlineNodePurpose").value.trim()===purpose)$("#outlineNodePurpose").value = "";
      $("#outlineOperationStatus").textContent = "节点已保存";
      await refreshOutline();
    } catch (error) { if(projectId()===id)$("#outlineOperationStatus").textContent = showError(error); }
  }
  function setOutlineDetailStatus(value) {
    const select=$("#outlineDetailStatus"),status=value || "todo";
    select.querySelectorAll('option[data-legacy-status]').forEach(option=>option.remove());
    if(![...select.options].some(option=>option.value===status)){
      const option=document.createElement('option');option.value=status;option.textContent=`历史状态：${status}`;option.dataset.legacyStatus='true';select.append(option);
    }
    select.value=status;
  }
  async function selectOutlineNode(node) {
    const id=projectId(), token=++outlineSelectionToken;
    state.selectedOutlineNode = node.id;
    document.querySelectorAll("#outlineTree .outline-node-select").forEach(button=>button.setAttribute("aria-pressed",String(button.dataset.nodeId===node.id)));
    outlineNodeDirty=false;
    $("#outlineNodeDetails").hidden = false;
    $("#outlineDetailTitle").value = node.title || "";
    $("#outlineDetailPurpose").value = node.purpose || "";
    $("#outlineDetailScope").value = node.scope || "";
    setOutlineDetailStatus(node.status);
    $("#outlineDetailParent").value = node.parent_id || "";
    $("#outlineDetailPosition").value = Number(node.position || 0);
    $("#outlineDetailAnchor").value = node.source_anchor || "";
    renderOutlineLinks(node);
    state.outlineDraft = null;
    $("#outlineNodeDraftRestore").hidden = true;
    $("#outlineNodeDraftDiscard").hidden = true;
    $("#outlineNodeDraftSelect").hidden = true;
    $("#outlineNodeDraftMore").hidden = true;
    $("#outlineNodeDraftStatus").textContent = "";
    try {
      await loadOutlineRecovery("node",true);
      if(projectId()!==id || token!==outlineSelectionToken || state.selectedOutlineNode!==node.id) return;
    } catch (error) {
      if(projectId()!==id || token!==outlineSelectionToken || state.selectedOutlineNode!==node.id) return;
      $("#outlineNodeDraftStatus").textContent = showError(error);
    }
  }
  function renderOutlineLinks(node) {
    const host = $("#outlineNodeLinks");
    if (!host) return;
    host.replaceChildren();
    const links = node?.links || [];
    if (!links.length) { host.textContent = "此节点没有关联资源"; return; }
    links.forEach(link => {
      const row = document.createElement("div");
      row.className = `outline-link-row${link.status === "active" ? "" : " warning"}`;
      const label = document.createElement("span");
      const stateLabel = link.status === "active" ? "有效" : `${link.status} · ${link.invalid_reason || "来源已变化"}`;
      label.textContent = `${link.resource_type} · ${link.label || link.resource_id} · 来源 v${link.source_version} · ${stateLabel}`;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "解除";
      remove.addEventListener("click", () => unlinkOutlineResource(link));
      row.append(label, remove);
      host.append(row);
    });
  }
  async function linkOutlineResource() {
    const node = state.graph?.nodes.find(item => item.id === state.selectedOutlineNode);
    const selection = $("#outlineResourceSelect")?.value;
    if (selection === "" || selection == null) return;
    const resource = state.linkableResources[Number(selection)];
    if (!node || !resource) return;
    const id=projectId();
    try {
      await outlineMutation(id,`/api/projects/${id}/outline/nodes`,{
          command: "link", base_version: state.graph.version,
          node_id: node.id,
          link: { resource_type: resource.resource_type, resource_id: resource.resource_id, source_version: resource.version }
      });
      if(projectId()!==id)return;
      $("#outlineOperationStatus").textContent = "资源已关联";
      await refreshOutline();
    } catch (error) { if(projectId()===id)$("#outlineOperationStatus").textContent = showError(error); }
  }
  async function unlinkOutlineResource(link) {
    const node = state.graph?.nodes.find(item => item.id === state.selectedOutlineNode);
    if (!node) return;
    const id=projectId();
    try {
      await outlineMutation(id,`/api/projects/${id}/outline/nodes`,{
          command: "unlink", base_version: state.graph.version,
          node_id: node.id,
          link: { resource_type: link.resource_type, resource_id: link.resource_id }
      });
      if(projectId()!==id)return;
      $("#outlineOperationStatus").textContent = "资源链接已解除";
      await refreshOutline();
    } catch (error) { if(projectId()===id)$("#outlineOperationStatus").textContent = showError(error); }
  }
  function outlineDetailValue() {
    return {
      title: $("#outlineDetailTitle").value.trim(),
      purpose: $("#outlineDetailPurpose").value.trim(),
      scope: $("#outlineDetailScope").value.trim(),
      status: $("#outlineDetailStatus").value,
      parent_id: $("#outlineDetailParent").value || null,
      position: Math.max(0, Number.parseInt($("#outlineDetailPosition").value || "0", 10)),
      source_anchor: $("#outlineDetailAnchor").value.trim()
    };
  }
  function outlineQueue(id, draftKey) {
    const key = JSON.stringify([id, draftKey]);
    if (!outlineDraftQueues.has(key)) outlineDraftQueues.set(key, { timer: null, pending: null, chain: Promise.resolve(), seq: 0 });
    return outlineDraftQueues.get(key);
  }
  function outlineStorageKey(id, logicalKey, rotate=false) {
    const key=JSON.stringify([id,logicalKey]);
    if(rotate || !outlinePageKeys.has(key))
      outlinePageKeys.set(key,`tab:${outlinePageId}:${uid()}${logicalKey==="editor"?":editor":":node"}`);
    return outlinePageKeys.get(key);
  }
  async function loadOutlineRecovery(kind, reset) {
    const id=projectId(), nodeId=kind==="node"?state.selectedOutlineNode:"";
    if(!id || (kind==="node"&&!nodeId))return;
    const slot=outlineRecovery[kind], request=++slot.request;
    if(reset){slot.items=[];slot.hasMore=false;}
    const offset=slot.items.length;
    const params=new URLSearchParams({kind,offset:String(offset),limit:"30"});
    if(nodeId)params.set("node_id",nodeId);
    const result=await api(`/api/projects/${id}/outline/draft-recovery?${params}`);
    if(projectId()!==id || request!==slot.request || (kind==="node"&&state.selectedOutlineNode!==nodeId))return;
    slot.items=reset?result.drafts:slot.items.concat(result.drafts);slot.hasMore=result.has_more;
    const select=$(kind==="node"?"#outlineNodeDraftSelect":"#outlineTextDraftSelect");
    const more=$(kind==="node"?"#outlineNodeDraftMore":"#outlineTextDraftMore");
    if(!select||!more)return;
    const previous=select.value;select.replaceChildren();
    const placeholder=document.createElement("option");placeholder.value="";placeholder.textContent="请选择草稿";select.append(placeholder);
    for(const item of slot.items){
      const option=document.createElement("option");option.value=item.draft_key;
      option.textContent=`${prettyTime(item.updated_at)} · 基线 v${item.base_version} · ${item.preview || "空内容"}`;
      select.append(option);
    }
    if(slot.items.some(item=>item.draft_key===previous))select.value=previous;
    select.hidden=!slot.items.length;more.hidden=!slot.hasMore;
    if(kind==="node"){
      $("#outlineNodeDraftRestore").hidden=!slot.items.length;
      $("#outlineNodeDraftDiscard").hidden=!slot.items.length;
      if(slot.items.length&&!outlineNodeDirty)$("#outlineNodeDraftStatus").textContent="发现可恢复草稿；请选择后查看。";
    } else $("#outlineTextRecovery").hidden=!slot.items.length;
  }
  function selectedOutlineRecovery(kind) {
    return $(kind==="node"?"#outlineNodeDraftSelect":"#outlineTextDraftSelect")?.value || "";
  }
  function selectedOutlineRecoveryItem(kind) {
    const key=selectedOutlineRecovery(kind);
    return outlineRecovery[kind].items.find(item=>item.draft_key===key) || null;
  }
  async function discardOutlineRecovery(kind) {
    const id=projectId(),nodeId=kind==="node"?state.selectedOutlineNode:"",item=selectedOutlineRecoveryItem(kind),key=item?.draft_key;
    const status=$(kind==="node"?"#outlineNodeDraftStatus":"#outlineStructuredStatus");
    if(!key){status.textContent="请先选择要删除的草稿。";return;}
    if(!window.confirm("删除所选草稿？正式大纲和其他草稿不会被删除；若另一页面仍在编辑该草稿，应先确认其内容。"))return;
    const logical=kind==="node"?`node:${nodeId}`:"editor",own=outlineStorageKey(id,logical),queue=outlineQueue(id,logical);
    const path=`/api/projects/${id}/outline/drafts/${encodeURIComponent(key)}?if_updated_at=${encodeURIComponent(item.updated_at)}`;
    try {
      if(key===own) {
        // Preserve pending edits before attempting a conditional deletion.  A
        // stale selection must never discard an unsaved write from this page.
        await flushOutlineDraft(queue);
        await enqueueOutlineWork(queue,()=>api(path,{method:"DELETE"}));
      } else await api(path,{method:"DELETE"});
      if(projectId()!==id||(kind==="node"&&state.selectedOutlineNode!==nodeId))return;
      status.textContent="所选草稿已删除；当前页面输入仍保留。";
      await loadOutlineRecovery(kind,true);
    } catch(error) {
      if(projectId()===id&&(kind!=="node"||state.selectedOutlineNode===nodeId))status.textContent=`删除未完成：${showError(error)}`;
    }
  }
  function enqueueOutlineWork(queue, work) {
    const running = queue.chain.catch(() => {}).then(work);
    queue.chain = running;
    return running;
  }
  function flushOutlineDraft(queue) {
    if (queue.timer) clearTimeout(queue.timer);
    queue.timer = null;
    if (queue.pending) {
      const pending = queue.pending;
      queue.pending = null;
      return enqueueOutlineWork(queue, pending);
    }
    return queue.chain;
  }
  function scheduleOutlineWrite(id, draftKey, baseVersion, content, delay, report) {
    const queue = outlineQueue(id, draftKey);
    const storageKey=outlineStorageKey(id,draftKey);
    const seq = ++queue.seq;
    if (queue.timer) clearTimeout(queue.timer);
    queue.pending = async () => {
      try {
        const saved = await api(`/api/projects/${id}/outline/drafts/${encodeURIComponent(storageKey)}`, {
          method: "PUT", body: JSON.stringify({ draft_key: storageKey, base_version: baseVersion, content })
        });
        report?.(null, saved, seq);
        return saved;
      } catch (error) {
        report?.(error, null, seq);
        throw error;
      }
    };
    queue.timer = setTimeout(() => { flushOutlineDraft(queue).catch(() => {}); }, delay);
    return queue;
  }
  function scheduleOutlineNodeDraft() {
    if (!state.graph || !state.selectedOutlineNode || state.projectId!==projectId()) return;
    outlineNodeDirty=true;
    const id=projectId(), nodeId=state.selectedOutlineNode, version=state.outlineDraft?.base_version ?? state.graph.version;
    const payload={node_id:nodeId,node:outlineDetailValue()};
    $("#outlineNodeDraftStatus").textContent = "草稿待保存";
    const queue=scheduleOutlineWrite(id,`node:${nodeId}`,version,payload,500,(error,saved,seq) => {
      if (projectId()!==id || state.selectedOutlineNode!==nodeId || queue.seq!==seq) return;
      if (!error) {
        state.outlineDraft = saved;
        $("#outlineNodeDraftStatus").textContent = `草稿已保存 · 基线 v${version}`;
        loadOutlineRecovery("node",true).catch(()=>{});
      } else $("#outlineNodeDraftStatus").textContent = `保存失败，当前输入仍保留：${showError(error)}`;
    });
  }
  async function restoreOutlineNodeDraft() {
    const id=projectId(),nodeId=state.selectedOutlineNode,item=selectedOutlineRecoveryItem("node"),key=item?.draft_key;
    if(!id||!nodeId||!key){$("#outlineNodeDraftStatus").textContent="请先选择要恢复的节点草稿。";return;}
    const logical=`node:${nodeId}`,queue=outlineQueue(id,logical);
    try {
      if(outlineNodeDirty){
        scheduleOutlineNodeDraft();const before=queue.seq;await flushOutlineDraft(queue);
        if(projectId()!==id||state.selectedOutlineNode!==nodeId)return;
        if(queue.seq!==before){$("#outlineNodeDraftStatus").textContent="输入仍在变化；已保留当前内容，请停止编辑后重新选择恢复。";return;}
      }
      const seq=queue.seq;
      const draft=await api(`/api/projects/${id}/outline/drafts/${encodeURIComponent(key)}`);
      if(projectId()!==id||state.selectedOutlineNode!==nodeId||queue.seq!==seq)return;
      if(draft.updated_at!==item.updated_at){await loadOutlineRecovery("node",true);throw new Error("草稿已被其他页面更新；请重新选择。");}
      if(!draft?.content?.node || (draft.content.node_id && draft.content.node_id!==nodeId) ||
         (!draft.content.node_id && key!==`node:${nodeId}`))throw new Error("所选草稿与当前节点不匹配。");
      outlineStorageKey(id,logical,true);
      state.outlineDraft=draft;outlineNodeDirty=true;
      const node=draft.content.node;
      $("#outlineDetailTitle").value=node.title || "";$("#outlineDetailPurpose").value=node.purpose || "";
      $("#outlineDetailScope").value=node.scope || "";setOutlineDetailStatus(node.status);
      $("#outlineDetailParent").value=node.parent_id || "";$("#outlineDetailPosition").value=Number(node.position || 0);
      $("#outlineDetailAnchor").value=node.source_anchor || "";
      $("#outlineNodeDraftStatus").textContent=`已载入所选草稿（基线 v${draft.base_version}）；原草稿仍保留，保存时会检查版本。`;
    } catch(error){if(projectId()===id&&state.selectedOutlineNode===nodeId)$("#outlineNodeDraftStatus").textContent=`恢复未完成，当前输入保留：${showError(error)}`;}
  }
  async function discardOutlineNodeDraft() { return discardOutlineRecovery("node"); }
  async function saveOutlineNode() {
    if(state.projectId!==projectId())return;
    const node = state.graph?.nodes.find(item => item.id === state.selectedOutlineNode);
    if (!node) return;
    const id=projectId(), nodeId=node.id, logical=`node:${nodeId}`, queue=outlineQueue(id,logical), storageKey=outlineStorageKey(id,logical), seq=queue.seq, content=outlineDetailValue();
    const baseVersion = state.outlineDraft?.base_version ?? state.graph.version;
    try {
      await flushOutlineDraft(queue);
      const saved=await outlineMutation(id,`/api/projects/${id}/outline/nodes`,{command:"update",base_version:baseVersion,node_id:nodeId,node:content});
      await enqueueOutlineWork(queue,()=>queue.seq===seq
        ? api(`/api/projects/${id}/outline/drafts/${encodeURIComponent(storageKey)}`, { method: "DELETE" })
        : Promise.resolve());
      if(projectId()!==id || state.selectedOutlineNode!==nodeId) return;
      $("#outlineOperationStatus").textContent = "节点已保存为新版本";
      if(queue.seq!==seq) {
        state.graph=saved;
        $("#outlineNodeDraftStatus").textContent = "节点已保存；保存期间的新输入仍是草稿，请核对后继续。";
        return;
      }
      state.outlineDraft = null;
      outlineNodeDirty=false;
      await refreshOutline();
    } catch (error) {
      if(projectId()!==id || state.selectedOutlineNode!==nodeId) return;
      $("#outlineNodeDraftStatus").textContent = `保存未完成，当前输入仍保留；请检查草稿状态：${showError(error)}`;
      $("#outlineOperationStatus").textContent = showError(error);
    }
  }
  async function deleteOutlineNode(node) { if (!window.confirm(`删除“${node.title}”及其子节点？`)) return; const id=projectId();try { await outlineMutation(id,`/api/projects/${id}/outline/nodes`,{command:"delete",base_version:state.graph.version,node_id:node.id});if(projectId()===id)await refreshOutline(); } catch (error) { if(projectId()===id)$("#outlineOperationStatus").textContent = showError(error); } }
  async function viewOutlineHistory() {
    const version = Number($("#outlineHistorySelect")?.value || 0);
    if (!version) return;
    try {
      const historic = await api(`/api/projects/${projectId()}/outline/structured?version=${version}`);
      $("#outlineHistoryDetails").hidden = false;
      $("#outlineHistoryDetails").textContent = JSON.stringify({ version: historic.version, source: historic.source, nodes: historic.nodes }, null, 2);
      $("#outlineHistoryRestore").disabled = version === state.graph.version;
    } catch (error) { $("#outlineOperationStatus").textContent = showError(error); }
  }
  async function restoreOutlineHistory() {
    const version = Number($("#outlineHistorySelect")?.value || 0);
    if (!version || !state.graph || !window.confirm(`将 v${version} 恢复为新的当前版本？原历史和当前版本都会保留。`)) return;
    const id=projectId();
    try {
      await outlineMutation(id,`/api/projects/${id}/outline/versions/${version}/restore`,{base_version:state.graph.version});
      if(projectId()!==id)return;
      $("#outlineOperationStatus").textContent = `已从 v${version} 创建新的大纲版本`;
      await refreshOutline();
    } catch (error) { if(projectId()===id)$("#outlineOperationStatus").textContent = showError(error); }
  }
  function scheduleOutlineTextDraft() {
    const id=projectId();if (!id) return;
    const content=$("#outlineText")?.value || "";
    const version=outlineTextDraft?.projectId===id && outlineTextDraft.restored
      ? outlineTextDraft.draft.base_version : (pp.data?.outline_version || 0);
    const queue=scheduleOutlineWrite(id,"editor",version,{text:content},600,(error,saved,seq)=>{
      if(projectId()!==id || queue.seq!==seq || $("#outlineText")?.value!==content) return;
      if(error) $("#outlineStructuredStatus").textContent=`大纲草稿保存失败，页面内容仍保留：${showError(error)}`;
      else {
        outlineTextDraft={projectId:id,draft:saved,restored:true};
        $("#outlineStructuredStatus").textContent="未保存大纲草稿已自动保存在本地项目中";
        loadOutlineRecovery("editor",true).catch(()=>{});
      }
    });
  }
  async function saveOutlineText(event) {
    event.preventDefault();event.stopImmediatePropagation();
    const id=projectId();if(!id)return;
    const content=$("#outlineText")?.value || "", queue=outlineQueue(id,"editor"), storageKey=outlineStorageKey(id,"editor"), seq=queue.seq;
    const version=outlineTextDraft?.projectId===id && outlineTextDraft.restored
      ? outlineTextDraft.draft.base_version : (pp.data?.outline_version || 0);
    const button=$("#saveOutline");button.disabled=true;
    try {
      await flushOutlineDraft(queue);
      const saved=await api(`/api/projects/${id}/outline`,{method:"PUT",body:JSON.stringify({content,base_version:version})});
      await enqueueOutlineWork(queue,()=>queue.seq===seq
        ? api(`/api/projects/${id}/outline/drafts/${encodeURIComponent(storageKey)}`,{method:"DELETE"})
        : Promise.resolve());
      if(projectId()!==id)return;
      if(pp.data?.project?.id===id){pp.data.outline=content;if(saved.version!=null)pp.data.outline_version=saved.version;}
      if(queue.seq!==seq){$("#outlineStructuredStatus").textContent="大纲已保存；保存期间的新输入仍是草稿，请核对后继续。";return;}
      outlineTextDraft=null;
      $("#outlineStructuredStatus").textContent="大纲已保存";
      await loadOutlineRecovery("editor",true);
    } catch(error) {
      if(projectId()===id)$("#outlineStructuredStatus").textContent=`保存未完成，当前输入仍保留；请检查草稿状态：${showError(error)}`;
    } finally {button.disabled=false;}
  }
  pp.keepOutlineTextDraft=async()=>{
    const id=projectId();if(!id)return false;
    const queue=outlineQueue(id,"editor");
    scheduleOutlineTextDraft();
    await flushOutlineDraft(queue);
    return true;
  };
  pp.flushOutlineDrafts = async () => {
    const id = projectId();
    for (const [key, queue] of outlineDraftQueues) {
      if (JSON.parse(key)[0] !== id) continue;
      const sequence = queue.seq;
      await flushOutlineDraft(queue);
      if (queue.seq !== sequence || projectId() !== id) throw new Error('草稿仍在编辑');
    }
  };
  async function saveOutlineDraftExplicitly(event) {
    event.preventDefault();event.stopImmediatePropagation();
    const id=projectId();
    try {
      await pp.keepOutlineTextDraft();
      if(projectId()===id)$("#outlineStructuredStatus").textContent="大纲草稿已保存，尚未写入正式大纲。";
    } catch(error) {
      if(projectId()===id)$("#outlineStructuredStatus").textContent=`草稿保存失败，当前输入仍保留：${showError(error)}`;
    }
  }
  async function refreshOutlineTextDraft() {
    const id=projectId(), token=++outlineTextDraftToken;if(!id)return;
    try {
      await loadOutlineRecovery("editor",true);
      if(projectId()!==id || token!==outlineTextDraftToken)return;
    } catch(error) {
      if(projectId()===id && token===outlineTextDraftToken)$("#outlineStructuredStatus").textContent=showError(error);
    }
  }
  async function restoreOutlineTextDraft() {
    const id=projectId(),item=selectedOutlineRecoveryItem("editor"),key=item?.draft_key;
    if(!id||!key){$("#outlineStructuredStatus").textContent="请先选择要恢复的大纲草稿。";return;}
    const queue=outlineQueue(id,"editor");
    try {
      if(($("#outlineText")?.value || "")!==(pp.data?.outline || "")) {
        scheduleOutlineTextDraft();const before=queue.seq;await flushOutlineDraft(queue);
        if(projectId()!==id)return;
        if(queue.seq!==before){$("#outlineStructuredStatus").textContent="输入仍在变化；已保留当前内容，请停止编辑后重新选择恢复。";return;}
      }
      const seq=queue.seq;
      const draft=await api(`/api/projects/${id}/outline/drafts/${encodeURIComponent(key)}`);
      if(projectId()!==id || queue.seq!==seq)return;
      if(draft.updated_at!==item.updated_at){await loadOutlineRecovery("editor",true);throw new Error("草稿已被其他页面更新；请重新选择。");}
      if(typeof draft?.content?.text!=="string")throw new Error("所选草稿不包含大纲文本。");
      outlineStorageKey(id,"editor",true);
      outlineTextDraft={projectId:id,draft,restored:true};
      $("#outlineText").value=draft.content.text;
      $("#outlineStructuredStatus").textContent=`已载入所选草稿（基线 v${draft.base_version}）；原草稿仍保留，保存时会检查版本。`;
    } catch(error) {
      if(projectId()===id)$("#outlineStructuredStatus").textContent=`恢复未完成，当前输入保留：${showError(error)}`;
    }
  }

  async function refreshAll(force = false) {
    const id = projectId(); if (!id || (!force && state.projectId === id && !pp.data)) return;
    if(state.projectId!==id){++buildPreviewToken;++restorePreviewToken;++outlineRefreshToken;++outlineSelectionToken;state.graph=null;state.selectedOutlineNode=null;state.outlineDraft=null;outlineNodeDirty=false;outlineTextDraft=null;if($("#outlineNodeDetails"))$("#outlineNodeDetails").hidden=true;if($("#outlineTextRecovery"))$("#outlineTextRecovery").hidden=true;$('#buildRestorePanel').hidden=true;buildPreviewMode='live';$('#pdfFrame').removeAttribute('src');$('#compileLog').textContent='正在准备预览…';}
    const changedProject = state.projectId !== id;
    state.projectId = id; injectPanels(); ensureHandoffButton();
    document.dispatchEvent(new CustomEvent('paperpilot:panels-ready'));
    await Promise.all([loadSettings(), refreshBuilds(), refreshModifications(), refreshCorpusPanel(), refreshOutline(), refreshCitationHistory(), refreshFormatHistory(), refreshEditorDraft()]);
    if (changedProject && id === projectId()) {
      const latest = state.builds.find(job => job.status === 'succeeded' && job.current && !job.cache_pruned)
        || state.builds.find(job => job.status === 'succeeded' && !job.cache_pruned);
      if (latest && pp.openBuildReader) pp.openBuildReader(id, latest.id, {background:true});
      scheduleAutomaticPreview(id);
    }
  }
  $('#autoPreviewBuild')?.addEventListener('change',async()=>{if($('#autoPreviewBuild').checked){buildPreviewMode='live';try{const id=projectId();await flushVisibleDraft(id);automaticPreview(id);}catch(error){announce(showError(error),true);}}else announce('已关闭新增自动预览；原排队任务仍保留，可在构建历史明确取消。');});
  $('#buildDraftSource')?.addEventListener('change',()=>scheduleAutomaticPreview(projectId()));
  document.addEventListener("click", event => { if (event.target.closest("#refreshBuildHistory")) refreshBuilds(); });
  document.addEventListener('paperpilot:view-changed', event => {
    if (event.detail?.view === 'history') refreshBuilds().catch(error => announce(showError(error), true));
  });
  document.addEventListener("paperpilot:project-changed",()=>refreshAll(true).catch(error=>announce(showError(error),true)));
  $("#compile")?.addEventListener("click", () => setTimeout(() => { refreshBuilds(); }, 500));
  setInterval(() => { if (projectId() !== state.projectId) refreshAll(true).catch(error => announce(showError(error), true)); }, 600);
  setTimeout(() => refreshAll(true).catch(error => announce(showError(error), true)), 0);
})();
