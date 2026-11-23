(() => {
  const pp=window.paperpilot,$=s=>document.querySelector(s);
  if(!pp||!$("#runVisualEvidence"))return;
  const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels={pass:"检查通过",warning:"存在提醒",failed:"检查失败",unknown:"信息不足或未执行",needs_human_review:"需要人工核验"};
  const checks={file_integrity:"文件完整性",readability:"可读性",provenance:"来源记录",source_consistency:"来源一致性",semantic:"科学含义",style:"参考风格",cloud_review:"云端核验",execution:"执行状态"};
  const decisions={accept:"作者认可",reject:"作者驳回",needs_review:"需要进一步核验"};
  const uid=()=>crypto.randomUUID();
  let projectId="",reports=[],selected=null,pending=null,busy=false,pollBusy=false,capacityPreview=null;
  const base=pid=>`/api/projects/${pid}`;
  function status(text,error=false){$("#visualEvidenceStatus").textContent=text;$("#visualEvidenceStatus").dataset.error=error;}
  const message=e=>{try{return JSON.parse(e.message).detail?.message||e.message;}catch{return e.message;}};
  async function post(path,payload={},pid=projectId){return pp.api(base(pid)+path,{method:"POST",body:JSON.stringify(payload)});}
  async function perform(action){if(busy)return;busy=true;for(const id of ['runVisualEvidence','saveVisualReview','previewVisualCapacity','repairVisualCapacity'])$('#'+id).disabled=true;try{await action();}catch(e){status(message(e),true);}finally{busy=false;for(const id of ['runVisualEvidence','previewVisualCapacity','repairVisualCapacity'])$('#'+id).disabled=false;$("#runVisualEvidence").disabled=!$("#evidenceAsset").value;$("#saveVisualReview").disabled=!!selected?.stale;}}
  function render(report){
    selected=report;$("#visualStyleCandidate").textContent="";
    const stale=report.stale?`<p class="drawing-warning">此报告和人工决定已过期：${esc(report.stale_reasons.join("；"))}。历史记录保留，请对当前图像重新核验。</p>`:"";
    $("#visualReportDetail").innerHTML=`<h4>${esc(labels[report.summary])} · 报告 v${report.version}</h4>${stale}<p class="muted">${esc(report.limitations.join("；"))}</p><div class="visual-finding-list">${report.findings.map(f=>`<article class="visual-finding" data-status="${esc(f.status)}"><strong>${esc(checks[f.check_type]||f.check_type)} · ${esc(labels[f.status])}</strong><p>${esc(f.message)}</p><small>${esc(f.limitations)}</small><details><summary>证据位置与检查方法</summary><pre>${esc(JSON.stringify(f.location,null,2))}</pre><p>${esc(f.method)} · ${esc(f.rule_version.slice(0,12))}</p></details></article>`).join("")}</div><h4>人工决定（独立记录）</h4>${report.reviews.map(r=>`<article class="visual-review"><strong>${esc(r.reviewer)} · ${esc(decisions[r.decision])}${r.stale?" · 已过期":""}</strong><p>${esc(r.reason)}</p><small>${esc(r.evidence_refs.join("；"))} · ${esc(r.created_at)}</small></article>`).join("")||'<p class="muted">尚无人工决定。</p>'}`;
    $("#visualReviewForm").hidden=false;$("#saveVisualReview").disabled=report.stale;$("#extractVisualStyle").disabled=report.stale;
  }
  async function refresh(){
    const pid=projectId;if(!pid)return;
    const [assets,list,paper]=await Promise.all([pp.api(base(pid)+"/drawing/assets"),pp.api(base(pid)+"/visual-reports"),pp.api(base(pid))]);if(pid!==projectId)return;
    for(const key of ["evidenceAsset","evidenceSourceAsset"]){const el=$("#"+key),old=el.value;el.innerHTML='<option value="">'+(key==="evidenceAsset"?"选择待核验图像":"不使用参考图")+'</option>'+assets.assets.map(a=>`<option value="${esc(a.current_version_id)}">${esc(a.title)} · v${a.current_version.version_no}</option>`).join("");if([...el.options].some(o=>o.value===old))el.value=old;if(key==="evidenceAsset"&&!el.value&&assets.assets.length)el.value=assets.assets[0].current_version_id;}
    const source=$("#evidenceCorpus"),old=source.value;source.innerHTML='<option value="">不使用语料原文</option>'+(paper.corpus||[]).map(c=>`<option value="${esc(c.id)}">${esc(c.title)}</option>`).join("");source.value=old;
    reports=list.reports;$("#visualReportSelect").innerHTML=reports.map(r=>`<option value="${esc(r.id)}">${esc(new Date(r.created_at).toLocaleString())} · ${esc(labels[r.summary])}${r.stale?" · 已过期":""}</option>`).join("")||'<option value="">暂无报告</option>';
    const report=reports.find(r=>r.id===selected?.id)||reports[0];if(report){$("#visualReportSelect").value=report.id;render(report);}else{selected=null;$("#visualReportDetail").textContent="";$("#visualReviewForm").hidden=true;}
  }
  async function inputs(){
    const pid=projectId,vid=$("#evidenceAsset").value;if(pid!==pp.current?.project.id)throw new Error("论文切换中，请稍后在当前论文重新预览。");if(!vid)throw new Error("请先选择待核验图像。");
    let source=null;const corpus=$("#evidenceCorpus").value;if(corpus){const lineage=await pp.api(base(pid)+`/corpus/${encodeURIComponent(corpus)}/lineage`);const version=lineage.current_source_version?.version;if(!version)throw new Error("所选语料没有可用原文版本。");source={corpus_id:corpus,version};}
    const sourceAsset=$("#evidenceSourceAsset").value,region=sourceAsset?{x:Number($("#evidenceRegionX").value),y:Number($("#evidenceRegionY").value),width:Number($("#evidenceRegionWidth").value),height:Number($("#evidenceRegionHeight").value)}:null;
    if(pid!==projectId||pid!==pp.current?.project.id)throw new Error('论文已切换，请在当前论文重新选择核验输入。');
    return {asset_version_id:vid,source,source_asset_version:sourceAsset||null,region,request_id:uid(),check_types:["file_integrity","readability","provenance","source_consistency","semantic","style"]};
  }
  function showCapacity(result){
    if(!result.generation_allowed){$('#visualCapacityPreview').textContent=result.message;return;}
    const c=result.capacity,p=result.provider;
    $('#visualCapacityPreview').textContent=`接收方：${p.name} · ${p.model} · v${p.config_revision} · ${p.base_url}。${c.image_count}张图（${c.images.map(i=>`${i.width}×${i.height}`).join('、')}），图片上界${c.estimated_image_tokens}，文本/封装${c.estimated_text_and_framing_tokens}，输出预留${c.reserved_output_tokens}，余量${c.safety_margin_tokens}，合计${c.total_with_output_and_margin} tokens。视觉依据：${c.vision_capacity_source}；总容量依据：${c.capacity_source}。用户提供，未实测。此次仅本地预览，外发0次。`;
  }
  async function previewCapacity(payload,pid){const result=await post('/visual-checks/preview',payload,pid);if(pid!==projectId||pid!==pp.current?.project.id)throw new Error('论文已切换，请重新预览。');showCapacity(result);return result;}
  $('#previewVisualCapacity').onclick=()=>perform(async()=>{const pid=projectId;capacityPreview=await previewCapacity(await inputs(),pid);$('#evidenceCloud').checked=false;status(capacityPreview.generation_allowed?'容量预览已更新。请核对接收方、范围、容量来源与费用，再明确授权本次云端核验。':capacityPreview.message,!capacityPreview.generation_allowed);});
  $('#repairVisualCapacity').onclick=()=>perform(async()=>{const pid=projectId,provider=await pp.api(base(pid)+'/drawing/capabilities?purpose=vision');if(pid!==projectId)return;await pp.selectProviderProfile?.(provider.profile_id,'vision',provider.model);status('已打开当前视觉模型配置；补齐容量/来源→保存→明确测试→启用→绑定视觉用途，再返回这里重新预览。');});
  for(const id of ['evidenceAsset','evidenceCorpus','evidenceSourceAsset','evidenceRegionX','evidenceRegionY','evidenceRegionWidth','evidenceRegionHeight'])$('#'+id).addEventListener('input',()=>{capacityPreview=null;$('#evidenceCloud').checked=false;$('#visualCapacityPreview').textContent='输入已变化，请重新预览容量后授权；当前输入保留。';});
  $("#runVisualEvidence").onclick=()=>perform(async()=>{
    const pid=projectId,payload=await inputs(),cloud=$("#evidenceCloud").checked;
    if(cloud){const old=capacityPreview,result=await previewCapacity(payload,pid);capacityPreview=result;if(!result.generation_allowed){$('#evidenceCloud').checked=false;throw new Error(result.message);}if(!old||JSON.stringify(old)!==JSON.stringify(result)){$('#evidenceCloud').checked=false;throw new Error('容量或接收方预览已更新，请核对上方信息并重新明确授权；本次未外发。');}}
    const provider=cloud?await pp.api(`/api/projects/${pid}/drawing/capabilities?purpose=vision`):null;
    if(pid!==projectId)return;
    const result=await post("/visual-checks",{...payload,cloud,capacity_preview_hash:cloud?capacityPreview.preview_hash:null,external_consent:cloud,unknown_price_confirmed:$("#evidenceUnknownPrice").checked,hard_limit:$("#evidenceBudget").value===""?null:Number($("#evidenceBudget").value),temporary_key:provider?(pp.takeTemporaryCredential?.(provider.profile_id)||''):''},pid);
    if(pid!==projectId)return;pending=result.id;$("#evidenceCloud").checked=false;$("#evidenceUnknownPrice").checked=false;status("核验任务已提交；不会修改正文或自动批准知识。");
  });
  $("#visualReviewForm").onsubmit=e=>{e.preventDefault();perform(async()=>{if(!selected)return;const pid=projectId;const result=await post(`/visual-reports/${selected.id}/reviews`,{request_id:uid(),report_version:selected.version,expected_review_count:selected.reviews.length,reviewer:$("#visualReviewer").value,decision:$("#visualReviewDecision").value,reason:$("#visualReviewReason").value,evidence_refs:$("#visualReviewRefs").value.split("\n").filter(Boolean)},pid);if(pid!==projectId)return;render(result);$("#visualReviewReason").value="";$("#visualReviewRefs").value="";status("人工决定已追加。自动发现项保持原记录。");});};
  $("#extractVisualStyle").onclick=()=>perform(async()=>{const pid=projectId;const candidate=await post(`/visual-reports/${selected.id}/style-candidates`,{},pid);if(pid!==projectId)return;if(candidate.status==="unavailable"){status(candidate.reason,true);return;}$("#visualStyleCandidate").innerHTML=`<p>参考区域主色候选：<strong>${esc(candidate.values.fill)}</strong>。此候选尚未生效。</p><button type="button" id="approveVisualColor">批准用于当前论文流程图</button>`;$("#approveVisualColor").onclick=()=>perform(async()=>{await post(`/drawing/style-rules/${candidate.rule_id}/approve`,{},pid);if(pid!==projectId)return;status("颜色规则已获批准。旧风格计划和报告需重新核验。");await refresh();});status("已生成带来源位置的颜色候选；需要显式批准才生效。");});
  $("#visualReportSelect").onchange=()=>{const r=reports.find(r=>r.id===$("#visualReportSelect").value);if(r)render(r);};
  $("#refreshVisualReports").onclick=()=>perform(async()=>{await refresh();status("图像与报告已刷新；未提交的复核理由保留。");});
  setInterval(async()=>{if(busy||pollBusy)return;const id=pp.data?.project?.id;if(!id)return;pollBusy=true;try{if(id!==projectId){projectId=id;reports=[];selected=null;pending=null;capacityPreview=null;$('#evidenceCloud').checked=false;$('#evidenceUnknownPrice').checked=false;$('#visualCapacityPreview').textContent='尚未预览；仅做本地核验无需模型容量。';$("#visualReviewReason").value="";$("#visualReviewRefs").value="";$("#visualStyleCandidate").textContent="";await refresh();}else if(pending){const pid=projectId,result=await pp.api(base(pid)+`/visual-checks/${pending}`);if(pid!==projectId)return;if(result.report_id){pending=null;selected={id:result.report_id};await refresh();status(result.status==="failed"?"核验失败，未完成云端检查。请阅读执行状态中的原因与修复步骤，原输入和本地报告保留。":result.status==="unknown"?"云端结果不明，已保留本地检查与诊断；请先对账，不会重发。":"核验报告已生成。请阅读自动发现并按需追加人工决定。");}}}catch(e){status(message(e),true);}finally{pollBusy=false;}},1000);
})();
