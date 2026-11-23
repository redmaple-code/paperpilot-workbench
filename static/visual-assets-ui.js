(() => {
  const pp = window.paperpilot;
  if (!pp || !document.querySelector("#visualImportForm")) return;
  const $ = selector => document.querySelector(selector);
  const esc = text => String(text ?? "").replace(/[&<>"']/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]));
  const uid = () => crypto.randomUUID();
  const state = { project: "", observed: null, assets: [], assetId: "", versionId: "", busy: false };
  const id = () => pp.data?.project?.id || "";
  const endpoint = tail => `/api/projects/${id()}/drawing/${tail}`;
  function status(message, error = false) { $("#visualAssetStatus").textContent = message; $("#visualAssetStatus").dataset.error = error; }
  function message(error) { try { const body = JSON.parse(error.message); return body.detail?.message || body.detail || error.message; } catch { return error.message; } }
  async function perform(action, pending) {
    if (state.busy) return;
    state.busy = true; $("#visualImportForm").setAttribute("aria-busy", "true");
    $("#visualImportForm").querySelectorAll("button,input").forEach(el => el.disabled = true);
    $("#visualReplacementOptions")?.querySelectorAll("button,input").forEach(el => el.disabled = true);
    status(pending);
    try { await action(); } catch (error) { status(message(error), true); }
    finally { state.busy = false; $("#visualImportForm").setAttribute("aria-busy", "false"); $("#visualImportForm").querySelectorAll("button,input").forEach(el => el.disabled = false); $("#importVisualAsset").disabled = !$("#visualAssetFile").files.length; $("#visualReplacementOptions")?.querySelectorAll("button,input").forEach(el => el.disabled = false); }
  }
  async function content(file) {
    if (!file || !file.size || file.size > 20 * 1024 * 1024) throw new Error("请选择不超过 20 MB 的文件。");
    return new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(String(reader.result).split(",")[1]); reader.onerror = () => reject(new Error("文件读取失败，请重新选择。")); reader.readAsDataURL(file); });
  }
  async function refresh() {
    const expected = id(); if (!expected) return;
    const response = await pp.api(endpoint(`assets?include_archived=${$("#visualShowArchived").checked}`));
    if (id() !== expected) return;
    state.assets = response.assets;
    if (!state.assets.some(asset => asset.id === state.assetId)) { state.assetId = state.assets[0]?.id || ""; state.versionId = ""; }
    render();
  }
  pp.openVisualAsset = async (assetId, versionId, options = {}) => {
    const origin = id();
    if (options.archived) $("#visualShowArchived").checked = true;
    await refresh();
    if (origin !== id()) return;
    if (state.assets.some(asset => asset.id === assetId)) { state.assetId = assetId; state.versionId = versionId || ""; render(); }
    status(options.message || "图像已保存到当前论文，可核对所选版本后插入正文。");
    pp.openWorkspace?.("visual/library");
  };
  function render() {
    document.querySelector(".visual-workspace").dataset.empty = String(!state.assets.length);
    $("#visualAssetList").innerHTML = state.assets.map(asset => {
      const v = asset.current_version;
      return `<button class="visual-asset-item" type="button" data-asset="${esc(asset.id)}" aria-current="${asset.id === state.assetId}"><img src="${esc(v.preview_url)}" alt="${esc(asset.title)}预览" loading="lazy"><span><strong>${esc(asset.title)}</strong><small>v${v.version_no} · ${v.width} × ${v.height}${asset.status === "archived" ? " · 已归档" : ""}</small></span></button>`;
    }).join("");
    $("#visualAssetList").querySelectorAll("[data-asset]").forEach(button => button.onclick = () => { state.assetId = button.dataset.asset; state.versionId = ""; render(); });
    const asset = state.assets.find(item => item.id === state.assetId);
    if (!asset) { $("#visualAssetDetail").innerHTML = '<div class="visual-empty"><h3>为论文整理第一张图</h3><p>使用上方“导入图像”添加本地文件，或选择“新建绘图”生成可审阅的候选。</p></div>'; return; }
    const v = asset.versions.find(version => version.id === state.versionId) || asset.current_version;
    state.versionId = v.id;
    const origin = v.result_source ? "由结果数据生成" : v.origin_type === "copy" ? "从其他论文显式复制" : v.origin_type === "generated" ? "由绘图任务生成" : "本地文件导入";
    $("#visualAssetDetail").innerHTML = `<div class="visual-detail-head"><h3>${esc(asset.title)}</h3><select id="visualVersionSelect" aria-label="图像版本">${asset.versions.map(version => `<option value="${esc(version.id)}" ${version.id === v.id ? "selected" : ""}>v${version.version_no}${version.id === asset.current_version_id ? " · 当前" : " · 历史"}</option>`).join("")}</select></div><div class="visual-preview"><img id="visualMainPreview" src="${esc(v.preview_url)}" alt="${esc(asset.title)}，第 ${v.version_no} 版"></div><details class="visual-metadata"><summary>来源与文件信息</summary><dl class="visual-asset-meta"><dt>来源</dt><dd>${origin} · ${esc(v.filename)}${v.page_no ? ` · 第 ${v.page_no} / ${v.page_count} 页` : ""}</dd><dt>文件信息</dt><dd>${esc(v.mime_type)} · ${v.width} × ${v.height} · ${(v.byte_size / 1024).toFixed(1)} KB</dd><dt>保存时间</dt><dd>${new Date(v.created_at).toLocaleString()}</dd><dt>来源说明</dt><dd>${esc(v.result_source ? "数据 v" + v.result_source.version_no + " · " + v.result_source.point_count + " 个点" : v.origin_ref || "保留受管原件，移动原文件不影响引用")}</dd></dl></details><p class="visual-history-note">${v.id !== asset.current_version_id ? "正在查看历史版本。插入正文只引用此版本，不改变当前图像。" : "替换图像会保留旧版本；已插入的正文和历史构建不会自动切换。"}</p><label class="visual-caption">图注（可选）<input id="visualInsertCaption" maxlength="1000" placeholder="为正文中的图像添加说明"></label><div class="visual-asset-actions"><button id="insertVisualAsset" type="button" class="primary" ${asset.status === "archived" ? "disabled" : ""}>插入当前正文</button><button id="reviewVisualAsset" type="button">检查此图像</button><button id="replaceVisualAsset" type="button" ${asset.status === "archived" ? "disabled" : ""}>替换并保留旧版</button><button id="archiveVisualAsset" type="button">${asset.status === "archived" ? "恢复图像" : "归档图像"}</button><a href="${esc(v.download_url)}">下载原件</a></div><input id="visualReplacementFile" type="file" accept=".png,.jpg,.jpeg,.webp,.svg,.pdf" hidden><section id="visualReplacementOptions" class="ws-inline-editor" hidden><h4>替换 PDF 图像</h4><p id="visualReplacementName"></p><label>使用第几页<input id="visualReplacementPage" type="number" min="1" max="200" value="1"></label><div class="visual-asset-actions"><button id="confirmVisualReplacement" type="button">保存新版本</button><button id="cancelVisualReplacement" type="button">取消</button></div></section>`;
    if (v.result_source) {
      const button = document.createElement("button");
      button.type = "button";
      button.id = "openVisualResultSource";
      button.textContent = "查看数据来源";
      button.onclick = () => pp.openResultVersion?.(v.result_source);
      $("#visualAssetDetail").querySelector(".visual-asset-actions").append(button);
    }
    $("#reviewVisualAsset").onclick = () => pp.openVisualReview?.(v);
    $("#visualVersionSelect").onchange = event => { state.versionId = event.target.value; render(); };
    $("#archiveVisualAsset").onclick = () => perform(async () => { const archived = asset.status !== "archived"; await pp.api(endpoint(`assets/${asset.id}/${archived ? "archive" : "restore"}`), { method: "POST", body: JSON.stringify({ request_id: uid(), expected_revision: asset.revision }) }); await refresh(); status(archived ? "图像已归档；历史引用保留，可勾选“显示归档”后恢复。" : "图像已恢复。"); }, "正在更新资产状态…");
    $("#replaceVisualAsset").onclick = () => $("#visualReplacementFile").click();
    const saveReplacement = () => perform(async () => {
      const file = $("#visualReplacementFile").files[0];
      if (!file) throw new Error("请先选择替换文件。");
      const origin = id();
      const encoded = await content(file);
      if (origin !== id()) return;
      const page = $("#visualReplacementPage").value;
      const response = await pp.api(`/api/projects/${origin}/drawing/assets/${asset.id}/versions`, {
        method: "POST",
        body: JSON.stringify({filename: file.name, content_base64: encoded, title: asset.title,
          expected_version: asset.current_version.version_no, request_id: uid(),
          page_no: file.name.toLowerCase().endsWith(".pdf") && page ? Number(page) : null})
      });
      if (origin !== id()) return;
      state.versionId = response.version.id;
      await refresh();
      status(response.content_reused ? "相同内容已存在，已使用保留的版本。" : "新版本已保存；旧版本和既有引用保持可用。");
    }, "正在校验替换文件并生成预览…");
    $("#visualReplacementFile").onchange = event => {
      const file = event.target.files[0];
      if (!file) return;
      if (file.name.toLowerCase().endsWith(".pdf")) {
        $("#visualReplacementOptions").hidden = false;
        $("#visualReplacementName").textContent = file.name;
        $("#visualReplacementPage").focus();
      } else saveReplacement();
    };
    $("#confirmVisualReplacement").onclick = saveReplacement;
    $("#cancelVisualReplacement").onclick = () => {
      $("#visualReplacementFile").value = "";
      $("#visualReplacementOptions").hidden = true;
      $("#replaceVisualAsset").focus();
    };
    $("#insertVisualAsset").onclick = () => perform(async () => {
      const file = pp.data.files.find(item => item.path === $("#fileSelect").value);
      if (!file) throw new Error("请先选择正文文件。");
      if (pp.flushEditorChanges && !(await pp.flushEditorChanges())) throw new Error("请先处理正文的保存提示，再插入图像。");
      if ($("#editor").value !== file.content) throw new Error("正文还在保存，请稍后插入图像。");
      await pp.api(endpoint(`assets/${asset.id}/insert-into-editor`), { method: "POST", body: JSON.stringify({ version_id: v.id, request_id: uid(), target_path: file.path, base_file_version: file.version, caption: $("#visualInsertCaption").value }) });
      const path = file.path; await pp.reload(); pp.openWorkspace?.("write"); $("#fileSelect").value = path; $("#editor").value = pp.data.files.find(item => item.path === path)?.content || "";
      $("#editor").focus();
      status(`图像 v${v.version_no} 已经插入 ${path}；引用固定为本次选择的版本。`);
    }, "正在校验正文版本并插入图像引用…");
  }
  $("#visualImportForm").onsubmit = event => { event.preventDefault(); perform(async () => { const file = $("#visualAssetFile").files[0]; const page = $("#visualPdfPage").value; const response = await pp.api(endpoint("assets/import"), { method: "POST", body: JSON.stringify({ filename: file.name, content_base64: await content(file), title: $("#visualAssetTitle").value, page_no: file.name.toLowerCase().endsWith(".pdf") && page ? Number(page) : null, request_id: uid() }) }); state.assetId = response.asset.id; state.versionId = response.version.id; $("#visualImportForm").reset(); await refresh(); status("图像已导入并生成预览。可选择版本后插入正文。"); }, "正在校验文件并生成预览…"); };
  $("#refreshVisualAssets").onclick = () => refresh().catch(error => status(message(error), true));
  $("#visualShowArchived").onchange = () => refresh().catch(error => status(message(error), true));
  $("#visualSourceProject").onchange = async event => { try { const source = event.target.value; const response = source ? await pp.api(`/api/projects/${source}/drawing/assets`) : { assets: [] }; $("#visualSourceVersion").innerHTML = response.assets.map(asset => `<option value="${esc(asset.current_version_id)}">${esc(asset.title)} · v${asset.current_version.version_no}</option>`).join("") || '<option value="">来源论文没有可复制图像</option>'; } catch (error) { status(message(error), true); } };
  $("#copyVisualAsset").onclick = () => perform(async () => { const source = $("#visualSourceProject").value, version = $("#visualSourceVersion").value; if (!source || !version) throw new Error("请先选择来源论文和图像。"); const response = await pp.api(endpoint("assets/copy"), { method: "POST", body: JSON.stringify({ source_project_id: source, source_version_id: version, request_id: uid() }) }); state.assetId = response.asset.id; await refresh(); status("图像已复制到当前论文，后续版本独立演进。"); }, "正在复制受管原件并建立独立版本…");
  setInterval(() => { if (!id() || pp.data === state.observed) return; state.observed = pp.data; if (id() !== state.project) { state.project = id(); state.assetId = ""; state.versionId = ""; } refresh().catch(error => status(message(error), true)); pp.api('/api/projects').then(projects => { $("#visualSourceProject").innerHTML = '<option value="">选择来源论文</option>' + projects.filter(project => project.id !== id()).map(project => `<option value="${esc(project.id)}">${esc(project.name)}</option>`).join(""); }).catch(() => {}); }, 500);
})();
