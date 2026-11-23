from __future__ import annotations

import base64
import io
import json
import zipfile

from PIL import Image

import app
from paperpilot.services import manuscript_apply
from paperpilot.services.project_errors import ProjectServiceError


URL = '/api/manuscript-imports'
PAPER = b'# Abstract\nWe evaluate a tabletop controller with a reproducible dataset.\n\n# Methods\nA controlled comparison uses three trials.\n\n# Glossary\nController: A tabletop device that regulates the actuator.\n'
BIB = b'@article{example2020,\n title = {A {Reproducible} Controller},\n author = {Doe, Jane and Doe, J.},\n year = {2020}\n}\n'


def ok(response):
    assert response.is_success, response.text
    return response.json()


def upload(client,bid,name,data,key=None):
    return client.post(f'{URL}/{bid}/sources',json={'request_id':key or name,'filename':name,
                       'content_base64':base64.b64encode(data).decode()})


def new(client, key='new'):
    result=ok(client.post(URL,json={'target_mode':'new','name':'Representative manuscript','request_id':key}))
    assert result['project_id'] is None
    return result['id']


def image():
    buffer=io.BytesIO()
    Image.new('RGB',(30,40),'white').save(buffer,'PNG')
    return buffer.getvalue()


def test_author_import_local_and_approved_ai_then_creates_project(client,monkeypatch):
    bid=new(client)
    for name,data in [('main.md',PAPER),('references.bib',BIB),('results.csv',b'run,score\n1,5\n'),('figure.png',image())]:
        batch=ok(upload(client,bid,name,data))
    assert {s['kind'] for s in batch['sources']}=={'text','bibliography','table','image'}
    assert not ok(client.get('/api/projects'))
    preview=ok(client.post(f'{URL}/{bid}/model-preview',json={}))
    sent=[]

    def fixture_model(action,prompt,payload,*args,**kwargs):
        sent.append(payload)
        fragments=json.loads(payload)
        assert [p['id'] for p in fragments]==[f'F{i}' for i in range(1,len(fragments)+1)]
        assert all(p.keys()=={'id','name','location','heading','text'} for p in fragments)
        source=next(p for p in fragments if 'controller with' in p['text'])
        bib=next(p for p in fragments if p['name']=='references.bib')
        quote='We evaluate a tabletop controller with a reproducible dataset.'
        return {'ok':True,'content':json.dumps({'items':[
            {'module':'research','label':'Research question','text':quote,'anchor':source['id'],'quote':quote,'field':'research_question'},
            {'module':'suggestion','label':'Validation plan','text':'Compare against the recorded baseline.','anchor':source['id'],'quote':quote},
            {'module':'references','label':'A {Reproducible} Controller','text':'Same bibliography entry.',
             'anchor':bib['id'],'quote':'title = {A {Reproducible} Controller}'},
            {'module':'logic','label':'Invalid fragment','text':quote,'anchor':'F999','quote':quote},
            {'module':'research','label':'Invented value','text':'Invented value','anchor':source['id'],'quote':'This is not in the uploaded document.'}
        ]}),'usage':{'total_tokens':30}}

    monkeypatch.setattr(app,'call_ai',fixture_model)
    request={'request_id':'model','chunks':preview['selected_indices'],'fingerprint':preview['fingerprint']}
    assert client.post(f'{URL}/{bid}/model-runs',json=request|{'authorize':False}).status_code==422
    assert not sent
    response=ok(client.post(f'{URL}/{bid}/model-runs',json=request|{'authorize':True}))
    assert sent==[part['payload'] for part in preview['selected']]
    assert response['analyses'][0]['status']=='succeeded'
    ok(client.post(f'{URL}/{bid}/model-runs',json=request|{'authorize':True}))
    assert len(sent)==1
    checked=ok(client.get(f'{URL}/{bid}/preview'))
    assert {module['id'] for module in checked['modules']}=={item['module'] for item in checked['items']}
    assert [item['data']['cite_key'] for item in checked['items'] if item['kind']=='reference']==['example2020']
    assert not {'Invented value','Invalid fragment'}.intersection(item['title'] for item in checked['items'])
    assert next(i for i in checked['items'] if i['title']=='Validation plan')['choices']==[]
    research=next(i for i in checked['items'] if i['title']=='Research question')
    assert research['origin']=='ai'
    staged=next(s for s in batch['sources'] if s['name']=='main.md')
    assert research['evidence'][0]['source_id']==staged['id']
    assert research['evidence'][0]['fragment_id']==next(p['id'] for p in staged['fragments'] if p['heading']=='Abstract')
    chosen={i['id']:'add' for i in checked['items'] if i['suggested']}
    chosen.update({next(i['id'] for i in checked['items'] if i['title']==title and i['module']==module):'add'
                   for title,module in [('Research question','research'),('Abstract','logic')]})
    apply={'request_id':'adopt','fingerprint':checked['fingerprint'],'decisions':chosen}
    result=ok(client.post(f'{URL}/{bid}/apply',json=apply))
    assert result['project_id']
    assert all(o['status']=='applied' for o in result['outcomes']),[o for o in result['outcomes'] if o['status']!='applied']
    again=ok(client.post(f'{URL}/{bid}/apply',json=apply))
    assert again['idempotent_replay']
    pid=result['project_id']
    payload=ok(client.get(f'/api/projects/{pid}'))
    assert any(f['path'].endswith('01.tex') and 'reproducible dataset' in f['content'] for f in payload['files'])
    assert any(t['term']=='Controller' for t in payload['terms'])
    assert any('A {Reproducible} Controller' in ref['title'] for ref in payload['refs'])
    assert ok(client.get(f'/api/projects/{pid}/outline/structured'))['nodes']
    assert ok(client.get(f'/api/projects/{pid}/logic/graph'))['nodes']
    batch=ok(client.get(f'{URL}/{bid}'))
    assert {a['module'] for a in batch['adopted']}=={m['id'] for m in checked['modules']}


