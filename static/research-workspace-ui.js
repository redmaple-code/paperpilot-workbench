/* Stage one shares the persistent Agent and the native scholar research store. */
(() => {
  const pp = window.paperpilot;
  const planner = document.querySelector('#startupPlanner');
  if (!pp || !planner) return;
  const $ = selector => document.querySelector(selector);
  const element = (tag, text, className) => {
    const node = document.createElement(tag);
    if (text !== undefined) node.textContent = text;
    if (className) node.className = className;
    return node;
  };
  const panel = element('section', undefined, 'research-workspace');
  panel.id = 'researchWorkspace';
  panel.hidden = true;
  panel.innerHTML = `<header class="research-heading"><div><span class="research-eyebrow">RESEARCH / DISCOVERY</span>
    <h2>从一个问题，展开你的研究</h2><p>与学徒讨论领域、追问机制，让知识、路线和证据逐步形成结构。</p></div>
    <div class="research-connection"><span id="researchSkillStatus" role="status">正在连接研究工作区</span><span>ReAct Agent · 持久会话</span></div></header>
    <div class="research-columns"><section id="researchActions" aria-label="研究对话快捷入口"><button id="openResearchAgent" type="button">与研究子 Agent 讨论</button>
    <div class="research-starters"><span>可以从这里开始</span><button type="button">帮我建立这个领域的知识地图</button><button type="button">梳理代表工作与尚未解决的问题</button><button type="button">从已有材料寻找可验证的创新切口</button></div>
    <p id="researchModelStatus" role="status"></p><button id="researchConfigureModel" type="button" hidden>配置研究模型</button></section>
    <section class="research-map" aria-label="研究结构可视化"><div class="research-map-toolbar"><div role="group" aria-label="研究图视图"><button type="button" data-research-view="knowledge" aria-pressed="true">知识与证据</button><button type="button" data-research-view="routes" aria-pressed="false">研究路线</button></div><button id="researchFit" type="button">适应视图</button></div>
    <div id="researchGraphBoard" role="region" aria-label="可点击的研究架构图"></div>
    <div id="researchGraphEmpty"><span class="research-empty-mark">＋</span><h3>研究结构会在对话中生长</h3><p>助手会将概念、机制、假设与证据缺口连接起来。点击节点可查看条件和来源。</p></div>
    <div id="researchNodeList" aria-label="研究节点列表"></div><section id="researchNodeDetail" aria-label="所选研究节点详情" hidden></section>
    <footer><span id="researchGraphStatus" role="status">尚未形成研究图</span><span>候选知识 · 科学结论需证据核验</span></footer></section></div>
    <section id="researchPlanSummary" aria-label="当前研究方案"><div class="research-summary-heading"><h3>当前研究方案</h3><div><button id="researchAdoptDraft" type="button" disabled>采纳图中研究方案</button><button id="researchRevise" type="button" hidden>修订研究方案</button></div></div><div id="researchSummaryFields"></div></section>`;
  $('#startupFormArea').prepend(panel);
  let view = null;
  let snapshot = null;
  let active = false;
  let projectId = '';
  let request = 0;
  let graph = null;
  let mapView = 'knowledge';
  let selected = '';
  let items = new Map();
  let controlState = { busy: false, actions: [] };
  const kinds = { concept: '核心概念', claim: '条件命题', method: '研究方法', practice: '操作经验',
    observation: '现象与缺口', resource: '证据材料', goal: '研究目标', route: '机制路线', plan: '判别方案', task: '研究任务' };
  const relations = { requires_understanding: '前置知识', supports: '支持', challenges: '反证', explains: '解释',
    contrasts_with: '对比', operationalizes: '实现', evaluated_by: '评估', applies_to: '适用', related_to: '关联', implies: '条件蕴含' };
  const currentStage = () => view?.plan.stages.find(stage => stage.id === 'STAGE-01');
  const inStage = () => !!pp.data?.workflow_enabled && $('.workspace')?.dataset.view === 'planning'
    && ['#planning/startup','#chat/planning/startup'].includes(window.location.hash) && pp.selectedStartupStage === 'STAGE-01';

  function details(identity) {
    selected = identity;
    const item = items.get(identity);
    const host = $('#researchNodeDetail');
    host.replaceChildren();
    host.hidden = !item;
    if (!item) return;
    host.append(element('span', kinds[item.kind] || '研究节点', 'research-eyebrow'), element('h3', item.title),
      element('p', item.content || item.rationale || item.spec?.question || item.acceptance || '待与助手进一步展开'));
    if (item.conditions?.length || item.scope || item.spec?.conditions) {
      host.append(element('h4', '适用条件'), element('p', [...(item.conditions || []),
        ...Object.entries(item.scope || item.spec?.conditions || {}).map(([key, value]) => `${key} · ${value}`)].join(' / ')));
    }
    if (item.spec?.method) host.append(element('h4', '判别动作'), element('p', item.spec.method), element('p', item.spec.acceptance));
    const sources = new Map((snapshot?.graph.sources || []).map(source => [source.id, source]));
    host.append(element('h4', '证据与来源'));
    if (!item.evidence?.length) host.append(element('p', '来源与论证待补充，此节点保留为候选。'));
    for (const reference of item.evidence || []) {
      const source = sources.get(reference.source_id);
      host.append(element('p', `${source?.title || reference.source_id} · ${reference.locator}`));
      if (source?.kind === 'search_pointer') host.append(element('small', '实际检索指针，当前仅有元数据，全文支持关系待核查。'));
    }
    $('#researchNodeList').querySelectorAll('button').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.node === identity)));
    const discuss=element('button','围绕这个节点讨论');discuss.type='button';
    discuss.onclick=()=>pp.openModuleAgent?.('research',`请围绕研究节点「${item.title}」（${identity}）继续分析，核对来源、适用条件和下一步验证。`);
    host.append(discuss);
  }
  pp.focusResearchNode=identity=>{if(items.has(identity))details(identity);};

  function renderGraph() {
    if (!active || !snapshot) return;
    const board = $('#researchGraphBoard');
    const nodes = mapView === 'routes' ? snapshot.routes : snapshot.graph.units || [];
    items = new Map(nodes.map(item => [item.id, item]));
    $('#researchGraphEmpty').hidden = nodes.length > 0;
    board.hidden = nodes.length === 0;
    const list = $('#researchNodeList');
    list.replaceChildren();
    for (const item of nodes) {
      const button = element('button', item.title);
      button.type = 'button'; button.dataset.node = item.id;
      button.setAttribute('aria-pressed', String(selected === item.id));
      button.onclick = () => details(item.id); list.append(button);
    }
    if (window.X6 && nodes.length) {
      if (!graph) {
        graph = new X6.Graph({ container: board, width: board.clientWidth || 500, height: 430,
          interacting: { nodeMovable: false }, panning: true,
          mousewheel: { enabled: true, modifiers: ['ctrl', 'meta'], minScale: 0.35, maxScale: 1.6 },
          background: { color: '#fafafa' }, grid: { visible: true, size: 18, type: 'dot', args: { color: '#dddddd' } } });
        graph.on('node:click', ({ node }) => details(node.id));
        new ResizeObserver(() => {
          if (active && board.clientWidth) { graph.resize(board.clientWidth, 430); graph.zoomToFit({ padding: 30, maxScale: 1 }); }
        }).observe(board);
      }
      graph.clearCells();
      const depths = new Map();
      const depth = item => {
        if (depths.has(item.id)) return depths.get(item.id);
        let value = 0;
        let parent = items.get(item.parent_id);
        const seen = new Set([item.id]);
        while (parent && !seen.has(parent.id)) { seen.add(parent.id); value++; parent = items.get(parent.parent_id); }
        depths.set(item.id, value); return value;
      };
      const rows = new Map();
      nodes.forEach((item, index) => {
        const column = mapView === 'routes' ? depth(item) : index % 3;
        const row = rows.get(column) || 0; rows.set(column, row + 1);
        graph.addNode({ id: item.id, x: column * 220 + 20, y: row * 125 + 20, width: 190, height: 86,
          attrs: { body: { fill: item.kind === 'goal' ? '#181818' : '#ffffff', stroke: '#b8b8b8', rx: 8, ry: 8 },
            label: { text: `${kinds[item.kind] || '研究节点'}\n${item.title}`, fill: item.kind === 'goal' ? '#ffffff' : '#181818',
              fontSize: 12, lineHeight: 20, textWrap: { width: -20, height: -16, ellipsis: true } } } });
      });
      const edges = mapView === 'routes' ? nodes.filter(item => item.parent_id).map(item => ({ source: item.parent_id, target: item.id, type: '路线分解' })) : snapshot.graph.edges || [];
      edges.filter(edge => items.has(edge.source) && items.has(edge.target)).forEach(edge => graph.addEdge({ source: edge.source, target: edge.target,
        attrs: { line: { stroke: '#999999', strokeWidth: 1.3, targetMarker: { name: 'classic', width: 7, height: 7 } } },
        labels: [{ attrs: { label: { text: relations[edge.type] || edge.type, fontSize: 10, fill: '#626262' } } }] }));
      requestAnimationFrame(() => { if (active && board.clientWidth) { graph.resize(board.clientWidth, 430); graph.zoomToFit({ padding: 30, maxScale: 1 }); } });
    }
    if (selected && items.has(selected)) details(selected);
    else $('#researchNodeDetail').hidden = true;
    $('#researchGraphStatus').textContent = `知识图 v${snapshot.revision} · ${snapshot.graph.units?.length || 0} 个知识节点 · ${snapshot.routes.length} 个路线节点`;
  }

  function renderSummary() {
    const host = $('#researchSummaryFields'); host.replaceChildren();
    const saved = currentStage()?.fields || {};
    const shown = Object.values(saved).some(value => value.trim()) ? saved : snapshot?.graph.stage_draft || {};
    $('#researchPlanSummary').hidden = !Object.values(shown).some(value => value.trim());
    for (const [key, label] of Object.entries({ research_question: '研究问题', contribution: '创新候选与可证伪假设', scope: '研究边界' })) {
      const block = element('div'); block.append(element('h4', label), element('p', shown[key] || '与助手讨论后形成，经采纳进入规划草稿。')); host.append(block);
    }
    const draft = snapshot?.graph.stage_draft;
    $('#researchRevise').hidden = currentStage()?.status !== 'confirmed';
    $('#researchRevise').disabled = !!pp.current?.project?.archived || pp.current?.project?.lifecycle_state === 'published';
    $('#researchAdoptDraft').disabled = !draft || !Object.values(draft).some(value => value.trim()) || !view?.stage_actions?.['STAGE-01']?.includes('save')
      || Object.entries(draft).every(([key, value]) => saved[key] === value);
    pp.updateResearchControls?.();
  }

  async function refresh() {
    const pid = pp.current?.project?.id;
    if (!active || !pid) return;
    const token = ++request;
    try {
      const result = await pp.api(`/api/projects/${pid}/agent/research`);
      if (token !== request || pid !== pp.current?.project?.id || !active) return;
      snapshot = result;
      $('#researchSkillStatus').textContent = result.skill.available ? 'scholar-apprentice 已连接' : '学徒研究接口待配置';
      renderGraph(); renderSummary();
    } catch (error) {
      if (token === request && pid === pp.current?.project?.id) $('#researchGraphStatus').textContent = '研究图读取暂未完成，请稍后重试。';
    }
  }

  function position() {
    const next = inStage();
    const pid = pp.current?.project?.id || '';
    const changed = next !== active || pid !== projectId;
    active = next; projectId = pid;
    $('.workspace').classList.toggle('research-mode', active);
    planner.classList.toggle('research-planner', active);
    panel.hidden = !active;
    $('#reviewStartup').textContent = active ? '提交研究方案审阅' : '提交审阅';
    $('#confirmStartup').textContent = active ? '确认研究方案' : '确认当前阶段';
    if (active) {
      $('#workspaceViewTitle').textContent = '研究问题与创新';
      $('#workspaceViewHint').textContent = 'AI 对话 · 知识结构 · 研究发现';
      renderSummary();
      if (changed) refresh();
    }
  }

  pp.renderResearchWorkspace = startup => { view = startup; position(); };
  pp.updateResearchControls = state => {
    if (state) controlState = state;
    if (!active) return;
    const complete = ['research_question', 'scope'].every(key => currentStage()?.fields[key]?.trim());
    $('#reviewStartup').disabled = controlState.busy || !controlState.actions.includes('review') || !complete
      || !!pp.current?.project?.archived || pp.current?.project?.lifecycle_state === 'published';
    $('#reviewStartup').title = complete ? '审阅已采纳的研究方案' : '先与助手形成研究方案，再采纳到规划草稿';
  };
  for (const name of ['paperpilot:view-changed', 'paperpilot:stage-selected']) document.addEventListener(name, position);
  document.addEventListener('paperpilot:project-changed', () => {
    snapshot = null; view = null; selected = ''; request++; items.clear(); graph?.clearCells();
    $('#researchNodeList').replaceChildren(); $('#researchNodeDetail').hidden = true;
    $('#researchGraphBoard').hidden = true; $('#researchGraphEmpty').hidden = false;
    $('#researchSkillStatus').textContent = '正在连接研究工作区'; position();
  });
  document.addEventListener('paperpilot:research-updated', event => { if (event.detail?.projectId === projectId) refresh(); });
  document.addEventListener('paperpilot:agent-preview', event => {
    if (!active || event.detail?.projectId !== projectId) return;
    const preview = event.detail.preview;
    $('#researchModelStatus').textContent = preview.generation_allowed ? '' : preview.provider_error || '研究模型尚未就绪，请先登记并完成测试。';
    $('#researchConfigureModel').hidden = !!preview.generation_allowed;
  });
  $('#researchConfigureModel').onclick = () => pp.openAgentModelSettings?.();
  $('#openResearchAgent').onclick = () => pp.openModuleAgent?.('research');
  $('#researchRevise').onclick = () => $('#backStartup').click();
  $('#researchFit').onclick = () => graph?.zoomToFit({ padding: 30, maxScale: 1 });
  panel.querySelectorAll('[data-research-view]').forEach(button => button.onclick = () => {
    mapView = button.dataset.researchView; selected = '';
    panel.querySelectorAll('[data-research-view]').forEach(item => item.setAttribute('aria-pressed', String(item === button))); renderGraph();
  });
  panel.querySelectorAll('.research-starters button').forEach(button => button.onclick = () => {
    pp.openModuleAgent?.('research', button.textContent);
  });
  $('#researchAdoptDraft').onclick = () => {
    const pid = projectId, version = view.workflow_version, draft = { ...snapshot.graph.stage_draft };
    const dialog = element('dialog', undefined, 'research-adopt-dialog');
    dialog.append(element('h2', '将候选研究方案写入规划草稿'));
    for (const [key, text] of Object.entries(draft)) dialog.append(element('h3', ({ research_question: '研究问题', contribution: '创新候选', scope: '研究范围' })[key]), element('p', text));
    const save = element('button', '写入规划草稿'), cancel = element('button', '继续讨论');
    save.type = cancel.type = 'button'; dialog.append(save, cancel); document.body.append(dialog);
    const close = () => { dialog.close(); dialog.remove(); };
    cancel.onclick = close; dialog.addEventListener('cancel', close, { once: true });
    save.onclick = async () => {
      if (pid !== pp.current?.project?.id) { close(); return; }
      save.disabled = true;
      try {
        await pp.api(`/api/projects/${pid}/workflow/startup/commands`, { method: 'POST', body: JSON.stringify({
          request_id: crypto.randomUUID(), expected_workflow_version: version, command: 'save', stage_id: 'STAGE-01', fields: draft }) });
        if (pid === pp.current?.project?.id) await pp.refreshWorkflow(); close();
      } catch (error) { save.disabled = false; dialog.append(element('p', '草稿尚未确认写入，请关闭后刷新规划状态再核对。')); }
    };
    dialog.showModal();
  };
  position();
})();
