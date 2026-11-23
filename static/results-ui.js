(() => {
  const pp = window.paperpilot;
  const panel = document.querySelector("#resultsWorkspace");
  if (!pp || !panel) return;
  const $ = id => document.getElementById(id);
  const escape = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"})[c]);
  const names = {source_table:"来源表", pipeline:"对话整理", select:"选取列", drop_blank:"删除含空值的行", deduplicate:"删除重复行", filter:"按数值范围筛选", summarize:"按组统计"};
  const hints = {
    select: "按原表顺序保留勾选的列。",
    drop_blank: "任意列为空或只含空白的行会被移除。",
    deduplicate: "逐列比较整行内容，保留首次出现的行。",
    filter: "至少填写一个边界。只保留位于范围内且该列数值有效的行。",
    summarize: "按分组列的原值计数，并计算数值列的均值、最小值和最大值。请先处理无效数值。"
  };
  const state = {project:pp.data?.project?.id || "", dataset:null, version:null, preview:null, requested:null, sequence:0, job:null, pending:{}, collectionChoices:{}, collectionsKey:""};
  const root = () => "/api/projects/" + encodeURIComponent(state.project) + "/results";
  const versionPath = (datasetId, versionId) => "/" + encodeURIComponent(datasetId) + "/versions/" + encodeURIComponent(versionId);
  const currentPath = () => versionPath(state.dataset.id, state.version.id);
  const visible = () => !panel.closest(".ws-task-page")?.hidden && !document.getElementById("visual").hidden;
  const readOnly = () => pp.data?.project?.lifecycle_state === "published";
  const post = (url, body) => pp.api(url, {method:"POST", body:JSON.stringify(body)});

  function status(value, error = false) {
    $("resultStatus").textContent = value;
    $("resultStatus").dataset.error = String(error);
  }

  function errorText(error) {
    try {
      const detail = JSON.parse(error.message).detail;
      if (Array.isArray(detail)) return "有输入值不符合要求，请检查文件或表单字段。";
      return detail?.message || detail || error.message;
    } catch {
      return error.message;
    }
  }

  function syncControls() {
    const busy = Boolean(state.job);
    const blocked = busy || !state.project;
    $("newResultDataset").disabled = blocked || readOnly();
    $("refreshResultDatasets").disabled = blocked;
    $("appendResultVersion").disabled = blocked || readOnly() || !state.dataset;
    $("resultUploadFields").disabled = blocked || readOnly();
    $("closeResultImport").disabled = blocked;
    $("resultDatasetSelect").disabled = blocked;
    $("resultVersionSelect").disabled = blocked;
    $("resultProcessPanel").hidden = readOnly();
    $("resultProcessingFields").disabled = blocked || readOnly();
    $("resultChartFields").disabled = blocked;
    $("saveResultChart").disabled = blocked || readOnly() || !state.preview;
    panel.setAttribute("aria-busy", String(busy));
  }

  function resetPreview() {
    state.preview = null;
    $("resultChartPreview").hidden = true;
    $("resultChartImage").removeAttribute("src");
    syncControls();
  }

  function closeUpload() {
    $("resultImportForm").hidden = true;
    $("resultImportForm").reset();
  }

  function showUpload(append) {
    const form = $("resultImportForm");
    form.reset();
    form.dataset.target = append ? state.dataset.id : "";
    $("resultImportHeading").textContent = append ? "追加原始版本" : "导入数据集";
    $("resultTitleField").hidden = append;
    form.hidden = false;
    $("resultFile").focus();
  }

  function requestId(scope, payload, file = null) {
    const signature = JSON.stringify(payload);
    const previous = state.pending[scope];
    if (previous?.signature === signature && previous.file === file) return previous.id;
    const id = crypto.randomUUID();
    state.pending[scope] = {signature, file, id};
    return id;
  }

  async function perform(label, action) {
    if (state.job || !state.project) return;
    const job = {project:state.project};
    const valid = () => state.job === job && pp.data?.project?.id === job.project;
    state.job = job;
    syncControls();
    status(label);
    try {
      await action(valid);
    } catch (error) {
      if (valid()) status(errorText(error), true);
    } finally {
      if (state.job === job) {
        state.job = null;
        syncControls();
        if (state.requested) loadOnOpen();
      }
    }
  }

  function versionLabel(version) {
    return "v" + version.version_no + " · " + (version.operation ? names[version.operation.kind] : "原始数据");
  }

  function fill(select, values, selected) {
    select.innerHTML = values.map(([value, label]) => '<option value="' + escape(value) + '">' + escape(label) + '</option>').join("");
    if (values.some(([value]) => value === selected)) select.value = selected;
  }

  function renderVersion() {
    const version = state.version;
    $("resultDetail").hidden = !version;
    if (!version) {
      for (const id of ["resultVersionHeading", "resultVersionFacts", "resultProvenance", "resultTable",
                        "resultPreviewCount", "resultSelectedColumns", "resultMetric", "resultGroup",
                        "resultChartX", "resultChartY", "resultImageList"]) $(id).replaceChildren();
      $("resultDownload").removeAttribute("href");
      return;
    }
    $("resultVersionHeading").textContent = state.dataset.title + " · " + versionLabel(version);
    $("resultVersionFacts").textContent =
      version.row_count.toLocaleString() + " 行 · " + version.columns.length + " 列 · " + new Date(version.created_at).toLocaleString();
    $("resultDownload").href = version.download_url;

    const source = state.dataset.versions.find(item => item.id === version.parent_version_id);
    const records = [["文件名", version.filename], ["SHA-256", version.sha256]];
    if (source) records.push(["父版本", versionLabel(source)]);
    if (version.note) records.push(["备注", version.note]);
    const op = version.operation;
    if (op?.kind === "select") records.push(["保留列", op.columns.join("、")]);
    if (op?.kind === "filter") records.push(["筛选范围", op.column + " · " + (op.minimum ?? "无下限") + " ～ " + (op.maximum ?? "无上限")]);
    if (op?.kind === "summarize") records.push(["统计设置", "按 " + op.group_by + " 分组，统计 " + op.column]);
    if (op?.kind === "pipeline") records.push(["处理步骤", op.steps.map(step => names[step.kind]).join(" → ")]);
    $("resultProvenance").innerHTML = records.map(([label,value]) => "<dt>" + escape(label) + "</dt><dd>" + escape(value) + "</dd>").join("");

    if (op?.kind === "source_table" && op.files?.length) {
      const label = document.createElement("dt");
      label.textContent = "原始文件";
      const files = document.createElement("dd");
      files.append(...op.files.map(file => originalLink(op.collection_id, file)));
      $("resultProvenance").append(label, files);
    }

    const headings = version.columns.map(column => '<th scope="col">' + escape(column) + '</th>').join("");
    const rows = version.rows.map(row => "<tr>" + row.map(cell => "<td>" + escape(cell) + "</td>").join("") + "</tr>").join("");
    $("resultTable").innerHTML = "<table><thead><tr>" + headings + "</tr></thead><tbody>" + rows + "</tbody></table>";
    $("resultPreviewCount").textContent = version.rows.length === version.row_count ?
      "预览 " + version.rows.length + " 行" : "预览前 " + version.rows.length + " 行，共 " + version.row_count.toLocaleString() + " 行";

    const columns = version.columns.map(column => [column, column]);
    fill($("resultMetric"), columns, version.columns[1]);
    fill($("resultGroup"), columns);
    fill($("resultChartX"), columns);
    fill($("resultChartY"), columns, version.columns[1]);
    $("resultSelectedColumns").innerHTML = version.columns.map(column =>
      '<label><input type="checkbox" value="' + escape(column) + '" checked>' + escape(column) + '</label>').join("");
    $("resultChartPanel").hidden = columns.length < 2;
    renderImages(version);
    updateOperation();
  }

  function renderImages(version) {
    $("resultLinkedImages").hidden = !version.images.length;
    $("resultImageList").replaceChildren(...version.images.map(image => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = image.title + " · 图像 v" + image.version_no;
      button.onclick = async () => {
        try {
          await pp.openVisualAsset?.(image.asset_id, image.version_id, {
            archived:image.asset_status === "archived", message:"正在查看由该结果版本生成的图像。"
          });
        } catch (error) {
          status(errorText(error), true);
        }
      };
      return button;
    }));
  }


  function sourceUrl(collectionId, sourceId) {
    return root() + "/collections/" + encodeURIComponent(collectionId) + "/sources/" + encodeURIComponent(sourceId);
  }

  function originalLink(collectionId, item) {
    const link = document.createElement("a");
    link.href = sourceUrl(collectionId, item.id);
    link.download = "";
    link.textContent = item.path;
    link.title = "SHA-256 " + item.sha256;
    return link;
  }

  function showCollectionItem(item, plot = false) {
    perform("正在读取所选实验版本…", async valid => {
      await loadList(item.dataset_id, item.version_id);
      if (!valid()) return;
      if (state.version?.id !== item.version_id) throw new Error("无法读取该实验记录固定的数据版本。");
      if (plot && item.plot) {
        $("resultChartX").value = item.plot.x;
        $("resultChartY").value = item.plot.y;
        $("resultChartPanel").open = true;
        $("resultChartPanel").scrollIntoView({block:"start"});
        $("resultChartX").focus({preventScroll:true});
      } else {
        $("resultVersionHeading").scrollIntoView({block:"start"});
        $("resultVersionHeading").focus({preventScroll:true});
      }
      status("");
    });
  }

  function renderCollections(collections) {
    const host = $("resultCollections");
    host.hidden = !collections.length;
    host.replaceChildren(...collections.map(collection => {
      const section = document.createElement("section");
      section.className = "result-collection";
      section.setAttribute("aria-label", collection.title);
      section.innerHTML =
        '<div class="result-toolbar result-collection-heading"><h4></h4><p class="muted"></p></div>' +
        '<p class="result-collection-note" hidden></p>' +
        '<div class="result-fields result-collection-fields">' +
        '<label>算例分组<select class="result-collection-group"></select></label>' +
        '<label>实验数据<select class="result-collection-item"></select></label></div>' +
        '<p class="result-collection-scope muted" hidden></p>' +
        '<div class="visual-asset-actions">' +
        '<button type="button" class="result-collection-read">查看数据版本</button>' +
        '<button type="button" class="result-collection-plot">打开绘图</button>' +
        '<button type="button" class="result-collection-figure">查看论文原图</button></div>' +
        '<details class="result-collection-originals"><summary></summary><ul></ul></details>';
      const get = selector => section.querySelector(selector);
      get("h4").textContent = collection.title;
      const count = collection.groups.reduce((total, group) => total + group.datasets.length, 0);
      get(".result-collection-heading p").textContent = count + " 个数据表 · " + collection.sources.length + " 份原件";
      if (collection.mismatches?.length) {
        const note = get(".result-collection-note");
        note.hidden = false;
        note.textContent = "来源核对发现 " + collection.mismatches.map(row =>
          row.case + " N=" + row.cells + " 的 CSV 与 NPZ 有 " + row.count + " 个值不同").join("、") +
          "。图 5 使用论文脚本指定的 NPZ，差异清单和 CSV 可在「来源核对」查看。";
      }
      const groupSelect = get(".result-collection-group");
      const itemSelect = get(".result-collection-item");
      const read = get(".result-collection-read");
      const draw = get(".result-collection-plot");
      const figure = get(".result-collection-figure");
      const remembered = state.collectionChoices[collection.id];
      fill(groupSelect, collection.groups.map(group => [group.id, group.title]), remembered?.group);
      function updateButtons() {
        const group = collection.groups.find(group => group.id === groupSelect.value);
        const item = group.datasets[itemSelect.selectedIndex];
        state.collectionChoices[collection.id] = {group:group.id, item:item?.key};
        const scope = get(".result-collection-scope");
        scope.textContent = group.description || "";
        scope.hidden = !scope.textContent;
        read.disabled = !item;
        draw.hidden = !item?.plot;
        figure.hidden = !group.figure;
        read.onclick = () => showCollectionItem(item);
        draw.onclick = () => showCollectionItem(item, true);
        figure.onclick = async () => {
          try {
            await pp.openVisualAsset(group.figure.asset_id, group.figure.version_id, {
              archived:group.figure.status === "archived", message:"正在查看该算例对应的论文原图。"
            });
          } catch (error) { status(errorText(error), true); }
        };
      }
      function updateGroup(selected) {
        const group = collection.groups.find(group => group.id === groupSelect.value);
        fill(itemSelect, group.datasets.map(item => [item.key, item.title + " · v" + item.version_no]), selected);
        updateButtons();
      }
      groupSelect.onchange = () => updateGroup();
      itemSelect.onchange = updateButtons;
      updateGroup(remembered?.item);
      get("summary").textContent = "下载实验原件（" + collection.sources.length + "）";
      get("ul").replaceChildren(...collection.sources.map(item => {
        const li = document.createElement("li");
        li.append(originalLink(collection.id, item));
        return li;
      }));
      return section;
    }));
  }

  function selectVersion(version) {
    state.version = version;
    $("resultProcessForm").reset();
    $("resultChartForm").reset();
    $("resultProcessPanel").open = false;
    $("resultChartPanel").open = false;
    $("resultProvenance").closest("details").open = false;
    resetPreview();
    renderVersion();
  }

  async function loadList(datasetId = state.dataset?.id, versionId = state.version?.id) {
    const sequence = ++state.sequence;
    const endpoint = root();
    const active = () => sequence === state.sequence && pp.data?.project?.id === state.project;
    const [{datasets}, {collections}] = await Promise.all([pp.api(endpoint), pp.api(endpoint + "/collections")]);
    if (!active()) return;
    const firstExperiment = datasetId ? null : collections[0]?.groups[0]?.datasets[0];
    const selected = datasets.find(item => item.id === (datasetId || firstExperiment?.dataset_id)) || datasets[0];
    const dataset = selected ? await pp.api(endpoint + "/" + selected.id) : null;
    if (!active()) return;
    const choice = dataset?.versions.find(item => item.id === (versionId || firstExperiment?.version_id)) || dataset?.versions[0];
    const detail = choice ? await pp.api(endpoint + versionPath(dataset.id, choice.id)) : null;
    if (!active()) return;
    const changed = detail?.id !== state.version?.id;
    state.dataset = dataset;
    const collectionKey = JSON.stringify(collections);
    if (state.collectionsKey !== collectionKey) {
      renderCollections(collections);
      state.collectionsKey = collectionKey;
    }
    $("resultEmpty").hidden = Boolean(dataset);
    $("resultBody").hidden = !dataset;
    fill($("resultDatasetSelect"), datasets.map(item => [item.id, item.title]), selected?.id);
    fill($("resultVersionSelect"), (dataset?.versions || []).map(item => [item.id, versionLabel(item)]), choice?.id);
    if (changed) selectVersion(detail);
    else if (detail) {
      state.version = detail;
      renderImages(detail);
    }
    syncControls();
  }

  function updateOperation() {
    const kind = $("resultOperation").value;
    $("resultSelectedColumns").hidden = kind !== "select";
    $("resultMetricField").hidden = !["filter", "summarize"].includes(kind);
    $("resultGroupField").hidden = kind !== "summarize";
    $("resultRangeFields").hidden = kind !== "filter";
    $("resultOperationHint").textContent = hints[kind];
  }

  function operation() {
    const kind = $("resultOperation").value;
    if (kind === "select") {
      const columns = Array.from($("resultSelectedColumns").querySelectorAll("input:checked"), field => field.value);
      if (!columns.length) throw new Error("请至少保留一列。");
      return {kind, columns};
    }
    if (kind === "filter") return {kind, column:$("resultMetric").value, minimum:$("resultMinimum").value.trim() || null, maximum:$("resultMaximum").value.trim() || null};
    if (kind === "summarize") return {kind, column:$("resultMetric").value, group_by:$("resultGroup").value};
    return {kind};
  }

  function chartConfig() {
    return {
      kind:$("resultChartType").value, title:$("resultChartTitle").value,
      x_column:$("resultChartX").value, y_column:$("resultChartY").value,
      x_label:$("resultChartXLabel").value, y_label:$("resultChartYLabel").value,
    };
  }

  function readFile(file) {
    if (!file || !file.size || file.size > 5 * 1024 * 1024) throw new Error("请选择非空且不超过 5 MiB 的 CSV 或 TSV。");
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(",")[1]);
      reader.onerror = () => reject(new Error("读取文件失败，请重新选择。"));
      reader.readAsDataURL(file);
    });
  }

  $("newResultDataset").onclick = () => showUpload(false);
  $("appendResultVersion").onclick = () => showUpload(true);
  $("closeResultImport").onclick = () => {
    const source = $("resultImportForm").dataset.target ? "appendResultVersion" : "newResultDataset";
    closeUpload();
    $(source).focus();
  };
  $("refreshResultDatasets").onclick = () => perform("正在刷新结果数据…", async valid => {
    await loadList();
    if (valid()) status("结果数据已刷新。");
  });

  $("resultDatasetSelect").onchange = event => {
    const datasetId = event.target.value;
    closeUpload();
    perform("正在读取数据集…", async valid => {
      try {
        await loadList(datasetId, "");
        if (valid()) status("");
      } catch (error) {
        if (valid()) $("resultDatasetSelect").value = state.dataset?.id || "";
        throw error;
      }
    });
  };

  $("resultVersionSelect").onchange = event => {
    const versionId = event.target.value;
    perform("正在读取版本…", async valid => {
      try {
        const detail = await pp.api(root() + versionPath(state.dataset.id, versionId));
        if (valid()) {
          selectVersion(detail);
          status("");
        }
      } catch (error) {
        if (valid()) $("resultVersionSelect").value = state.version.id;
        throw error;
      }
    });
  };

  $("resultImportForm").onsubmit = event => {
    event.preventDefault();
    const target = event.currentTarget.dataset.target;
    perform("正在校验并保存原始文件…", async valid => {
      const endpoint = root() + (target ? "/" + target + "/versions" : "");
      const file = $("resultFile").files[0];
      const title = target ? "" : $("resultTitle").value.trim();
      const note = $("resultUploadNote").value;
      const content = await readFile(file);
      if (!valid()) return;
      const body = {filename:file.name, title, note};
      const saved = await post(endpoint, {...body, content_base64:content, request_id:requestId("upload", {...body, target}, file)});
      if (!valid()) return;
      await loadList(saved.dataset_id, saved.id);
      if (valid()) {
        delete state.pending.upload;
        closeUpload();
        status("原始数据已保存为 v" + saved.version_no + "。");
      }
    });
  };

  $("resultOperation").onchange = updateOperation;
  $("resultProcessForm").onsubmit = event => {
    event.preventDefault();
    perform("正在处理数据…", async valid => {
      const path = currentPath();
      const body = {operation:operation(), note:$("resultProcessNote").value};
      const saved = await post(root() + path + "/derive", {...body, request_id:requestId("derive", {...body, path})});
      if (!valid()) return;
      await loadList(saved.dataset_id, saved.id);
      if (valid()) {
        delete state.pending.derive;
        status("处理结果已保存为 v" + saved.version_no + "。");
      }
    });
  };

  $("resultChartForm").addEventListener("input", resetPreview);
  $("resultChartForm").addEventListener("change", resetPreview);
  $("resultChartForm").onsubmit = event => {
    event.preventDefault();
    perform("正在渲染图表预览…", async valid => {
      resetPreview();
      const chart = chartConfig();
      const path = currentPath();
      const preview = await post(root() + path + "/chart-preview", chart);
      if (!valid()) return;
      state.preview = {chart, path, sha256:preview.sha256};
      $("resultChartImage").src = "data:image/svg+xml;charset=utf-8," + encodeURIComponent(preview.svg);
      $("resultChartCaption").textContent = "数据 v" + preview.source.version_no + " · " + preview.source.point_count.toLocaleString() + " 个点";
      $("resultChartPreview").hidden = false;
      status("请核对图表预览。");
    });
  };

  $("saveResultChart").onclick = () => perform("正在保存图表并生成论文可用图像…", async valid => {
    const {chart, path, sha256} = state.preview;
    const body = {chart, preview_sha256:sha256};
    const saved = await post(root() + path + "/chart", {...body, request_id:requestId("chart", {...body, path})});
    if (!valid()) return;
    delete state.pending.chart;
    resetPreview();
    status("图表已保存，可在图像库查看。");
    await pp.openVisualAsset?.(saved.asset.id, saved.version.id);
  });

  pp.openResultVersion = source => {
    state.requested = {datasetId:source.dataset_id, versionId:source.version_id};
    pp.openWorkspace?.("visual/results");
  };

  function loadOnOpen() {
    if (!visible() || !state.project || state.job) return;
    const requested = state.requested;
    state.requested = null;
    perform("正在读取结果数据…", async valid => {
      await loadList(requested?.datasetId, requested?.versionId);
      if (valid()) status(readOnly() ? "论文已发布，当前可查看和下载数据。" : "");
    });
  }

  document.addEventListener("paperpilot:project-changed", event => {
    state.project = event.detail.projectId;
    state.dataset = null;
    state.requested = null;
    state.sequence++;
    state.job = null;
    state.pending = {};
    state.collectionChoices = {};
    state.collectionsKey = "";
    renderCollections([]);
    $("resultBody").hidden = true;
    $("resultEmpty").hidden = true;
    $("resultDatasetSelect").replaceChildren();
    $("resultVersionSelect").replaceChildren();
    closeUpload();
    selectVersion(null);
    status("");
    syncControls();
  });
  document.addEventListener("paperpilot:project-loaded", event => {
    if (!state.project) state.project = event.detail.projectId;
    if (readOnly()) closeUpload();
    syncControls();
    if (!state.dataset) loadOnOpen();
  });
  document.addEventListener("paperpilot:view-changed", loadOnOpen);
  syncControls();
  loadOnOpen();
})();
