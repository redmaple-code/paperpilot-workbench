/* Durable review of explicit draft/saved/candidate snapshots. No manuscript writes. */
(() => {
  const pp=window.paperpilot;if(!pp)return;
  const $=s=>document.querySelector(s),uid=()=>crypto.randomUUID();
  const pid=()=>pp.data?.project?.id||'',same=p=>p===pid();
  const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const api=(p,path,body,method='POST')=>pp.api(`/api/projects/${p}/citation-review${path}`,body===undefined?undefined:{method,body:JSON.stringify(body)});
  let task=null,preflight=null,token=0,dispatching=false,inputRevision=0;
  const labels={queued:'等待继续核验',running:'正在核验',partial:'部分完成',completed:'已检查，待人工审阅',cancelled:'已取消',deleted:'已永久清理',pending:'未检查',failed:'失败待恢复',interrupted:'中断'};
  function note(text){$('#citationTaskStatus').textContent=text;}
  function error(e){try{return JSON.parse(e.message).detail?.message||e.message;}catch{return e.message;}}
  const host=$('#referenceLibraryCard');if(!host)return;
  host.insertAdjacentHTML('beforeend',`<section id="citationTaskWorkbench" class="resource-workbench" aria-labelledby="citationTaskTitle">
    <h4 id="citationTaskTitle">文献审查任务</h4><p class="review-note">逐条保存结果。默认检查已保留的当前草稿；候选单独预检。人工理由不会使证据自动通过。</p>
    <label>输入<select id="citationTaskInput"><option value="draft">当前草稿（默认）</option><option value="saved">已保存正文</option><option value="candidate">指定候选，只读</option></select></label>
    <label>检查文件（逗号分隔，不自动扩大范围）<input id="citationTaskPaths" value="main.tex"></label>
    <label>草稿来源<select id="citationTaskDraft"><option value="all">全部草稿，冲突时停止</option><option value="editor">编辑器草稿</option><option value="revision">人工修订草稿</option></select></label>
    <label>候选编号<input id="citationTaskCandidate" placeholder="仅候选预检需要"></label>
    <label>论文语言<input id="citationTaskLanguage" value="auto" placeholder="如中文、English"></label>
    <label>发送的来源范围<select id="citationTaskScope"><option value="all">当前论文全部来源（须预览确认）</option><option value="selected">仅下方勾选来源</option></select></label><div id="citationTaskSourceScope"></div>
    <div class="workbench-actions"><button id="previewCitationTask" type="button">预览逐项检查</button><button id="refreshCitationTasks" type="button">刷新任务与来源核验</button></div>
    <div id="citationTaskPreflight"></div><p id="citationTaskStatus" role="status" aria-live="polite"></p>
    <div id="citationTaskHistory"></div><div id="citationTaskResult"></div>
    <details><summary>来源可靠性、谱系与研究关系</summary><p>只有人工核实后才标为可靠；研究关系未知时留空。图表类证据仍要求原图区域人工核查。</p><div id="citationTaskSources"></div></details>
  </section>`);
  function input(){return {request_id:uid(),input_kind:$('#citationTaskInput').value,
    target_paths:$('#citationTaskPaths').value.split(',').map(x=>x.trim()).filter(Boolean),draft_source:$('#citationTaskDraft').value,
    candidate_id:$('#citationTaskInput').value==='candidate'?$('#citationTaskCandidate').value.trim()||null:null,paper_language:$('#citationTaskLanguage').value||'auto',
    source_ids:$('#citationTaskScope').value==='selected'?[...$('#citationTaskSourceScope').querySelectorAll('input:checked')].map(e=>e.value):null};}
  async function preview(opts=input(),resuming=null){
    const p=pid(),t=++token;
    if(opts.input_kind==='draft')await pp.flushVisibleDraftForReading?.(p);
    const result=await api(p,'/preview',opts);if(!same(p)||t!==token)return;
    preflight={pid:p,options:opts,result,resuming,inputRevision};
    $('#citationTaskPreflight').innerHTML=`<p>${esc(opts.input_kind)} · ${result.claim_count}条论断 · ${result.source_scope.length}个来源 · 模型 ${esc(result.provider.model)}；逐项输出上限${result.max_output_tokens}token。</p>
      <details><summary>完整逐项外发内容与工程输入覆盖</summary><pre>${esc(JSON.stringify({files:result.input_files,engineering:result.engineering,items:result.outbound_items},null,2))}</pre></details>
      <label><input id="citationTaskConsent" type="checkbox">我已检查本次输入，授权发送上述逐项请求</label>
      ${resuming?'<label><input id="citationTaskDuplicate" type="checkbox">已核对中断项，明确接受可能重复费用</label>':''}
      ${result.provider.auth_mode==='temporary'?'<label>本批次临时密钥<input id="citationTaskKey" type="password" autocomplete="off"></label>':''}
      <button id="startCitationTask" type="button">${resuming?'确认恢复未完成项':'确认并开始检查'}</button>`;
    $('#startCitationTask').onclick=()=>safely(async()=>{
      const f=preflight;if(!f||!same(f.pid)||!$('#citationTaskConsent').checked)throw new Error('请先核对外发内容并勾选授权。');
      const key=$('#citationTaskKey')?.value||pp.takeTemporaryCredential?.(f.result.provider.profile_id)||'';if($('#citationTaskKey'))$('#citationTaskKey').value='';
      $('#startCitationTask').disabled=true;
      const command={request_id:uid(),preview_fingerprint:f.result.preview_fingerprint,consent_to_send:true,duplicate_cost_confirmed:!!$('#citationTaskDuplicate')?.checked};
      const current=f.resuming?await api(p,`/tasks/${f.resuming}/resume`,command):await api(p,'/tasks',{...f.options,request_id:command.request_id,preview_fingerprint:command.preview_fingerprint,consent_to_send:true});
      if(!same(p))return;task=current;render();$('#citationTaskPreflight').replaceChildren();
      await dispatch(p,current,command,key);
    });
    note(resuming?'恢复只重试未完成项；若输入已变化，请新建任务。':'预览已生成，请检查实际外发内容。');
  }
  async function dispatch(p,current,command,key){
    if(dispatching)return;dispatching=true;const revision=inputRevision;
    try{
      while(same(p)&&task?.id===current.id&&['queued','running'].includes(task.status)&&!task.stale){
        const next=await api(p,`/tasks/${current.id}/step`,{...command,request_id:uid(),temporary_key:key});
        if(!same(p)||task?.id!==current.id)return;
        task=next;if(inputRevision!==revision)task.stale=true;render();if(!['queued','running'].includes(task.status))break;
      }
    }finally{key='';dispatching=false;if(same(p))await history(p);}
  }
  function render(){
    if(!task)return;const p=pid(),current=task,root=$('#citationTaskResult');
    root.innerHTML=`<h5>${esc(labels[task.status]||task.status)} · ${esc(task.id)}</h5><p>${task.coverage?`已检查${task.coverage.checked}/${task.coverage.total}，未覆盖${task.coverage.uncovered.length}`:''} ${task.stale?'输入或权限已变更；旧报告只读，不得继续交接':''}；清理状态 ${esc(task.cleanup_state)}</p>
      <div class="workbench-actions"><button id="cancelCitationTask" type="button">取消后续派发</button><button id="resumeCitationTask" type="button">重新预览并恢复</button><button id="deleteCitationTask" type="button">预览永久清理此报告</button></div><div id="citationDeletePreview"></div><div id="citationTaskItems"></div>`;
    $('#cancelCitationTask').disabled=!['queued','running'].includes(task.status);
    $('#resumeCitationTask').disabled=task.stale||task.status==='deleted'||task.status==='completed';
    $('#cancelCitationTask').onclick=()=>safely(async()=>{const r=await api(p,`/tasks/${current.id}/cancel`,{request_id:uid()});if(same(p)&&task?.id===current.id){task=r;render();note('已取消后续派发；已发请求可能仍收费，迟到结果不会晋级。');}});
    $('#resumeCitationTask').onclick=()=>safely(()=>preview({...current.options,request_id:uid()},current.id));
    $('#deleteCitationTask').onclick=()=>safely(async()=>{
      const r=await api(p,`/tasks/${current.id}/delete-preview`,{request_id:uid()});if(!same(p)||task?.id!==current.id)return;
      $('#citationDeletePreview').innerHTML=`<p>${esc(r.scope)}</p><label><input type="checkbox" id="citationDeleteConsent">确认永久清除</label><button type="button" id="commitCitationDelete">执行清理</button>`;
      $('#commitCitationDelete').onclick=()=>safely(async()=>{if(!$('#citationDeleteConsent').checked)throw new Error('请确认清理范围。');const r=await api(p,`/tasks/${current.id}/delete`,{request_id:uid(),confirm:true});if(same(p)){task=r;render();await history(p);}});
    });
    for(const item of task.items||[]){
      const claim=item.report.claims?.[0];if(!claim)continue;
      const row=document.createElement('article');row.className='review-row';
      row.innerHTML=`<h5>${esc(claim.target_path)} · ${esc(labels[item.status]||item.status)}</h5><blockquote>${esc(claim.text)}</blockquote><p>类型：${esc(claim.claim_type)}；${esc(item.error_code)}</p>
        <details><summary>原文引句、逐条件结果、概念差异与辅助译释</summary><pre>${esc(JSON.stringify({supports:claim.supports,conditions:claim.condition_results,language_review:claim.language_review,semantic_review:claim.semantic_review,study_relations:item.report.study_relations},null,2))}</pre></details>
        <p>${esc(item.dispositions.map(x=>`${x.action}：${x.reason||'理由已清理'}`).join('；'))}</p>
        <label>人工理由<textarea data-citation-reason></textarea></label><label>处置<select data-citation-disposition><option value="needs_evidence">待补证据</option><option value="retain">保留作者原文</option><option value="dismiss">不采纳建议</option><option value="correct_type">修正论断类型</option><option value="handoff">送入人工修订草稿（不提交正文）</option></select></label>
        <label>修正类型<select data-citation-type><option value="descriptive">描述</option><option value="original_result">作者原创结果（解释另审）</option><option value="quantitative">量化</option><option value="causal">因果</option><option value="comparative">比较</option><option value="definition">定义</option></select></label>
        <button type="button" data-citation-save ${task.stale?'disabled':''}>记录处置</button>`;
      row.querySelector('[data-citation-save]').onclick=()=>safely(async()=>{
        const action=row.querySelector('[data-citation-disposition]').value,reason=row.querySelector('[data-citation-reason]').value.trim();if(!reason)throw new Error('请填写人工判断理由。');
        if(current.options.input_kind==='draft')await pp.flushVisibleDraftForReading?.(p);
        const r=await api(p,`/tasks/${current.id}/claims/${item.claim_id}/disposition`,{request_id:uid(),action,reason,claim_type:action==='correct_type'?row.querySelector('[data-citation-type]').value:null});
        if(!same(p))return;
        if(r.new_preview_required){await preview({...current.options,request_id:uid(),claim_types:{...current.options.claim_types,[item.claim_id]:row.querySelector('[data-citation-type]').value}});note('类型修正已记录；请检查新预览，原报告证据状态不变。');}
        else {task=await api(p,`/tasks/${current.id}`);if(same(p)){render();note('已记录处置；人工理由不会自动判定证据通过。');}}
      });
      $('#citationTaskItems').append(row);
    }
  }
  async function history(p=pid()){
    const rows=await api(p,'/tasks');if(!same(p))return;
    $('#citationTaskHistory').innerHTML=rows.map(r=>`<button type="button" data-open-citation-task="${esc(r.id)}">${esc(labels[r.status]||r.status)} ${esc(r.created_at)}</button>`).join('');
    $('#citationTaskHistory').querySelectorAll('[data-open-citation-task]').forEach(b=>b.onclick=()=>safely(async()=>{const r=await api(p,`/tasks/${b.dataset.openCitationTask}`);if(same(p)){task=r;render();}}));
  }
  async function sources(p=pid()){
    const r=await api(p,'/source-profiles');if(!same(p))return;const root=$('#citationTaskSources');root.replaceChildren();
    const selected=new Set([...$('#citationTaskSourceScope').querySelectorAll('input:checked')].map(e=>e.value));
    $('#citationTaskSourceScope').innerHTML=r.sources.map(s=>`<label><input type="checkbox" value="${esc(s.source_id)}" ${selected.has(s.source_id)?'checked':''}>${esc(s.title||s.cite_key)}</label>`).join('')||'<p>暂无当前论文来源；可以先检查未引用论断，仍不能把缺来源判断为支持。</p>';
    for(const source of r.sources){
      const profile=r.profiles[source.source_id]||{},row=document.createElement('details');
      row.innerHTML=`<summary>${esc(source.title||source.cite_key)} · 记录v${profile.version||0}</summary>
        <label>解析可靠性<select data-parse><option value="unverified">尚未核实</option><option value="verified">已人工核实</option><option value="unreliable">不可靠/OCR错误</option></select></label>
        <label>来源角色<select data-role><option value="unknown">未知</option><option value="primary">实际原文</option><option value="secondary">二手转引</option><option value="note">阅读笔记</option></select></label>
        <label>证据类型<select data-kind><option value="text">文本</option><option value="table">表格数值</option><option value="figure">图像</option><option value="mixed">混合</option></select></label>
        <label>实际原文来源编号<input data-origin value="${esc(profile.origin_source_id||'')}"></label>
        <label>已核实的同研究组（未知留空）<input data-group value="${esc(profile.study_group||'')}"></label>
        <label><input data-derived type="checkbox" ${profile.derived_from_current?'checked':''}>来源是当前论文或其派生副本</label><button data-save-profile type="button">保存来源核验记录</button>`;
      row.querySelector('[data-parse]').value=profile.parse_reliability||'unverified';row.querySelector('[data-role]').value=profile.source_role||'unknown';row.querySelector('[data-kind]').value=profile.evidence_kind||'text';
      row.querySelector('[data-save-profile]').onclick=()=>safely(async()=>{await api(p,`/source-profiles/${encodeURIComponent(source.source_id)}`,{expected_version:profile.version||0,parse_reliability:row.querySelector('[data-parse]').value,source_role:row.querySelector('[data-role]').value,evidence_kind:row.querySelector('[data-kind]').value,origin_source_id:row.querySelector('[data-origin]').value,study_group:row.querySelector('[data-group]').value,derived_from_current:row.querySelector('[data-derived]').checked,visual_asset_version_id:profile.visual_asset_version_id||''},'PUT');if(same(p)){note('来源记录已更新；旧报告将失效，请重新预览核验。');await sources(p);}});
      root.append(row);
    }
  }
  async function safely(work){try{await work();}catch(e){note(error(e));if($('#startCitationTask'))$('#startCitationTask').disabled=false;}}
  $('#previewCitationTask').onclick=()=>safely(()=>preview());
  $('#refreshCitationTasks').onclick=()=>safely(async()=>{await history();await sources();});
  $('#editor')?.addEventListener('input',()=>{
    ++inputRevision;++token;
    if(preflight?.options.input_kind==='draft'){preflight=null;$('#citationTaskPreflight').replaceChildren();note('当前草稿已变化，请重新预览；原报告仍按旧快照保留。');}
    if(task?.options?.input_kind==='draft'){task.stale=true;render();}
  });
  document.addEventListener('paperpilot:project-changed',()=>{++token;task=null;preflight=null;$('#citationTaskResult').replaceChildren();$('#citationTaskPreflight').replaceChildren();$('#citationTaskSources').replaceChildren();$('#citationTaskHistory').replaceChildren();note('');if(pid())safely(async()=>{await history();await sources();});});
  if(pid())safely(async()=>{await history();await sources();});
})();
