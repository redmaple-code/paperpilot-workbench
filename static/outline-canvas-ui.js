/* The directory and canvas share the structured outline and its node commands. */
(() => {
  const pp = window.paperpilot;
  const { Graph } = window.X6 || {};
  if (!pp || !Graph) return;
  const $ = selector => document.querySelector(selector);
  const width = 244, height = 62, column = 300, row = 78;
  const statusLabels = {todo:'待写', draft:'草稿', reviewed:'已审阅', done:'已完成'};
  let outline, projectId, selectedId, graph, viewPositions;
  let positionAtPress, selectionFromCanvas = false, saving = false, initializedView = false;

  function showStatus(message, error = false) {
    $('#ocStatus').textContent = message;
    $('#ocStatus').dataset.error = String(error);
  }

  function mount() {
    if (graph) return;
    const tree = $('#outlineTree');
    const panel = document.createElement('section');
    panel.id = 'outlineCanvas';
    panel.className = 'outline-canvas';
    panel.setAttribute('aria-label', '章节画布');
    panel.innerHTML = `
      <div class="oc-toolbar">
        <button type="button" id="ocAdd">新增节点</button>
        <button type="button" id="ocEdit" disabled>查看节点详情</button>
        <button type="button" id="ocAuto">重排视图</button>
        <div class="oc-zoom" role="group" aria-label="画布缩放">
          <button type="button" id="ocZoomOut" aria-label="缩小画布">−</button>
          <button type="button" id="ocZoomReset" title="恢复阅读比例">100%</button>
          <button type="button" id="ocZoomIn" aria-label="放大画布">+</button>
          <button type="button" id="ocFit">适应全图</button>
        </div>
        <span id="ocStatus" class="oc-status" role="status"></span>
      </div>
      <div class="oc-layout">
        <section class="oc-directory" aria-labelledby="ocDirectoryTitle">
          <h3 id="ocDirectoryTitle">章节目录 <span id="ocCount"></span></h3>
        </section>
        <div class="oc-board" id="ocBoard" aria-label="章节关系图">
          <div class="x6-host"></div>
          <p id="ocEmpty" role="status" hidden></p>
        </div>
      </div>
      <p class="oc-hint">点击目录定位节点。右键拖动画布，Ctrl 或 ⌘ + 滚轮缩放。</p>`;
    tree.before(panel);
    panel.querySelector('.oc-directory').append(tree);
    panel.querySelector('#ocBoard').addEventListener('contextmenu', event => event.preventDefault());
    const board = $('#ocBoard'), host = panel.querySelector('.x6-host');
    graph = new Graph({
      container: host, width: board.clientWidth || 1, height: board.clientHeight || 1,
      background: {color:'#f4f7fb'},
      grid: {size:16, visible:true, type:'dot', args:{color:'#cbd6e5'}},
      interacting: () => editable(),
      panning: {enabled:true, eventTypes:['rightMouseDown', 'mouseWheel']},
      mousewheel: {enabled:true, modifiers:['ctrl', 'meta'], minScale:0.12, maxScale:2},
      connecting: {
        anchor:'center', connectionPoint:'boundary', allowBlank:false, allowLoop:false, allowMulti:false,
        validateMagnet: ({magnet}) => editable() && magnet.getAttribute('port-group') === 'out',
        createEdge: () => graph.createEdge(edgeStyle())
      }
    });
    window.__outlineGraph = graph;
    new ResizeObserver(() => {
      if (board.clientWidth && board.clientHeight) graph.resize(board.clientWidth, board.clientHeight);
    }).observe(board);
    graph.on('scale', ({sx}) => { $('#ocZoomReset').textContent = Math.round(sx * 100) + '%'; });
    graph.on('node:mousedown', ({node}) => { positionAtPress = {id:node.id, ...node.getPosition()}; });
    graph.on('node:mouseup', ({node}) => {
      const start = positionAtPress;
      positionAtPress = null;
      const position = node.getPosition();
      if (start?.id === node.id && (Math.round(start.x) !== Math.round(position.x) || Math.round(start.y) !== Math.round(position.y))) {
        moveNode(node.id, position);
      }
    });
    graph.on('node:click', ({node}) => {
      selectionFromCanvas = true;
      treeButton(node.id)?.click();
      selectionFromCanvas = false;
    });
    graph.on('node:dblclick', ({node}) => {
      treeButton(node.id)?.click();
      editNode();
    });
    graph.on('edge:connected', ({edge, isNew}) => {
      if (!isNew) return;
      const source = edge.getSourceCellId(), target = edge.getTargetCellId();
      graph.removeEdge(edge);
      if (source && target && source !== target) updateNode(target, {parent_id:source, position:0}, '已设置父节点');
    });

    $('#ocAdd').onclick = () => $('#outlineNodeTitle').focus();
    $('#ocEdit').onclick = editNode;
    $('#ocAuto').onclick = () => {
      viewPositions = layout(outline.nodes);
      render();
      readingScale(true);
      showStatus('已重排当前视图');
    };
    $('#ocFit').onclick = () => {
      graph.zoomToFit({padding:28, maxScale:1});
      graph.centerContent();
    };
    $('#ocZoomOut').onclick = () => graph.zoomTo(Math.max(0.12, graph.zoom() / 1.25));
    $('#ocZoomIn').onclick = () => graph.zoomTo(Math.min(2, graph.zoom() * 1.25));
    $('#ocZoomReset').onclick = () => readingScale();
  }

  function editable() {
    const project = pp.current?.project;
    return !!outline && project?.id === projectId && !project.archived && project.lifecycle_state !== 'published' && !saving;
  }

  function treeButton(id) {
    return id ? $(`#outlineTree .outline-node-select[data-node-id="${CSS.escape(id)}"]`) : null;
  }

  function editNode() {
    if (selectedId) $('#outlineDetailTitle').focus();
  }

  function layout(nodes) {
    const siblings = new Map(), points = new Map();
    for (const node of nodes) {
      const parent = node.parent_id || '';
      if (!siblings.has(parent)) siblings.set(parent, []);
      siblings.get(parent).push(node);
    }
    let nextRow = 0;
    function placeChildren(parent, depth) {
      for (const node of (siblings.get(parent) || []).sort((a, b) => a.position - b.position)) {
        points.set(node.id, {x:depth * column, y:nextRow});
        nextRow += row;
        placeChildren(node.id, depth + 1);
        if (depth === 0) nextRow += 12;
      }
    }
    placeChildren('', 0);
    return points;
  }

  function edgeStyle() {
    return {
      router:{name:'orth'},
      connector:{name:'rounded', args:{radius:8}},
      attrs:{line:{stroke:'#859ab6', strokeWidth:1.4, targetMarker:{name:'block', width:7, height:5}}},
      zIndex:0
    };
  }

  function nodeStyle(node, point) {
    const active = node.id === selectedId, root = !node.parent_id;
    const port = (position, magnet) => ({
      position, attrs:{circle:{r:4, magnet, stroke:'#47628c', strokeWidth:1.25, fill:'#fff'}}
    });
    return {
      id:node.id, ...point, width, height, zIndex:1,
      markup:[{tagName:'rect', selector:'body'}, {tagName:'text', selector:'label'}, {tagName:'text', selector:'meta'}],
      attrs:{
        body:{refWidth:'100%', refHeight:'100%', rx:7, ry:7, fill:active ? '#e6efff' : root ? '#edf3ff' : '#fff',
          stroke:active ? '#2e53a4' : root ? '#8fa9d0' : '#bbc9da', strokeWidth:active ? 2 : 1.2, cursor:'pointer'},
        label:{text:node.title, fill:'#182942', fontSize:14, fontWeight:root ? 650 : 500, refX:13, refY:8,
          textAnchor:'start', textVerticalAnchor:'top', textWrap:{width:-26, height:38, ellipsis:true}},
        meta:{text:statusLabels[node.status] || node.status || '待写', fill:'#526682', fontSize:11,
          refX:13, refY:46, textAnchor:'start', textVerticalAnchor:'top', pointerEvents:'none'}
      },
      ports:{
        groups:{in:port('left', 'passive'), out:port('right', true)},
        items:[{id:'in', group:'in'}, {id:'out', group:'out'}]
      }
    };
  }

  function readingScale(fromStart = false) {
    graph.zoomTo(1);
    const selected = !fromStart && selectedId && graph.getCellById(selectedId);
    if (selected) graph.centerCell(selected);
    else graph.positionPoint(graph.getContentArea().getOrigin(), 24, 24);
  }

  function render() {
    const nodes = outline?.nodes || [];
    const positions = layout(nodes);
    graph.clearCells();
    for (const node of nodes) {
      const fallback = positions.get(node.id);
      const point = viewPositions?.get(node.id) || {
        x:node.canvas_x ?? fallback.x, y:node.canvas_y ?? fallback.y
      };
      graph.addNode(nodeStyle(node, point));
    }
    for (const node of nodes) {
      if (!node.parent_id) continue;
      graph.addEdge({
        ...edgeStyle(), id:`edge-${node.id}`,
        source:{cell:node.parent_id, port:'out'}, target:{cell:node.id, port:'in'}
      });
    }
    $('#ocCount').textContent = outline ? `${nodes.length} 节 · v${outline.version}` : '';
    $('#ocEmpty').hidden = !!nodes.length;
    $('#ocEmpty').textContent = outline ? '还没有章节节点' : '正在读取章节节点…';
    $('#ocAdd').disabled = !editable();
    $('#ocEdit').disabled = !selectedId;
    for (const id of ['ocZoomIn', 'ocZoomOut', 'ocZoomReset', 'ocFit', 'ocAuto']) $(`#${id}`).disabled = !nodes.length;
    if (!initializedView && nodes.length) {
      initializedView = true;
      readingScale(true);
    }
  }

  async function updateNode(id, fields, message) {
    if (!editable()) return;
    const project = projectId;
    saving = true;
    showStatus('正在保存…');
    try {
      await pp.updateOutlineFromCanvas(id, fields);
      if (project === projectId) showStatus(message);
    } catch (error) {
      if (project === projectId) {
        render();
        showStatus(error.message, true);
      }
    } finally {
      saving = false;
      if (graph) $('#ocAdd').disabled = !editable();
    }
  }

  function moveNode(id, point) {
    const node = outline?.nodes.find(item => item.id === id);
    if (!node) return;
    viewPositions?.delete(id);
    const position = outline.nodes.filter(item => item.id !== id && item.parent_id === node.parent_id
      && graph.getCellById(item.id).getPosition().y < point.y).length;
    updateNode(id, {canvas_x:Math.round(point.x), canvas_y:Math.round(point.y), position}, '位置已保存');
  }

  function showCanvas() {
    const anchor = $('#outlineTree');
    if ($('.workspace')?.dataset.view !== 'outline' || !anchor.closest('.ws-task-page')?.getClientRects().length) return;
    mount();
    render();
  }

  document.addEventListener('paperpilot:outline-node-selected', ({detail:{nodeId}}) => {
    if (!graph) return;
    const previous = selectedId;
    selectedId = nodeId;
    for (const id of new Set([previous, nodeId])) {
      const model = outline?.nodes.find(item => item.id === id);
      const cell = graph.getCellById(id);
      if (!model || !cell) continue;
      cell.attr('body', nodeStyle(model, {}).attrs.body);
    }
    $('#ocEdit').disabled = false;
    if (!selectionFromCanvas) {
      const cell = graph.getCellById(nodeId);
      if (cell) {
        if (graph.zoom() < 0.85) graph.zoomTo(1);
        graph.centerCell(cell);
      }
    }
  });
  document.addEventListener('paperpilot:project-changed', ({detail}) => {
    projectId = detail.projectId;
    outline = selectedId = viewPositions = null;
    initializedView = false;
    $('#outlineTree').replaceChildren();
    if (graph) {
      render();
      showStatus('');
    }
  });
  function syncOutline(snapshot) {
    if (!snapshot?.graph || snapshot.projectId !== pp.current?.project?.id) return;
    projectId = snapshot.projectId;
    outline = snapshot.graph;
    selectedId = snapshot.selectedId;
    showCanvas();
    if ($('#ocStatus')?.dataset.error === 'true') showStatus('');
  }
  document.addEventListener('paperpilot:outline-refreshed', event => syncOutline(event.detail));
  document.addEventListener('paperpilot:outline-load-failed', ({detail}) => {
    if (!graph || detail.projectId !== pp.current?.project?.id) return;
    if (!outline) $('#ocEmpty').textContent = '章节节点读取失败，可用下方「刷新树」重试。';
    showStatus(detail.message, true);
  });
  document.addEventListener('paperpilot:view-changed', showCanvas);
  pp.refreshOutlineCanvas = () => syncOutline(pp.structuredOutlineSnapshot?.());
  syncOutline(pp.structuredOutlineSnapshot?.());
  showCanvas();
})();