def test_adopted_manuscript_opens_traceify_with_bidirectional_evidence(client):
    bid = new(client, 'traceify-import')
    batch = ok(upload(client, bid, 'main.md', PAPER))
    preview = ok(client.get(f'{URL}/{bid}/preview'))
    decisions = {item['id']: 'add' for item in preview['items'] if item['module'] in {'manuscript', 'outline'}}
    applied = ok(client.post(f'{URL}/{bid}/apply', json={
        'request_id':'traceify-adopt', 'fingerprint':preview['fingerprint'], 'decisions':decisions}))
    pid = applied['project_id']
    base = f'/api/projects/{pid}/structure-workspaces'
    assert ok(client.get(base))['imported_manuscripts'][0]['id'] == bid
    formal = ok(client.get(f'/api/projects/{pid}/outline/structured'))
    created = ok(client.post(f'{base}/from-manuscript/{bid}'))
    workspace = created['workspace']
    assert created['host']['context_manifest']['manuscript_import']['source_id'] == batch['sources'][0]['id']
    section = next(node for node in workspace['graph']['nodes'] if node['label'] == 'Methods')
    evidence = section['source_refs'][0]
    assert 'three trials' in evidence['excerpt']
    mapping = next(item for item in workspace['mappings'] if item['source_ref']['locator'] == evidence['locator'])
    assert section['id'] in mapping['node_ids']
    assert ok(client.post(f'{base}/from-manuscript/{bid}'))['workspace']['id'] == workspace['id']
    assert ok(client.get(f'/api/projects/{pid}/outline/structured')) == formal
    other = ok(client.post('/api/projects', json={'name':'另一篇论文'}))['project']['id']
    assert client.post(f'/api/projects/{other}/structure-workspaces/from-manuscript/{bid}').status_code == 404


