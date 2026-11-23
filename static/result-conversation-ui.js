/* Author-facing result previews, receipts and fixed version links. */
(() => {
  const pp = window.paperpilot;
  const $ = selector => document.querySelector(selector);
  const library = $('#resultAuthorLibrary'), detail = $('#resultAuthorDetail');
  if (!pp?.agentCards || pp.standaloneOutline || pp.standaloneTerms || !library || !detail) return;
  const store = window.paperpilotAgentStore;
  const pid = () => pp.current?.project?.id || '';
  const cid = () => $('#agentConversation')?.value || '';
  const readOnly = () => pp.current?.project?.archived || pp.current?.project?.lifecycle_state === 'published';
  const segment = encodeURIComponent;
  const base = project => '/api/projects/' + segment(project) + '/results';
  const cardPath = card => base(card.project_id) + '/conversations/' + segment(card.conversation_id) + '/cards/' + segment(card.id);
  const versionPath = (project, data) => base(project) + '/' + segment(data.dataset_id) + '/versions/' + segment(data.version_id);
  const assetPath = (project, data) => '/api/projects/' + segment(project) + '/drawing/assets/' + segment(data.asset_id)
    + '/versions/' + segment(data.version_id);
  const libraryRoute = 'visual/result-library';
  const detailRoute = 'visual/result-detail/';
  const cardRoute = card => detailRoute + ['card',card.conversation_id,card.id].map(segment).join('/');
  const versionRoute = data => detailRoute + ['table',data.dataset_id,data.version_id].map(segment).join('/');
  const figureRoute = data => detailRoute + ['figure',data.asset_id,data.version_id].map(segment).join('/');
  const el = (tag, text, css) => {
    const node = document.createElement(tag);
    if (text !== undefined && text !== null) node.textContent = text;
    if (css) node.className = css;
    return node;
  };
  const button = (text, handler, primary = false) => {
    const node = el('button', text, primary ? 'primary' : '');
    node.type = 'button';
    node.onclick = handler;
    return node;
  };
  const link = (text, href) => {
    const node = el('a', text); node.href = href;
    return node;
  };
  const status = text => {
    const node = el('p', text, 'result-author-status'); node.setAttribute('role', 'status');
    return node;
  };
  const get = async (url, data, method = 'POST') => {
    const response = await fetch(url, data === undefined ? {} : {
      method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(data)
    });
    const result = await response.json();
    if (!response.ok) {
      const error = new Error(result.detail?.message || '结果暂时不可用，请重试。');
      error.status = response.status; error.code = result.detail?.code;
      throw error;
    }
    return result;
  };
  const open = path => pp.openPaperArtifact('visual',path);
  const states = {choose:'待选择',ready:'可审阅',saved:'已保存',resolved:'已选择',cancelled:'已取消'};
  const actions = {chart:'保存图表',derive:'保存为新版本',upload:'保存为新版本',insert:'确认插入正文'};
  const composerRef = el('div',undefined,'result-compose-reference');
  composerRef.hidden = true;
  $('#agentInput').before(composerRef);
  function storedReference(project, conversation) {
    try {return JSON.parse(store.read(project, conversation, 'resultReference') || 'null');}
    catch {return null;}
  }
  function renderReference() {
    const current = storedReference(pid(),cid());
    composerRef.replaceChildren(); composerRef.hidden = !current;
    if (current) composerRef.append(el('span','引用 · ' + current.title),button('移除引用',() => {
      store.write(pid(),cid(),'resultReference',''); renderReference(); $('#agentInput').focus();
    }));
  }
  async function useReference(title, reference, conversation, draft = '') {
    const project = pid();
    if (!conversation) {
      await pp.openModuleAgent('paper');
      conversation = cid();
    }
    if (project !== pid() || !conversation || conversation !== cid()) return;
    store.write(project, conversation, 'resultReference', JSON.stringify({title, reference}));
    pp.openWorkspace('chat');
    const input = $('#agentInput');
    if (draft && input.value.trim() !== draft) {
      input.value = [input.value.trim(),draft].filter(Boolean).join('\n');
      input.dispatchEvent(new Event('input'));
    }
    renderReference(); input.focus();
  }
  pp.tryResultTurn = async ({pid:project,cid:conversation,instruction,file_path,signalAlive}) => {
    const current = storedReference(project,conversation);
    const cache = store.read(project,conversation,'resultPending');
    let old = null;
    try { old = JSON.parse(cache); } catch { /* Empty draft. */ }
    let data = {instruction,file_path,...current?.reference};
    if (old && JSON.stringify(old.data) !== JSON.stringify(data)) {
      if (old.data.instruction !== instruction) throw new Error('上一条结果请求尚待核对，请恢复原指令后重试。');
      data = old.data;
    }
    if (!old && /插入|插到|放到|写进|写入/.test(instruction) && current &&
        await pp.flushEditorChanges?.() === false) throw new Error('请先处理正文保存提示。');
    if (!signalAlive()) return null;
    const request = {data, request_id:old?.request_id || store.uid()};
    store.write(project,conversation,'resultPending',JSON.stringify(request));
    $('#agentRunStatus').textContent = '正在处理请求…';
    try {
      const result = await get(base(project) + '/conversations/' + segment(conversation) + '/turn',
        {...data,request_id:request.request_id});
      store.write(project,conversation,'resultPending','');
      if (result.handled && !result.message && result.card?.kind !== 'insert' && result.card?.state !== 'saved') {
        store.write(project,conversation,'resultReference','');
      }
      if (signalAlive()) {
        renderReference();
        if (!result.handled) $('#agentRunStatus').textContent = '';
      }
      return result;
    } catch (error) {
      if (error.status && error.status < 500) store.write(project,conversation,'resultPending','');
      throw error;
    }
  };

  function renderTable(data) {
    const scroll = el('div',undefined,'result-preview-scroll');
    scroll.tabIndex = 0; scroll.setAttribute('role','region'); scroll.setAttribute('aria-label','表格预览');
    const table = el('table'), head = table.createTHead().insertRow();
    for (const name of data.columns) {
      const th = el('th',name); th.scope = 'col'; head.append(th);
    }
    const body = table.createTBody();
    for (const values of data.rows || []) {
      const row = body.insertRow();
      for (const value of values) row.insertCell().textContent = value;
    }
    scroll.append(table); return scroll;
  }
  pp.resultPreviewTable = renderTable;
  function sourceDetails() {
    const node = el('details',undefined,'result-details');
    node.append(el('summary','来源与版本')); return node;
  }
  function operationsSummary(operations) {
    return (operations || []).map(op => {
      switch (op.kind) {
        case 'filter': return op.column + ' 范围 ' + (op.minimum ?? '不限') + ' 至 ' + (op.maximum ?? '不限');
        case 'select': return '保留列 ' + op.columns.join('、');
        case 'summarize': return '按 ' + op.group_by + ' 分组统计 ' + op.column;
        case 'deduplicate': return '删除重复行';
        case 'drop_blank': return '移除含空值的行';
        default: return '';
      }
    }).filter(Boolean).join('；');
  }
  function originalLinks(host, project, version, collection) {
    const files = version?.operation?.files || [];
    if (!collection || !files.length) return;
    for (const file of files) {
      host.append(link('原件 · ' + file.path, base(project) + '/collections/' + segment(collection) + '/sources/' + segment(file.id)));
    }
  }
  function heading(title, label, meta) {
    const head = el('div',undefined,'result-author-heading');
    if (label) head.append(el('span',label,'result-state'));
    head.append(el('h3',title));
    if (meta) head.append(el('p',meta));
    return head;
  }
  function figure(url,title) {
    const host = el('div');
    const img = el('img',undefined,'result-preview-image'); img.src = url; img.alt = title;
    const full = link('查看大图',url);
    full.className = 'result-full-preview'; full.target = '_blank'; full.rel = 'noopener';
    host.append(img,full);
    return host;
  }
  function openFromCard(path, trigger) {
    returnFocus = trigger;
    open(path);
  }
  let returnFocus = null, libraryFocus = null, detailEpoch = 0, libraryEpoch = 0, currentPath = '';
  const scoped = card => card.project_id === pid() && card.conversation_id === cid();
  async function postDecision(card, action, option) {
    const body = {action,option:option ?? null};
    const key = 'resultDecision:' + card.id + ':' + action + ':' + (option ?? '');
    const requestId = store.read(card.project_id,card.conversation_id,key) || store.uid();
    store.write(card.project_id,card.conversation_id,key,requestId);
    if (action === 'confirm' && card.kind === 'insert' && await pp.flushEditorChanges?.() === false) {
      throw new Error('请先处理正文保存提示。');
    }
    if (!scoped(card)) return null;
    const updated = await get(cardPath(card) + '/decision',{...body,request_id:requestId});
    store.write(card.project_id,card.conversation_id,key,'');
    if (!scoped(card)) return null;
    if (action === 'confirm') {
      document.dispatchEvent(new CustomEvent('paperpilot:result-saved',{detail:{card:updated}}));
      if (card.kind === 'insert') await pp.reload();
    } else await pp.refreshAgentMessages?.();
    return updated;
  }
  function resultSummary(card) {
    if (card.kind === 'chart') return [card.origin,card.row_count + ' 个点','数据 v' + card.version.version_no].join(' · ');
    if (card.kind === 'upload' || card.kind === 'derive') {
      return [card.input_rows === card.row_count ? card.row_count + ' 行' : card.input_rows + ' 行 → ' + card.row_count + ' 行',
        card.receipt ? 'v' + card.receipt.version_no : card.kind === 'upload' ? (card.target_dataset ? '追加到现有数据集' : '新建数据集') : '整理表候选'].join(' · ');
    }
    if (card.kind === 'table') return [card.origin,card.row_count + ' 行','v' + card.version.version_no].join(' · ');
    if (card.kind === 'insert') return card.target.file_path + ' · 图像 v' + card.asset.version_no;
    if (card.kind === 'library' && Number.isInteger(card.grouped_count)) return '已归组 ' + card.grouped_count.toLocaleString() + ' 项';
    return '';
  }
  function drawCard(host, card) {
    const label = card.kind === 'retry' ? '待调整' : card.kind === 'insert' && card.state === 'saved' ? '已插入正文'
      : card.kind === 'library' ? '' : states[card.state];
    host.replaceChildren(heading(card.title,label,resultSummary(card)));
    if (card.kind === 'choice') {
      if (card.state === 'choose' && !readOnly()) {
        const count = card.difference?.count;
        if (count) host.append(el('p','论文 NPZ 与 CSV 导出有 ' + count + ' 个数值不同。'));
        const choices = el('div',undefined,'result-options'), feedback = status('');
        const run = async (action, index) => {
          choices.querySelectorAll('button').forEach(node => {node.disabled = true;});
          feedback.textContent = action === 'choose' ? '正在读取所选来源…' : '';
          try {
            const updated = await postDecision(card,action,index);
            if (!updated || !host.isConnected) return;
            if (action === 'choose') {
              drawCard(host,{...card,state:'resolved',receipt:{card_id:updated.id}});
            } else drawCard(host,updated);
          } catch (error) {
            feedback.textContent = error.message;
            choices.querySelectorAll('button').forEach(node => {node.disabled = false;});
          }
        };
        card.options.forEach((option,index) => {
          const label = option.origin === '论文 NPZ' ? (option.figure_label || option.group) + ' · NPZ'
            : option.origin === 'CSV 导出' ? option.group + ' · CSV 导出' : option.title;
          choices.append(button(label,() => run('choose',index)));
        });
        choices.append(button('取消',() => run('cancel')));
        host.append(choices,feedback);
      } else if (card.receipt?.card_id) {
        const view = button('查看结果',() => openFromCard(cardRoute({...card,id:card.receipt.card_id}),view),true); host.append(view);
      }
      return;
    }
    if (card.kind === 'retry') {
      if (!readOnly()) host.append(button('继续修改请求',() => useReference(
        storedReference(pid(),cid())?.title || '上次请求',
        Object.fromEntries(['reference_id','dataset_id','asset_id','version_id'].filter(key => card[key]).map(key => [key,card[key]])),
        card.conversation_id,card.instruction)));
      return;
    }
    if (card.kind === 'library') {
      const view = button('查看' + (card.query || (card.grouped_count ? '分组' : '已保存成果')),() => {
        search.value = card.query || ''; openFromCard(libraryRoute,view);
      },true);
      host.append(view); return;
    }
    if (card.state === 'saved' && card.kind === 'chart') {
      const view = button('打开已保存图表',() => openFromCard(figureRoute(card.receipt),view),true);
      host.append(view); return;
    }
    if (card.state === 'saved' && card.kind !== 'insert') {
      const view = button('打开已保存版本',() => openFromCard(versionRoute(card.receipt),view),true);
      host.append(view); return;
    }
    const view = button(card.kind === 'insert' ? '核对插入位置' : '查看结果',() => openFromCard(cardRoute(card),view),true);
    host.append(view);
  }
  pp.agentCards.register('result_work', (pointer, context) => {
    const host = el('article',undefined,'result-chat-card');
    host.dataset.resultCard = pointer.id;
    const load = () => {
      host.replaceChildren(status('正在读取结果…'));
      get(cardPath({...context,id:pointer.id})).then(card => {
        if (scoped(card)) drawCard(host,card);
      }).catch(error => host.replaceChildren(status(error.message),button('重试',load)));
    };
    load(); return host;
  });

  const search = el('input');
  search.type = 'search'; search.placeholder = '搜索名称、算例、图号或文件名';
  search.setAttribute('aria-label','搜索结果与图表');
  const libraryStatus = status(''), results = el('div',undefined,'result-author-groups');
  const libraryBar = el('div',undefined,'result-library-bar');
  let libraryData = null, organizing = false;
  const openGroups = new Map();
  const organize = button('整理分组',() => {
    organizing = !organizing;
    groupWithAI.hidden = !organizing;
    organize.textContent = organizing ? '完成整理' : '整理分组';
    organize.setAttribute('aria-pressed',String(organizing));
    if (libraryData) renderGroups(libraryData);
  });
  organize.hidden = true;
  organize.setAttribute('aria-pressed','false');
  const groupWithAI = button('让 AI 分组',() => {
    pp.openWorkspace('chat');
    pp.openModuleAgent('paper','请按数据类型和来源关联给结果库重新分组。');
  });
  groupWithAI.hidden = true;
  libraryBar.append(search,organize,groupWithAI);
  library.append(libraryBar,libraryStatus,results);

  const dialog = el('dialog',undefined,'result-organize-dialog');
  const editForm = el('form');
  const dialogTitle = el('h3','整理条目');
  dialogTitle.id = 'resultOrganizeTitle';
  dialog.setAttribute('aria-labelledby',dialogTitle.id);
  const editName = el('input'), editGroup = el('select'), newGroup = el('input');
  editName.maxLength = 160; newGroup.maxLength = 80;
  function labeled(text, input) {
    const label = el('label',text);
    input.setAttribute('aria-label',text);
    label.append(input);
    return label;
  }
  const groupName = labeled('新分组名称',newGroup);
  const automaticHint = el('p',undefined,'result-author-status');
  const editStatus = status('');
  const saveEdit = el('button','保存','primary');
  saveEdit.type = 'submit';
  const editActions = el('div',undefined,'result-author-actions');
  editActions.append(saveEdit,button('取消',() => dialog.close()));
  const nameHint = el('small','清空可恢复自动名称');
  nameHint.id = 'resultOrganizeNameHint';
  editName.setAttribute('aria-describedby',nameHint.id);
  const nameLabel = labeled('显示名称',editName);
  nameLabel.append(nameHint);
  editForm.append(dialogTitle,nameLabel,labeled('分组',editGroup),groupName,automaticHint,editStatus,editActions);
  dialog.append(editForm); document.body.append(dialog);
  let activeEdit = null;
  function option(value,title) {
    const item = el('option',title); item.value = value; return item;
  }
  function editKey(item) { return item.kind + ':' + item.item_id; }
  function syncGroupFields() {
    groupName.hidden = editGroup.value !== 'new';
    newGroup.required = !groupName.hidden;
    automaticHint.hidden = editGroup.value !== '';
  }
  editGroup.onchange = syncGroupFields;
  function editItem(item) {
    const groups = libraryData?.manual_groups || [];
    activeEdit = {project:pid(),item,groups};
    editName.value = item.display_title;
    editName.placeholder = item.automatic_title;
    newGroup.value = '';
    editGroup.replaceChildren(option('','自动分组'),...groups.map((name,index) => option(String(index),name)),option('new','新建分组'));
    editGroup.value = item.manual_group ? String(groups.indexOf(item.manual_group)) : '';
    automaticHint.textContent = [item.automatic_category,item.automatic_group].filter((value,index,list) => value !== list[index-1]).join(' · ');
    editStatus.textContent = ''; saveEdit.disabled = false;
    syncGroupFields(); dialog.showModal(); editName.focus();
  }
  editForm.onsubmit = async event => {
    event.preventDefault();
    const edit = activeEdit;
    if (!edit || edit.project !== pid() || saveEdit.disabled) return;
    const {project,item,groups} = edit;
    const group = editGroup.value === 'new' ? newGroup.value.trim() : groups[editGroup.value] || '';
    if (editGroup.value === 'new' && !group) {editStatus.textContent = '请输入分组名称。'; newGroup.focus(); return;}
    saveEdit.disabled = true;
    editStatus.textContent = '正在保存…';
    try {
      const title = editName.value.trim();
      const updated = await get(base(project) + '/library/items/' + segment(item.kind) + '/' + segment(item.item_id),
        {title:title === item.automatic_title ? '' : title,group},'PUT');
      if (activeEdit !== edit || project !== pid()) return;
      dialog.close();
      await loadLibrary();
      if (project !== pid() || currentPath !== libraryRoute) return;
      if (libraryData) libraryStatus.textContent = updated.manual_group ? '已保存到「' + updated.manual_group + '」。' : '已保存。';
      const revised = results.querySelector('[data-library-edit="' + CSS.escape(editKey(item)) + '"]');
      revealGroups(revised);
      (revised || organize).focus();
    } catch (error) {
      if (activeEdit === edit && project === pid()) editStatus.textContent = error.message;
    } finally {
      if (activeEdit === edit) saveEdit.disabled = false;
    }
  };
  dialog.addEventListener('close',() => { if (!dialog.open) activeEdit = null; });

  function openLibraryItem(route) { libraryFocus = route; open(route); }
  function resultLink(title,route,className) {
    const target = link(title,'#chat/' + route);
    target.dataset.resultRoute = route;
    if (className) target.className = className;
    target.onclick = event => {
      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault(); openLibraryItem(route);
    };
    return target;
  }
  function editControl(item) {
    const action = button('整理条目',() => editItem(item));
    action.setAttribute('aria-label','整理条目：' + item.display_title);
    action.dataset.libraryEdit = editKey(item);
    return action;
  }
  function imagePreview(project,item,withCaption) {
    const node = el('figure',undefined,'result-library-figure');
    const route = figureRoute(item);
    const preview = resultLink('',route,'result-library-image');
    preview.setAttribute('aria-label','打开图表：' + item.display_title);
    const image = el('img');
    image.alt = item.display_title; image.loading = 'lazy'; image.decoding = 'async';
    image.width = item.width;
    image.height = item.height;
    image.addEventListener('error',() => {
      preview.classList.add('preview-unavailable');
      image.replaceWith(el('span','图像预览暂不可用，打开图表查看。'));
    },{once:true});
    image.src = assetPath(project,item) + '/preview';
    preview.append(image); node.append(preview);
    if (withCaption) {
      const caption = el('figcaption');
      caption.append(resultLink(item.display_title,route),el('small','图像 v' + item.version_no));
      if (organizing && !readOnly()) caption.append(editControl(item));
      node.append(caption);
    }
    return node;
  }
  function libraryItem(project,item) {
    const row = el('li',undefined,'result-library-item');
    const header = el('div',undefined,'result-library-item-heading');
    const info = el('div');
    info.append(el('h5',item.display_title));
    const facts = item.kind === 'table' ? [item.structure,item.row_count.toLocaleString() + ' 行',item.column_count + ' 列','v' + item.version_no]
      : [item.origin,'v' + item.version_no];
    info.append(el('p',facts.join(' · ')));
    header.append(info);
    if (organizing && !readOnly() && !item.adopted) header.append(editControl(item));
    row.append(header);
    if (item.kind === 'figure') row.append(imagePreview(project,item,false));
    for (const image of item.figures) row.append(imagePreview(project,image,true));
    if (item.kind === 'table') {
      row.append(resultLink('查看数据与原件',versionRoute(item),'result-library-data-link'));
    } else if (item.source) {
      row.append(resultLink('查看来源数据 v' + item.source.version_no,versionRoute(item.source),'result-library-data-link'));
    } else if (item.tables?.length) {
      const sources = el('details',undefined,'result-library-sources');
      sources.append(el('summary','查看关联数据与原件（' + item.tables.length + '）'));
      for (const table of item.tables) sources.append(resultLink(table.display_title,versionRoute(table)));
      row.append(sources);
    }
    return row;
  }
  function revealGroups(target) {
    for (let ancestor = target?.parentElement; ancestor && ancestor !== results; ancestor = ancestor.parentElement) {
      if (ancestor.tagName === 'DETAILS') ancestor.open = true;
    }
  }
  function libraryDisclosure(key,title,count,level) {
    const block = el('details',undefined,level === 'h3' ? 'result-library-category' : 'result-author-group');
    const label = el('summary');
    label.append(el(level,title),el('small',count.toLocaleString() + ' 项'));
    block.append(label);
    block.open = Boolean(search.value.trim()) || Boolean(openGroups.get(key));
    block.addEventListener('toggle',() => {
      if (block.isConnected && !search.value.trim()) openGroups.set(key,block.open);
    });
    return block;
  }
  function renderGroups(data) {
    const project = pid(), categories = new Map();
    results.replaceChildren();
    if (!search.value.trim()) categories.set('adopted',[]);
    for (const group of data.groups) {
      if (!categories.has(group.category)) categories.set(group.category,[]);
      categories.get(group.category).push(group);
    }
    for (const [key,groups] of categories) {
      const count = groups.reduce((total,group) => total + group.items.length,0);
      const title = groups[0]?.category_title || '论文已采纳';
      const category = libraryDisclosure(key,title,count,'h3');
      if (!groups.length) category.append(el('p','暂无已采纳结果。','result-author-status'));
      for (const group of groups) {
        const list = el('ul');
        list.append(...group.items.map(item => libraryItem(project,item)));
        if (group.title !== title) {
          const sub = libraryDisclosure(key + ':' + group.title,group.title,group.items.length,'h4');
          sub.append(list); category.append(sub);
        } else category.append(list);
      }
      results.append(category);
    }
    if (!data.groups.length && !search.value.trim() && !readOnly()) {
      results.append(button('到对话添加资料',() => {
        pp.openWorkspace('chat'); $('#conversationAttach').click();
      }));
    }
  }
  async function loadLibrary() {
    const project = pid(), epoch = ++libraryEpoch, query = search.value.trim();
    if (!project) return;
    libraryStatus.textContent = '正在读取已保存成果…';
    try {
      const data = await get(base(project) + '/library?q=' + segment(query));
      if (project !== pid() || epoch !== libraryEpoch) return;
      libraryData = data;
      organize.hidden = readOnly() || !data.groups.length;
      groupWithAI.hidden = organize.hidden || !organizing;
      libraryStatus.textContent = data.groups.length ? '' : query ? '没有找到匹配的结果。' : '还没有已保存的成果。';
      renderGroups(data);
      if (libraryFocus && currentPath === libraryRoute) {
        const focused = results.querySelector('[data-result-route="' + CSS.escape(libraryFocus) + '"]');
        revealGroups(focused);
        focused?.focus();
        libraryFocus = null;
      }
    } catch (error) {
      if (project === pid() && epoch === libraryEpoch) {
        results.replaceChildren(button('重新读取',loadLibrary)); libraryStatus.textContent = error.message;
      }
    }
  }
  let searchTimer;
  search.oninput = () => {
    clearTimeout(searchTimer);
    libraryEpoch++;
    searchTimer = setTimeout(loadLibrary,170);
  };

  function showCardDetail(project, card) {
    const bar = el('div',undefined,'result-author-actions'), notice = status('');
    detail.append(heading(card.title,card.kind === 'insert' && card.state === 'saved' ? '已插入正文' : states[card.state],resultSummary(card)));
    if (card.innovation_claims?.length) {
      const block = document.createElement('details');
      const label = document.createElement('summary');
      label.textContent = '本次结果对应的创新主张';
      block.append(label);
      for (const claim of card.innovation_claims) {
        const row = document.createElement('p');
        const target = document.createElement('a');
        target.textContent = claim.claim;
        target.href = '/innovations?' + new URLSearchParams({project: card.project_id, point: claim.point_id, version: claim.version});
        row.append(target);
        block.append(row);
      }
      detail.append(block);
    }
    const sources = sourceDetails();
    if (card.kind === 'chart') {
      detail.append(figure(cardPath(card) + '/preview',card.title));
      sources.append(el('p','X 轴 · ' + card.chart.x_column + '，Y 轴 · ' + card.chart.y_column));
    } else if (card.kind === 'insert') {
      detail.append(figure(assetPath(project,card.asset) + '/preview',card.asset.title));
      const location = [card.target.section,card.target.file_path].filter(Boolean).join(' · ');
      detail.append(el('p','插入位置 · ' + location),el('p','图注 · ' + card.target.caption));
      if (card.target.excerpt) detail.append(el('pre',card.target.excerpt,'result-insert-excerpt'));
      sources.append(el('p','图像 · v' + card.asset.version_no),el('p','正文基线 · ' + card.target.base_file_version));
    } else if (card.columns) {
      detail.append(renderTable(card));
      if (card.rows.length < card.row_count) detail.append(el('p','预览前 ' + card.rows.length + ' 行','result-author-status'));
    }
    if (card.version) {
      sources.append(el('p',card.version.filename + ' · v' + card.version.version_no),
        link('下载数据表',card.version.download_url),
        button('打开来源表版本',() => open(versionRoute(card.source))));
      originalLinks(sources,project,card.version,card.collection_id);
    }
    if (card.filename) {
      sources.append(el('p','附件 · ' + card.filename));
      if (!card.receipt && card.attachment_id) sources.append(link('下载附件原件',
        '/api/projects/' + segment(project) + '/agent/conversations/' + segment(card.conversation_id)
        + '/attachments/' + segment(card.attachment_id) + '/download'));
    }
    if (card.other_source) sources.append(button('查看另一份来源 · ' + card.other_source.origin,() => open(versionRoute(card.other_source))));
    if (card.comparison) sources.append(button('查看差异清单',() => open(versionRoute(card.comparison))));
    if (card.paper_figure) sources.append(button('查看论文原图',() => open(figureRoute(card.paper_figure))));
    const explanation = operationsSummary(card.operations);
    if (explanation) sources.append(el('p','本次处理 · ' + explanation));
    if (card.receipt?.original_download_url) sources.append(link('下载归档原件',card.receipt.original_download_url));
    if (sources.children.length > 1) detail.append(sources);
    if (scoped(card) && !readOnly() && card.state === 'ready' && actions[card.kind]) {
      const confirm = button(actions[card.kind],async () => {
        const epoch = detailEpoch;
        confirm.disabled = true; notice.textContent = '正在写入…';
        try {
          const updated = await postDecision(card,'confirm');
          if (!updated || epoch !== detailEpoch) return;
          if (card.kind === 'chart') store.write(project,card.conversation_id,'resultReference',
            JSON.stringify({title:updated.title,reference:{reference_id:updated.id}}));
          renderReference();
          await renderDetail(project,cardRoute(updated));
        } catch (error) {
          if (epoch !== detailEpoch) return;
          notice.textContent = error.message; confirm.disabled = false;
          if (['editor_version_conflict','draft_checkpoint_required','section_changed'].includes(error.code)) {
            notice.append(button('回到对话重新预览',() => useReference(card.title,{reference_id:card.id},card.conversation_id,'重新预览插入位置')));
          }
        }
      },true);
      bar.append(confirm);
    }
    if (card.state === 'saved') {
      if (card.kind === 'chart') bar.append(button('查看已保存图表',() => open(figureRoute(card.receipt)),true));
      if (card.receipt.dataset_id) bar.append(button('查看已保存版本',() => open(versionRoute(card.receipt)),true));
      if (card.kind === 'insert') bar.append(button('打开正文',() => pp.openPaperTool({route:'write',target:{file_path:card.target.file_path}}),true));
    }
    if (scoped(card) && !readOnly() && (card.source || card.receipt?.dataset_id || card.kind === 'insert')) {
      bar.append(button('在对话中继续',() => useReference(card.title,{reference_id:card.id},card.conversation_id)));
    }
    detail.append(bar,notice);
  }
  function tableDetail(project, item, table, history) {
    detail.append(heading(item.display_title || item.title,'数据表','v' + table.version_no + ' · ' + table.row_count + ' 行 · ' + table.columns.length + ' 列'),renderTable(table));
    if (table.rows.length < table.row_count) detail.append(el('p','预览前 ' + table.rows.length + ' 行','result-author-status'));
    const sources = sourceDetails();
    sources.append(el('p',table.filename),link('下载此版本',table.download_url));
    let raw = table;
    while (raw.parent_version_id) {
      const parent = history.find(version => version.id === raw.parent_version_id);
      if (!parent) break;
      raw = parent;
    }
    if (raw.id !== table.id) sources.append(link('下载归档原件',raw.download_url));
    originalLinks(sources,project,raw,item.collection_id);
    const operation = table.operation;
    if (operation && operation.kind !== 'source_table') {
      sources.append(el('p','处理记录 · ' + operationsSummary(operation.steps || [operation])));
    }
    if (item.paper_figure) sources.append(button('查看论文原图',() => open(figureRoute(item.paper_figure))));
    if (history.length > 1) {
      const prior = el('ul');
      for (const version of history) {
        const row = el('li');
        const label = 'v' + version.version_no + ' · ' + version.filename;
        row.append(version.id === table.id ? el('span',label + '（正在查看）')
          : button(label,() => open(versionRoute({dataset_id:item.dataset_id,version_id:version.id}))));
        prior.append(row);
      }
      sources.append(el('h4','数据版本'),prior);
    }
    for (const image of (table.images || []).filter(image => image.asset_status === 'active')) {
      sources.append(button(image.title + ' · 图像 v' + image.version_no,() => open(figureRoute(image))));
    }
    detail.append(sources);
    if (!readOnly()) detail.append(button('在对话中继续',() => useReference(item.title + ' · v' + table.version_no,
      {dataset_id:item.dataset_id,version_id:table.id},cid()),true));
  }
  async function renderDetail(project, path) {
    const epoch = ++detailEpoch;
    const [type, owner, identity] = path.slice(detailRoute.length).split('/').map(decodeURIComponent);
    const visible = () => project === pid() && epoch === detailEpoch && currentPath === path;
    const back = () => libraryFocus ? [button('返回结果与图表',() => open(libraryRoute))] : [];
    detail.replaceChildren(status('正在读取固定版本…'));
    try {
      if (type === 'card') {
        const card = await get(cardPath({project_id:project,conversation_id:owner,id:identity}));
        if (!visible()) return;
        detail.replaceChildren(...back()); showCardDetail(project,card); return;
      }
      if (type === 'table') {
        const data = {dataset_id:owner,version_id:identity};
        const [table, dataset, info] = await Promise.all([
          get(versionPath(project,data)),get(base(project) + '/' + segment(owner)),get(base(project) + '/library')
        ]);
        if (!visible()) return;
        const item = info.groups.flatMap(group => group.items).find(entry => entry.dataset_id === owner) || {...data,title:dataset.title};
        detail.replaceChildren(...back()); tableDetail(project,item,table,dataset.versions); return;
      }
      if (type !== 'figure') throw new Error('请选择一个结果版本。');
      const data = {asset_id:owner,version_id:identity};
      const [asset,info] = await Promise.all([
        get('/api/projects/' + segment(project) + '/drawing/assets/' + segment(owner)),get(base(project) + '/library')
      ]);
      if (!visible()) return;
      const version = asset?.versions.find(item => item.id === identity);
      if (!version) throw new Error('此图像版本暂不可读。');
      const catalog = info.groups.flatMap(group => group.items).flatMap(item => [item,...item.figures]);
      const title = catalog.find(item => item.asset_id === owner)?.display_title || asset.title;
      const source = version.result_source;
      const sources = sourceDetails();
      detail.replaceChildren(...back(),heading(title,'图像','v' + version.version_no + (source ? ' · ' + source.point_count + ' 个点' : '')),
        figure(version.preview_url,title));
      sources.append(link('下载图像原件',version.download_url));
      if (source) {
        sources.append(button('打开数据 v' + source.version_no,() => open(versionRoute(source))));
        const description = operationsSummary(source.operations);
        if (description) sources.append(el('p','绘图前处理 · ' + description));
      }
      if (asset.versions.length > 1) {
        const history = el('ul');
        for (const former of asset.versions) {
          const row = el('li');
          const label = '图像 v' + former.version_no;
          row.append(former.id === identity ? el('span',label + '（正在查看）')
            : button(label,() => open(figureRoute({asset_id:owner,version_id:former.id}))));
          history.append(row);
        }
        sources.append(el('h4','图像版本'),history);
      }
      detail.append(sources);
      const controls = el('div',undefined,'result-author-actions');
      if (!readOnly() && asset.status === 'active') controls.append(button('在对话中继续',() => useReference(
        asset.title + ' · v' + version.version_no,data,cid()),true));
      controls.append(button('在图像库打开',() => pp.openVisualAsset(owner,identity,{archived:asset.status === 'archived'})));
      detail.append(controls);
    } catch (error) {
      if (!visible()) return;
      detail.replaceChildren(status(error.message),button('重新读取',() => renderDetail(project,path)));
    }
  }
  const moduleVersion = pp.openResultVersion;
  pp.openResultVersion = source => pp.resultModuleView ? moduleVersion(source) : open(versionRoute(source));
  document.addEventListener('paperpilot:artifact-opened',event => {
    currentPath = event.detail.path;
    if (currentPath === libraryRoute) loadLibrary();
    else if (currentPath.startsWith(detailRoute)) renderDetail(pid(),currentPath);
    else detailEpoch++;
  });
  document.addEventListener('paperpilot:view-changed',event => {
    if (event.detail.view !== 'chat') return;
    currentPath = ''; detailEpoch++; libraryFocus = null;
    if (returnFocus) {
      const trigger = returnFocus; returnFocus = null;
      requestAnimationFrame(() => { if (trigger.isConnected && trigger.closest('#agentThread') && document.activeElement === $('#agentInput')) trigger.focus(); });
    }
  });
  document.addEventListener('paperpilot:agent-selected',renderReference);
  document.addEventListener('paperpilot:result-saved',event => {
    const card = event.detail.card;
    const row = $('#agentThread').querySelector('[data-result-card="' + CSS.escape(card.id) + '"]');
    if (row && scoped(card)) drawCard(row,card);
  });
  document.addEventListener('paperpilot:project-changed',() => {
    ++libraryEpoch; ++detailEpoch; currentPath = ''; returnFocus = null; libraryFocus = null;
    search.value = ''; results.replaceChildren(); detail.replaceChildren();
    libraryData = null; openGroups.clear();
    organizing = false; organize.hidden = true; organize.textContent = '整理分组'; organize.setAttribute('aria-pressed','false');
    groupWithAI.hidden = true;
    if (dialog.open) dialog.close();
    composerRef.replaceChildren(); composerRef.hidden = true;
  });
  if (location.hash.startsWith('#chat/' + detailRoute) && pid()) {
    currentPath = location.hash.slice(6); renderDetail(pid(),currentPath);
  } else if (location.hash === '#chat/' + libraryRoute && pid()) loadLibrary();
})();
