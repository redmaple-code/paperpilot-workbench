import {StructureSession} from './structure/session.js';
import {StructureEditor} from './structure/editor.js?v=20261123-chapter-map-v2';

/* PaperPilot native structure tools. Domain writes use the existing reviewable action service. */
(() => {
  const pp = window.paperpilot;
  if (!pp) return;
  const uid = () => crypto.randomUUID();
  const host = document.querySelector('#outlineStructureWorkbench');
  if (!host) return;
  const shell = document.createElement('section');
  shell.className = 'structure-workbench';
  shell.innerHTML = `
    <div class="structure-toolbar"><div class="structure-entry"><button class="primary" data-structure-action="idea">从创意生成大纲</button><button data-structure-action="import">导入论文</button><select aria-label="结构工作区" id="structureWorkspace"><option value="">选择构思版本</option></select></div><div class="structure-file-actions"><button data-structure-action="save">保存</button><button data-structure-action="resolve">比较修改</button><details><summary>更多</summary><button data-structure-action="history">版本历史</button><button data-structure-action="reload">重新载入</button></details></div></div>
    <p class="structure-status" role="status"></p>
    <div class="structure-import" hidden>
      <div class="structure-imported" hidden><label>已导入的论文<select id="structureImportedManuscript"></select></label><button data-structure-action="use-imported">使用已导入原稿</button></div>
      <label>论文名称<input id="structureImportTitle" value="导入论文"></label><label>选择论文文件<input id="structureImportFile" type="file" accept=".pdf,.txt,.md,.tex,.markdown,.html"></label><label>公开论文地址<input id="structureImportUrl" type="url" placeholder="https://…"></label><button data-structure-action="read-url">读取公开论文</button><label>论文文本<textarea id="structureImportText" rows="8" placeholder="粘贴已有论文，或选择PDF、文本、Markdown、LaTeX 文件"></textarea></label><button data-structure-action="import-submit">导入并查看结构</button></div>
    <section class="structure-active" hidden>
      <div class="structure-context-row"><p class="structure-origin-summary"></p><button data-structure-action="preview">AI 调整结构</button><button data-structure-action="stop" hidden>停止生成</button><button data-structure-action="accept-draft" hidden>接受结构草稿</button><button class="primary" data-structure-action="proposal">审阅并采用大纲</button></div>
      <p class="structure-innovation-refs" hidden></p>
      <details class="structure-generate"><summary>本轮结构要求与采用选项</summary><label class="structure-replace-label"><input id="structureReplace" type="checkbox">采用时替换现有章节</label><label>补充构思或调整方向<textarea id="structureInstruction" rows="2" placeholder="例如突出创新点，为每章安排证据与验证计划"></textarea></label></details>
      <details class="structure-preview" hidden open><summary>确认生成范围</summary><p></p><label><input id="structureConsent" type="checkbox">确认接收方、来源范围与预算</label><button data-structure-action="run">开始生成</button></details>
      <section class="structure-native" aria-label="论文结构编辑"></section>
      <section class="structure-history" hidden></section>
    </section>
    <dialog class="structure-apply-dialog"><h3>审阅章节变更</h3><div class="structure-apply-rows"></div><pre></pre><label><input class="structure-revise" type="checkbox">接受修订已确认阶段并将相关阶段恢复待审</label><div><button data-structure-action="apply">应用所选章节与目标</button><button data-structure-action="close-apply">保留待审</button></div></dialog>`;
  host.replaceChildren(shell);
  const $ = selector => shell.querySelector(selector);
  let pid = '', wid = '', draftId = '', current, draft, generation, preview, payload, action, session, epoch = 0;
  const editor = new StructureEditor($('.structure-native'), {onStatus: message => status(message), onAssist: target => { void previewGeneration(target).catch(error => status(error.message)); }});
  let importSource = {pages: [], status: 'complete', warnings: [], locator: ''};
  const status = message => { $('.structure-status').textContent = message; };
  const base = (project = pid, workspace = wid) => `/api/projects/${encodeURIComponent(project)}/structure-workspaces${workspace ? '/' + encodeURIComponent(workspace) : ''}`;
  const isCurrent = token => token === epoch && pp.current?.project?.id === pid;
  async function api(path, body, method = 'POST') {
    const response = await fetch(path, {method, headers: {'Content-Type': 'application/json'}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
    const data = await response.json();
    if (!response.ok) { const error = new Error(data.detail?.message || JSON.stringify(data.detail || data)); error.status = response.status; throw error; }
    return data;
  }
  const workspace = () => session?.workspace;
  async function saveSections() {
    const active = session;
    if (!active) return;
    await active.save();
    if (session !== active) throw new Error('当前论文已切换，本次保存已留在原论文。');
    current = active.current; draft = draftId ? active.record : null;
  }
  async function resolveChanges() {
    const active = session;
    const compared = await active.compare();
    if (session !== active) return;
    const dialog = document.createElement('dialog'); dialog.className = 'structure-apply-dialog';
    const title = document.createElement('h3'); title.textContent = '比较结构修改'; dialog.append(title);
    const choices = [];
    for (const conflict of compared.comparison.conflicts) {
      const label = document.createElement('h4'), text = document.createElement('pre'), select = document.createElement('select');
      label.textContent = conflict.label; text.textContent = `共同基线\n${JSON.stringify(conflict.before)}\n本地\n${JSON.stringify(conflict.left)}\n远端\n${JSON.stringify(conflict.right)}`;
      for (const [value, name] of [['local', '采用本地修改'], ['remote', '采用远端修改']]) { const option = document.createElement('option'); option.value = value; option.textContent = name; select.append(option); }
      dialog.append(label, text, select); choices.push(select);
    }
    if (!choices.length) { const p = document.createElement('p'); p.textContent = '修改可以合并，保存后会再次校验章节与关系。'; dialog.append(p); }
    const save = document.createElement('button'), cancel = document.createElement('button'); save.textContent = '保存合并结果'; cancel.textContent = '保留待处理'; dialog.append(save, cancel); shell.append(dialog); dialog.showModal();
    const accepted = await new Promise(resolve => { save.onclick = () => resolve(true); cancel.onclick = () => resolve(false); dialog.oncancel = () => resolve(false); }); dialog.remove();
    if (!accepted || session !== active) return;
    compared.comparison.conflicts.forEach((conflict, index) => { conflict.object[conflict.field] = choices[index].value === 'local' ? conflict.left : conflict.right; });
    await active.resolve(compared);
    if (session === active) editor.render();
  }
  async function open(workspaceId, selectedDraft = '') {
    const token = ++epoch, targetPid = pid;
    session?.close(); session = null; $('.structure-native').hidden = true;
    resetTransient();
    const next = new StructureSession({projectId: targetPid, workspaceId, draftId: selectedDraft,
      onStatus: message => { if (isCurrent(token)) status(message); }});
    await next.load();
    if (!isCurrent(token)) { next.close(); return; }
    session = next; wid = workspaceId; draftId = selectedDraft; current = next.current; draft = selectedDraft ? next.record : null;
    pp.structureWorkspaceId = wid;
    $('#structureWorkspace').value = wid;
    localStorage.setItem('paperpilot.structure.' + pid, wid);
    $('.structure-active').hidden = false; $('.structure-native').hidden = false;
    $('.structure-origin-summary').textContent = `${current.host.entry_mode === 'idea_from_stage01' ? '承接研究构思' : '导入论文'} · ${workspace().source.segments.length} 个来源片段${draft ? ' · 生成草稿待审' : ''}`;
    const sourceLinks = $('.structure-innovation-refs');
    const refs = draft?.innovation_refs || [];
    sourceLinks.hidden = !refs.length;
    sourceLinks.replaceChildren();
    if (refs.length) sourceLinks.append(document.createTextNode(draft.innovation_review_required ? '创新点已更新，建议复核。 ' : '结构草稿引用：'));
    for (const ref of refs) {
      const anchor = document.createElement('a');
      anchor.href = '/innovations?' + new URLSearchParams({project: pid, point: ref.point_id, version: ref.version});
      anchor.textContent = (ref.role === 'primary' ? '主创新' : '副创新') + ' v' + ref.version;
      sourceLinks.append(anchor, document.createTextNode(' '));
    }
    $('[data-structure-action="accept-draft"]').hidden = !draftId;
    status(next.dirty ? '已恢复本机修改，请核对后保存。' : current.host.context_changed ? '前序构思已更新，可重新承接创意生成大纲。' : '选择章节编辑目标，选择脉络节点核对来源。');
    editor.setSession(next);
    const pendingRun = localStorage.getItem(`paperpilot.structure.run.${pid}.${wid}`);
    if (pendingRun) { generation = {id: pendingRun}; void poll(token, targetPid, workspaceId, pendingRun); }
  }
  function resetTransient() {
    action = null; preview = null; payload = null; generation = null;
    $('.structure-apply-dialog').close();
    $('.structure-history').hidden = true; $('.structure-history').replaceChildren();
    $('.structure-preview').hidden = true;
    $('[data-structure-action="stop"]').hidden = true;
  }
  async function refresh(preferredWorkspace = '') {
    const project = pp.current?.project;
    if (!project) return;
    session?.close(); session = null; $('.structure-active').hidden = true;
    resetTransient();
    $('.structure-import').hidden = true;
    $('#structureImportText').value = ''; $('#structureImportUrl').value = ''; $('#structureImportFile').value = '';
    importSource = {pages: [], status: 'complete', warnings: [], locator: ''};
    pid = project.id; wid = ''; draftId = ''; current = null; draft = null; preview = null; generation = null;
    const token = ++epoch;
    const result = await api(base(pid, ''), undefined, 'GET');
    if (!isCurrent(token)) return;
    const imports = result.imported_manuscripts || [];
    $('.structure-imported').hidden = !imports.length;
    $('#structureImportedManuscript').replaceChildren(...imports.map(item => new Option(item.name, item.id)));
    const select = $('#structureWorkspace'); select.replaceChildren();
    const empty = document.createElement('option'); empty.value = ''; empty.textContent = '选择工作区'; select.append(empty);
    for (const item of result.workspaces) { const option = document.createElement('option'); option.value = item.id; option.textContent = `${item.title} · v${item.revision}`; select.append(option); }
    const params = new URLSearchParams(location.search);
    const requested = preferredWorkspace || params.get('structure');
    const remembered = localStorage.getItem('paperpilot.structure.' + pid);
    const choice = result.workspaces.find(w => w.id === requested || (!requested && w.id === remembered)) || result.workspaces[0];
    if (choice) await open(choice.id, !preferredWorkspace && choice.id === requested ? params.get('draft') || '' : '');
    else { $('.structure-active').hidden = true; status('从前一步创意讨论开始搭建，或导入一篇已有论文。'); }
  }
  async function create(mode) {
    const token = epoch, targetPid = pid;
    const data = {request_id: uid(), entry_mode: mode};
    if (mode === 'paper_import') Object.assign(data, {content: $('#structureImportText').value, title: $('#structureImportTitle').value,
      locator: importSource.locator || $('#structureImportFile').files[0]?.name || $('#structureImportUrl').value || '粘贴论文', pages: importSource.pages,
      source_status: importSource.status, source_warnings: importSource.warnings});
    const result = await api(base(targetPid, ''), data);
    if (!isCurrent(token)) return;
    localStorage.setItem('paperpilot.structure.' + pid, result.workspace.id);
    $('.structure-import').hidden = true;
    await refresh(result.workspace.id);
    if (pp.current?.project?.id !== targetPid || session?.workspaceId !== result.workspace.id) return;
    if (mode === 'idea_from_stage01' && current.workspace.source.segments.length === 0) status('前一步尚无已保存构思。可先在论文助手讨论研究问题，再承接创意；也可在画布手动搭建。');
    else await previewGeneration();
  }
  async function previewGeneration(target = null) {
    const active = session;
    if (!active) return;
    const token = epoch, targetPid = pid, targetWid = wid, targetDraft = draftId;
    const instruction = target ? `补充选中${target.kind} ${target.id}的空白内容与来源，保护已有人工字段。` : $('#structureInstruction').value.trim() || (current.host.entry_mode === 'idea_from_stage01' ? '承接完整论文idea构思，主动搭建章节大纲、章节目标与论证主线，并用Traceify保存可编辑结构。' : '按Traceify skill抽取这篇论文的章节、论点、证据、关系、模块与依据链，并保存完整结构。');
    await saveSections();
    if (!isCurrent(token) || session !== active) return;
    let cid = document.querySelector('#agentConversation')?.value;
    if (!cid) cid = (await api(`/api/projects/${targetPid}/agent/conversations`, {title: '论文结构讨论'})).id;
    if (!isCurrent(token) || session !== active) return;
    const request = {conversation_id: cid, stage_id: 'STAGE-02', instruction, structure_workspace_id: targetWid, structure_draft_id: targetDraft, structure_target: target, context_snapshot: {project_id: targetPid}};
    const result = await api(`${base(targetPid, targetWid)}/generation/preview`, request);
    if (!isCurrent(token) || wid !== targetWid) return;
    payload = request; preview = result;
    $('.structure-preview').hidden = false;
    $('.structure-preview p').textContent = `${result.provider?.model || '尚未配置模型'} 将读取当前构思或导入原文。最多 ${result.limits.model_calls} 次模型调用，${result.limits.tokens} tokens。${result.provider_error || ''}`;
    $('#structureConsent').checked = !result.authorization_required;
    status(result.generation_allowed ? '请核对模型、来源范围和预算后开始生成。' : result.provider_error || '请先在论文设置中配置并测试模型。');
  }
  async function poll(token, targetPid, targetWid, runId) {
    if (!isCurrent(token) || wid !== targetWid) return;
    const result = await api(`${base(targetPid, targetWid)}/generations/${runId}`, undefined, 'GET');
    if (!isCurrent(token) || wid !== targetWid) return;
    const running = ['queued', 'running'].includes(result.run.status);
    $('[data-structure-action="stop"]').hidden = !running;
    if (running) { status(`AI 正在组织论文结构 · ${result.run.status} · 已用 ${result.run.tokens_used || 0} tokens，已完成部分持续保留`); setTimeout(() => { void poll(token, targetPid, targetWid, runId).catch(error => status(error.message)); }, 1200); }
    else {
      localStorage.removeItem(`paperpilot.structure.run.${targetPid}.${targetWid}`);
      if (result.drafts.length) { const did = result.drafts[0].id; await open(targetWid, did); status(`运行 ${result.run.status} · ${draft.state === 'ready' ? '完整草稿待审' : '部分草稿已保留'} · 已覆盖 ${draft.workspace.coverage?.length || 0}/${current.workspace.source.segments.length} 个片段 · ${draft.errors.length} 项待修正，可手动编辑或继续生成。`); }
      else status(`运行 ${result.run.status} · ${result.run.blocked_reason || result.run.error_code || '本轮返回说明，尚无已保存结构草稿。可继续讨论或缩小输入范围。'}`);
    }
  }
  shell.addEventListener('click', async event => {
    const task = event.target.closest('[data-structure-action]')?.dataset.structureAction;
    if (!task) return;
    const active = session, token = epoch, targetPid = pid, targetWid = wid, targetDraft = draftId;
    try {
      if (task === 'save') await saveSections();
      if (task === 'resolve') await resolveChanges();
      if (task === 'idea') await create('idea_from_stage01');
      if (task === 'import') $('.structure-import').hidden = !$('.structure-import').hidden;
      if (task === 'use-imported') {
        const batch = $('#structureImportedManuscript').value;
        if (!batch) return;
        const button = event.target.closest('[data-structure-action]');
        button.disabled = true;
        try {
          const result = await api(`${base(targetPid, '')}/from-manuscript/${encodeURIComponent(batch)}`, {});
          if (isCurrent(token)) await refresh(result.workspace.id);
        } finally {
          button.disabled = false;
        }
      }
      if (task === 'import-submit') await create('paper_import');
      if (task === 'read-url') {
        const url = $('#structureImportUrl').value.trim();
        importSource = {pages: [], status: 'failed', warnings: ['公开来源尚未读取成功'], locator: url};
        const token = epoch, result = await api(`${base(pid, '')}/import-url/preview`, {url});
        if (!isCurrent(token)) return;
        importSource = result; $('#structureImportText').value = result.content; status('来源已读取，请核对正文及提取范围后导入。');
      }
      if (task === 'preview') await previewGeneration();
      if (task === 'run') {
        if (!preview?.generation_allowed || !$('#structureConsent').checked) throw new Error('请核对预览并确认发送范围与预算。');
        const targetPid = pid, targetWid = wid, token = epoch;
        const result = await api(`${base(targetPid, targetWid)}/generations`, {...payload, request_id: uid(), preview_fingerprint: preview.preview_fingerprint, consent_to_send: true, possible_cost_confirmed: true});
        if (!isCurrent(token)) return;
        generation = result; $('.structure-preview').hidden = true;
        localStorage.setItem(`paperpilot.structure.run.${pid}.${wid}`, result.id); await poll(token, targetPid, targetWid, result.id);
      }
      if (task === 'stop' && generation) { await api(`${base()}/generations/${generation.id}/cancel`, {request_id: uid()}); if (isCurrent(token)) status('已请求停止，部分结果保留。'); }
      if (task === 'reload') await open(wid, draftId);
      if (task === 'accept-draft') {
        await saveSections();
        if (!isCurrent(token) || session !== active) return;
        await api(`${active.path}/accept`, {request_id: uid(), expected_workspace_revision: active.current.version, source_hash: active.current.workspace.source.content_hash});
        if (!isCurrent(token) || session !== active) return;
        await open(targetWid);
      }
      if (task === 'proposal') {
        await saveSections();
        if (!isCurrent(token) || session !== active) return;
        const result = await api(`${active.root}/outline-proposal`, {request_id: uid(), draft_id: targetDraft || null, replace_outline: $('#structureReplace').checked});
        if (!isCurrent(token) || session !== active) return;
        action = {...result, session: active};
        const rows = $('.structure-apply-rows'); rows.replaceChildren();
        action.payload.operations.forEach((op, index) => { const label = document.createElement('label'), input = document.createElement('input'); input.type = 'checkbox'; input.checked = true; input.value = index; label.append(input, document.createTextNode(`${op.op} · ${op.node?.title || op.changes?.title || op.id}`)); rows.append(label); });
        $('.structure-apply-dialog pre').textContent = '请选择章节。父子依赖、当前工作区、大纲和阶段版本将在预览及应用时再次校验。';
        $('.structure-apply-dialog').showModal();
      }
      if (task === 'close-apply') $('.structure-apply-dialog').close();
      if (task === 'apply') {
        const selectedAction = action;
        if (!selectedAction || selectedAction.session !== active) return;
        const path = `/api/projects/${targetPid}/agent/actions/${selectedAction.id}`;
        const indices = Array.from(shell.querySelectorAll('.structure-apply-rows input:checked'), input => Number(input.value));
        const diff = await api(path + '/preview', {selected_indices: indices, acknowledge_revision: $('.structure-revise').checked});
        if (!isCurrent(token) || session !== active || action !== selectedAction) return;
        $('.structure-apply-dialog pre').textContent = JSON.stringify(diff.diff, null, 2);
        if (!confirm('已展示所选章节差异。确认应用章节、目标和阶段草稿？')) return;
        const result = await api(path + '/apply', {request_id: uid(), expected_version: selectedAction.base_version, action_fingerprint: diff.action_fingerprint, accepted: true});
        if (!isCurrent(token) || session !== active) return;
        $('.structure-apply-dialog').close(); document.dispatchEvent(new CustomEvent('paperpilot:resource-updated', {detail: result.event}));
        await open(targetWid);
      }
      if (task === 'history') {
        if (!active) return;
        const result = await api(`${active.root}/history`, undefined, 'GET');
        if (!isCurrent(token) || session !== active) return;
        const list = $('.structure-history'); list.hidden = false; list.replaceChildren();
        for (const item of result.versions) {
          const button = document.createElement('button'); button.textContent = `恢复为新版本 · v${item.revision} · ${item.operation}`;
          button.onclick = async () => {
            if (!isCurrent(token) || session !== active) return;
            try {
              await saveSections();
              if (!isCurrent(token) || session !== active) return;
              await api(`${active.root}/restore`, {request_id: uid(), expected_workspace_revision: active.current.version,
                source_hash: active.current.workspace.source.content_hash, revision: item.revision});
              if (isCurrent(token) && session === active) await open(targetWid);
            } catch (error) { if (isCurrent(token)) status(error.message); }
          }; list.append(button);
        }
      }
    } catch (error) { if (isCurrent(token)) status(error.message); }
  });
  $('#structureWorkspace').onchange = () => { if ($('#structureWorkspace').value) void open($('#structureWorkspace').value).catch(error => status(error.message)); };
  $('#structureImportFile').onchange = async () => {
    const file = $('#structureImportFile').files[0];
    if (!file) return;
    const token = epoch, targetPid = pid;
    importSource = {pages: [], status: 'failed', warnings: ['文件尚未读取成功'], locator: file.name};
    $('#structureImportTitle').value = file.name;
    try {
      const response = await fetch(`${base(targetPid, '')}/import-file/preview?filename=${encodeURIComponent(file.name)}`, {method: 'POST', headers: {'Content-Type': file.type || 'application/octet-stream'}, body: file});
      const result = await response.json();
      if (!isCurrent(token)) return;
      if (!response.ok) throw new Error(result.detail?.message || '论文文件读取失败。');
      importSource = result; $('#structureImportText').value = result.content; status(result.warnings?.join('；') || '论文已读取，来源页码会随快照保存。');
    } catch (error) { status(error.message); }
  };
  $('#structureImportText').oninput = () => { importSource = {pages: [], status: 'complete', warnings: [], locator: '作者提供的论文文本'}; };
  const loadProject = () => {
    if (pp.standaloneOutline && pp.current?.project?.id !== pid) void refresh().catch(error => status(error.message));
  };
  document.addEventListener('paperpilot:project-loaded', loadProject);
  window.addEventListener('pagehide', () => session?.close());
  if (pp.current?.project) loadProject();
  pp.agentCards?.register('structure_preview', card => {
    const article = document.createElement('article'), title = document.createElement('h4'), text = document.createElement('p'), button = document.createElement('button');
    title.textContent = card.title; text.textContent = card.summary; button.textContent = '打开可编辑结构草稿';
    button.onclick = () => {
      if (card.project_id !== pp.current?.project?.id) { status('请返回该草稿所属论文。'); return; }
      const url = new URL(pp.paperWorkspaceUrl('/outline', 'outline/nodes'), location.origin);
      url.searchParams.set('structure', card.workspace_id);
      if (card.draft_id) url.searchParams.set('draft', card.draft_id);
      location.assign(url);
    };
    article.append(title, text, button); return article;
  });
})();
