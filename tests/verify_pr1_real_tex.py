"""Saved snapshot -> real Docker XeLaTeX/Biber -> downloaded PDF, in private-free storage."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import re


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);parser.add_argument('--image',default='paperpilot-tex:local');args=parser.parse_args()
    root=Path(__file__).resolve().parents[1];out=Path(args.output).resolve();out.mkdir(parents=True,exist_ok=True)
    local=root/'.project-to-act/local';local.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='pr1-real-tex-',dir=local,ignore_cleanup_errors=True) as temporary:
        storage=Path(temporary).resolve();assert storage.is_relative_to(local.resolve())
        os.environ.update(PWA_DATA_DIR=str(storage),PWA_HOST_BUDGET_DB=str(storage/'budget.db'),PWA_TEX_SANDBOX_IMAGE=args.image,
                          PWA_LATEX_ENGINE='xelatex',PWA_BIB_ENGINE='biber',PWA_LATEX_TIMEOUT_SECONDS='120')
        sys.path.insert(0,str(root))
        from fastapi.testclient import TestClient
        from pypdf import PdfReader
        import app
        app.init_db();client=TestClient(app.app)
        def ok(response):
            assert response.status_code==200,response.text
            return response.json()
        pid=ok(client.post('/api/projects',json={'name':'PR1真实编译合成论文'}))['project']['id']
        files=ok(client.get(f'/api/projects/{pid}'))['files']
        main=r'\documentclass{ctexart}\usepackage[backend=biber]{biblatex}\addbibresource{references.bib}\begin{document}中文保存快照与多作者书目核验。\cite{sample}\printbibliography\end{document}'
        bib='@article{sample,author={{张三} and Smith, Alice and García, José},title={Traceable multi-author bibliography},journal={Synthetic Verification},year={2026}}'
        for path,text in [('main.tex',main),('references.bib',bib)]:
            version=next(f['version'] for f in files if f['path']==path)
            ok(client.put(f'/api/projects/{pid}/editor/save',json={'path':path,'base_version':version,'content':text,'request_id':'save-'+path}))
        queued=ok(client.post(f'/api/projects/{pid}/compile',json={'request_id':'pr1-real-tex'}))
        for _ in range(600):
            completed=ok(client.get(queued['status_url']))
            if completed['status'] not in {'queued','running'}:break
            time.sleep(.3)
        assert completed['status']=='succeeded',completed
        response=client.get(f'/api/projects/{pid}/pdf');assert response.status_code==200,response.text
        raw=response.content;reader=PdfReader(io.BytesIO(raw));text='\n'.join(p.extract_text() or '' for p in reader.pages)
        (out/'REAL-TEX.pdf').write_bytes(raw)
        normalized=re.sub(r'-\s*\n\s*','',text)
        assert 'Traceable multi-author bibliography' in normalized,normalized
        assert 'Smith' in text and ('Garc' in text or 'José' in text),text
        manifest=queued['snapshot']['manifest']
        hashes={item['path']:item['sha256'] for item in manifest['files']}
        assert hashes['main.tex']==hashlib.sha256(main.encode()).hexdigest()
        assert hashes['references.bib']==hashlib.sha256(bib.encode()).hexdigest()
        report={'passed':True,'real_tex':True,'real_model_calls':False,'database_isolated':True,'compiler':'xelatex','bibliography':'biber',
                'image':args.image,'pages':len(reader.pages),'pdf_sha256':hashlib.sha256(raw).hexdigest(),'checks':['saved-source-hash','multiple-authors','bibliography','pdf-download'],
                'chinese_render':'pending_visual_check','text_extraction_limit':'中文字体映射的文本提取不可作为渲染结论，需要检查实际渲染图像。'}
        (out/'REAL-TEX.pdf').write_bytes(raw);(out/'REAL-TEX-REPORT.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(report,ensure_ascii=False))


if __name__=='__main__':main()
