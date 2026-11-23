"""Actual PDF canvas, reader persistence and both source-map directions in UI."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time

from playwright.sync_api import sync_playwright

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'.project-to-act/docs/project-development/evidence/full-product-20261101'
parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--port',type=int,required=True)
URL=f'http://127.0.0.1:{parser.parse_args().port}'
TEXT='\\documentclass{article}\\usepackage{xeCJK}\\setCJKmainfont{Noto Serif CJK SC}\n\\begin{document}\n\\section{First page}\nFirst mapping sentence. 中文阅读。\n\\newpage\n\\section{Second page}\nSecond mapping sentence. 来源定位。\n\\end{document}\n'
SIMPLE='\\documentclass{article}\n\\begin{document}\nOther first.\n\\newpage\nOther second.\n\\end{document}\n'

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    context=browser.new_context(viewport={'width':1600,'height':1100});req=context.request
    def ok(response):
        assert response.ok,response.text()
        return response.json()
    def create(name,content=TEXT):
        pid=ok(req.post(URL+'/api/projects',data={'name':name}))['project']['id']
        wf=ok(req.get(URL+f'/api/projects/{pid}/workflow'))
        started=ok(req.post(URL+f'/api/projects/{pid}/workflow/commands',data={'command':'start','request_id':'start','base_version':wf['version']}))
        ok(req.post(URL+f'/api/projects/{pid}/human-revision',data={'workflow_version':started['version']}))
        file=next(f for f in ok(req.get(URL+f'/api/projects/{pid}'))['files'] if f['path']=='main.tex')
        ok(req.put(URL+f'/api/projects/{pid}/files',data={'path':'main.tex','base_version':file['version'],'content':content}))
        built=ok(req.post(URL+f'/api/projects/{pid}/compile',data={'request_id':'reading-baseline','purpose':'submitted'}))
        deadline=time.monotonic()+80
        while time.monotonic()<deadline:
            job=ok(req.get(URL+built['status_url']))
            if job['status'] not in {'queued','running'}:
                assert job['status']=='succeeded',job
                return pid,job
            time.sleep(.1)
        raise AssertionError('Actual TeX build did not finish')
    pid,job=create('实际PDF阅读定位');other,other_job=create('阅读状态隔离对照',SIMPLE)
    page=context.new_page();errors=[];network_failures=[]
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('requestfailed',lambda r:network_failures.append({'url':r.url,'failure':r.failure}))
    page.goto(URL);page.wait_for_load_state('networkidle')
    page.locator(f'.project-item[data-id="{pid}"]').click()
    page.wait_for_function('(id)=>paperpilot.current?.project.id===id&&paperpilot.isRevisionActive?.()',arg=pid)
    page.locator('#autoPreviewBuild').uncheck()
    def open_job(build):
        page.locator('[data-tab="preview"]').click();page.locator('#refreshBuildHistory').click()
        page.locator(f'[data-build-job="{build["id"]}"]').get_by_role('button',name='打开此 PDF',exact=True).click()
        page.wait_for_function('(job)=>document.querySelector("#pdfFrame").dataset.readingJob===job',arg=build['id'])
        frame=page.frame_locator('#pdfFrame')
        page.wait_for_function('()=>document.querySelector("#pdfFrame").contentDocument?.querySelector("#pageCount")?.textContent.trim()==="/ 2"')
        frame.locator('.page[data-page-number="1"] canvas').wait_for(state='visible')
        return frame
    frame=open_job(job)
    assert page.locator('#write').is_visible() and page.locator('#preview').is_visible()
    left,right=page.locator('#write').bounding_box(),page.locator('#preview').bounding_box()
    assert right['x']>=left['x']+left['width']-2,'Wide source/PDF must be side by side'
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+2')
    frame.locator('#pageNumber').fill('2');frame.locator('#pageNumber').press('Tab')
    frame.locator('#readerZoom').fill('125');frame.locator('#readerZoom').press('Tab')
    frame.locator('.page[data-page-number="2"] canvas').wait_for(state='visible')
    frame.locator('.textLayer').filter(has_text='来源定位').wait_for()
    time.sleep(.4)
    frame.locator('#viewerContainer').evaluate('(element)=>{element.scrollTop+=65}')
    time.sleep(.4)
    saved=frame.locator('body').evaluate('(_,key)=>JSON.parse(localStorage.getItem(key))',f'paperpilot.pdf-reading.v1:{pid}:{job["id"]}')
    assert saved['page']==2 and abs(saved['scale']-1.25)<.01,saved
    scroll=frame.locator('#viewerContainer').evaluate('e=>e.scrollTop')
    frame.locator('body').evaluate('()=>location.reload()')
    frame.locator('.page[data-page-number="2"] canvas').wait_for(state='visible')
    page.wait_for_function('()=>document.querySelector("#pdfFrame").contentDocument?.querySelector("#pageNumber")?.value==="2"')
    assert frame.locator('#readerZoom').input_value()=='125'
    assert abs(frame.locator('#viewerContainer').evaluate('e=>e.scrollTop')-scroll)<40
    frame.locator('#viewerContainer').focus();frame.locator('#viewerContainer').press('Alt+ArrowLeft')
    assert frame.locator('#pageNumber').input_value()=='1'
    frame.locator('#viewerContainer').press('Alt+ArrowRight');assert frame.locator('#pageNumber').input_value()=='2'
    # Source -> PDF goes through actual SyncTeX, not a fabricated browser point.
    page.locator('#editor').evaluate(r'(e)=>{const lines=e.value.split("\n"),pos=lines.slice(0,6).join("\n").length+1;e.setSelectionRange(pos,pos)}')
    page.locator('#sourceToPdf').click()
    page.wait_for_function('()=>document.querySelector("#pdfReaderStatus").textContent.includes("第2页")')
    frame.locator('.source-location-marker').wait_for(state='visible')
    marker=frame.locator('.source-location-marker').bounding_box()
    canvas=frame.locator('.page[data-page-number="2"] canvas').bounding_box()
    frame.locator('.page[data-page-number="2"] canvas').dblclick(position={'x':marker['x']+10-canvas['x'],'y':marker['y']+10-canvas['y']},force=True)
    page.locator('#pdfSnapshotSource').wait_for(state='visible')
    assert page.locator('#snapshotSourceText').input_value()==TEXT
    assert page.locator('#snapshotOpenCurrent').is_enabled()
    page.locator('#snapshotOpenCurrent').click()
    page.locator('#editor').wait_for(state='visible')
    assert page.locator('#editor').input_value()==TEXT
    selected_line=page.locator('#editor').evaluate(r'e=>e.value.slice(0,e.selectionStart).split("\n").length')
    assert selected_line in {6,7},selected_line
    # A different visible draft must not get a historical location applied.
    changed=TEXT.replace('Second mapping sentence.','Unsubmitted local edit.')
    forward_url=URL+f'/api/projects/{pid}/builds/{job["id"]}/source-map/forward'
    delayed={'delivered':False}
    def deliver_late(route):
        response=route.fetch()
        delayed['point']=response.json()
        delayed['navigation']=int(page.locator('#pdfFrame').get_attribute('data-source-navigation'))
        page.locator('#editor').fill(changed)
        route.fulfill(response=response)
        delayed['delivered']=True
    page.route(forward_url,deliver_late)
    page.locator('#editor').evaluate(r'(e)=>{const pos=e.value.split("\n").slice(0,3).join("\n").length+1;e.setSelectionRange(pos,pos)}')
    page.locator('#sourceToPdf').click()
    page.wait_for_function('()=>document.querySelector("#pdfReaderStatus").textContent.includes("之前的结果不会跳转")')
    page.wait_for_timeout(200)
    assert delayed['delivered'] and frame.locator('#pageNumber').input_value()=='2'
    assert page.locator('#editor').input_value()==changed
    page.evaluate('({project,job,point,navigation})=>document.querySelector("#pdfFrame").contentWindow.postMessage({type:"paperpilot:pdf-goto",projectId:project,jobId:job,point:{...point,navigation}},location.origin)',{'project':pid,'job':job['id'],'point':delayed['point'],'navigation':delayed['navigation']})
    page.wait_for_timeout(200)
    assert frame.locator('#pageNumber').input_value()=='2','A queued render point must also expire after input changes'
    page.unroute(forward_url)
    page.locator('#editor').fill(changed);page.locator('#sourceToPdf').click()
    page.wait_for_function('()=>document.querySelector("#pdfReaderStatus").textContent.includes("不一致")')
    assert page.locator('#editor').input_value()==changed
    # Read the old fixed source, independent of newer body/draft state.
    page.locator('#viewBuildSource').click();page.locator('#pdfSnapshotSource').wait_for(state='visible')
    assert page.locator('#snapshotSourceText').input_value()==TEXT
    page.locator('#snapshotSourceLine').fill('7');page.locator('#snapshotSourceLine').press('Tab')
    page.locator('#snapshotSourceToPdf').click()
    page.wait_for_function('()=>document.querySelector("#pdfReaderStatus").textContent.includes("已定位到此构建第2页")')
    frame.locator('.source-location-marker').wait_for(state='visible')
    assert page.locator('#editor').input_value()==changed
    # Screenshot the actual canvas and its frozen source in the wide layout.
    page.locator('#readingWorkspace').screenshot(path=str(OUT/'REQ-011-READING-desktop.png'))
    page.set_viewport_size({'width':390,'height':844});page.locator('[data-tab="preview"]').click()
    assert page.locator('#write').is_hidden() and page.locator('#preview').is_visible()
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+2')
    page.locator('#preview').screenshot(path=str(OUT/'REQ-011-READING-mobile.png'))
    # Remove only the generated map seal inside this isolated UI server's temp
    # directory. The real PDF stays intact; the visible action must create a
    # fresh build instead of reusing the successful job whose map was lost.
    map_path=Path(job['artifact_path']).parent/'.paperpilot-source-map.json'
    assert any(part.startswith('paperpilot-ui-') for part in map_path.parts) and map_path.is_file()
    old_pdf=req.get(URL+f'/api/projects/{pid}/builds/{job["id"]}/pdf').body()
    map_path.unlink()
    frame.locator('body').evaluate('()=>location.reload()')
    frame.locator('#readerStatus').get_by_text('没有可用位置映射',exact=False).wait_for()
    frame.locator('canvas').first.wait_for(state='visible')
    assert frame.locator('#locatePageSource').is_disabled()
    assert frame.locator('#downloadPdf').get_attribute('href').endswith('/pdf')
    page.locator('#rebuildReading').click()
    page.wait_for_function('(old)=>{const el=document.querySelector("#pdfFrame");return el.dataset.readingJob&&el.dataset.readingJob!==old}',arg=job['id'],timeout=90000)
    rebuilt_id=page.locator('#pdfFrame').get_attribute('data-reading-job')
    assert ok(req.get(URL+f'/api/projects/{pid}/builds/{rebuilt_id}/reading'))['source_map']['available']
    assert req.get(URL+f'/api/projects/{pid}/builds/{job["id"]}/pdf').body()==old_pdf
    assert next(f for f in ok(req.get(URL+f'/api/projects/{pid}'))['files'] if f['path']=='main.tex')['content']==TEXT
    assert page.locator('#editor').input_value()==changed
    # Project switch retains each reader's own position rather than reusing it.
    page.locator(f'.project-item[data-id="{other}"]').click()
    page.get_by_role('button',name='保留为草稿并切换',exact=True).click()
    page.wait_for_function('(id)=>paperpilot.current?.project.id===id',arg=other)
    page.wait_for_function('()=>!document.querySelector("#pdfFrame").getAttribute("src")')
    other_frame=open_job(other_job)
    assert other_frame.locator('#pageNumber').input_value()=='1'
    assert page.locator('#pdfSnapshotSource').is_hidden()
    # Real compiler error: open its source, with no older PDF left beside it.
    other_file=next(f for f in ok(req.get(URL+f'/api/projects/{other}'))['files'] if f['path']=='main.tex')
    bad=SIMPLE.replace('Other first.',r'\unknownReadingCommand')
    ok(req.put(URL+f'/api/projects/{other}/files',data={'path':'main.tex','base_version':other_file['version'],'content':bad}))
    broken=ok(req.post(URL+f'/api/projects/{other}/compile',data={'request_id':'error-reading','purpose':'submitted'}))
    deadline=time.monotonic()+80
    while time.monotonic()<deadline:
        failed=ok(req.get(URL+broken['status_url']))
        if failed['status'] not in {'queued','running'}:break
        time.sleep(.1)
    assert failed['status']=='failed',failed
    page.locator('#refreshBuildHistory').click()
    page.locator(f'[data-build-job="{failed["id"]}"]').get_by_role('button',name='查看错误源码',exact=False).click()
    page.locator('#pdfSnapshotSource').wait_for(state='visible')
    assert page.locator('#snapshotSourceText').input_value()==bad
    assert page.locator('#pdfFrame').get_attribute('src')=='about:blank'
    vendor=json.loads((ROOT/'static/vendor/pdfjs-6.4.299/MANIFEST.json').read_text())
    assert all(hashlib.sha256((ROOT/'static/vendor/pdfjs-6.4.299'/name).read_bytes()).hexdigest()==h for name,h in vendor['files'].items())
    checks=['actual local PDF.js canvas and selectable CJK text layer', 'two pages and wide source/PDF split', 'page/zoom/scroll restored on iframe refresh', 'keyboard page navigation', 'actual source line forward query highlights page2', 'PDF double click opens exact read-only source', 'current editor jump validates exact visible source', 'late page1 location result ignored after new input; reader remains on page2', 'unsubmitted changed input preserved and location refused', 'frozen source can locate old PDF without changing current draft', '390px tab switch and no page horizontal overflow', 'actual lost map explains fallback while real PDF renders', 'explicit rebuild makes new map without changing body, draft or old PDF', 'project switch clears old reader and source; independent page position', 'actual compiler error opens only its frozen source and clears old PDF', 'vendored local assets match pinned manifest']
    assert not errors,errors
    names=['app.py','paperpilot/adapters/latex.py','paperpilot/services/build_source_maps.py','paperpilot/services/build_snapshots.py','paperpilot/domain/schemas.py','static/pdf-reading.html','static/pdf-reading.css','static/pdf-reading.js','static/pdf-reading-ui.js','static/development-ui.js','static/index.html','static/vendor/pdfjs-6.4.299/MANIFEST.json','tests/browser_req011_reading.py','tests/ui_reading_server.py']
    (OUT/'REQ-011-READING-BROWSER.json').write_text(json.dumps({'passed':True,'time':datetime.now(timezone.utc).isoformat(),'real_tex':True,'real_synctex':True,'isolated_database':True,'pdf_renderer':'local pinned PDF.js 6.4.299','checks':checks,'browser_errors':errors,'request_failures':network_failures,'source_sha256':{n:hashlib.sha256((ROOT/n).read_bytes()).hexdigest() for n in names}},ensure_ascii=False,indent=2),encoding='utf-8')
    browser.close();print(json.dumps({'passed':True,'checks':len(checks),'saved_position':saved}))
