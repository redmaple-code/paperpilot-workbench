import {addNode, addEdge, editItem, deleteItem, derive, liveNodes, liveEdges, History, acceptSuggestion, sourceRef} from './graph.js';
import {NODE_WIDTH, NODE_HEIGHT, edgePath, edgeLabelPosition, bindCanvas} from './canvas.js';
import {moduleFrames} from './modules.js';

const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c]));
const names = {concept: '概念', claim: '论点', entity: '对象', section: '章节', evidence: '证据', other: '待定'};

/** Native PaperPilot component. It owns view state, the session owns all editable data. */
export class StructureEditor {
  constructor(container, {onStatus, onAssist}) {
    this.container = container;
    this.onStatus = onStatus;
    this.onAssist = onAssist;
    this.zoom = 1;
    this.selection = null;
    this.inspectorTab = 'details';
    this.history = new History();
    container.innerHTML = `
      <aside class="structure-chapters"><div class="structure-pane-heading"><h3>章节大纲</h3><button data-editor-action="add-section" aria-label="新增章节">＋</button></div><nav aria-label="论文章节树" class="structure-tree"></nav></aside>
      <section class="structure-map"><div class="structure-map-toolbar"><div role="group" aria-label="图谱视图" class="structure-map-modes"><button data-editor-action="outline-map" aria-pressed="true">章节图谱</button><button data-editor-action="full-map" aria-pressed="false">完整图谱</button></div><div><button data-editor-action="add-node">添加论点</button><button data-editor-action="undo" aria-label="撤销结构编辑">撤销</button><button data-editor-action="redo" aria-label="重做结构编辑">重做</button></div></div>
      <div class="structure-outline-map" role="region" aria-label="分层章节图谱"></div>
      <div class="structure-viewport" role="region" aria-label="文章脉络画布"><div class="structure-world"><div class="structure-groups"></div><svg class="structure-edges" aria-label="论证关系"></svg><div class="structure-nodes"></div></div></div>
      <div class="structure-map-footer"><span class="structure-map-count"></span><div><button data-editor-action="zoom-out" aria-label="缩小脉络图">−</button><span class="structure-zoom"></span><button data-editor-action="zoom-in" aria-label="放大脉络图">＋</button><button data-editor-action="fit">适合视图</button></div></div></section>
      <aside class="structure-inspector"><div class="structure-inspector-tabs"><button data-inspector="details" aria-pressed="true">内容与目标</button><button data-inspector="evidence" aria-pressed="false">来源依据</button></div><div class="structure-inspector-body"></div></aside>`;
    this.$ = selector => container.querySelector(selector);
    this.viewport = this.$('.structure-viewport');
    this.world = this.$('.structure-world');
    this.fitWhenVisible = () => {
      if (!this.needsInitialFit || !this.viewport.clientWidth) return;
      this.needsInitialFit = false;
      this.fit(true);
    };
    new ResizeObserver(this.fitWhenVisible).observe(this.viewport);
    this.controller = bindCanvas({viewport: this.viewport, world: this.world, scale: () => this.zoom,
      select: (kind, id, show) => this.select(kind, id, show),
      move: (id, position, finished, cancelled) => {
        if (!this.session) return;
        if (position) {
          this.dragBefore ||= structuredClone(this.workspace);
          this.workspace.layout.positions[id] = position;
          const node = [...this.$('.structure-nodes').children].find(el => el.dataset.node === id);
          if (node) { node.style.left = position.x + 'px'; node.style.top = position.y + 'px'; }
          this.renderEdges();
        } else if (this.dragBefore) {
          if (cancelled) this.session.workspace = this.dragBefore;
          if (finished) { this.history.push(this.dragBefore); this.session.changed(); }
          this.dragBefore = null;
          this.renderGraph();
        }
      },
      connect: (from, to) => this.connect(from, to),
      preview: (from, point) => { this.wire = from ? {from, point} : null; this.renderEdges(); },
      connectionState: from => { if (from) this.onStatus('选择另一节点的输入端，建立论证关系。'); }});
    container.addEventListener('click', event => this.handleClick(event));
    container.addEventListener('keydown', event => {
      if (event.target.matches('[data-node]') && ['Enter', ' '].includes(event.key)) {
        event.preventDefault(); this.select('node', event.target.dataset.node, true);
      }
      if (event.target.matches('[data-edge]') && ['Enter', ' '].includes(event.key)) {
        event.preventDefault(); this.select('edge', event.target.dataset.edge, true);
      }
    });
  }

