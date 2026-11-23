/* Stable tools, artifact locations and author decisions in the paper workspace. */
(() => {
  const pp = window.paperpilot;
  pp.agentCards.register('innovation_draft', (card, context) => {
    const container = document.createElement('article');
    if (card.project_id !== context.project_id) return container;
    const title = document.createElement('p');
    title.textContent = card.summary || card.title;
    const action = document.createElement('a');
    action.textContent = '审阅创新点候选';
    action.href = '/innovations?' + new URLSearchParams({project: card.project_id, point: card.id});
    container.append(title, action);
    return container;
  });
  if (!pp || pp.standaloneOutline || pp.standaloneTerms || !document.querySelector('#agentChat')) return;
  const $ = selector => document.querySelector(selector);
  const node = (tag, text, className) => {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  };
  const button = (label, action) => {
    const element = node('button', label); element.type='button'; element.onclick=action; return element;
  };
  const pid = () => pp.current?.project?.id;
  const cid = () => $('#agentConversation').value;
  const base = () => `/api/projects/${encodeURIComponent(pid())}/agent`;
  const post = async(path,body)=>{
    const response=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    const result=await response.json();
    if(!response.ok){const error=new Error(result.detail?.message||'操作未完成，请核对后重试。');error.status=response.status;throw error;}
    return result;
  };
  let entries = [], directoryEpoch=0;
  const directory = node('dialog',undefined,'paper-tool-directory'); directory.id='paperToolDirectory';
  directory.setAttribute('aria-labelledby','paperToolDirectoryTitle');
  const heading=node('div',undefined,'paper-directory-heading'),title=node('h2','资料与工具');title.id='paperToolDirectoryTitle';
  heading.append(title,button('关闭',()=>directory.close()));
  const search=node('input');search.type='search';search.id='paperToolSearch';search.placeholder='搜索功能、旧名称或用途';search.setAttribute('aria-label','搜索资料与工具');
  const results=node('div',undefined,'paper-tool-results');results.id='paperToolResults';
  const directoryStatus=node('p',undefined,'paper-directory-status');directoryStatus.setAttribute('role','status');
  directory.append(heading,search,directoryStatus,results);document.body.append(directory);
  function renderDirectory() {
    results.replaceChildren();
    const query=search.value.trim().toLocaleLowerCase();
    const filtered=entries.filter(item=>[item.title,item.group,item.description,item.aliases,...item.requirement_ids].join(' ').toLocaleLowerCase().includes(query));
    const groups=new Map();
    for(const item of filtered){
      if(!groups.has(item.group)){
        const section=node('section'),list=node('div',undefined,'paper-tool-group');section.append(node('h3',item.group),list);results.append(section);groups.set(item.group,list);
      }
      const row=node('div',undefined,'paper-tool-row');row.dataset.toolId=item.id;
      const description=node('div');description.append(node('strong',item.title),node('p',item.description));
      const actions=node('div',undefined,'paper-tool-actions');
      if(item.id!=='results'||pp.resultModuleView)actions.append(node('small',item.execution));
      actions.append(button('打开',async()=>{directory.close();await pp.openPaperTool(item);}));
      if(item.execution==='Agent 可执行')actions.append(button('交给 Agent',async()=>{
        directory.close();pp.openWorkspace('chat');await pp.openModuleAgent('paper',`请帮我处理${item.title}。`);
      }));
      row.append(description,actions);groups.get(item.group).append(row);
    }
    directoryStatus.textContent=filtered.length ? '' : '没有找到匹配工具，可以试试旧功能名称。';
  }
  async function loadDirectory(){
    const project=pid(),epoch=++directoryEpoch;
    if(!project){entries=[];results.replaceChildren();directoryStatus.textContent='先选择或新建一篇论文。';return;}
    try{
      const result=await pp.api(base()+'/tools');
      if(project!==pid()||epoch!==directoryEpoch)return;
      entries=result.items.map(item=>item.id==='results'&&!pp.resultModuleView
        ? {...item,title:'结果与图表',description:'按算例查找实验数据、图表和原件。',route:'visual/result-library',execution:''}
        : item);renderDirectory();
    }catch(error){if(project===pid()&&epoch===directoryEpoch)directoryStatus.textContent='工具目录加载失败，请关闭后重试。';}
  }
  function openDirectory(){search.value='';if(!directory.open)directory.showModal();loadDirectory();search.focus();}
  search.oninput=renderDirectory;
  const menu=$('#conversationArtifacts');menu.hidden=true;
  const openTools=button('资料与工具',openDirectory);openTools.id='openPaperTools';menu.after(openTools);
  $('.cf-sidebar-footer').prepend(button('资料与工具',openDirectory));
  $('.cf-sidebar-footer > details')?.remove();
  pp.openPaperTool = async entry => {
    if(typeof entry==='string')entry=entries.find(item=>item.id===entry);
    if(!entry)return;
    if(entry.route==='manuscript-import'){pp.openManuscriptImport?.();return;}
    if(entry.route==='agent-settings'){pp.openAgentSettings();return;}
    if(entry.route==='paper-library'){
      $('.ws-paper-switcher').open=true;
      if(matchMedia('(max-width:760px)').matches){$('.shell').classList.add('ws-library-open');$('.ws-mobile-toggle').setAttribute('aria-expanded','true');}
      $('#projectSearch').focus();return;
    }
    const project=pid();
    if(entry.route==='research')await pp.openPaperArtifact('research');else await pp.openWorkspace(entry.route);
    if(project!==pid())return;
    if(entry.node_id)pp.focusResearchNode?.(entry.node_id);
    const toolId=entry.entry_id||entry.id;
    const legacyAction={companion:'ai',follow:'ai',continue:'continue',modify:'revise',recovery:'recover'}[toolId];
    writingTools.hidden=!legacyAction;
    if(legacyAction){
      const item=writingPages.find(page=>page.key===legacyAction);
      item?.button.click();
      if(['companion','follow'].includes(toolId))$('#workflowAssistAction').value=toolId;
      writingTools.scrollIntoView({block:'start'});
    }
    if(entry.route==='write'&&entry.target?.file_path){
      const target=entry.target,select=$('#fileSelect');
      if([...select.options].some(option=>option.value===target.file_path)){
        if(select.value!==target.file_path){
          if(await pp.flushEditorChanges?.()===false||project!==pid())return;
          select.value=target.file_path;select.dispatchEvent(new Event('change',{bubbles:true}));
        }
        const editor=$('#editor');
        if(target.file_hash&&await window.paperpilotAgentStore.hash(editor.value)===target.file_hash&&project===pid()){
          editor.setSelectionRange(target.selection_start||0,target.selection_end||target.selection_start||0);editor.focus();
        }
      }
    }
    const target=entry.selector&&$(entry.selector);
    if(target){
      for(let parent=target.parentElement;parent;parent=parent.parentElement)if(parent.tagName==='DETAILS')parent.open=true;
      target.scrollIntoView({block:'center'});
      if(!target.matches('button,input,textarea,select'))target.tabIndex=-1;
      target.focus({preventScroll:true});
    }
  };
  const revision=node('section',undefined,'paper-revision-controls');revision.id='workspaceRevisionControls';
  revision.setAttribute('aria-label','人工修订模式');revision.append(node('h3','人工修订模式'),$('#workflowModeState'),$('#workflowModeControls'));
  $('#write').prepend(revision);
  const writingTools=node('section',undefined,'paper-writing-tools');writingTools.id='workspaceWritingTools';writingTools.hidden=true;
  const writingNav=node('nav');writingNav.setAttribute('aria-label','写作工具');writingTools.append(writingNav);
  const writingPages=[];
  const originalPages=[...$('#assistLegacy').querySelectorAll('.ws-assist-page')];
  for(const [index,[key,label]] of [['ai','伴写与跟写'],['continue','续写'],['revise','指定修改'],['recover','恢复']].entries()){
    const page=originalPages[index];if(!page)continue;
    const select=button(label,()=>{
      writingPages.forEach(item=>{item.page.hidden=item.key!==key;item.button.setAttribute('aria-pressed',String(item.key===key));});
    });
    select.setAttribute('aria-pressed',String(index===0));writingNav.append(select);writingTools.append(page);
    writingPages.push({key,page,button:select});
  }
  $('#write').append(writingTools);$('#assistLegacy').remove();

  const settingsDialog=node('dialog',undefined,'paper-agent-settings');settingsDialog.id='paperAgentSettings';
  settingsDialog.setAttribute('aria-labelledby','paperAgentSettingsTitle');
  const settingsHeading=node('div',undefined,'paper-directory-heading'),settingsTitle=node('h2','对话设置');settingsTitle.id='paperAgentSettingsTitle';
  settingsHeading.append(settingsTitle,button('返回对话',()=>settingsDialog.close()));
  settingsDialog.append(settingsHeading,$('#agentSettings'));document.body.append(settingsDialog);
  const confirmSend=button('确认并发送',()=>{settingsDialog.close();$('#agentSend').click();});
  confirmSend.className='primary';settingsDialog.append(confirmSend);
  document.addEventListener('paperpilot:agent-settings',event=>{
    if(event.detail.open){if(!settingsDialog.open)settingsDialog.showModal();}else if(settingsDialog.open)settingsDialog.close();
  });
  settingsDialog.addEventListener('close',()=>{$('#agentSettings').hidden=true;$('#agentSettingsToggle').setAttribute('aria-expanded','false');});

  const tasks=node('details',undefined,'paper-task-panel');tasks.id='paperTaskPanel';tasks.hidden=true;
  const taskSummary=node('summary','本轮任务'),taskList=node('div');tasks.append(taskSummary,taskList);$('#agentRunStatus').after(tasks);
  let taskEpoch=0;
  document.addEventListener('paperpilot:agent-run',async event=>{
    const {projectId,conversationId,run}=event.detail,epoch=++taskEpoch;
    try{
      const result=await pp.api(`/api/projects/${projectId}/agent/subtasks?run_id=${encodeURIComponent(run.id)}`);
      if(epoch!==taskEpoch||projectId!==pid()||conversationId!==cid())return;
      tasks.hidden=!result.tasks.length;taskList.replaceChildren();
      taskSummary.textContent=`专项任务 ${result.tasks.filter(t=>t.status==='succeeded').length}/${result.tasks.length}`;
      for(const task of [...result.tasks].reverse()){
        const row=node('section');row.append(node('strong',task.instruction),node('span',({running:'执行中',succeeded:'已完成',failed:'未完成',blocked:'受阻',cancelled:'已停止'})[task.status]||task.status));
        if(task.result.text)row.append(node('p',task.result.text));
        row.append(button('进入专题追问',async()=>{pp.openWorkspace('chat');await pp.openModuleAgent(task.module_key);}));taskList.append(row);
      }
    }catch{/* A polling failure preserves the visible task summary. */}
  });
  const decisions=node('section',undefined,'paper-decision-targets');decisions.hidden=true;$('#agentRunStatus').after(decisions);
  let decisionPending=null;
  pp.resolveAuthorDecision=async(instruction,actionId='')=>{
    const project=pid(),conversation=cid();
    const key=project+':'+conversation+':'+instruction+':'+actionId;
    const storage=window.paperpilotAgentStore;
    const saved=storage.read(project,conversation,'authorDecisionPending');
    if(saved){
      const pending=JSON.parse(saved);
      if(pending.key!==key)throw new Error('上一条采纳请求的结果待核对，请先用原确认消息重试。');
      decisionPending=pending;
    }
    if(!decisionPending||decisionPending.key!==key)decisionPending={key,request_id:crypto.randomUUID()};
    storage.write(project,conversation,'authorDecisionPending',JSON.stringify(decisionPending));
    let response;
    try{
      response=await post(base()+'/author-decisions',{request_id:decisionPending.request_id,conversation_id:conversation,instruction,action_id:actionId});
    }catch(error){
      if(error.status&&error.status<500){storage.write(project,conversation,'authorDecisionPending','');decisionPending=null;}
      throw error;
    }
    if(project!==pid()||conversation!==cid())return true;
    storage.write(project,conversation,'authorDecisionPending','');
    if(!response.handled){decisionPending=null;return false;}
    decisions.replaceChildren();decisions.hidden=false;
    decisions.append(node('p',response.message||({applied:'已采纳，继续讨论下一步。',rejected:'已记录不采纳。'})[response.status]||'请核对方案。'));
    for(const card of response.choices||[])decisions.append(button(card.title+` · ${card.id.slice(0,6)}`,()=>pp.resolveAuthorDecision(instruction,card.id).catch(error=>{decisions.textContent=error.message;})));
    if(response.card){
      const old=[...document.querySelectorAll('[data-business-card]')].filter(element=>element.dataset.businessCard===response.card.id);
      if(old.length)old.forEach(element=>element.replaceWith(pp.agentBusiness.render(response.card)));else pp.agentBusiness.mount(response.card);
    }
    if(response.event){
      document.dispatchEvent(new CustomEvent('paperpilot:resource-updated',{detail:response.event}));
      await pp.refreshWorkflow?.();
      document.dispatchEvent(new CustomEvent('paperpilot:author-decision-applied'));
    }
    decisionPending=null;return true;
  };
  const renderArtifact=card=>{
    const section=node('section',undefined,'paper-artifact-link');
    section.append(button('打开'+card.title,()=>pp.openPaperTool(card)));
    const storage=window.paperpilotAgentStore,project=pid(),conversation=cid();
    if(card.open_requested&&!storage.read(project,conversation,'artifact-opened-'+card.id)){
      queueMicrotask(()=>{
        if(section.isConnected&&project===pid()&&conversation===cid()){
          storage.write(project,conversation,'artifact-opened-'+card.id,'yes');pp.openPaperTool(card);
        }
      });
    }
    return section;
  };
  pp.agentCards.register('artifact',renderArtifact);
  const attachmentDialog=node('dialog',undefined,'paper-attachment-dialog');attachmentDialog.setAttribute('aria-labelledby','paperAttachmentTitle');
  const attachmentHeading=node('div',undefined,'paper-directory-heading'),attachmentTitle=node('h2','添加资料');attachmentTitle.id='paperAttachmentTitle';
  attachmentHeading.append(attachmentTitle,button('关闭',()=>attachmentDialog.close()));
  const attachmentInput=node('input');attachmentInput.type='file';attachmentInput.accept='.pdf,.txt,.md,.tex,.bib,.csv,.tsv';attachmentInput.setAttribute('aria-label','选择资料文件');
  const pageLabel=node('label','PDF页码（留空读取全部，最多40页）'),pageInput=node('input');pageInput.placeholder='例如 1,2,3';pageInput.setAttribute('aria-label','PDF页码');pageLabel.append(pageInput);
  const attachmentStatus=node('p');attachmentStatus.setAttribute('role','status');
  let uploadPending=null;
  const upload=button('加入当前对话',async()=>{
    const file=attachmentInput.files[0];if(!file){attachmentStatus.textContent='请选择一份文件。';return;}
    const project=pid(),conversation=cid();upload.disabled=true;
    try{
      if(file.size>20*1024*1024)throw new Error('单个文件最多20MB。');
      const pages=pageInput.value.trim()?pageInput.value.split(/[,，\s]+/).map(Number):[];
      if(pages.some(p=>!Number.isInteger(p)||p<1))throw new Error('请用逗号分隔有效页码。');
      const content=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file);});
      if(project!==pid()||conversation!==cid())return;
      const signature=project+':'+conversation+':'+file.name+':'+file.size+':'+file.lastModified+':'+pages.join(',');
      if(!uploadPending||uploadPending.signature!==signature)uploadPending={signature,request_id:crypto.randomUUID()};
      await post(base()+`/conversations/${conversation}/attachments`,{request_id:uploadPending.request_id,filename:file.name,content_base64:content,pages});
      uploadPending=null;
      if(project!==pid()||conversation!==cid())return;
      attachmentDialog.close();await loadAttachments();document.dispatchEvent(new CustomEvent('paperpilot:agent-context-changed'));
      $('#agentInput').focus();
    }catch(error){attachmentStatus.textContent=error.message;}finally{upload.disabled=false;}
  });
  attachmentInput.onchange=()=>{pageLabel.hidden=!attachmentInput.files[0]?.name.toLowerCase().endsWith('.pdf');pageInput.value='';};
  attachmentDialog.append(attachmentHeading,attachmentInput,pageLabel,node('p','CSV 和 TSV 在本地整理。PDF 和文本资料会列入模型发送预览。'),upload,
    button('导入图像或扫描件',async()=>{attachmentDialog.close();await pp.openPaperTool(entries.find(item=>item.id==='images'));$('#openVisualImport')?.click();}),attachmentStatus);
  document.body.append(attachmentDialog);
  $('#conversationAttach').onclick=()=>{attachmentStatus.textContent='';attachmentInput.value='';pageInput.value='';pageLabel.hidden=true;attachmentDialog.showModal();};
  const attachmentList=node('div',undefined,'paper-attachment-list');attachmentList.id='paperAttachmentList';$('.agent-compose').prepend(attachmentList);
  pp.openConversationAttachment=async identity=>{
    const project=pid(),conversation=cid();
    try{
      const file=await pp.api(base()+`/conversations/${conversation}/attachments/${encodeURIComponent(identity)}`);
      if(project!==pid()||conversation!==cid())return;
      const dialog=node('dialog',undefined,'paper-attachment-preview'),heading=node('div',undefined,'paper-directory-heading'),title=node('h2',file.filename);
      title.id='paperAttachmentPreviewTitle';dialog.setAttribute('aria-labelledby',title.id);
      heading.append(title,button('关闭',()=>dialog.close()));dialog.append(heading);
      if(file.columns&&pp.resultPreviewTable)dialog.append(node('p',`${file.row_count} 行 · ${file.columns.length} 列`),pp.resultPreviewTable(file));
      else dialog.append(node('pre',file.content));
      dialog.addEventListener('close',()=>dialog.remove(),{once:true});document.body.append(dialog);dialog.showModal();
    }catch(error){$('#agentRunStatus').textContent='附件读取失败，请核对当前对话和资料状态。';}
  };
  let attachmentEpoch=0;
  async function loadAttachments(){
    const project=pid(),conversation=cid(),epoch=++attachmentEpoch;attachmentList.replaceChildren();if(!project||!conversation)return;
    try{
      const result=await pp.api(base()+`/conversations/${conversation}/attachments`);
      if(epoch!==attachmentEpoch||project!==pid()||conversation!==cid())return;
      for(const file of result.files){
        const chip=node('span',undefined,'paper-attachment-chip');chip.append(button(file.filename,()=>pp.openConversationAttachment(file.id)));
        if(file.result_data)chip.append(node('small','本地表格'));
        chip.append(button('移除',async()=>{
          try{
            await pp.api(`/api/projects/${project}/agent/conversations/${conversation}/attachments/${file.id}`,{method:'DELETE'});
            if(project===pid()&&conversation===cid()){await loadAttachments();document.dispatchEvent(new CustomEvent('paperpilot:agent-context-changed'));}
          }catch(error){$('#agentRunStatus').textContent=error.message;}
        }));attachmentList.append(chip);
      }
    }catch(error){if(epoch===attachmentEpoch)$('#agentRunStatus').textContent='附件列表加载失败，请切换对话后重试。';}
  }
  document.addEventListener('paperpilot:project-changed',()=>{++taskEpoch;++directoryEpoch;tasks.hidden=true;decisions.hidden=true;entries=[];results.replaceChildren();decisionPending=null;if(directory.open)directory.close();if(settingsDialog.open)settingsDialog.close();});
  document.addEventListener('paperpilot:agent-selected',()=>{++taskEpoch;tasks.hidden=true;decisions.hidden=true;decisionPending=null;loadAttachments();if(attachmentDialog.open)attachmentDialog.close();$('.paper-attachment-preview')?.close();});
  document.addEventListener('paperpilot:project-changed',()=>{++attachmentEpoch;attachmentList.replaceChildren();if(attachmentDialog.open)attachmentDialog.close();$('.paper-attachment-preview')?.close();});
  document.addEventListener('paperpilot:project-loaded',loadDirectory);
  document.addEventListener('keydown',event=>{if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='k'){event.preventDefault();openDirectory();}});
  loadDirectory();
})();
