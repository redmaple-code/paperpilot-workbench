/* Author edits use the versioned editor API independently of AI workflow state. */
(() => {
  const pp = window.paperpilot;
  const editor = document.querySelector('#editor');
  if (!pp || !editor) return;
  const queues = new Map();
  const currentId = () => pp.current?.project?.id || '';
  const selectedPath = () => document.querySelector('#fileSelect').value;
  const writable = () => !pp.current?.project?.archived && pp.current?.project?.lifecycle_state !== 'published';
  const uid = () => crypto.randomUUID?.() || `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const storageKey = queue => `paperpilot.editor.${queue.project}.${encodeURIComponent(queue.path)}`;
  const visible = queue => currentId() === queue.project && selectedPath() === queue.path;
  function request(path, body) {
    return pp.api(path, body ? { method: 'PUT', body: JSON.stringify(body) } : undefined);
  }
  function remember(queue, content) {
    try { sessionStorage.setItem(storageKey(queue), JSON.stringify({ content, base_version: queue.version })); } catch {}
  }
  function show(queue, message, failed = false) {
    if (!visible(queue)) return;
    const status = document.querySelector('#editorSaveStatus');
    if (!status) return;
    status.textContent = message;
    let retry = document.querySelector('#editorSaveRetry');
    if (failed && !retry) {
      retry = document.createElement('button');
      retry.id = 'editorSaveRetry'; retry.type = 'button';
      status.after(retry);
    }
    if (retry) {
      retry.hidden = !failed;
      retry.textContent = queue.conflict ? '比较版本' : '重试保存';
      retry.onclick = () => queue.conflict ? compare(queue) : flush(queue);
    }
  }
  function queueForCurrent() {
    const project = currentId();
    const file = pp.data?.files?.find(item => item.path === selectedPath());
    if (!project || !file) return null;
    const key = `${project}\n${file.path}`;
    let queue = queues.get(key);
    if (!queue) {
      queue = { project, path: file.path, version: file.version || 0, saved: file.content,
        pending: null, request: null, running: null, timer: null, conflict: false };
      queues.set(key, queue);
    } else if (!queue.running && !queue.request && queue.pending === null && !queue.conflict) {
      queue.version = file.version || 0; queue.saved = file.content;
    }
    return queue;
  }
  async function flush(queue) {
    clearTimeout(queue.timer);
    if (queue.running) return queue.running;
    if (queue.conflict) return false;
    if (!queue.request && (queue.pending === null || queue.pending === queue.saved)) {
      queue.pending = null; show(queue, '已自动保存'); return true;
    }
    queue.running = (async () => {
      try {
        while (queue.request || queue.pending !== null) {
          if (!queue.request) {
            const content = queue.pending; queue.pending = null;
            if (content === queue.saved) continue;
            queue.request = { path: queue.path, content, base_version: queue.version, request_id: uid() };
          }
          const sent = queue.request;
          show(queue, '正在保存…');
          await request(`/api/projects/${queue.project}/editor/draft`, {
            path: queue.path, base_version: sent.base_version, base_content: queue.saved, content: sent.content
          });
          const saved = await request(`/api/projects/${queue.project}/editor/save`, sent);
          queue.version = saved.version; queue.saved = sent.content; queue.request = null;
          if (currentId() === queue.project) {
            const file = pp.data?.files?.find(item => item.path === queue.path);
            if (file && (file.version || 0) <= saved.version) {
              file.content = sent.content; file.version = saved.version;
            }
          }
          if (queue.pending === sent.content) queue.pending = null;
          if (queue.pending === null) {
            try {
              const stored = JSON.parse(sessionStorage.getItem(storageKey(queue)) || 'null');
              if (stored?.content === sent.content) sessionStorage.removeItem(storageKey(queue));
            } catch {}
          }
          document.dispatchEvent(new CustomEvent('paperpilot:editor-saved', {
            detail: { project_id: queue.project, path: queue.path, version: saved.version }
          }));
        }
        show(queue, '已自动保存');
        return true;
      } catch (error) {
        queue.conflict = error.status === 409 || /content_conflict|版本已变化|draft_checkpoint_required/.test(error.message);
        show(queue, queue.conflict ? '版本有变化，请比较后保存' : '保存暂未完成，编辑已保留', true);
        return false;
      } finally { queue.running = null; }
    })();
    return queue.running;
  }
  async function compare(queue) {
    if (!visible(queue)) return;
    const latest = await request(`/api/projects/${queue.project}`).catch(() => null);
    const file = latest?.files?.find(item => item.path === queue.path);
    if (!file || !visible(queue)) return;
    const local = editor.value;
    const dialog = document.createElement('dialog'); dialog.className = 'editor-version-dialog';
    const title = document.createElement('h2'); title.textContent = '比较文件版本'; dialog.append(title);
    for (const [label, text] of [['服务器当前版本', file.content], ['本页编辑内容', local]]) {
      const heading = document.createElement('h3'); heading.textContent = label;
      const area = document.createElement('textarea'); area.readOnly = true; area.value = text;
      area.setAttribute('aria-label', label); dialog.append(heading, area);
    }
    const keep = document.createElement('button'); keep.textContent = '继续编辑';
    const save = document.createElement('button'); save.textContent = '保存本页版本';
    const close = () => { dialog.close(); dialog.remove(); };
    keep.onclick = close;
    save.onclick = async () => {
      if (!visible(queue) || editor.value !== local) { close(); return; }
      queue.version = file.version; queue.saved = file.content; queue.request = null;
      queue.conflict = false; queue.pending = local; remember(queue, local); close();
      if (await flush(queue)) pp.scheduleLivePreview?.(queue.project);
    };
    dialog.addEventListener('cancel', close, { once: true });
    dialog.append(keep, save); document.body.append(dialog); dialog.showModal();
  }
  function changed() {
    if (!writable()) return;
    const queue = queueForCurrent(); if (!queue) return;
    queue.pending = editor.value; remember(queue, editor.value);
    show(queue, queue.conflict ? '版本有变化，请比较后保存' : '正在保存…', queue.conflict);
    clearTimeout(queue.timer); queue.timer = setTimeout(() => flush(queue), 800);
    pp.scheduleLivePreview?.(queue.project);
  }
  pp.editorChanged = changed;
  pp.flushEditorChanges = async () => {
    if (!writable()) return false;
    const queue = queueForCurrent(); if (!queue) return false;
    if (editor.value !== queue.saved) { queue.pending = editor.value; remember(queue, editor.value); }
    return flush(queue);
  };
  pp.flushAllEditorChanges = async project => {
    if (project === currentId() && !(await pp.flushEditorChanges())) return false;
    const results = await Promise.all([...queues.values()].filter(queue => queue.project === project).map(flush));
    return results.every(Boolean);
  };
  const saveOther = pp.saveCurrentEdit;
  pp.saveCurrentEdit = kind => kind === 'file' ? pp.flushEditorChanges() : saveOther?.(kind);
  editor.addEventListener('input', changed);
  document.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 's') {
      event.preventDefault(); pp.flushEditorChanges();
    }
  });
  function restore() {
    const queue = queueForCurrent(); if (!queue || !writable()) return;
    try {
      const stored = JSON.parse(sessionStorage.getItem(storageKey(queue)) || 'null');
      if (stored && stored.content !== queue.saved) {
        editor.value = stored.content; queue.pending = stored.content;
        queue.conflict = stored.base_version !== queue.version;
        changed();
      } else show(queue, '已自动保存');
    } catch {}
  }
  document.addEventListener('paperpilot:project-loaded', restore);
  document.querySelector('#fileSelect').addEventListener('change', restore);
  restore();
})();