  get workspace() { return this.session.workspace; }

  setSession(session) {
    this.session = session;
    this.selection = session.workspace.paper_outline?.[0] ? {kind: 'section', id: session.workspace.paper_outline[0].section_id} : null;
    this.mapMode = this.selection ? 'outline' : 'full';
    this.branchId = null;
    this.history = new History();
    this.inspectorTab = 'details';
    this.activeSegment = null;
    this.needsInitialFit = true;
    this.controller.cancelConnection();
    this.render();
    this.fitWhenVisible();
  }

  change(operation, refresh = true) {
    const before = structuredClone(this.workspace);
    try {
      operation(this.workspace);
      derive(this.workspace);
      this.history.push(before);
      this.session.changed();
      if (refresh) this.render();
      else { this.renderTree(); this.renderGraph(); }
    } catch (error) { this.session.workspace = before; this.render(); this.onStatus(error.message); }
  }

  selectedNodeIds() {
    if (!this.selection) return [];
    if (this.selection.kind === 'node') return [this.selection.id];
    if (this.selection.kind === 'section') return this.workspace.paper_outline.find(s => s.section_id === this.selection.id)?.node_ids || [];
    const edge = this.workspace.graph.edges.find(e => e.id === this.selection.id);
    return edge ? [edge.from, edge.to] : [];
  }

  select(kind, id, show = true) {
    if (!this.session || !id) return;
    this.selection = {kind, id};
    this.activeSegment = null;
    if (show) this.inspectorTab = 'details';
    this.renderTree();
    if (this.mapMode === 'outline') this.renderOutlineMap();
    else {
      this.highlight();
      if (kind === 'section') this.revealNode(this.selectedNodeIds()[0]);
    }
    this.renderInspector();
    this.$('.structure-inspector-body').scrollTop = 0;
  }

  revealNode(id) {
    const point = this.workspace.layout.positions[id];
    if (!point || !this.viewport.clientWidth) return;
    const left = (point.x - 8) * this.zoom, top = (point.y - 8) * this.zoom;
    const right = (point.x + NODE_WIDTH + 8) * this.zoom, bottom = (point.y + NODE_HEIGHT + 8) * this.zoom;
    const {scrollLeft, scrollTop, clientWidth, clientHeight} = this.viewport;
    this.viewport.scrollTo({
      left: left < scrollLeft || right > scrollLeft + clientWidth ? (left + right - clientWidth) / 2 : scrollLeft,
      top: top < scrollTop || bottom > scrollTop + clientHeight ? (top + bottom - clientHeight) / 2 : scrollTop
    });
  }

  render() {
    this.renderTree(); this.renderGraph(); this.renderInspector();
    this.$('[data-editor-action="undo"]').disabled = !this.history.past.length;
    this.$('[data-editor-action="redo"]').disabled = !this.history.future.length;
  }

  renderTree() {
    const sections = this.workspace.paper_outline || [];
    this.sectionsById = new Map(sections.map(section => [section.section_id, section]));
    this.outlineNodes = new Map(liveNodes(this.workspace).map(node => [node.id, node]));
    this.sectionChildren = new Map();
    for (const section of [...sections].sort((a, b) => a.position - b.position)) {
      const parent = this.sectionsById.has(section.parent_id) ? section.parent_id : null;
      if (!this.sectionChildren.has(parent)) this.sectionChildren.set(parent, []);
      this.sectionChildren.get(parent).push(section);
    }
    const target = this.$('.structure-tree');
    const ordered = [];
    const seen = new Set();
    const visit = (parent, depth) => {
      for (const section of this.sectionChildren.get(parent) || []) {
        if (seen.has(section.section_id)) continue;
        seen.add(section.section_id); ordered.push({section, depth}); visit(section.section_id, depth + 1);
      }
    };
    visit(null, 0);
    for (const section of sections) if (!seen.has(section.section_id)) ordered.push({section, depth: 0});
    target.innerHTML = ordered.map(({section, depth}) => `<button data-section="${escape(section.section_id)}" style="padding-left:${12 + Math.min(depth, 8) * 16}px" aria-pressed="${this.selection?.kind === 'section' && this.selection.id === section.section_id}"><span>${escape(section.title)}</span><small>${escape(section.purpose || '填写章节目标')}</small></button>`).join('') || '<p class="structure-empty-note">从创意构思生成大纲，也可以手动添加章节。</p>';
  }

