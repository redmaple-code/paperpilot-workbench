/* Pure conversation helpers, also exercised by Node's built-in test runner. */
((root) => {
  async function hash(text) {
    const bytes = new TextEncoder().encode(text);
    if (root.crypto?.subtle) {
      const digest = await root.crypto.subtle.digest('SHA-256', bytes);
      return Array.from(new Uint8Array(digest), b => b.toString(16).padStart(2, '0')).join('');
    }
    // Same SHA-256 on non-secure local-network origins; no remote hashing request.
    const k = [], h = [], prime = n => { for (let x = 2; x * x <= n; x++) if (n % x === 0) return false; return true; };
    for (let n = 2; k.length < 64; n++) if (prime(n)) {
      if (h.length < 8) h.push((Math.sqrt(n) % 1 * 0x100000000) | 0);
      k.push((Math.cbrt(n) % 1 * 0x100000000) | 0);
    }
    const size = Math.ceil((bytes.length + 9) / 64) * 64, data = new Uint8Array(size);
    data.set(bytes); data[bytes.length] = 128;
    const view = new DataView(data.buffer), bits = bytes.length * 8;
    view.setUint32(size - 8, Math.floor(bits / 0x100000000)); view.setUint32(size - 4, bits >>> 0);
    const rotr = (n, b) => (n >>> b) | (n << (32 - b));
    for (let off = 0; off < size; off += 64) {
      const w = Array.from({ length: 16 }, (_, i) => view.getInt32(off + 4 * i));
      for (let i = 16; i < 64; i++) w[i] = (w[i - 16] + (rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3)) + w[i - 7] + (rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10))) | 0;
      let [a, b, c, d, e, f, g, hh] = h;
      for (let i = 0; i < 64; i++) {
        const t1 = (hh + (rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25)) + ((e & f) ^ (~e & g)) + k[i] + w[i]) | 0;
        const t2 = ((rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22)) + ((a & b) ^ (a & c) ^ (b & c))) | 0;
        hh = g; g = f; f = e; e = (d + t1) | 0; d = c; c = b; b = a; a = (t1 + t2) | 0;
      }
      [a, b, c, d, e, f, g, hh].forEach((v, i) => h[i] = (h[i] + v) | 0);
    }
    return h.map(n => (n >>> 0).toString(16).padStart(8, '0')).join('');
  }
  const key = (pid, cid, kind) => `paperpilot.agent.v2:${encodeURIComponent(pid)}:${encodeURIComponent(cid)}:${kind}`;
  function read(pid, cid, kind, fallback = '') { try { return root.localStorage.getItem(key(pid, cid, kind)) ?? fallback; } catch { return fallback; } }
  function write(pid, cid, kind, value) { try { root.localStorage.setItem(key(pid, cid, kind), value); } catch { /* optional browser draft cache */ } }
  function targetConflict(target, current, digest) {
    if (target.project_id !== current.project_id) return '论文已变化';
    if (!target.file_path || target.file_path !== current.file_path) return '请先选择候选原目标文件';
    if (target.file_hash !== digest) return '原文件内容已变化，请比较后重新定位';
    return '';
  }
  const uid = () => root.crypto?.randomUUID?.() || `${Date.now().toString(16)}-${Array.from(root.crypto?.getRandomValues?.(new Uint32Array(4)) || [Math.random() * 2 ** 32, Math.random() * 2 ** 32], n => (n >>> 0).toString(16)).join('')}`;
  const api = { hash, read, write, targetConflict, uid };
  root.paperpilotAgentStore = api;
  if (typeof module !== 'undefined') module.exports = api;
})(globalThis);