def test_revision_conflict_group_rollback_and_saved_removal_impact(client,monkeypatch):
    pid=ok(client.post('/api/projects',json={'name':'Existing'}))['project']['id']
    bid=ok(client.post(URL,json={'request_id':'merge','target_mode':'existing','project_id':pid}))['id']
    source=ok(upload(client,bid,'paper.md',PAPER))['sources'][0]
    checked=ok(client.get(f'{URL}/{bid}/preview'))
    root=next(i for i in checked['items'] if i['data'].get('path')=='main.tex')
    assert root['adoption']=='conflict' and not root['suggested']
    ok(client.put(f'/api/projects/{pid}/files',json={'path':'main.tex','content':'Author changes.','base_version':1}))
    stale=client.post(f'{URL}/{bid}/apply',json={'request_id':'old','fingerprint':checked['fingerprint'],'decisions':{root['id']:'replace'}})
    assert stale.status_code==409 and stale.json()['detail']['code']=='manuscript_preview_stale'
    checked=ok(client.get(f'{URL}/{bid}/preview'))
    write=manuscript_apply._write_outline
    def fail(*_args,**_kwargs):
        raise ProjectServiceError(409,'injected_failure','A dependent module write failed.')
    monkeypatch.setattr(manuscript_apply,'_write_outline',fail)
    chosen={i['id']:'add' for i in checked['items'] if i['suggested']} | {root['id']:'replace'}
    result=ok(client.post(f'{URL}/{bid}/apply',json={'request_id':'partial','fingerprint':checked['fingerprint'],'decisions':chosen}))
    assert {'failed','applied'}=={item['status'] for item in result['outcomes']}
    state=ok(client.get(f'/api/projects/{pid}'))
    assert next(f['content'] for f in state['files'] if f['path']=='main.tex')=='Author changes.'
    assert not any(f['path'].startswith('sections/imported-') for f in state['files'])
    assert state['corpus']
    monkeypatch.setattr(manuscript_apply,'_write_outline',write)
    checked=ok(client.get(f'{URL}/{bid}/preview'))
    remaining={i['id']:'add' for i in checked['items'] if i['suggested']} | {root['id']:'replace'}
    result=ok(client.post(f'{URL}/{bid}/apply',json={'request_id':'retry','fingerprint':checked['fingerprint'],'decisions':remaining}))
    assert all(i['status']=='applied' for i in result['outcomes'])
    impact=ok(client.get(f'{URL}/{bid}/removal-preview',params={'source_id':source['id']}))
    assert len(impact['impact'])>=2
    removed=ok(client.post(f'{URL}/{bid}/sources/{source["id"]}/remove',json={'fingerprint':impact['fingerprint']}))
    assert not removed['sources'] and removed['adopted']
    assert client.get(f'{URL}/{bid}/sources/{source["id"]}/original').status_code==404
    assert ok(client.get(f'/api/projects/{pid}'))['corpus']


def test_archive_tex_graphics_and_format_isolation(client):
    bid=new(client)
    trace=io.BytesIO()
    with zipfile.ZipFile(trace,'w') as archive:
        archive.writestr('main.tex','\\documentclass{article}\n\\usepackage{graphicx}\n\\begin{document}\n\\input{sections/methods}\n\\includegraphics{figures/plot}\n\\end{document}')
        archive.writestr('sections/methods.tex','\\section{Methods}\nSource study methods.\n')
        archive.writestr('figures/plot.png',image())
        archive.writestr('article.sty','% package style fixture')
    parsed=ok(upload(client,bid,'draft.zip',trace.getvalue()))
    assert len(parsed['sources'])==5 and [p['name'] for p in parsed['main_options']]==['main.tex']
    checked=ok(client.get(f'{URL}/{bid}/preview'))
    assert any(i['kind']=='research' and i['data']['text']=='Source study methods.' for i in checked['items'])
    assert not any(i['kind'] in {'research','logic'} and i['data']['text'].startswith('\\input') for i in checked['items'])
    choices={i['id']:'add' for i in checked['items'] if i['suggested']}
    written=ok(client.post(f'{URL}/{bid}/apply',json={'request_id':'zip','fingerprint':checked['fingerprint'],'decisions':choices}))
    assert all(i['status']=='applied' for i in written['outcomes']),written
    files={i['path']:i['content'] for i in ok(client.get('/api/projects/'+written['project_id']))['files']}
    assert 'sections/methods.tex' in files and 'article.sty' in files
    assert '\\includegraphics{figures/asset_' in files['main.tex']
    invalid=io.BytesIO()
    with zipfile.ZipFile(invalid,'w') as archive:
        archive.writestr('../main.tex','untrusted local fixture')
    fail=upload(client,bid,'invalid.zip',invalid.getvalue())
    assert fail.status_code==422 and fail.json()['detail']['code']=='unsafe_path'
    other=new(client,'different')
    root=parsed['sources'][0]['id']
    assert client.get(f'{URL}/{other}/sources/{root}/original').status_code==404


def test_asset_recovery_removes_uncommitted_copies_only(client,monkeypatch):
    from paperpilot.services import manuscript_asset_journal as journal
    from paperpilot.services.visual_assets import managed_path
    pid=ok(client.post('/api/projects',json={'name':'Recovery'}))['project']['id']
    version='1'*32
    prefix='a'*32+'/'+version
    paths={name:f'{prefix}/{name}.png' for name in ('source','preview','build')}
    marker=journal.record(app.PROJECTS_DIR,pid,version,paths)
    physical=[managed_path(app.PROJECTS_DIR,pid,path) for path in paths.values()]
    parent=physical[0].parent
    parent.mkdir(parents=True)
    for path in physical:
        path.write_bytes(b'uncommitted sample')
    unrelated=parent/'unrelated.png'
    unrelated.write_bytes(b'leave this alone')
    conn=app.db()
    try:
        monkeypatch.setattr(journal,'owner_state',lambda _:'alive')
        journal.recover(conn,app.PROJECTS_DIR)
        assert all(path.exists() for path in physical)
        monkeypatch.setattr(journal,'owner_state',lambda _:'dead')
        journal.recover(conn,app.PROJECTS_DIR)
        assert not marker.exists() and not any(path.exists() for path in physical)
        assert unrelated.read_bytes()==b'leave this alone'
    finally:
        conn.close()