  openSection(id) {
    const section = this.sectionsById.get(id);
    if (!section) return;
    if (this.mapMode === 'outline') {
      const hasDetails = this.sectionChildren.has(id) || this.relatedOutlineNodes(section).length > 0;
      if (hasDetails) this.branchId = id;
      else if (this.branchId !== id) this.branchId = this.sectionsById.has(section.parent_id) ? section.parent_id : null;
    }
    this.select('section', id, false);
  }

  relatedOutlineNodes(section) {
    return (section.node_ids || []).map(id => this.outlineNodes.get(id)).filter(node => node && node.kind !== 'section');
  }

  showMap(mode) {
    if (this.mapMode !== mode) this.controller.cancelConnection();
    this.mapMode = mode;
    this.renderGraph();
    this.fitWhenVisible();
  }

  renderOutlineMap() {
    const host = this.$('.structure-outline-map');
    const active = this.sectionsById.get(this.branchId);
    const roots = this.sectionChildren.get(null) || [];
    const selected = this.selection?.kind === 'section' ? this.selection.id : null;
    const mapping = this.workspace.mappings.find(item => item.source_ref.locator === this.activeSegment);
    const mapped = new Set(mapping?.node_ids || []);
    const oldSupplementOpen = host.querySelector('.structure-supplements')?.open;
    const scroll = host.dataset.branch === (active?.section_id || '') ? host.scrollTop : 0;
    const card = (section, parent = false) => {
      const id = section.section_id;
      const children = this.sectionChildren.get(id)?.length || 0;
      const nodes = children ? [] : this.relatedOutlineNodes(section);
      const badge = children ? `${children} 小节` : nodes.length ? `${nodes.length} 节点` : '';
      const title = section.title.match(/^(\d+(?:\.\d+)*|[A-Z])(?:[.、．]?\s+)(.+)$/);
      const linked = section.node_ids?.some(nodeId => mapped.has(nodeId));
      return `<button class="structure-chapter-card${linked ? ' source-linked' : ''}" type="button" data-map-section="${escape(id)}" aria-current="${selected === id}" aria-label="${escape(section.title)}${!parent && badge ? '，展开 ' + badge : ''}">
        ${title ? `<span class="structure-chapter-index">${escape(title[1])}</span>` : ''}
        <strong>${escape(title ? title[2] : section.title)}</strong>
        ${!parent && badge ? `<small>${escape(badge)} <span aria-hidden="true">›</span></small>` : ''}</button>`;
    };
    let content;
    if (active) {
      const children = this.sectionChildren.get(active.section_id) || [];
      const nodes = this.relatedOutlineNodes(active);
      const parent = this.sectionsById.get(active.parent_id);
      content = `<div class="structure-outline-head"><button type="button" data-editor-action="map-overview">‹ 章节总览</button>
        ${parent ? `<button type="button" data-map-section="${escape(parent.section_id)}">返回 ${escape(parent.title)}</button>` : ''}</div>
        <div class="structure-branch${children.length ? ' has-children' : ''}"><div class="structure-branch-root">${card(active, true)}</div>
        ${children.length ? `<ul class="structure-branch-children">${children.map(section => `<li>${card(section)}</li>`).join('')}</ul>` : ''}</div>
        ${nodes.length ? `<div class="structure-branch-nodes"><h3>关联脉络节点</h3>${nodes.map(node => `<button type="button" data-map-node="${escape(node.id)}">${escape(node.label)}</button>`).join('')}</div>` : ''}`;
    } else {
      const primary = [], supplemental = [];
      let foundReferences = false;
      for (const section of roots) {
        const references = /^(?:参考文献|References|Bibliography)\s*$/i.test(section.title.trim());
        if (references) foundReferences = true;
        const appendix = /^(?:附录|Appendix\b|Appendices\b)/i.test(section.title.trim()) ||
          (foundReferences && /^[A-Z][.、．]?\s/.test(section.title.trim()));
        (references || appendix ? supplemental : primary).push(section);
      }
      content = `<div class="structure-outline-head"><h3 tabindex="-1">章节总览</h3><span>${primary.length} 个主章节</span></div>
        ${primary.length ? `<ol class="structure-chapter-spine">${primary.map(section => `<li>${card(section)}</li>`).join('')}</ol>` : ''}
        ${supplemental.length ? `<details class="structure-supplements" ${oldSupplementOpen || supplemental.some(section => section.section_id === selected || section.node_ids?.some(id => mapped.has(id))) ? 'open' : ''}>
          <summary>参考文献与附录 <small>${supplemental.length}</small></summary>
          <ul>${supplemental.map(section => `<li>${card(section)}</li>`).join('')}</ul></details>` : ''}
        ${roots.length ? '' : '<p class="structure-empty-note">使用左侧的大纲添加章节。</p>'}`;
    }
    host.dataset.branch = active?.section_id || '';
    host.innerHTML = content;
    host.scrollTop = scroll;
  }

