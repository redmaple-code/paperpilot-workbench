(() => {
  const pp = window.paperpilot;
  if (!pp) return;

  const states = { new:'新增', supplement:'可补充', covered:'已有', conflict:'待判断' };
  const grades = { fulltext:'全文', excerpt:'原文摘录', abstract:'摘要', metadata:'仅书目信息', unreadable:'附件不可读' };
  const sourceLabels = { manuscript:'稿件', reference:'文献', domain:'领域术语库' };
  const readableGrades = new Set(['fulltext','excerpt','abstract']);
  const readableReference = source => source.kind === 'reference' && readableGrades.has(source.grade) && Boolean(source.quote?.trim());
  const hasLiterature = item => item.sources.some(readableReference);
  const size = 12;
  const node = (tag, text, attributes={}) => {
    const el = document.createElement(tag);
    if (text != null) el.textContent = text;
    for (const [name, value] of Object.entries(attributes)) el.setAttribute(name, value);
    return el;
  };
  const button = (text, click, className) => {
    const el = node('button', text, {type:'button', ...(className ? {class:className} : {})});
    el.onclick = click;
    return el;
  };
  const label = (text, control) => {
    const el = node('label', text);
    el.append(control);
    return el;
  };
  const disclosure = (text, ...children) => {
    const el = node('details');
    el.append(node('summary', text), ...children);
    return el;
  };
  const post = (url, data) => pp.api(url, {method:'POST', body:JSON.stringify(data)});
  const currentProject = () => pp.current?.project?.id;
  const aliasNames = value => value.split(/[,，;；、\n]/).map(item=>item.trim()).filter(Boolean);

  function dialog(id, title) {
    const modal = node('dialog',null,{id,class:'term-comparison-dialog','aria-labelledby':id+'Title'});
    const heading = node('div',null,{class:'term-comparison-header'});
    heading.append(node('h2',title,{id:id+'Title'}),button('关闭',()=>modal.close()));
    const status = node('p',null,{role:'status',class:'term-comparison-status'});
    const body = node('div');
    modal.append(heading,status,body);
    document.body.append(modal);
    return {modal,status,body};
  }

  const compare = dialog('termComparisonDialog','识别并比对术语');
  const reader = dialog('termSourceDialog','术语来源');
  let comparison = null;

  function errorText(error) {
    try {
      const {detail} = JSON.parse(error.message);
      return detail?.message || (typeof detail === 'string' ? detail : '操作未完成，请核对。');
    }
    catch { return error.message; }
  }

  function evidence(source) {
    const heading = [sourceLabels[source.kind],source.name,source.location,
      source.grade ? grades[source.grade] : ''].filter(Boolean).join(' · ');
    const block = disclosure(heading,node('blockquote',source.quote || '此处没有可显示的摘录。'));
    if (source.definition) block.append(node('p','可选定义 · '+source.definition));
    if (source.suggestion) block.append(node('p','AI 建议草案 · '+source.suggestion));
    if (source.url?.startsWith('/api/manuscript-imports/')) block.append(node('a','下载稿件原件',{href:source.url}));
    if (source.kind==='reference' && (/^https?:\/\//i.test(source.source_url||'') || source.url?.endsWith('/pdf'))) {
      const target = new URL(source.url || source.source_url, location.origin);
      const page = /^page:(\d+)$/.exec(source.location)?.[1];
      if (page) target.hash = `page=${page}`;
      block.append(node('a',source.url ? '打开原件对应页' : '打开文献来源',{href:target.href,target:'_blank',rel:'noopener noreferrer'}));
    }
    return block;
  }

  async function showHistory(projectId, termId) {
    reader.body.replaceChildren(); reader.status.textContent='正在载入术语历史…';
    if (!reader.modal.open) reader.modal.showModal();
    try {
      const result=await pp.api(`/api/projects/${encodeURIComponent(projectId)}/terminology/comparison/${encodeURIComponent(termId)}/history`);
      reader.body.replaceChildren(node('h3',result.term.term));
      for (const entry of result.versions) {
        const section=disclosure(`v${entry.version}${entry.version===result.term.version ? ' · 当前' : ''}`,
          node('p',entry.snapshot.definition||'定义待补'),
          ...(entry.snapshot.aliases ? [node('p','别名 · '+entry.snapshot.aliases)] : []),
          ...entry.sources.map(evidence));
        section.open=entry.version===result.term.version;
        reader.body.append(section);
      }
      reader.status.textContent='';
    } catch (error) { reader.status.textContent=errorText(error); }
  }

  async function showReference(referenceId, projectId, initialVersion) {
    reader.body.replaceChildren(); reader.status.textContent='正在读取文献术语…';
    if (!reader.modal.open) reader.modal.showModal();
    const id=encodeURIComponent(referenceId);
    const endpoint=projectId ? `/api/projects/${encodeURIComponent(projectId)}/references/${id}/terms` : `/api/references/library/${id}/terms`;
    try {
      const record=await pp.api(`/api/references/${id}`);
      const select=node('select',null,{'aria-label':'文献术语版本'});
      for (const revision of record.versions) select.append(new Option(`v${revision.version}`,revision.version));
      select.value=initialVersion || record.current_version;
      const content=node('section');
      reader.body.replaceChildren(node('h3',record.title),label('文献版本',select),content);
      if (projectId) reader.body.append(button('纳入当前论文术语比对',()=>{
        reader.modal.close();
        pp.openTermComparison({projectId,includeReference:referenceId});
      }));
      async function load() {
        const version=select.value;
        select.disabled=true;
        content.replaceChildren();
        try {
          const result=await pp.api(`${endpoint}?version=${encodeURIComponent(version)}`);
          const status=result.status==='fulltext' ? '全文可定位' : grades[result.status];
          const coverage=result.page_count ? `${result.text_page_count}/${result.page_count} 页可检索` : '';
          reader.status.textContent=[status,result.source_edition,coverage,`${result.count} 项术语`,result.limitation].filter(Boolean).join(' · ');
          if (result.pdf_url) content.append(node('a','打开 PDF 原件',{href:result.pdf_url,target:'_blank',rel:'noopener noreferrer'}));
          const groups=new Map();
          result.items.forEach(item=>{
            if (!groups.has(item.term)) groups.set(item.term,[]);
            groups.get(item.term).push(item.evidence);
          });
          for (const [term,sources] of groups) content.append(disclosure(term,...sources.map(evidence)));
          if (!groups.size) content.append(node('p','此版本暂无可识别术语。'));
        } catch (error) { reader.status.textContent=errorText(error); }
        finally { select.disabled=false; }
      }
      select.onchange=load;
      await load();
    } catch (error) { reader.status.textContent=errorText(error); }
  }

  function draw(state) {
    const {preview,choices}=state;
    const summary=preview.summary;
    const header=node('div',null,{class:'term-comparison-summary'});
    for (const text of [`稿件 ${summary.manuscript}`,`文献 ${summary.references}`,`比对后 ${summary.merged}`,
      `待判断 ${summary.conflicts}`,`当前已收录 ${summary.adopted}`,`缺全文 ${summary.missing_fulltext}`]) header.append(node('span',text));

    const references=node('div',null,{class:'term-comparison-reference-list'});
    const referenceInputs=[];
    for (const item of preview.references) {
      const check=node('input',null,{type:'checkbox',value:item.source_id}); check.checked=item.selected;
      referenceInputs.push(check);
      const row=node('div',null,{class:'term-comparison-reference-row'});
      const caption=[item.cite_key,item.name,`v${item.version}`,grades[item.status],
        item.current_version!==item.version ? `有更新版本 v${item.current_version}` : '', item.ambiguous ? '候选匹配' : '',item.limitation].filter(Boolean).join(' · ');
      row.append(label(caption,check),button('术语',()=>showReference(item.source_id,preview.project_id,item.version)));
      references.append(row);
    }
    for (const issue of preview.issues) {
      const message={ambiguous:'多个匹配，按文献版本选择',unmatched:'尚未匹配到文献库',unavailable:'资料不可用'}[issue.status];
      references.append(node('p',[issue.cite_key,issue.title,message].filter(Boolean).join(' · ')));
    }
    if (!references.childElementCount) references.append(node('p','没有可选的关联文献。'));
    const scope=disclosure(`文献对照范围（${preview.reference_ids.length} 项）`,references);
    if (referenceInputs.length) scope.append(button('按所选文献重新比对',()=>{
      state.referenceIds=referenceInputs.filter(input=>input.checked).map(input=>input.value);
      refresh(state);
    }));

    const filters=node('div',null,{class:'term-comparison-filters'});
    const search=node('input',null,{type:'search',placeholder:'搜索术语、别名或出处','aria-label':'搜索比对术语'});
    search.value=state.focusTerm || '';
    const filter=node('select',null,{'aria-label':'术语比对状态'});
    for (const [value,name] of [['all','全部结果'],...Object.entries(states)]) filter.append(new Option(name,value));
    filters.append(search,filter);
    const list=node('div',null,{class:'term-comparison-list'});
    const pager=node('div',null,{class:'term-comparison-actions'});
    const pageText=node('span');
    let page=1;
    const back=button('上一页',()=>{page--; renderRows();});
    const next=button('下一页',()=>{page++; renderRows();});
    pager.append(back,pageText,next);

    const actions=node('div',null,{class:'term-comparison-actions'});
    const selected=node('span',null,{role:'status'});
    const apply=button('采纳所选术语',()=>commit(state),'primary');
    function matches(item) {
      const query=search.value.trim().toLocaleLowerCase();
      return (filter.value==='all' || filter.value===item.status) && (state.focusTerm
        ? item.canonical===state.focusTerm
        : !query || [item.canonical,...item.aliases,...item.sources.flatMap(source=>[source.name, source.location])]
          .join('\n').toLocaleLowerCase().includes(query));
    }
    function updateCount() {
      selected.textContent=`已选择 ${choices.size} 项`;
      apply.disabled=!choices.size || state.busy || preview.read_only;
    }
    if (!preview.read_only) {
      actions.append(button('选择有文献依据的新增与补充项',()=>{
        preview.items.filter(item=>matches(item) && ['new','supplement'].includes(item.status) && !item.reference_only && hasLiterature(item))
          .forEach(item=>{ if (!choices.has(item.id)) choices.set(item.id,{action:item.target?'merge':'add',...defaultDefinition(item)}); });
        renderRows();
      }),apply,selected);
    }

    function defaultDefinition(item) {
      if (item.target?.definition && item.target.definition!=='原文关键词，定义待校订。') return {};
      const source=item.sources.find(entry=>entry.definition && entry.definition===item.definition);
      return source ? {definition_id:source.id} : {};
    }

    function candidate(item) {
      const row=node('article',null,{class:'term-comparison-row','data-candidate':item.id});
      const name=node('div',null,{class:'term-comparison-title'});
      name.append(node('h3',item.canonical),node('span',states[item.status]));
      row.append(name);
      if (item.aliases.length) row.append(node('p','候选别名 · '+item.aliases.join('、')));
      if (item.reason) row.append(node('p',item.reason));
      if (!item.reference_only && !hasLiterature(item)) row.append(node('p','可疑 · 当前比对缺少可核对的文献表述'));
      if (item.evidence_state==='low') row.append(node('p','低证据线索 · 缺少可核对定义'));
      const sources=disclosure(`原文与出处（${item.sources.length} 条）`,...item.sources.map(evidence));
      row.append(sources);
      for (const old of item.existing) {
        row.append(disclosure(`已有术语 · ${old.term} v${old.version}`,
          node('p',old.definition||'定义待补'),
          ...(preview.project_id ? [button('核对版本与来源',()=>showHistory(preview.project_id,old.id))] : [])));
      }
      if (preview.read_only || item.status==='covered') return row;
      const resolution=choices.get(item.id) || {};
      const method=node('select',null,{'aria-label':`${item.canonical} 处理方式`});
      method.append(new Option('暂不采纳',''));
      if (item.existing.length) method.append(new Option('合并到现有术语','merge'));
      else method.append(new Option('新增术语','add'));
      if (item.status==='conflict' || item.existing.length) method.append(new Option('保留为独立术语','separate'));
      method.value=resolution.action||'';
      const fields=node('div',null,{class:'term-comparison-fields'});
      row.append(label('处理方式',method),fields);
      function renderFields() {
        const choice=choices.get(item.id);
        fields.replaceChildren();
        if (!choice) return;
        const target=node('select',null,{'aria-label':`${item.canonical} 合并目标`});
        item.existing.forEach(old=>target.append(new Option(`${old.term} v${old.version}`,old.id)));
        target.value=choice.target_id||item.target?.id||'';
        const canonical=node('input',null,{maxlength:'120','aria-label':`${item.canonical} 规范名`});
        canonical.value=choice.canonical??(choice.action==='separate' && item.target ? '' : item.canonical);
        const aliases=node('input',null,{maxlength:'4000','aria-label':`${item.canonical} 别名`});
        aliases.value=(choice.aliases??item.aliases).join('，');
        const meaning=node('select',null,{'aria-label':`${item.canonical} 定义来源`});
        meaning.append(new Option(choice.action==='merge' ? '保留已有定义' : '待补定义',''));
        for (const source of item.sources.filter(source=>source.definition)) {
          meaning.append(new Option(`${source.location} · ${source.definition.slice(0,100)}`,source.id));
        }
        meaning.value=choice.definition_id||'';
        if (choice.action==='merge') fields.append(label('合并目标',target));
        fields.append(label('规范名',canonical),label('别名（逗号分隔）',aliases),label('定义来源',meaning));
        target.onchange=()=>{choice.target_id=target.value; canonical.value=item.existing.find(old=>old.id===target.value).term; choice.canonical=canonical.value;};
        canonical.oninput=()=>{choice.canonical=canonical.value;};
        aliases.oninput=()=>{choice.aliases=aliasNames(aliases.value);};
        meaning.onchange=()=>{choice.definition_id=meaning.value||null;};
        if (choice.action==='separate' && item.status==='conflict') {
          const options=node('div',null,{class:'term-comparison-reference-list'});
          for (const source of item.sources) {
            const check=node('input',null,{type:'checkbox',value:source.id});
            check.checked=choice.source_ids?.includes(source.id) || false;
            check.onchange=()=>{
              choice.source_ids=[...options.querySelectorAll('input:checked')].map(input=>input.value);
              if (choice.definition_id && !choice.source_ids.includes(choice.definition_id)) {
                choice.definition_id=null; meaning.value='';
              }
            };
            options.append(label(`${source.location} · ${source.quote.slice(0,160)}`,check));
          }
          fields.append(disclosure('选择独立术语对应的原文',options));
        }
      }
      method.onchange=()=>{
        if (!method.value) choices.delete(item.id);
        else choices.set(item.id,{action:method.value,...(item.status==='conflict' ? {} : defaultDefinition(item))});
        renderFields(); updateCount();
      };
      renderFields();
      return row;
    }
    function renderRows() {
      const items=preview.items.filter(matches);
      const pages=Math.max(1,Math.ceil(items.length/size));
      page=Math.min(page,pages);
      list.replaceChildren(...items.slice((page-1)*size,page*size).map(candidate));
      if (!items.length) list.append(node('p','没有符合条件的术语。'));
      pageText.textContent=`第 ${page} / ${pages} 页 · ${items.length} 项`;
      back.disabled=page===1; next.disabled=page===pages;
      updateCount();
    }
    search.oninput=()=>{state.focusTerm=null;page=1;renderRows();};
    filter.onchange=()=>{page=1;renderRows();};
    compare.body.replaceChildren(header,scope,filters,actions,list,pager);
    renderRows();
  }

  async function refresh(state) {
    if (state.busy) return;
    state.busy=true;
    compare.status.textContent='正在读取稿件和文献术语…'; compare.body.replaceChildren();
    try {
      let result=await post(state.base+'/preview',{reference_ids:state.referenceIds});
      if (state.includeReference && !result.reference_ids.includes(state.includeReference)) {
        state.referenceIds=[...result.reference_ids,state.includeReference];
        result=await post(state.base+'/preview',{reference_ids:state.referenceIds});
      }
      if (comparison!==state) return;
      state.includeReference=null;
      state.preview=result;
      state.choices.clear();
      state.busy=false;
      compare.status.textContent=result.read_only ? '当前论文只读，可核对术语与证据。' : '';
      draw(state);
    } catch (error) {
      if (comparison===state) {
        compare.status.textContent=errorText(error);
        compare.body.append(button('重新识别并比对',()=>refresh(state)));
      }
    } finally {state.busy=false;}
  }

  async function commit(state) {
    if (state.busy || !state.choices.size) return;
    for (const choice of state.choices.values()) {
      if (choice.action==='separate' && !choice.canonical?.trim()) {
        compare.status.textContent='请给独立术语填写可区分的规范名。';
        return;
      }
    }
    state.busy=true;
    compare.body.inert=true;
    const decisions=Object.fromEntries(state.choices);
    const signature=JSON.stringify([state.preview.fingerprint,decisions]);
    if (state.receipt?.signature!==signature) state.receipt={signature,id:crypto.randomUUID()};
    try {
      const result=await post(state.base+'/apply',{fingerprint:state.preview.fingerprint,reference_ids:state.referenceIds,
        decisions,request_id:state.receipt.id});
      if (comparison!==state) return;
      state.choices.clear();
      if (currentProject()===result.project_id) await pp.reload();
      if (state.onApply) await state.onApply(result);
      state.busy=false;
      await refresh(state);
      if (state.preview) compare.status.textContent=`本次采纳 ${result.items.filter(item=>item.status!=='covered').length} 项。`;
    } catch (error) {
      compare.status.textContent=errorText(error);
      if (!compare.body.querySelector('[data-term-retry]')) {
        const retry=button('重新识别并比对',()=>refresh(state));
        retry.dataset.termRetry=''; compare.body.prepend(retry);
      }
    } finally {state.busy=false;compare.body.inert=false;}
  }

  pp.openTermComparison=({projectId=currentProject(),batchId,includeReference,focusTerm,referenceIds=null,onApply}={})=>{
    if (!projectId && !batchId) return;
    const base=batchId ? `/api/manuscript-imports/${encodeURIComponent(batchId)}/terminology` :
      `/api/projects/${encodeURIComponent(projectId)}/terminology/comparison`;
    comparison={base,includeReference,focusTerm,onApply,referenceIds,choices:new Map()};
    if (!compare.modal.open) compare.modal.showModal();
    return refresh(comparison);
  };
  pp.openReferenceTerms=(referenceId,version)=>showReference(referenceId,currentProject(),version);
  pp.termComparisonView = {readableReference, hasLiterature, grades, showHistory, showReference, errorText};
})();
