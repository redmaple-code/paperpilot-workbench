import test from 'node:test';
import assert from 'node:assert/strict';
import {emptyWorkspace, normalize, addNode, editItem, targetSignature, applyEnrichment, acceptSuggestion} from '../static/structure/graph.js';

import {compareSnapshots} from '../static/structure/merge.js';

test('explicitly clearing an already empty field locks it', () => {
  const workspace = normalize(emptyWorkspace());
  const node = addNode(workspace, '问题');
  editItem(workspace, 'node', node.id, {summary: ''});
  assert.ok(node.locked_fields.includes('summary'));
  const signature = targetSignature(workspace, 'node', node.id);
  applyEnrichment(workspace, 'node', node.id, {mode: 'model', fields: {summary: '迟到补充'}, source_refs: [], suggested_edges: []}, signature);
  assert.equal(node.summary, '');
});

test('changing a field invalidates a late enrichment signature', () => {
  const workspace = normalize(emptyWorkspace());
  const node = addNode(workspace, '问题');
  const signature = targetSignature(workspace, 'node', node.id);
  editItem(workspace, 'node', node.id, {content: '作者新内容'});
  assert.throws(() => applyEnrichment(workspace, 'node', node.id, {fields: {}, source_refs: [], suggested_edges: []}, signature));
});

test('node suggestions remain separate until explicitly accepted', () => {
  const workspace = normalize(emptyWorkspace());
  workspace.pending_suggestions.push({id: 's1', target_kind: 'node', suggested_action: 'add', status: 'pending', proposed_fields: {label: '验证计划', kind: 'concept', summary: '待执行'}});
  assert.equal(workspace.graph.nodes.length, 0);
  acceptSuggestion(workspace, 's1');
  assert.equal(workspace.graph.nodes[0].label, '验证计划');
  assert.equal(workspace.graph.nodes[0].review_state, 'pending');
  assert.equal(workspace.pending_suggestions[0].status, 'accepted');
});

test('suggestion dependencies are enforced before acceptance', () => {
  const workspace = normalize(emptyWorkspace());
  workspace.pending_suggestions.push({id: 's1', target_kind: 'node', status: 'pending', dependency_ids: ['s0'], proposed_fields: {label: '方法'}});
  assert.throws(() => acceptSuggestion(workspace, 's1'));
  assert.equal(workspace.graph.nodes.length, 0);
});

test('three-way merge includes object deletion, draft fields and independent positions', async () => {
  const base = normalize(emptyWorkspace());
  const node = addNode(base, '原始名称');
  base.graph.groups = [{id: 'g1', label: '模块', node_ids: [node.id]}];
  base.paper_outline = [{section_id: 's1', title: '章节', purpose: '原目标'}];
  base.layout.positions[node.id] = {x: 1, y: 2};
  const local = structuredClone(base), remote = structuredClone(base);
  local.graph.groups = [];
  remote.graph.groups[0].label = '远端模块';
  local.graph.nodes[0].summary = '本地摘要';
  remote.graph.nodes[0].label = '远端名称';
  local.paper_outline[0].purpose = '本地目标';
  remote.layout.positions[node.id] = {x: 50, y: 60};
  const compared = compareSnapshots(base, local, remote);
  assert.equal(compared.conflicts.length, 1);
  for (const conflict of compared.conflicts) conflict.object[conflict.field] = conflict.left;
  const merged = compared.finish();
  assert.deepEqual(merged.graph.groups, []);
  assert.equal(merged.graph.nodes[0].summary, '本地摘要');
  assert.equal(merged.graph.nodes[0].label, '远端名称');
  assert.equal(merged.paper_outline[0].purpose, '本地目标');
  assert.deepEqual(merged.layout.positions[node.id], {x: 50, y: 60});
});
