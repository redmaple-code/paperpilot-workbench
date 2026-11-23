import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, rm, writeFile, readFile, copyFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createRedMapleRuntime } from '../src/full-runtime.js';
import { PlatformState } from '../src/platform.js';
import { SqliteStore } from '../src/sqlite-store.js';
import { model, assistant, call, scripted } from './fixtures.js';
import { logoPath } from '../src/branding.js';

async function fixture(t, replies = [assistant('complete')], extra = {}) {
  const cwd = await mkdtemp(join(tmpdir(), 'redmaple-full-'));
  const agentDir = join(cwd, 'agent');
  const script = scripted(replies);
  const options = { cwd, agentDir, model, streamFn: script.streamFn, inMemory: true,
    policy: { profile: 'trusted' }, settings: { retry: { enabled: false } }, ...extra };
  const runtime = await createRedMapleRuntime(options);
  const cleanups = [];
  t.after(async () => { for (const cleanup of cleanups) await cleanup(); await runtime.dispose(); await rm(cwd, { recursive: true, force: true }); });
  return { runtime, cwd, agentDir, script, options, cleanups };
}

test('full SDK uses native tools, Code Mode, themes, resources and one ledger', async t => {
  const f = await fixture(t, [assistant([call('r1', 'read', { path: 'note.txt' })], 'toolUse'), assistant('complete')]);
  await writeFile(join(f.cwd, 'note.txt'), 'source');
  assert.deepEqual(f.runtime.services.resourceLoader.getExtensions().errors, []);
  const tools = f.runtime.session.getAllTools().map(tool => tool.name);
  for (const name of ['read', 'bash', 'powershell', 'edit', 'write', 'find', 'grep', 'ls', 'codemode', 'tool_search',
    'redmaple_plan', 'redmaple_memory', 'redmaple_delegate', 'redmaple_propose', 'redmaple_image']) assert.ok(tools.includes(name), name);
  assert.ok(f.runtime.services.resourceLoader.getThemes().themes.some(theme => theme.name === 'redmaple'));
  await f.runtime.session.prompt('read the note');
  assert.deepEqual(Object.values(f.runtime.redmaple.controls.ledger.snapshot().calls).map(call => [call.kind, call.status]),
    [['model', 'succeeded'], ['tool', 'succeeded'], ['model', 'succeeded']]);
  assert.equal(f.script.requests.length, 2);
});

test('native session replacements release locks and preserve resources', async t => {
  const { runtime } = await fixture(t, [assistant('first'), assistant('second')]);
  const first = runtime.session.sessionId;
  await runtime.session.prompt('one');
  await runtime.newSession();
  assert.notEqual(first, runtime.session.sessionId);
  await runtime.session.prompt('two');
  assert.equal(Object.values(runtime.redmaple.controls.ledger.snapshot().runs).length, 1);
});

test('native resource hot reload retains runtime settings and deferred tool discovery', async t => {
  const { runtime } = await fixture(t, [assistant('reloaded')], { settings: { theme: 'redmaple-light', retry: { enabled: false } } });
  await runtime.session.reload();
  assert.equal(runtime.services.settingsManager.getThemeSetting(), 'redmaple-light');
  assert.ok(runtime.session.getActiveToolNames().includes('tool_search'));
  await runtime.session.prompt('after reload');
  assert.equal(Object.values(runtime.redmaple.controls.ledger.snapshot().calls).length, 1);
});

test('shared native ModelRuntime is metered once and restored after final owner closes', async t => {
  const first = await fixture(t, [assistant('one'), assistant('two')]);
  const shared = first.runtime.services.modelRuntime;
  const script = scripted([assistant('two'), assistant('three')]);
  const second = await createRedMapleRuntime({ ...first.options, modelRuntime: shared, streamFn: script.streamFn });
  try {
    await first.runtime.session.prompt('first');
    await second.session.prompt('second');
    assert.equal(first.script.requests.length, 1);
    assert.equal(Object.values(first.runtime.redmaple.controls.ledger.snapshot().calls).length, 1);
    assert.equal(Object.values(second.redmaple.controls.ledger.snapshot().calls).length, 1);
    await first.runtime.dispose();
    await second.session.prompt('third');
    assert.equal(Object.values(second.redmaple.controls.ledger.snapshot().calls).length, 2);
  } finally { await second.dispose(); }
});

test('runtime cleanup failures still restore the shared model transport', async t => {
  const f = await fixture(t);
  const controls = f.runtime.redmaple.controls;
  const close = controls.close.bind(controls);
  const models = f.runtime.services.modelRuntime;
  controls.close = async () => { await close(); throw new Error('cleanup fixture'); };
  try {
    await assert.rejects(f.runtime.dispose(), /Red Maple cleanup failed/);
    assert.equal(models.streamSimple, Object.getPrototypeOf(models).streamSimple);
  } finally { controls.close = close; }
});

