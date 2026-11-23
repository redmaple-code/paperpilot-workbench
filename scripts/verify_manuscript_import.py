"""Build a reproducible split report without modifying the user's active workspaces."""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import sys
import tempfile
import uuid
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))


def demo_archive():
    from seed_complete_paper import MAIN_TEX, REFERENCES, SECTIONS

    data=io.BytesIO()
    with zipfile.ZipFile(data,'w') as archive:
        files={'main.tex':MAIN_TEX,'references.bib':REFERENCES,**SECTIONS}
        for path,value in sorted(files.items()):
            archive.writestr(zipfile.ZipInfo(path.replace('\\','/')),value.encode('utf-8'))
    return [('paperpilot-synthetic.zip',data.getvalue())]


def body(response):
    if not response.is_success:
        detail=response.json().get('detail',{})
        raise RuntimeError(detail.get('message','请求未完成。') if isinstance(detail,dict) else '请求未完成。')
    return response.json()


def report(name, files, preview, adoption, out, *, fixture, model=None):
    from paperpilot.services.manuscript_plan import redact

    safe=lambda value:redact(str(value)).replace('\n',' ').replace('\r',' ')
    labels={'grounded':'有来源','draft':'待审阅草稿','missing':'待补','suggestion':'建议草案'}
    header=['# 已有论文导入拆分结果','',f'样稿：{safe(name)}','',
            '项目自带合成论文。' if fixture else '用户选定稿件。',
            (f'本地解析及真实模型 {safe(model["name"])}。结构有效的回复 {model["succeeded"]}/{model["sent"]} 批，'
             f'全稿共 {model["total"]} 批，保留 {sum(item["origin"]=="ai" for item in preview["items"])} 项模型候选。'
             if model else '以下为本地提取和模块映射。真实模型的拆分质量另行验证。'),'',
            '| 原始文件 | SHA-256 |','|---|---|']
    header.extend(f'| {safe(path).replace("|","/")} | `{hashlib.sha256(data).hexdigest()}` |' for path,data in files)
    applied_ids=applied_item_ids(adoption)
    header.extend(['',f'模块候选共 {len(preview["items"])} 项，隔离工作区采纳 {len(applied_ids)} 项。',''])
    for module in preview['modules']:
        items=[item for item in preview['items'] if item['module']==module['id']]
        adopted=sum(item['id'] in applied_ids for item in items)
        header.extend([f'## {module["title"]}','',f'{len(items)} 项候选，{adopted} 项采纳。',''])
        for item in items:
            source='；'.join(f'{safe(e["name"])} · {safe(e["location"])}' for e in item['evidence'][:2])
            summary=item['data'].get('text') or item['data'].get('definition') or item['data'].get('caption') or ''
            description=f'。{safe(summary)[:180]}' if summary and item['kind'] not in {'corpus'} else ''
            written='已采纳' if item['id'] in applied_ids else '仅预览'
            header.append(f'- {safe(item["title"])} · {labels.get(item["status"],item["status"])} · {"AI" if item["origin"]=="ai" else "本地"} · {written}' +
                          (f' · {source}' if source else '') +description)
        header.append('')
    failures=[g for g in (adoption or {}).get('outcomes',[]) if g['status']=='failed']
    if failures:
        header.extend(['## 采纳失败','',*(f'- {safe(g["message"])}' for g in failures),''])
    out.write_text('\n'.join(header)+'\n',encoding='utf-8')


def configure_model(client,base,model):
    profile=body(client.post('/api/ai/profiles',json={'request_id':'verify-provider','name':'Isolated manuscript verification',
        'base_url':base,'auth_mode':'environment','models':[model],
        'model_limits':{model:{'input_tokens':16000,'output_tokens':4000,'source':'Conservative verification bound'}}}))
    profile_id=profile['id']
    endpoint=f'/api/ai/profiles/{profile_id}'
    queued=body(client.post(endpoint+'/test',json={'request_id':'verify-protocol','model_id':model,'mode':'chat',
        'expected_version':profile['current_version'],'consent_to_send':True,'possible_cost_confirmed':True}))
    result=next(row for row in body(client.get(endpoint+'/tests')) if row['id']==queued['id'])
    if result['status']!='ready':
        raise RuntimeError(f'Real provider protocol test failed: {result["error_code"]}')
    enabled=body(client.put(endpoint,json={'base_version':profile['current_version'],'changes':{'status':'enabled'}}))
    body(client.put('/api/ai/default',json={'profile_id':profile_id,'config_revision':enabled['current_version'],'model_id':model,'expected_version':0}))


