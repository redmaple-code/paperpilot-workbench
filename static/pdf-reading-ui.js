/* Version-bound reading/navigation. This module never applies source text. */
(() => {
  const pp=window.paperpilot;if(!pp)return;
  const $=s=>document.querySelector(s),frame=$('#pdfFrame'),write=$('#write'),preview=$('#preview');
  if(!frame||!write||!preview)return;
  const workspace=write.closest('.workspace'),wrapper=document.createElement('div');wrapper.id='readingWorkspace';write.before(wrapper);wrapper.append(write,preview);
  const currentId=()=>pp.data?.project?.id||pp.current?.project?.id||'';
  const state={project:'',job:'',reading:null,source:null,pendingPoint:null,navigation:0,pendingNavigation:null};
  const controls=document.createElement('div');controls.className='pdf-reading-controls';
  controls.innerHTML='<label><input id="pdfSplitReading" type="checkbox" checked>宽屏并排阅读</label><button id="viewBuildSource" type="button">查看此构建源码</button><button id="rebuildReading" type="button">重新编译此版本</button>';
  const status=document.createElement('p');status.id='pdfReaderStatus';status.setAttribute('role','status');status.setAttribute('aria-live','polite');status.textContent='阅读位置随当前PDF保存。';
  frame.before(controls,status);
  const sourceToPdf=document.createElement('button');sourceToPdf.id='sourceToPdf';sourceToPdf.type='button';sourceToPdf.textContent='定位光标行到PDF';write.querySelector('.editor-toolbar')?.append(sourceToPdf);
  const snapshot=document.createElement('section');snapshot.id='pdfSnapshotSource';snapshot.hidden=true;snapshot.setAttribute('aria-label','构建版本只读源码');
  snapshot.innerHTML='<h3>此构建的只读源码</h3><p id="snapshotSourceStatus" role="status"></p><div class="snapshot-source-actions"><label>文件 <select id="snapshotSourcePath"></select></label><label>行号 <input id="snapshotSourceLine" type="number" min="1" value="1"></label><button id="snapshotSourceToPdf" type="button">定位此行到PDF</button><button id="snapshotOpenCurrent" type="button">定位当前对应源码</button><button id="snapshotSourceClose" type="button">返回当前编辑器</button></div><textarea id="snapshotSourceText" readonly spellcheck="false" aria-label="对应构建版本源码（只读）"></textarea>';
  write.prepend(snapshot);
  const errorMessage=error=>{try{const d=JSON.parse(error.message);return d.detail?.message||error.message;}catch{return error.message||'读取失败，请重试。';}};
  function message(text){status.textContent=text;}
  function nextNavigation(){state.navigation+=1;frame.dataset.sourceNavigation=String(state.navigation);if(state.pendingPoint){state.pendingPoint=null;frame.contentWindow?.postMessage({type:'paperpilot:pdf-cancel-location',projectId:state.project,jobId:state.job},location.origin);}return state.navigation;}
  function invalidateNavigation(){const pending=state.pendingNavigation!==null||state.pendingPoint!==null;nextNavigation();if(pending){state.pendingNavigation=null;message('定位来源或输入已变化，之前的结果不会跳转；请重新定位或编译当前草稿。');}}
  function layout(){const active=Boolean(write.classList.contains('active')||preview.classList.contains('active'));workspace?.classList.toggle('pdf-reading-split',Boolean($('#pdfSplitReading').checked&&active));}
  async function hash(text){return [...new Uint8Array(await crypto.subtle.digest('SHA-256',new TextEncoder().encode(text)))].map(b=>b.toString(16).padStart(2,'0')).join('');}
  function postPoint(point){const bound={...point,navigation:point.navigation??state.navigation};if(bound.navigation!==state.navigation)return;state.pendingPoint=bound;frame.contentWindow?.postMessage({type:'paperpilot:pdf-goto',projectId:state.project,jobId:state.job,point:bound},location.origin);}
  function positionSource(line){const area=$('#snapshotSourceText');const rows=area.value.split('\n'),start=rows.slice(0,line-1).join('\n').length+(line>1?1:0);area.setSelectionRange(start,start+(rows[line-1]?.length||0));area.scrollTop=Math.max(0,line-5)*parseFloat(getComputedStyle(area).lineHeight);$('#snapshotSourceLine').value=line;}
  function closeSource(){snapshot.hidden=true;write.classList.remove('show-build-source');state.source=null;}
  function showSource(source){
    if(source.job_id!==state.job||currentId()!==state.project)return;
    invalidateNavigation();
    state.source=source;snapshot.hidden=false;write.classList.add('show-build-source');
    $('#snapshotSourceStatus').textContent=`论文版本${state.reading?.snapshot_revision??'—'}，${source.path} v${source.version}。${source.message}`;
    $('#snapshotSourceText').value=source.content;$('#snapshotSourceLine').max=Math.max(1,source.content.split('\n').length);
    const select=$('#snapshotSourcePath');select.replaceChildren();
    (state.reading?.source_files||[{path:source.path}]).forEach(file=>{const option=document.createElement('option');option.value=file.path;option.textContent=file.path;select.append(option);});select.value=source.path;
    $('#snapshotOpenCurrent').disabled=!source.can_open_current;
    positionSource(source.line);$('[data-tab="write"]')?.click();layout();$('#snapshotSourceText').focus({preventScroll:true});
  }
  async function fetchSource(path,line=1){
    const project=state.project,job=state.job,token=nextNavigation();
    try{const result=await pp.api(`/api/projects/${project}/builds/${job}/source-map/source`,{method:'POST',body:JSON.stringify({path,line})});if(project===currentId()&&job===state.job&&token===state.navigation)showSource(result);}
    catch(error){if(project===currentId()&&token===state.navigation)message(errorMessage(error));}
  }
  async function forward(path,line,content,source_version,source_context='editor'){
    const project=state.project,job=state.job,token=nextNavigation();
    if(!job||project!==currentId())return message('请先编译当前论文或打开本论文的历史PDF，再定位源码。');
    message('正在查找此源码行在对应PDF中的位置…');
    state.pendingNavigation=token;
    try{const source_sha256=await hash(content);if(token!==state.navigation||project!==currentId())return;const point=await pp.api(`/api/projects/${project}/builds/${job}/source-map/forward`,{method:'POST',body:JSON.stringify({path,line,column:0,source_sha256,source_version,source_context})});if(project!==currentId()||job!==state.job||token!==state.navigation)return;postPoint(point);$('[data-tab="preview"]')?.click();layout();message(`已定位到此构建第${point.page}页；PDF和源码仍绑定同一快照。`);}
    catch(error){if(project===currentId()&&job===state.job&&token===state.navigation)message(errorMessage(error));}
    finally{if(state.pendingNavigation===token)state.pendingNavigation=null;}
  }
  pp.openBuildReader=(project,job)=>{
    if(project!==currentId())return;
    invalidateNavigation();
    const url=`/static/pdf-reading.html?project=${encodeURIComponent(project)}&job=${encodeURIComponent(job)}&v=20261109-live3`;
    if(state.project!==project||state.job!==job){closeSource();state.reading=null;state.pendingPoint=null;}
    state.project=project;state.job=job;frame.dataset.readingProject=project;frame.dataset.readingJob=job;
    if($('#compileLog').textContent.startsWith('已切换论文'))$('#compileLog').textContent='正在打开所选构建PDF；正文和工作流保持不变。';
    if(frame.getAttribute('src')!==url)frame.src=url;
    else frame.contentWindow?.postMessage({type:'paperpilot:pdf-refresh',projectId:project,jobId:job},location.origin);
    message('正在打开此构建的PDF，阅读位置按论文和构建分别保存。');layout();
  };
  pp.refreshBuildReading=(project,jobs)=>{if(project!==state.project||project!==currentId())return;const selected=jobs.find(j=>j.id===state.job);if(selected&&state.reading&&selected.current!==state.reading.current)frame.contentWindow?.postMessage({type:'paperpilot:pdf-refresh',projectId:project,jobId:state.job},location.origin);};
  pp.openBuildSource=async(project,job,path,line=1)=>{
    if(project!==currentId())return;
    const token=nextNavigation();
    // Failed jobs have useful frozen source/diagnostics even without a PDF.
    if(state.project!==project||state.job!==job){closeSource();state.project=project;state.job=job;state.reading=null;state.pendingPoint=null;frame.src='about:blank';frame.dataset.readingProject=project;frame.dataset.readingJob=job;message('此任务的诊断只定位到冻结源码；不会显示另一构建的PDF。');}
    try{const result=await pp.api(`/api/projects/${project}/builds/${job}/source-map/source`,{method:'POST',body:JSON.stringify({path,line})});if(project===currentId()&&state.job===job&&token===state.navigation)showSource(result);}
    catch(error){if(project===currentId())message(errorMessage(error));}
  };
  sourceToPdf.onclick=()=>{const path=$('#fileSelect').value,area=$('#editor');const line=area.value.slice(0,area.selectionStart).split('\n').length;forward(path,line,area.value,pp.data?.files?.find(f=>f.path===path)?.version??0);};
  $('#viewBuildSource').onclick=()=>{const first=state.reading?.source_files?.find(f=>f.path==='main.tex')||state.reading?.source_files?.[0];if(first)fetchSource(first.path);else message('请先打开本论文的成功构建，或从错误诊断查看固定源码。');};
  $('#rebuildReading').onclick=async()=>{
    const project=state.project,job=state.job,button=$('#rebuildReading');
    if(!job||project!==currentId())return message('请先打开本论文的构建，再选择重新编译。');
    button.disabled=true;
    try{
      const reading=await pp.api(`/api/projects/${project}/builds/${job}/reading`);
      if(project!==currentId()||job!==state.job)return;
      if(!reading.can_rebuild_map)return message(reading.rebuild_reason);
      message('正在按此版本重新编译；旧PDF和正文会保留。');
      const result=await pp.rebuildBuildReader(project,reading);
      if(project===currentId()&&job===state.job&&result&&result.status!=='succeeded')message('重新编译未成功；旧PDF和正文保留。请查看构建历史中的诊断，修复后重试。');
    }catch(error){if(project===currentId()&&job===state.job)message(errorMessage(error));}
    finally{button.disabled=false;}
  };
  $('#pdfSplitReading').onchange=layout;
  $('#snapshotSourceClose').onclick=()=>{invalidateNavigation();closeSource();layout();};
  $('#snapshotSourcePath').onchange=()=>fetchSource($('#snapshotSourcePath').value);
  $('#snapshotSourceLine').onchange=()=>{invalidateNavigation();const line=Number($('#snapshotSourceLine').value),max=Number($('#snapshotSourceLine').max);if(Number.isInteger(line)&&line>=1&&line<=max)positionSource(line);};
  $('#snapshotSourceToPdf').onclick=()=>{if(state.source)forward(state.source.path,Number($('#snapshotSourceLine').value),state.source.content,state.source.version,'snapshot');};
  $('#snapshotOpenCurrent').onclick=async()=>{
    const saved=state.source,project=state.project,job=state.job,token=nextNavigation();if(!saved)return;
    try{
      const fresh=await pp.api(`/api/projects/${project}/builds/${job}/source-map/source`,{method:'POST',body:JSON.stringify({path:saved.path,line:Number($('#snapshotSourceLine').value)})});
      if(project!==currentId()||job!==state.job||token!==state.navigation)return;
      const target=pp.data?.files?.find(f=>f.path===fresh.path);
      if(!fresh.can_open_current||!target||target.version!==fresh.version)throw new Error('当前源码版本已变化，仅保留对应构建的只读源码。');
      if($('#fileSelect').value!==fresh.path){await pp.flushVisibleDraftForReading?.(project);if(project!==currentId()||token!==state.navigation)return;$('#fileSelect').value=fresh.path;$('#fileSelect').dispatchEvent(new Event('change',{bubbles:true}));await new Promise(resolve=>setTimeout(resolve,0));}
      if(await hash($('#editor').value)!==fresh.display_sha256)throw new Error('当前可见输入或草稿与此PDF不同，已保留输入；请在只读快照中查看，或先编译当前草稿。');
      closeSource();$('[data-tab="write"]')?.click();await new Promise(resolve=>setTimeout(resolve,0));
      if(project!==currentId()||token!==state.navigation)return;
      if(await hash($('#editor').value)!==fresh.display_sha256){showSource(fresh);const reason='当前修订草稿与该版本不同，已切回只读快照，未覆盖输入。';$('#snapshotSourceStatus').textContent=reason;message(reason);return;}
      const area=$('#editor'),rows=area.value.split('\n'),start=rows.slice(0,fresh.line-1).join('\n').length+(fresh.line>1?1:0);area.focus();area.setSelectionRange(start,start+(rows[fresh.line-1]?.length||0));area.scrollTop=Math.max(0,fresh.line-5)*parseFloat(getComputedStyle(area).lineHeight);message(`已定位到${fresh.path}第${fresh.line}行；可见源码已核对。`);
    }catch(error){if(project===currentId()&&job===state.job&&token===state.navigation){$('#snapshotSourceStatus').textContent=errorMessage(error);message(errorMessage(error));}}
  };
  $('#editor').addEventListener('input',invalidateNavigation);
  $('#fileSelect').addEventListener('change',event=>{if(event.isTrusted)invalidateNavigation();});
  window.addEventListener('message',event=>{
    if(event.origin!==location.origin||event.source!==frame.contentWindow)return;const data=event.data;
    if(!data||data.projectId!==currentId()||data.projectId!==state.project||data.jobId!==state.job||frame.dataset.readingJob!==state.job)return;
    if(data.type==='paperpilot:pdf-info'){state.reading=data.reading;message(`论文版本${data.reading.snapshot_revision}。${data.reading.source_map.message}`);}
    if(data.type==='paperpilot:pdf-source')showSource(data.source);
    if(data.type==='paperpilot:pdf-ready'){
      if($('#compileLog').textContent.startsWith('正在打开所选构建PDF'))$('#compileLog').textContent=`PDF已加载，正在阅读论文版本${state.reading?.snapshot_revision??'—'}的构建。`;
      if(state.pendingPoint)postPoint(state.pendingPoint);
    }
    if(data.type==='paperpilot:pdf-point-applied'&&data.navigation===state.pendingPoint?.navigation)state.pendingPoint=null;
    if(data.type==='paperpilot:pdf-error')message('PDF查看器未打开：'+data.message+'。可以从构建历史下载原PDF。');
  });
  document.querySelectorAll('.tabs button').forEach(button=>button.addEventListener('click',()=>setTimeout(layout,0)));
  new MutationObserver(()=>{if(!frame.getAttribute('src')){state.project='';state.job='';state.reading=null;state.pendingPoint=null;closeSource();delete frame.dataset.readingProject;delete frame.dataset.readingJob;message('请选择当前论文的构建；上一论文的阅读和定位已关闭。');layout();}}).observe(frame,{attributes:true,attributeFilter:['src']});
})();
