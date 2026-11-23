/* The workspace moves the existing live controls; their IDs and business handlers remain intact. */
(() => {
  const pp = window.paperpilot;
  if (!pp) return;
  const $ = selector => document.querySelector(selector);
  const move = (target, selector) => { const node = $(selector); if (node) target.append(node); return node; };
  const icon = name => {
    const paths = {
      write: '<path d="m15 4 5 5M4 20l5-1L20 8a2.1 2.1 0 0 0-5-3L4 16z"/>',
      outline: '<path d="M8 5h13M8 12h13M8 19h13M3 5h.01M3 12h.01M3 19h.01"/>',
      knowledge: '<path d="M3 4h6a4 4 0 0 1 3 2 4 4 0 0 1 3-2h6v16h-6a4 4 0 0 0-3 1 4 4 0 0 0-3-1H3zM12 6v15"/>',
      visual: '<rect x="3" y="3" width="18" height="18" rx="3"/><circle cx="8" cy="8" r="1.5"/><path d="m3 16 5-5 4 4 4-6 5 7"/>',
      preview: '<path d="M6 3h8l4 4v14H6zM14 3v5h4M9 12h6M9 16h6"/>',
      planning: '<path d="M8 4h12v16H4V8M8 2v4M16 2v4M8 11l2 2 5-5M8 17h8"/>',
      settings: '<path d="M3 6h18M3 12h18M3 18h18"/><circle cx="8" cy="6" r="2"/><circle cx="16" cy="12" r="2"/><circle cx="10" cy="18" r="2"/>',
      history: '<path d="M3 11a9 9 0 1 1 2 7M3 4v7h7M12 7v5l3 2"/>',
      menu: '<path d="M4 6h16M4 12h16M4 18h16"/>',
      assist: '<path d="M8 3v3M3 8h3M18 16v5M16 18h5M5 19l10-10 3 3L8 22zM14 4h6v6"/>',
      plus: '<path d="M12 5v14M5 12h14"/>'
    };
    return `<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round">${paths[name] || paths.write}</svg>`;
  };
  document.body.classList.add('studio');
  const standaloneOutline = location.pathname === '/outline';
  const standaloneTerms = location.pathname === '/terms';
  pp.standaloneOutline = standaloneOutline;
  pp.standaloneTerms = standaloneTerms;
  pp.paperWorkspaceUrl = (path, hash = '', project = pp.current?.project?.id) =>
    path + (project ? '?' + new URLSearchParams({project}) : '') + (hash ? '#' + hash : '');
  document.body.classList.toggle('desktop-outline', standaloneOutline);
  document.body.classList.toggle('desktop-terms', standaloneTerms);
  const shell = $('.shell'), workspace = $('.workspace'), sidebar = $('.sidebar');
  if (standaloneTerms) {
    workspace.classList.add('ws-assistant-collapsed');
    $('#autoPreviewBuild').checked = false;
  }
  if (standaloneOutline) {
    workspace.classList.add('ws-assistant-collapsed');
    $('#autoPreviewBuild').checked = false;
  }
  const header = $('body > header');
  header.querySelector('.brand').innerHTML = `<span class="ws-brand-mark">${icon('knowledge')}</span><strong>PaperPilot</strong><span class="ws-brand-caption">研究与写作</span>`;
  const libraryToggle = document.createElement('button');
  libraryToggle.className = 'ws-mobile-toggle'; libraryToggle.type = 'button';
  libraryToggle.innerHTML = icon('menu') + '<span>论文库</span>';
  libraryToggle.setAttribute('aria-controls', 'workspaceSidebar'); libraryToggle.setAttribute('aria-expanded', 'false');
  sidebar.id = 'workspaceSidebar'; header.prepend(libraryToggle);
  const assistToggle = document.createElement('button');
  assistToggle.type = 'button'; assistToggle.className = 'ws-assist-toggle';
  assistToggle.innerHTML = icon('assist') + '<span>论文 Agent</span>';
  assistToggle.setAttribute('aria-controls', 'workspaceAssistant'); assistToggle.setAttribute('aria-expanded', 'false');
  header.append(assistToggle);
  $('#newProject').innerHTML = icon('plus') + '新建论文';
  const tabs = $('.tabs');
  tabs.setAttribute('role', 'navigation'); tabs.setAttribute('aria-label', '工作区导航');
  // Sidebar reads top-down as a workflow: create button, workflow guide, tools,
  // current chapters, then the paper library.
  const guide = $('#wfGuide');
  const toolsHeading = document.createElement('h3');
  toolsHeading.textContent = '工具';
  toolsHeading.id = 'wsToolsHeading';
  if (guide && guide.isConnected) { guide.after(toolsHeading); toolsHeading.after(tabs); }
  else { $('#newProject').after(tabs); }
  const labels = {write:'正文编辑', outline:'章节大纲', knowledge:'知识与文献', visual:'图像工作台', preview:'PDF 阅读'};
  tabs.querySelectorAll('[data-tab]').forEach(button => {
    button.removeAttribute('role'); button.innerHTML = icon(button.dataset.tab) + `<span>${labels[button.dataset.tab]}</span>`;
  });
  function navButton(name, label) {
    const button = document.createElement('button'); button.type = 'button'; button.dataset.view = name;
    button.innerHTML = icon(name) + `<span>${label}</span>`; button.onclick = () => navigate(name);
    tabs.append(button); return button;
  }
  navButton('planning', '启动与流程'); navButton('history', '版本与诊断'); navButton('settings', '论文设置');
  const navigationLabel = document.createElement('h3');
  navigationLabel.className = 'ws-nav-label'; navigationLabel.textContent = '论文工作区';
  tabs.prepend(navigationLabel);
  const managementLabel = document.createElement('h3');
  managementLabel.className = 'ws-nav-label'; managementLabel.textContent = '项目管理';
  tabs.querySelector('[data-view="planning"]').before(managementLabel);
  $('#projectSearch').placeholder = '搜索你的论文';
  $('.side-note').textContent = '论文与资料保存在本机';
  const center = document.createElement('div'); center.className = 'ws-center'; center.id='workspaceMain';center.tabIndex=-1;workspace.append(center);
  const skip=document.createElement('a');skip.className='ws-skip-link';skip.href='#workspaceMain';skip.textContent='跳到当前工作区';document.body.prepend(skip);
  ['#readingWorkspace','#outline','#knowledge','#visual'].forEach(selector => move(center, selector));
  const views = {};
  function view(name, title, hint) {
    const section = document.createElement('section'); section.className = 'ws-view'; section.id=`workspace-${name}`;section.hidden = true; section.dataset.workspaceView = name;
    const heading = document.createElement('div'); heading.className = 'ws-view-heading';
    section.setAttribute('aria-label', title);
    const p = document.createElement('p'); p.textContent = hint; heading.append(p); section.append(heading);
    center.append(section); views[name] = section; return section;
  }
  const planning = view('planning','启动与流程','先规划研究与结构，再进入写作、审阅和发布。');
  move(planning,'#workflowDashboard');
  const settings = view('settings','论文设置','管理当前论文配置、模型绑定与文风偏好。');
  const history = view('history','版本与诊断','查看已保存的内容、修订记录、AI 任务与构建结果。');
  const assistant = document.createElement('aside'); assistant.id = 'workspaceAssistant'; assistant.className = 'ws-assistant'; assistant.setAttribute('aria-label','论文 Agent 侧栏'); workspace.append(assistant);
  assistant.innerHTML = `<div class="ws-assistant-heading"><h2>${icon('assist')}论文 Agent</h2><button type="button" id="closeWorkspaceAssistant">收起</button></div>`;
  const assistNav = document.createElement('div'); assistNav.className = 'ws-subnav'; assistNav.setAttribute('role','group'); assistNav.setAttribute('aria-label','辅助任务'); assistant.append(assistNav);
  const assistPages = {};
  [['ai','AI 辅助'],['continue','续写'],['revise','指定修改'],['recover','恢复']].forEach(([name,label], index) => {
    const button = document.createElement('button'); button.type='button'; button.textContent=label; button.setAttribute('aria-pressed',String(index===0)); assistNav.append(button);
    const section = document.createElement('div'); section.className='ws-assist-page'; section.hidden=index!==0; assistant.append(section); assistPages[name]=section;
    button.onclick=()=>{Object.entries(assistPages).forEach(([key,node])=>node.hidden=key!==name); [...assistNav.children].forEach(b=>b.setAttribute('aria-pressed',String(b===button)));};
  });
  const session = document.createElement('section'); session.className='ws-writing-session';
  session.hidden=true; assistPages.ai.append(session);
  move(session,'#workflowModeState'); move(session,'#workflowModeControls');
  assistant.insertBefore(session, assistNav);
  const writingStage = document.createElement('button'); writingStage.type = 'button';
  writingStage.textContent = '管理写作阶段'; writingStage.className = 'ws-text-action';
  writingStage.onclick = () => navigate('planning/progress'); session.append(writingStage);
  const actions = document.createElement('div'); actions.className='ws-ai-actions'; assistPages.ai.append(actions);
  document.querySelectorAll('#write .editor-toolbar [data-action]').forEach(node=>actions.append(node));
  const actionHints = {write:'依据大纲生成章节',companion:'围绕当前段落协作',follow:'延续选中文本的表达',continue:'接着当前内容展开',edit:'精修已选中的内容'};
  actions.querySelectorAll('button').forEach(button => {
    const hint = actionHints[button.dataset.action];
    if (!hint) return;
    const description = document.createElement('small'); description.textContent = hint;
    description.setAttribute('aria-hidden', 'true');
    button.append(description);
  });
  move(assistPages.ai,'#workflowAssistControls'); move(assistPages.ai,'#workflowContext');
  [['write', '自动写作'], ['companion', '自动伴写']].reverse().forEach(([value, label]) => {
    const option = document.createElement('option'); option.value = value; option.textContent = label;
    $('#workflowAssistAction').prepend(option);
  });
  $('#workflowAssistGenerate').textContent = '预览 AI 请求';
  move(assistPages.ai,'#workflowJobStatus'); move(assistPages.ai,'#workflowCandidate'); move(assistPages.ai,'#refreshWorkflow');
  move(assistPages.continue,'#continuationPanel'); move(assistPages.continue,'#continuationPlanWorkbench');
  move(assistPages.revise,'#targetedModification');
  move(assistPages.revise,'.targeted-modification');
  move(assistPages.recover,'.revision-recovery'); move(assistPages.recover,'#editorRecoveryWorkbench');
  const recoveryHint=document.createElement('p'); recoveryHint.className='muted'; recoveryHint.textContent='恢复前先比较当前内容与保存的副本，避免覆盖尚未合并的修改。'; assistPages.recover.prepend(recoveryHint);
  ['#workflowContentHistory','#workflowEventHistory','#workflowJobHistory'].forEach((selector,index)=>{
    const block=document.createElement('details'); block.open=index===0;
    const summary=document.createElement('summary'); summary.textContent=['内容版本','修订记录','AI 任务记录'][index]; block.append(summary); move(block,selector); history.append(block);
  });
  move(history,'.build-history');
  // Remove the now-empty headings left in the original dashboard after moving their live controls.
  document.querySelector('[aria-labelledby="revisionModeTitle"]')?.remove();
  document.querySelector('[aria-labelledby="candidateTitle"]')?.remove();
  $('.workflow-lower')?.remove();
  const knowledge=$('#knowledge'); const grid=knowledge.querySelector('.cards')||knowledge;
  const cards=[...grid.querySelectorAll(':scope > .card')];
  const taskGroups = new Map();
  function cardWith(selector){return $(selector)?.closest('.card');}
  function subpages(host, entries, navLabel) {
    const nav=document.createElement('div'); nav.className='ws-subnav'; nav.setAttribute('role','group'); nav.setAttribute('aria-label',navLabel); host.prepend(nav);
    const pages=[];
    const route = host.dataset.workspaceView || host.id;
    entries.forEach(([label,nodes,taskKey])=>{
      const button=document.createElement('button'); button.type='button'; button.textContent=label; nav.append(button);
      const key = taskKey || String(pages.length);
      button.dataset.task = key;
      const page=document.createElement('section'); page.className='ws-task-page'; page.id=`${host.id}-task-${pages.length}`;page.setAttribute('aria-label',label);button.setAttribute('aria-controls',page.id);page.hidden=pages.length>0; host.append(page);
      nodes.filter(Boolean).forEach(node=>page.append(node));
      const select=()=>{
        pages.forEach(item=>{item.page.hidden=item.page!==page;item.button.setAttribute('aria-pressed',String(item.page===page));});
        if (workspace.dataset.view === route) {
          window.history.replaceState(null, '', `#${route}/${key}`);
          if (!routing) document.dispatchEvent(new CustomEvent('paperpilot:view-changed', {detail: {view: route, task: key}}));
        }
      };
      pages.push({page,button,select,key}); button.onclick=select; button.setAttribute('aria-pressed',String(pages.length===1));
    });
    taskGroups.set(route, pages);
    return pages;
  }
  function disclosure(host, title, nodes) {
    const details = document.createElement('details'); details.className = 'ws-disclosure';
    const summary = document.createElement('summary'); summary.textContent = title;
    details.append(summary, ...nodes.filter(Boolean)); host.append(details);
    return details;
  }
  const configuration=cardWith('#configurationStatus'), lifecycle=$('#projectLifecycleCard'), shared=$('#sharedResourceWorkbench');
  const drawingProvider = $('.drawing-provider');
  const imageSettings = document.createElement('section'); imageSettings.className = 'ws-image-settings';
  const imageSettingsTitle = document.createElement('h2'); imageSettingsTitle.textContent = '图像模型';
  imageSettings.append(imageSettingsTitle, drawingProvider);
  drawingProvider.open = true;
  const settingPages=subpages(settings,[['论文配置',[configuration],'paper'],['AI 模型',[$('#providerSettings')],'models'],['图像模型',[imageSettings],'images'],['文风学习',[$('#styleLearningPanel')],'learning']],'设置类别');
  const preferences = $('.settings-governance-panel');
  preferences.querySelector('summary').textContent = '写作偏好与引用规则';
  const domainSelect = $('#domainSelect');
  const domainLabel = configuration.querySelector('label[for="domainSelect"]');
  const domainHint = $('#saveDomainBinding').nextElementSibling;
  disclosure(configuration, '绑定领域库', [domainLabel, domainSelect, $('#saveDomainBinding'), domainHint]);
  const legacySettings = disclosure(configuration, '兼容旧版论文配置', [
    $('#domain'), $('#style'), $('#model'), $('#aiBaseUrl'), $('#saveSettings'),
    configuration.querySelector('label[for="aiProfileSelect"]'), $('#aiProfileSelect'), $('#aiModelSelect'), $('#saveAiBinding')
  ]);
  legacySettings.querySelectorAll(':scope > input').forEach(input => {
    const label = document.createElement('label'); label.textContent = input.placeholder;
    input.before(label); label.append(input);
  });
  configuration.querySelector('hr')?.remove();
  const provider = $('#providerSettings');
  const providerChildren = [...provider.children];
  const directChild = selector => {
    const control = $(selector);
    return providerChildren.find(child => child === control || child.contains(control));
  };
  const connectionStart = providerChildren.indexOf(directChild('#providerName'));
  const capacityStart = providerChildren.indexOf(directChild('#providerModel'));
  const testStart = providerChildren.indexOf(directChild('#providerTemporaryKey'));
  const bindStart = providerChildren.indexOf(directChild('#providerVersion'));
  const savePosition = providerChildren.indexOf($('#providerSave'));
  const providerDiscovery = directChild('#providerDiscover');
  const providerPages = subpages(provider, [
    ['连接与模型', [...providerChildren.slice(connectionStart, capacityStart), providerDiscovery], 'connection'],
    ['能力与费用', providerChildren.slice(capacityStart, savePosition), 'capacity'],
    ['测试与启用', providerChildren.slice(testStart, bindStart).filter(node => node !== providerDiscovery), 'test'],
    ['绑定论文', providerChildren.slice(bindStart), 'binding']
  ], '模型配置步骤');
  provider.append($('#providerSave'), $('#providerVersionStatus'));
  $('#providerNew').addEventListener('click', () => providerPages[0].select());
  const selectProviderProfile = pp.selectProviderProfile;
  if (selectProviderProfile) pp.selectProviderProfile = async (...args) => {
    await selectProviderProfile(...args); providerPages[1].select();
    if (args[1] === 'vision') $('#providerVisionTokens').closest('.ws-disclosure').open = true;
  };
  [
    ['#providerTextInputPrice', '文本费率'], ['#providerVisionTokens', '视觉图片容量'], ['#providerImageGenerate', '图像生成与编辑用途']
  ].forEach(([selector, title]) => {
    const fieldset = $(selector).closest('fieldset');
    disclosure(providerPages[1].page, title, [fieldset]);
  });
  settings.querySelector('.ws-subnav').after(settings.querySelector('.ws-view-heading'));
  const knowledgePages=subpages(knowledge,[
    ['发现论文',[cardWith('#academicCandidates')],'discover'],['参考文献',[cardWith('#refList'),$('#referenceLibraryCard')],'references'],
    ['语料库',[cardWith('#corpusList')],'corpus'],['术语库',[cardWith('#termList')],'terms'],
    ['论证图',[$('#logicGraphCard')],'graph'],['领域与复用',[shared,cardWith('#copyResource')],'shared']
  ],'资料类别');
  const legacyDrawing=cardWith('#drawImage');
  // The versioned drawing flow provides the local diagram capability.
  // Retire its duplicate quick-entry UI after legacy handlers have initialized.
  legacyDrawing?.remove();
  // Preserve any extension cards instead of silently dropping future functionality.
  cards.filter(node=>node.isConnected&&node.parentElement===grid).forEach(node=>knowledgePages.at(-1).page.append(node));
  if(grid!==knowledge&&!grid.children.length)grid.remove();
  const visual = $('#visual');
  const assetRefresh = $('#refreshVisualAssets');
  visual.querySelector(':scope > .visual-heading')?.remove();
  const visualLibrary = document.createElement('div'); visualLibrary.className = 'ws-image-library';
  const visualToolbar = document.createElement('div'); visualToolbar.className = 'ws-task-toolbar';
  visualToolbar.innerHTML = '<p>管理当前论文使用的图像与版本。</p><div><button type="button" id="openVisualImport" class="primary">导入图像</button><button type="button" id="openVisualDrawing">新建绘图</button></div>';
  visualLibrary.append(visualToolbar);
  assetRefresh.textContent = '刷新图像库'; visualToolbar.lastElementChild.append(assetRefresh);
  const importPanel = document.createElement('section'); importPanel.id = 'workspaceVisualImport';
  importPanel.className = 'ws-inline-editor'; importPanel.hidden = true;
  importPanel.innerHTML = '<div class="ws-inline-heading"><h2>导入图像</h2><button id="closeVisualImport" type="button">收起</button></div>';
  move(importPanel, '#visualImportForm'); visualLibrary.append(importPanel);
  move(visualLibrary, '#visualAssetStatus'); move(visualLibrary, '.visual-workspace'); move(visualLibrary, '.visual-copy');
  const drawingStudio = visual.querySelector('.drawing-studio');
  const evidenceStudio = visual.querySelector('.visual-evidence-studio');
  pp.resultModuleView = new URLSearchParams(location.search).get('view') === 'module';
  const visualPages = subpages(visual, [
    ['图像库', [visualLibrary], 'library'], ['绘图任务', [drawingStudio], 'drawing'],
    ['结果与图表', [$('#resultAuthorLibrary')], 'result-library'], ['检查报告', [evidenceStudio], 'review'],
    ['结果数据', [$('#resultsWorkspace')], 'results'], ['结果详情', [$('#resultAuthorDetail')], 'result-detail']
  ], '图像任务');
  visualPages.find(page => page.key === 'result-detail').button.hidden = true;
  if (!pp.resultModuleView) visualPages.find(page => page.key === 'results').button.hidden = true;
  const newDrawing = disclosure(drawingStudio, '新建绘图任务', [$('.drawing-start')]);
  newDrawing.id = 'workspaceDrawingRequest'; newDrawing.open = true;
  drawingStudio.querySelector('.visual-heading').after(newDrawing);
  new MutationObserver(() => { if (!$('#drawingRunDetails').hidden) newDrawing.open = false; })
    .observe($('#drawingRunDetails'), {attributes: true, attributeFilter: ['hidden']});
  $('#openVisualImport').onclick = () => { importPanel.hidden = false; $('#visualAssetFile').focus(); };
  $('#closeVisualImport').onclick = () => { importPanel.hidden = true; $('#openVisualImport').focus(); };
  $('#openVisualDrawing').onclick = () => { visualPages[1].select(); newDrawing.open = true; $('#drawingIntent').focus(); };
  const drawingActions = $('#confirmDrawingType').parentElement;
  disclosure($('#drawingRunDetails'), '任务管理与编辑历史', [
    $('#undoDrawingEdit'), $('#redoDrawingEdit'), $('#cancelDrawingRun'), $('#reconcileDrawingRun'), $('#copyDrawingRun')
  ]);
  $('#confirmDrawingType').textContent = '确认绘图输入';
  drawingActions.classList.add('ws-primary-actions');
  const drawingConfiguration = document.createElement('button');
  drawingConfiguration.type = 'button'; drawingConfiguration.textContent = '配置图像模型';
  drawingConfiguration.onclick = () => { navigate('settings/images'); };
  drawingStudio.querySelector('.visual-heading').append(drawingConfiguration);
  const fileChoice = $('#visualAssetFile');
  const updateImportFields = () => {
    $('#visualPdfPage').closest('label').hidden = !fileChoice.files[0]?.name.toLowerCase().endsWith('.pdf');
    $('#importVisualAsset').disabled = !fileChoice.files.length;
  };
  fileChoice.addEventListener('change', updateImportFields);
  $('#visualImportForm').addEventListener('reset', () => {
    importPanel.hidden = true;
    $('#openVisualImport').focus();
    setTimeout(updateImportFields, 0);
  });
  updateImportFields();
  const syncVisualAvailability = () => {
    const hasAsset = Boolean($('#evidenceAsset').value);
    $('#runVisualEvidence').disabled = !hasAsset;
    $('#visualReportSelect').closest('label').hidden = !$('#visualReportSelect').value;
    $('#drawingRunSelect').closest('label').hidden = !$('#drawingRunSelect').value;
  };
  ['#evidenceAsset', '#visualReportSelect', '#drawingRunSelect'].forEach(selector => {
    new MutationObserver(syncVisualAvailability).observe($(selector), {childList: true});
    $(selector).addEventListener('change', syncVisualAvailability);
  });
  syncVisualAvailability();
  pp.openVisualReview = version => {
    navigate('visual/review');
    const choice = $('#evidenceAsset');
    if (![...choice.options].some(option => option.value === version.id)) {
      const option = document.createElement('option'); option.value = version.id;
      option.textContent = `所选图像 v${version.version_no}`; choice.append(option);
    }
    choice.value = version.id; choice.dispatchEvent(new Event('input', {bubbles: true}));
    syncVisualAvailability(); choice.focus();
  };
  const paperManagement = document.createElement('div'); paperManagement.className = 'ws-paper-management';
  disclosure(configuration, '论文管理', [paperManagement]); move(paperManagement, '#archiveProject');
  // Collection pages start with saved material. Creation and provenance are separate tasks.
  [
    ['#termList', '添加术语', ['#term', '#definition', '#termAliases', '#addTerm']],
    ['#corpusList', '添加语料', ['#corpusTitle', '#corpusContent', '#addCorpus']],
    ['#refList', '添加文献', ['#citeKey', '#refTitle', '#addRef']]
  ].forEach(([listSelector, title, selectors]) => {
    const card = cardWith(listSelector);
    const form = disclosure(card, title, selectors.map(selector => $(selector)));
    form.classList.add('ws-resource-create');
    card.querySelector('h3').after(form);
    form.querySelectorAll('input, textarea').forEach(input => {
      const label = document.createElement('label');
      label.textContent = input.placeholder === 'cite key' ? '引用标识' : input.placeholder.replace('（逗号分隔）', '');
      input.before(label); label.append(input);
    });
  });
  const referenceLibrary = $('#referenceLibraryCard');
  disclosure(knowledgePages[1].page, '全局文献库与待确认导入', [referenceLibrary]);
  $('#referenceLibraryCard .search-heading h3').textContent = '全局文献库';
  const searchFilters = $('#academicYearFrom').closest('.inline-fields');
  const filterDisclosure = disclosure($('#academicSearchCard'), '筛选年份与开放获取', [searchFilters]);
  $('#academicSearchCard .search-query-row').after(filterDisclosure);
  $('#academicSearchCard .search-badge')?.remove();
  $('#academicQuery').setAttribute('aria-label', '研究问题或关键词');
  $('#academicYearFrom').setAttribute('aria-label', '起始年份');
  $('#academicYearTo').setAttribute('aria-label', '结束年份');
  move(planning,'#projectLifecycleCard');
  if(lifecycle&&lifecycle.parentElement!==planning)planning.append(lifecycle);
  const providerOpen=pp.openProviderSettings;
  const modelDialog = document.createElement('dialog'); modelDialog.id = 'agentModelDialog';
  modelDialog.setAttribute('aria-labelledby', 'agentModelDialogTitle');
  modelDialog.innerHTML = '<div class="agent-model-heading"><h2 id="agentModelDialogTitle">论文 Agent 模型</h2><button type="button">返回对话</button></div>';
  document.body.append(modelDialog);
  let providerAnchor = null;
  modelDialog.querySelector('button').onclick = () => modelDialog.close();
  modelDialog.addEventListener('close', () => {
    if (providerAnchor) { providerAnchor.replaceWith($('#providerSettings')); providerAnchor = null; }
    document.dispatchEvent(new CustomEvent('paperpilot:agent-context-changed'));
  });
  pp.openAgentModelSettings = () => {
    if (modelDialog.open) return;
    const providerPanel = $('#providerSettings');
    providerAnchor = document.createComment('Agent model settings return location');
    providerPanel.before(providerAnchor); modelDialog.append(providerPanel); providerPanel.open = true;
    modelDialog.showModal(); providerOpen?.();
  };
  pp.openProviderSettings=()=>{if(modelDialog.open){providerOpen?.();return;}navigate('settings');settingPages[1].select();$('#providerSettings').open=true;providerOpen?.();};
  $('#openProviderSettings').onclick=pp.openProviderSettings;
  ['#providerSettings','#styleLearningPanel'].forEach(selector=>{if($(selector))$(selector).open=true;});
  const announcement=$('#workflowAnnouncement'); if(announcement) {announcement.classList.add('ws-announcement'); workspace.prepend(announcement);}
  const bar=document.createElement('div'); bar.className='ws-location-bar';
  bar.innerHTML='<div><h1 id="workspaceViewTitle">正文编辑</h1><span id="workspaceViewHint">选择文件，继续你的研究写作</span></div><span id="workspaceAccessState" role="status"></span>';
  $('.project-head').after(bar);
  // Advanced build choices stay next to the real compile action, out of the writing surface.
  const buildOptions=document.createElement('details'); buildOptions.className='ws-history-build-options';
  buildOptions.innerHTML='<summary>编译选项</summary><div></div>';
  ['#buildPurpose','#buildDraftSource'].forEach(selector=>move(buildOptions.lastElementChild,selector));
  const auto=$('#autoPreviewBuild')?.closest('label');if(auto)buildOptions.lastElementChild.append(auto);
  history.append(buildOptions);
  buildOptions.lastElementChild.append($('#compile'));
  $('#compile').textContent = '手动编译';
  const readerTools = document.createElement('details');
  readerTools.className = 'ws-reader-tools';
  const readerSummary = document.createElement('summary'); readerSummary.textContent = 'PDF版本工具';
  readerTools.append(readerSummary);
  ['.pdf-reading-controls', '#pdfReaderStatus', '#sourceToPdf'].forEach(selector => move(readerTools, selector));
  history.append(readerTools);

  const fileActions=document.createElement('details');fileActions.className='ws-file-actions';fileActions.innerHTML='<summary>文件操作</summary><div></div>';
  ['#newFilePath','#addFile','#deleteFile','#convert'].forEach(selector=>move(fileActions.lastElementChild,selector));
  move(fileActions.lastElementChild,'#editorGovernance');
  $('#fileSelect').after(fileActions);
  fileActions.addEventListener('toggle',()=>{
    const details = fileActions;
    if(!details.open)return;
    const rect=details.querySelector('summary').getBoundingClientRect(), menu=details.lastElementChild;
    menu.style.top=`${rect.bottom+6}px`;menu.style.left=`${Math.max(8,Math.min(rect.left,innerWidth-288))}px`;
    menu.style.maxHeight=`${Math.max(120,innerHeight-rect.bottom-22)}px`;
  });
  const footer=document.createElement('div');footer.className='ws-editor-footer';footer.innerHTML='<span id="workspaceFileInfo">未选择文件</span>';$('#editor').after(footer);
  move(footer,'#editorSaveStatus');
  ['#restoreEditorDraft','#discardEditorDraft'].forEach(selector=>move(assistPages.recover,selector));
  const referenceCard=cardWith('#refList');if(referenceCard)move(referenceCard,'#compare');
  function placeDeferredPanels(){
    move(assistPages.revise,'#targetedModificationPanel');
    move(fileActions.lastElementChild,'#editorGovernance');
    move(footer,'#editorSaveStatus');
    ['#restoreEditorDraft','#discardEditorDraft'].forEach(selector=>move(assistPages.recover,selector));
  }
  document.addEventListener('paperpilot:panels-ready',placeDeferredPanels);placeDeferredPanels();
  const outline = $('#outline');
  const outlineStructure = document.createElement('div'); outlineStructure.id = 'outlineStructureWorkbench';
  const outlineText = document.createElement('div'); outlineText.className = 'ws-outline-text';
  const outlineNodes = document.createElement('div'); outlineNodes.className = 'ws-outline-nodes';
  const outlineSuggestions = document.createElement('div'); outlineSuggestions.className = 'ws-outline-suggestions';
  const textToolbar = outline.querySelector('.panel-title');
  const outlineAction = textToolbar.querySelector('[data-action="outline"]');
  outlineSuggestions.append(outlineAction);
  outlineText.append(textToolbar); move(outlineText, '#outlineText');
  move(outlineText, '.outline-structured-toolbar'); move(outlineText, '#outlineMigrationResult');
  move(outlineNodes, '#outlineTree');
  const outlinePages = subpages(outline, [
    ['文章脉络', [outlineStructure], 'nodes'], ['文本大纲', [outlineText], 'text'],
    ['正式章节', [outlineNodes], 'formal'], ['AI 建议', [outlineSuggestions], 'suggestions']
  ], '大纲任务');
  function mountFormalOutlineCanvas() {
    if (!standaloneOutline || workspace.dataset.view !== 'outline' || outlinePages[2].page.hidden
      || !window.X6 || $('#legacyOutlineScript')) return;
    const script = document.createElement('script');
    script.id = 'legacyOutlineScript';
    script.src = '/static/outline-canvas-ui.js?v=20261123-outline';
    script.onerror = () => script.remove();
    document.head.append(script);
  }
  document.addEventListener('DOMContentLoaded', mountFormalOutlineCanvas, {once:true});
  document.addEventListener('paperpilot:view-changed', ({detail}) => {
    if (detail?.view === 'outline') mountFormalOutlineCanvas();
  });
  const progress = document.createElement('section'); progress.className = 'ws-progress-task';
  const workflow = $('#workflowDashboard');
  ['.workflow-head', '#workflowSteps', '#workflowControls', '#projectLifecycleCard'].forEach(selector => move(progress, selector));
  const startup = $('#startupPlanner');
  const legacyPlanning = document.createElement('section');
  legacyPlanning.id = 'startupLegacyNotice';
  legacyPlanning.hidden = true;
  legacyPlanning.innerHTML = '<h3>这篇论文使用旧版格式</h3><p>新建论文后，可使用五阶段规划、阶段确认和研究设计建议。</p><button type="button">新建工作流论文</button>';
  legacyPlanning.querySelector('button').onclick = () => $('#newProject').click();
  const workItems = $('#workflowWorkItems').closest('section');
  const planningPages = subpages(planning, [
    ['启动规划', [legacyPlanning, startup], 'startup'], ['写作阶段与发布', [progress], 'progress'], ['资源待办', [workItems], 'resources']
  ], '规划任务');
  // All live children now have a task home; remove the retired outer dashboard shell.
  if (workflow.querySelector('button,input,select,textarea')) disclosure(planningPages[2].page, '其他流程操作', [workflow]);
  else workflow.remove();
  const historyBlocks = [...history.querySelectorAll(':scope > details')];
  historyBlocks.forEach(details => details.open = true);
  const historyPages = subpages(history, [
    ['内容版本', [historyBlocks[0]], 'content'], ['修订记录', [historyBlocks[1]], 'revisions'],
    ['AI 任务', [historyBlocks[2]], 'ai'], ['PDF 构建', [$('.build-history')], 'builds']
  ], '历史类别');
  historyPages[3].page.prepend(buildOptions, readerTools);
  const paperSwitcher = document.createElement('details'); paperSwitcher.className = 'ws-paper-switcher';
  paperSwitcher.innerHTML = '<summary><span>当前论文</span><strong id="workspaceCurrentPaper">选择论文</strong></summary><div class="ws-paper-picker"></div>';
  sidebar.prepend(paperSwitcher);
  const picker = paperSwitcher.lastElementChild;
  picker.addEventListener('click', event => {
    if (event.target.closest('.project-item')) {
      paperSwitcher.open = false;
      paperSwitcher.querySelector('summary').focus();
    }
  });
  ['#projectSearch', '#projectSearchScope', '#projects', '#newProject'].forEach(selector => move(picker, selector));
  sidebar.querySelectorAll(':scope > h3').forEach(heading => heading.remove());
  if (guide) { paperSwitcher.after(guide); guide.after(tabs); }
  let switcherProjectId = '';
  function organizeTaskExtensions() {
    [
      ['#termReviewWorkbench', '术语复核与领域沉淀'],
      ['#corpusLineageWorkbench', '来源追踪与校正版'],
      ['#corpusIndexPanel', '原文索引与段落检索'],
      ['#citationReviewPanel', '引用检查与历史报告']
    ].forEach(([selector, title]) => {
      const panel = $(selector);
      if (!panel || panel.closest('.ws-disclosure')) return;
      const host = panel.parentElement;
      const details = disclosure(host, title, []); host.insertBefore(details, panel); details.append(panel);
      panel.querySelector('h4')?.setAttribute('hidden', '');
    });
    move(outlineNodes, '#outlineOperations'); move(outlineSuggestions, '#structuredSuggestionWorkbench');
    const continuationLimits = $('#continuationPlanWorkbench');
    if (continuationLimits && !continuationLimits.closest('.ws-disclosure')) disclosure(assistPages.continue, '分段目标与预算', [continuationLimits]);
  }
  document.addEventListener('paperpilot:panels-ready', organizeTaskExtensions); organizeTaskExtensions();
  const empty=document.createElement('div');empty.className='ws-pdf-empty';empty.innerHTML=`${icon('preview')}<h3>PDF 预览</h3><p>编辑后自动生成预览，可在构建记录查看状态。</p><button type="button">查看构建记录</button>`;
  $('#pdfFrame').before(empty);empty.querySelector('button').onclick=()=>navigate('history/builds');
  const updatePdf=()=>{empty.hidden=Boolean($('#pdfFrame').getAttribute('src')&&!$('#pdfFrame').getAttribute('src').startsWith('about:'));$('#pdfFrame').classList.toggle('ws-empty-frame',!empty.hidden);};
  new MutationObserver(updatePdf).observe($('#pdfFrame'),{attributes:true,attributeFilter:['src']});updatePdf();
  const viewNames={...labels,planning:'启动与流程',history:'版本与诊断',settings:'论文设置'};
  let routing=false, currentView='write';
  const wideWorkspace = matchMedia('(min-width:1191px)');
  function setOutlineAssistant(open) {
    assistant.hidden = !open;
    workspace.classList.toggle('ws-assistant-collapsed', !open);
    workspace.classList.toggle('ws-assistant-open', open && !wideWorkspace.matches);
    assistToggle.setAttribute('aria-expanded', String(open));
  }
  function closeDrawers(){
    shell.classList.remove('ws-library-open');
    workspace.classList.remove('ws-assistant-open');
    libraryToggle.setAttribute('aria-expanded','false');
    if (standaloneOutline) return setOutlineAssistant(false);
    const assistantVisible = !assistant.hidden && matchMedia('(min-width:1191px)').matches && !workspace.classList.contains('ws-assistant-collapsed');
    assistToggle.setAttribute('aria-expanded',String(assistantVisible));
  }
  wideWorkspace.addEventListener('change', closeDrawers);
  async function leaveOutline(url) {
    try {
      await pp.flushOutlineDrafts?.();
      location.assign(url);
    } catch (error) {
      $('#workspaceAccessState').textContent = '大纲草稿保存失败，请重试。';
    }
  }
  function navigate(name, fromLegacy=false) {
    if(routing)return;
    if (standaloneOutline && name.split('/')[0] !== 'outline') {
      if (name !== 'workspaceMain') leaveOutline(pp.paperWorkspaceUrl('/', name === 'chat' ? 'chat' : 'chat/' + name));
      return;
    }
    if (standaloneTerms && name !== 'knowledge/terms') {
      if (name !== 'knowledge' && name !== 'workspaceMain') location.assign(pp.paperWorkspaceUrl('/', name === 'chat' ? 'chat' : 'chat/' + name));
      return;
    }
    let [viewName, task] = name.split('/'); name = viewName;
    if (name === 'planning' && !task) task = 'startup';
    routing=true;currentView=name;
    if(labels[name]&&!fromLegacy)$(`.tabs [data-tab="${name}"]`)?.click();
    if(!labels[name])document.querySelectorAll('.panel.active,.tabs .active').forEach(node=>node.classList.remove('active'));
    Object.entries(views).forEach(([key,node])=>node.hidden=key!==name);
    $('#readingWorkspace').hidden=!['write','preview'].includes(name);
    ['outline','knowledge','visual'].forEach(key=>{const node=$('#'+key);node.hidden=key!==name;node.classList.toggle('active',key===name);});
    tabs.querySelectorAll('button').forEach(button=>{const selected=(button.dataset.tab||button.dataset.view)===name;button.classList.toggle('active',selected);if(selected)button.setAttribute('aria-current','page');else button.removeAttribute('aria-current');});
    workspace.dataset.view=name;$('#workspaceViewTitle').textContent=viewNames[name]||name;
    const group = taskGroups.get(name);
    const destination = group?.find(item => item.key === task);
    destination?.select();
    const activeTask = group?.find(item => !item.page.hidden)?.key;
    const nextHash = `#${name}${activeTask ? '/' + activeTask : ''}`;
    if(location.hash!==nextHash)window.history.replaceState(null,'',nextHash);
    $('#compile').hidden = false; buildOptions.hidden = false;
    $('#workspaceViewHint').textContent=({write:'选择文件，继续你的研究写作',outline:'组织章节，审阅结构建议',knowledge:'检索、整理和核对研究资料',preview:'PDF 与源码始终对应同一构建',visual:'绘图、审阅并管理图像版本',planning:'规划、资源待办与发布',history:'所有历史集中在这里',settings:'配置只在明确保存后生效'})[name];
    // The agent panel is global: keep it available on every view so the same
    // conversation can drive outline, knowledge and settings too.
    assistToggle.hidden = false;
    if (!standaloneOutline) assistant.hidden = false;
    closeDrawers();
    center.scrollTop=0;routing=false;
    document.dispatchEvent(new CustomEvent('paperpilot:view-changed',{detail:{view:name}}));
  }
  tabs.querySelectorAll('[data-tab]').forEach(button=>button.addEventListener('click',()=>navigate(button.dataset.tab,true)));
  pp.openWorkspace=navigate;
  pp.showAgentSidebar = () => {
    if (standaloneOutline) return;
    workspace.classList.remove('ws-assistant-collapsed');
    if (!matchMedia('(min-width:1191px)').matches) workspace.classList.add('ws-assistant-open');
    assistToggle.setAttribute('aria-expanded','true');
  };
  const moduleButton = document.createElement('button'); moduleButton.id = 'openCurrentModuleAgent'; moduleButton.type = 'button';
  moduleButton.textContent = '讨论当前模块'; $('.ws-location-bar').append(moduleButton);
  if (standaloneOutline || standaloneTerms) {
    moduleButton.hidden = true;
    const back = document.createElement('a');
    back.id = standaloneOutline ? 'outlineBackToPaper' : 'termsBackToPaper';
    back.href = '/';
    back.textContent = '返回论文';
    if (standaloneOutline) back.onclick = event => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      leaveOutline(back.href);
    };
    $('.ws-location-bar').append(back);
    document.addEventListener('paperpilot:project-loaded', () => {
      back.href = pp.paperWorkspaceUrl('/');
      const url = new URL(location.href);
      const project = pp.current?.project?.id;
      if (url.searchParams.get('project') !== project) {
        url.searchParams.delete('structure');
        url.searchParams.delete('draft');
      }
      if (project) url.searchParams.set('project', project);
      window.history.replaceState(null, '', url);
    });
  }
  moduleButton.onclick = () => {
    const stages = {'STAGE-01':'research','STAGE-02':'outline','STAGE-03':'structure','STAGE-04':'evidence','STAGE-05':'resources'};
    const modules = {write:'writing',outline:'outline',knowledge:'knowledge',visual:'visual',preview:'reading'};
    pp.openModuleAgent?.(currentView === 'planning' ? stages[pp.selectedStartupStage] || 'paper' : modules[currentView] || 'paper');
  };
  const openReader=pp.openBuildReader;
  if(openReader)pp.openBuildReader=(project,job,options={})=>{
    if(project!==pp.data?.project?.id)return;
    // PDF.js measures its container on load: reveal the reader before assigning its URL.
    if (!options.background) navigate('preview');
    openReader(project,job);
  };
  libraryToggle.onclick=()=>{const open=shell.classList.toggle('ws-library-open');libraryToggle.setAttribute('aria-expanded',String(open));};
  assistToggle.onclick=()=>{
    if (standaloneOutline) {
      setOutlineAssistant(assistant.hidden);
      if (!assistant.hidden) assistant.querySelector('button')?.focus();
      return;
    }
    const open=wideWorkspace.matches?!workspace.classList.toggle('ws-assistant-collapsed'):workspace.classList.toggle('ws-assistant-open');assistToggle.setAttribute('aria-expanded',String(open));if(open)assistant.querySelector('button')?.focus();
  };
  $('#closeWorkspaceAssistant').onclick=()=>{
    if (standaloneOutline) setOutlineAssistant(false);
    else {workspace.classList.remove('ws-assistant-open');if(wideWorkspace.matches)workspace.classList.add('ws-assistant-collapsed');assistToggle.setAttribute('aria-expanded','false');}
    assistToggle.focus();
  };
  document.addEventListener('keydown',event=>{if(event.key==='Escape'){const libraryOpen=shell.classList.contains('ws-library-open');const assistOpen=workspace.classList.contains('ws-assistant-open');closeDrawers();if(libraryOpen)libraryToggle.focus();else if(assistOpen)assistToggle.focus();}});
  document.addEventListener('click',event=>{if(shell.classList.contains('ws-library-open')&&!sidebar.contains(event.target)&&!libraryToggle.contains(event.target))closeDrawers();});
  // Group the actual project nodes, retaining their existing switch/dirty-input protection.
  function groupProjects(){
    const host=$('#projects');if(host.querySelector(':scope > .ws-domain'))return;
    const rows=[...host.querySelectorAll(':scope > .project-item')];if(!rows.length)return;
    rows.forEach(row => {
      row.tabIndex = 0;
      row.setAttribute('role', 'button');
      row.setAttribute('aria-pressed', String(row.classList.contains('selected')));
      row.onkeydown = event => {
        if (event.target === row && (event.key === 'Enter' || event.key === ' ')) {
          event.preventDefault();
          row.click();
        }
      };
    });
    const groups=new Map();
    for(const row of rows){const name=row.querySelector('small')?.textContent.split(' · ')[0]||'未设置领域';if(!groups.has(name))groups.set(name,[]);groups.get(name).push(row);}
    const fragment=document.createDocumentFragment();
    for(const [name,items] of groups){const domain=document.createElement('details');domain.className='ws-domain';domain.open=true;const summary=document.createElement('summary');summary.textContent=`${name}（${items.length}）`;domain.append(summary,...items);fragment.append(domain);}
    host.replaceChildren(fragment);
  }
  new MutationObserver(groupProjects).observe($('#projects'),{childList:true});groupProjects();
  const chapterNav=document.createElement('nav');chapterNav.className='ws-chapters';chapterNav.setAttribute('aria-label','当前论文章节');chapterNav.innerHTML='<h3>当前章节</h3><div></div>';
  // Chapters belong to the current paper, so they sit after the tool list.
  tabs.after(chapterNav);
  function updateChapters(){
    const structured=[...document.querySelectorAll('#outlineTree .outline-node-select')];
    const headings=($('#outlineText').value||'').split('\n').map(line=>line.trim()).filter(line=>/^#{1,3}\s|^\\(?:sub)*section\{|^\d+(?:\.\d+)*[.)、]?\s+|^[一二三四五六七八九十]+[、.]/.test(line)).slice(0,24);
    const host=chapterNav.lastElementChild;host.replaceChildren();
    if(structured.length){structured.forEach(source=>{const button=document.createElement('button');button.type='button';button.textContent=source.textContent;button.onclick=()=>{navigate('outline/formal');source.click();source.scrollIntoView({block:'center'});source.focus();};host.append(button);});return;}
    if(!headings.length){const p=document.createElement('p');p.textContent='在章节大纲中组织论文';host.append(p);}
    headings.forEach(line=>{const button=document.createElement('button');button.type='button';button.textContent=line.replace(/^#+\s*/,'').replace(/^\\(?:sub)*section\{(.*)\}$/,'$1');button.onclick=()=>{navigate('outline/text');const area=$('#outlineText'),start=area.value.indexOf(line);area.focus();area.setSelectionRange(start,start+line.length);};host.append(button);});
  }
  function updateAccess(){
    const project=pp.data?.project;if(!project)return;
    $('#workspaceCurrentPaper').textContent = project.name;
    if (switcherProjectId !== project.id) paperSwitcher.open = false;
    switcherProjectId = project.id;
    actions.hidden = Boolean(pp.data.workflow_enabled);
    legacyPlanning.hidden = Boolean(pp.data.workflow_enabled);
    startup.hidden = !pp.data.workflow_enabled;
    const readonly=Boolean(project.archived||project.lifecycle_state==='published');
    $('#workspaceAccessState').textContent=readonly?(project.archived?'已归档 · 只读':'已发布 · 只读'):'可编辑';
    $('#workspaceAccessState').classList.toggle('is-readonly',readonly);
    ['#editor','#outlineText','#projectName'].forEach(selector=>$(selector).readOnly=readonly);
    ['#saveFile','#saveOutline','#addFile','#deleteFile','#saveSettings','#addTerm','#addCorpus','#addRef','#saveDomainBinding','#saveAiBinding'].forEach(selector=>{
      const node=$(selector);if(!node)return;if(readonly&&!node.disabled){node.dataset.workspaceDisabled='true';node.disabled=true;}else if(!readonly&&node.dataset.workspaceDisabled){node.disabled=false;delete node.dataset.workspaceDisabled;}
    });
    document.querySelectorAll('[data-action]').forEach(node=>{if(readonly&&!node.disabled){node.dataset.workspaceDisabled='true';node.disabled=true;}else if(!readonly&&node.dataset.workspaceDisabled){node.disabled=false;delete node.dataset.workspaceDisabled;}});
    updateChapters();updateFileInfo();
  }
  function updateFileInfo(){const text=$('#editor').value;$('#workspaceFileInfo').textContent=`${$('#fileSelect').value||'未选择文件'} · ${text?text.split('\n').length:0} 行 · ${text.length.toLocaleString()} 字符`;}
  $('#editor').addEventListener('input',updateFileInfo);$('#fileSelect').addEventListener('change',()=>setTimeout(updateFileInfo,0));$('#outlineText').addEventListener('input',updateChapters);
  new MutationObserver(updateChapters).observe($('#outlineTree'),{childList:true,subtree:true,characterData:true});
  document.addEventListener('paperpilot:project-loaded',updateAccess);
  new MutationObserver(updateAccess).observe($('#projectLifecycleState'),{childList:true});
  document.addEventListener('paperpilot:project-changed',()=>{closeDrawers();document.querySelectorAll('.ws-file-actions,.ws-history-build-options').forEach(node=>node.open=false);});
  const initialHash=location.hash.slice(1);
  const initialView=standaloneOutline
    ? (initialHash.startsWith('outline/') ? initialHash : 'outline/nodes')
    : standaloneTerms ? 'knowledge/terms' : initialHash;
  pp.initialWorkspaceLocation = initialView;
  if (standaloneOutline) document.title = '章节大纲 | PaperPilot';
  if (standaloneTerms) document.title = '术语库 | PaperPilot';
  navigate(viewNames[initialView.split('/')[0]]?initialView:'write');updateAccess();
  window.addEventListener('hashchange',()=>{const name=location.hash.slice(1);if(viewNames[name.split('/')[0]])navigate(name);});
  document.querySelectorAll('h2,h3,h4').forEach(node=>{
    for(const text of node.childNodes)if(text.nodeType===Node.TEXT_NODE)text.textContent=text.textContent.replace(/\s*[（(]REQ-\d+[）)]/g,'');
  });
})();
