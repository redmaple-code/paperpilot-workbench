/* Persistent, project-owned chat. The old panel entry boots this module. */
(() => {
  const pp = window.paperpilot, store = window.paperpilotAgentStore;
  if (!pp || !store) return;
  const $ = s => document.querySelector(s);
  const node = (tag, text, cls) => { const n = document.createElement(tag); if (text) n.textContent = text; if (cls) n.className = cls; return n; };
  const renderers = new Map();
  pp.agentToolsChanged = () => document.dispatchEvent(new CustomEvent('paperpilot:agent-tools-changed'));
  pp.agentCards = { register(kind, renderer) { if (renderers.has(kind)) throw new Error(`重复卡片类型：${kind}`); renderers.set(kind, renderer); } };
  (pp.agentCardQueue || []).forEach(([kind, renderer]) => pp.agentCards.register(kind, renderer));
  pp.agentCardQueue = [];

  pp.startAgentConversationUI = function () {
    const assistant = $('#workspaceAssistant');
    if (!assistant || $('#agentChat')) return;
    const shell = document.createElement('section'); shell.id = 'agentChat'; shell.className = 'agent-chat';
    shell.innerHTML = `<nav id="agentRoutes" aria-label="论文 Agent 对话"></nav>
      <details id="agentOtherConversations" hidden><summary>其他已有讨论</summary><select id="agentConversation" aria-label="当前论文会话"></select></details>
      <div class="agent-chat-head"><div><span class="agent-role-label" id="agentRoleLabel">论文总 Agent</span><h3 id="agentActiveTitle">论文总对话</h3></div><button id="agentSettingsToggle" type="button" aria-expanded="false" aria-controls="agentSettings">对话设置</button></div>
      <p id="agentRoleDescription" class="agent-role-description"></p>
      <button id="agentOlder" type="button" hidden>加载更早消息</button>
      <ol id="agentThread" class="agent-thread" aria-label="会话消息"></ol>
      <p id="agentRunStatus" class="agent-note" role="status" aria-live="polite"></p>
      <section id="agentSettings" aria-label="全局 Agent 运行设置" hidden>
        <button id="agentConfigureModel" type="button">在本页配置模型</button>
        <details id="agentPreview"><summary>发送范围与预算</summary><div id="agentPreviewBody"></div></details>
        <label class="agent-consent"><input type="checkbox" id="agentConsent">确认当前对话的接收方、范围与预算</label>
        <label class="agent-field"><span>正文发送范围</span><select id="agentScope"><option value="excerpt">选区与附近正文</option><option value="full">当前文件全文</option></select></label>
        <details id="agentTrace"><summary>本轮操作记录</summary><ol id="agentEvents"></ol></details>
        <details id="agentMemoryPanel"><summary>论文长期记忆</summary><div id="agentMemories"></div></details>
      </section>
      <div class="agent-compose"><label class="agent-field" for="agentInput">告诉 Agent 你要做什么</label>
        <textarea id="agentInput" maxlength="4000" rows="3" placeholder="讨论这篇论文的目标、进度，或安排下一步工作"></textarea>
        <span id="agentContextChip" class="agent-chip"></span>
        <div class="agent-compose-actions"><button id="agentSend" class="primary" type="button">发送</button><button id="agentStop" type="button" hidden>停止</button><button id="agentResume" type="button" hidden>预览并续跑</button></div></div>`;
    assistant.querySelector('.ws-assistant-heading').after(shell);
    const legacyNav = assistant.querySelector('.ws-subnav');
    if (legacyNav && !$('#assistLegacy')) {
      const legacy = node('details'); legacy.id = 'assistLegacy'; legacy.append(node('summary', '高级操作（续写 / 指定修改 / 恢复）'));
      $('#agentSettings').append(legacy); legacy.append(legacyNav); assistant.querySelectorAll('.ws-assist-page').forEach(n => legacy.append(n));
    }
    let state = { pid: '', cid: '', epoch: 0, run: null, cursor: 0, revision: 0, busy: false, firstSeq: 0 };
    let timer = null, authority = '', latestPreview = null, decisions = {};
    let previewSequence = 0, workspaceData = null, conversationList = [];
    const selectedConversation = () => conversationList.find(item => item.id === state.cid);
    const settings = open => {
      $('#agentSettings').hidden = !open;
      $('#agentSettingsToggle').setAttribute('aria-expanded', String(open));
      document.dispatchEvent(new CustomEvent('paperpilot:agent-settings', { detail: { open } }));
    };
    pp.openAgentSettings = () => settings(true);
    $('#agentSettingsToggle').onclick = () => settings($('#agentSettings').hidden);
    $('#agentConfigureModel').onclick = () => pp.openAgentModelSettings?.();
    const researchRequest = () => selectedConversation()?.module_key === 'research'
      ? { stage_id: 'STAGE-01', research_profile: 'scholar_apprentice' } : {};
    const base = () => `/api/projects/${encodeURIComponent(state.pid)}/agent`;
    const alive = (epoch, pid, cid) => state.epoch === epoch && state.pid === pid && state.cid === cid && pp.current?.project?.id === pid;
    const call = async (path, body) => {
      const response = await fetch(path, body === undefined ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
      const value = await response.json();
      if (!response.ok) { const e = new Error(value.detail?.message || value.detail || '请求未完成'); e.status = response.status; e.code = value.detail?.code; throw e; }
      return value;
    };
    const note = text => { $('#agentRunStatus').textContent = text; };
    function persistInput() { if (state.pid && state.cid) store.write(state.pid, state.cid, 'draft', $('#agentInput').value); }
    function stopPoll() { if (timer) clearTimeout(timer); timer = null; }
    function controls() {
      const readOnly = !!pp.current?.project?.archived || pp.current?.project?.lifecycle_state === 'published';
      $('#agentSend').disabled = !state.cid || readOnly || (state.busy && !state.run) || !!state.submitting;
      $('#agentSend').setAttribute('aria-label', state.busy ? '补充要求' : '发送');
      $('#agentSend').title = state.busy ? '补充要求（Ctrl+Enter）' : '发送（Ctrl+Enter）';
      const canStop = state.busy && state.run && ['queued', 'running', 'awaiting_human'].includes(state.run.status);
      $('#agentStop').hidden = !canStop; $('#agentStop').disabled = !canStop;
      $('#agentResume').hidden = !state.run || !['failed', 'blocked', 'interrupted'].includes(state.run.status);
    }
    function contextNow() {
      const editor = $('#editor'), value = editor?.value || '';
      return { project_id: pp.current?.project?.id || '', file_path: $('#fileSelect')?.value || '', value,
        selection_start: editor?.selectionStart || 0, selection_end: editor?.selectionEnd || 0 };
    }
    async function snapshot() {
      const ctx = contextNow(), scope = $('#agentScope').value;
      if (selectedConversation()?.module_key === 'research') return { project_id: ctx.project_id, scope: 'excerpt' };
      if (scope === 'full' && ctx.value.length > 32000) throw new Error('当前全文超过本轮容量，请改用相关正文或拆分任务；全文未被静默截断。');
      const selected = ctx.value.slice(ctx.selection_start, ctx.selection_end);
      if (selected.length > 8000) throw new Error('选区超过8000字，请缩小选区后发送。');
      return { project_id: ctx.project_id, file_path: ctx.file_path, file_hash: await store.hash(ctx.value),
        selection_start: ctx.selection_start, selection_end: ctx.selection_end, selection_hash: await store.hash(selected),
        selected_text: selected, scope,
        excerpt: scope === 'full' ? ctx.value : ctx.value.slice(Math.max(0, ctx.selection_start - 2000), Math.min(ctx.value.length, Math.max(ctx.selection_end, ctx.selection_start) + 3000)) };
    }
    function chip() {
      if (document.body.classList.contains('conversation-first') && !document.querySelector('.cf-artifact-open')) { $('#agentContextChip').textContent = 'Ctrl+Enter 发送'; return; }
      const ctx = contextNow(); $('#agentContextChip').textContent = `${pp.current?.project?.name || '未选择论文'} · ${selectedConversation()?.title || '论文对话'}${selectedConversation()?.module_key === 'research' ? '' : ' · ' + (ctx.file_path || '无正文选区')}`;
    }
    function showPreview(preview) {
      latestPreview = preview;
      const host = $('#agentPreviewBody'); host.replaceChildren();
      const research = preview.context_manifest.research_profile === 'scholar_apprentice';
      const scope = preview.context_manifest.scope === 'result-library' ? '结果库的匿名表头结构、算例和来源关联'
        : research ? '阶段草稿、当前会话、论文结构与研究图'
        : (preview.context_manifest.scope === 'full' ? '全文' : '相关正文') + ' ' + preview.context_manifest.excerpt_characters + ' 字';
      host.append(node('p', `接收方：${preview.provider?.model || '未配置'} ${preview.provider?.base_url || ''}；${scope}`));
      host.append(node('p', `本轮上限：${preview.limits.model_calls}次模型调用 / ${preview.limits.tokens} token / ${preview.limits.seconds}秒。实际金额取决于提供方定价。`));
      if (research) {
        const details = node('details'); details.append(node('summary', '查看本轮研究材料'),
          node('pre', JSON.stringify(preview.research_context, null, 2))); host.append(details);
      }
      if (preview.context_manifest.scope !== 'result-library' && preview.context_manifest.agent_identity?.module_key === 'paper') host.append(node('p', '论文总对话可使用本论文的研究图、方案、已授权资料和当前正文范围。'));
      const related = preview.context_manifest.related_conversations || [];
      if (related.length) host.append(node('p', '关联上下文 · ' + related.map(item => item.title).join('、') + '的近期进展摘要'));
      preview.context_manifest.omitted.forEach(text => host.append(node('p', text, 'agent-note')));
      const attachments=preview.context_manifest.attachments||[];
      if(attachments.length)host.append(node('p','当前对话附件 '+attachments.map(item=>`${item.filename}（${item.characters}字）`).join('、')));
      if (!preview.authorization_required) $('#agentConsent').checked = true;
      else if (authority !== preview.authorization_fingerprint) $('#agentConsent').checked = false;
      $('#agentConsent').disabled = false;
      document.dispatchEvent(new CustomEvent('paperpilot:agent-preview', { detail: { projectId: state.pid, preview } }));
    }
    async function previewCurrent() {
      const { epoch, pid, cid } = state; if (!cid) return;
      const sequence = ++previewSequence;
      try {
        const body = { conversation_id: cid, instruction: $('#agentInput').value.trim(), context_snapshot: await snapshot(), ...researchRequest() };
        const preview = await call(base() + '/preview', body);
        if (alive(epoch, pid, cid) && sequence === previewSequence) showPreview(preview);
      } catch (e) { if (alive(epoch, pid, cid) && sequence === previewSequence) note(e.message); }
    }
    $('#agentConsent').onchange = () => { authority = $('#agentConsent').checked ? latestPreview?.authorization_fingerprint || '' : ''; };
    $('#agentScope').onchange = () => { authority = ''; $('#agentConsent').checked = false; previewCurrent(); };

    async function decide(card, action, target) {
      return call(`/api/projects/${encodeURIComponent(card.project_id)}/agent/conversations/${encodeURIComponent(card.conversation_id)}/cards/${encodeURIComponent(card.id)}/decision`, { action, file_path: target?.file_path || '', target_hash: target?.file_hash || '' });
    }
    async function applyDraft(card, replace, button) {
      const { epoch, pid, cid } = state;
      if (card.project_id !== pid || card.conversation_id !== cid) return note('这条候选属于另一篇论文或会话。');
      button.disabled = true;
      try {
        const current = contextNow(), digest = await store.hash(current.value), target = card.target || {};
        if (!alive(epoch, pid, cid)) return;
        const conflict = store.targetConflict(target, current, digest);
        if (conflict) {
          if (target.project_id !== current.project_id || target.file_path !== current.file_path) return note(conflict + '，原内容未改变。');
          await relocate(card, replace, current, digest); return;
        }
        const start = target.selection_start || 0, end = replace ? target.selection_end || start : start;
        await insert(card, start, end, current, digest, epoch, pid, cid);
        button.disabled = true;
      } catch (e) { note(e.message); } finally { if (alive(epoch, pid, cid) && !decisions[card.id]) button.disabled = false; }
    }
    async function insert(card, start, end, current, digest, epoch, pid, cid) {
      const editor = $('#editor');
      if (!alive(epoch, pid, cid) || contextNow().file_path !== current.file_path || editor.value !== current.value) throw new Error('目标在应用前发生变化，未写入。');
      // Record the user's explicit decision first; a network failure cannot silently alter the editor.
      await decide(card, 'accepted', { file_path: current.file_path, file_hash: digest });
      if (!alive(epoch, pid, cid) || editor.value !== current.value || contextNow().file_path !== current.file_path) throw new Error('应用确认后目标变化，未插入；请核对候选决定记录。');
      editor.value = current.value.slice(0, start) + card.text + current.value.slice(end);
      editor.setSelectionRange(start + card.text.length, start + card.text.length);
      editor.dispatchEvent(new Event('input', { bubbles: true })); editor.focus(); decisions[card.id] = 'applied';
      document.dispatchEvent(new CustomEvent('paperpilot:draft-applied', { detail: { projectId: pid, filePath: current.file_path } }));
      document.querySelectorAll(`[data-agent-card="${CSS.escape(card.id)}"] button`).forEach(b => b.disabled = true);
      note('已插入正文，正在自动保存。');
      try { await decide(card, 'applied', { file_path: current.file_path, file_hash: digest }); }
      catch { note('已插入草稿，但应用回执未完成；请先核对编辑器内容，不要重复插入。'); }
    }
    async function relocate(card, replace, current, digest) {
      const { epoch, pid, cid } = state, dialog = node('dialog'); dialog.className = 'agent-compare-dialog';
      dialog.append(node('h3', '目标内容已变化'), node('p', `原目标：${card.target.file_path}。比较后可以按当前选区重新定位，或取消。`),
        node('h4', '当前选区'), node('pre', current.value.slice(current.selection_start, current.selection_end) || '当前光标处'), node('h4', '候选草稿'), node('pre', card.text));
      const apply = node('button', replace ? '替换当前选区' : '插入当前光标'), cancel = node('button', '取消'); apply.type = cancel.type = 'button';
      dialog.append(apply, cancel); document.body.append(dialog);
      const close = () => { dialog.close(); dialog.remove(); };
      cancel.onclick = close; dialog.addEventListener('cancel', close, { once: true });
      apply.onclick = async () => { apply.disabled = true; try { await insert(card, current.selection_start, replace ? current.selection_end : current.selection_start, current, digest, epoch, pid, cid); close(); } catch (e) { note(e.message); close(); } };
      dialog.showModal();
    }
    function renderCard(card) {
      if (renderers.has(card.kind)) return renderers.get(card.kind)(Object.freeze({ ...card }), { project_id: state.pid, conversation_id: state.cid });
      const box = node('section', '', 'agent-card'); box.dataset.agentCard = card.id;
      if (card.kind === 'draft') {
        box.append(node('pre', card.text, 'agent-draft-text'), node('small', `目标：${card.target?.file_path || '未指定文件'} · 草稿尚未保存`));
        const actions = node('div', '', 'agent-draft-actions');
        for (const [label, replace] of [['插入原位置', false], ['替换原选区', true]]) {
          const button = node('button', label); button.type = 'button'; button.disabled = ['applied', 'rejected'].includes(decisions[card.id]);
          button.onclick = () => applyDraft(card, replace, button); actions.append(button);
        }
        const reject = node('button', '不采纳'); reject.type = 'button'; reject.disabled = !!decisions[card.id];
        reject.onclick = async () => { try { await decide(card, 'rejected', card.target); decisions[card.id] = 'rejected'; box.querySelectorAll('button').forEach(b => b.disabled = true); note('已记录不采纳，正文未改变。'); } catch (e) { note(e.message); } }; actions.append(reject); box.append(actions);
      } else if (card.kind === 'outline_proposal') {
        box.append(node('strong', '大纲修改提案（尚未应用）'), node('p', `${card.operation?.command || ''} · ${card.operation?.node?.title || card.operation?.node_id || ''}`), node('small', '可在大纲中核对；统一应用动作由功能整合包接入。'));
      } else box.append(node('strong', card.title || card.idea || card.gap || '研究候选'), node('p', card.summary || card.limitations || '供作者核对，不代表已验证结论。'));
      return box;
    }
    function renderMessages(result, prepend = false) {
      decisions = { ...decisions, ...(result.decisions || {}) }; state.revision = result.conversation.revision;
      const conversation=conversationList.find(item=>item.id===state.cid);
      if(conversation)Object.assign(conversation,result.conversation);
      renderRoutes();
      const list = $('#agentThread'), nearEnd = list.scrollHeight - list.scrollTop - list.clientHeight < 70;
      const fragment = document.createDocumentFragment();
      for (const message of result.messages) {
        if (list.querySelector(`[data-message="${CSS.escape(message.id)}"]`)) continue;
        const item = node('li', '', `agent-msg ${message.role}`); item.dataset.message = message.id;
        item.append(node('span', message.role === 'user' ? '你' : selectedConversation()?.title || '助手', 'agent-msg-role'));
        if (message.text) item.append(node('p', message.text, 'agent-visible-text'));
        message.cards.forEach(card => item.append(renderCard(card))); fragment.append(item);
      }
      if (prepend) list.prepend(fragment); else list.append(fragment);
      const seq = result.messages[0]?.seq; if (seq) state.firstSeq = state.firstSeq ? Math.min(state.firstSeq, seq) : seq;
      $('#agentOlder').hidden = !result.has_previous;
      if (!prepend && nearEnd) list.scrollTop = list.scrollHeight;
    }
    async function loadMessages() {
      const { epoch, pid, cid } = state, path = base();
      const [result, actions] = await Promise.all([
        call(path + `/conversations/${cid}/messages?tail=true`),
        call(path + '/actions?conversation_id=' + encodeURIComponent(cid)).catch(() => ({ cards: [] }))
      ]);
      if (!alive(epoch, pid, cid)) return null;
      const currentPlans = new Map(actions.cards.filter(card => card.kind === 'planning_proposal').map(card => [card.id, card]));
      result.messages.forEach(message => { message.cards = message.cards.map(card => currentPlans.get(card.id) || card); });
      renderMessages(result);
      return result;
    }
    function renderEvents(run) {
      for (const event of run.events || []) {
        if (event.seq <= state.cursor) continue;
        state.cursor = event.seq;
        const title = event.payload.tool || event.payload.node || event.kind;
        $('#agentEvents').append(node('li', `${title}${event.payload.error_code ? ' · ' + event.payload.error_code : ''}`));
        if (event.kind === 'tool_completed' && event.payload.tool === 'scholar_research')
          document.dispatchEvent(new CustomEvent('paperpilot:research-updated', { detail: { projectId: state.pid } }));
      }
    }
    async function poll(runId) {
      stopPoll(); const { epoch, pid, cid } = state, path = base();
      try {
        const run = await call(path + `/runs/${runId}?after_seq=${state.cursor}`);
        if (!alive(epoch, pid, cid) || state.observedRun !== runId) return;
        state.run = run; state.busy = ['queued', 'running', 'awaiting_human'].includes(run.status); renderEvents(run); controls();
        document.dispatchEvent(new CustomEvent('paperpilot:agent-run', { detail: { projectId: pid, conversationId: cid, run } }));
        const pendingSupplement=(run.steering||[]).some(item=>item.status==='not_received');
        note(state.busy ? `正在处理 · ${run.budget.model_calls||0}次模型调用 · ${run.budget.tool_calls||0}次工具调用` : `${({ succeeded: '本轮完成', cancelled: '已停止', blocked: '运行受限', interrupted: '运行已中断', failed: '运行未完成' })[run.status] || run.status}${run.blocked_reason ? '：' + run.blocked_reason : ''}${pendingSupplement?'。有补充要求未被本轮接收，请作为新消息继续。':''}`);
        if (state.busy) timer = setTimeout(() => poll(runId), 1000);
        else { await loadMessages(); await loadMemories(); }
      } catch (e) { if (alive(epoch, pid, cid) && state.observedRun === runId) { state.busy = !e.status; controls(); note(e.message + '；正在核对原运行，不会自动重新发送。'); if (!e.status) timer = setTimeout(() => poll(runId), 3000); } }
    }
    async function loadMemories() {
      const { epoch, pid, cid } = state, path = base();
      try {
        const result = await call(path + '/memories'); if (!alive(epoch, pid, cid)) return;
        const host = $('#agentMemories'); host.replaceChildren();
        for (const memory of result.memories.filter(m => ['pending', 'active'].includes(m.status))) {
          const item = node('section', '', 'agent-memory-item'); item.append(node('p', memory.title), node('small', memory.status === 'pending' ? '待采纳，尚未参与长期召回' : '已采纳，可从来源核对'), node('small', `来源运行：${memory.source_id}`));
          for (const [label, action] of memory.status === 'pending' ? [['采纳', 'approve'], ['拒绝', 'reject']] : [['删除', 'delete']]) {
            const b = node('button', label); b.type = 'button'; b.onclick = async () => { try { await call(path + `/memories/${memory.id}/decision`, { action }); if (alive(epoch, pid, cid)) loadMemories(); } catch (e) { if (alive(epoch, pid, cid)) note(e.message); } }; item.append(b);
          } host.append(item);
        }
        if (!host.children.length) host.append(node('p', '暂无待采纳记忆。'));
      } catch (e) { if (alive(epoch, pid, cid)) $('#agentMemories').textContent = e.message; }
    }
    function renderRoutes() {
      const modulesOpen=$('#agentRoutes .agent-module-routes')?.open||false;
      const host = $('#agentRoutes'); host.replaceChildren();
      if (!workspaceData?.root) return;
      const route = (conversation, parent) => {
        const button = node('button', conversation.title, 'agent-route'); button.type = 'button';
        button.dataset.conversationId = conversation.id; button.dataset.module = conversation.module_key;
        button.setAttribute('aria-pressed', String(conversation.id === state.cid));
        button.onclick = () => { $('#agentConversation').value = conversation.id; selectConversation(conversation.id); };
        parent.append(button);
      };
      route({ ...workspaceData.root, title: '论文对话' }, host);
      const modules = node('details', '', 'agent-module-routes');
      modules.open = modulesOpen;
      const summary = node('summary', '专题对话');
      const selected = selectedConversation();
      if (selected?.parent_id) summary.append(node('span', selected.title));
      modules.append(summary);
      const children = node('div', '', 'agent-module-list');
      workspaceData.children.filter(item=>item.revision>0||item.id===state.cid).forEach(item => route(item, children));
      const createTopic=node('button','新建专题讨论');createTopic.type='button';
      createTopic.onclick=()=>{
        const dialog=node('dialog','', 'paper-topic-dialog');
        const title=node('h2','新建专题讨论'),input=node('input');input.placeholder='例如 引言的论证结构';input.maxLength=80;input.setAttribute('aria-label','专题名称');
        const create=node('button','创建专题'),cancel=node('button','取消');create.type=cancel.type='button';
        cancel.onclick=()=>dialog.close();dialog.addEventListener('close',()=>dialog.remove(),{once:true});
        create.onclick=async()=>{
          if(!input.value.trim())return;
          const {epoch,pid,cid}=state;create.disabled=true;
          try{
            const topic=await call(base()+'/conversations',{title:input.value.trim(),parent_id:workspaceData.root.id});
            if(!alive(epoch,pid,cid)){dialog.close();return;}
            workspaceData.children.push(topic);conversationList.push(topic);
            const option=node('option',topic.title);option.value=topic.id;$('#agentConversation').append(option);dialog.close();selectConversation(topic.id);
          }catch(error){title.textContent=error.message;create.disabled=false;}
        };
        dialog.append(title,input,create,cancel);document.body.append(dialog);dialog.showModal();input.focus();
      };
      const specialist=node('details');specialist.append(node('summary','按工作内容讨论'));
      workspaceData.children.filter(item=>item.module_key).forEach(item=>route(item,specialist));
      children.append(createTopic,specialist);modules.append(children);host.append(modules);
    }
    async function selectConversation(cid) {
      persistInput(); stopPoll(); state = { ...state, cid, epoch: state.epoch + 1, run: null, busy: true, cursor: 0, firstSeq: 0 };
      const conversation = selectedConversation();
      pp.agentIdentity = conversation || null;
      $('#agentActiveTitle').textContent = conversation?.title || '论文讨论';
      $('#agentRoleLabel').textContent = conversation?.parent_id ? '专题讨论' : '论文总 Agent';
      $('#agentRoleDescription').textContent = conversation?.parent_id ? '围绕当前专题持续讨论，参考论文总对话目标。' : '统筹整篇论文，汇集各模块进展与下一步工作。';
      $('#agentInput').placeholder = conversation?.module_key === 'research' ? '说说研究兴趣、已有工作或当前困惑…' : conversation?.parent_id ? `与${conversation.title}讨论…` : '说说你的想法，或交给我一项论文任务…';
      $('#agentScope').closest('label').hidden = conversation?.module_key === 'research';
      renderRoutes(); chip(); settings(false);
      document.dispatchEvent(new CustomEvent('paperpilot:agent-selected', { detail: { conversation } }));
      authority = ''; latestPreview = null; decisions = {}; $('#agentConsent').checked = false;
      $('#agentConsent').disabled = true; $('#agentPreviewBody').replaceChildren();
      $('#agentThread').replaceChildren(); $('#agentEvents').replaceChildren(); $('#agentInput').value = store.read(state.pid, cid, 'draft');
      store.write(state.pid, '', 'selected', cid); controls(); note('正在恢复会话…');
      const { epoch, pid } = state;
      try {
        const result = await loadMessages(); if (!result || !alive(epoch, pid, cid)) return;
        let recovered = null;
        const pending = store.read(pid, cid, 'pending');
        if (pending) {
          const sent = JSON.parse(pending);
          const found = await call(base() + `/conversations/${cid}/requests/${encodeURIComponent(sent.request_id)}`);
          if (!alive(epoch, pid, cid)) return;
          recovered = found.run;
          if (recovered) {
            store.write(pid, cid, 'pending', '');
            if ($('#agentInput').value.trim() === sent.instruction) { $('#agentInput').value = ''; persistInput(); }
          }
        }
        state.busy = false; controls(); note('');
        const rid = recovered?.id || result.active_run?.id || result.messages.at(-1)?.run_id;
        if (rid) { state.observedRun = rid; poll(rid); }
        previewCurrent(); loadMemories();
      } catch (e) { if (alive(epoch, pid, cid)) { state.busy = false; controls(); note(e.message); } }
    }
    async function projectLoaded() {
      persistInput(); stopPoll(); state = { pid: pp.current?.project?.id || '', cid: '', epoch: state.epoch + 1, run: null, busy: false, cursor: 0, revision: 0, firstSeq: 0 };
      const { epoch, pid } = state; $('#agentThread').replaceChildren(); $('#agentConversation').replaceChildren(); controls(); chip();
      if (!pid) return;
      try {
        const readOnly = pp.current?.project?.archived || pp.current?.project?.lifecycle_state === 'published';
        const result = await call(base() + '/workspace', readOnly ? undefined : {});
        if (state.epoch !== epoch || state.pid !== pid) return;
        workspaceData = result;
        conversationList = [result.root, ...result.children, ...result.other_conversations].filter(Boolean);
        conversationList.forEach(c => { const option = node('option', c.title); option.value = c.id; $('#agentConversation').append(option); });
        $('#agentOtherConversations').hidden = !result.other_conversations.length;
        const selected = store.read(pid, '', 'selected'), cid = conversationList.find(c => c.id === selected)?.id || result.root?.id || conversationList[0]?.id;
        if (cid) { $('#agentConversation').value = cid; selectConversation(cid); } else note('此只读论文没有已保存会话。');
      } catch (e) { if (state.epoch === epoch) note(e.message); }
    }
    async function send() {
      if (!state.cid || state.submitting) return;
      const instruction = $('#agentInput').value.trim(); if (!instruction) return;
      if (state.busy && state.run) {
        const { epoch, pid, cid } = state;
        state.submitting = true; controls(); persistInput();
        try {
          const pending = store.read(pid, cid, 'steeringPending');
          const saved = pending ? JSON.parse(pending) : { request_id: store.uid(), text: instruction };
          if (saved.text !== instruction) throw new Error('上一条补充要求的结果待核对，请先恢复原输入再重试。');
          store.write(pid, cid, 'steeringPending', JSON.stringify(saved));
          const result = await call(base() + `/runs/${state.run.id}/steering`, saved);
          if (!alive(epoch,pid,cid)) return;
          store.write(pid,cid,'steeringPending','');
          if ($('#agentInput').value.trim() === instruction) $('#agentInput').value = '';
          persistInput(); note(result.status === 'received' ? '补充要求已接收。' : '补充要求已排队，将在下一安全检查点接收。');
          await loadMessages();
        } catch (e) { if (alive(epoch,pid,cid)) note(e.message); }
        finally { if (alive(epoch,pid,cid)) { state.submitting=false; controls(); } }
        return;
      }
      if (state.busy) return;
      const { epoch, pid, cid } = state, path = base(); persistInput(); state.busy = true; state.run = null; state.observedRun = ''; controls();
      try {
        if (pp.tryResultTurn) {
          const result = await pp.tryResultTurn({pid,cid,instruction,file_path:contextNow().file_path,signalAlive:()=>alive(epoch,pid,cid)});
          if (!alive(epoch,pid,cid)) return;
          if (result?.handled) {
            if (!result.message && $('#agentInput').value.trim() === instruction) $('#agentInput').value = '';
            persistInput(); state.busy = false; controls(); note(result.message || ''); await loadMessages(); return;
          }
        }
        if (pp.resolveAuthorDecision && await pp.resolveAuthorDecision(instruction)) {
          if (!alive(epoch,pid,cid)) return;
          if ($('#agentInput').value.trim() === instruction) $('#agentInput').value='';
          persistInput(); state.busy=false; controls(); await loadMessages(); return;
        }
        const context = await snapshot(); if (!alive(epoch, pid, cid)) return;
        const mode = researchRequest();
        const body = { conversation_id: cid, instruction, context_snapshot: context, ...mode };
        const preview = await call(path + '/preview', body); if (!alive(epoch, pid, cid)) return; showPreview(preview);
        if (JSON.stringify(mode) !== JSON.stringify(researchRequest())) throw new Error('当前研究范围已切换，请在当前页面核对后重新发送。输入已保留。');
        if (!preview.generation_allowed) { settings(true); throw new Error(preview.provider_error || '模型尚未就绪，请先完成配置。'); }
        if (preview.authorization_required && authority !== preview.authorization_fingerprint) {
          settings(true); $('#agentPreview').open = true; throw new Error('请核对接收方、范围与预算，勾选确认后再次发送。输入已保留。');
        }
        const pending = store.read(pid, cid, 'pending'); let requestId = store.uid();
        if (pending) { const old = JSON.parse(pending); if (old.instruction !== instruction || JSON.stringify(old.context_snapshot) !== JSON.stringify(context) || (old.research_profile || '') !== (body.research_profile || '')) throw new Error('上次发送结果未知。请先刷新核对会话后再发新任务。'); requestId = old.request_id; }
        const payload = { ...body, request_id: requestId, preview_fingerprint: preview.preview_fingerprint,
          consent_to_send: authority === preview.authorization_fingerprint, possible_cost_confirmed: authority === preview.authorization_fingerprint };
        store.write(pid, cid, 'pending', JSON.stringify(payload));
        const run = await call(path + '/runs', payload); if (!alive(epoch, pid, cid)) return;
        $('#agentPreview').open = false; settings(false);
        store.write(pid, cid, 'pending', ''); if ($('#agentInput').value.trim() === instruction) $('#agentInput').value = ''; persistInput();
        state.run = run; state.observedRun = run.id; state.cursor = 0; controls(); $('#agentEvents').replaceChildren(); await loadMessages(); poll(run.id);
      } catch (e) { if (alive(epoch, pid, cid)) { if (e.status && e.status < 500) store.write(pid, cid, 'pending', ''); state.busy = false; controls(); note(e.message); } }
    }
    $('#agentSend').onclick = send;
    $('#agentInput').oninput = persistInput;
    $('#agentInput').onkeydown = e => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); send(); } };
    $('#agentConversation').onchange = e => selectConversation(e.target.value);
    $('#agentStop').onclick = async () => { const { epoch, pid, cid, run } = state; if (!run) return; $('#agentStop').disabled = true; try { await call(base() + `/runs/${run.id}/cancel`, {}); if (alive(epoch, pid, cid)) poll(run.id); } catch (e) { if (alive(epoch, pid, cid)) { note(e.message); $('#agentStop').disabled = false; } } };
    $('#agentResume').onclick = async () => { const { epoch, pid, cid, run } = state; if (!run) return; try { const info = await call(base() + `/runs/${run.id}/resume/preview`, {}); if (!alive(epoch, pid, cid)) return; if (!window.confirm(info.note + '\n剩余预算仍沿用原运行，是否继续？')) return; await call(base() + `/runs/${run.id}/resume`, { request_id: store.uid(), preview_fingerprint: info.preview_fingerprint, consent_to_send: true, possible_cost_confirmed: true }); if (alive(epoch, pid, cid)) poll(run.id); } catch (e) { if (alive(epoch, pid, cid)) note(e.message); } };
    $('#agentOlder').onclick = async () => { const { epoch, pid, cid } = state; try { const result = await call(base() + `/conversations/${cid}/messages?before_seq=${state.firstSeq}`); if (alive(epoch, pid, cid)) renderMessages(result, true); } catch (e) { if (alive(epoch, pid, cid)) note(e.message); } };
    document.addEventListener('paperpilot:project-changed', () => { persistInput(); stopPoll(); state = { ...state, pid: '', cid: '', epoch: state.epoch + 1, run: null, busy: false }; latestPreview = null; authority = ''; workspaceData = null; conversationList = []; $('#agentPreviewBody').replaceChildren(); $('#agentConsent').checked = false; $('#agentConsent').disabled = true; $('#agentRoutes').replaceChildren(); $('#agentThread').replaceChildren(); $('#agentInput').value = ''; controls(); });
    document.addEventListener('paperpilot:project-loaded', projectLoaded);
    document.addEventListener('paperpilot:agent-tools-changed', () => { authority = ''; latestPreview = null; $('#agentConsent').checked = false; $('#agentConsent').disabled = true; $('#agentPreviewBody').replaceChildren(); previewCurrent(); });
    document.addEventListener('paperpilot:agent-context-changed', () => { authority = ''; latestPreview = null; $('#agentConsent').checked = false; $('#agentConsent').disabled = true; $('#agentPreviewBody').replaceChildren(); previewCurrent(); });
    document.addEventListener('paperpilot:view-changed', chip); $('#fileSelect')?.addEventListener('change', chip);
    document.addEventListener('paperpilot:author-decision-applied',loadMessages);
    pp.refreshAgentMessages = loadMessages;
    pp.openModuleAgent = async (moduleKey = 'paper', instruction = '') => {
      const conversation = conversationList.find(item => item.module_key === moduleKey);
      if (!conversation) return note('正在恢复论文对话，请稍后重试。');
      pp.showAgentSidebar?.();
      $('#agentConversation').value = conversation.id;
      if (state.cid !== conversation.id) await selectConversation(conversation.id);
      if (instruction && state.cid === conversation.id) {
        $('#agentInput').value = [$('#agentInput').value.trim(), instruction].filter(Boolean).join('\n'); persistInput();
      }
      $('#agentInput').focus();
    };
    window.addEventListener('beforeunload', persistInput); setInterval(chip, 2000); projectLoaded();
  };
})();
