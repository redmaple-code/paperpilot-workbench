(() => {
  const element = (tag, content) => {
    const node = document.createElement(tag);
    if (content !== undefined) node.textContent = content;
    return node;
  };
  const byId = id => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const project = params.get('project');
  const root = `/api/projects/${encodeURIComponent(project || '')}/innovations`;
  const home = `/?project=${encodeURIComponent(project || '')}#chat`;
  const options = { sources: [], support: [] };
  let list;
  let item;
  let busy = false;
  let dirty = false;
  let detailSequence = 0;

  const notice = text => { byId('notice').textContent = text; };
  const route = (id, version) => '/innovations?' + new URLSearchParams({
    project, ...(id && { point: id }), ...(version && { version })
  }).toString();

  async function call(path, data) {
    const response = await fetch(path, data === undefined ? undefined : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data)
    });
    const result = await response.json();
    if (!response.ok) {
      const error = new Error(result.detail?.message || (typeof result.detail === 'string' && result.detail)
        || (Array.isArray(result.detail) && '请核对输入字段和长度。') || '暂时无法完成操作。');
      error.code = result.detail?.code;
      throw error;
    }
    return result;
  }

  function button(text, action, emphasis = false) {
    const node = element('button', text);
    node.type = 'button';
    if (emphasis) node.className = 'emphasis';
    node.addEventListener('click', action);
    return node;
  }

  const link = (text, url) => {
    const node = element('a', text);
    node.href = url;
    return node;
  };

  async function run(action) {
    if (busy) return;
    busy = true;
    try {
      await action();
    } catch (error) {
      notice(error.message);
    } finally {
      busy = false;
    }
  }

  function renderList() {
    const host = byId('points');
    host.replaceChildren();
    const groups = [['主创新', list.primary ? [list.primary] : []], ['副创新', list.secondary], ['候选', list.drafts]];
    for (const [label, entries] of groups) {
      if (!entries.length) continue;
      host.append(element('h3', label));
      for (const entry of entries) {
        const title = entry.active?.claim || entry.draft?.claim || '未命名草稿';
        const node = button(title.slice(0, 64) + (entry.active ? ` · v${entry.active.version}` : ''), () => {
          if (!dirty || window.confirm('离开这条创新点并放弃未保存的编辑？')) open(entry.point_id);
        });
        node.setAttribute('aria-current', String(item?.point_id === entry.point_id));
        host.append(node);
      }
    }
    if (!host.childElementCount) host.append(element('p', '这篇论文还没有创新点。'));
  }

  function readOnlyBody(body) {
    const content = element('div');
    content.append(element('p', body.claim));
    for (const [label, value] of [['相对已有工作', body.difference], ['适用范围', body.scope]]) {
      if (value) content.append(element('h3', label), element('p', value));
    }
    renderSource(content, body);
    if (body.support.length) {
      content.append(element('h3', '工作支撑'));
      const list = element('ul');
      for (const work of body.support) {
        const row = element('li', [work.description, work.expected_output].filter(Boolean).join(' · '));
        if (!work.targets?.length) row.append(element('p', '待关联实际工作或产物'));
        for (const target of work.targets || []) {
          const label = target.label || target.id;
          const status = { done: '已完成', pending: '待处理', historical: '引用历史版本', gap: '论证有缺口', conflict: '论证有冲突' }[target.status] || '已关联';
          const line = element('p');
          line.append(target.url ? link(label, target.url) : element('span', label), document.createTextNode(' · ' + status));
          row.append(line);
        }
        list.append(row);
      }
      content.append(list);
    }
    return content;
  }

  function renderSource(host, body) {
    if (!body.source) return;
    const details = element('details');
    const source = body.source;
    details.append(element('summary', body.source_status && body.source_status !== 'current' ? '来源已变化，需重新定位' : '查看原文来源'),
      element('p', `${source.location} · 版本 ${source.version}`), element('blockquote', source.excerpt));
    host.append(details);
  }

  function textarea(label, value, limit = 4000) {
    const wrapper = element('label', label);
    const input = element('textarea');
    input.rows = 2;
    input.maxLength = limit;
    input.value = value || '';
    wrapper.append(input);
    return [wrapper, input];
  }

  function editor(host, entry, body) {
    dirty = false;
    const form = element('form');
    const claim = textarea('创新主张', body.claim);
    const difference = textarea('相对已有工作的差异', body.difference);
    const scope = textarea('适用范围', body.scope);
    form.append(claim[0], difference[0], scope[0]);
    renderSource(form, body);
    const keepSource = element('input');
    keepSource.type = 'checkbox';
    if (body.source && body.source_status !== 'current' && body.source_status) {
      const label = element('label');
      label.append(keepSource, document.createTextNode(' 以作者录入的主张保存'));
      form.append(label);
    }
    const work = element('div');
    const supportRows = [];
    const addSupport = (item = { refs: [] }) => {
      if (supportRows.length >= 25) return;
      const group = element('fieldset');
      const description = textarea('需要做什么', item.description, 1200);
      const expected = textarea('预期产物', item.expected_output, 1200);
      const select = element('select');
      select.multiple = true;
      select.size = 3;
      const related = new Map(options.support.map(ref => [JSON.stringify([ref.kind, ref.id, ref.version]), ref]));
      for (const ref of item.refs) {
        const key = JSON.stringify([ref.kind, ref.id, ref.version]);
        if (!related.has(key)) related.set(key, ref);
      }
      for (const [key, ref] of related) {
        const option = new Option(ref.label || ref.id, key);
        option.selected = item.refs.some(ref => JSON.stringify([ref.kind, ref.id, ref.version]) === key);
        select.add(option);
      }
      const relationship = element('label', '关联已有工作、章节、证据或结果');
      relationship.append(select);
      const row = { description: description[1], expected: expected[1], select };
      group.append(element('legend', '支撑工作'), description[0], expected[0], relationship,
        button('移除这一项', () => { supportRows.splice(supportRows.indexOf(row), 1); group.remove(); dirty = true; }));
      supportRows.push(row);
      work.append(group);
    };
    body.support.forEach(addSupport);
    const confirm = entry?.draft && button('确认此版本', () => {
      if (dirty) { notice('请先保存更新后的草稿，再确认。'); return; }
      run(() => mutate('confirm', entry));
    }, true);
    const actions = element('div');
    actions.className = 'actions';
    const save = element('button', '保存草稿');
    save.type = 'submit';
    actions.append(save);
    if (confirm) {
      confirm.disabled = !entry.draft.claim.trim() || ['changed', 'unavailable'].includes(body.source_status);
      actions.append(confirm);
    }
    form.append(work, button('添加支撑工作', () => { addSupport(); dirty = true; }), actions);
    form.addEventListener('input', () => { dirty = true; });
    form.addEventListener('submit', event => {
      event.preventDefault();
      run(() => mutate(entry ? 'save_draft' : 'create_draft', entry, {
        claim: claim[1].value.trim(), difference: difference[1].value.trim(), scope: scope[1].value.trim(),
        support: supportRows.filter(row => row.description.value.trim()).map(row => ({
          description: row.description.value.trim(), expected_output: row.expected.value.trim(),
          refs: Array.from(row.select.selectedOptions, choice => {
            const [kind, id, version] = JSON.parse(choice.value);
            return { kind, id, version };
          })
        }))
      }, keepSource.checked));
    });
    host.append(form);
    claim[1].focus();
  }

  function renderDetail() {
    const host = byId('detail');
    host.replaceChildren();
    if (!item) {
      host.append(element('p', '选择一条创新点查看主张、工作支撑与版本。'));
      return;
    }
    const title = item.role === 'primary' ? '主创新' : item.role === 'secondary' ? '副创新' : '候选';
    host.append(element('h2', item.selected ? `${title} · v${item.selected.version}` : title));
    if (item.selected) host.append(readOnlyBody(item.selected));
    if (!options.read_only && !params.get('version')) {
      if (item.draft) {
        host.append(element('h3', item.active ? '修订草稿' : '待确认草稿'));
        editor(host, item, item.draft);
      } else if (item.active) {
        const actions = element('div');
        actions.className = 'actions';
        actions.append(button('起草修订', () => {
          actions.remove();
          editor(host, item, item.active);
        }));
        if (item.role === 'secondary') actions.append(button('设为主创新', () => run(() => mutate('set_primary', item))));
        host.append(actions);
      }
    }
    if (item.history.length) {
      const history = element('details');
      history.append(element('summary', '版本历史'));
      const entries = element('ul');
      for (const entry of item.history) {
        const row = element('li');
        row.append(link(`v${entry.version}`, route(item.point_id, entry.version)));
        entries.append(row);
      }
      history.append(entries);
      if (params.get('version')) history.append(link('返回当前版本', route(item.point_id)));
      host.append(history);
    }
    if (item.usages.length) {
      const usage = element('details');
      usage.open = true;
      usage.append(element('summary', '下游引用'));
      const entries = element('ul');
      for (const ref of item.usages) {
        const row = element('li');
        const label = `${ref.label} · 创新点 v${ref.version}${ref.needs_review ? ' · 待复核' : ''}`;
        row.append(ref.url ? link(label, ref.url) : element('span', label));
        if (ref.version_url) row.append(document.createTextNode(' · '), link('版本记录', ref.version_url));
        entries.append(row);
      }
      usage.append(entries);
      host.append(usage);
    }
  }

  async function open(id, version, replace = true) {
    const sequence = ++detailSequence;
    try {
      const next = await call(`${root}/${encodeURIComponent(id)}${version ? '?version=' + encodeURIComponent(version) : ''}`);
      if (sequence !== detailSequence) return;
      if (version) params.set('version', version);
      else params.delete('version');
      item = next;
      dirty = false;
      if (replace) history.replaceState(null, '', route(id, version));
      renderList();
      renderDetail();
    } catch (error) {
      if (sequence === detailSequence) notice(error.message);
    }
  }

  async function mutate(action, entry, body, detachSource = false) {
    let result;
    try {
      result = await call(root + '/commands', { request_id: crypto.randomUUID(), action,
        point_id: entry?.point_id, draft_revision: entry?.draft_revision, active_revision: list.active_revision,
        ...(body && { body, detach_source: detachSource }) });
    } catch (error) {
      if (entry && ['innovation_draft_stale', 'innovation_set_stale'].includes(error.code)) {
        await compareConflict(entry);
      }
      throw error;
    }
    list = await call(root);
    await open(result.point_id);
    notice({ create_draft: '草稿已保存。', save_draft: '草稿已保存。', confirm: '创新点已确认。', set_primary: '主创新已更新。' }[action]);
  }

  async function compareConflict(entry) {
    const [latest, current] = await Promise.all([
      call(root + '/' + encodeURIComponent(entry.point_id)), call(root)
    ]);
    if (item?.point_id !== entry.point_id) return;
    document.querySelector('.revision-conflict')?.remove();
    const box = element('details');
    box.className = 'revision-conflict';
    box.open = true;
    box.append(element('summary', '对照当前版本'), element('blockquote', latest.draft?.claim || latest.active?.claim || ''));
    if (latest.draft_revision === entry.draft_revision || dirty) {
      box.append(button('用当前版本作为基线', () => {
        entry.draft_revision = latest.draft_revision;
        list = current;
        renderList();
        box.remove();
        notice('已对齐版本，请复核当前编辑后重新提交。');
      }));
    } else {
      box.append(button('打开新版草稿', () => { list = current; open(entry.point_id); }));
    }
    byId('detail').insertBefore(box, byId('detail').children[1] || null);
  }

  async function previewSource() {
    const source = options.sources[Number(byId('source').value)];
    if (!source) return;
    const result = await call(root + '/extractions/preview', { kind: source.kind, id: source.id });
    const host = byId('extraction');
    host.replaceChildren();
    if (!result.candidates.length) { host.append(element('p', '这份材料中没有可定位的候选，可手工录入。')); return; }
    for (const candidate of result.candidates) {
      const section = element('fieldset');
      section.append(element('legend', candidate.source.location), element('blockquote', candidate.claim));
      const adopt = mode => run(async () => {
        if (dirty) { notice('请先保存正在编辑的草稿。'); return; }
        const saved = await call(root + '/extractions', { request_id: crypto.randomUUID(),
          kind: result.kind, id: result.id, fingerprint: result.fingerprint, keys: [candidate.key],
          ...(mode === 'relocate' && { point_id: item.point_id, draft_revision: item.draft_revision }) });
        list = await call(root);
        await open(saved.point_ids[0]);
        notice(mode === 'relocate' ? '原文来源已重新定位。' : '已加入候选，请审阅后确认。');
      });
      section.append(button('作为候选', () => adopt('create')));
      if (item?.draft?.source) section.append(button('作为当前草稿的来源', () => adopt('relocate')));
      host.append(section);
    }
  }

  async function initialize() {
    const papers = await call('/api/projects');
    const select = byId('paper');
    select.add(new Option('选择论文', ''));
    for (const paper of papers.projects || papers) select.add(new Option(paper.name, paper.id));
    select.value = project || '';
    select.addEventListener('change', () => {
      if (dirty && !window.confirm('切换论文并放弃未保存的编辑？')) { select.value = project; return; }
      if (select.value) location.assign('/innovations?' + new URLSearchParams({ project: select.value }));
    });
    if (!project) { notice('请先选择要查看的论文。'); return; }
    byId('back').href = home;
    const [summary, choices] = await Promise.all([call(root), call(root + '/options')]);
    list = summary;
    Object.assign(options, choices);
    byId('create').disabled = options.read_only;
    byId('create').addEventListener('click', () => {
      if (dirty && !window.confirm('新建草稿并放弃未保存的编辑？')) return;
      item = null;
      renderList();
      const host = byId('detail');
      host.replaceChildren(element('h2', '新建草稿'));
      editor(host, null, { support: [] });
    });
    byId('extract').hidden = options.read_only;
    options.sources.forEach((source, index) => byId('source').add(new Option(source.label, String(index))));
    byId('source-preview').disabled = !options.sources.length;
    byId('source-preview').addEventListener('click', () => run(previewSource));
    if (options.read_only) notice('这篇论文已发布，可查看版本与来源。');
    if (params.get('point')) await open(params.get('point'), params.get('version'), false);
    else { renderList(); renderDetail(); }
  }

  initialize().catch(error => notice(error.message));
})();
