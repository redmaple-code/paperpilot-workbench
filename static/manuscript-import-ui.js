(() => {
  const pp = window.paperpilot;
  if (!pp) return;
  const endpoint = '/api/manuscript-imports';
  const $ = selector => document.querySelector(selector);
  const node = (tag, text, attrs = {}) => {
    const element = document.createElement(tag);
    if (text != null) element.textContent = text;
    for (const [name, value] of Object.entries(attrs)) element.setAttribute(name, value);
    return element;
  };
  const button = (text, action) => {
    const element = node('button', text, {type:'button'});
    element.onclick = () => run(action);
    return element;
  };
  const label = (text, input) => {
    const element = node('label', text);
    element.append(input);
    return element;
  };
  const expandable = (title, ...content) => {
    const element = node('details');
    element.append(node('summary', title), ...content);
    return element;
  };
  const pre = text => node('pre', text, {class:'manuscript-pre'});
  const statusNames = {grounded:'有来源',draft:'待审阅',suggestion:'建议草案',missing:'待补',new:'可新增',conflict:'和已有条目冲突',
    applied:'已采纳',review:'待核对',staged:'待导入',cancelled:'已取消',failed:'失败',running:'运行中',partial:'部分成功',
    succeeded:'完成',unknown:'发送结果待核对',stale:'预览已失效',interrupted:'已中断'};
  const labels = {pdf:'PDF',docx:'DOCX',text:'正文或笔记',tex:'LaTeX',bibliography:'书目',image:'图像',page_asset:'图表原页',table:'表格',archive:'ZIP 工程',support:'工程支持文件'};
  const advice = {add:'新增',replace:'替换已有条目',keep:'保留并使用已有条目',skip:'跳过',enable_planning:'启用研究规划并保存草稿'};
  let batch = null, projects = [], model = null, preview = null, busy = false, applyPending = null;
  let chosenChunks = [], chosenItems = new Map(), uploadKeys = new WeakMap(), modelPending = null;
  let resultModule = '', resultFilter = 'all', resultQuery = '', resultPage = 1;
  const projectsNeedingRefresh = new Set();
  const dialog = node('dialog',null,{class:'manuscript-import-dialog','aria-labelledby':'manuscriptImportTitle'});
  const title = node('h2','导入已有论文',{id:'manuscriptImportTitle'});
  const status = node('p',null,{id:'manuscriptImportStatus',role:'status','aria-live':'polite'});
  const content = node('div',null,{class:'manuscript-import-content'});
  const header = node('div',null,{class:'paper-directory-heading'});
  const close = node('button','关闭',{type:'button'});
  close.onclick = () => dialog.close();
  header.append(title,close);
  dialog.append(header,status,content);
  document.body.append(dialog);

  async function request(path, method='GET', body) {
    const response = await fetch(path,{method,headers:body ? {'Content-Type':'application/json'} : {},
      body:body ? JSON.stringify(body) : undefined});
    const data = await response.json();
    if (!response.ok) throw new Error(data.detail?.message || (Array.isArray(data.detail) ? '输入不符合要求。' : '操作失败，请刷新后重试。'));
    return data;
  }
  const path = tail => `${endpoint}/${encodeURIComponent(batch.id)}${tail}`;
  function setBatch(value, initialPreview=null) {
    if (batch?.id !== value.id) {
      resultModule = ''; resultFilter = 'all'; resultQuery = ''; resultPage = 1;
    }
    batch = value;
    model = null;
    preview = initialPreview;
    chosenChunks = [];
    chosenItems.clear();
    renderBatch();
  }
  async function openBatch(id) {
    const url=`${endpoint}/${encodeURIComponent(id)}`;
    const value=await request(url);
    const initialPreview=value.adopted.length ? await request(`${url}/preview`) : null;
    setBatch(value,initialPreview);
    dialog.scrollTop = 0;
  }
  async function run(action) {
    if (busy) return;
    busy = true;
    dialog.setAttribute('aria-busy','true');
    status.textContent = '';
    try { await action(); }
    catch (error) { status.textContent = error.message; }
    finally { busy = false; dialog.removeAttribute('aria-busy'); }
  }
  async function startScreen() {
    const [all,history] = await Promise.all([request('/api/projects'),request(endpoint)]);
    projects = all.filter(item=>!item.archived && item.lifecycle_state!=='published');
    batch = null;
    content.replaceChildren();
    const select = node('select',null,{'aria-label':'导入目标'});
    select.append(new Option('新建论文',''));
    for (const project of projects) select.append(new Option(project.name,project.id));
    if (projects.some(item=>item.id===pp.current?.project?.id)) select.value=pp.current.project.id;
    const name = node('input',null,{maxlength:160,placeholder:'新论文名称'});
    const nameLabel = label('新论文名称',name);
    const refreshTarget = () => {nameLabel.hidden = Boolean(select.value);};
    select.onchange=refreshTarget;
    refreshTarget();
    const create=button('选择稿件和资料',async()=>{
      const data=await request(endpoint,'POST',{request_id:crypto.randomUUID(),target_mode:select.value?'existing':'new',
        name:name.value.trim(),project_id:select.value||null});
      setBatch(data);
    });
    content.append(label('导入目标',select),nameLabel,create);
    if (history.batches.length) {
      const list=node('ul');
      for (const item of history.batches) {
        const li=node('li');
        li.append(button(`${item.name} · ${statusNames[item.status]||item.status}`,()=>openBatch(item.id)));
        list.append(li);
      }
      content.append(expandable('历史导入批次',list));
    }
  }
  function fileBase64(file) {
    return new Promise((resolve,reject)=>{
      const reader = new FileReader();
      reader.onload=()=>resolve(String(reader.result).split(',')[1]);
      reader.onerror=()=>reject(new Error(`无法读取 ${file.name}。`));
      reader.readAsDataURL(file);
    });
  }
  function sourceCard(source,readOnly) {
    const title=`${labels[source.kind]||source.kind} · ${source.name}${source.location?' · '+source.location:''}`;
    const meta=node('div',null,{class:'manuscript-source-note'});
    meta.append(node('a','下载原件',{href:path(`/sources/${source.id}/original`)}));
    if (!readOnly) meta.append(button('查看移除影响',()=>confirmRemoval(source)));
    for (const warning of source.warnings) meta.append(node('p',warning));
    for (const fragment of source.fragments) {
      const location=fragment.location+(fragment.heading?' · '+fragment.heading:'');
      const body=expandable(location, pre(fragment.text || fragment.issue || fragment.heading));
      if (fragment.issue) body.append(node('p',fragment.issue));
      if (!readOnly && ['pdf','docx','text'].includes(source.kind) && fragment.text.length<=16000) {
        body.append(button('校对这段文字',()=>{
          if (body.querySelector('textarea')) return;
          const edit=node('textarea',null,{rows:6,maxlength:16000,'aria-label':location+' 校对'});
          edit.value=fragment.text;
          body.append(edit,button('保存校对',async()=>setBatch(await request(path(`/sources/${source.id}/fragments/${fragment.id}`),'PUT',
            {text:edit.value,revision:batch.revision}))));
        }));
      }
      if (!readOnly && fragment.page && (fragment.issue || fragment.mode==='ocr')) {
        body.append(button('重试此页识别',async()=>{
          status.textContent=`正在识别第 ${fragment.page} 页…`;
          setBatch(await request(path(`/sources/${source.id}/retry-page`),'POST',{page:fragment.page,revision:batch.revision}));
          status.textContent='识别已完成，请核对提取文本。';
        }));
      }
      meta.append(body);
    }
    return expandable(title,meta);
  }
  async function confirmRemoval(source=null) {
    const result = await request(path('/removal-preview')+(source?`?source_id=${source.id}`:''));
    const panel=node('section',null,{class:'manuscript-callout'});
    panel.append(node('h3',source?'移除此来源':'取消导入批次'),node('p',`将清理 ${result.sources.length} 份来源。`));
    if (result.impact.length) {
      const list=node('ul');
      for (const item of result.impact) list.append(node('li',item.title));
      panel.append(node('p','这些已采纳的成果会保留，可从工作区版本或资源入口管理。'),list);
    }
    panel.append(button('确认清理',async()=>{
      setBatch(await request(path(source?`/sources/${source.id}/remove`:'/cancel'),'POST',{fingerprint:result.fingerprint}));
      status.textContent='已清理所选暂存来源。';
    }),button('返回',()=>panel.remove()));
    content.querySelector('.manuscript-callout')?.remove();
    content.prepend(panel);
    panel.querySelector('button').focus();
  }
  function renderAnalysis(panel) {
    if (!model) return;
    if (model.unavailable) panel.append(node('p',model.unavailable));
    if (model.provider) panel.append(node('p',`${model.provider.name} · ${model.provider.model} · ${model.provider.base_url}`));
    const chunks=node('fieldset');
    chunks.append(node('legend',`选择要分析的片段（共 ${model.total_chunks} 批，每次最多 6 批）`));
    for (const item of model.chunks) {
      const choice=node('input',null,{type:'checkbox'});
      choice.checked=chosenChunks.includes(item.index);
      choice.onchange=()=>{
        if (choice.checked && chosenChunks.length>=6) {choice.checked=false; status.textContent='一次最多选 6 批。'; return;}
        const next=new Set(chosenChunks);
        if (choice.checked) next.add(item.index); else next.delete(item.index);
        chosenChunks=[...next].sort((a,b)=>a-b);
        panel.querySelector('.manuscript-send')?.remove();
      };
      chunks.append(label(`第 ${item.index+1} 批 · ${item.characters} 字 · ${item.sources.join('、')}`,choice));
    }
    panel.append(chunks,button('更新发送预览',async()=>{
      model=await request(path('/model-preview'),'POST',{chunks:chosenChunks});
      renderBatch();
    }));
    const send=node('div',null,{class:'manuscript-send'});
    if (model.allowed) {
      send.append(expandable('模型任务说明',pre(model.prompt)));
      for (const item of model.selected) send.append(expandable(`第 ${item.index+1} 批实际发送材料`,pre(item.payload)));
      const consent=node('input',null,{type:'checkbox'});
      const fingerprint=model.fingerprint;
      const execute=button('确认并开始 AI 拆分',async()=>{
        if (!consent.checked || model?.fingerprint!==fingerprint) return;
        status.textContent='正在分析选定片段，批次将保留已返回的结果…';
        if (modelPending?.fingerprint!==fingerprint) modelPending={fingerprint,id:crypto.randomUUID()};
        setBatch(await request(path('/model-runs'),'POST',{request_id:modelPending.id,fingerprint,
          chunks:model.selected_indices,authorize:true}));
        modelPending=null;
        preview=await request(path('/preview'));
        renderBatch();
        status.textContent='AI 拆分已处理，请核对各批状态和模块结果。';
      });
      execute.disabled=true;
      consent.onchange=()=>execute.disabled=!consent.checked;
      send.append(label('确认以上材料与接收方',consent),execute);
    }
    panel.append(send);
  }
  function renderCandidate(item,readonly) {
    const row=node('article',null,{class:'manuscript-candidate'});
    const heading=node('div',null,{class:'manuscript-result-row-heading'});
    heading.append(node('h5',item.title),node('span',statusNames[item.adoption]||statusNames[item.status],{class:'manuscript-result-state','data-adoption':item.adoption}));
    row.append(heading,node('p',[statusNames[item.status],item.origin==='ai'?'AI 拆分':'本地提取'].filter(Boolean).join(' · '),{class:'manuscript-result-meta'}));
    const data=item.data;
    const sample=data.content || data.text || data.definition || data.caption || data.notes || data.path || '';
    if (sample) row.append(expandable('查看候选内容',pre(sample)));
    if (item.current && item.adoption!=='applied') row.append(expandable('查看已有内容与影响',pre(JSON.stringify(item.current,null,2))));
    if (item.impact?.length) row.append(expandable('受影响的引用或阶段',pre(item.impact.map(i=>`${i.path||i.stage_id||i.consumer_id} ${i.reason||i.consumer_kind||''}`).join('\n'))));
    if (item.dependencies?.length) row.append(node('p','需同时选择：'+item.dependencies.map(id=>preview.items.find(entry=>entry.id===id)?.title||id).join('、')));
    const sources=new Map();
    for (const source of item.evidence) {
      const key=JSON.stringify([source.source_id,source.name,source.location]);
      if (!sources.has(key)) sources.set(key,{...source,quotes:new Set()});
      if (source.quote) sources.get(key).quotes.add(source.quote);
    }
    for (const source of sources.values()) {
      const evidence=expandable(`来源 · ${source.name} · ${source.location}`,pre([...source.quotes].join('\n\n')||'见原件'));
      if (batch.sources.some(item=>item.id===source.source_id)) evidence.append(node('a','下载对应原件',{href:path(`/sources/${source.source_id}/original`)}));
      row.append(evidence);
    }
    if (!readonly && item.choices?.length) {
      const action=node('select',null,{'aria-label':item.title+' 采纳方式'});
      action.append(new Option('暂不采纳',''));
      for (const value of item.choices) action.append(new Option(advice[value],value));
      action.value=chosenItems.get(item.id)||'';
      action.onchange=()=>{
        if (action.value) chosenItems.set(item.id,action.value); else chosenItems.delete(item.id);
        updateApplyCount();
      };
      row.append(action);
    }
    return row;
  }
  function updateApplyCount() {
    const count = content.querySelector('#manuscriptApplyCount');
    const total=[...chosenItems.values()].filter(x=>x!=='skip').length;
    if (count) count.textContent=`已选择 ${total} 项`;
    const apply=content.querySelector('#manuscriptApplySelection');
    if (apply) apply.disabled=total===0;
  }
  async function openTool(id) {
    if (!batch.project_id) return;
    const pid=batch.project_id;
    if (pp.current?.project?.id!==pid || projectsNeedingRefresh.has(pid)) {
      if (pp.current?.project?.id && await pp.flushEditorChanges?.()===false) return;
      await pp.loadProject(pid);
      if (pp.current?.project?.id===pid) projectsNeedingRefresh.delete(pid);
    }
    if (pp.current?.project?.id!==pid) return;
    const directory=id ? await request(`/api/projects/${pid}/agent/tools`) : null;
    const tool=directory?.items.find(item=>item.id===id);
    if (id && !tool) throw new Error('此工作区入口暂不可用。');
    dialog.close();
    if (tool) await pp.openPaperTool?.(tool);
  }
  function renderResultBrowser(panel, readonly) {
    const modules = preview.modules.map(module=>{
      const items = preview.items.filter(item=>item.module===module.id);
      return {...module, items, adopted:items.filter(item=>item.adoption==='applied').length};
    });
    if (!modules.some(module=>module.id===resultModule)) resultModule=modules[0]?.id;
    const nav=node('nav',null,{class:'manuscript-result-nav','aria-label':'拆分模块'});
    const region=node('div',null,{class:'manuscript-result-region'});
    const heading=node('h4');
    const open=node('div',null,{class:'manuscript-actions'});
    const top=node('div',null,{class:'manuscript-result-heading'});
    top.append(heading,open);
    const search=node('input',null,{type:'search',placeholder:'查找标题、内容或来源页', 'aria-label':'搜索当前模块'});
    const filter=node('select',null,{'aria-label':'筛选采纳状态'});
    for (const [key,name] of [['all','全部状态'],['applied','已采纳'],['remaining','待处理']]) filter.append(new Option(name,key));
    const controls=node('div',null,{class:'manuscript-result-controls'});
    controls.append(search,filter);
    const list=node('div',null,{class:'manuscript-result-list'});
    const count=node('p',null,{role:'status',class:'manuscript-result-meta'});
    const pager=node('div',null,{class:'manuscript-result-pager'});
    const previous=button('上一页',()=>{ resultPage--; draw(); });
    const next=button('下一页',()=>{ resultPage++; draw(); });
    const pageLabel=node('span');
    pager.append(previous,pageLabel,next);
    region.append(top,controls,count,list,pager);

    function draw() {
      const module=modules.find(entry=>entry.id===resultModule);
      const query=resultQuery.trim().toLocaleLowerCase();
      const matches=module.items.filter(item=>{
        const adopted=item.adoption==='applied';
        if (resultFilter==='applied' && !adopted || resultFilter==='remaining' && adopted) return false;
        return !query || [item.title,...Object.values(item.data).filter(value=>typeof value==='string'),
          ...item.evidence.flatMap(ref=>[ref.name,ref.location,ref.quote])].join('\n').toLocaleLowerCase().includes(query);
      });
      const pageSize=12;
      const pages=Math.max(1,Math.ceil(matches.length/pageSize));
      resultPage=Math.max(1,Math.min(resultPage,pages));
      const start=(resultPage-1)*pageSize;
      list.replaceChildren(...matches.slice(start,start+pageSize).map(item=>renderCandidate(item,readonly)));
      list.scrollTop=0;
      if (!matches.length) list.append(node('p','没有符合当前条件的条目。',{class:'manuscript-result-empty'}));
      count.textContent=`显示 ${matches.length ? start+1 : 0}–${Math.min(start+pageSize,matches.length)} / ${matches.length} 项`;
      pageLabel.textContent=`第 ${resultPage} / ${pages} 页`;
      pager.hidden=pages===1;
      previous.disabled=resultPage===1;
      next.disabled=resultPage===pages;
    }
    function selectModule() {
      const module=modules.find(entry=>entry.id===resultModule);
      heading.textContent=module.title;
      open.replaceChildren();
      if (batch.project_id) open.append(button(`打开${module.title}`,()=>openTool(module.entry)));
      for (const control of nav.children) control.setAttribute('aria-pressed',String(control.dataset.module===module.id));
      search.value=resultQuery;
      filter.value=resultFilter;
      draw();
    }
    for (const module of modules) {
      const control=button('',()=>{
        if (resultModule===module.id) return;
        resultModule=module.id; resultQuery=''; resultPage=1;
        selectModule();
      });
      control.dataset.module=module.id;
      control.append(node('span',module.title),node('small',`${module.adopted} / ${module.items.length}`));
      control.setAttribute('aria-label',`${module.title} · ${module.items.length} 项，${module.adopted} 项已采纳`);
      nav.append(control);
    }
    search.oninput=()=>{ resultQuery=search.value; resultPage=1; draw(); };
    filter.onchange=()=>{ resultFilter=filter.value; resultPage=1; draw(); };
    const browser=node('div',null,{class:'manuscript-result-browser'});
    browser.append(nav,region);
    panel.append(browser);
    selectModule();
  }
  function renderPreview(panel) {
    if (!preview) return;
    const readonly=preview.read_only;
    const toolbar=node('div',null,{class:'manuscript-actions manuscript-result-toolbar'});
    const count=node('span',null,{id:'manuscriptApplyCount'});
    const adopted=preview.items.filter(item=>item.adoption==='applied').length;
    const summary=node('div',null,{class:'manuscript-result-summary'});
    summary.append(node('h3','拆分结果'),node('span',`${preview.items.length} 项候选`),node('span',`${adopted} 项已采纳`));
    panel.append(summary);
    if (!readonly) {
      const selectSuggested=button('选择可新增的有来源条目',()=>{
        for (const item of preview.items) if (item.suggested) chosenItems.set(item.id,'add');
        renderBatch();
      });
      if (preview.items.some(item=>item.suggested)) toolbar.append(selectSuggested);
      const apply=button('采纳所选',async()=>{
        const decisions=Object.fromEntries(chosenItems);
        if (!Object.values(decisions).some(x=>x!=='skip')) throw new Error('请先选一项要采纳的内容。');
        if (Object.values(decisions).includes('replace') && !window.confirm('所选条目包含替换。确定按预览写入对应内容？')) return;
        if (Object.values(decisions).includes('enable_planning') && !window.confirm('为旧论文启用研究规划，并将所选研究内容保存为草稿？')) return;
        const signature=JSON.stringify([preview.fingerprint,decisions]);
        if (applyPending?.signature!==signature) applyPending={signature,id:crypto.randomUUID()};
        const result=await request(path('/apply'),'POST',{request_id:applyPending.id,fingerprint:preview.fingerprint,decisions});
        applyPending=null;
        if (result.project_id) projectsNeedingRefresh.add(result.project_id);
        const [value, nextPreview]=await Promise.all([request(path('')),request(path('/preview'))]);
        setBatch(value,nextPreview);
        const failed=result.outcomes.filter(group=>group.status==='failed');
        status.textContent=`${result.outcomes.length-failed.length} 组已采纳，${failed.length} 组失败。 ${failed.map(x=>x.message).join(' ')}`;
      });
      apply.id='manuscriptApplySelection';
      toolbar.append(apply,count);
    }
    if (batch.project_id) toolbar.append(button('打开论文工作区',()=>openTool()));
    if (batch.sources.some(source=>['pdf','docx','text','tex'].includes(source.kind))) {
      const batchId=batch.id;
      toolbar.append(button('识别并比对术语',()=>pp.openTermComparison?.({batchId,onApply:()=>openBatch(batchId)})));
    }
    panel.append(toolbar);
    if (preview.modules.length) renderResultBrowser(panel,readonly);
    if (batch.project_id) panel.append(expandable('继续使用工作区工具',...preview.tools.map(id=>button(toolNames[id]||id,()=>openTool(id)))));
    updateApplyCount();
  }
  const toolNames={papers:'论文库',revision:'人工修订',companion:'伴写',follow:'跟写',continue:'续写',modify:'修改选区',style:'文风学习',
    search:'发现论文',import:'获取公开全文',draw:'编辑图像',vision:'视觉核验',compare:'文献支撑',citation:'引用检查',compile:'编译',pdf:'PDF 预览',
    release:'发布与导出',progress:'进展',resources:'资源待办',versions:'内容版本',revisions:'修订记录',recovery:'恢复副本',tasks:'AI 任务',
    domain:'领域资料',paper:'论文设置',model:'模型设置',agent:'发送与预算'};
  function renderBatch() {
    if (!batch) return;
    content.replaceChildren();
    const readonly=batch.status==='cancelled';
    const batchHeader=node('div',null,{class:'manuscript-batch-heading'});
    batchHeader.append(button('返回导入列表',startScreen),node('h3',batch.name));
    content.append(batchHeader);
    if (preview) {
      const panel=node('section',null,{class:'manuscript-result-panel','aria-label':'拆分结果'});
      content.append(panel);
      renderPreview(panel);
    } else content.append(node('p',statusNames[batch.status]||batch.status));
    if (!readonly) {
      const input=node('input',null,{type:'file',multiple:'multiple',accept:'.pdf,.docx,.md,.markdown,.txt,.tex,.zip,.bib,.csv,.png,.jpg,.jpeg,.webp,.svg','aria-label':'选择稿件和辅助材料'});
      content.append(input,button('暂存并解析文件',async()=>{
        if (!input.files.length) throw new Error('请选择文件。');
        try {
          for (const file of input.files) {
            if (file.size>20*1024*1024) throw new Error(`${file.name} 超过 20 MB，请拆分后再上传。`);
            status.textContent=`正在解析 ${file.name}…`;
            if (!uploadKeys.has(file)) uploadKeys.set(file,crypto.randomUUID());
            batch=await request(path('/sources'),'POST',{request_id:uploadKeys.get(file),filename:file.name,content_base64:await fileBase64(file)});
          }
        } finally {
          setBatch(batch);
        }
        status.textContent='已暂存，请检查来源和解析状态。';
      }),button('清理未采纳来源',()=>confirmRemoval()));
      if (batch.main_options.length>1) {
        const select=node('select',null,{'aria-label':'正文主源'});
        select.append(new Option('请选择主稿',''));
        for (const item of batch.main_options) select.append(new Option(item.name,item.id));
        select.value=batch.primary_source_id;
        select.onchange=()=>run(async()=>{
          if (select.value) setBatch(await request(path('/primary'),'PUT',{source_id:select.value}));
        });
        content.append(label('正文主源',select));
      }
    }
    if (batch.sources.length) content.append(expandable(`来源文件与解析 · ${batch.sources.length} 份`,...batch.sources.map(item=>sourceCard(item,readonly))));
    if (batch.analyses.length) {
      const list=node('ul');
      for (const value of batch.analyses) {
        const outcomes=value.result.outcomes||[];
        list.append(node('li',`${statusNames[value.status]||value.status} · ${outcomes.map(v=>`第 ${v.index+1} 批 ${statusNames[v.status]||v.status}${v.message?' '+v.message:''}`).join('；')}`));
      }
      content.append(expandable('AI 分析记录',list));
    }
    const actions=node('div',null,{class:'manuscript-actions'});
    if (!readonly && batch.sources.length) actions.append(button('预览本地拆分',async()=>{
      preview=await request(path('/preview'));
      chosenItems.clear();
      renderBatch();
    }),button('查看 AI 发送范围',async()=>{
      model=await request(path('/model-preview'),'POST',{});
      chosenChunks=[...model.selected_indices];
      renderBatch();
    }));
    content.append(actions);
    if (model) {
      const panel=node('section');
      renderAnalysis(panel);
      content.append(panel);
    }
    if (batch.receipts?.length) content.append(expandable('采纳历史',...batch.receipts.map(receipt=>{
      const lines=receipt.outcomes.map(group=>node('p',`${statusNames[group.status]||group.status} · ${group.items.map(i=>i.title).join('、')}${group.message?' · '+group.message:''}`));
      return expandable(`${receipt.outcomes.length} 组采纳结果`,...lines);
    })));
  }
  pp.openManuscriptImport=()=>{
    if (!dialog.open) dialog.showModal();
    return run(startScreen);
  };
  const launcher=button('导入已有论文',()=>{
    if (!dialog.open) dialog.showModal();
    return startScreen();
  });
  launcher.id='openManuscriptImport';
  ($('#conversationAttach') || $('#newProject'))?.after(launcher);
  const resultLauncher=button('导入结果',async()=>{
    content.replaceChildren();
    status.textContent='正在载入导入结果…';
    if (!dialog.open) dialog.showModal();
    const project=pp.current?.project?.id;
    const {batches}=await request(endpoint);
    const latest=batches.find(item=>item.project_id===project && item.status==='applied');
    if (project!==pp.current?.project?.id) return;
    if (latest) {
      await openBatch(latest.id);
      status.textContent='';
    }
    else {
      await startScreen();
      status.textContent='当前论文暂无已采纳的导入结果。';
    }
  });
  resultLauncher.id='openManuscriptResults';
  resultLauncher.classList.add('manuscript-import-artifact');
  $('#paperArtifactTitle')?.after(resultLauncher);
  $('#newDialog menu')?.prepend(button('导入已有论文',async()=>{
    $('#newDialog').close();
    if (!dialog.open) dialog.showModal();
    await startScreen();
  }));
  const importId=new URLSearchParams(location.search).get('import');
  if (importId) {
    dialog.showModal();
    run(()=>openBatch(importId));
    dialog.addEventListener('close',()=>{
      const target=new URL(location.href);
      target.searchParams.delete('import');
      history.replaceState(null,'',target);
    },{once:true});
  }
})();
