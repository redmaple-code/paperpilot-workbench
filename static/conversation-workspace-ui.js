/* The conversation is the workspace. Existing live tools open as paper artifacts. */
(() => {
  const pp = window.paperpilot;
  const $ = selector => document.querySelector(selector);
  if (!pp || pp.standaloneOutline || pp.standaloneTerms || !$('#agentChat')) return;
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const button = (text, action) => {
    const element = node('button', text); element.type = 'button'; element.onclick = action; return element;
  };
  const workspace = $('.workspace'), sidebar = $('.sidebar'), assistant = $('#workspaceAssistant');
  const chat = $('#agentChat'), center = $('.ws-center'), header = $('body > header');
  const originalOpen = pp.openWorkspace;
  document.body.classList.add('conversation-first');
  assistant.setAttribute('role', 'main');
  assistant.setAttribute('aria-label', '论文对话工作区');
  chat.tabIndex = -1;
  $('.ws-skip-link').href = '#agentChat';
  $('.ws-skip-link').textContent = '跳到论文对话';

  const brand = header.querySelector('.brand');
  sidebar.prepend(brand);
  const newPaper = $('#newProject');
  brand.after(newPaper);
  const conversations = node('section', undefined, 'cf-conversations');
  conversations.setAttribute('aria-label', '论文对话列表');
  conversations.append($('#agentRoutes'), $('#agentOtherConversations'));
  $('.ws-paper-switcher').after(conversations);

  const paperTitle = node('div', undefined, 'cf-paper-title');
  paperTitle.append($('#projectName'));
  header.querySelector('.ws-mobile-toggle').after(paperTitle);
  $('.ws-assist-toggle').hidden = true;
  const menu = node('details', undefined, 'cf-artifact-menu');
  menu.id = 'conversationArtifacts';
  menu.append(node('summary', '论文成果'));
  const menuItems = node('div'); menuItems.setAttribute('role', 'group'); menuItems.setAttribute('aria-label', '论文成果');
  const appTitle = document.title;
  const artifactNames = { research:'研究图谱', write:'论文稿件', outline:'章节大纲', knowledge:'资料与文献', visual:'论文图像', preview:'PDF 预览', history:'版本与诊断', settings:'论文设置', planning:'论文进展' };
  const artifactPaths = { research:'planning/startup', write:'write', outline:'outline/nodes', knowledge:'knowledge', visual:'visual/library', preview:'preview', history:'history', settings:'settings/paper', planning:'planning/progress' };
  const routeTitles = { write:'论文正文', 'planning/startup':'研究规划', 'knowledge/graph':'论证图',
    'knowledge/terms':'术语库', 'knowledge/corpus':'语料库', 'knowledge/references':'参考文献', 'visual/library':'图像库',
    'visual/results':'结果数据', 'visual/result-library':'结果与图表', 'visual/result-detail':'结果详情' };
  for (const kind of ['research', 'write', 'outline', 'knowledge', 'visual', 'preview']) {
    const item = button(artifactNames[kind], () => { menu.open = false; openArtifact(kind); });
    item.dataset.artifact = kind; menuItems.append(item);
  }
  menu.append(menuItems); header.append(menu);
  const footer = node('div', undefined, 'cf-sidebar-footer');
  footer.append(button('模型设置', () => pp.openAgentModelSettings?.()));
  const tools = node('details'); tools.append(node('summary', '更多工具'));
  const toolList = node('div');
  for (const kind of ['settings', 'history', 'planning']) toolList.append(button(artifactNames[kind], () => { tools.open = false; openArtifact(kind); }));
  tools.append(toolList); footer.append(tools); sidebar.append(footer);

  const artifact = node('section', undefined, 'cf-artifact'); artifact.id = 'paperArtifact'; artifact.hidden = true;
  artifact.setAttribute('aria-label', '论文成果工作区');
  const artifactHeader = node('div', undefined, 'cf-artifact-heading');
  const artifactTitle = node('h2', '论文成果'); artifactTitle.id = 'paperArtifactTitle';
  const expand = button('展开', () => {
    const expanded = workspace.classList.toggle('cf-artifact-expanded');
    expand.textContent = expanded ? '并排查看' : '展开'; expand.setAttribute('aria-pressed', String(expanded));
  });
  expand.id = 'expandPaperArtifact'; expand.setAttribute('aria-pressed', 'false');
  const close = button('返回对话', closeArtifact); close.id = 'closePaperArtifact';
  artifactHeader.append(artifactTitle, expand, close); artifact.append(artifactHeader, center); workspace.append(artifact);

  const welcome = node('section', undefined, 'cf-welcome'); welcome.id = 'conversationWelcome';
  welcome.append(node('h1', '一起把论文写下去'), node('p', '说说你的想法，或从已有材料开始。'));
  const prompts = node('div', undefined, 'cf-prompts');
  function renderPrompts(conversation) {
    const key = conversation?.module_key || 'paper';
    const questions = {
      paper:['从一个研究想法开始', '梳理文献中的问题与机会', '帮我讨论论文结构'],
      research:['建立领域知识图谱', '讨论一个可检验的研究问题', '核查最近邻工作与创新切口'],
      outline:['梳理章节目标与结构', '检查章节之间的论证衔接', '根据已有方案调整大纲'],
      writing:['讨论这一段如何展开', '根据现有材料起草正文', '帮我润色当前选区']
    };
    prompts.replaceChildren();
    for (const text of questions[key] || ['整理当前材料与问题', '讨论这一部分的改进方向', '给出下一步可执行建议'])
      prompts.append(button(text, () => pp.openModuleAgent?.(key, text)));
  }
  renderPrompts();
  welcome.append(prompts); $('#agentThread').before(welcome);
  const outputs = node('div', undefined, 'cf-output-links'); outputs.id = 'conversationOutputs'; outputs.hidden = true;
  $('.agent-compose').before(outputs);
  const composerTools = node('div', undefined, 'cf-composer-tools');
  const attach = button('添加资料', () => openArtifact('knowledge', 'knowledge/corpus'));
  attach.id = 'conversationAttach';
  const model = button('配置模型', () => pp.openAgentModelSettings?.()); model.id = 'conversationModel';
  composerTools.append(attach, model);
  $('.agent-compose-actions').prepend(composerTools);
  const send = $('#agentSend'); send.setAttribute('aria-label', '发送'); send.title = '发送（Ctrl+Enter）';
  send.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M12 19V5m-6 6 6-6 6 6"/></svg>';
  $('#agentInput').rows = 3;
  let target = '', routing = false, pendingArtifact = '';
  const outputKinds = new Set();
  const overlaySize = matchMedia('(max-width: 1100px)');
  function syncArtifactAccess() { assistant.inert = !!target && overlaySize.matches; }
  overlaySize.addEventListener('change', syncArtifactAccess);

  function closeLibrary() {
    $('.shell').classList.remove('ws-library-open');
    $('.ws-mobile-toggle').setAttribute('aria-expanded', 'false');
  }
  function showChat(focus = true) {
    workspace.classList.remove('ws-assistant-open', 'ws-assistant-collapsed');
    closeLibrary();
    if (focus) $('#agentInput').focus();
  }
  pp.showAgentSidebar = () => { if (overlaySize.matches && target) closeArtifact(); else showChat(); };
  function closeArtifact() {
    target = ''; artifact.hidden = true;
    document.title = appTitle;
    syncArtifactAccess();
    workspace.classList.remove('cf-artifact-open', 'cf-artifact-expanded', 'cf-research-artifact', 'cf-result-artifact');
    expand.textContent = '展开'; expand.setAttribute('aria-pressed', 'false');
    routing = true; originalOpen('chat'); routing = false;
    showChat();
  }
  function reveal(path, kind) {
    if (kind === 'outline') {
      location.assign(pp.paperWorkspaceUrl('/outline', path));
      return;
    }
    target = kind; artifact.hidden = false;
    const name = routeTitles[path.split('/').slice(0, 2).join('/')] || artifactNames[kind] || '论文成果';
    artifactTitle.textContent = name;
    document.title = `${name} | ${appTitle}`;
    workspace.classList.add('cf-artifact-open'); workspace.classList.toggle('cf-research-artifact', kind === 'research');
    workspace.classList.toggle('cf-result-artifact', path.startsWith('visual/result-'));
    window.history.replaceState(null, '', '#chat/' + path);
    closeLibrary();
    syncArtifactAccess(); close.focus();
    document.dispatchEvent(new CustomEvent('paperpilot:artifact-opened', {detail:{path}}));
  }
  async function openArtifact(kind, path = artifactPaths[kind]) {
    if (!path) return closeArtifact();
    if (kind === 'innovations') {
      const pid = pp.current?.project?.id;
      if (pid) location.assign('/innovations?' + new URLSearchParams({project: pid}));
      return;
    }
    if (path === 'visual/results' && !pp.resultModuleView) path = 'visual/result-library';
    if (kind === 'outline') {
      reveal(path, kind);
      return;
    }
    const pid = pp.current?.project?.id;
    if (kind === 'research' && pp.selectStartupStage && pp.data?.workflow_enabled) {
      if (!await pp.selectStartupStage('STAGE-01') || pid !== pp.current?.project?.id) return;
    }
    routing = true; originalOpen(path); routing = false; reveal(path, kind);
  }
  pp.openPaperArtifact = openArtifact;
  pp.openWorkspace = path => {
    if (path === 'chat') return closeArtifact();
    const name = path.split('/')[0];
    return openArtifact(name, path);
  };
  pp.openFirstPlanningStage = () => { closeArtifact(); return true; };
  // Existing modules can navigate through closures as well as the public entry.
  document.addEventListener('paperpilot:view-changed', event => {
    if (routing) return;
    const name = event.detail?.view;
    if (name && name !== 'chat') reveal(location.hash.slice(1), name);
  });
  function messagesChanged() {
    const empty = !$('#agentThread').children.length;
    welcome.hidden = !empty; assistant.classList.toggle('cf-empty-chat', empty);
  }
  new MutationObserver(messagesChanged).observe($('#agentThread'), { childList:true }); messagesChanged();
  function addOutput(kind, label) {
    if (outputKinds.has(kind)) return;
    outputKinds.add(kind); outputs.hidden = false;
    const item = button(label, () => openArtifact(kind)); item.dataset.output = kind; outputs.append(item);
  }
  document.addEventListener('paperpilot:research-updated', event => {
    if (event.detail?.projectId === pp.current?.project?.id) addOutput('research', '查看研究图谱');
  });
  document.addEventListener('paperpilot:draft-applied', event => {
    if (event.detail?.projectId === pp.current?.project?.id) addOutput('write', '查看论文稿件');
  });
  document.addEventListener('paperpilot:resource-updated', event => {
    if (event.detail?.project_id !== pp.current?.project?.id) return;
    const kind = {outline:'outline', logic:'outline', term:'knowledge', corpus:'knowledge', reference:'knowledge', planning:'research'}[event.detail.kind];
    if (kind) addOutput(kind, '查看' + artifactNames[kind]);
    document.dispatchEvent(new CustomEvent('paperpilot:agent-context-changed'));
  });
  document.addEventListener('paperpilot:agent-selected', event => {
    const conversation = event.detail?.conversation;
    welcome.querySelector('h1').textContent = conversation?.parent_id ? '一起梳理' + conversation.title : '一起把论文写下去';
    renderPrompts(conversation);
    showChat(false);
  });
  document.addEventListener('paperpilot:agent-preview', event => {
    if (event.detail?.projectId !== pp.current?.project?.id) return;
    const preview = event.detail.preview;
    model.textContent = preview.generation_allowed ? preview.provider.model : '配置模型';
  });
  document.addEventListener('paperpilot:project-changed', () => {
    outputKinds.clear(); outputs.replaceChildren(); outputs.hidden = true;
    if (target && !pendingArtifact) closeArtifact();
  });
  document.addEventListener('paperpilot:project-loaded', () => {
    if (pendingArtifact) {
      const saved = pendingArtifact; pendingArtifact = ''; restoreLocation(saved);
    }
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') {
      menu.open = false; tools.open = false;
      if (!document.querySelector('dialog[open]') && target) closeArtifact();
    }
  });
  document.addEventListener('click', event => { if (!menu.contains(event.target)) menu.open = false; });
  function restoreLocation(path = location.hash.slice(1)) {
    if (path === 'outline' || path.startsWith('outline/') || path === 'visual/results') path = 'chat/' + path;
    if (path.startsWith('chat/')) {
      if (!pp.current?.project?.id) { pendingArtifact = path; return; }
      const saved = path.slice(5), name = saved.split('/')[0];
      if (name === 'outline') {
        location.replace(pp.paperWorkspaceUrl('/outline', saved));
        return;
      }
      openArtifact(name === 'planning' && saved === 'planning/startup' ? 'research' : name, saved);
    } else closeArtifact();
  }
  window.addEventListener('hashchange', () => restoreLocation());
  restoreLocation(pp.initialWorkspaceLocation || location.hash.slice(1));
  pp.initialWorkspaceLocation = ''; 
})();
