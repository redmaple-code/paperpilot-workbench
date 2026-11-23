/* Explicit resource review actions. All requests retain their originating paper. */
(() => {
  const pp = window.paperpilot;
  if (!pp) return;
  const $ = s => document.querySelector(s);
  const uid = () => crypto.randomUUID();
  const id = () => pp.data?.project?.id || pp.current?.project?.id || '';
  const same = pid => pid === id();
  const esc = value => String(value ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[x]));
  const post = (path, body, method = 'POST') => pp.api(path, {method, body: JSON.stringify(body)});
  function message(error) { try { const data = JSON.parse(error.message); return data.detail?.message || error.message; } catch { return error.message; } }
  function panel(anchor, name, title, body) {
    const target = $(anchor);
    if (!target || $(`#${name}`)) return;
    target.insertAdjacentHTML('afterend', `<section id="${name}" class="resource-workbench" aria-labelledby="${name}Title"><h4 id="${name}Title">${title}</h4>${body}<p id="${name}Status" class="review-status" role="status" aria-live="polite"></p></section>`);
  }
  async function action(button, status, work) {
    const pid = id(); button.disabled = true;
    try { await work(pid); } catch (error) { if (same(pid) && status) status.textContent = message(error); }
    finally { if (button.isConnected) button.disabled = false; }
  }
  function bind(selector, status, work) {
    const button = $(selector); if (button) button.onclick = () => action(button, $(status), work);
  }
  let terminologyLoad = 0, sharedLoad = 0, outlineLoad = 0, corpusLoad = 0;

  panel('#termList', 'termReviewWorkbench', '术语沉淀与人工复核', `
    <p class="review-note">选择论文术语后预览沉淀。AI只提出建议，领域库始终需要人工确认。</p>
    <div class="workbench-actions"><button id="refreshTermReview" type="button">刷新术语与复核</button><button id="previewTermPromotion" type="button">预览沉淀</button></div>
    <div id="termReviewChoices"></div><div id="termPromotionPreview"></div><div id="termClassificationPreview"></div>
    <details open><summary>人工复核收件箱</summary><div id="termManualInbox"></div></details>`);
  panel('#configurationStatus', 'sharedResourceWorkbench', '领域共享资料', `
    <p class="review-note">编辑共享库会生成新版本；已复制到论文的资料仍保留原快照。</p>
    <div class="workbench-actions"><button id="refreshSharedResources" type="button">刷新领域库与推荐</button></div>
    <div id="sharedResourceList"></div><details><summary>新增共享资料</summary>
      <label>资料类型<select id="sharedNewType"><option value="corpus">语料</option><option value="reference">参考文献</option><option value="term">术语</option></select></label>
      <label>标题<input id="sharedNewTitle"></label><label>内容<textarea id="sharedNewContent"></textarea></label><button id="addSharedResource" type="button">添加到领域库</button></details>
    <details open><summary>同领域推荐</summary><div id="domainRecommendationList"></div></details>`);
  panel('#editor', 'editorRecoveryWorkbench', '比较恢复草稿', `
    <button id="compareEditorDraft" type="button">查看草稿差异</button><div id="editorDraftComparison"></div>`);
  panel('#outlineTree', 'structuredSuggestionWorkbench', '逐节点审阅大纲建议', `
    <label>希望怎样改进大纲<textarea id="structuredOutlineInstruction" placeholder="描述要调整的结构、章节目的或范围"></textarea></label>
    <div class="workbench-actions"><button id="previewStructuredOutline" type="button">预览结构化建议请求</button><button id="refreshStructuredCandidates" type="button">刷新节点候选</button></div>
    <div id="structuredOutlinePreview"></div><div id="structuredOutlineCandidates"></div>`);
  panel('#corpusList', 'corpusLineageWorkbench', '语料来源与校正版', `
    <div class="workbench-actions"><button type="button" id="refreshCorpusLineage">刷新语料列表</button></div>
    <label>查看语料<select id="corpusLineageChoice"><option value="">选择语料</option></select></label>
    <div id="corpusLineageView"></div>`);
  panel('#continuationOutlineStatus', 'continuationPlanWorkbench', '逐段目标与整批限制', `
    <label><input id="continuationUseSubgoals" type="checkbox">分别指定每段的章节目标</label>
    <button id="editContinuationSubgoals" type="button">按分段数展开目标</button><div id="continuationSubgoalRows"></div>
    <label>整批token硬上限<input id="continuationBatchTokenLimit" type="number" min="1" placeholder="留空使用当前任务上限"></label>
    <label>整批金额硬上限（使用所选模型登记币种）<input id="continuationBatchAmount" type="number" min="0" step="0.0001" placeholder="费率未知时不能执行金额限制"></label>
    <p class="review-note">每次发送前累计实际用量或未知请求的保守预留；恢复不会清空既有消耗。</p>`);
  // Match the server's canonical JSON exactly, including explicit null anchors.
  function canonical(value) {
    if (Array.isArray(value)) return '['+value.map(canonical).join(', ')+']';
    if (value && typeof value === 'object') return '{'+Object.keys(value).sort().map(k => JSON.stringify(k)+': '+canonical(value[k])).join(', ')+'}';
    return JSON.stringify(value);
  }
  pp.continuationRequestPrompt = (prompt, goals) => goals?.length ? prompt+'\n本批逐段目标（只生成当前指定子目标，不自动改动其他章节）：'+canonical(goals) : prompt;
  pp.continuationPlanSnapshot = () => {
    const subgoals = [];
    if ($('#continuationUseSubgoals')?.checked) {
      const rows = [...$('#continuationSubgoalRows').querySelectorAll('[data-subgoal-row]')];
      if (rows.length !== Number($('#continuationSegments').value)) throw new Error('分段数已改变，请重新展开逐段目标并确认。');
      for (const row of rows) {
        const goal = row.querySelector('[data-subgoal-text]').value.trim();
        if (!goal) throw new Error('请为每段填写具体目标。');
        const start = row.querySelector('[data-subgoal-start]').value, end = row.querySelector('[data-subgoal-end]').value;
        subgoals.push({goal, outline_node_id: row.querySelector('[data-subgoal-node]').value, anchor_start: start === '' ? null : Number(start), anchor_end: end === '' ? null : Number(end)});
      }
    }
    return {subgoals, hard_budget_tokens: $('#continuationBatchTokenLimit')?.value ? Number($('#continuationBatchTokenLimit').value) : null,
      hard_budget_amount: $('#continuationBatchAmount')?.value !== '' ? Number($('#continuationBatchAmount').value) : null};
  };
  bind('#editContinuationSubgoals', '#continuationPlanWorkbenchStatus', async pid => {
    const graph = await pp.api(`/api/projects/${pid}/outline/structured`);
    if (!same(pid)) return;
    if (!graph.nodes.length) throw new Error('先在大纲页建立结构化节点。');
    const count = Number($('#continuationSegments').value), old = [...$('#continuationSubgoalRows').querySelectorAll('[data-subgoal-text]')].map(x => x.value);
    $('#continuationSubgoalRows').innerHTML = Array.from({length:count}, (_,i) => `<fieldset data-subgoal-row><legend>第${i+1}段</legend>
      <label>目标章节<select data-subgoal-node>${graph.nodes.map(n => `<option value="${esc(n.id)}">${esc(n.title)}</option>`).join('')}</select></label>
      <label>本段目标<input data-subgoal-text value="${esc(old[i] || '')}"></label>
      <label>起点（留空使用当前选区）<input data-subgoal-start type="number" min="0"></label><label>终点<input data-subgoal-end type="number" min="0"></label></fieldset>`).join('');
    $('#continuationUseSubgoals').checked = true;
    $('#continuationPlanWorkbenchStatus').textContent = `已按结构化大纲v${graph.version}展开${count}个子目标；提交时会再次核对版本。`;
  });

  async function refreshTerms(pid = id()) {
    if (!pid || !$('#termReviewChoices')) return;
    const token = ++terminologyLoad;
    const [paper, reviews, origins] = await Promise.all([
      pp.api(`/api/projects/${pid}`), pp.api(`/api/projects/${pid}/terminology/reviews`), pp.api(`/api/projects/${pid}/terminology/provenance`)
    ]);
    if (!same(pid) || token !== terminologyLoad) return;
    const domain = paper.project.domain_id;
    $('#termReviewChoices').innerHTML = paper.terms.map(term => {
      const origin = origins.find(x => x.created_term_id === term.id);
      return `<div class="review-row"><label><input type="checkbox" data-promote-term="${esc(term.id)}" checked>${esc(term.term)} — ${esc(term.definition)}</label>
        <p class="review-note">${origin ? `领域复制版 v${origin.source_domain_version}，独立论文副本` : '当前论文维护的术语'}${term.aliases ? `；别名：${esc(term.aliases)}` : ''}</p>
        <button type="button" data-classify-term="${esc(term.id)}" ${!domain ? 'disabled' : ''}>分类预览 / 人工复核</button></div>`;
    }).join('') || '<p class="review-note">先添加论文术语，再进行沉淀或复核。</p>';
    $('#termReviewWorkbenchStatus').textContent = domain ? '论文术语与领域版本独立；未确认的分类不会发布。' : '请先在论文配置中绑定领域库。';
    $('#termReviewChoices').querySelectorAll('[data-classify-term]').forEach(button => button.onclick = () => action(button, $('#termReviewWorkbenchStatus'), p => previewClassification(p, button.dataset.classifyTerm)));
    const domains = await pp.api('/api/domains');
    const terms = domain ? await pp.api(`/api/domains/${domain}/terms`) : [];
    if (same(pid) && token === terminologyLoad) renderInbox(pid, reviews, terms, domains.find(d => d.id === domain)?.version);
  }
  async function previewPromotion(pid) {
    const termIds = [...$('#termReviewChoices').querySelectorAll('[data-promote-term]:checked')].map(x => x.dataset.promoteTerm);
    if (!termIds.length) throw new Error('请先选择至少一个论文术语。');
    const paper = await pp.api(`/api/projects/${pid}`);
    if (!same(pid)) return;
    if (!paper.project.domain_id) throw new Error('请先绑定领域库。');
    const preview = await post(`/api/projects/${pid}/terms/promote/preview`, {request_id: uid(), domain_id: paper.project.domain_id, term_ids: termIds, source_project_revision: paper.project.project_revision});
    if (!same(pid)) return;
    const host = $('#termPromotionPreview');
    host.innerHTML = `<h5>沉淀到领域 v${preview.domain_version} 的差异</h5>` +
      preview.rows.map(row => `<label><input type="checkbox" data-promotion-choice="${esc(row.term_id)}" ${row.outcome === 'new' ? 'checked' : 'disabled'}>${esc(row.term)}：${row.outcome === 'new' ? '新增' : '领域已有相同值，无需重复添加'}</label>`).join('') +
      preview.conflicts.map(row => `<div class="conflict-choice"><label><input type="checkbox" data-promotion-choice="${esc(row.term_id)}">${esc(row.term)}：定义冲突</label>
        <p>领域原值：${esc(row.existing.definition)}</p><p>论文值：${esc(row.incoming.definition)}</p>
        <label>冲突处理<select data-promotion-resolution="${esc(row.term_id)}"><option value="keep_domain">保留领域原值</option><option value="use_paper">确认使用论文值，保留旧领域版本</option></select></label></div>`).join('') +
      '<div class="workbench-actions"><button type="button" id="commitTermPromotion">按选择确认沉淀</button><button type="button" id="cancelTermPromotion">取消预览</button></div>';
    const requestId = uid();
    $('#cancelTermPromotion').onclick = () => host.replaceChildren();
    $('#commitTermPromotion').onclick = () => action($('#commitTermPromotion'), $('#termReviewWorkbenchStatus'), async () => {
      if (!same(pid)) return;
      const selected = [...host.querySelectorAll('[data-promotion-choice]:checked')].map(x => x.dataset.promotionChoice);
      const resolutions = Object.fromEntries([...host.querySelectorAll('[data-promotion-resolution]')].map(x => [x.dataset.promotionResolution, x.value]));
      const result = await post(`/api/projects/${pid}/terms/promote/commit`, {preview_id: preview.id, request_id: requestId, selected_term_ids: selected, conflict_decisions: resolutions});
      if (!same(pid)) return;
      host.replaceChildren(); await pp.reload(); await refreshTerms(pid);
      $('#termReviewWorkbenchStatus').textContent = `已确认沉淀；领域当前版本 v${result.domain_version}，旧版本和论文原值保留。`;
    });
  }
  async function previewClassification(pid, termId) {
    const preview = await post(`/api/projects/${pid}/terminology/classification-preview`, {request_id: uid(), term_id: termId});
    if (!same(pid)) return;
    const host = $('#termClassificationPreview');
    host.innerHTML = `<h5>分类发送预览</h5><pre>${esc(JSON.stringify(preview.outbound_preview, null, 2))}</pre>
      <p class="review-note">接收方：${esc(preview.provider?.name || '未配置可用模型')}；费用未知。只发送以上术语内容。${esc(preview.provider_error || '')}</p>
      <label><input type="checkbox" id="consentTermClassification">授权本次发送，接受可能费用；结果仍需人工复核</label>
      ${preview.provider?.auth_mode === 'temporary' ? '<label>本次临时密钥<input type="password" id="termClassificationKey" autocomplete="off"></label>' : ''}
      <div class="workbench-actions"><button type="button" id="sendTermClassification" ${preview.generation_allowed ? '' : 'disabled'}>授权后请求建议</button><button type="button" id="manualTermClassification">仅加入人工复核</button></div>`;
    const requestId = uid();
    const send = async consent => {
      if (!same(pid)) return;
      if (consent && !$('#consentTermClassification').checked) throw new Error('请先勾选本次发送和费用授权。');
      const input = $('#termClassificationKey'), temporaryKey = consent ? input?.value || pp.takeTemporaryCredential?.(preview.provider?.profile_id) || '' : '';
      if (input) input.value = '';
      await post(`/api/projects/${pid}/terminology/classify`, {request_id: requestId, term_id: termId, preview_fingerprint: preview.preview_fingerprint, consent_to_send: consent, temporary_key: temporaryKey});
      if (!same(pid)) return;
      host.replaceChildren(); await refreshTerms(pid);
      $('#termReviewWorkbenchStatus').textContent = '已进入人工复核收件箱，领域库尚未改变。';
    };
    bind('#sendTermClassification', '#termReviewWorkbenchStatus', () => send(true));
    bind('#manualTermClassification', '#termReviewWorkbenchStatus', () => send(false));
  }
  function renderInbox(pid, reviews, domainTerms, domainVersion) {
    const host = $('#termManualInbox'); host.replaceChildren();
    const pending = reviews.filter(x => x.status === 'pending_manual');
    if (!pending.length) { host.textContent = '暂无待处理复核。'; return; }
    for (const review of pending) {
      const row = document.createElement('article'); row.className = 'review-row';
      row.innerHTML = `<p>${esc(review.classifier_version)}；置信度 ${Number(review.confidence).toFixed(2)}</p><p class="review-note">${esc(review.basis)}</p>
        <label>确认术语<input data-review-name value="${esc(review.suggested_term)}"></label>
        <label>确认定义<textarea data-review-definition>${esc(review.suggested_definition)}</textarea></label>
        <label>确认别名<input data-review-aliases value="${esc(review.suggested_aliases)}"></label>
        ${domainTerms.find(t => t.term.toLowerCase() === review.suggested_term.toLowerCase()) ? `<div class="conflict-choice"><p>领域v${domainVersion}原值：${esc(domainTerms.find(t => t.term.toLowerCase() === review.suggested_term.toLowerCase()).definition)}</p><label><input type="checkbox" data-review-replace>确认替换所示领域值，保留旧版本（也可改成新名称）</label></div>` : ''}
        <div class="workbench-actions"><button type="button" data-review-accept>确认并发布领域新版本</button><button type="button" data-review-reject>拒绝建议</button></div>`;
      for (const [selector, decision] of [['[data-review-accept]', 'edit'], ['[data-review-reject]', 'reject']]) {
        const button = row.querySelector(selector); const requestId = uid();
        button.onclick = () => action(button, $('#termReviewWorkbenchStatus'), async () => {
          if (!same(pid)) return;
          const replacing = row.querySelector('[data-review-replace]')?.checked || false;
          if(replacing && row.querySelector('[data-review-name]').value !== review.suggested_term) throw new Error('改名后不能沿用原冲突替换确认，请取消勾选或重新核对。');
          await post(`/api/projects/${pid}/terminology/reviews/${review.id}/decision`, {decision, request_id: requestId, term: row.querySelector('[data-review-name]').value, definition: row.querySelector('[data-review-definition]').value, aliases: row.querySelector('[data-review-aliases]').value,replace_conflict:replacing,expected_domain_version:domainVersion});
          if (same(pid)) { await refreshTerms(pid); $('#termReviewWorkbenchStatus').textContent = decision === 'reject' ? '已拒绝；论文和领域库原值不变。' : '已人工确认，领域新版本已发布；论文副本不自动同步。'; }
        });
      }
      host.append(row);
    }
  }
  async function refreshShared(pid = id()) {
    if (!pid || !$('#sharedResourceList')) return;
    const token = ++sharedLoad;
    const paper = await pp.api(`/api/projects/${pid}`);
    if (!same(pid) || token !== sharedLoad) return;
    const domain = paper.project.domain_id;
    $('#sharedResourceWorkbench').dataset.domain = domain || '';
    if (!domain) { $('#sharedResourceList').textContent = '先为论文绑定领域库。'; $('#domainRecommendationList').replaceChildren(); return; }
    const [resources, recommendations] = await Promise.all([pp.api(`/api/domains/${domain}/resources`), pp.api(`/api/projects/${pid}/domain-recommendations`)]);
    if (!same(pid) || token !== sharedLoad) return;
    const host = $('#sharedResourceList'); host.replaceChildren();
    for (const resource of resources) {
      const row = document.createElement('details');
      row.innerHTML = `<summary>${esc(resource.title)}（v${resource.version}）</summary>
        <label>资料标题<input data-resource-title value="${esc(resource.title)}"></label>
        <label>资料内容<textarea data-resource-content>${esc(resource.content)}</textarea></label>
        <div class="workbench-actions"><button type="button" data-resource-save>保存共享新版本</button><button type="button" data-resource-history>查看版本与差异</button></div><div data-resource-history-view></div>`;
      const save = row.querySelector('[data-resource-save]'); const requestId = uid();
      save.onclick = () => action(save, $('#sharedResourceWorkbenchStatus'), async () => {
        if (!same(pid)) return;
        await post(`/api/domains/${domain}/resources/${resource.id}`, {request_id: requestId, expected_version: resource.version, title: row.querySelector('[data-resource-title]').value, content: row.querySelector('[data-resource-content]').value}, 'PUT');
        if (same(pid)) { await refreshShared(pid); $('#sharedResourceWorkbenchStatus').textContent = '共享新版本已保存；其他论文刷新领域库后可见。'; }
      });
      const history = row.querySelector('[data-resource-history]');
      history.onclick = () => action(history, $('#sharedResourceWorkbenchStatus'), async () => {
        const versions = await pp.api(`/api/domains/${domain}/resources/${resource.id}/versions`);
        if (same(pid)) row.querySelector('[data-resource-history-view]').innerHTML = versions.map(v => `<details><summary>v${v.version} · ${esc(v.title)}</summary><pre>${esc(v.content)}</pre></details>`).join('');
      });
      host.append(row);
    }
    if (!resources.length) host.textContent = '领域库尚无共享资料，可在下方添加。';
    $('#domainRecommendationList').innerHTML = recommendations.results.map(r => `<article class="review-row"><strong>${esc(r.title)}</strong><p class="review-note">v${r.version}；${r.match_reasons.map(esc).join('；')}</p></article>`).join('') || '暂无匹配；添加术语、引用或正文后，手动刷新推荐。';
  }
  async function addShared(pid) {
    const domain = $('#sharedResourceWorkbench').dataset.domain;
    if (!domain) throw new Error('请先绑定领域库。');
    const title = $('#sharedNewTitle').value.trim();
    if (!title) throw new Error('请填写资料标题。');
    await post(`/api/domains/${domain}/resources`, {resource_type: $('#sharedNewType').value, title, content: $('#sharedNewContent').value});
    if (same(pid)) { $('#sharedNewTitle').value = ''; $('#sharedNewContent').value = ''; await refreshShared(pid); }
  }
  async function compareDraft(pid) {
    const path = $('#fileSelect')?.value;
    const draft = await pp.api(`/api/projects/${pid}/editor/draft?path=${encodeURIComponent(path)}`);
    const paper = await pp.api(`/api/projects/${pid}`);
    if (!same(pid) || $('#fileSelect').value !== path) return;
    const host = $('#editorDraftComparison');
    if (draft.exists === false) { host.textContent = '当前文件没有可恢复草稿。'; return; }
    const current = paper.files.find(x => x.path === path);
    host.innerHTML = `<p>基线 v${draft.base_version}；服务器 v${draft.current_version}${draft.stale ? '，草稿已过期，需要明确选择' : ''}</p>
      <div class="three-way">${[['草稿基线', draft.base_content], ['服务器当前', current?.content || ''], ['待恢复草稿', draft.content]].map(([title, content]) => `<section><h5>${title}</h5><pre>${esc(content)}</pre></section>`).join('')}</div>
      <div class="workbench-actions"><button type="button" id="loadComparedDraft">载入编辑器继续合并</button><button type="button" id="discardComparedDraft">丢弃这个草稿</button></div>
      <label>另存为新文件<input id="draftCopyTarget" placeholder="例如：recovered-section.tex"></label><button type="button" id="saveComparedDraftCopy">将此草稿另存</button>`;
    $('#loadComparedDraft').onclick = () => { if (same(pid) && $('#fileSelect').value === path) { $('#editor').value = draft.content; $('#editor').dispatchEvent(new Event('input', {bubbles: true})); $('#editorRecoveryWorkbenchStatus').textContent = '已载入编辑器，尚未覆盖服务器内容；请合并后明确保存。'; } };
    bind('#saveComparedDraftCopy', '#editorRecoveryWorkbenchStatus', async () => {
      if (!same(pid)) return;
      const target = $('#draftCopyTarget').value.trim(); if (!target) throw new Error('请填写新文件路径。');
      await post(`/api/projects/${pid}/editor/draft-copy`, {request_id: uid(), source_path: path, target_path: target, draft_updated_at: draft.updated_at});
      if (same(pid)) { await pp.reload(); $('#editorRecoveryWorkbenchStatus').textContent = `草稿已另存到 ${target}；原文件和原草稿均保留。`; }
    });
    bind('#discardComparedDraft', '#editorRecoveryWorkbenchStatus', async () => {
      if (!same(pid)) return;
      const latest = await pp.api(`/api/projects/${pid}/editor/draft?path=${encodeURIComponent(path)}`);
      if (latest.updated_at !== draft.updated_at) throw new Error('草稿已更新，请重新比较，未丢弃。');
      await pp.api(`/api/projects/${pid}/editor/draft?path=${encodeURIComponent(path)}`, {method: 'DELETE'});
      if (same(pid)) { host.replaceChildren(); $('#editorRecoveryWorkbenchStatus').textContent = '已丢弃所查看的草稿，服务器原文不变。'; }
    });
  }
  async function refreshOutlineCandidates(pid = id()) {
    if (!pid || !$('#structuredOutlineCandidates')) return;
    const token = ++outlineLoad;
    const [graph, candidates] = await Promise.all([pp.api(`/api/projects/${pid}/outline/structured`), pp.api(`/api/projects/${pid}/outline/candidates`)]);
    if (!same(pid) || token !== outlineLoad) return;
    const host = $('#structuredOutlineCandidates'); host.replaceChildren();
    for (const candidate of candidates) {
      const row = document.createElement('article'); row.className = 'review-row';
      row.innerHTML = `<h5>基于大纲 v${candidate.base_version} 的节点建议</h5><p class="review-note">${candidate.stale ? '当前大纲已变化，旧候选只读；请基于当前版本重新生成。' : esc(candidate.status)}</p>`;
      candidate.operations.forEach((op, index) => {
        const before = graph.nodes.find(x => x.id === (op.id || op.node?.id));
        const after = op.op === 'delete_node' ? null : op.op === 'add_node' ? op.node : {...before, ...op.changes};
        const describe = node => node ? `${node.title || '未命名节点'}\n目的：${node.purpose || '未填写'}\n范围：${node.scope || '未填写'}\n父节点：${node.parent_id || '根节点'}` : '（无此节点）';
        const detail = document.createElement('details'); detail.open = true;
        detail.innerHTML = `<summary>${{'add_node':'新增','update_node':'修改','delete_node':'删除'}[op.op] || '待核对'}：${esc(before?.title || after?.title || op.id || '')}</summary>
          <label><input type="checkbox" data-node-operation="${index}" checked ${candidate.stale || candidate.status !== 'candidate' ? 'disabled' : ''}>采纳这项修改</label>
          <div class="three-way"><section><h5>当前节点</h5><pre>${esc(describe(before))}</pre></section><section><h5>建议节点</h5><pre>${esc(describe(after))}</pre></section></div>`;
        row.append(detail);
      });
      if (candidate.status === 'candidate') {
        const actions = document.createElement('div'); actions.className = 'workbench-actions';
        for (const decision of ['partial_apply', 'reject']) {
          const button = document.createElement('button'); button.type = 'button';
          button.textContent = decision === 'reject' ? '拒绝此候选' : '仅采纳所选节点修改';
          button.disabled = decision !== 'reject' && (candidate.stale || !candidate.operations.length);
          const requestId = uid();
          button.onclick = () => action(button, $('#structuredSuggestionWorkbenchStatus'), async () => {
            if (!same(pid)) return;
            const selected = [...row.querySelectorAll('[data-node-operation]:checked')].map(x => Number(x.dataset.nodeOperation));
            if (decision !== 'reject' && !selected.length) throw new Error('至少选择一项修改；空选择不会应用整个候选。');
            await post(`/api/projects/${pid}/outline/candidates/${candidate.id}/decision`, {request_id: requestId, decision, base_version: candidate.base_version, selected_indices: selected});
            if (same(pid)) { await pp.refreshStructuredOutline?.(); await refreshOutlineCandidates(pid); $('#structuredSuggestionWorkbenchStatus').textContent = decision === 'reject' ? '已拒绝，大纲原版本保留。' : '只将选中的节点修改保存为大纲新版本。'; }
          });
          actions.append(button);
        }
        row.append(actions);
      }
      host.append(row);
    }
    if (!candidates.length) host.textContent = '暂无节点候选。先填写目标并预览，授权生成后逐项审阅。';
  }
  async function previewOutlineSuggestion(pid) {
    const instruction = $('#structuredOutlineInstruction').value.trim();
    if (!instruction) throw new Error('请先描述希望调整的章节结构。');
    const preview = await post(`/api/projects/${pid}/outline/suggestion-preview`, {request_id: uid(), instruction});
    if (!same(pid)) return;
    const host = $('#structuredOutlinePreview');
    host.innerHTML = `<p>接收方：${esc(preview.provider?.name || '无可用模型')}；大纲 v${preview.base_version}；费用未知。</p>
      <details><summary>查看本次发送的章节与指令</summary><pre>${esc(JSON.stringify(preview.outbound_preview, null, 2))}</pre></details>
      <p class="review-note">${esc(preview.provider_error || '结果只进入节点候选，不自动应用。')}</p>
      <label><input id="consentStructuredOutline" type="checkbox">授权本次发送并接受可能费用</label>
      ${preview.provider?.auth_mode === 'temporary' ? '<label>本次临时密钥<input type="password" id="structuredOutlineTemporaryKey" autocomplete="off"></label>' : ''}
      <button id="generateStructuredOutline" type="button" ${preview.generation_allowed ? '' : 'disabled'}>生成待审节点建议</button>`;
    const requestId = uid();
    bind('#generateStructuredOutline', '#structuredSuggestionWorkbenchStatus', async () => {
      if (!same(pid)) return;
      if (!$('#consentStructuredOutline').checked) throw new Error('请先勾选本次发送和费用授权。');
      if ($('#structuredOutlineInstruction').value.trim() !== instruction) throw new Error('调整目标已变化，请重新生成预览。');
      const input = $('#structuredOutlineTemporaryKey'); const temporaryKey = input?.value || ''; if (input) input.value = '';
      await post(`/api/projects/${pid}/outline/suggestions`, {request_id: requestId, instruction, preview_fingerprint: preview.preview_fingerprint, consent_to_send: true, temporary_key: temporaryKey});
      if (same(pid)) { host.replaceChildren(); await refreshOutlineCandidates(pid); $('#structuredSuggestionWorkbenchStatus').textContent = '节点建议已保存，请逐项比较后决定；原大纲尚未改变。'; }
    });
  }
  async function refreshCorpora(pid = id()) {
    if (!pid || !$('#corpusLineageChoice')) return;
    const token = ++corpusLoad;
    const paper = await pp.api(`/api/projects/${pid}`);
    if (!same(pid) || token !== corpusLoad) return;
    const select = $('#corpusLineageChoice'), old = select.value;
    select.innerHTML = '<option value="">选择语料</option>' + paper.corpus.map(x => `<option value="${esc(x.id)}">${esc(x.title)}</option>`).join('');
    if (paper.corpus.some(x => x.id === old)) select.value = old;
    $('#corpusLineageWorkbenchStatus').textContent = paper.corpus.length ? '选择语料后比较来源、校正和待处理更新。' : '先添加语料，再查看来源或人工校正。';
    if (select.value) await showLineage(pid, select.value);
  }
  async function showLineage(pid, corpusId) {
    const token = ++corpusLoad;
    const lineage = await pp.api(`/api/projects/${pid}/corpus/${corpusId}/lineage`);
    if (!same(pid) || token !== corpusLoad || $('#corpusLineageChoice').value !== corpusId) return;
    const current = lineage.current_source_version;
    const host = $('#corpusLineageView');
    host.innerHTML = `<p>当前原文 v${current?.version || '未知'}；权限：${esc(current?.permission_state || '未知')}；解析来源：${esc(current?.source_kind || '未登记')}（来源关联不等于全文已核验）</p>
      <details open><summary>当前原文与历史</summary>${lineage.source_versions.map(v => `<details><summary>原文 v${v.version}；${esc(v.source || '手工提供')}；${esc(v.permission_state)}</summary><pre>${esc(v.content || '内容受限或为空')}</pre></details>`).join('')}</details>
      <details><summary>确认文献来源关联</summary><p class="review-note">只建立来源身份，不自动合并资料，也不将手工语料升级为已核验证据。</p>
        <select id="corpusReferenceChoice" aria-label="选择来源文献">${lineage.reference_candidates.map(r => `<option value="${esc(r.id)}" data-version="${r.version}">${esc(r.title || r.cite_key)}；${esc(r.match_reason)}</option>`).join('')}</select>
        <button id="confirmCorpusReference" type="button" ${lineage.reference_candidates.length ? '' : 'disabled'}>确认关联此来源版本</button>
        <p>${lineage.source_links.map(r => `${esc(r.cite_key)}：原文 v${r.source_version} → 文献 v${r.reference_version}`).join('；') || '尚无已确认关联'}</p></details>
      <details><summary>保存人工校正或注释</summary><label>形式<select id="corpusCurationKind"><option value="manual">人工校正</option><option value="annotation">人工注释</option></select></label>
        <label>人工内容<textarea id="corpusCurationContent"></textarea></label><label>理由<input id="corpusCurationNotes"></label><button id="saveCorpusCuration" type="button">独立保存，不改原文</button></details>
      <div id="corpusCurationVersions"></div>
      <details><summary>预览来源更新</summary><label>新原文<textarea id="corpusRefreshContent"></textarea></label><label>来源链接或说明<input id="corpusRefreshSource"></label>
        <label>来源授权<select id="corpusRefreshPermission"><option value="unknown">尚未核验</option><option value="authorized">已确认有权使用</option><option value="revoked">授权已撤销</option></select></label><button id="previewCorpusRefresh" type="button">保存待审更新，不替换原文</button></details>
      <div id="corpusRefreshCandidates"></div><div id="corpusMigrationCandidates"></div>`;
    const url = `/api/projects/${pid}/corpus/${corpusId}`;
    const reload = async () => { if (same(pid) && $('#corpusLineageChoice').value === corpusId) await showLineage(pid, corpusId); };
    bind('#confirmCorpusReference', '#corpusLineageWorkbenchStatus', async () => {
      if (!same(pid)) return;
      const selected = $('#corpusReferenceChoice').selectedOptions[0];
      await post(url+'/source-links', {request_id: uid(), source_version: current.version, reference_id: selected.value, reference_version: Number(selected.dataset.version)}); await reload();
    });
    bind('#saveCorpusCuration', '#corpusLineageWorkbenchStatus', async () => {
      if (!same(pid)) return;
      await post(url+'/curations', {base_source_version: current.version, content: $('#corpusCurationContent').value, notes: $('#corpusCurationNotes').value, kind: $('#corpusCurationKind').value}); await reload();
    });
    bind('#previewCorpusRefresh', '#corpusLineageWorkbenchStatus', async () => {
      if (!same(pid)) return;
      await post(url+'/refresh', {request_id: uid(), base_source_version: current.version, content: $('#corpusRefreshContent').value, source: $('#corpusRefreshSource').value, permission_state: $('#corpusRefreshPermission').value}); await reload();
    });
    const curations = $('#corpusCurationVersions');
    for (const item of lineage.curation_versions) {
      const row = document.createElement('details');
      row.innerHTML = `<summary>人工版本 v${item.version}，绑定原文 v${item.source_version}${item.source_stale ? '（旧原文）' : ''}</summary><pre>${esc(item.content || '内容受限或为空')}</pre><p>${esc(item.notes)}</p>`;
      if (item.source_stale && item.permission_available && current.permission_state === 'authorized') {
        row.insertAdjacentHTML('beforeend', `<label>迁移到原文 v${current.version} 后的人工内容<textarea data-migration-content>${esc(item.content)}</textarea></label><button type="button" data-preview-migration>生成这一项的迁移预览</button>`);
        const button = row.querySelector('[data-preview-migration]');
        button.onclick = () => action(button, $('#corpusLineageWorkbenchStatus'), async () => {
          if (!same(pid)) return;
          await post(url+'/migrations', {request_id: uid(), curation_id: item.id, target_source_version: current.version, content: row.querySelector('[data-migration-content]').value, notes: item.notes}); await reload();
        });
      }
      curations.append(row);
    }
    const candidateList = $('#corpusRefreshCandidates');
    for (const candidate of lineage.refresh_candidates.filter(x => x.status === 'pending')) {
      const row = document.createElement('article'); row.className = 'review-row';
      row.innerHTML = `<h5>待审原文更新（基于 v${candidate.base_source_version}）</h5><pre>${esc(candidate.proposed_content)}</pre><p>授权：${esc(candidate.permission_state)}；确认前原文不变。</p>`;
      for (const decision of ['accept', 'reject']) {
        const button = document.createElement('button'); button.type = 'button'; button.textContent = decision === 'accept' ? '确认更新原文' : '拒绝此更新';
        button.disabled = decision === 'accept' && candidate.permission_state !== 'authorized';
        button.onclick = () => action(button, $('#corpusLineageWorkbenchStatus'), async () => { if (same(pid)) { await post(url+`/refresh/${candidate.id}/decision`, {request_id: uid(), decision}); await reload(); } });
        row.append(button);
      }
      candidateList.append(row);
    }
    const migrations = $('#corpusMigrationCandidates');
    for (const candidate of lineage.migration_candidates) {
      const row = document.createElement('article'); row.className = 'review-row';
      row.innerHTML = `<h5>校正版迁移：原文 v${candidate.from_source_version} → v${candidate.target_source_version}</h5><p>${esc(candidate.status)}${candidate.stale ? '；目标原文又已更新，请重新预览' : ''}${!candidate.permission_available ? '；来源权限不可用，内容已隐藏' : ''}</p><pre>${esc(candidate.proposed_content)}</pre>`;
      if (candidate.status === 'pending') for (const decision of ['accept', 'reject']) {
        const button = document.createElement('button'); button.type = 'button'; button.textContent = decision === 'accept' ? '只采纳这项迁移' : '拒绝这项迁移';
        button.disabled = decision === 'accept' && (candidate.stale || !candidate.permission_available);
        const requestId = uid(); button.onclick = () => action(button, $('#corpusLineageWorkbenchStatus'), async () => { if (same(pid)) { await post(url+`/migrations/${candidate.id}/decision`, {request_id: requestId, decision}); await reload(); } });
        row.append(button);
      }
      migrations.append(row);
    }
  }
  bind('#refreshTermReview', '#termReviewWorkbenchStatus', refreshTerms);
  bind('#previewTermPromotion', '#termReviewWorkbenchStatus', previewPromotion);
  bind('#refreshSharedResources', '#sharedResourceWorkbenchStatus', refreshShared);
  bind('#addSharedResource', '#sharedResourceWorkbenchStatus', addShared);
  bind('#compareEditorDraft', '#editorRecoveryWorkbenchStatus', compareDraft);
  bind('#refreshStructuredCandidates', '#structuredSuggestionWorkbenchStatus', refreshOutlineCandidates);
  bind('#previewStructuredOutline', '#structuredSuggestionWorkbenchStatus', previewOutlineSuggestion);
  bind('#refreshCorpusLineage', '#corpusLineageWorkbenchStatus', refreshCorpora);
  if ($('#corpusLineageChoice')) $('#corpusLineageChoice').onchange = () => {
    const pid = id(), value = $('#corpusLineageChoice').value;
    if (value) showLineage(pid, value).catch(error => { if (same(pid)) $('#corpusLineageWorkbenchStatus').textContent = message(error); });
    else $('#corpusLineageView').replaceChildren();
  };
  const refresh = () => {
    if (!id()) return;
    refreshTerms().catch(error => { $('#termReviewWorkbenchStatus').textContent = message(error); });
    refreshShared().catch(error => { $('#sharedResourceWorkbenchStatus').textContent = message(error); });
    refreshOutlineCandidates().catch(error => { $('#structuredSuggestionWorkbenchStatus').textContent = message(error); });
    refreshCorpora().catch(error => { $('#corpusLineageWorkbenchStatus').textContent = message(error); });
  };
  document.addEventListener('paperpilot:project-changed', () => {
    ++terminologyLoad; ++sharedLoad; ++outlineLoad; ++corpusLoad;
    ['#termPromotionPreview', '#termClassificationPreview', '#editorDraftComparison', '#structuredOutlinePreview', '#structuredOutlineCandidates', '#corpusLineageView'].forEach(selector => $(selector)?.replaceChildren());
    $('#structuredOutlineInstruction').value = '';
    refresh();
  });
  let resourceRefreshTimer;
  document.addEventListener('paperpilot:resources-changed', () => { clearTimeout(resourceRefreshTimer); resourceRefreshTimer=setTimeout(refresh,120); });
  pp.refreshResourceWorkbench = refresh;
  if (id()) refresh();
})();
