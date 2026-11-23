/* Browsing never changes the versioned confirmation position. */
(() => {
  const pp=window.paperpilot, $=s=>document.querySelector(s);
  if(!pp)return;
  const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels={start:"未开始",draft:"草稿",review:"待审阅",confirmed:"已确认",skipped:"已跳过"};
  let current=null,collapsed=false;
  function render(){
    const startup=current?.startup,plan=startup?.plan,summary=startup?.summary;
    const progress=$("#wfProgress"),guide=$("#wfGuide");
    if(!startup||!pp.current?.project){if(progress)progress.hidden=true;if(guide)guide.hidden=true;return;}
    const selected=pp.selectedStartupStage||startup.current_stage;
    const workspace=document.querySelector(".workspace")?.dataset.view;
    const inPlanningStage=workspace==="planning"&&window.location.hash.startsWith("#planning/startup");
    const location=inPlanningStage?(startup.definitions.find(s=>s.id===selected)?.title||"启动规划"):($("#workspaceViewTitle")?.textContent||"论文工作区");
    if(progress){progress.hidden=false;progress.innerHTML=`<strong>${esc(location)}</strong><span>规划确认 ${summary?.confirmed_count??startup.confirmed_context.length}/${startup.definitions.length}</span><span>跳过 ${summary?.skipped_count??0}</span><span class="wf-next">下一步：${esc(summary?.next_action||startup.next_stage)}</span>`;}
    if(!guide)return;
    guide.hidden=false;
    const items=startup.definitions.map(def=>{const stage=plan.stages.find(s=>s.id===def.id);return `<li data-status="${esc(stage.status)}"><button type="button" data-stage="${esc(def.id)}" aria-current="${workspace==="planning"&&selected===def.id?'page':'false'}"><span>${esc(def.title)}</span><small>${esc(labels[stage.status]||stage.status)}${stage.skip_reason?' · '+esc(stage.skip_reason):''}</small></button></li>`;}).join("");
    guide.innerHTML=`<details ${collapsed?'':'open'}><summary>规划阶段 <small>${summary?.confirmed_count??0}/${startup.definitions.length} 已确认</small></summary><ol>${items}</ol></details><button type="button" data-view="write">正文写作</button><button type="button" data-view="planning/progress">写作阶段与发布</button><p class="wf-guide-note">可自由浏览；草稿需单独审阅确认。</p>`;
    guide.querySelector("details").addEventListener("toggle",e=>{collapsed=!e.target.open;});
    guide.querySelectorAll("[data-stage]").forEach(button=>button.onclick=async()=>{
      try{if(pp.selectStartupStage&&!await pp.selectStartupStage(button.dataset.stage))return;pp.openWorkspace?.("planning/startup");render();document.querySelector("#startupFieldInputs textarea:not([readonly])")?.focus();}
      catch(error){const node=$("#startupStatus");if(node){node.textContent=error.message;node.dataset.error="true";}}
    });
    guide.querySelectorAll("[data-view]").forEach(button=>button.onclick=()=>pp.openWorkspace?.(button.dataset.view));
  }
  pp.renderWorkflowNav=workflow=>{current=workflow;render();};
  pp.openFirstPlanningStage=()=>{if(current?.startup?.project_id!==pp.current?.project.id||current?.startup?.plan.status!=="active"||current.startup.plan.current_index!==0)return false;pp.openWorkspace?.("planning/startup");return true;};
  for(const name of ["paperpilot:view-changed","paperpilot:stage-selected"])document.addEventListener(name,render);
  document.addEventListener("paperpilot:project-changed",()=>{current=null;render();});
})();
