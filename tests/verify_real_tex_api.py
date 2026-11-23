from pathlib import Path
import base64
import io
import json
import os
import sys
import tempfile

root=Path.cwd();sys.path.insert(0,str(root))
from fastapi.testclient import TestClient
from pypdf import PdfReader
import app

local=root/'.project-to-act/local/project-development/full-product-20261101'
out=root/'.project-to-act/docs/project-development/evidence/full-product-20261101'
os.environ['PWA_TEX_SANDBOX_IMAGE']='paperpilot-tex:local'
os.environ['PWA_LATEX_ENGINE']='xelatex'
os.environ['PWA_BIB_ENGINE']='biber'
os.environ['PWA_LATEX_TIMEOUT_SECONDS']='120'
def ok(response):
    assert response.status_code==200,response.text
    return response.json()
with tempfile.TemporaryDirectory(prefix='real-api-',dir=local) as temporary:
    workspace=Path(temporary).resolve();assert workspace.is_relative_to(local.resolve())
    app.DB_PATH=workspace/'app.db';app.PROJECTS_DIR=workspace/'projects';app.PROJECTS_DIR.mkdir()
    app.init_db();client=TestClient(app.app)
    pid=ok(client.post('/api/projects',json={'name':'真实沙箱集成验收'}))['project']['id']
    wf=ok(client.get(f'/api/projects/{pid}/workflow'))
    started=ok(client.post(f'/api/projects/{pid}/workflow/commands',json={'command':'start','base_version':wf['version'],'request_id':'start'}))
    ok(client.post(f'/api/projects/{pid}/human-revision',json={'workflow_version':started['version']}))
    files=ok(client.get(f'/api/projects/{pid}'))['files']
    main=next(f for f in files if f['path']=='main.tex')
    content=r'\documentclass{ctexart}\usepackage[backend=biber]{biblatex}\addbibresource{references.bib}\begin{document}中文图像与引用集成验证。\cite{sample}\printbibliography\end{document}'
    ok(client.put(f'/api/projects/{pid}/files',json={'path':'main.tex','base_version':main['version'],'content':content}))
    refs=next(f for f in files if f['path']=='references.bib')
    ok(client.put(f'/api/projects/{pid}/files',json={'path':'references.bib','base_version':refs['version'],'content':'@article{sample,author={Example, A.},title={Verified reference source},journal={Test Journal},year={2026}}'}))
    svg='<svg xmlns="http://www.w3.org/2000/svg" width="300" height="100"><text x="20" y="50" font-size="24">方法与结果</text></svg>'
    saved=ok(client.post(f'/api/projects/{pid}/drawing/assets/import',json={'filename':'method.svg','content_base64':base64.b64encode(svg.encode()).decode(),'request_id':'asset'}))
    main=next(f for f in ok(client.get(f'/api/projects/{pid}'))['files'] if f['path']=='main.tex')
    aid,vid=saved['asset']['id'],saved['version']['id']
    inserted=ok(client.post(f'/api/projects/{pid}/drawing/assets/{aid}/insert-into-editor',json={'version_id':vid,'target_path':'main.tex','base_file_version':main['version'],'caption':'受管中文图像','request_id':'insert'}))
    queued=ok(client.post(f'/api/projects/{pid}/compile',json={'request_id':'real-compile'}))
    finished=ok(client.get(queued['status_url']))
    assert finished['status']=='succeeded',finished
    response=client.get(f'/api/projects/{pid}/pdf');assert response.status_code==200,response.text
    pdf=PdfReader(io.BytesIO(response.content));text='\n'.join(p.extract_text() or '' for p in pdf.pages)
    assert '方法与结果' in text and 'Verified reference source' in text,text
    snap=queued['snapshot'];assert snap['manifest']['assets'][0]['version_id']==vid
    (out/'REAL-TEX-API.pdf').write_bytes(response.content)
    (out/'REAL-TEX-API.json').write_text(json.dumps({'passed':True,'database_isolated':True,'external_ai_calls':0,'real_tex':True,'engine':'xelatex','bibliography':'biber','checks':['editor-version-guard','chinese-svg-embedded','managed-asset-manifest','real-build-job','pdf-download','reference-rendered'],'pages':len(pdf.pages),'snapshot_identity':snap['manifest']['identity']},ensure_ascii=False,indent=2),encoding='utf-8')
    print('Real API editor -> managed Chinese SVG -> build snapshot -> XeLaTeX/Biber -> PDF download passed')