def test_legacy_planning_requires_explicit_adoption_and_preserves_old_files(client):
    pid=ok(client.post('/api/projects',json={'name':'Legacy paper'}))['project']['id']
    conn=app.db()
    try:
        with conn:
            conn.execute('DELETE FROM workflow_snapshots WHERE project_id=?',(pid,))
            conn.execute('DELETE FROM workflow_work_items WHERE project_id=?',(pid,))
            conn.execute('DELETE FROM content_versions WHERE project_id=?',(pid,))
            conn.execute("UPDATE files SET content='Original legacy text' WHERE project_id=? AND path='main.tex'",(pid,))
    finally:
        conn.close()
    batch=ok(client.post(URL,json={'request_id':'legacy','target_mode':'existing','project_id':pid}))
    bid=batch['id']
    ok(upload(client,bid,'draft.md',PAPER))
    preview=ok(client.get(f'{URL}/{bid}/preview'))
    research=next(item for item in preview['items'] if item['kind']=='research')
    assert research['choices']==['enable_planning','skip'] and not research['suggested']
    assert not ok(client.get(f'/api/projects/{pid}'))['workflow_enabled']
    result=ok(client.post(f'{URL}/{bid}/apply',json={
        'request_id':'enable','fingerprint':preview['fingerprint'],'decisions':{research['id']:'enable_planning'}}))
    assert result['outcomes'][0]['status']=='applied',result
    saved=ok(client.get(f'/api/projects/{pid}'))
    assert saved['workflow_enabled'] and next(f['content'] for f in saved['files'] if f['path']=='main.tex')=='Original legacy text'
    conn=app.db()
    try:
        row=conn.execute("SELECT content,source FROM content_versions WHERE project_id=? AND target_path='main.tex'",(pid,)).fetchone()
        assert tuple(row)==('Original legacy text','legacy_manuscript_baseline')
        snapshot=json.loads(conn.execute('SELECT stage_outputs_json FROM workflow_snapshots WHERE project_id=? ORDER BY version DESC LIMIT 1',(pid,)).fetchone()[0])
        assert snapshot['startup_plan']['stages'][0]['status']=='draft'
    finally:
        conn.close()


def test_docx_and_pdf_page_locations_support_manual_correction(client,monkeypatch):
    from docx import Document
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen.canvas import Canvas
    from paperpilot.services import manuscript_sources
    bid=new(client)
    doc=Document()
    doc.add_heading('Methods',level=1)
    doc.add_paragraph('A source paragraph with sufficient text.')
    doc.add_table(1,1).cell(0,0).text='Exact table value'
    doc.add_picture(io.BytesIO(image()))
    stream=io.BytesIO()
    doc.save(stream)
    state=ok(upload(client,bid,'study.docx',stream.getvalue()))
    docx=next(s for s in state['sources'] if s['name']=='study.docx')
    docx_download=client.get(f'{URL}/{bid}/sources/{docx["id"]}/original?inline=true')
    assert docx_download.headers['content-disposition'].startswith('attachment;')
    assert any(p['mode']=='table' and p['text']=='Exact table value' for p in state['sources'][0]['fragments'] if 'mode' in p)
    assert any(s['kind']=='image' for s in state['sources'])
    scanned=io.BytesIO()
    canvas=Canvas(scanned)
    canvas.drawString(20,700,'Abstract')
    canvas.drawString(20,680,'A source paragraph with sufficient text.')
    canvas.showPage()
    canvas.drawImage(ImageReader(io.BytesIO(image())),20,400)
    canvas.save()
    monkeypatch.setattr(manuscript_sources,'_ocr',lambda *_:'')
    state=ok(upload(client,bid,'scan.pdf',scanned.getvalue()))
    source=next(s for s in state['sources'] if s['name']=='scan.pdf')
    original=f'{URL}/{bid}/sources/{source["id"]}/original'
    download=client.get(original)
    assert download.headers['content-disposition'].startswith('attachment;')
    inline=client.get(original+'?inline=true')
    assert inline.headers['content-disposition'].startswith('inline;')
    assert inline.headers['content-type']=='application/pdf'
    assert inline.content==download.content==scanned.getvalue()
    part=next(p for p in source['fragments'] if p.get('page')==2)
    assert part['issue'] and client.get(f'{URL}/{bid}/preview').status_code==409
    state=ok(client.put(f'{URL}/{bid}/sources/{source["id"]}/fragments/{part["id"]}',json={'text':'Corrected OCR line.','revision':state['revision']}))
    ok(client.put(f'{URL}/{bid}/primary',json={'source_id':source['id']}))
    checked=ok(client.get(f'{URL}/{bid}/preview'))
    assert any('Corrected OCR line' in r['quote'] and r['page']==2 for item in checked['items'] for r in item['evidence'])


