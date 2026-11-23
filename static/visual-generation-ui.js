(() => {
  const pp = window.paperpilot;
  const $ = s => document.querySelector(s);
  if (!pp || !$("#createDrawingRun")) return;
  const names = {structured_svg:"结构化流程图",scene_graph:"可编辑图形",ai_image:"AI 图片"};
  const states = {awaiting_confirmation:"等待选择绘图方式",tool_ready:"输入已确认，可以生成预览",queued:"已排队",running:"正在生成候选",preview_ready:"候选可供审阅",saved:"已保存到图像库",cancelled:"已取消",unknown:"结果不明，需要对账",failed:"执行失败",stale:"输入已过期"};
  const esc = s => String(s ?? "").replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const uid = () => crypto.randomUUID();
  $("#saveDrawingProvider").insertAdjacentHTML("beforebegin", '<label class="drawing-check"><input id="drawingProviderVision" type="checkbox">支持视觉分析（核验图像内容）</label><label>视觉核验单次报价上限（USD，未知留空）<input id="drawingProviderCheckPrice" type="number" min="0" step="0.001"></label>');
  let projectId = "", run = null, runs = [], busy = false, dirty = false, pollBusy = false, sceneEdges = [];
  const currentId = () => pp.data?.project?.id || "";
  const base = pid => `/api/projects/${pid}/drawing`;
  const message = e => {try {const d=JSON.parse(e.message);return d.detail?.message||e.message;}catch{return e.message;}};
  function status(text,error=false){$("#drawingStatus").textContent=text;$("#drawingStatus").dataset.error=error;}
  async function post(path,body={},pid=projectId){return pp.api(base(pid)+path,{method:"POST",body:JSON.stringify(body)});}
  async function perform(action){if(busy)return;busy=true;try{await action();}catch(e){status(message(e),true);}finally{busy=false;updateButtons();}}
  function updateButtons(){
    const editable = run && !run.attempts.length && run.status!=="cancelled";
    $("#confirmDrawingType").disabled=busy||!editable;
    $("#dispatchDrawingRun").disabled=busy||!run||run.status!=="tool_ready"||dirty||run.stale_reasons.length>0;
    $("#undoDrawingEdit").disabled=busy||!editable||run.history_cursor<1;
    $("#redoDrawingEdit").disabled=busy||!editable||run.history_cursor>=run.history.length-1;
    $("#cancelDrawingRun").disabled=busy||!run||["saved","cancelled"].includes(run.status);
    $("#reconcileDrawingRun").disabled=busy||!run;
    $("#copyDrawingRun").disabled=busy||!run;
  }
  function typeFields(){const kind=$("#drawingType").value;$("#drawingGraphFields").hidden=kind==="ai_image";$("#drawingImageFields").hidden=kind!=="ai_image";$("#drawingSceneEditor").hidden=kind!=="scene_graph";}
  function nodeRows(nodes){$("#drawingNodeRows").innerHTML=nodes.map((n,i)=>`<fieldset class="drawing-node" data-node-id="${esc(n.id)}"><legend>节点 ${i+1}</legend><label>文字<input data-field="label" value="${esc(n.label)}" maxlength="300"></label><label>横坐标<input data-field="x" type="number" min="0" value="${n.x}"></label><label>纵坐标<input data-field="y" type="number" min="0" value="${n.y}"></label><label>宽<input data-field="width" type="number" min="10" value="${n.width??140}"></label><label>高<input data-field="height" type="number" min="10" value="${n.height??60}"></label><label>形状<select data-field="shape"><option value="rect" ${n.shape==="rect"?"selected":""}>矩形</option><option value="ellipse" ${n.shape==="ellipse"?"selected":""}>椭圆</option><option value="text" ${n.shape==="text"?"selected":""}>文字</option></select></label></fieldset>`).join("");}
  function layout(){const labels=$("#drawingLabels").value.split("\n").map(s=>s.trim()).filter(Boolean);nodeRows(labels.map((label,i)=>({id:`n${i}`,label,x:30+(i%4)*190,y:30+Math.floor(i/4)*110,width:150,height:65,shape:"rect"})));dirty=true;updateButtons();}
  function collectSpec(){
    const kind=$("#drawingType").value;
    if(kind==="ai_image")return {prompt:$("#drawingImagePrompt").value,size:$("#drawingImageSize").value,input_asset_version:$("#drawingInputAsset").value||null,mask_version:$("#drawingMaskAsset").value||null};
    let nodes;
    if(kind==="scene_graph") nodes=[...document.querySelectorAll("#drawingNodeRows .drawing-node")].map(row=>{const n={id:row.dataset.nodeId};row.querySelectorAll("[data-field]").forEach(el=>n[el.dataset.field]=el.type==="number"?Number(el.value):el.value);return n;});
    else nodes=$("#drawingLabels").value.split("\n").map(s=>s.trim()).filter(Boolean).map((label,i)=>({id:`n${i}`,label}));
    const edges=kind==="scene_graph"?sceneEdges:nodes.slice(1).map((n,i)=>({source:nodes[i].id,target:n.id}));
    return kind==="scene_graph"?{schema_version:1,width:Number($("#drawingWidth").value),height:Number($("#drawingHeight").value),nodes,edges}:{nodes,edges};
  }
  function fillInput(item){
    $("#drawingType").value=item.selected_type||item.recommendation.suggested_type;
    const spec=item.spec||{};
    sceneEdges=spec.edges||[];
    if(spec.nodes){$("#drawingLabels").value=spec.nodes.map(n=>n.label).join("\n");nodeRows(spec.nodes.map((n,i)=>({...n,x:n.x??30+(i%4)*190,y:n.y??30+Math.floor(i/4)*110})));}
    else layout();
    $("#drawingWidth").value=spec.width||900;$("#drawingHeight").value=spec.height||500;
    $("#drawingImagePrompt").value=spec.prompt||item.intent;$("#drawingImageSize").value=spec.size||"1024x1024";
    $("#drawingInputAsset").value=spec.input_asset_version||"";$("#drawingMaskAsset").value=spec.mask_version||"";
    const explicit=item.plan?.sources?.find(s=>s.source==="explicit_request")?.values||{};
    $("#drawingUseFill").checked=!!explicit.fill;if(explicit.fill)$("#drawingFill").value=explicit.fill;
    $("#drawingExternalConsent").checked=false;$("#drawingUnknownPrice").checked=false;
    dirty=false;typeFields();
  }
  function render(item,fill=false){
    run=item;$("#drawingRunDetails").hidden=false;
    const option=[...$("#drawingRunSelect").options].find(o=>o.value===item.id);if(option)option.textContent=`${item.title} · ${states[item.status]||item.status}`;
    const r=item.recommendation;
    $("#drawingRecommendation").innerHTML=`<strong>${esc(states[item.status]||item.status)}</strong><p>建议：${esc(names[r.suggested_type])} · 参考置信度 ${Math.round(r.confidence*100)}%</p><p>${esc(r.reason)}</p><p>假设：${esc(r.assumptions.join("；"))}</p><p>备选：${esc(r.alternatives.map(x=>names[x]).join("、"))}</p><p>注意：${esc(r.risks.join("；"))}</p>${item.stale_reasons.length?`<p class="drawing-warning">输入差异：${esc(item.stale_reasons.join("；"))}。请重新确认或复制任务。</p>`:""}`;
    $("#drawingStyleSources").textContent=item.plan?.sources?.map(s=>`${s.source} · v${s.version}${s.approval?" · 已批准":""}`).join("\n")||"确认输入后显示本次风格来源。";
    $("#drawingCandidateList").innerHTML=item.candidates.map(c=>`<article class="drawing-candidate"><h4>候选预览</h4><img src="${esc(c.preview_url)}" alt="待审阅的绘图候选"><p>${esc(c.status==="ready"?"请核对结构、文字与事实；保存后可在图像库中插入正文。":c.status==="saved"?"已保存为独立图像资产。":c.status==="cancelled"?"取消后的历史候选，不能保存。":"候选已过期："+c.stale_reasons.join("；"))}</p><button type="button" data-save-candidate="${esc(c.id)}" ${c.status!=="ready"?"disabled":""}>审阅通过，保存到图像库</button></article>`).join("");
    $("#drawingCandidateList").querySelectorAll("[data-save-candidate]").forEach(button=>button.onclick=()=>perform(async()=>{const pid=projectId;const saved=await post(`/candidates/${button.dataset.saveCandidate}/save`,{request_id:uid(),expected_run_revision:run.revision},pid);if(pid!==projectId)return;status("候选已保存到图像库，可选择版本后插入正文。");await pp.reload();await refresh(true);if(pid===currentId())await pp.openVisualAsset?.(saved.asset.id,saved.version.id);}));
    if(item.attempts.length){const a=item.attempts.at(-1);if(a.error_code)status(`任务状态：${states[item.status]||item.status}。错误：${a.error_code}。结果不明时请使用“对账与恢复”；不会自动重发。`,true);}
    if(fill)fillInput(item);updateButtons();
  }
  async function refresh(fill=false){
    const pid=projectId;if(!pid)return;
    const result=await pp.api(base(pid)+"/runs");if(pid!==projectId)return;
    runs=result.runs;const chosen=run?.id;
    $("#drawingRunSelect").innerHTML=runs.map(r=>`<option value="${esc(r.id)}">${esc(r.title)} · ${esc(states[r.status]||r.status)}</option>`).join("")||'<option value="">暂无任务</option>';
    const item=runs.find(r=>r.id===chosen)||runs[0];if(item){$("#drawingRunSelect").value=item.id;render(item,fill||!chosen);}else{run=null;$("#drawingRunDetails").hidden=true;}
  }
  async function refreshAssets(){
    const pid=projectId;const [result,cap]=await Promise.all([pp.api(base(pid)+"/assets"),pp.api(base(pid)+"/capabilities")]);if(pid!==projectId)return;
    const versions=result.assets.filter(a=>["image/png","image/jpeg","image/webp"].includes(a.current_version?.mime_type));
    for(const key of ["drawingInputAsset","drawingMaskAsset"]){const el=$("#"+key),selected=el.value;el.innerHTML='<option value="">不使用'+(key==="drawingMaskAsset"?"遮罩":"参考图")+'</option>'+versions.map(a=>`<option value="${esc(a.current_version.id)}">${esc(a.title)} · v${a.current_version.version_no}</option>`).join("");el.value=selected;}
    const c=cap.capabilities;$("#drawingCapabilities").textContent=`当前提供方：${cap.provider_name}；模型：${cap.model||"未选择"}；生成：${c.image_generation?"支持":"未登记"}；参考图：${c.image_edit?"支持":"未登记"}；遮罩：${c.mask?"支持":"未登记"}；单张费用：${c.price_per_image==null?"未知":c.price_per_image+" USD"}。`;
  }
  $("#createDrawingRun").onclick=()=>perform(async()=>{const pid=projectId;const created=await post("/runs",{title:$("#drawingTitle").value||"论文图像",intent:$("#drawingIntent").value,request_id:uid()},pid);if(pid!==projectId)return;run=created;await refreshAssets();await refresh(true);status("建议已生成。请选择绘图方式、核对输入，再确认；尚未运行绘图工具。");});
  $("#drawingType").onchange=()=>{typeFields();dirty=true;updateButtons();};
  $("#layoutDrawingNodes").onclick=()=>{layout();const rows=[...document.querySelectorAll("#drawingNodeRows .drawing-node")];sceneEdges=rows.slice(1).map((n,i)=>({source:rows[i].dataset.nodeId,target:n.dataset.nodeId}));};
  $("#drawingRunDetails").addEventListener("input",e=>{if(e.target.closest("#drawingImageFields")&&["drawingBudget","drawingExternalConsent","drawingUnknownPrice"].includes(e.target.id))return;dirty=true;updateButtons();});
  $("#confirmDrawingType").onclick=()=>perform(async()=>{const pid=projectId;const result=await post(`/runs/${run.id}/decision`,{expected_revision:run.revision,request_id:uid(),selected_type:$("#drawingType").value,spec:collectSpec(),constraints:$("#drawingUseFill").checked&&$("#drawingType").value!=="ai_image"?{fill:$("#drawingFill").value}:{}},pid);if(pid!==projectId)return;render(result,true);status("输入与类型已确认。点击“生成候选预览”执行本次任务。");});
  $("#dispatchDrawingRun").onclick=()=>perform(async()=>{const pid=projectId,provider=run.selected_type==='ai_image'?await pp.api(base(pid)+'/capabilities'):null;await post(`/runs/${run.id}/dispatch`,{expected_revision:run.revision,request_id:uid(),external_consent:$("#drawingExternalConsent").checked,unknown_price_confirmed:$("#drawingUnknownPrice").checked,hard_limit:$("#drawingBudget").value===""?null:Number($("#drawingBudget").value),temporary_key:provider?(pp.takeTemporaryCredential?.(provider.profile_id)||''):''},pid);if(pid!==projectId)return;status("任务已提交；候选生成后将在此显示，尚未保存到资产库。");await refresh();});
  for(const action of ["undo","redo","cancel","reconcile"]){const selector={undo:"#undoDrawingEdit",redo:"#redoDrawingEdit",cancel:"#cancelDrawingRun",reconcile:"#reconcileDrawingRun"}[action];$(selector).onclick=()=>perform(async()=>{const pid=projectId;const result=await post(`/runs/${run.id}/${action}`,{expected_revision:run.revision,request_id:uid()},pid);if(pid!==projectId)return;render(result,["undo","redo"].includes(action));status(result.message||states[result.status]||"操作已记录。");});}
  $("#copyDrawingRun").onclick=()=>perform(async()=>{const pid=projectId;const copied=await post(`/runs/${run.id}/copy`,{expected_revision:run.revision,request_id:uid()},pid);if(pid!==projectId)return;run=copied;await refresh(true);if(copied.copied_type){fillInput({...copied,selected_type:copied.copied_type,spec:copied.copied_spec});dirty=true;}status(copied.message);});
  $("#drawingRunSelect").onchange=()=>{const item=runs.find(r=>r.id===$("#drawingRunSelect").value);if(item)render(item,true);};
  $("#refreshDrawingRuns").onclick=()=>perform(async()=>{await refreshAssets();await refresh();status("任务已刷新，本地尚未提交的输入保留。");});
  $("#saveDrawingProvider").onclick=()=>perform(async()=>{
    const pid=projectId,model=$("#drawingProviderModel").value.trim(),price=$("#drawingProviderPrice").value;
    if(!model)throw new Error("请填写提供方的实际图像模型编号。");
    const checkPrice=$("#drawingProviderCheckPrice").value;
    const cap={image_generation:$("#drawingProviderGenerate").checked,image_edit:$("#drawingProviderEdit").checked,mask:$("#drawingProviderMask").checked,vision_analysis:$("#drawingProviderVision").checked,price_per_image:price===""?null:Number(price),price_per_check:checkPrice===""?null:Number(checkPrice),currency:"USD"};
    const profile=await pp.api("/api/ai/profiles",{method:"POST",body:JSON.stringify({request_id:uid(),name:$("#drawingProviderName").value||model,base_url:$("#drawingProviderUrl").value.trim(),auth_mode:$("#drawingProviderAuth").value,models:[model],capabilities:{[model]:cap}})});
    if(pid!==projectId)return;
    await pp.selectProviderProfile?.(profile.id,cap.image_generation||cap.image_edit?'drawing':'vision');status('配置草稿已保存；已打开模型测试引导。测试、启用并明确绑定后才可执行，不会自动改动写作模型。');
  });
  $('#saveDrawingProvider').textContent='保存草稿并进入测试引导';
  // The legacy button now enters the same review flow, never injecting SVG into the page.
  $("#drawImage").onclick=()=>perform(async()=>{const pid=projectId;const result=await pp.api(`/api/projects/${pid}/draw`,{method:"POST",body:JSON.stringify({title:$("#drawTitle").value||"论文示意图",labels:$("#drawLabels").value.split(",").map(s=>s.trim()).filter(Boolean)})});if(pid!==projectId)return;run=result.run;$("[data-tab='visual']").click();await refresh(true);status(result.message);});
  setInterval(async()=>{if(busy||pollBusy)return;const id=currentId();if(!id)return;pollBusy=true;try{if(id!==projectId){projectId=id;run=null;runs=[];dirty=false;$("#drawingRunDetails").hidden=true;$("#drawingIntent").value="";await refreshAssets();await refresh(true);status("描述图片用途，或继续当前论文的已有任务。");}else if(run&&["queued","running"].includes(run.status)){await refresh();}}catch(e){status(message(e),true);}finally{pollBusy=false;}},1000);
})();
