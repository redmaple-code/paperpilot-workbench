import {normalize, editItem, sourceRef} from './graph.js';
import {compareSnapshots} from './merge.js';

const stable = value => JSON.stringify(value, (_, item) => item && typeof item === 'object' && !Array.isArray(item) ?
  Object.fromEntries(Object.keys(item).sort().map(key => [key, item[key]])) : item);
const semantic = workspace => stable([workspace.graph, workspace.paper_outline, workspace.pending_suggestions,
  workspace.layout.grouping_mode, workspace.layout.module_layout_version]);

export async function request(path, body, method = 'POST') {
  const response = await fetch(path, {method, headers: {'Content-Type': 'application/json'},
    ...(body === undefined ? {} : {body: JSON.stringify(body)})});
  const value = await response.json();
  if (!response.ok) {
    const error = new Error(value.detail?.message || value.error || '暂时无法保存，请重试。');
    error.status = response.status;
    throw error;
  }
  return value;
}

/** A project-bound state shared by chapters, graph and source inspection. */
export class StructureSession {
  constructor({projectId, workspaceId, draftId = '', api = request, storage = localStorage, onStatus = () => {}}) {
    this.projectId = projectId;
    this.workspaceId = workspaceId;
    this.draftId = draftId;
    this.api = api;
    this.storage = storage;
    this.onStatus = onStatus;
    this.root = `/api/projects/${encodeURIComponent(projectId)}/structure-workspaces/${encodeURIComponent(workspaceId)}`;
    this.path = draftId ? `${this.root}/drafts/${encodeURIComponent(draftId)}` : this.root;
    this.key = `paperpilot.structure.edit:${projectId}:${workspaceId}:${draftId}`;
    this.generation = 0;
    this.closed = false;
    this.conflicted = false;
    this.retries = 0;
    this.queue = Promise.resolve();
    const query = `project=${projectId}&workspace=${workspaceId}${draftId ? '&draft=' + draftId : ''}`;
    this.legacyKeys = ['paperpilot-traceify-v2:' + query,
      `paperpilot.structure.sections:${projectId}:${workspaceId}:${draftId}`,
      `paperpilot-traceify-request:${projectId}:${workspaceId}:${draftId}`];
    this.legacyKeys.push(this.legacyKeys[0] + '-property-drafts');
  }

  readCache(key = this.key) {
    try { return JSON.parse(this.storage.getItem(key) || 'null'); }
    catch { this.onStatus('本机恢复记录无法读取，已保留原记录。'); return null; }
  }

  persist() {
    try {
      if (!this.dirty && !this.pending) { this.storage.removeItem(this.key); return; }
      this.storage.setItem(this.key, JSON.stringify({workspace: this.workspace, base: this.base,
        version: this.version, layoutRevision: this.layoutRevision, pending: this.pending,
        generation: this.generation, dirty: this.dirty, unknownBaseline: this.unknownBaseline}));
    } catch { this.onStatus('本机存储空间不足，请保持页面打开并保存到论文。'); }
  }

  async load() {
    this.current = await this.api(this.root, undefined, 'GET');
    this.record = this.draftId ? await this.api(this.path, undefined, 'GET') : this.current;
    this.workspace = normalize(this.record.workspace);
    this.base = structuredClone(this.workspace);
    this.version = this.record.version;
    this.layoutRevision = this.current.host.layout_revision;
    const cached = this.readCache();
    if (cached?.dirty && cached.base && cached.workspace?.source?.content_hash === this.workspace.source.content_hash) {
      this.workspace = normalize(cached.workspace);
      this.base = cached.base;
      this.version = cached.version;
      this.layoutRevision = cached.layoutRevision;
      this.pending = cached.pending;
      this.unknownBaseline = cached.unknownBaseline;
      this.generation = cached.generation || 1;
      this.dirty = true;
      this.conflicted = this.version !== this.record.version && !this.pending;
      this.onStatus(this.conflicted ? '已恢复本机修改，请比较并发版本。' : '已恢复上次未保存的修改。');
    } else {
      // Import only the previous editor's recovery data, preserving the same source identity.
      const graphCache = this.readCache(this.legacyKeys[0]);
      const chapters = this.readCache(this.legacyKeys[1]);
      if (graphCache?.dirty && graphCache.workspace?.source?.content_hash === this.workspace.source.content_hash) {
        this.workspace = normalize(graphCache.workspace);
        this.version = graphCache.base_version;
        this.conflicted = this.version !== this.record.version;
        this.dirty = true;
        if (this.conflicted && !this.draftId) {
          const previous = await this.api(`${this.root}/conflicts/preview`,
            {base_revision: this.version, workspace: this.workspace});
          this.base = normalize(previous.base);
        }
        // Old draft caches have no historical baseline. Compare all differences explicitly.
        if (this.conflicted && this.draftId) this.unknownBaseline = true;
      }
      if (chapters?.sections) {
        this.workspace.paper_outline = chapters.sections;
        this.base.paper_outline = chapters.base;
        this.conflicted ||= JSON.stringify(chapters.base) !== JSON.stringify(this.record.workspace.paper_outline);
        this.dirty = true;
      }
      const pending = this.readCache(this.legacyKeys[2]);
      if (pending) {
        this.pending = {semantic: pending.semantic, path: pending.path, result: pending.result,
          initial: pending.initial, layout: pending.layout, generation: -1};
        this.conflicted = false;
        this.dirty = true;
      }
      const formDrafts = this.readCache(this.legacyKeys[3]);
      const source = this.workspace.source;
      const sourceIdentity = `${this.workspace.id}:${source.content_hash || source.id}:${source.title}`;
      for (const [, draft] of Array.isArray(formDrafts) ? formDrafts : []) {
        if (!draft || draft.source !== sourceIdentity || !['node', 'edge'].includes(draft.kind)) continue;
        const items = draft.kind === 'node' ? this.workspace.graph.nodes : this.workspace.graph.edges;
        if (!items.some(item => item.id === draft.id && item.status !== 'deleted')) continue;
        const fields = {...draft.fields};
        if (Array.isArray(fields.source_refs)) fields.source_refs = source.segments
          .filter(segment => fields.source_refs.includes(segment.id)).map(segment => sourceRef(this.workspace, segment));
        editItem(this.workspace, draft.kind, draft.id, fields);
        this.generation++;
        this.dirty = true;
      }
    }
    if (this.dirty) this.persist();
    return this;
  }