  renderGraph() {
    const full = this.mapMode === 'full';
    this.$('[data-editor-action="outline-map"]').setAttribute('aria-pressed', String(!full));
    this.$('[data-editor-action="full-map"]').setAttribute('aria-pressed', String(full));
    this.$('.structure-outline-map').hidden = full;
    this.viewport.hidden = !full;
    this.$('.structure-map-footer').hidden = !full;
    if (!full) { this.renderOutlineMap(); return; }
    const nodes = liveNodes(this.workspace);
    this.$('.structure-map-count').textContent = `${nodes.length} 个节点，${liveEdges(this.workspace).length} 条关系`;
    this.$('.structure-nodes').innerHTML = nodes.map(node => {
      const p = this.workspace.layout.positions[node.id];
      return `<div class="structure-node" data-node="${escape(node.id)}" role="button" tabindex="0" aria-label="节点：${escape(node.label)}" style="left:${p.x}px;top:${p.y}px"><button data-input="${escape(node.id)}" aria-label="连接到${escape(node.label)}" class="structure-port input"></button><strong title="${escape(node.label)}">${escape(node.label)}</strong><span>${escape(names[node.kind] || '概念')}${node.review_state === 'pending' ? ' · 待核验' : ''}</span><button data-output="${escape(node.id)}" aria-label="从${escape(node.label)}连线" class="structure-port output"></button></div>`;
    }).join('');
    const right = Math.max(900, ...nodes.map(n => this.workspace.layout.positions[n.id].x + NODE_WIDTH + 80));
    const bottom = Math.max(520, ...nodes.map(n => this.workspace.layout.positions[n.id].y + NODE_HEIGHT + 80));
    this.world.style.width = right + 'px'; this.world.style.height = bottom + 'px';
    this.$('.structure-edges').setAttribute('width', right);
    this.$('.structure-edges').setAttribute('height', bottom);
    this.world.style.zoom = this.zoom;
    this.$('.structure-zoom').textContent = Math.round(this.zoom * 100) + '%';
    this.renderEdges(); this.highlight();
  }

  renderEdges() {
    if (!this.session || this.mapMode !== 'full') return;
    const nodes = new Map(liveNodes(this.workspace).map(n => [n.id, n]));
    const positions = this.workspace.layout.positions;
    const frames = moduleFrames(this.workspace);
    this.$('.structure-groups').innerHTML = frames.map(g => `<div class="structure-group" style="left:${g.x}px;top:${g.y}px;width:${g.width}px;height:${g.height}px"><span>${escape(g.label)}</span></div>`).join('');
    const obstacles = [...nodes.keys()].map(id => ({...positions[id], x: positions[id].x - 8, width: NODE_WIDTH + 16, height: NODE_HEIGHT}));
    const edges = liveEdges(this.workspace);
    const dense = edges.length > 12;
    const occupied = [];
    let markup = '<defs><marker id="pp-structure-arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" orient="auto"><path d="M0 0L10 5L0 10Z" fill="#8291a6"/></marker></defs>';
    for (const edge of edges) {
      if (!nodes.has(edge.from) || !nodes.has(edge.to)) continue;
      const a = positions[edge.from], b = positions[edge.to];
      const from = {x: a.x + NODE_WIDTH, y: a.y + NODE_HEIGHT / 2}, to = {x: b.x, y: b.y + NODE_HEIGHT / 2};
      const label = edge.relation.slice(0, 16);
      const point = edgeLabelPosition(from, to, obstacles, occupied, Math.max(70, label.length * 12), 24);
      const hasCaption = Math.abs(point.y - point.anchor.y) * this.zoom <= 72;
      if (hasCaption) occupied.push(point.bounds);
      const opacity = edge.confidence == null ? .65 : .35 + edge.confidence * .6;
      const path = edgePath(from, to);
      const description = `${nodes.get(edge.from).label} → ${edge.relation} → ${nodes.get(edge.to).label}`;
      const subdued = dense || edge.relation === '章节归属';
      markup += `<g class="${subdued ? 'subdued' : ''}" data-edge="${escape(edge.id)}" role="button" tabindex="0" aria-label="关系：${escape(description)}" style="--edge-opacity:${opacity}">
        <title>${escape(description)}</title><path class="structure-edge-hit" d="${path}"/><path class="structure-edge-line" d="${path}" marker-end="url(#pp-structure-arrow)"/>
        ${hasCaption ? `<text x="${point.x}" y="${point.y}" text-anchor="middle" dominant-baseline="middle">${escape(label)}</text>` : ''}</g>`;
    }
    if (this.wire && positions[this.wire.from]) {
      const p = positions[this.wire.from];
      markup += `<path class="structure-wire" d="${edgePath({x: p.x + NODE_WIDTH, y: p.y + NODE_HEIGHT / 2}, this.wire.point)}"/>`;
    }
    this.$('.structure-edges').innerHTML = markup;
  }