test('workspace policy blocks external paths before native read executes', async t => {
  const { runtime } = await fixture(t, [assistant('complete')], { policy: { profile: 'workspace' } });
  await assert.rejects(runtime.session.getToolDefinition('read').execute('outside', { path: '../private' }), { code: 'outside_workspace' });
  assert.equal(Object.keys(runtime.redmaple.controls.ledger.snapshot().calls).length, 0);
});

test('money gate rejects incomplete prices before dispatch and applies explicit zero prices', async t => {
  const f = await fixture(t, [assistant('no')], { money: { amount: 1 }, pricing: { 'openai/local-test': { input: 1 } } });
  await f.runtime.session.prompt('blocked');
  assert.equal(f.script.requests.length, 0);
  assert.equal(Object.values(f.runtime.redmaple.controls.ledger.snapshot().calls)[0].status, 'failed');
  const free = await fixture(t, [assistant('yes')], { money: { amount: 1 },
    pricing: { 'openai/local-test': { input: 0, output: 0 } } });
  await free.runtime.session.prompt('allowed');
  assert.equal(free.script.requests.length, 1);
});

test('image edit preprocessing and native image generation share parent accounting', async t => {
  let inputs;
  const image = { ...model, type: 'image', id: 'image', name: 'Image fixture', api: 'fixture-images',
    provider: 'visual-fixture', input: ['text', 'image'], output: ['image'] };
  const f = await fixture(t, [assistant([call('image', 'redmaple_image', { model: 'visual-fixture/image', prompt: 'edit image', images: ['leaf.png'] })], 'toolUse'), assistant('image complete')],
    { tools: ['redmaple_image'], money: { amount: 1 }, pricing: { 'openai/local-test': { input: 0, output: 0 },
      'visual-fixture/image': { input: 0, output: 0, request: 0.02 } }, providers: {
      'visual-fixture': { baseUrl: 'http://127.0.0.1', apiKey: 'fixture', models: [image],
        images: { 'fixture-images': { async generateImages(chosen, context) {
          inputs = context.input;
          return { api: chosen.api, provider: chosen.provider, model: chosen.id, output: [{ type: 'image', data: inputs[1].data, mimeType: 'image/png' }],
            stopReason: 'stop', timestamp: Date.now(), usage: assistant('').usage };
        } } } },
    } });
  await copyFile(logoPath.replace(/\.svg$/, '.png'), join(f.cwd, 'leaf.png'));
  await f.runtime.session.prompt('edit the leaf');
  assert.equal(inputs?.[1]?.mimeType, 'image/png');
  const state = f.runtime.redmaple.controls.ledger.snapshot();
  assert.ok(Object.values(state.calls).some(call => call.purpose === 'generateImages' && call.status === 'succeeded'));
  assert.equal(Object.values(state.runs)[0].usage.cost, 0.02);
});

test('multimodal token reservations use native image estimates rather than encoded image bytes', async t => {
  const f = await fixture(t, [assistant('image inspected')], { model: { ...model, input: ['text', 'image'] } });
  await f.runtime.session.prompt('inspect the attached image', {
    images: [{ type: 'image', mimeType: 'image/png', data: 'A'.repeat(900000) }],
  });
  assert.equal(f.script.requests.length, 1);
  const state = f.runtime.redmaple.controls.ledger.snapshot();
  assert.equal(Object.values(state.runs)[0].status, 'succeeded');
  assert.equal(Object.values(state.calls)[0].status, 'succeeded');
});

test('manual native compaction uses the same model ledger and retains original session history', async t => {
  const f = await fixture(t, [assistant('one'), assistant('two'), assistant('summary with task and sources')],
    { settings: { retry: { enabled: false }, compaction: { enabled: false, keepRecentTokens: 100, reserveTokens: 1024 } } });
  await f.runtime.session.prompt('original task and evidence '.repeat(150));
  await f.runtime.session.prompt('follow-up evidence '.repeat(100));
  await f.runtime.session.compact();
  assert.equal(f.script.requests.length, 3);
  assert.equal(Object.values(f.runtime.redmaple.controls.ledger.snapshot().calls).filter(call => call.kind === 'model').length, 3);
  assert.ok(f.runtime.session.sessionManager.getEntries().some(entry => entry.type === 'compaction'));
  assert.ok(f.runtime.session.sessionManager.getEntries().some(entry => JSON.stringify(entry).includes('original task and evidence')));
});

test('SQLite session ownership, persistence and release are enforced', async t => {
  const cwd = await mkdtemp(join(tmpdir(), 'redmaple-sql-'));
  t.after(() => rm(cwd, { recursive: true, force: true }));
  const file = join(cwd, 'sessions.sqlite');
  const store = await SqliteStore.open(file, 'a');
  await assert.rejects(SqliteStore.open(file, 'a', { recoverLock: true }), { code: 'session_locked' });
  await store.update(state => { state.context.test = 'saved'; });
  await store.close();
  const reopened = await SqliteStore.open(file, 'a');
  assert.equal(reopened.snapshot().context.test, 'saved');
  await reopened.close();
});

