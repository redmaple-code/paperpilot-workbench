/* Business cards are immutable snapshots. Every click checks the original project. */
(() => {
  const pp=window.paperpilot;if(!pp)return;
  const base=pid=>`/api/projects/${pid}/agent`,uid=()=>crypto.randomUUID();
  const el=(tag,text)=>{const node=document.createElement(tag);if(text!==undefined)node.textContent=text;return node;};
  const errorText=error=>{try{return JSON.parse(error.message).detail?.message||error.message;}catch{return error.message;}};
  const labels={proposed:"待审阅",previewed:"已预览",applied:"已应用",rejected:"已拒绝"};
  const conversation=()=>document.querySelector("#agentConversation")?.value||"";
  function check(card){if(pp.current?.project?.id!==card.project_id||(card.conversation_id&&card.conversation_id!==conversation()))throw new Error("这张卡片属于另一篇论文或会话，请返回原目标后操作。");}
  function describe(diff){
    if(diff.fields)return diff.fields.map(field=>`${field.label}\n${field.before ? '原方案\n'+field.before+'\n' : ''}新方案\n${field.after}`).join('\n\n')+'\n\n'+diff.effect;
    if(diff.operations)return diff.operations.map((op,i)=>`${i+1}. ${{add_node:"新增",update_node:"修改",delete_node:"删除"}[op.op]||op.op} ${op.node?.title||op.changes?.title||op.id||"节点"}${op.changes?.purpose?'：'+op.changes.purpose:''}`).join("\n");
    if(diff.new_resource)return Object.values(diff.new_resource).join("\n")+"\n"+diff.effect+(diff.existing_same_name?.length?"\n已有同名定义：\n"+diff.existing_same_name.map(r=>r.definition).join("\n"):"");
    if(typeof diff.after==="string")return `原内容：\n${diff.before||"（空）"}\n\n候选内容：\n${diff.after}\n\n${diff.effect}`;
    if(diff.after?.nodes)return `节点：${diff.before.nodes.length} → ${diff.after.nodes.length}；关系：${diff.before.edges.length} → ${diff.after.edges.length}\n`+diff.after.nodes.map(n=>n.label).join("\n");
    return [diff.title,diff.doi,diff.access_status,diff.effect].filter(Boolean).join("\n");
  }
  function planFields(host, diff) {
    host.replaceChildren();
    const list = el('dl'); list.className = 'planning-fields';
    for (const field of diff.fields || []) {
      const group = el('div'); group.append(el('dt', field.label), el('dd', field.after));
      if (field.before && field.before !== field.after) {
        const previous = el('details'); previous.append(el('summary', '原方案'), el('p', field.before)); group.append(previous);
      }
      list.append(group);
    }
    host.append(list);
    if (diff.effect) host.append(el('p', diff.effect));
  }
  function render(card){
    const planning = card.kind === 'planning_proposal';
    const confirmsPlan=card.payload?.mode==='confirm'&&card.selection?.selected_indices==null;
    const completed = planning && card.status === 'applied';
    const node=el("article");node.className="business-card";node.dataset.businessCard=card.id;node.dataset.projectId=card.project_id;
    node.dataset.businessStatus = card.status;
    const title = completed ? (confirmsPlan ? '已确认的论文方案' : '已保存的讨论草稿') : card.title;
    node.append(el('h4', title));
    if (!completed && card.summary) node.append(el('p', card.summary));
    if (completed) {
      node.classList.add('planning-conversation-card', 'planning-completed');
      const content = el('details'); content.className = 'planning-record';
      content.append(el('summary', confirmsPlan ? '查看已确认方案' : '查看方案草稿'));
      const fields = el('div'); planFields(fields, { ...card.reviewed_plan, effect:'' }); content.append(fields);
      const proceed = el('button', '继续对话'); proceed.type = 'button';
      proceed.onclick = () => { try { check(card); document.querySelector('#agentInput').focus(); } catch (error) { proceed.textContent = errorText(error); } };
      node.append(content, proceed); return node;
    }
    const state=el("p",labels[card.status]||card.status||"待审阅");state.className="business-status";state.setAttribute("role","status");node.append(state);
    const feedback=el("p");feedback.setAttribute("role","status");feedback.className="business-feedback";
    if(card.metadata){node.append(el("p",[card.metadata.authors,card.metadata.year,card.metadata.provider,card.metadata.access_status].filter(Boolean).join(" · ")));}
    if(card.source_refs?.length)node.append(el("p",card.source_refs.map(s=>[s.provider,s.query,s.retrieved_at,s.id].filter(Boolean).join(" · ")).join("\n")));
    for(const limit of card.limitations||[])node.append(el("p",limit));
    const scope=el("fieldset");scope.append(el("legend","接受范围"));
    const operations=card.payload?.operations;
    if(operations)operations.forEach((op,i)=>{const label=el("label"),input=el("input");input.type="checkbox";input.checked=!card.selection?.selected_indices||card.selection.selected_indices.includes(i);input.value=i;label.append(input,document.createTextNode(op.node?.title||op.changes?.title||op.id||"结构操作"));scope.append(label);});
    if(operations)node.append(scope);
    const previewArea=el("section");previewArea.hidden=true;
    const diff=el(planning?"div":"pre"),controls=el("div"),buttons=el("div");buttons.className="business-actions";
    const previewButton=el("button","预览差异"),applyButton=el("button","明确应用"),rejectButton=el("button","拒绝候选");
    for(const button of [previewButton,applyButton,rejectButton])button.type="button";
    let snapshot=null,requestId=uid(),running=false,selectionRevision=0;
    if (planning) {
      previewButton.textContent = '审阅方案';
      applyButton.textContent = confirmsPlan ? '确认这份方案' : '采纳为讨论草稿';
      rejectButton.textContent = '继续讨论';
      node.classList.add('planning-conversation-card');
    }
    const done=["applied","rejected"].includes(card.status);
    let closed=done;
    previewButton.disabled=done;rejectButton.disabled=done;applyButton.disabled=true;
    async function perform(fn){
      if(running)return;
      running=true;previewButton.disabled=true;applyButton.disabled=true;rejectButton.disabled=true;
      try{check(card);await fn();}
      catch(error){snapshot=null;feedback.textContent=errorText(error);feedback.dataset.error="true";}
      finally{running=false;previewButton.disabled=closed;rejectButton.disabled=closed;applyButton.disabled=closed||!snapshot;}
    }
    previewButton.onclick=()=>perform(async()=>{
      const revision=selectionRevision;
      const selected_indices=operations?[...scope.querySelectorAll("input:checked")].map(i=>Number(i.value)):card.selection?.selected_indices ?? null;
      snapshot=await pp.api(base(card.project_id)+`/actions/${card.id}/preview`,{method:"POST",body:JSON.stringify({...card.selection,selected_indices})});
      if(revision!==selectionRevision){snapshot=null;feedback.textContent='接受范围已变化，请重新预览。';return;}
      check(card);previewArea.hidden=false;if(planning)planFields(diff,snapshot.diff);else diff.textContent=describe(snapshot.diff);controls.textContent="";
      node.dataset.previewFingerprint=snapshot.action_fingerprint;
      await pp.api(base(card.project_id)+`/actions/${card.id}/displayed`,{method:'POST',body:JSON.stringify({conversation_id:card.conversation_id,action_fingerprint:snapshot.action_fingerprint})});
      if(revision!==selectionRevision){snapshot=null;feedback.textContent='接受范围已变化，请重新预览。';return;}
      node.dataset.businessStatus='previewed';state.textContent='已预览';requestId=uid();feedback.textContent="请核对原目标、修改范围和差异；尚未应用。";
    });
    scope.onchange=()=>{selectionRevision++;snapshot=null;applyButton.disabled=true;previewArea.hidden=true;};
    applyButton.onclick=()=>perform(async()=>{
      if(!snapshot)return;
      const response=await pp.api(base(card.project_id)+`/actions/${card.id}/apply`,{method:"POST",body:JSON.stringify({request_id:requestId,expected_version:snapshot.base_version,action_fingerprint:snapshot.action_fingerprint,accepted:true})});
      check(card);
      closed=true;
      if (planning) {
        node.replaceWith(render({...card, status:'applied', reviewed_plan:snapshot.diff}));
      } else {
        state.textContent="已应用";applyButton.disabled=true;previewButton.disabled=true;rejectButton.disabled=true;
      }
      feedback.textContent=response.result?.message||"所选修改已应用；正文保存与阶段确认仍需单独执行。";
      document.dispatchEvent(new CustomEvent("paperpilot:resource-updated",{detail:response.event}));
      await pp.refreshWorkflow?.();
    });
    rejectButton.onclick=()=>perform(async()=>{await pp.api(base(card.project_id)+`/actions/${card.id}/reject`,{method:"POST",body:"{}"});check(card);closed=true;state.textContent="已拒绝";previewButton.disabled=true;applyButton.disabled=true;rejectButton.disabled=true;feedback.textContent="候选已拒绝，领域内容未改变。";});
    if(card.kind==="search_result"){
      for(const operation of ["reference","oa"]){
        if(operation==="oa"&&!card.metadata?.oa_url&&!card.metadata?.has_oa)continue;
        const button=el("button",operation==="reference"?"加入参考文献草稿":"预览全文导入");button.type="button";
        button.onclick=()=>perform(async()=>{check(card);const project=await pp.api(`/api/projects/${card.project_id}`);check(card);
          const action=await pp.api(base(card.project_id)+"/actions",{method:"POST",body:JSON.stringify({request_id:uid(),conversation_id:conversation(),operation,base_version:project.project.project_revision,payload:card.payload})});
          check(card);mount(action);feedback.textContent="已创建交接提案，请在下方预览并明确应用。";});buttons.append(button);
      }
    }else buttons.append(previewButton,rejectButton);
    previewArea.append(diff,controls,applyButton);node.append(buttons,previewArea,feedback);
    if(!done&&card.kind!=="search_result")queueMicrotask(()=>{if(node.isConnected)previewButton.click();});
    return node;
  }
  function mount(card){
    if(pp.current?.project?.id!==card.project_id||(card.conversation_id&&card.conversation_id!==conversation()))return;
    const host=document.querySelector("#agentThread");if(!host||[...host.querySelectorAll("[data-business-card]")].some(n=>n.dataset.businessCard===card.id))return;
    const li=el("li");li.className="agent-msg assistant business-message";li.dataset.businessProject=card.project_id;li.append(render(card));host.append(li);
  }
  pp.agentBusiness={render,mount};
  const kinds=["search_result","outline_proposal","logic_proposal","term_proposal","reference_candidate","research_candidate","planning_proposal"];
  for(const kind of kinds){if(pp.agentCards?.register)pp.agentCards.register(kind,render);else (pp.agentCardQueue??=[]).push([kind,render]);}
  async function refresh(){
    const pid=pp.current?.project?.id,cid=conversation();if(!pid||document.hidden)return;
    try{const [actions,searches]=await Promise.all([pp.api(base(pid)+"/actions?conversation_id="+encodeURIComponent(cid)),pp.api(base(pid)+"/business-search?conversation_id="+encodeURIComponent(cid))]);if(pp.current?.project?.id!==pid||conversation()!==cid)return;for(const card of [...actions.cards,...searches.cards]){
        const existing=[...document.querySelectorAll('[data-business-card]')].filter(node=>node.dataset.businessCard===card.id);
        if(['applied','rejected'].includes(card.status)&&existing.length){
          existing.filter(node=>node.dataset.businessStatus!==card.status).forEach(node=>node.replaceWith(render(card)));
        }else mount(card);
      }}
    catch{/* The active request surfaces its error; background refresh preserves existing cards. */}
  }
  document.addEventListener("paperpilot:project-loaded",()=>{document.querySelectorAll(".business-message").forEach(n=>{n.hidden=n.dataset.businessProject!==pp.current?.project?.id;});refresh();});
  document.addEventListener("paperpilot:resource-updated",async event=>{
    if(event.detail?.project_id!==pp.current?.project?.id)return;
    try {
      await pp.refreshResourceLists?.();
      if(event.detail.kind==="outline"){await pp.refreshStructuredOutline?.();await pp.refreshOutlineCanvas?.();}
    } catch(error) { pp.flash?.("修改已保存，列表刷新失败："+errorText(error)); }
  });
  setInterval(refresh,4000);
  refresh();
})();
