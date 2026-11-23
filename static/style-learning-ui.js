(() => {
  const pp=window.paperpilot,$=s=>document.querySelector(s);
  if(!pp||!$('#styleLearningPanel'))return;
  const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const labels={disabled:'已关闭',collecting:'积累样本',distilling:'整理中',active:'已生效',paused:'已暂停',failed:'处理失败',candidate:'待批准',approved:'已批准',stale:'已失效',queued:'已排队',running:'整理中',completed:'已完成',example:'示例',feedback:'明确反馈',accepted_edit:'已提交修改',rejected_edit:'拒绝观察'};
  const familyLabel=value=>value.startsWith('feedback:')?'作者明确反馈':({sentence_length:'句长建议',paragraph_length:'段落长度',transitions:'衔接表达'}[value]||'经验规则');
  const methodLabel=value=>({local_sentence_statistics_v1:'近期句长统计',local_paragraph_statistics_v1:'近期分段统计',local_transition_frequency_v1:'近期衔接表达频率',explicit_author_feedback_v1:'作者主动提供的反馈',explicit_author_edit:'作者编辑'}[value]||'来源已不可用');
  let pid='',view=null,busy=false,loading=false,lastPoll=0,signature='',selected=new Set(),edits=new Map();
  const drafts=new Map(),base=()=>`/api/projects/${pid}/style-learning`;
  const message=e=>{try{return JSON.parse(e.message).detail?.message||e.message;}catch{return e.message;}};
  function status(text,error=false){$('#learningStatus').textContent=text;$('#learningStatus').dataset.error=String(error);}
  function controls(){
    const active=view?.opt_in,writable=pp.current?.project.lifecycle_state!=='published';
    $('#learningBody').querySelectorAll('button').forEach(b=>b.disabled=busy||!view||!writable);
    $('#learningRefresh').disabled=busy||!view;
    $('#learningPreview').disabled=busy||!view;
    $('#learningPause').disabled=busy||!active||!writable;
    $('#learningDisable').disabled=busy||!active||!writable;
    $('#learningResume').disabled=busy||!['paused','failed'].includes(view?.state)||!writable;
    $('#learningDistill').disabled=busy||!active||!view?.signals.some(s=>s.eligible)||!writable;
    $('#learningAddSignal').disabled=busy||!active||!view?.scopes.includes($('#learningSignalKind').value)||!writable;
    $('#learningApprove').disabled=busy||!active||!selected.size||!writable;
    $('#learningRollback').disabled=busy||!active||!$('#learningHistory').value||!writable;
    $('#learningDeleteAll').disabled=busy||!$('#learningDeleteConfirmed').checked||!writable;
  }
  function render(next){
    const changed=view&&JSON.stringify([view.version,view.effective_rule_ids,view.profile_applies])!==JSON.stringify([next.version,next.effective_rule_ids,next.profile_applies]);
    if(changed){$('#learningContextPreview').textContent='学习档案或授权已变化，请重新预览。';document.dispatchEvent(new CustomEvent('paperpilot:style-learning-changed',{detail:{projectId:pid}}));}
    view=next;$('#learningBody').hidden=false;$('#styleLearningPanel').dataset.projectId=pid;
    $('#learningBadge').textContent=labels[next.state]||next.state;
    $('#learningEffective').textContent=next.profile_applies?`有效档案 v${next.profile_version}：${next.effective_instructions.join('；')}`:'当前没有生效经验；正文与显式风格设置保持原有规则。';
    const sig=JSON.stringify([next.rules,next.signals,next.profiles,next.runs]);
    if(sig!==signature){
      signature=sig;
      selected=new Set([...selected].filter(id=>next.rules.some(r=>r.id===id&&r.status==='candidate'&&r.source_valid)));
      $('#learningRules').innerHTML=next.rules.map(r=>`<article class="learning-rule" data-rule="${esc(r.id)}"><strong>${esc(familyLabel(r.family))} · v${r.version} · ${esc(labels[r.status]||r.status)}${r.source_valid?'':' · 来源不可用'}</strong><p>${esc(r.instruction||'来源已过期或删除，内容不可用。')}</p><small>依据：${esc(methodLabel(r.method.method))} · 置信度 ${r.confidence}（未校准）<br>${esc(r.method.limitation||'')}</small><details><summary>核对来源与统计依据</summary><small>来源样本：${r.source_signal_ids.map(id=>esc(next.signals.find(s=>s.id===id)?.source_version||id)).join('、')}${r.method.sentence_count?`<br>${r.method.sentence_count}句，句长中位数 ${r.method.median_units}字/词`:''}${r.method.counts?`<br>${Object.entries(r.method.counts).map(([word,count])=>`${esc(word)}：${count}次`).join('；')}`:''}</small></details>${r.source_valid?`${r.status==='candidate'?`<label><input type="checkbox" data-select-rule="${esc(r.id)}" ${selected.has(r.id)?'checked':''}>选择此候选</label>`:''}<label>编辑为新候选<textarea data-edit-rule="${esc(r.id)}" rows="2" maxlength="2000">${esc(edits.get(r.id)??r.instruction)}</textarea></label><button type="button" data-save-rule="${esc(r.id)}">保存为新候选</button>${next.effective_rule_ids.includes(r.id)?` <button type="button" data-remove-rule="${esc(r.id)}">移出当前档案</button>`:''}`:''}</article>`).join('')||'<p>暂无规则。先授权并积累至少3个完整句子的示例，或提供明确反馈；不足时会说明原因。</p>';
      $('#learningSignals').innerHTML=next.signals.map(s=>`<article class="learning-signal"><strong>${esc(labels[s.kind])} · ${s.eligible?'可用':'当前不可用'}</strong><small>${esc(s.source_version)}<br>保留至 ${esc(new Date(s.retention_until).toLocaleString())} · 来源 ${esc(s.id)}</small><pre>${esc(s.content||'已过期，不再显示内容。')}</pre><button type="button" data-delete-signal="${esc(s.id)}">删除此学习副本及依赖规则</button></article>`).join('')||'<p>暂无学习样本，不会回扫授权前的论文。</p>';
      $('#learningRuns').innerHTML=next.runs.map(r=>`<article class="learning-run">任务 ${esc(labels[r.status]||r.status)}${r.error_code?` · ${esc(r.error_code)}`:''}<small>${r.diagnostics.map(d=>esc(d.message)).join('<br>')}</small></article>`).join('');
      const old=$('#learningHistory').value;
      $('#learningHistory').innerHTML=next.profiles.filter(p=>p.source_valid).map(p=>`<option value="${esc(p.id)}">v${p.version}${p.id===next.active_profile_id?' · 当前':''} · ${esc(new Date(p.created_at).toLocaleString())}</option>`).join('')||'<option value="">无来源有效的历史档案</option>';
      if([...$('#learningHistory').options].some(o=>o.value===old))$('#learningHistory').value=old;
    }
    controls();
  }
  async function refresh(announce=false){const id=pid,next=await pp.api(base());if(id!==pid)return;render(next);if(announce)status((next.last_error_code?`处理失败：${next.last_error_code}。`:'')+next.next_action);}
  async function send(action,extra={}){
    const id=pid,result=await pp.api(base()+'/'+action,{method:'POST',body:JSON.stringify({request_id:crypto.randomUUID(),expected_version:view.version,...extra})});
    if(id!==pid)throw new Error('论文已切换；操作结果保存在原论文，请返回核对。');
    return result;
  }
  async function perform(action,extra={},success='操作已保存。',after=null){
    if(busy||!view)return;busy=true;controls();const id=pid;
    try{await send(action,extra);if(id!==pid)return;if(after)after();await refresh();status(success+' '+view.next_action);}
    catch(e){if(id===pid)status(message(e)+' 当前输入已保留；若版本冲突，请刷新状态并核对后再次操作。',true);}
    finally{busy=false;controls();}
  }
  $('#learningEnable').onclick=()=>{
    if(!$('#learningAuthorized').checked){status('请先明确勾选授权，并核对采集范围和期限。',true);return;}
    const scopes=[...document.querySelectorAll('[name=learningScope]:checked')].map(x=>x.value),days=Number($('#learningRetention').value);
    if(!scopes.length||days<1||days>365||!Number.isInteger(days)){status('请选择至少一种样本，保留期限为1～365天。',true);return;}
    perform('consent',{authorized:true,scopes,retention_days:days},'已启用当前论文学习。',()=>{$('#learningAuthorized').checked=false;});
  };
  $('#learningRefresh').onclick=()=>refresh(true).catch(e=>status(message(e),true));
  $('#learningPreview').onclick=async()=>{const id=pid,version=view.version;try{const snapshot=await pp.api(`/api/projects/${id}/ai-context`);if(id===pid&&version===view.version)$('#learningContextPreview').textContent=snapshot.preview;}catch(e){if(id===pid)status(message(e),true);}};
  for(const action of ['pause','resume','disable','distill'])$('#learning'+({pause:'Pause',resume:'Resume',disable:'Disable',distill:'Distill'}[action])).onclick=()=>perform(action);
  $('#learningSignalKind').onchange=controls;
  $('#learningAddSignal').onclick=()=>{const content=$('#learningSignalText').value;if(!content.trim()){status('请填写示例或明确反馈。',true);return;}perform('signals',{kind:$('#learningSignalKind').value,content,source_version:'author:'+crypto.randomUUID()},'样本已保存，后台将自动整理；候选仍需批准。',()=>{$('#learningSignalText').value='';});};
  $('#learningRules').onchange=e=>{const id=e.target.dataset.selectRule;if(id){if(e.target.checked)selected.add(id);else selected.delete(id);controls();}};
  $('#learningRules').oninput=e=>{if(e.target.dataset.editRule)edits.set(e.target.dataset.editRule,e.target.value);};
  $('#learningRules').onclick=e=>{const edit=e.target.dataset.saveRule,remove=e.target.dataset.removeRule;if(edit)perform(`rules/${edit}/edit`,{instruction:edits.get(edit)??view.rules.find(r=>r.id===edit).instruction},'新候选已保存，批准前不生效。');if(remove)perform(`rules/${remove}/remove`);};
  $('#learningApprove').onclick=()=>perform('approve',{rule_ids:[...selected]},'批准完成，可在写作上下文预览中核对档案与来源。',()=>selected.clear());
  $('#learningHistory').onchange=controls;
  $('#learningRollback').onclick=()=>perform('rollback',{target_profile_id:$('#learningHistory').value});
  $('#learningSignals').onclick=e=>{if(e.target.dataset.deleteSignal)perform('delete',{confirmation:'删除学习数据',signal_id:e.target.dataset.deleteSignal},'已删除此学习副本及依赖规则；历史经验上下文已清除，相关旧候选失效。');};
  $('#learningDeleteConfirmed').onchange=controls;
  $('#learningDeleteAll').onclick=()=>{if($('#learningDeleteConfirmed').checked)perform('delete',{confirmation:'删除学习数据'},'全部学习副本已删除，学习已关闭。',()=>{$('#learningDeleteConfirmed').checked=false;});};
  setInterval(async()=>{
    const nextPid=pp.current?.project.id;
    if(!nextPid||busy||loading)return;
    const switched=nextPid!==pid;
    if(!switched&&(!$('#styleLearningPanel').open||Date.now()-lastPoll<1600))return;
    loading=true;lastPoll=Date.now();
    if(switched){if(pid)drafts.set(pid,{text:$('#learningSignalText').value,edits:new Map(edits)});pid=nextPid;view=null;signature='';selected.clear();edits=drafts.get(pid)?.edits||new Map();$('#learningBody').hidden=true;$('#learningSignalText').value=drafts.get(pid)?.text||'';$('#learningContextPreview').textContent='';$('#learningAuthorized').checked=false;$('#learningDeleteConfirmed').checked=false;$('#learningConsentForm').open=false;status('正在读取当前论文学习状态…');}
    try{await refresh(switched);if(switched){$('#learningConsentForm').open=view.state==='disabled';$('#learningRetention').value=view.retention_days;document.querySelectorAll('[name=learningScope]').forEach(x=>{x.checked=view.scopes.length?view.scopes.includes(x.value):['accepted_edit','example','feedback'].includes(x.value);});}}
    catch(e){status(message(e)+' 请刷新重试，当前输入保留。',true);}
    finally{loading=false;}
  },400);
})();