def analyze(client,endpoint,model,limit):
    scope=body(client.post(endpoint+'/model-preview',json={}))
    total=scope['total_chunks']
    count=min(total,limit) if limit else total
    passed=0
    for start in range(0,count,3):
        indices=list(range(start,min(start+3,count)))
        scoped=body(client.post(endpoint+'/model-preview',json={'chunks':indices}))
        if not scoped['allowed']:
            raise RuntimeError(scoped['unavailable'])
        result=body(client.post(endpoint+'/model-runs',json={'request_id':uuid.uuid4().hex,
            'fingerprint':scoped['fingerprint'],'chunks':indices,'authorize':True}))
        passed+=sum(row['status']=='succeeded' for row in result['analyses'][0]['result']['outcomes'])
    if not passed:
        raise RuntimeError('No model batch returned usable output.')
    return {'name':model,'succeeded':passed,'sent':count,'total':total}


def contract_metrics(batch, preview):
    sources=batch['sources']
    pages={(source['id'],fragment.get('page')) for source in sources if source['kind']=='pdf'
           for fragment in source['fragments'] if fragment.get('page')}
    items=preview['items']
    numbers=[int(item['data']['original_label'][1:-1]) for item in items if item['data'].get('original_label')]
    captions=[f['heading'] for s in sources if s['kind']=='page_asset' for f in s['fragments']]
    return {'pdf_pages':len(pages),'outline_nodes':sum(item['kind']=='outline' for item in items),
            'reference_numbers':sorted(numbers),'figure_captions':sum(v.startswith('图 ') for v in captions),
            'table_captions':sum(v.startswith('表 ') for v in captions),
            'page_assets':sum(s['kind']=='page_asset' for s in sources)}


def applied_item_ids(adoption):
    return {item['item_id'] for group in (adoption or {}).get('outcomes', []) if group['status']=='applied'
            for item in group['items']}


