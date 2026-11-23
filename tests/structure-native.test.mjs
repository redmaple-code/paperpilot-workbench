import test from 'node:test';
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {StructureSession} from '../static/structure/session.js';
import {emptyWorkspace, normalize, addNode, editItem, applyEnrichment, targetSignature, acceptSuggestion} from '../static/structure/graph.js';

function storage() {
  const items = new Map();
  return {getItem: key => items.get(key) ?? null, setItem: (key, value) => items.set(key, value), removeItem: key => items.delete(key)};
}

function fixture() {
  let workspace = normalize(emptyWorkspace());
  workspace.source.content_hash = 'synthetic';
  workspace.paper_outline = [{section_id: 'section', title: '引言', purpose: '', node_ids: []}];
  let version = 1, layoutRevision = 0, semanticWrites = 0, layoutWrites = 0;
  const receipts = new Map(), calls = [];
  let loseSemantic = false, loseLayout = false, holdSemantic = null;
  const api = async (path, body, method) => {
    calls.push({path, body: structuredClone(body), method});
    if (method === 'GET') return path.endsWith('/layout') ? {revision: layoutRevision, positions: structuredClone(workspace.layout.positions)} :
      {version, workspace: structuredClone(workspace), host: {layout_revision: layoutRevision}};
    const key = path + ':' + body.request_id;
    if (receipts.has(key)) return structuredClone(receipts.get(key));
    if (path.endsWith('/layout')) {
      if (body.expected_layout_revision !== layoutRevision) throw Object.assign(new Error('layout conflict'), {status: 409});
      layoutWrites++;
      workspace.layout.positions = structuredClone(body.positions);
      const result = {revision: ++layoutRevision, positions: structuredClone(body.positions)};
      receipts.set(key, result);
      if (loseLayout) { loseLayout = false; throw Object.assign(new Error('lost layout response'), {status: 503}); }
      return result;
    }
    if (body.expected_workspace_revision !== version) throw Object.assign(new Error('semantic conflict'), {status: 409});
    semanticWrites++;
    workspace = structuredClone(body.commands[0].workspace);
    if (holdSemantic) await holdSemantic;
    // Model the server's normalized lock/version fields and sorted JSON keys.
    const result = JSON.parse(JSON.stringify({version: ++version, workspace}, (_, value) => value && typeof value === 'object' && !Array.isArray(value) ?
      Object.fromEntries(Object.keys(value).sort().map(key => [key, value[key]])) : value));
    receipts.set(key, result);
    if (loseSemantic) { loseSemantic = false; throw Object.assign(new Error('lost semantic response'), {status: 503}); }
    return result;
  };
  return {api, calls, counts: () => ({semanticWrites, layoutWrites, version}),
    loseSemantic: () => { loseSemantic = true; }, loseLayout: () => { loseLayout = true; },
    hold: promise => { holdSemantic = promise; }, remoteMove: () => { layoutRevision++; workspace.layout.positions['node'] = {x: 90, y: 95}; }};
}

async function session(f = fixture(), cache = storage()) {
  const s = new StructureSession({projectId: 'paper', workspaceId: 'workspace', api: f.api, storage: cache});
  await s.load();
  return {s, f, cache};
}

test('chapter and graph edits use one semantic write and retain bound object identities', async () => {
  const {s, f} = await session();
  const section = s.workspace.paper_outline[0];
  section.purpose = '第一版目标';
  const node = addNode(s.workspace, '研究问题');
  s.changed(); await s.save();
  assert.equal(s.workspace.paper_outline[0], section);
  assert.equal(s.workspace.graph.nodes[0], node);
  section.purpose = '第二版目标'; s.changed(); await s.save();
  await s.save();
  assert.equal(f.counts().semanticWrites, 2);
  assert.equal(s.dirty, false);
  s.close();
});

test('lost semantic response replays the same request after page recovery', async () => {
  const {s, f, cache} = await session();
  addNode(s.workspace, '论点'); s.changed(); f.loseSemantic();
  await assert.rejects(s.save()); s.close();
  const recovered = (await session(f, cache)).s;
  await recovered.save();
  const writes = f.calls.filter(call => call.method === 'PUT' && !call.path.endsWith('/layout'));
  assert.deepEqual(writes[0].body, writes[1].body);
  assert.equal(f.counts().semanticWrites, 1);
  assert.equal(recovered.dirty, false);
  recovered.close();
});

test('semantic success and layout failure replay layout without another semantic write', async () => {
  const {s, f} = await session();
  addNode(s.workspace, '论点'); s.changed(); f.loseLayout();
  await assert.rejects(s.save());
  await s.save();
  assert.deepEqual(f.counts(), {semanticWrites: 1, layoutWrites: 1, version: 2});
  const layouts = f.calls.filter(call => call.path.endsWith('/layout') && call.method === 'PUT');
  assert.deepEqual(layouts[0].body, layouts[1].body);
  s.close();
});

test('layout-only editing does not increment the semantic version', async () => {
  const {s, f} = await session();
  addNode(s.workspace, '论点'); s.changed(); await s.save();
  const before = s.version;
  s.workspace.layout.positions[s.workspace.graph.nodes[0].id].x += 30; s.changed(); await s.save();
  assert.equal(s.version, before);
  assert.equal(f.counts().semanticWrites, 1);
  s.close();
});

