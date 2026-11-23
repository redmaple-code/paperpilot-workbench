/* Paper terminology review and versioned domain terminology. */
(() => {
  const pp = window.paperpilot;
  const tools = pp?.termComparisonView;
  const legacy = document.querySelector('#termList')?.closest('.card');
  if (!legacy || !tools) return;

  const host = document.createElement('section');
  host.id = 'termLibrary';
  host.innerHTML = '<div class="tl-header"><h2>术语库</h2><div data-tl="actions" class="tl-actions"></div></div>'
    + '<nav class="tl-scopes" aria-label="术语库作用域"></nav><div data-tl="context" class="tl-context"></div>'
    + '<p data-tl="status" class="tl-status" role="status" aria-live="polite"></p>'
    + '<section class="tl-surface"><nav data-tl="tabs" class="tl-tabs" aria-label="术语库视图"></nav>'
    + '<div class="tl-split"><section class="tl-queue"><div class="tl-controls">'
    + '<input type="search" aria-label="搜索术语" placeholder="搜索术语或别名"><div class="tl-filters"></div>'
    + '<div data-tl="referenceBatch" class="tl-actions tl-reference-batch"></div></div>'
    + '<div data-tl="headings" class="tl-columns"></div><ul data-tl="list" class="tl-list"></ul>'
    + '<div data-tl="count" class="tl-count"></div></section><section data-tl="detail" class="tl-detail" aria-label="术语详情"></section>'
    + '</div></section>';
  legacy.before(host);
  legacy.hidden = true;
  legacy.classList.add('tl-legacy');
  const fields = Object.fromEntries([...host.querySelectorAll('[data-tl]')].map(el => [el.dataset.tl, el]));
  const search = host.querySelector('input');
  const filters = host.querySelector('.tl-filters');
  const scopes = host.querySelector('.tl-scopes');
  const project = () => pp.data?.project;
  const apiPath = pid => '/api/projects/' + encodeURIComponent(pid);
  const domainPath = id => '/api/domains/' + encodeURIComponent(id);
  const post = (path, data, method = 'POST') => pp.api(path, {method, body: JSON.stringify(data)});
  const readOnly = paper => paper.archived || paper.lifecycle_state === 'published';
  const norm = value => String(value ?? '').normalize('NFKC').trim().toLowerCase();
  const closestName = (a, b) => norm(a) === norm(b);
  const grades = tools.grades;
  let state;
  let visible = false;
  let modalOwner;

  function node(tag, text, className) {
    const el = document.createElement(tag);
    if (text != null) el.textContent = text;
    if (className) el.className = className;
    return el;
  }

  function button(text, action, className) {
    const el = node('button', text, className);
    el.type = 'button';
    el.onclick = action;
    return el;
  }

  function toggle(text, selected, action) {
    const el = button(text, action);
    el.setAttribute('aria-pressed', String(selected));
    return el;
  }

  function countedTab(text, count, selected, action) {
    const tab = toggle(text, selected, action);
    tab.setAttribute('aria-label', text);
    if (count != null) tab.append(node('span', String(count), 'tl-tab-count'));
    return tab;
  }

  function say(text) {
    fields.status.textContent = text;
  }

  function title(text, children) {
    const section = node('section', null, 'tl-evidence');
    const heading = node('div', null, 'tl-evidence-heading');
    heading.append(node('h4', text));
    section.append(heading, ...children);
    return section;
  }

  function field(label, value = '', type = 'input') {
    const wrapper = node('label', label);
    const input = node(type);
    input.value = value;
    wrapper.append(input);
    return {wrapper, input};
  }

  function eligible(item) {
    return ['new', 'supplement'].includes(item.status) && tools.hasLiterature(item);
  }

  function coverage(preview) {
    const refs = preview.references.filter(ref => ref.selected);
    return {refs, full: refs.filter(ref => ref.status === 'fulltext').length,
      abstracts: refs.filter(ref => ref.status === 'abstract').length,
      excerpts: refs.filter(ref => ref.status === 'excerpt').length,
      missing: refs.filter(ref => !['fulltext', 'excerpt', 'abstract'].includes(ref.status)).length,
      issues: preview.issues.length};
  }

  function disposition(item, preview) {
    if (item.status === 'conflict') return ['conflict', '冲突', '术语含义或条目有冲突'];
    const gradesFound = ['fulltext', 'excerpt', 'abstract'].filter(grade =>
      item.sources.some(source => tools.readableReference(source) && source.grade === grade));
    if (gradesFound.length) return ['supported', '有文献依据', grades[gradesFound[0]] + '匹配'];
    const counts = coverage(preview);
    return ['suspect', '可疑', counts.full || counts.abstracts || counts.excerpts ? '当前范围未找到近似表述' : '待补可读文献原文'];
  }

  function exclusionId(item) {
    const editions = [...new Set(item.sources.filter(source => source.kind === 'manuscript')
      .map(source => JSON.stringify([source.source_id, source.version])))].sort();
    return JSON.stringify([item.id, editions]);
  }

  function savedTerms() {
    return (pp.data?.terms || []).filter(term => !term.status || term.status === 'active');
  }

  function pendingTerms() {
    return (state.preview?.items || []).filter(item => !item.reference_only && item.status !== 'covered'
      && !state.excluded.has(exclusionId(item)));
  }

  function saveExclusions() {
    try { localStorage.setItem('paperpilot.term-exclusions.' + state.pid, JSON.stringify([...state.excluded])); }
    catch { /* The current session still retains the decision. */ }
  }

  function setProject() {
    const pid = project()?.id;
    if (state && state.pid === pid) return;
    popup.close();
    if (advanced.open) advanced.close();
    let exclusions = [];
    try { exclusions = JSON.parse(localStorage.getItem('paperpilot.term-exclusions.' + pid) || '[]'); }
    catch { /* Use an empty set when earlier local data cannot be read. */ }
    const initialTab = pp.standaloneTerms ? new URLSearchParams(location.search).get('tab') : '';
    const domainReview = initialTab === 'domain-review';
    state = {pid, scope: domainReview ? 'domain' : 'paper', paperTab: initialTab === 'adopted' ? 'adopted' : 'queue',
      domainTab: domainReview ? 'pending' : 'published', query: '', filter: 'all',
      domains: null, domainId: '', domainData: null, domainRefs: null, selected: '', mobile: false,
      excluded: new Set(Array.isArray(exclusions) ? exclusions : []), selectedItems: new Set(), referenceIds: null};
    say('');
    render();
    if (pid && (visible || pp.standaloneTerms)) {
      if (domainReview) refreshDomains(state);
      else refreshComparison(state);
    }
  }

  function go(scope, tab) {
    state.scope = scope;
    if (tab) state[scope + 'Tab'] = tab;
    if (scope === 'domain' && state.domainTab === 'pending') state.domainVersion = null;
    state.query = '';
    state.filter = 'all';
    state.selected = '';
    state.mobile = false;
    search.value = '';
    say('');
    render();
    if (!state.pid) return;
    if (scope === 'paper' && state.paperTab === 'queue' && !state.preview) refreshComparison(state);
    if (scope === 'domain') refreshDomains(state);
  }

  function available() {
    if (!state.pid) return [];
    if (state.scope === 'domain') {
      return state.domainTab === 'pending'
        ? [
          ...(state.domainRefs?.items || []).map(item => ({...item, label: item.term,
            detail: item.aliases.join('、'), stateKey: 'reference', sublabel: item.reference_count + ' 篇文献',
            stateLabel: {new:'文献候选',supplement:'补充文献来源',conflict:'术语冲突'}[item.status], key: item.id, kind: 'reference-review'})),
          ...(state.domainData?.reviews || []).map(review => ({...review, label: review.suggested_term,
            detail: review.projectName, stateKey: 'submission', stateLabel: '论文提交', key: review.id, kind: 'review'})),
        ]
        : (state.domainData?.terms || []).map(term => ({...term, label: term.term, detail: term.aliases,
          stateLabel: term.reference_sources?.length ? '参考文献' : term.source_project_id ? '论文沉淀' : '领域维护',
          stateKey: term.reference_sources?.length ? 'reference' : term.source_project_id ? 'paper' : 'domain',
          sublabel: 'v' + term.version + (term.definition?.trim() ? '' : ' · 待补定义'), key: term.id, kind: 'domain'}));
    }
    if (state.paperTab === 'adopted') {
      return savedTerms().map(term =>
        ({...term, label: term.term, detail: term.aliases, stateLabel: term.source_domain_version
          ? '领域复制' : term.source_project_id ? '论文复用' : '论文术语', sublabel: 'v' + term.version, key: term.id, kind: 'adopted'}));
    }
    const order = {suspect: 0, conflict: 1, supported: 2};
    return pendingTerms().map(item => {
      const [stateKey, stateLabel, sublabel] = disposition(item, state.preview);
      return {...item, label: item.canonical, detail: item.aliases.join('、'), stateKey, stateLabel,
        sublabel, key: item.id, kind: 'candidate'};
    }).sort((left, right) => order[left.stateKey] - order[right.stateKey]);
  }

  function render() {
    if (!state) return;
    const focused = document.activeElement;
    const group = [scopes, fields.tabs, filters].find(container => container.contains(focused));
    const controlName = control => control.getAttribute('aria-label') || control.textContent;
    const focusLabel = group ? controlName(focused) : '';
    const {scope, paperTab, domainTab, pid} = state;
    const paper = scope === 'paper';
    const writable = pid && !readOnly(project());
    const queue = paper && paperTab === 'queue';
    const pending = !paper && domainTab === 'pending';
    scopes.replaceChildren(toggle('论文术语库', paper, () => go('paper')), toggle('领域术语库', !paper, () => go('domain')));
    fields.tabs.replaceChildren(...(paper ? [
      countedTab('待复核', state.preview ? pendingTerms().length : null, queue, () => go('paper', 'queue')),
      countedTab('已收录', pid ? savedTerms().length : null, !queue, () => go('paper', 'adopted')),
    ] : [
      toggle('已发布', !pending, () => go('domain', 'published')),
      countedTab('待复核', state.domainRefs && state.domainTab === 'pending'
        ? state.domainRefs.items.length + (state.domainData?.reviews.length || 0) : null, pending, () => go('domain', 'pending')),
    ]));
    fields.actions.replaceChildren();
    fields.context.replaceChildren();
    const domain = state.domains?.find(item => item.id === state.domainId);
    if (pid && paper) {
      const compare = button('重新识别并对照', () => {
        if (state.paperTab !== 'queue') go('paper', 'queue');
        refreshComparison(state);
      });
      compare.disabled = Boolean(state.comparing || state.saving);
      fields.actions.append(compare, button('从领域库复制', () => go('domain', 'published')));
      if (writable) fields.actions.append(button('添加术语', () => editTerm()));
      if (state.preview) {
        const {refs, full, abstracts, excerpts, missing, issues} = coverage(state.preview);
        fields.context.append(node('strong', '文献覆盖'), node('span', full + ' / ' + refs.length + ' 篇有全文'),
          node('span', abstracts + ' 篇有摘要'));
        if (excerpts) fields.context.append(node('span', excerpts + ' 篇有原文摘录'));
        fields.context.append(node('span', missing + ' 篇待补原文'));
        if (issues) fields.context.append(node('span', issues + ' 项关联待核对'));
        fields.context.append(button('查看对照范围', showScope, 'tl-link'));
      } else fields.context.append(node('span', state.comparing ? '正在读取稿件和参考文献…' : '识别后可查看文献对照范围。'));
      if (writable && !queue && document.querySelector('#termReviewWorkbench')) {
        fields.actions.append(button('批量沉淀与分类', openAdvanced));
      }
    } else if (pid) {
      const select = node('select');
      select.setAttribute('aria-label', '当前领域');
      select.append(new Option(state.loadingDomain ? '正在读取领域…' : '选择领域', ''));
      for (const item of state.domains || []) select.append(new Option(item.name, item.id));
      select.value = state.domainId;
      select.onchange = () => {
        state.domainId = select.value;
        state.domainVersion = null;
        state.domainData = null;
        state.domainRefs = null;
        state.selected = '';
        state.mobile = false;
        say('');
        refreshDomains(state);
      };
      fields.context.append(node('strong', '当前领域'), select);
      if (domain) {
        const version = state.domainData?.version ?? state.domainVersion ?? domain.version;
        fields.context.append(node('span', '已发布版本 v' + domain.version));
        if (version !== domain.version) fields.context.append(node('span', '正在查看历史版本 v' + version));
        if (state.loadingDomainRefs) fields.context.append(node('span', '正在汇总参考文献术语…'));
        else if (state.domainRefsError) fields.context.append(button('重试文献汇总', () => refreshDomains(state), 'tl-link'));
        else if (state.domainRefs) {
          const coverage = state.domainRefs.coverage;
          const count = Object.values(coverage).reduce((total, number) => total + number, 0);
          if (count) {
            fields.context.append(node('span', count + ' 篇关联文献'),
              node('span', Object.entries(tools.grades).filter(([grade]) => coverage[grade])
                .map(([grade, label]) => label + ' ' + coverage[grade]).join(' · ')),
              button('文献生成 ' + state.domainRefs.items.length + ' 项候选', () => go('domain', 'pending'), 'tl-link'));
          }
          else fields.context.append(node('span', '关联到此领域的论文文献后，系统会自动汇总术语。'));
          if (domain.id !== project().domain_id) {
            const linked = state.domainRefs.source_projects.includes(pid);
            fields.context.append(button(linked ? '移出当前论文文献' : '纳入当前论文文献',
              () => setDomainReferenceSource(domain, !linked), 'tl-link'));
          }
        }
        fields.context.append(button('领域版本历史', () => showVersions(domain), 'tl-link'));
        if (writable && domain.id !== project().domain_id)
          fields.actions.append(button('绑定为论文领域', () => bindDomain(domain)));
        if (version === domain.version) fields.actions.append(button('添加领域术语', () => editTerm(null, domain)));
      } else if (state.domains && !state.domains.length) {
        fields.actions.append(button('新建领域', createDomain));
      }
    }
    const values = queue ? [['all','全部'], ['suspect','可疑'], ['supported','有文献依据'], ['conflict','冲突']]
      : pending ? [['all','全部来源'], ['reference','文献生成'], ['submission','论文提交']]
        : !paper ? [['all','全部来源'], ['reference','参考文献'], ['domain','领域维护'], ['paper','论文沉淀']] : [];
    filters.replaceChildren(...values.map(([key, text]) => toggle(text, state.filter === key, () => {
      state.filter = key;
      state.mobile = false;
      render();
    })));
    fields.headings.replaceChildren(node('span', '术语'), node('span', queue ? '文献对照' : '来源'));
    renderList();
    if (group) [...group.querySelectorAll('button')].find(control => controlName(control) === focusLabel)?.focus();
  }

  function emptyText() {
    if (!state.pid) return '选择论文后查看术语。';
    if (state.scope === 'domain') {
      if (state.loadingDomain || (state.domainTab === 'pending' && state.loadingDomainRefs)) return '正在读取领域术语…';
      if (!state.domainId) return '选择一个领域，查看共享术语和历史版本。';
      return state.domainTab === 'pending' ? '当前领域暂无待复核术语。' : '当前领域暂无已发布术语。';
    }
    if (state.paperTab === 'adopted') return '当前论文尚未收录术语。';
    if (state.comparing && !state.preview) return '正在读取稿件和参考文献…';
    return state.preview ? '当前没有待复核术语。' : '运行识别并对照，查看稿件中的术语候选。';
  }

  function matchesQueueFilter(item) {
    const query = norm(state.query);
    return (state.filter === 'all' || state.filter === item.stateKey) &&
      [item.label, item.detail].some(text => norm(text).includes(query));
  }

  function renderReferenceBatch(shown) {
    fields.referenceBatch.replaceChildren();
    if (state.scope !== 'domain' || state.domainTab !== 'pending' || state.domainRefsError) return;
    const references = shown.filter(item => item.kind === 'reference-review');
    const ready = references.filter(item => item.status !== 'conflict');
    if (ready.length) {
      const publish = button('一键入库（' + ready.length + ' 项）', () => acceptReferenceTerms(ready, true), 'primary');
      publish.title = '将当前筛选下可入库的文献候选保存为一个领域版本';
      publish.disabled = Boolean(state.saving || state.loadingDomain || state.loadingDomainRefs);
      fields.referenceBatch.append(publish);
    }
    const conflicts = references.length - ready.length;
    if (conflicts) fields.referenceBatch.append(node('small', conflicts + ' 项名称或别名冲突待处理'));
  }

  function renderList() {
    if (!state) return;
    const previousScroll = fields.list.scrollTop;
    const all = available();
    const shown = all.filter(matchesQueueFilter);
    renderReferenceBatch(shown);
    const selected = shown.find(item => item.key === state.selected) || shown[0];
    state.selected = selected?.key || '';
    const batch = state.scope === 'paper' && state.paperTab === 'queue' && !state.preview?.read_only
      && !state.compareError && !readOnly(project() || {});
    fields.list.replaceChildren(...shown.map(item => {
      const line = node('li', null, 'tl-line');
      line.classList.toggle('tl-selected', item.key === state.selected);
      const pick = toggle(item.label, item.key === state.selected, () => {
        state.selected = item.key;
        state.mobile = true;
        renderList();
        if (host.clientWidth <= 640) fields.detail.querySelector('h3')?.focus();
        else [...fields.list.querySelectorAll('.tl-row')].find(el => el.dataset.key === item.key)?.focus();
      });
      pick.className = 'tl-row';
      pick.dataset.key = item.key;
      const name = node('span');
      name.append(node('strong', item.label));
      if (item.detail) name.append(node('small', item.detail));
      const evidence = node('span');
      evidence.append(node('span', item.stateLabel, 'tl-badge tl-' + (item.stateKey || 'neutral')));
      if (item.sublabel) evidence.append(node('small', item.sublabel));
      pick.replaceChildren(name, evidence);
      line.append(pick);
      if (batch && eligible(item)) {
        const check = node('input');
        check.type = 'checkbox';
        check.checked = state.selectedItems.has(item.key);
        check.setAttribute('aria-label', '批量选择 ' + item.label);
        check.onchange = () => {
          if (check.checked) state.selectedItems.add(item.key);
          else state.selectedItems.delete(item.key);
          renderCount(shown.length, batch);
        };
        line.append(check);
      }
      return line;
    }));
    if (!shown.length) fields.list.append(node('li', all.length ? '没有符合条件的术语。' : emptyText(), 'tl-empty'));
    fields.list.scrollTop = previousScroll;
    renderCount(shown.length, batch);
    host.classList.toggle('tl-mobile-detail', Boolean(state.mobile && selected));
    fields.detail.replaceChildren();
    if (!selected) return;
    const back = button('返回术语列表', () => {
      state.mobile = false;
      renderList();
      [...fields.list.querySelectorAll('.tl-row')].find(el => el.dataset.key === state.selected)?.focus();
    }, 'tl-mobile-back');
    const heading = node('div', null, 'tl-detail-heading');
    const label = node('h3', selected.label);
    label.tabIndex = -1;
    const copy = node('div'); copy.append(label);
    if (selected.detail) copy.append(node('p', selected.detail));
    heading.append(copy, node('span', selected.stateLabel, 'tl-badge tl-' + (selected.stateKey || 'neutral')));
    const content = node('div', null, 'tl-detail-content');
    content.append(heading);
    const actions = node('footer', null, 'tl-footer');
    fields.detail.append(back, content);
    if (selected.kind === 'candidate') candidateDetail(selected, content, actions);
    if (selected.kind === 'adopted') adoptedDetail(selected, content, actions);
    if (selected.kind === 'domain') domainDetail(selected, content, actions);
    if (selected.kind === 'review') reviewDetail(selected, content, actions);
    if (selected.kind === 'reference-review') referenceReviewDetail(selected, content, actions);
    if (actions.childElementCount) {
      if (state.saving) for (const control of actions.querySelectorAll('button')) control.disabled = true;
      fields.detail.append(actions);
    }
  }

  function renderCount(count, batch) {
    fields.count.replaceChildren(node('span', '显示 ' + count + ' 项'));
    if (batch && state.selectedItems.size) {
      const commit = button('收录所选 ' + state.selectedItems.size + ' 项', () => {
        const choices = Object.fromEntries(available().filter(item => state.selectedItems.has(item.id) && eligible(item))
          .map(item => [item.id, resolution(item)]));
        if (Object.keys(choices).length) applyCandidates(choices);
      }, 'primary');
      commit.disabled = Boolean(state.saving || state.comparing);
      fields.count.append(commit);
    }
  }

  function quote(source, terms) {
    const block = node('blockquote');
    const text = source.quote || '此处没有可显示的摘录。';
    const alternatives = [...new Set([source.label, ...terms].filter(Boolean))].sort((a,b) => b.length-a.length);
    const expression = alternatives.map(value => value.replace(/[.*+?^\${}()|[\]\\]/g, '\\$&')).join('|');
    let cursor = 0;
    if (expression) for (const match of text.matchAll(new RegExp(expression, 'giu'))) {
      block.append(text.slice(cursor, match.index), node('mark', match[0]));
      cursor = match.index + match[0].length;
    }
    block.append(text.slice(cursor));
    return block;
  }

  function sourceLink(source) {
    const asPage = /^(?:page:|第\s*)(\d+)(?:\s*页)?$/.exec(source.location || '');
    const locationLabel = asPage ? 'PDF 第 ' + asPage[1] + ' 页' : source.location;
    const label = [...new Set([grades[source.grade], locationLabel].filter(Boolean))].join(' · ');
    const manuscript = source.url?.startsWith('/api/manuscript-imports/');
    const internal = manuscript || (source.url?.startsWith('/api/projects/') && source.url.endsWith('/pdf'));
    if (!internal && !/^https?:\/\//i.test(source.source_url || '')) {
      if (source.kind === 'reference' && source.source_id)
        return button(label || '查看文献', () => tools.showReference(source.source_id, source.project_id || state.pid, source.version), 'tl-link');
      return node('span', label);
    }
    const target = new URL(internal ? source.url : source.source_url, window.location.origin);
    if (manuscript && asPage) target.searchParams.set('inline', 'true');
    if (asPage && internal) target.hash = 'page=' + asPage[1];
    const link = node('a', manuscript && !asPage ? '下载稿件原件' : label || '打开原文', 'tl-link');
    link.href = target.href;
    link.target = '_blank';
    link.rel = 'noopener noreferrer';
    return link;
  }

  function sourceSection(sectionName, sources, item, empty) {
    if (!sources.length) return title(sectionName, [node('p', empty)]);
    const terms = [item.canonical, ...item.aliases];
    function entry(source) {
      const container = node('div', null, 'tl-source');
      const head = node('div', null, 'tl-source-heading');
      head.append(node('strong', source.name), sourceLink(source));
      container.append(head, quote(source, terms));
      return container;
    }
    const children = [entry(sources[0])];
    if (sources.length > 1) {
      const details = node('details');
      details.append(node('summary', '查看其余 ' + (sources.length - 1) + ' 条出处'));
      details.addEventListener('toggle', () => {
        if (details.open && details.childElementCount === 1) details.append(...sources.slice(1).map(entry));
      });
      children.push(details);
    }
    return title(sectionName, children);
  }

  function resolution(item, source) {
    const chosen = source || (!item.target?.definition && item.sources.find(entry =>
      entry.definition && entry.definition === item.definition));
    return {action: item.target ? 'merge' : 'add', ...(chosen ? {definition_id: chosen.id} : {})};
  }

  function candidateDetail(item, content, actions) {
    const manuscript = item.sources.filter(source => source.kind === 'manuscript');
    const literature = item.sources.filter(tools.readableReference)
      .sort((a,b) => ['fulltext','excerpt','abstract'].indexOf(a.grade) - ['fulltext','excerpt','abstract'].indexOf(b.grade));
    const metadata = item.sources.filter(source => source.kind === 'reference' && !tools.readableReference(source));
    content.append(sourceSection('稿件用法', manuscript, item, '此候选暂无可展示的稿件引文。'),
      sourceSection('参考文献表述', literature, item, '当前比对结果未返回可核对的文献原句。'));
    if (metadata.length) content.append(sourceSection('书目线索', metadata, item, ''));
    content.append(title('判断理由', [node('p', item.stateKey === 'supported'
      ? '当前比对在可读文献中找到了对应表述。请核对上下文后决定是否收录。' : item.sublabel + '。')]));
    if (state.preview.read_only || readOnly(project())) return;
    actions.append(button('排除', () => exclude(item), 'tl-link'));
    if (state.compareError || state.comparing) return;
    if (item.status === 'conflict' || item.target) {
      actions.append(button(item.status === 'conflict' ? '处理冲突' : '合并到已有术语', () => {
        const view = state;
        pp.openTermComparison({projectId: view.pid, focusTerm: item.canonical, referenceIds: view.referenceIds,
          onApply: () => view === state && refreshComparison(view)});
      }, 'primary'));
      return;
    }
    if (item.stateKey === 'suspect') {
      const definition = manuscript.find(source => source.definition?.trim());
      actions.append(button(definition ? '按作者定义收录' : '人工确认收录', () => {
        confirmDialog('人工确认收录', '当前比对缺少文献原句。请根据稿件原文核对这个术语。',
          '确认收录到论文', () => applyCandidates({[item.id]: resolution(item, definition)}));
      }, 'primary'));
    } else actions.append(button('收录到论文', () => applyCandidates({[item.id]: resolution(item)}), 'primary'));
  }

  function exclude(item) {
    const id = exclusionId(item);
    state.excluded.add(id);
    state.selectedItems.delete(item.id);
    saveExclusions();
    state.mobile = false;
    render();
    say('本浏览器已从待复核列表排除该术语。');
    fields.status.append(' ', button('撤销排除', () => {
      state.excluded.delete(id);
      saveExclusions();
      say('');
      render();
    }, 'tl-link'));
  }

  async function refreshComparison(view, referenceIds = view.referenceIds) {
    if (!view.pid || view.comparing) return;
    view.comparing = true;
    view.referenceIds = referenceIds;
    view.selectedItems.clear();
    render();
    try {
      const value = await post(apiPath(view.pid) + '/terminology/comparison/preview', {reference_ids: referenceIds});
      if (view !== state) return;
      view.preview = value;
      view.compareError = false;
    } catch (error) {
      if (view === state) { view.compareError = true; say(tools.errorText(error)); }
    } finally {
      view.comparing = false;
      if (view === state) render();
    }
  }

  async function applyCandidates(decisions) {
    const view = state;
    if (view.saving || view.comparing || !view.preview || view.compareError) return;
    const data = {fingerprint: view.preview.fingerprint, reference_ids: view.referenceIds, decisions};
    const signature = JSON.stringify(data);
    if (view.receipt?.signature !== signature) view.receipt = {signature, id: crypto.randomUUID()};
    view.saving = true;
    try {
      const result = await post(apiPath(view.pid) + '/terminology/comparison/apply', {...data, request_id: view.receipt.id});
      if (view !== state) return;
      if (popup.open) popup.close();
      view.mobile = false;
      await pp.refreshResourceLists();
      await refreshComparison(view);
      if (view === state && !view.compareError) say('已收录 ' + result.items.length + ' 项论文术语。');
    } catch (error) {
      if (view === state) say(tools.errorText(error));
      if (popup.open && view === state) throw error;
    } finally {
      view.saving = false;
      if (view === state) render();
    }
  }

  const popup = node('dialog', null, 'tl-dialog');
  const dialogTitle = node('h3');
  dialogTitle.id = 'termLibraryDialogTitle';
  popup.setAttribute('aria-labelledby', dialogTitle.id);
  const dialogStatus = node('p', null, 'tl-status'); dialogStatus.setAttribute('role', 'status');
  const dialogBody = node('div', null, 'tl-dialog-body');
  const dialogHead = node('div', null, 'tl-dialog-head');
  dialogHead.append(dialogTitle, button('关闭', () => popup.close()));
  popup.append(dialogHead, dialogStatus, dialogBody);
  const advanced = node('dialog', null, 'tl-dialog');
  advanced.setAttribute('aria-label', '批量沉淀与分类');
  advanced.append(button('关闭', () => advanced.close()));
  document.body.append(popup, advanced);

  function showDialog(text, children) {
    modalOwner = state;
    dialogTitle.textContent = text;
    dialogStatus.textContent = '';
    dialogBody.replaceChildren(...children);
    if (!popup.open) popup.showModal();
  }

  function submit(text, work) {
    const control = button(text, async () => {
      const view = modalOwner;
      control.disabled = true;
      dialogStatus.textContent = '';
      try { await work(view); }
      catch (error) { if (popup.open && view === state) dialogStatus.textContent = tools.errorText(error); }
      finally { control.disabled = false; }
    }, 'primary');
    return control;
  }

  function confirmDialog(text, note, action, work) {
    showDialog(text, [node('p', note), submit(action, work)]);
  }

  function showScope() {
    const preview = state.preview;
    const inputs = [];
    const list = node('div', null, 'tl-ref-list');
    for (const reference of preview.references) {
      const row = node('label');
      const check = node('input');
      check.type = 'checkbox'; check.value = reference.source_id; check.checked = reference.selected;
      row.append(check, node('span', [reference.cite_key, reference.name, 'v' + reference.version,
        grades[reference.status], reference.ambiguous ? '待核对匹配' : ''].filter(Boolean).join(' · ')));
      inputs.push(check);
      list.append(row);
    }
    for (const issue of preview.issues) list.append(node('p', [issue.cite_key, issue.title,
      {ambiguous: '有多个匹配', unmatched: '尚未匹配到文献库', unavailable: '资料不可用'}[issue.status]].filter(Boolean).join(' · ')));
    if (!list.childElementCount) list.append(node('p', '当前论文没有可选的关联文献。'));
    const apply = button('按所选文献重新对照', () => {
      popup.close();
      refreshComparison(state, inputs.filter(input => input.checked).map(input => input.value));
    }, 'primary');
    showDialog('文献对照范围', inputs.length ? [list, apply] : [list]);
  }

  function editTerm(item, domain) {
    const name = field('术语', item?.term);
    const meaning = field('定义', item?.definition, 'textarea');
    const aliases = field('别名', item?.aliases);
    const requestId = crypto.randomUUID();
    const children = [name.wrapper, meaning.wrapper];
    if (!domain) children.push(aliases.wrapper);
    const save = submit('保存术语', async view => {
      const term = name.input.value.trim();
      if (!term) throw new Error('请输入术语名称。');
      const definition = meaning.input.value.trim();
      if (domain) {
        if (item) {
          const resource = item.resource;
          await post(domainPath(domain.id) + '/resources/' + encodeURIComponent(resource.id),
            {title: term, content: definition, expected_version: resource.version, request_id: requestId}, 'PUT');
        } else {
          const latest = await pp.api(domainPath(domain.id) + '/terms');
          if (view !== state) return;
          if (latest.some(entry => closestName(entry.term, term))) throw new Error('领域已有同名术语，请编辑现有条目。');
          await post(domainPath(domain.id) + '/resources', {resource_type: 'term', title: term, content: definition});
        }
      } else {
        await post(apiPath(view.pid) + '/terms', {term, definition, aliases: aliases.input.value.trim()});
      }
      if (view !== state) return;
      popup.close();
      if (domain) await refreshDomains(view);
      else {
        await pp.refreshResourceLists();
        if (view !== state) return;
        view.preview = null;
        go('paper', 'adopted');
      }
      if (view === state) say('已保存术语。');
    });
    showDialog(item ? '编辑领域术语' : domain ? '添加领域术语' : '添加术语', [...children, save]);
  }

  function adoptedDetail(item, content, actions) {
    const fromDomain = item.source_domain_version;
    content.append(title('定义', [node('p', item.definition || '定义待补。')]),
      title('来源与版本', [node('p', fromDomain ? '从领域库 v' + fromDomain + ' 复制，当前为论文术语 v' + item.version + '。'
        : item.source_project_id ? '来源论文 ' + item.source_project_id + '，当前为论文术语 v' + item.version + '。'
          : '当前论文术语 v' + item.version + '。')]));
    actions.append(button('术语历史', () => tools.showHistory(state.pid, item.id)));
    if (project().domain_id && !readOnly(project()))
      actions.append(button('提交领域复核', () => submitForDomain(item), 'primary'));
  }

  function submitForDomain(item) {
    const domainId = project().domain_id;
    const requestId = crypto.randomUUID();
    confirmDialog('提交领域复核', '将当前论文术语提交给已绑定的领域库。人工确认后可发布。',
      '确认提交', async view => {
        await post(apiPath(view.pid) + '/terminology/reviews', {
          request_id: requestId, term_id: item.id, domain_id: domainId, suggested_term: item.term,
          suggested_definition: item.definition, suggested_aliases: item.aliases, confidence: 1,
          basis: '作者从论文术语库提交人工复核', classifier_version: 'manual',
        });
        if (view !== state) return;
        popup.close();
        view.domainId = domainId;
        go('domain', 'pending');
        say('已提交领域人工复核。');
      });
  }

  async function refreshDomains(view) {
    const token = Symbol();
    view.domainToken = token;
    view.loadingDomain = true;
    const isCurrent = () => view === state && token === view.domainToken;
    render();
    try {
      const domains = await pp.api('/api/domains');
      if (!isCurrent()) return;
      view.domains = domains;
      if (view.domainId === '') view.domainId = project().domain_id || domains[0]?.id || '';
      const domain = domains.find(item => item.id === view.domainId);
      if (!domain) { view.domainData = null; return; }
      const version = view.domainVersion ?? domain.version;
      const base = domainPath(domain.id);
      view.loadingDomainRefs = true;
      view.domainRefsError = false;
      const referenceTerms = pp.api(base + '/reference-terms').then(data => ({data}), error => ({error}));
      render();
      const [terms, resources, papers] = await Promise.all([
        pp.api(base + '/terms?version=' + version),
        version === domain.version ? pp.api(base + '/resources') : [],
        view.domainTab === 'pending' ? pp.api('/api/projects') : [],
      ]);
      const reviewSets = await Promise.all(papers.filter(paper => paper.domain_id === domain.id || paper.id === view.pid)
        .map(async paper => {
          const reviews = await pp.api(apiPath(paper.id) + '/terminology/reviews?status=pending_manual');
          return reviews.filter(review => review.domain_id === domain.id).map(review =>
            ({...review, projectName: paper.name, sourceReadOnly: readOnly(paper)}));
        }));
      if (isCurrent()) {
        view.domainData = {version, terms, resources, reviews: reviewSets.flat()};
        view.loadingDomain = false;
        render();
      }
      const proposals = await referenceTerms;
      if (!isCurrent()) return;
      view.domainRefs = proposals.data || null;
      view.domainRefsError = Boolean(proposals.error);
    } catch (error) {
      if (isCurrent()) say(tools.errorText(error));
    } finally {
      if (isCurrent()) { view.loadingDomain = false; view.loadingDomainRefs = false; render(); }
    }
  }

  function showVersions(domain) {
    const select = node('select');
    select.setAttribute('aria-label', '查看领域版本');
    for (let version = domain.version; version >= 1; version--)
      select.append(new Option('v' + version + (version === domain.version ? ' · 当前发布' : ''), version));
    select.value = state.domainVersion ?? domain.version;
    showDialog('领域版本历史', [node('p', domain.name), select, button('查看版本', () => {
      state.domainVersion = Number(select.value);
      state.domainData = null;
      popup.close();
      go('domain', 'published');
    }, 'primary')]);
  }

  function domainDetail(item, content, actions) {
    const domain = state.domains.find(entry => entry.id === state.domainId);
    if (!domain) return;
    content.append(title('定义', [node('p', item.definition || '定义待补。')]),
      title('来源与版本', [node('p', domain.name + ' · v' + item.version + (item.source_project_id ? ' · 来源论文 ' + item.source_project_id : ' · 领域维护')),
        node('p', '复制后，论文保存独立术语副本和来源版本。')]));
    if (item.reference_sources?.length) content.append(sourceSection('已发布的文献依据', item.reference_sources,
      {canonical:item.term, aliases:[]}, ''));
    const resource = state.domainData.resources.find(entry => entry.resource_type === 'term' &&
      closestName(entry.title, item.term) && entry.content === item.definition);
    if (resource && !state.loadingDomain) actions.append(button('编辑领域术语', () => editTerm({...item, resource}, domain)));
    if (!readOnly(project()) && project().domain_id === domain.id) {
      const copy = button('复制到当前论文', () => copyFromDomain(item, domain), 'primary');
      copy.disabled = Boolean(state.loadingDomain);
      actions.append(copy);
    } else if (!readOnly(project())) content.append(node('p', '绑定此领域后可复制术语到当前论文。'));
  }

  function copyFromDomain(item, domain) {
    const requestId = crypto.randomUUID();
    confirmDialog('复制到当前论文', '将「' + item.term + '」的领域 v' + item.version + ' 复制为论文术语副本。', '确认复制', async view => {
      const latest = await pp.api(apiPath(view.pid));
      if (view !== state) return;
      if (latest.project.domain_id !== domain.id) throw new Error('论文的绑定领域已变化，请重新选择。');
      if (latest.terms.some(term => (!term.status || term.status === 'active') && closestName(term.term, item.term)))
        throw new Error('论文已有同名术语，请先在论文术语库核对。');
      const result = await post(apiPath(view.pid) + '/terms/import', {
        domain_term_id: item.id, source_domain_version: item.version,
        base_project_revision: latest.project.project_revision, request_id: requestId,
      });
      if (view !== state) return;
      popup.close();
      await pp.refreshResourceLists();
      if (view !== state) return;
      view.preview = null;
      go('paper', 'adopted');
      view.selected = result.id;
      renderList();
      say('已复制领域术语 v' + item.version + '。');
    });
  }

  function bindDomain(domain) {
    confirmDialog('绑定为论文领域', '将当前论文绑定到「' + domain.name + '」。复制术语仍需单独确认。', '确认绑定', async view => {
      const latest = await pp.api(apiPath(view.pid));
      if (view !== state) return;
      await post(apiPath(view.pid) + '/domain', {
        domain_id: domain.id, base_project_revision: latest.project.project_revision,
      }, 'PUT');
      if (view !== state) return;
      popup.close();
      await pp.reload();
      if (view !== state) return;
      view.preview = null;
      await refreshDomains(view);
      if (view === state) say('已绑定论文领域。');
    });
  }

  function createDomain() {
    const name = field('领域名称');
    showDialog('新建领域', [name.wrapper, submit('创建领域', async view => {
      if (!name.input.value.trim()) throw new Error('请输入领域名称。');
      const created = await post('/api/domains', {name: name.input.value.trim()});
      if (view !== state) return;
      popup.close();
      view.domainId = created.id;
      await refreshDomains(view);
    })]);
  }

  function reviewDetail(item, content, actions) {
    const domain = state.domains.find(entry => entry.id === state.domainId);
    const current = state.domainData.terms.find(term => closestName(term.term, item.suggested_term));
    content.append(title('论文提交', [node('p', item.projectName), node('p', item.suggested_definition || '定义待补。')]));
    if (current) content.append(title('领域当前定义', [node('p', current.definition || '定义待补。')]));
    if (item.sourceReadOnly || state.loadingDomain || state.domainData.version !== domain.version) return;
    actions.append(button('拒绝建议', () => decideReview(item, domain, current, false)),
      button('复核并发布', () => decideReview(item, domain, current, true), 'primary'));
  }

  function referenceReviewDetail(item, content, actions) {
    content.append(title('识别依据', [node('p', item.reasons.join('。') + '。')]));
    content.append(sourceSection('参考文献原文', item.sources, {canonical:item.term, aliases:item.aliases}, ''));
    if (item.existing) content.append(title('领域当前定义', [node('p', item.existing.definition || '定义待补。')]));
    if (item.definition_conflict) content.append(title('待补定义', [node('p', '文献给出了不同的定义表述，接受后可再补定义。')]));
    else if (item.definition) content.append(title('原文定义', [node('p', item.definition)]));
    if (item.status === 'conflict') content.append(node('p', '名称或别名存在冲突。请核对领域库与其他候选。'));
    if (state.loadingDomainRefs) return;
    actions.append(button('排除文献候选', () => rejectReferenceReview(item)));
    if (item.status !== 'conflict')
      actions.append(button('接受并入库', () => acceptReferenceTerms([item]), 'primary'));
  }

  async function applyReferenceDecision(view, action, payload) {
    const domainId = view.domainId;
    const result = await post(domainPath(domainId) + '/reference-terms/' + action, {
      ...payload, expected_version: view.domainRefs.version,
    });
    const current = () => view === state && view.domainId === domainId && view.scope === 'domain';
    if (!current()) return;
    if (result.status === 'rejected') popup.close();
    const released = result.items || [{candidate_id: result.candidate.id, definition: result.definition}];
    const resolved = new Set(released.map(item => item.candidate_id));
    if (view.domainTab === 'pending' && resolved.has(view.selected)) {
      const shown = available().filter(matchesQueueFilter);
      const position = shown.findIndex(item => item.key === view.selected);
      const next = shown.slice(position + 1).find(item => !resolved.has(item.key)) ||
        shown.slice(0, position).reverse().find(item => !resolved.has(item.key));
      view.selected = next?.key || '';
      if (!next) view.mobile = false;
    }
    if (view.domainRefs) view.domainRefs.items = view.domainRefs.items.filter(item => !resolved.has(item.id));
    await refreshDomains(view);
    if (!current()) return;
    if (result.status === 'published') {
      const incomplete = released.filter(item => !item.definition).length;
      say('已入库 ' + released.length + ' 项，领域术语库 v' + result.domain_version + '。' +
        (incomplete ? incomplete + ' 项待补定义。' : ''));
    } else say('已排除当前文献依据下的候选。');
  }

  async function acceptReferenceTerms(items, batch = false) {
    const view = state;
    if (view.saving || view.loadingDomain || view.loadingDomainRefs || !items.length) return;
    const action = batch ? 'batch' : encodeURIComponent(items[0].id) + '/decision';
    const payload = batch
      ? {candidates: Object.fromEntries(items.map(item => [item.id, item.fingerprint]))}
      : {decision: 'publish', fingerprint: items[0].fingerprint};
    const signature = JSON.stringify([view.domainId, view.domainRefs.version, action, payload]);
    if (view.referenceAcceptance?.signature !== signature)
      view.referenceAcceptance = {signature, requestId: crypto.randomUUID()};
    view.saving = true;
    renderList();
    try {
      await applyReferenceDecision(view, action, {...payload, request_id: view.referenceAcceptance.requestId});
    } catch (error) {
      if (view === state) say(tools.errorText(error));
    } finally {
      view.saving = false;
      if (view === state) renderList();
    }
  }

  function rejectReferenceReview(item) {
    const requestId = crypto.randomUUID();
    showDialog('排除文献候选', [node('p', '排除「' + item.term + '」在当前文献依据下的候选。'),
      submit('确认排除', view => applyReferenceDecision(view, encodeURIComponent(item.id) + '/decision', {
        request_id: requestId, fingerprint: item.fingerprint, decision: 'reject',
      }))]);
  }

  function setDomainReferenceSource(domain, enabled) {
    const title = enabled ? '纳入当前论文文献' : '移出当前论文文献';
    const message = enabled ? '将当前论文的关联文献用于此领域的术语识别。'
      : '此领域停止用当前论文的关联文献生成候选。已发布版本保留原有出处。';
    showDialog(title, [node('p', message), submit(enabled ? '确认纳入' : '确认移出', async view => {
      await post(domainPath(domain.id) + '/reference-source-projects/' + encodeURIComponent(view.pid), {enabled}, 'PUT');
      if (view !== state) return;
      popup.close();
      go('domain', 'pending');
    })]);
  }

  function decideReview(item, domain, current, accept) {
    const name = field('术语', item.suggested_term);
    const definition = field('定义', item.suggested_definition, 'textarea');
    const aliases = field('别名', item.suggested_aliases);
    const checkbox = node('input'); checkbox.type = 'checkbox';
    const replace = node('label', '确认用复核后的内容替换同名领域术语'); replace.prepend(checkbox);
    const requestId = crypto.randomUUID();
    const send = submit(accept ? '确认发布' : '确认拒绝', async view => {
      if (accept && !name.input.value.trim()) throw new Error('请输入术语名称。');
      const data = {request_id: requestId, decision: accept ? 'edit' : 'reject', expected_domain_version: domain.version};
      if (accept) Object.assign(data, {term: name.input.value.trim(), definition: definition.input.value.trim(),
        aliases: aliases.input.value.trim(), replace_conflict: checkbox.checked && closestName(name.input.value, item.suggested_term)});
      await post(apiPath(item.project_id) + '/terminology/reviews/' + encodeURIComponent(item.id) + '/decision', data);
      if (view !== state) return;
      popup.close();
      await refreshDomains(view);
      if (view === state) say(accept ? '已人工确认并发布领域版本。' : '已拒绝领域发布建议。');
    });
    showDialog(accept ? '复核并发布领域术语' : '拒绝领域发布建议', accept
      ? [name.wrapper, definition.wrapper, aliases.wrapper, ...(current ? [replace] : []), send]
      : [node('p', '拒绝「' + item.suggested_term + '」的领域发布建议。'), send]);
  }

  function openAdvanced() {
    const panel = document.querySelector('#termReviewWorkbench');
    if (!panel) return;
    advanced.append(panel);
    panel.querySelector('h4')?.removeAttribute('hidden');
    advanced.showModal();
  }

  document.querySelector('#referenceLibraryCard .search-heading')?.append(
    button('查看领域术语候选', async () => {
      const view = state;
      if (!view?.pid) return;
      await pp.openWorkspace('knowledge/terms');
      if (view === state) go('domain', 'pending');
    }));

  search.oninput = () => {
    state.query = search.value;
    state.mobile = false;
    renderList();
  };
  document.addEventListener('paperpilot:project-changed', setProject);
  function refreshVisibleDomain() {
    if (visible && state?.pid && state.scope === 'domain' && !state.loadingDomain && !state.loadingDomainRefs)
      refreshDomains(state);
  }
  document.addEventListener('paperpilot:resources-changed', () => {
    if (state?.pid !== project()?.id) return;
    render();
    refreshVisibleDomain();
  });
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'visible') refreshVisibleDomain();
  });
  new IntersectionObserver(entries => {
    const wasVisible = visible;
    visible = entries.some(entry => entry.isIntersecting);
    if (visible && state?.pid && state.scope === 'paper' && !state.preview && !state.compareError)
      refreshComparison(state);
    if (visible && !wasVisible) refreshVisibleDomain();
  }).observe(host);
  setProject();
})();