def main():
    opts=argparse.ArgumentParser(description=__doc__)
    inputs=opts.add_mutually_exclusive_group(required=True)
    inputs.add_argument('--source',type=Path,nargs='+',help='Local paper files in the same import batch.')
    inputs.add_argument('--fixture',action='store_true',help='Use the bundled synthetic LaTeX paper.')
    inputs.add_argument('--benchmark',type=Path,help='Use a pinned research manuscript and verify its structure.')
    opts.add_argument('--out',type=Path,help='Markdown report path. Defaults to a local temporary directory.')
    opts.add_argument('--name',default='论文导入回归样稿')
    opts.add_argument('--real-model',metavar='MODEL',help='Explicitly send the selected materials to the authorized provider.')
    opts.add_argument('--max-model-chunks',type=int,default=0,help='Verify at most this many chunks. Default is all.')
    opts.add_argument('--target',choices=('new','existing'),default='new',help='Use a new or pre-existing isolated workspace.')
    args=opts.parse_args()
    if args.real_model and (not os.environ.get('PWA_AI_API_KEY') or not os.environ.get('PWA_AI_BASE_URL')):
        opts.error('Set the provider URL and inject PWA_AI_API_KEY before starting this process.')
    benchmark=json.loads(args.benchmark.read_text(encoding='utf8')) if args.benchmark else None
    if benchmark:
        source=ROOT/benchmark['source']
        content=source.read_bytes()
        if hashlib.sha256(content).hexdigest()!=benchmark['sha256']:
            opts.error('The benchmark manuscript no longer matches its recorded SHA-256.')
        files=[(source.name,content)]
        args.name=benchmark['title']
    else:
        files=demo_archive() if args.fixture else [(path.name,path.read_bytes()) for path in args.source]
    from fastapi.testclient import TestClient
    import app
    from paperpilot.services.manuscript_import import parser

    if any(len(value)>parser.MAX_FILE_BYTES for _,value in files):
        opts.error('An input exceeds the per-file import limit.')
    with tempfile.TemporaryDirectory(prefix='paperpilot-manuscript-case-') as temp:
        root=Path(temp)
        app.DB_PATH=root/'paperpilot.db'
        app.PROJECTS_DIR=root/'projects'
        app.PROJECTS_DIR.mkdir()
        app.init_db()
        with TestClient(app.app) as client:
            if args.real_model:
                configure_model(client,os.environ['PWA_AI_BASE_URL'],args.real_model)
            prefix='/api/manuscript-imports'
            pid=None
            baseline='Existing manuscript content. Keep this text.'
            if args.target=='existing':
                pid=body(client.post('/api/projects',json={'name':args.name}))['project']['id']
                body(client.put(f'/api/projects/{pid}/files',json={'path':'main.tex','content':baseline,'base_version':1}))
            batch=body(client.post(prefix,json={'request_id':uuid.uuid4().hex,'target_mode':args.target,
                                              'name':args.name,'project_id':pid}))
            endpoint=prefix+'/'+batch['id']
            for filename,content in files:
                batch=body(client.post(endpoint+'/sources',json={'request_id':uuid.uuid4().hex,'filename':filename,
                          'content_base64':base64.b64encode(content).decode()}))
            if len(batch['main_options'])>1:
                opts.error('Multiple principal documents. Verify the intended principal document on its own or through the UI.')
            model=analyze(client,endpoint,args.real_model,args.max_model_chunks) if args.real_model else None
            preview=body(client.get(endpoint+'/preview'))
            metrics=contract_metrics(batch,preview)
            if benchmark:
                changed=[label for label,expected in benchmark['expected'].items() if metrics.get(label)!=expected]
                if changed:
                    raise RuntimeError('Benchmark output changed: '+', '.join(changed))
            choices={item['id']:'add' for item in preview['items'] if item['suggested']}
            targets={item['target'] for item in preview['items'] if item['id'] in choices}
            for item in preview['items']:
                if item['status']=='draft' and item['adoption']=='new' and item['target'] not in targets and item['evidence']:
                    choices[item['id']]='add'
                    targets.add(item['target'])
            adoption=None
            if choices:
                adoption=body(client.post(endpoint+'/apply',json={'request_id':'verify','fingerprint':preview['fingerprint'],'decisions':choices}))
                if adoption['project_id']:
                    target=body(client.get('/api/projects/'+adoption['project_id']))
                    if pid:
                        saved=next(file for file in target['files'] if file['path']=='main.tex')
                        if saved['content']!=baseline or adoption['project_id']!=pid:
                            raise RuntimeError('The existing manuscript was overwritten or the target changed.')
    output=args.out or Path(tempfile.mkdtemp(prefix='paperpilot-split-report-'))/'split-review.md'
    output.parent.mkdir(parents=True,exist_ok=True)
    report(args.name,files,preview,adoption,output,fixture=args.fixture,model=model)
    groups=(adoption or {}).get('outcomes',[])
    failed=sum(g['status']=='failed' for g in groups)
    applied=sum(g['status']=='applied' for g in groups)
    applied_ids=applied_item_ids(adoption)
    adopted_modules={item['module'] for item in preview['items'] if item['id'] in applied_ids}
    print(json.dumps({'report':str(output.resolve()),'candidate_count':len(preview['items']),
                      'applied_items':len(applied_ids),'applied_groups':applied,'failed_groups':failed,
                      'adopted_modules':sorted(adopted_modules),'model':model,'checks':metrics},ensure_ascii=False))
    if benchmark:
        if failed or not adoption or not adoption['project_id'] or applied_ids!=set(choices):
            raise RuntimeError('The benchmark did not complete all selected adoption groups.')
        if adopted_modules!={module['id'] for module in preview['modules']}:
            raise RuntimeError('The benchmark did not populate every module.')


if __name__=='__main__':
    main()