test('edits made during a save survive the earlier response', async () => {
  const {s, f} = await session();
  let release;
  f.hold(new Promise(resolve => { release = resolve; }));
  s.workspace.paper_outline[0].purpose = '已发送目标'; s.changed();
  const saving = s.save();
  await new Promise(resolve => setImmediate(resolve));
  s.workspace.paper_outline[0].purpose = '后续手改'; s.changed();
  release(); await saving;
  assert.equal(s.workspace.paper_outline[0].purpose, '后续手改');
  await s.save();
  assert.equal(s.dirty, false);
  s.close();
});

test('layout conflict retains the original common baseline', async () => {
  const {s, f} = await session();
  const node = addNode(s.workspace, '论点'); node.id = 'node'; s.workspace.layout.positions.node = {x: 40, y: 50};
  s.changed(); await s.save();
  s.workspace.layout.positions.node = {x: 70, y: 80}; f.remoteMove(); s.changed();
  await assert.rejects(s.save());
  const compared = await s.compare();
  const conflict = compared.comparison.conflicts.find(c => c.label === '布局 / node');
  assert.deepEqual(conflict.before, {x: 40, y: 50});
  assert.deepEqual(conflict.left, {x: 70, y: 80});
  assert.deepEqual(conflict.right, {x: 90, y: 95});
  s.close();
});

test('manual clears and pending suggestions retain migrated core behavior', () => {
  const workspace = normalize(emptyWorkspace());
  const node = addNode(workspace, '问题');
  editItem(workspace, 'node', node.id, {summary: ''});
  applyEnrichment(workspace, 'node', node.id, {fields: {summary: '迟到内容'}, source_refs: [], suggested_edges: []}, targetSignature(workspace, 'node', node.id));
  assert.equal(node.summary, '');
  workspace.pending_suggestions.push({id: 'suggestion', status: 'pending', target_kind: 'node', proposed_fields: {label: '验证计划'}});
  acceptSuggestion(workspace, 'suggestion');
  assert.equal(workspace.graph.nodes.length, 2);
});

test('native page has no editor iframe, message bridge or independent canvas app', () => {
  const source = readFileSync(new URL('../static/structure-workbench-ui.js', import.meta.url), 'utf8');
  const index = readFileSync(new URL('../static/index.html', import.meta.url), 'utf8');
  assert.doesNotMatch(source, /<iframe|postMessage|structure-frame|traceify\/index|window\.X6/);
  assert.doesNotMatch(index, /outline-canvas-ui\.js/);
  assert.match(source, /new StructureEditor/);
  assert.match(source, /new StructureSession/);
});


test('legacy caches are consumed after journal recovery and never reimported', async () => {
  const {s, f, cache} = await session();
  addNode(s.workspace, '论文节点');
  const legacy = s.legacyKeys[0];
  cache.setItem(legacy, JSON.stringify({dirty: true, workspace: s.workspace, base_version: s.version}));
  s.changed(); s.close();
  const recovered = (await session(f, cache)).s;
  await recovered.save(); recovered.close();
  assert.equal(cache.getItem(legacy), null);
  const clean = (await session(f, cache)).s;
  assert.ok(!clean.dirty);
  clean.close();
});

test('legacy property drafts preserve explicit clears and source selections', async () => {
  const {s, f, cache} = await session();
  const node = addNode(s.workspace, '原节点'); node.summary = '原摘要';
  s.workspace.source.segments = [{id: 'paragraph', text: '作者原文'}];
  s.changed(); await s.save();
  const source = s.workspace.source;
  cache.setItem(s.legacyKeys[3], JSON.stringify([['draft', {
    source: `${s.workspace.id}:${source.content_hash}:${source.title}`, kind: 'node', id: node.id,
    fields: {summary: '', source_refs: ['paragraph']}
  }]]));
  s.close();
  const recovered = (await session(f, cache)).s;
  assert.equal(recovered.workspace.graph.nodes[0].summary, '');
  assert.ok(recovered.workspace.graph.nodes[0].locked_fields.includes('summary'));
  assert.equal(recovered.workspace.graph.nodes[0].source_refs[0].locator, 'paragraph');
  await recovered.save();
  assert.equal(cache.getItem(recovered.legacyKeys[3]), null);
  recovered.close();
});

test('legacy stale graph uses its historical baseline for a three-way comparison', async () => {
  const {s, f, cache} = await session();
  const baseline = structuredClone(s.workspace);
  const local = structuredClone(baseline); local.paper_outline[0].purpose = '本地目标';
  const oldVersion = s.version;
  s.workspace.paper_outline[0].title = '远端标题'; s.changed(); await s.save(); s.close();
  cache.setItem(s.legacyKeys[0], JSON.stringify({dirty: true, workspace: local, base_version: oldVersion}));
  const api = async (path, body, method) => path.endsWith('/conflicts/preview') ? {base: baseline} : f.api(path, body, method);
  const recovered = new StructureSession({projectId: 'paper', workspaceId: 'workspace', api, storage: cache});
  await recovered.load();
  const compared = await recovered.compare();
  assert.equal(compared.comparison.conflicts.length, 0);
  await recovered.resolve(compared);
  assert.equal(recovered.workspace.paper_outline[0].purpose, '本地目标');
  assert.equal(recovered.workspace.paper_outline[0].title, '远端标题');
  recovered.close();
});
