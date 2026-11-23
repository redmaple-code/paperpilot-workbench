const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const vm = require('node:vm');
const { TextEncoder } = require('node:util');
const source = fs.readFileSync(require('node:path').join(__dirname, '../static/agent-conversation-store.js'), 'utf8');
const context = { TextEncoder, module: { exports: {} }, localStorage: new Map() };
context.localStorage.getItem = context.localStorage.get.bind(context.localStorage);
context.localStorage.setItem = context.localStorage.set.bind(context.localStorage);
vm.runInNewContext(source, context);
const store = context.module.exports;
test('SHA256 on a non-secure origin agrees with standard UTF-8 digests', async () => {
  for (const text of ['', 'abc', '论文里的中文、🙂与LaTeX', 'x'.repeat(10000)]) {
    assert.equal(await store.hash(text), crypto.createHash('sha256').update(text).digest('hex'));
  }
});
test('draft storage is isolated by project and conversation', () => {
  store.write('A', 'one', 'draft', 'A1'); store.write('B', 'one', 'draft', 'B1'); store.write('A', 'two', 'draft', 'A2');
  assert.equal(store.read('A', 'one', 'draft'), 'A1'); assert.equal(store.read('A', 'two', 'draft'), 'A2'); assert.equal(store.read('B', 'one', 'draft'), 'B1');
});
test('old draft cards refuse a different project, file or content', () => {
  const target = { project_id: 'A', file_path: 'main.tex', file_hash: 'old' };
  assert.ok(store.targetConflict(target, { project_id: 'B', file_path: 'main.tex' }, 'old'));
  assert.ok(store.targetConflict(target, { project_id: 'A', file_path: 'other.tex' }, 'old'));
  assert.ok(store.targetConflict(target, { project_id: 'A', file_path: 'main.tex' }, 'new'));
  assert.equal(store.targetConflict(target, { project_id: 'A', file_path: 'main.tex' }, 'old'), '');
});
