(() => {
  const pp=window.paperpilot,$=s=>document.querySelector(s);
  if(!pp||!$("#startupPlanner"))return;
  const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels={start:"待开始",draft:"草稿",review:"待审阅",confirmed:"已确认",skipped:"已跳过",cancelled:"已暂停",active:"进行中",complete:"启动规划完成",not_started:"尚未开始"};
  let projectId="",view=null,pendingRemote=null,busy=false,loading=false,dirty=false,projectNames=new Map(),selectedStage="";
  const keptDrafts=new Map();
  const uid=()=>crypto.randomUUID(),base=pid=>`/api/projects/${pid}/workflow/startup`;
  const message=e=>{try{return JSON.parse(e.message).detail?.message||e.message;}catch{return e.message;}};
  function status(text,error=false){$("#startupStatus").textContent=text;$("#startupStatus").dataset.error=error;}
  function current(item=view){return item?.plan.stages.find(s=>s.id===selectedStage)||item?.plan.stages[item.plan.current_index];}
  function selectedActions(){return view?.stage_actions?.[current()?.id]||view?.allowed_actions||[];}
  function provenanceText(item){const stage=current(item),lineage=item.plan.lineage;return [stage.invalidated_reason,stage.skip_reason?"跳过原因："+stage.skip_reason:"",lineage?`复制来源：${projectNames.get(lineage.source_project_id)||lineage.source_project_id} · v${lineage.source_version} · ${new Date(lineage.copied_at).toLocaleString()}。当前论文需重新核验。`:""].filter(Boolean).join(" ");}
  function fields(){return Object.fromEntries([...document.querySelectorAll("#startupFieldInputs [data-startup-field]")].map(el=>[el.dataset.startupField,el.value]));}
  function sourceText(ref){return [ref.label,ref.version,ref.location].filter(Boolean).join(" · ");}
  function sources(){return $("#startupSources").value.split("\n").map(s=>s.trim()).filter(Boolean).map(label=>current().source_refs.find(r=>sourceText(r)===label)||({label,version:"",location:""}));}
  function describe(item){return item.plan.stages.map(s=>`${s.title} · ${labels[s.status]||s.status}\n${Object.entries(s.fields).map(([key,value])=>`${item.definitions.find(d=>d.id===s.id).fields[key]}：${value}`).join("\n")}\n来源：${s.source_refs.map(sourceText).join("；")}`).join("\n\n");}
  function updateButtons(){
    const actions=selectedActions();
    for(const [action,id] of Object.entries({start:"startStartup",save:"saveStartup",review:"reviewStartup",confirm:"confirmStartup",cancel:"cancelStartup",resume:"resumeStartup",back:"backStartup",restart:"restartStartup",skip:"skipStartup"}))$("#"+id).disabled=busy||!( ["save","review","confirm","skip"].includes(action)?actions:view?.allowed_actions||[]).includes(action)||(action==="confirm"&&dirty);
    $("#startupSkipArea").hidden=!actions.includes("skip");
    pp.updateResearchControls?.({busy,actions});
  }
  function render(item,preserveInput=false){
    view=item;pp.selectedStartupStage=current().id;$("#startupFormArea").hidden=false;
    try{sessionStorage.setItem('paperpilot.plan.stage.'+projectId,current().id);}catch{}
    $("#startupPlanner").dataset.projectId=item.project_id;
    $("#startupStages").innerHTML=item.plan.stages.map((s,i)=>`<li data-status="${esc(s.status)}"><button type="button" data-stage-select="${esc(s.id)}" ${s.id===current().id?'aria-current="page"':''}><span>${i+1}</span><strong>${esc(s.title)}</strong><small>${esc(labels[s.status])}${s.optional?" · 可选":""}</small></button></li>`).join("");
    const stage=current(),definition=item.definitions.find(s=>s.id===stage.id);
    $("#backStartup").textContent=["confirmed","skipped"].includes(stage.status)?"基于此版本修订":"回退到所选阶段";
    $("#startupCurrentTitle").textContent=`${stage.title} · ${labels[stage.status]}${item.plan.status==="cancelled"?" · 已暂停":""}`;
    $("#startupNext").textContent=`下一步：${item.next_stage}。当前 ${item.confirmed_context.length} / 5 个阶段已确认。`;
    $("#startupInvalidation").textContent=provenanceText(item);
    if(!preserveInput){
      $("#startupFieldInputs").innerHTML=Object.entries(definition.fields).map(([key,label])=>`<label>${esc(label)}<textarea data-startup-field="${esc(key)}" aria-label="${esc(label)}" maxlength="16000" rows="3">${esc(stage.fields[key]||"")}</textarea></label>`).join("");
      $("#startupSources").value=stage.source_refs.map(sourceText).join("\n");
      $("#startupSkipReason").value=stage.skip_reason||"";dirty=false;
    }
    const editable=selectedActions().includes("save");
    $("#startupFieldInputs").querySelectorAll("textarea").forEach(el=>el.readOnly=!editable);$("#startupSources").readOnly=!editable;
    $("#startupMissing").innerHTML=Object.entries(definition.fields).filter(([key])=>!stage.fields[key]?.trim()).map(([,label])=>`<li>填写“${esc(label)}”后提交审阅</li>`).join("");
    $("#startupBackTarget").innerHTML=item.definitions.slice(0,item.plan.current_index+1).map(s=>`<option value="${esc(s.id)}">${esc(s.title)}</option>`).join("");$("#startupBackTarget").value=stage.id;
    $("#startupConflict").hidden=true;pendingRemote=null;updateButtons();
    pp.renderResearchWorkspace?.(item);
  }
  function backup(){if(!view)return;$("#startupLocalBackup").hidden=false;$("#startupBackupText").textContent=`${current().title} · 本地草稿（基于 v${view.workflow_version}）\n${Object.entries(fields()).map(([key,value])=>`${view.definitions.find(s=>s.id===current().id).fields[key]}：${value}`).join("\n")}\n来源：${$("#startupSources").value}`;}
  function showConflict(remote){pendingRemote=remote;$("#startupConflict").hidden=false;$("#startupConflictInfo").textContent=`本地输入基于 v${view.workflow_version}，服务端为 v${remote.workflow_version}。比较后选择如何继续。`;
    $("#startupServerDraft").textContent=describe(remote);$("#rebaseStartupDraft").disabled=current(remote).id!==current().id||remote.plan.branch_id!==view.plan.branch_id||!remote.allowed_actions.includes("save");status("工作流已更新；本地输入保留。请比较服务端版本后继续。",true);}
  async function refresh(){const pid=projectId;const remote=await pp.api(base(pid));if(pid!==projectId||pp.current?.project.id!==pid)return;if(dirty&&view&&remote.workflow_version!==view.workflow_version){showConflict(remote);return;}render(remote,dirty);}
  async function perform(action){if(busy)return;busy=true;updateButtons();try{await action();}catch(e){status(message(e),true);}finally{busy=false;updateButtons();}}
  async function submit(action,extra={}){
    if(projectId!==pp.current?.project.id)throw new Error("论文正在切换，请等待当前论文规划加载后再操作。输入尚未提交。");
    const pid=projectId,payload={request_id:uid(),expected_workflow_version:view.workflow_version,command:action,stage_id:current().id,...extra};
    if(["save","review"].includes(action))Object.assign(payload,{fields:fields(),source_refs:sources()});
    const next=await pp.api(base(pid)+"/commands",{method:"POST",body:JSON.stringify(payload)});if(pid!==projectId)return false;
    keptDrafts.delete(pid+":"+current().id);
    if(["confirm","skip","start","back","restart"].includes(action))selectedStage=next.current_stage;
    render(next);if(pp.current?.project.id===pid&&next.project_revision)pp.current.project.project_revision=next.project_revision;
    status(next.message);if(pp.refreshWorkflow)await pp.refreshWorkflow();return true;
  }
  async function leaveDraft(){
    if(!dirty)return true;
    const dialog=document.createElement("dialog");dialog.className="stage-draft-dialog";
    dialog.innerHTML='<form method="dialog"><h3>当前阶段有未保存输入</h3><p>选择如何保留输入，再切换位置。</p><button value="save">保存后切换</button><button value="keep">保留草稿后切换</button><button value="cancel">取消</button></form>';
    document.body.append(dialog);dialog.showModal();
    const choice=await new Promise(resolve=>dialog.addEventListener("close",()=>resolve(dialog.returnValue),{once:true}));dialog.remove();
    if(choice==="save"){await submit("save");return true;}
    if(choice!=="keep")return false;
    keptDrafts.set(projectId+":"+current().id,{fields:fields(),sources:$("#startupSources").value,version:view.workflow_version});backup();dirty=false;return true;
  }
  function restoreHeld(){const held=keptDrafts.get(projectId+":"+current().id);if(!held)return;for(const input of $("#startupFieldInputs").querySelectorAll("textarea"))input.value=held.fields[input.dataset.startupField]||"";$("#startupSources").value=held.sources;dirty=true;updateButtons();status(`已恢复本页保留的草稿（基于 v${held.version}），请核对后保存。`);if(held.version!==view.workflow_version)showConflict(view);}
  pp.selectStartupStage=async stageId=>{
    if(busy||!view||!view.definitions.some(s=>s.id===stageId))return false;
    if(stageId===current().id){if(!dirty)restoreHeld();return true;}
    if(!await leaveDraft())return false;
    selectedStage=stageId;render(view);
    restoreHeld();
    pp.selectedStartupStage=stageId;
    if(pp.renderResearchDesign)await pp.renderResearchDesign(view);
    document.dispatchEvent(new CustomEvent("paperpilot:stage-selected",{detail:{projectId,stage:stageId}}));return true;
  };
  $("#startupStages").addEventListener("click",e=>{const button=e.target.closest("[data-stage-select]");if(button)pp.selectStartupStage(button.dataset.stageSelect).catch(e=>status(message(e),true));});
  $("#startupFieldInputs").addEventListener("input",()=>{dirty=true;updateButtons();});$("#startupSources").addEventListener("input",()=>{dirty=true;updateButtons();});
  for(const [action,id] of Object.entries({start:"startStartup",save:"saveStartup",review:"reviewStartup",confirm:"confirmStartup",cancel:"cancelStartup",resume:"resumeStartup"}))$("#"+id).onclick=()=>perform(async()=>{if(dirty&&!["save","review"].includes(action)){await submit("save");}await submit(action);});
  $("#skipStartup").onclick=()=>perform(()=>submit("skip",{reason:$("#startupSkipReason").value}));
  $("#backStartup").onclick=()=>perform(async()=>{const stage=current();if(["confirmed","skipped"].includes(stage.status)&&!window.confirm("将基于当前版本重新打开“"+stage.title+"”及其下游阶段的确认。已有内容保留为待审草稿，旧确认仍可在历史查看。继续修订？"))return;if(dirty)await submit("save");await submit("back",{stage_id:$("#startupBackTarget").value||stage.id});});
  $("#restartStartup").onclick=()=>perform(async()=>{if(dirty)await submit("save");await submit("restart");});
  $("#refreshStartup").onclick=()=>perform(refresh);
  $("#rebaseStartupDraft").onclick=()=>{if(!pendingRemote)return;backup();const remote=pendingRemote;render(remote,true);dirty=true;updateButtons();status("已采用当前版本号并保留本地字段；核对后保存草稿，再提交审阅。");};
  $("#useServerStartup").onclick=()=>{if(!pendingRemote)return;backup();const remote=pendingRemote;render(remote);status("已采用服务端版本；本地未提交输入保留在草稿备份中。");};
  document.addEventListener("paperpilot:workflow-refreshed",event=>{const next=event.detail.startup;if(event.detail.projectId!==projectId||!next||busy||!view||next.workflow_version===view.workflow_version)return;if(dirty)showConflict(next);else refresh().catch(e=>status(message(e),true));});
  const previousLoad=pp.loadProject;
  pp.loadProject=async function(id){if(id!==projectId&&dirty&&view){try{if(!await leaveDraft())return false;}catch(e){status("切换前草稿保存失败，仍保留当前输入。"+message(e),true);return false;}}if(id!==projectId){$("#startupFormArea").hidden=true;status("正在加载所选论文的规划…");}try{return await previousLoad.apply(this,arguments);}finally{if(pp.current?.project.id===projectId)$("#startupFormArea").hidden=!view;}};
  window.addEventListener("beforeunload",event=>{if(dirty){event.preventDefault();event.returnValue="";}});
  // Rendering is driven by the workflow projection; this module never polls the
  // same snapshot on its own interval.
  pp.renderStartup=(startup,workflow)=>{
    const pid=pp.current?.project?.id;
    if(!pid||!startup||busy)return;
    if(pid!==projectId){projectId=pid;selectedStage=startup.current_stage;try{const held=sessionStorage.getItem('paperpilot.plan.stage.'+pid);if(startup.definitions.some(stage=>stage.id===held))selectedStage=held;}catch{}pp.selectedStartupStage=selectedStage;view=null;dirty=false;$("#startupConflict").hidden=true;$("#startupLocalBackup").hidden=true;}
    if(dirty&&view&&startup.workflow_version!==view.workflow_version){showConflict(startup);return;}
    render(startup,dirty);
    if(!dirty)restoreHeld();
    if(pp.renderResearchDesign)Promise.resolve(pp.renderResearchDesign(startup)).catch(()=>{});
    if(!view.plan.branch_id&&view.plan.status==="not_started")status("尚未开始启动规划；点击“开始规划”进入第一阶段。");
    else status("从当前阶段继续；草稿需审阅确认后才会用于 AI 上下文。");
  };
})();