  highlight() {
    const mapping = this.workspace.mappings.find(m => m.source_ref.locator === this.activeSegment);
    const ids = new Set(mapping?.node_ids || this.selectedNodeIds());
    const edges = new Set(mapping?.edge_ids || []);
    if (this.selection?.kind === 'edge') edges.add(this.selection.id);
    for (const element of this.$('.structure-nodes').children) element.classList.toggle('selected', ids.has(element.dataset.node));
    for (const element of this.$('.structure-edges').querySelectorAll('[data-edge]')) {
      element.classList.toggle('selected', edges.has(element.dataset.edge));
    }
  }

  renderInspector() {
    const target = this.$('.structure-inspector-body');
    for (const button of this.container.querySelectorAll('[data-inspector]')) button.setAttribute('aria-pressed', String(button.dataset.inspector === this.inspectorTab));
    if (this.inspectorTab === 'evidence') { this.renderEvidence(target); return; }
    if (!this.selection) { target.innerHTML = '<p class="structure-empty-note">选择章节或脉络节点，编辑目标并核对来源。</p>'; return; }
    const {kind, id} = this.selection;
    const section = kind === 'section' ? this.workspace.paper_outline.find(s => s.section_id === id) : null;
    const item = section || (kind === 'node' ? this.workspace.graph.nodes : this.workspace.graph.edges).find(n => n.id === id);
    if (!item || item.status === 'deleted') { this.selection = null; this.renderInspector(); return; }
    const title = section ? item.title : kind === 'node' ? item.label : item.relation;
    target.innerHTML = `<h3>${escape(section ? '章节目标' : kind === 'node' ? '论证节点' : '论证关系')}</h3><label>${section ? '章节标题' : '名称'}<input data-field="${section ? 'title' : kind === 'node' ? 'label' : 'relation'}" aria-label="${section ? '章节标题' : '节点或关系名称'}" value="${escape(title)}" maxlength="180"></label>
      <label>${section ? '写作目标' : kind === 'node' ? '简述' : '关系依据'}<textarea rows="4" data-field="${section ? 'purpose' : kind === 'node' ? 'summary' : 'relation_basis'}" aria-label="${section ? '章节写作目标' : '内容说明'}">${escape(section ? item.purpose : kind === 'node' ? item.summary : item.relation_basis)}</textarea></label>`;
    if (section) {
      const parent = document.createElement('label'); parent.textContent = '父章节';
      const select = document.createElement('select'); select.setAttribute('aria-label', '父章节');
      const descendants = new Set([id]);
      for (let count = -1; count !== descendants.size;) {
        count = descendants.size;
        this.workspace.paper_outline.filter(s => descendants.has(s.parent_id)).forEach(s => descendants.add(s.section_id));
      }
      select.innerHTML = '<option value="">根章节</option>' + this.workspace.paper_outline.filter(s => !descendants.has(s.section_id)).map(s => `<option value="${escape(s.section_id)}">${escape(s.title)}</option>`).join('');
      select.value = item.parent_id || '';
      select.onchange = () => this.change(() => { item.parent_id = select.value || null; });
      parent.append(select); target.append(parent);
      const order = document.createElement('label'); order.textContent = '同级顺序';
      const input = document.createElement('input'); input.type = 'number'; input.min = 0; input.value = item.position || 0; input.setAttribute('aria-label', '章节顺序');
      input.onchange = () => this.change(() => { item.position = Number(input.value); }); order.append(input); target.append(order);
      const bindings = document.createElement('details'); bindings.innerHTML = '<summary>关联脉络节点</summary>';
      for (const node of liveNodes(this.workspace)) {
        const label = document.createElement('label'), checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = item.node_ids?.includes(node.id);
        checkbox.onchange = () => this.change(() => { item.node_ids = checkbox.checked ? [...new Set([...(item.node_ids || []), node.id])] : item.node_ids.filter(key => key !== node.id); });
        label.append(checkbox, document.createTextNode(node.label)); bindings.append(label);
      }
      target.append(bindings);
    } else {
      if (kind === 'node') {
        const details = document.createElement('details');
        details.innerHTML = `<summary>详细说明</summary><textarea rows="5" data-field="content" aria-label="节点详细说明">${escape(item.content)}</textarea>`;
        target.append(details);
      } else {
        const endpoints = new Map(this.workspace.graph.nodes.map(node => [node.id, node.label]));
        const relation = document.createElement('p');
        relation.className = 'structure-edge-description';
        relation.textContent = `${endpoints.get(item.from)} → ${item.relation} → ${endpoints.get(item.to)}`;
        target.append(relation);
      }
      const note = document.createElement('p'); note.className = 'structure-empty-note';
      note.textContent = `${item.provenance === 'extracted' ? '来源提取' : item.provenance === 'ai_suggestion' ? 'AI 建议' : '作者编辑'}，${item.confidence == null ? '置信度待评估' : '置信度 ' + Math.round(item.confidence * 100) + '%'}。`;
      target.append(note);
      const sourceButton = document.createElement('button'); sourceButton.textContent = `查看来源依据（${item.source_refs?.length || 0}）`; sourceButton.onclick = () => { this.inspectorTab = 'evidence'; this.renderInspector(); }; target.append(sourceButton);
      const assist = document.createElement('button'); assist.textContent = '请 AI 补充'; assist.onclick = () => this.onAssist({kind, id}); target.append(assist);
      this.renderSuggestions(target, id);
    }
    const remove = document.createElement('button'); remove.textContent = section ? '删除章节' : '删除当前对象'; remove.className = 'structure-delete';
    remove.onclick = () => this.change(w => {
      if (section) {
        const ids = new Set([id]);
        for (let size = -1; size !== ids.size;) { size = ids.size; w.paper_outline.filter(s => ids.has(s.parent_id)).forEach(s => ids.add(s.section_id)); }
        if (!confirm(`删除这 ${ids.size} 个章节及其子章节？可通过撤销恢复。`)) return;
        w.paper_outline = w.paper_outline.filter(s => !ids.has(s.section_id));
      } else deleteItem(w, kind, id);
      this.selection = null;
    }); target.append(remove);
    for (const field of target.querySelectorAll('[data-field]')) field.oninput = () => this.change(() => {
      if (section) item[field.dataset.field] = field.value;
      else editItem(this.workspace, kind, id, {[field.dataset.field]: field.value});
    }, false);
  }