  changed() {
    this.generation++;
    this.dirty = true;
    this.persist();
    this.onStatus('修改已保留，正在自动保存。');
    clearTimeout(this.timer);
    if (!this.closed && !this.conflicted) this.timer = setTimeout(() => this.save().catch(() => {}), 600);
  }

  save() {
    clearTimeout(this.timer);
    const operation = this.queue.then(() => this.write());
    this.queue = operation.catch(() => {});
    return operation;
  }

  async finishPending() {
    const pending = this.pending;
    if (!pending.result) {
      pending.result = pending.semantic ? await this.api(pending.path, pending.semantic, 'PUT') : pending.initial;
      this.persist();
    }
    this.version = pending.result.version;
    const previousPositions = this.base.layout.positions;
    this.base = normalize(pending.result.workspace);
    this.base.layout.positions = previousPositions;
    const layout = await this.api(`${this.root}/layout`, pending.layout, 'PUT');
    this.layoutRevision = layout.revision;
    this.base.layout.positions = layout.positions;
    if (pending.generation === this.generation) {
      // Keep form-bound object identities stable while adopting server validation metadata.
      for (const [target, source, key] of [[this.workspace.graph.nodes, this.base.graph.nodes, 'id'],
        [this.workspace.graph.edges, this.base.graph.edges, 'id'], [this.workspace.paper_outline, this.base.paper_outline, 'section_id'],
        [this.workspace.pending_suggestions, this.base.pending_suggestions, 'id']]) {
        const existing = new Map((target || []).map(item => [item[key], item]));
        if (target) target.splice(0, target.length, ...source.map(item => Object.assign(existing.get(item[key]) || {}, item)));
      }
      this.workspace.layout.positions = structuredClone(this.base.layout.positions);
    }
    this.record.version = this.version;
    if (!this.draftId) this.current.version = this.version;
    this.pending = null;
    this.persist();
  }

  async write() {
    if (this.conflicted) throw new Error('存在并发修改，请先比较版本。');
    try {
      if (this.pending) await this.finishPending();
      const changed = semantic(this.workspace) !== semantic(this.base);
      const moved = JSON.stringify(this.workspace.layout.positions) !== JSON.stringify(this.base.layout.positions);
      if (changed || moved) {
        const workspace = structuredClone(this.workspace);
        this.pending = {generation: this.generation, path: this.path, initial: {version: this.version, workspace},
          semantic: changed ? (this.draftId ? {request_id: crypto.randomUUID(), expected_draft_revision: this.version, workspace} :
            {request_id: crypto.randomUUID(), expected_workspace_revision: this.version, source_hash: this.base.source.content_hash,
              commands: [{op: 'replace_graph', workspace}]}) : null,
          layout: {request_id: crypto.randomUUID(), expected_layout_revision: this.layoutRevision,
            positions: workspace.layout.positions, draft_id: this.draftId}};
        this.persist();
        await this.finishPending();
      }
      this.dirty = semantic(this.workspace) !== semantic(this.base) ||
        JSON.stringify(this.workspace.layout.positions) !== JSON.stringify(this.base.layout.positions);
      this.retries = 0;
      this.persist();
      if (!this.dirty) for (const key of this.legacyKeys) this.storage.removeItem(key);
      this.onStatus(this.dirty ? '还有修改待保存。' : `已保存 · v${this.version}`);
      if (this.dirty && !this.closed) this.timer = setTimeout(() => this.save().catch(() => {}), 300);
    } catch (error) {
      this.conflicted = error.status === 409;
      this.dirty = true;
      this.persist();
      this.onStatus(this.conflicted ? '版本冲突，修改已保留。点击“比较修改”处理。' : error.message + ' 修改已保留。');
      if (!this.closed && !this.conflicted && this.retries++ < 3) this.timer = setTimeout(() => this.save().catch(() => {}), 1500 * this.retries);
      throw error;
    }
    return this.workspace;
  }

  async compare() {
    const remote = await this.api(this.path, undefined, 'GET');
    const layout = await this.api(`${this.root}/layout`, undefined, 'GET');
    remote.workspace.layout.positions = layout.positions;
    const baseline = this.unknownBaseline ? {graph: {nodes: [], edges: [], groups: []},
      paper_outline: [], pending_suggestions: [], layout: {positions: {}}} : this.base;
    return {remote, layout, comparison: compareSnapshots(baseline, this.workspace, normalize(remote.workspace),
      {unknownBaseline: this.unknownBaseline})};
  }

  async resolve({remote, layout, comparison}) {
    this.workspace = normalize(comparison.finish());
    this.base = normalize(remote.workspace);
    this.version = remote.version;
    this.layoutRevision = layout.revision;
    this.pending = null;
    this.conflicted = false;
    this.unknownBaseline = false;
    this.changed();
    return this.save();
  }

  close() {
    this.closed = true;
    clearTimeout(this.timer);
    if (this.dirty) this.persist();
  }
}
