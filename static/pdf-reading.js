const $=id=>document.getElementById(id);
const params=new URLSearchParams(location.search), project=params.get('project'), job=params.get('job');
const valid=value=>/^[A-Za-z0-9_-]{1,120}$/.test(value||'');
const status=message=>{$('readerStatus').textContent=message;$('readerStatus').hidden=!message;};
const base=valid(project)&&valid(job)?`/api/projects/${project}/builds/${job}`:null;
const storageKey=`paperpilot.pdf-reading.v1:${project}:${job}`;
const leaseId='r-'+Array.from(crypto.getRandomValues(new Uint8Array(16)),x=>x.toString(16).padStart(2,'0')).join('');
let viewer,linkService,doc,metadata,ready=false,latestPosition=null,saveTimer,pendingPoint=null,locating=false,pointSequence=0;
function tellParent(type,extra={}){if(parent!==window)parent.postMessage({type,projectId:project,jobId:job,...extra},location.origin);}
function savePosition(){if(!ready||!latestPosition)return;try{localStorage.setItem(storageKey,JSON.stringify(latestPosition));}catch{status('当前浏览器无法持久保存阅读位置；PDF仍可阅读。');}}
function restorePosition(){
  let saved;try{saved=JSON.parse(localStorage.getItem(storageKey)||'null');}catch{}
  if(saved&&Number.isInteger(saved.page)&&saved.page>=1&&saved.page<=doc.numPages&&Number.isFinite(saved.scale)&&saved.scale>=.25&&saved.scale<=4&&[saved.left,saved.top].every(n=>Number.isFinite(n)&&Math.abs(n)<=1000000)){
    viewer.currentScale=saved.scale;viewer.scrollPageIntoView({pageNumber:saved.page,destArray:[null,{name:'XYZ'},saved.left,saved.top,null],allowNegativeOffset:true});
  }else viewer.currentScaleValue='page-width';
}
async function api(url,options){const response=await fetch(url,options);const result=await response.json();if(!response.ok)throw new Error(result.detail?.message||'读取失败，请检查当前构建后重试。');return result;}
async function renewLease(){if(!base)return;await api(base+'/read-lease?lease_id='+leaseId+'&persistent=true',{method:'POST'});}
async function refreshReadingInfo(){
  if(!base)return;
  try{metadata=await api(base+'/reading');status(metadata.current ? '' : '正在查看历史版本');$('locatePageSource').disabled=!ready||!metadata.source_map.available;tellParent('paperpilot:pdf-info',{reading:metadata});}
  catch(error){if(metadata)metadata.source_map.available=false;$('locatePageSource').disabled=true;status(error.message+' PDF已加载的纸页保留，定位暂停。');}
}
function setScale(scale){if(viewer&&ready)viewer.currentScale=Math.min(4,Math.max(.25,scale));}
function setPage(page){if(viewer&&ready)viewer.currentPageNumber=Math.min(doc.numPages,Math.max(1,Number(page)||1));}
async function inverse(pageNumber,point){
  if(!metadata?.source_map.available||locating)return;
  locating=true;$('locatePageSource').disabled=true;status('正在查找此PDF位置对应的固定源码…');
  try{const source=await api(base+'/source-map/inverse',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({page:pageNumber,x:point[0],y:point[1]})});status(source.message);tellParent('paperpilot:pdf-source',{source});}
  catch(error){status(error.message);}
  finally{locating=false;$('locatePageSource').disabled=!metadata?.source_map.available;}
}
function pointInPage(pageNumber,clientX,clientY){
  const pageView=viewer.getPageView(pageNumber-1),rect=pageView.div.getBoundingClientRect();
  const [x,y]=pageView.viewport.convertToPdfPoint(clientX-rect.left,clientY-rect.top);
  return [Math.max(0,x-pageView.pdfPage.view[0]),Math.max(0,pageView.pdfPage.view[3]-y)];
}
async function goToPoint(point){
  const sequence=++pointSequence;
  if(!ready){pendingPoint=point;return;}
  if(!Number.isInteger(point.page)||point.page<1||point.page>doc.numPages||![point.x,point.y].every(n=>Number.isFinite(n)&&n>=0&&n<=1000000))return;
  const pdfPage=await doc.getPage(point.page),x=pdfPage.view[0]+point.x,y=pdfPage.view[3]-point.y;
  if(sequence!==pointSequence)return;
  if(frameElement&&point.navigation!==undefined&&String(point.navigation)!==frameElement.dataset.sourceNavigation)return;
  viewer.scrollPageIntoView({pageNumber:point.page,destArray:[null,{name:'XYZ'},Math.max(pdfPage.view[0],x-48),Math.min(pdfPage.view[3],y+48),null],allowNegativeOffset:true});
  const pageView=viewer.getPageView(point.page-1),[left,top]=pageView.viewport.convertToViewportPoint(x,y);
  document.querySelectorAll('.source-location-marker').forEach(e=>e.remove());
  const marker=document.createElement('span');marker.className='source-location-marker';marker.style.left=left+'px';marker.style.top=top+'px';pageView.div.append(marker);
  status(`已定位到第${point.page}页的对应源码位置。`);
  tellParent('paperpilot:pdf-point-applied',{navigation:point.navigation});
}
window.addEventListener('message',event=>{if(event.source!==parent||event.origin!==location.origin)return;const data=event.data;if(data?.projectId!==project||data?.jobId!==job)return;if(data.type==='paperpilot:pdf-goto')goToPoint(data.point).catch(()=>status('此位置暂时不可显示，请重新定位。'));if(data.type==='paperpilot:pdf-cancel-location'){++pointSequence;pendingPoint=null;}if(data.type==='paperpilot:pdf-refresh')refreshReadingInfo();});
window.addEventListener('pagehide',event=>{savePosition();if(base&&!event.persisted)navigator.sendBeacon(base+'/read-lease-release?lease_id='+leaseId);});
document.addEventListener('visibilitychange',()=>{if(document.hidden)savePosition();else if(ready)renewLease().catch(()=>status('阅读保护暂时无法续期；请检查连接并重新打开此构建。'));});
$('previousPage').onclick=()=>{if(ready)setPage(viewer.currentPageNumber-1);};$('nextPage').onclick=()=>{if(ready)setPage(viewer.currentPageNumber+1);};
$('pageNumber').onchange=()=>setPage($('pageNumber').value);
$('zoomIn').onclick=()=>{if(ready)setScale(viewer.currentScale+.1);};$('zoomOut').onclick=()=>{if(ready)setScale(viewer.currentScale-.1);};
$('readerZoom').onchange=()=>setScale(Number($('readerZoom').value)/100);$('fitWidth').onclick=()=>{if(ready)viewer.currentScaleValue='page-width';};
document.addEventListener('keydown',event=>{if(!ready)return;if(event.altKey&&['ArrowLeft','ArrowRight'].includes(event.key)){event.preventDefault();setPage(viewer.currentPageNumber+(event.key==='ArrowRight'?1:-1));}});
$('viewerContainer').addEventListener('dblclick',event=>{const page=event.target.closest('.page');if(!page||!ready)return;const pageNumber=Number(page.dataset.pageNumber);inverse(pageNumber,pointInPage(pageNumber,event.clientX,event.clientY));});
$('locatePageSource').onclick=()=>{const pageNumber=viewer.currentPageNumber,page=viewer.getPageView(pageNumber-1).div,rect=page.getBoundingClientRect(),visible=$('viewerContainer').getBoundingClientRect();const cx=(Math.max(rect.left,visible.left)+Math.min(rect.right,visible.right))/2,cy=(Math.max(rect.top,visible.top)+Math.min(rect.bottom,visible.bottom))/2;inverse(pageNumber,pointInPage(pageNumber,cx,cy));};
try{
  if(!base)throw new Error('缺少有效论文或构建编号，请从本论文的构建历史打开PDF。');
  $('downloadPdf').href=base+'/pdf';
  const pdfjs=await import('/static/vendor/pdfjs-6.4.299/build/pdf.mjs');
  const {EventBus,PDFViewer,PDFLinkService}=await import('/static/vendor/pdfjs-6.4.299/web/pdf_viewer.mjs');
  pdfjs.GlobalWorkerOptions.workerSrc='/static/vendor/pdfjs-6.4.299/build/pdf.worker.mjs';
  const eventBus=new EventBus();linkService=new PDFLinkService({eventBus,externalLinkTarget:2,externalLinkRel:'noopener noreferrer'});
  viewer=new PDFViewer({container:$('viewerContainer'),viewer:$('viewer'),eventBus,linkService,imageResourcesPath:'/static/vendor/pdfjs-6.4.299/web/images/',enableAutoLinking:false});
  linkService.setViewer(viewer);
  eventBus.on('pagesinit',()=>{ready=true;$('locatePageSource').disabled=!metadata?.source_map.available;restorePosition();if(pendingPoint){goToPoint(pendingPoint);pendingPoint=null;}tellParent('paperpilot:pdf-ready');});
  eventBus.on('pagechanging',event=>{$('pageNumber').value=event.pageNumber;});
  eventBus.on('scalechanging',event=>{$('readerZoom').value=Math.round(event.scale*100);});
  eventBus.on('updateviewarea',event=>{if(!ready)return;const p=event.location;latestPosition={page:p.pageNumber,scale:viewer.currentScale,left:p.left,top:p.top};clearTimeout(saveTimer);saveTimer=setTimeout(savePosition,180);});
  await refreshReadingInfo();
  if(!metadata)throw new Error('构建版本信息不可用。');
  await renewLease();
  setInterval(()=>renewLease().catch(()=>status('阅读保护暂时无法续期；请检查连接并重新打开此构建。')),60000);
  const resource='/static/vendor/pdfjs-6.4.299/';
  doc=await pdfjs.getDocument({url:metadata.pdf_url,cMapUrl:resource+'cmaps/',cMapPacked:true,standardFontDataUrl:resource+'standard_fonts/',wasmUrl:resource+'wasm/',isEvalSupported:false,enableXfa:false}).promise;
  const singlePage = doc.numPages === 1;
  $('previousPage').hidden = singlePage; $('nextPage').hidden = singlePage; $('readerPageInput').hidden = singlePage;
  $('pageCount').textContent = singlePage ? '1 页' : '/ ' + doc.numPages;
  $('pageNumber').max = doc.numPages;
  viewer.setDocument(doc);linkService.setDocument(doc);
}catch(error){status('PDF查看器无法打开：'+error.message+' 可以使用“下载原PDF”，或核对任务/文件后重试。');tellParent('paperpilot:pdf-error',{message:error.message});}