  renderEvidence(target) {
    const nodeIds = this.selectedNodeIds();
    const items = [...this.workspace.graph.nodes.filter(n => nodeIds.includes(n.id)), ...this.workspace.graph.edges.filter(e => e.id === this.selection?.id)];
    const refs = new Set(items.flatMap(item => (item.source_refs || []).map(ref => ref.locator)));
    if (this.selection?.kind === 'section') {
      for (const ref of this.sectionsById.get(this.selection.id)?.source_refs || []) refs.add(ref.locator);
    }
    target.innerHTML = '<h3>来源依据</h3><p class="structure-empty-note">选择片段可定位相关节点与关系。</p>';
    const segments = [...this.workspace.source.segments].sort((a, b) => Number(refs.has(b.id)) - Number(refs.has(a.id)));
    const imported = this.session.current.host.context_manifest?.manuscript_import;
    const page = segments.find(segment => segment.id === this.activeSegment)?.origin_locator?.page;
    if (imported && Number.isInteger(page)) {
      const link = document.createElement('a');
      link.href = `/api/manuscript-imports/${encodeURIComponent(imported.batch_id)}/sources/${encodeURIComponent(imported.source_id)}/original?inline=true#page=${page}`;
      link.target = '_blank'; link.rel = 'noopener';
      link.textContent = `打开论文原件第 ${page} 页`;
      target.append(link);
    }
    for (const segment of segments) {
      const button = document.createElement('button'); button.className = 'structure-source-fragment';
      button.classList.toggle('related', refs.has(segment.id)); button.classList.toggle('selected', this.activeSegment === segment.id);
      const origin = segment.origin_locator || {};
      const fields = {research_question: '研究问题', contribution: '创新构思', scope: '研究范围'};
      const originName = origin.kind === 'stage_field' ? fields[origin.field] || '阶段构思' :
        origin.kind === 'message' ? '构思讨论' : origin.kind === 'candidate' ? '创意候选' :
          origin.kind === 'paper_page' ? `论文第 ${origin.page} 页` : '原文片段';
      const title = document.createElement('small'); title.textContent = originName + (refs.has(segment.id) ? ' · 已关联' : '');
      const text = document.createElement('span'); text.textContent = segment.text; button.append(title, text);
      button.onclick = () => {
        this.activeSegment = segment.id;
        const mapping = this.workspace.mappings.find(item => item.source_ref.locator === segment.id);
        const edge = this.workspace.graph.edges.find(item => mapping?.edge_ids.includes(item.id));
        const mapped = mapping?.node_ids || [];
        const nodeId = this.selectedNodeIds().find(id => mapped.includes(id)) || mapped[0] || edge?.from;
        if (this.mapMode === 'outline') {
          const selected = this.sectionsById.get(this.selection?.id);
          const section = mapped.length ? [selected, ...this.sectionsById.values()].find(item => item?.node_ids?.includes(nodeId)) : null;
          if (section || !nodeId) {
            if (section && this.branchId !== section.section_id) this.branchId = this.sectionsById.get(section.parent_id)?.section_id || null;
            this.renderOutlineMap();
          } else this.showMap('full');
        }
        if (this.mapMode === 'full') {
          this.highlight();
          this.revealNode(nodeId);
        }
        const scroll = target.scrollTop;
        this.renderInspector();
        target.querySelector('.structure-source-fragment.selected')?.focus({preventScroll: true});
        target.scrollTop = scroll;
      };
      target.append(button);
    }
    if (!segments.length) target.append(document.createTextNode('来源尚未读取，先承接构思或导入论文。'));
    const chains = this.workspace.evidence_chains.filter(c => !nodeIds.length || c.node_ids.some(id => nodeIds.includes(id))).slice(0, 12);
    if (chains.length) {
      const details = document.createElement('details'); details.innerHTML = '<summary>相关论证路径</summary>';
      for (const chain of chains) {
        const p = document.createElement('p'); p.textContent = chain.node_ids.map(id => this.workspace.graph.nodes.find(n => n.id === id)?.label || '已删除节点').join(' → ') + (chain.status === 'broken' ? '（关系待修复）' : ''); details.append(p);
      }
      target.append(details);
    }
    if (this.selection && this.selection.kind !== 'section') {
      const item = this.selection.kind === 'node' ? this.workspace.graph.nodes.find(n => n.id === this.selection.id) : this.workspace.graph.edges.find(e => e.id === this.selection.id);
      if (item) {
        const details = document.createElement('details'); details.innerHTML = '<summary>调整当前对象的来源</summary>';
        for (const segment of segments) {
          const label = document.createElement('label'), checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = item.source_refs.some(ref => ref.locator === segment.id);
          checkbox.onchange = () => this.change(w => {
            const refs = item.source_refs.filter(ref => ref.locator !== segment.id);
            if (checkbox.checked) refs.push(sourceRef(w, segment));
            editItem(w, this.selection.kind, item.id, {source_refs: refs});
          }); label.append(checkbox, document.createTextNode(segment.text.slice(0, 70))); details.append(label);
        }
        target.append(details);
      }
    }
  }

