"""Real public search -> selected reference/OA action -> preview -> explicit import."""
from datetime import datetime,timezone
import argparse
import json
import os
from pathlib import Path
import sys
import uuid


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True);args=parser.parse_args()
    root=Path(__file__).resolve().parents[1];out=Path(args.output).resolve();out.mkdir(parents=True,exist_ok=True)
    storage=root/'.project-to-act/local/pr1-real-oa';storage.mkdir(parents=True,exist_ok=True)
    os.environ.update(PWA_DATA_DIR=str(storage),PWA_HOST_BUDGET_DB=str(storage/'budget.db'),PWA_ACADEMIC_SOURCES='openalex')
    sys.path.insert(0,str(root))
    from fastapi.testclient import TestClient
    import app
    app.init_db();client=TestClient(app.app)
    report={'mode':'real_external','executed_at':datetime.now(timezone.utc).isoformat(),'model_calls':0,'private_manuscript_sent':False,'steps':[],'product_acceptance':False}
    destination=out/'REAL-OA.json'
    def save():destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    def ok(response):
        if response.status_code!=200:raise RuntimeError('HTTP '+str(response.status_code)+' '+response.text[:350])
        return response.json()
    try:
        pid=ok(client.post('/api/projects',json={'name':'真实OA交接隔离验证'}))['project']['id']
        searched=ok(client.post(f'/api/projects/{pid}/agent/business-search',json={'query':'Terminologies for Reproducible Research','consent_to_search':True}))
        card=next((c for c in searched.get('cards',[]) if c.get('metadata',{}).get('doi')=='10.48550/arxiv.1802.03311'),None)
        if card is None:raise RuntimeError('预期的公开样例未在本次实际检索中返回；未改选不相关来源。')
        report['source']={'title':card['title'],**card['metadata']};report['steps'].append('real-search-stable-candidate');save()
        for operation in ['reference','oa']:
            revision=ok(client.get(f'/api/projects/{pid}'))['project']['project_revision']
            action=ok(client.post(f'/api/projects/{pid}/agent/actions',json={'request_id':uuid.uuid4().hex,'operation':operation,'base_version':revision,'payload':card['payload']}))
            preview=ok(client.post(f'/api/projects/{pid}/agent/actions/{action["id"]}/preview',json={}))
            applied=ok(client.post(f'/api/projects/{pid}/agent/actions/{action["id"]}/apply',json={'request_id':uuid.uuid4().hex,'expected_version':revision,'action_fingerprint':preview['action_fingerprint'],'accepted':True}))
            if operation=='oa':report['created_import_id']=applied['result']['import_id']
            report['steps'].append(operation+'-preview-and-explicit-handoff');save()
        conn=app.db();before=conn.execute('SELECT count(*) FROM corpus WHERE project_id=?',(pid,)).fetchone()[0];conn.close()
        assert before==0,'未确认资源不得已经进入语料库'
        fetched=ok(client.post(f'/api/projects/{pid}/imports',json={**{'candidate_id':card['payload']['candidate_id']},'idempotency_key':pid+':'+card['payload']['candidate_id']}))
        report['fetch']={'status':fetched.get('status'),'error':fetched.get('error')};save()
        if fetched.get('status')!='preview_ready':raise RuntimeError('真实下载/解析未就绪，保留原候选及导入请求。')
        row=fetched['import'];report['download_sha256']=row.get('file_sha256')
        conn=app.db();assert conn.execute('SELECT count(*) FROM corpus WHERE project_id=?',(pid,)).fetchone()[0]==0;conn.close()
        report['steps'].append('real-fetch-preview-not-yet-imported')
        confirmed=ok(client.post(f'/api/projects/{pid}/imports/{row["id"]}/confirm',json={'selected_fragments':[],'keep_attachment':False}))
        report['confirmation_status']=confirmed.get('status')
        conn=app.db();count=conn.execute('SELECT count(*) FROM corpus WHERE project_id=?',(pid,)).fetchone()[0];conn.close()
        assert count>0,'明确确认后应有真实来源语料'
        report.update(status='passed',corpus_records=count);report['steps'].append('explicit-source-import-confirmation')
    except Exception as error:
        report.update(status='blocked',error_type=type(error).__name__,reason=str(error)[:450])
    save();print(json.dumps(report,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
