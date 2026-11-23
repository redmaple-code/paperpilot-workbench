/* The same candidate editor serves the stage panel and the conversation. */
(() => {
  const pp=window.paperpilot;if(!pp)return;
  const el=(tag,text)=>{const n=document.createElement(tag);if(text!==undefined)n.textContent=text;return n;};
  const uid=()=>crypto.randomUUID();
  const messages=new Set();
  function render(candidate,card,definitions){
    const pid=candidate.project_id||pp.current?.project?.id,root=el("article");root.className="rd-card";
    root.append(el("h4",card.title||card.idea||card.gap||"阶段候选"),el("p",card.limitations||"未查重，未验证；采纳后仍需审阅确认。"));
    const reportArea=el("section");
    function report(value){reportArea.textContent="";reportArea.append(el("p",value?`关键词粗筛：${value.ok?'覆盖有限':'服务不可用'} · 相似度 ${value.level||'未知'} · ${value.query||''} · ${value.queried_at||''}`:"尚未执行关键词粗筛；不能据此判断创新成立。"));for(const source of value?.sources||[])reportArea.append(el("p",[source.title,source.year,source.doi,source.provider].filter(Boolean).join(" · ")));}
    report(card.dedup_report);root.append(reportArea);
    const fields=definitions.find(d=>d.id===candidate.stage_id)?.fields||{};
    const select=el("select"),label=el("label","目标草稿字段");
    for(const key of Object.keys(card.payload||fields)){if(!(key in fields))continue;const option=el("option",fields[key]);option.value=key;select.append(option);}
    if(!card.payload&&fields.contribution)select.value="contribution";
    label.append(select);root.append(label);
    const editor=el("textarea");editor.className="rd-stage-payload";editor.maxLength=16000;editor.setAttribute("aria-label","可编辑候选草稿");
    function updateText(){editor.value=card.payload?.[select.value]||[card.idea,card.hypothesis,card.falsifiable_prediction,card.verification_plan,"（待核验候选，尚未查重，也未验证。）"].filter(Boolean).join("\n");}
    select.onchange=updateText;updateText();root.append(editor);
    if(candidate.stage_id==="STAGE-01"){
      const details=el("details");details.append(el("summary","假设与红队自评"));for(const [key,value] of Object.entries(card.red_team||{}))details.append(el("p",key+"："+value));
      for(const n of card.nearest_neighbors||[])details.append(el("p","待检索方向："+n.work+" · "+n.relation));root.append(details);
    }
    const queryLabel=el("label","关键词检索式（发送给学术检索来源）"),query=el("input");query.value=card.search_queries?.[0]||"";query.maxLength=240;queryLabel.append(query);root.append(queryLabel);
    const dedup=el("button","执行关键词粗筛"),preview=el("button","预览草稿应用"),reject=el("button","不采纳"),feedback=el("p");feedback.setAttribute("role","status");
    const reasonLabel=el("label","高相似候选采纳理由"),reason=el("textarea");reason.maxLength=2000;reasonLabel.append(reason);root.append(reasonLabel);
    const revisionLabel=el("label"),revision=el("input");revision.type="checkbox";revisionLabel.append(revision,document.createTextNode("如目标已确认，同意重新打开该阶段及下游确认；旧版本保留历史。"));root.append(revisionLabel);
    const resultHost=el("div");
    async function perform(fn){if(pp.current?.project?.id!==pid){feedback.textContent="请返回原论文后操作此候选。";return;}dedup.disabled=preview.disabled=true;try{await fn();}catch(e){try{feedback.textContent=JSON.parse(e.message).detail?.message||e.message;}catch{feedback.textContent=e.message;}}finally{dedup.disabled=false;preview.disabled=["applied","rejected"].includes(card.status);}}
    dedup.onclick=()=>perform(async()=>{const value=await pp.api(`/api/projects/${pid}/workflow/research-design/${candidate.id}/cards/${card.id}/dedup`,{method:"POST",body:JSON.stringify({query:query.value,consent_to_search:true})});if(pp.current?.project?.id===pid){report(value);feedback.textContent="粗筛已完成；来源覆盖和未验证项仍需核对。";}});
    preview.onclick=()=>perform(async()=>{
      const action=await pp.api(`/api/projects/${pid}/agent/actions`,{method:"POST",body:JSON.stringify({request_id:uid(),conversation_id:document.querySelector("#agentConversation")?.value||"",operation:"research",base_version:candidate.base_workflow_version,payload:{candidate_id:candidate.id,card_id:card.id,field:select.value}})});
      if(pp.current?.project?.id!==pid)return;
      action.selection={edited_text:editor.value,review_reason:reason.value,acknowledge_revision:revision.checked};
      const rendered=pp.agentBusiness.render(action);rendered.className="research-apply-preview";resultHost.replaceChildren(rendered);rendered.querySelector("button")?.click();
    });
    reject.onclick=()=>perform(async()=>{await pp.api(`/api/projects/${pid}/workflow/research-design/${candidate.id}/cards/${card.id}/reject`,{method:"POST",body:"{}"});if(pp.current?.project?.id===pid){preview.disabled=true;reject.disabled=true;feedback.textContent="已拒绝，阶段草稿未改变；刷新后仍保留处理记录。";card.status="rejected";}});
    for(const button of [dedup,preview,reject])button.type="button";
    const buttons=el("div");buttons.className="business-actions";buttons.append(dedup,preview,reject);root.append(buttons,feedback,resultHost);
    if(["applied","rejected"].includes(card.status)){preview.disabled=reject.disabled=true;feedback.textContent=card.status==="applied"?"已应用到阶段草稿；其它修改请基于当前版本重新生成。":"已拒绝，阶段草稿未改变。";}
    return root;
  }
  pp.agentResearch={render};
  document.addEventListener("paperpilot:research-candidates",event=>{
    const {candidate,definitions}=event.detail||{};if(!candidate||candidate.project_id!==pp.current?.project?.id)return;
    const thread=document.querySelector("#agentThread");if(!thread)return;
    for(const card of candidate.cards){const key=candidate.id+":"+card.id;if(messages.has(key))continue;messages.add(key);const li=el("li");li.className="agent-msg assistant business-message";li.dataset.businessProject=candidate.project_id;li.append(render(candidate,card,definitions));thread.append(li);}
  });
})();