  renderSuggestions(target, id) {
    const suggestions = this.workspace.pending_suggestions.filter(s => s.status === 'pending' &&
      ([s.target_id, s.owner_node_id, s.from, s.to].includes(id) || ![s.target_id, s.owner_node_id, s.from, s.to].some(Boolean)));
    if (!suggestions.length) return;
    const title = document.createElement('h4'); title.textContent = '待审建议'; target.append(title);
    for (const suggestion of suggestions) {
      const article = document.createElement('article'); article.className = 'structure-suggestion';
      const proposed = suggestion.proposed_fields || suggestion;
      const text = document.createElement('p');
      text.textContent = [proposed.label, proposed.relation, proposed.summary, proposed.content, proposed.relation_basis].filter(Boolean).join('\n');
      article.append(text);
      const accept = document.createElement('button'); accept.textContent = '接受'; accept.onclick = () => this.change(w => acceptSuggestion(w, suggestion.id));
      const edit = document.createElement('button'); edit.textContent = '编辑'; edit.onclick = () => {
        const proposed = suggestion.proposed_fields || suggestion;
        const field = suggestion.target_kind === 'node' ? 'summary' : 'relation_basis';
        const input = document.createElement('textarea'); input.value = proposed[field] || ''; input.setAttribute('aria-label', '修改建议内容');
        const save = document.createElement('button'); save.textContent = '保存建议'; save.onclick = () => this.change(() => { suggestion.proposed_fields = {...proposed, [field]: input.value}; suggestion.user_edited = true; }); article.append(input, save);
      };
      const reject = document.createElement('button'); reject.textContent = '拒绝'; reject.onclick = () => this.change(() => { suggestion.status = 'rejected'; });
      article.append(accept, edit, reject); target.append(article);
    }
    const batch = document.createElement('button'); batch.textContent = '接受以上建议'; batch.onclick = () => this.change(w => { const ids = suggestions.map(s => s.id); for (const id of ids) acceptSuggestion(w, id, ids); }); target.append(batch);
  }

