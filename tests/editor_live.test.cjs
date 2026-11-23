const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const { randomUUID } = require('node:crypto');

function fixture(saveHook) {
  const elements = new Map();
  function element(value = '') {
    return { value, textContent: '', hidden: false, callbacks: {},
      addEventListener(name, callback) { this.callbacks[name] = callback; },
      after(node) { elements.set('#' + node.id, node); } };
  }
  const editor = element('original');
  elements.set('#editor', editor);
  elements.set('#fileSelect', element('main.tex'));
  elements.set('#editorSaveStatus', element());
  const file = { path: 'main.tex', version: 1, content: 'original' };
  const project = { id: 'A', archived: false, lifecycle_state: 'draft' };
  const store = { version: 1, content: 'original', requests: new Map() };
  const calls = [];
  const timers = new Set();
  const storage = new Map();
  const pp = { current: { project }, data: { project, files: [file] },
    async api(path, options) {
      const body = JSON.parse(options?.body || '{}');
      if (!path.endsWith('/editor/save')) return {};
      calls.push({ endpoint: path, ...body });
      const old = store.requests.get(body.request_id);
      if (old) return old;
      if (saveHook) await saveHook(body, store);
      if (body.base_version !== store.version) {
        const error = new Error('内容版本已变化'); error.status = 409; throw error;
      }
      store.version += 1; store.content = body.content;
      const result = { version: store.version };
      store.requests.set(body.request_id, result);
      return result;
    } };
  const context = vm.createContext({ window: { paperpilot: pp }, crypto: { randomUUID },
    document: { querySelector: name => elements.get(name), createElement: () => element(),
      addEventListener() {}, dispatchEvent() {} },
    CustomEvent: class { constructor(type, options) { this.type = type; this.detail = options?.detail; } },
    sessionStorage: { setItem: (k, v) => storage.set(k, v), getItem: k => storage.get(k), removeItem: k => storage.delete(k) },
    setTimeout: callback => { timers.add(callback); return callback; },
    clearTimeout: callback => timers.delete(callback) });
  vm.runInContext(fs.readFileSync('static/editor-live-ui.js', 'utf8'), context);
  function change(value) { editor.value = value; editor.callbacks.input(); }
  return { pp, file, store, calls, change, elements, storage };
}

test('an unchanged flush does not strand the next real author edit', async () => {
  const f = fixture();
  assert.equal(await f.pp.flushEditorChanges(), true);
  f.change('next edit');
  assert.equal(await f.pp.flushEditorChanges(), true);
  assert.equal(f.store.content, 'next edit');
  assert.equal(f.store.version, 2);
});

test('typing during an active save drains the latest text against the new version', async () => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const f = fixture(async body => { if (body.content === 'first edit') await gate; });
  f.change('first edit');
  const saving = f.pp.flushEditorChanges();
  await new Promise(resolve => setImmediate(resolve));
  f.change('latest edit'); release();
  assert.equal(await saving, true);
  assert.equal(f.store.content, 'latest edit');
  assert.deepEqual(f.calls.map(call => call.base_version), [1, 2]);
  assert.equal(f.elements.get('#editorSaveStatus').textContent, '已自动保存');
});

test('an unknown save response retries the original request identity', async () => {
  const f = fixture();
  const api = f.pp.api;
  let lose = true;
  f.pp.api = async (path, options) => {
    const result = await api(path, options);
    if (path.endsWith('/editor/save') && lose) { lose = false; throw new Error('network disconnected'); }
    return result;
  };
  f.change('once');
  assert.equal(await f.pp.flushEditorChanges(), false);
  assert.equal(await f.pp.flushEditorChanges(), true);
  assert.equal(f.store.version, 2);
  assert.equal(f.calls[0].request_id, f.calls[1].request_id);
});

test('a concurrent version conflict preserves both server and local input', async () => {
  const f = fixture();
  f.store.version = 2; f.store.content = 'another page';
  f.change('my input');
  assert.equal(await f.pp.flushEditorChanges(), false);
  assert.equal(f.store.content, 'another page');
  assert.equal(f.elements.get('#editor').value, 'my input');
  assert.equal(f.elements.get('#editorSaveRetry').textContent, '比较版本');
  assert.ok([...f.storage.values()].some(value => value.includes('my input')));
});

test('late saves stay with their original paper after a project switch', async () => {
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  const f = fixture(() => gate);
  f.change('paper A edit');
  const saving = f.pp.flushEditorChanges();
  await new Promise(resolve => setImmediate(resolve));
  const paperB = { id: 'B', archived: false, lifecycle_state: 'draft' };
  const fileB = { path: 'main.tex', version: 1, content: 'paper B original' };
  f.pp.current = { project: paperB }; f.pp.data = { project: paperB, files: [fileB] };
  f.elements.get('#editor').value = 'paper B original'; release();
  await saving;
  assert.equal(fileB.content, 'paper B original');
  assert.ok(f.calls.every(call => call.endpoint.includes('/projects/A/')));
});