test('unknown result reconciliation preserves the canonical native transcript', async t => {
  const f = await fixture(t, [() => { throw new Error('connection lost after send'); }, assistant('continued')]);
  await f.runtime.session.prompt('keep this original goal');
  const unknown = Object.values(f.runtime.redmaple.controls.ledger.snapshot().calls).find(call => call.status === 'unknown');
  assert.ok(unknown);
  await assert.rejects(f.runtime.session.prompt('blocked'), { code: 'result_unknown' });
  await f.runtime.redmaple.controls.reconcile(unknown.key, assistant('externally verified answer'));
  assert.ok(f.runtime.session.messages.some(message => JSON.stringify(message).includes('keep this original goal')));
  await f.runtime.session.prompt('continue with verified result');
  assert.equal(f.script.requests.length, 2);
  assert.ok(f.script.requests[1].messages.some(message => JSON.stringify(message).includes('externally verified answer')));
});

test('sourced memory requires approval, handles conflicts and excludes changed sources', async t => {
  const f = await fixture(t);
  const state = await PlatformState.open(join(f.agentDir, 'platform.sqlite'), f.cwd);
  f.cleanups.push(() => state.close());
  const path = join(f.cwd, 'note.txt');
  await writeFile(path, 'source');
  const sources = [await state.source(path)];
  const memory = await state.remember({ key: 'preference', text: 'one', sources });
  assert.equal((await state.searchMemory()).length, 0);
  await state.decideMemory(memory.id, true);
  const conflict = await state.remember({ key: 'preference', text: 'two', sources });
  assert.deepEqual(conflict.conflicts, [memory.id]);
  await state.decideMemory(conflict.id, true);
  assert.equal((await state.searchMemory())[0].text, 'two');
  await writeFile(path, 'changed');
  assert.equal((await state.searchMemory()).length, 0);
  assert.equal(state.snapshot().memories[conflict.id].status, 'stale');
});

test('workflow validates DAG, approval, optimistic versions and completion evidence', async t => {
  const f = await fixture(t);
  const state = await PlatformState.open(join(f.agentDir, 'platform.sqlite'), f.cwd);
  f.cleanups.push(() => state.close());
  assert.throws(() => state.createWorkflow({ title: 'cycle', steps: [{ id: 'a', title: 'a', dependsOn: ['b'] }, { id: 'b', title: 'b', dependsOn: ['a'] }] }), { code: 'plan_cycle' });
  const plan = state.createWorkflow({ title: 'research', steps: [{ id: 'a', title: 'collect' }, { id: 'b', title: 'review', dependsOn: ['a'], approvalRequired: true }] });
  assert.throws(() => state.transitionWorkflow(plan.id, 1, 'approve'), { code: 'approval_required' });
  state.transitionWorkflow(plan.id, 1, 'approve', { human: true });
  assert.throws(() => state.transitionWorkflow(plan.id, 1, 'start', { stepId: 'a' }), { code: 'version_conflict' });
  assert.throws(() => state.transitionWorkflow(plan.id, 2, 'start', { stepId: 'b', human: true }), { code: 'dependency_pending' });
  state.transitionWorkflow(plan.id, 2, 'start', { stepId: 'a' });
  assert.throws(() => state.transitionWorkflow(plan.id, 3, 'complete', { stepId: 'a' }), { code: 'evidence_required' });
  await state.verifyWorkflow({ id: plan.id, version: 3, stepId: 'a', evidence: 'source saved', human: true });
  state.transitionWorkflow(plan.id, 4, 'complete', { stepId: 'a' });
  assert.throws(() => state.transitionWorkflow(plan.id, 5, 'start', { stepId: 'b' }), { code: 'approval_required' });
  state.transitionWorkflow(plan.id, 5, 'start', { stepId: 'b', human: true });
  await state.verifyWorkflow({ id: plan.id, version: 6, stepId: 'b', evidence: 'reviewed', human: true });
  assert.equal(state.transitionWorkflow(plan.id, 7, 'complete', { stepId: 'b' }).status, 'completed');
});

test('proposal approval fingerprints, stale writes and undo preserve user changes', async t => {
  const f = await fixture(t);
  const state = await PlatformState.open(join(f.agentDir, 'platform.sqlite'), f.cwd);
  f.cleanups.push(() => state.close());
  const path = join(f.cwd, 'note.txt');
  await writeFile(path, 'before');
  const proposal = await state.propose({ path, content: 'after' });
  const preview = await state.preview(proposal.id);
  await assert.rejects(state.applyProposal(proposal.id, 'wrong'), { code: 'approval_changed' });
  await state.applyProposal(proposal.id, preview.approvalHash);
  await state.applyProposal(proposal.id, preview.approvalHash);
  assert.equal(await readFile(path, 'utf8'), 'after');
  await writeFile(path, 'user change');
  await assert.rejects(state.applyProposal(proposal.id, preview.approvalHash, true), { code: 'proposal_stale' });
  await writeFile(path, 'after');
  await state.applyProposal(proposal.id, preview.approvalHash, true);
  assert.equal(await readFile(path, 'utf8'), 'before');
});