  connect(from, to) {
    const relation = prompt('填写两个节点之间的论证关系', '支持');
    if (relation?.trim()) this.change(w => { const edge = addEdge(w, from, to, relation); this.selection = {kind: 'edge', id: edge.id}; });
  }

  fit(initial = false) {
    if (!this.session || !this.viewport.clientWidth) return;
    const nodes = liveNodes(this.workspace);
    const width = Math.max(500, ...nodes.map(n => this.workspace.layout.positions[n.id].x + NODE_WIDTH + 40));
    const height = Math.max(340, ...nodes.map(n => this.workspace.layout.positions[n.id].y + NODE_HEIGHT + 40));
    const fitWidth = (this.viewport.clientWidth - 24) / width;
    const fitAll = Math.min(1, fitWidth, (this.viewport.clientHeight - 24) / height);
    this.zoom = initial && fitAll < .45 ? Math.max(.65, Math.min(1, fitWidth)) : Math.max(.1, fitAll);
    this.renderGraph(); this.viewport.scrollLeft = 0; this.viewport.scrollTop = 0;
    if (initial) this.revealNode(this.selectedNodeIds()[0]);
  }

  handleClick(event) {
    if (!this.session) return;
    const mapSection = event.target.closest('[data-map-section]');
    if (mapSection) {
      const id = mapSection.dataset.mapSection;
      this.openSection(id);
      [...this.$('.structure-outline-map').querySelectorAll('.structure-chapter-card')].find(card => card.dataset.mapSection === id)?.focus({preventScroll: true});
      return;
    }
    const mapNode = event.target.closest('[data-map-node]');
    if (mapNode) {
      const id = mapNode.dataset.mapNode;
      this.showMap('full');
      this.select('node', id);
      this.revealNode(id);
      [...this.$('.structure-nodes').children].find(node => node.dataset.node === id)?.focus({preventScroll: true});
      return;
    }
    const section = event.target.closest('[data-section]');
    if (section) {
      const id = section.dataset.section;
      this.openSection(id);
      [...this.$('.structure-tree').children].find(item => item.dataset.section === id)?.focus({preventScroll: true});
      return;
    }
    const tab = event.target.closest('[data-inspector]');
    if (tab) { this.inspectorTab = tab.dataset.inspector; this.renderInspector(); return; }
    const action = event.target.closest('[data-editor-action]')?.dataset.editorAction;
    if (action === 'outline-map') this.showMap('outline');
    if (action === 'full-map') this.showMap('full');
    if (action === 'map-overview') {
      this.branchId = null;
      this.renderOutlineMap();
      this.$('.structure-outline-head h3')?.focus({preventScroll: true});
    }
    if (action === 'add-section') this.change(w => {
      const id = crypto.randomUUID(); w.paper_outline ||= []; w.paper_outline.push({section_id: id, parent_id: null, position: w.paper_outline.length, title: '新章节', purpose: '', scope: '', node_ids: []}); this.selection = {kind: 'section', id};
    });
    if (action === 'add-node') {
      const label = prompt('填写论点或概念名称');
      if (label?.trim()) {
        this.showMap('full');
        this.change(w => { const node = addNode(w, label, 'claim'); this.selection = {kind: 'node', id: node.id}; });
        this.revealNode(this.selection?.id);
      }
    }
    if (action === 'undo' || action === 'redo') {
      const next = this.history[action](this.workspace);
      if (next) { this.session.workspace = next; this.session.changed(); this.render(); }
    }
    if (action === 'fit') this.fit();
    if (action === 'zoom-in' || action === 'zoom-out') { this.zoom = Math.max(.1, Math.min(1.5, this.zoom + (action === 'zoom-in' ? .1 : -.1))); this.renderGraph(); }
  }
}