def test_pdf_bookmarks_cross_page_references_and_vector_page_assets(client):
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfgen.canvas import Canvas

    data=io.BytesIO()
    canvas=Canvas(data)
    canvas.drawString(20,780,'Abstract')
    canvas.drawString(20,760,'Keywords: five point stencil; reconstruction')
    canvas.drawString(20,740,'1 Introduction')
    canvas.drawString(20,720,'This article uses a designed interpolation baseline.')
    canvas.drawString(20,690,'1.1 Method detail')
    canvas.drawString(20,670,'Method details can be reviewed in the original.')
    canvas.rect(20,550,80,75)
    canvas.drawString(20,520,'Figure 1: Original vector diagram.')
    canvas.showPage()
    canvas.drawString(20,780,'References')
    canvas.drawString(20,760,'[1] First Author. Original title.')
    canvas.drawString(20,740,'A journal and year as printed.')
    canvas.showPage()
    canvas.drawString(20,780,'References 3')
    canvas.drawString(20,760,'[2] Second Author. Another original title.')
    canvas.drawString(20,740,'A second journal and year as printed.')
    canvas.drawString(20,710,'Appendix')
    canvas.drawString(20,690,'Some additional context.')
    canvas.save()
    writer=PdfWriter()
    for page in PdfReader(data).pages:
        writer.add_page(page)
    intro=writer.add_outline_item('1 Introduction',0)
    writer.add_outline_item('1.1 Method detail',0,parent=intro)
    writer.add_outline_item('References',1)
    writer.add_outline_item('Appendix',2)
    raw=io.BytesIO()
    writer.write(raw)
    bid=new(client)
    batch=ok(upload(client,bid,'vector-study.pdf',raw.getvalue()))
    assert [s['kind'] for s in batch['sources']]==['pdf','page_asset']
    preview=ok(client.get(f'{URL}/{bid}/preview'))
    section=next(i for i in preview['items'] if i['kind']=='outline' and i['title']=='1.1 Method detail')
    assert section['data']['level']==2
    reference=next(i for i in preview['items'] if i['data'].get('original_label')=='[2]')
    assert reference['evidence'][0]['page']==3 and 'second journal' in reference['data']['notes']
    assert 'additional context' not in reference['data']['notes']
    image=next(i for i in preview['items'] if i['kind']=='image')
    assert image['data']['original_page']==1 and '原页' in image['title']
    assert any(i['kind']=='term' and i['title']=='five point stencil' and i['status']=='draft' for i in preview['items'])
    adopted=ok(client.post(f'{URL}/{bid}/apply',json={
        'request_id':'page-asset','fingerprint':preview['fingerprint'],'decisions':{image['id']:'add'}}))
    assert adopted['outcomes'][0]['status']=='applied',adopted
    assert adopted['outcomes'][0]['items'][0]['result']['build_name'].endswith('.pdf')


def test_reasoning_model_protocol_probe_reserves_room_for_visible_response(monkeypatch):
    from paperpilot.adapters import openai_compat
    from paperpilot.services import credential_vault, provider_runtime
    recorded=[]
    def probe(*_args,**kwargs):
        recorded.append(kwargs['max_tokens'])
        return {'ok':True,'usage':{'total_tokens':20}}
    monkeypatch.setattr(openai_compat,'call_chat_completion',probe)
    model='deepseek-v4-pro'
    base='http://127.0.0.1:1'
    config={'base_url':base,'model_limits':{model:{'output_tokens':160}}}
    credential=credential_vault.RuntimeCredential(base,'none','','isolated',1,{})
    provider_runtime.perform_test(config,model,'chat',credential)
    provider_runtime.perform_test(config,'non-reasoning-model','chat',credential)
    assert recorded==[160,16]
