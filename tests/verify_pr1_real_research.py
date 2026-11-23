"""Bounded real-provider samples using synthetic inputs and environment credentials only."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import uuid


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--env-file');parser.add_argument('--output',required=True);args=parser.parse_args()
    root=Path(__file__).resolve().parents[1];out=Path(args.output).resolve();out.mkdir(parents=True,exist_ok=True)
    from dotenv import load_dotenv
    if args.env_file:load_dotenv(args.env_file,override=False)
    storage=root/'.project-to-act/local/pr1-real-research';storage.mkdir(parents=True,exist_ok=True)
    os.environ.update(PWA_DATA_DIR=str(storage),PWA_HOST_BUDGET_DB=str(storage/'host-budget.sqlite3'))
    os.environ.setdefault('PWA_AI_TIMEOUT','90')
    model=os.environ.get('PWA_AI_MODEL','');base=os.environ.get('PWA_AI_BASE_URL','')
    report={'mode':'real_external','executed_at':datetime.now(timezone.utc).isoformat(),'synthetic_manuscript':True,'model':model,
            'budget':{'max_requests':6,'stage_requests':5,'max_output_tokens_per_stage':6000,'automatic_retries':0},'stages':[],'product_acceptance':False}
    destination=out/'REAL-RESEARCH.json'
    def save():destination.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    if not model or not base or not os.environ.get('PWA_AI_API_KEY'):
        report.update(status='blocked',reason='未找到完整的现有模型环境配置；未发送任何请求。');save();print('Real research blocked: model configuration unavailable');return
    sys.path.insert(0,str(root))
    import app
    from fastapi.testclient import TestClient
    from paperpilot.services.plan_state import STAGES
    app.init_db();client=TestClient(app.app)
    def ok(response):
        if response.status_code!=200:raise RuntimeError('HTTP '+str(response.status_code)+' '+str(response.json().get('detail',{}))[:350])
        return response.json()
    profile=ok(client.post('/api/ai/profiles',json={'name':'PR1 synthetic stage quality sample','base_url':base,'auth_mode':'environment','models':[model],
        'model_limits':{model:{'input_tokens':16000,'output_tokens':6000,'source':'本轮开发抽样的保守请求上限，不宣称模型最大容量'}},'request_id':uuid.uuid4().hex}))
    ok(client.post(f"/api/ai/profiles/{profile['id']}/test",json={'request_id':uuid.uuid4().hex,'expected_version':profile['current_version'],'model_id':model,'mode':'chat','consent_to_send':True,'possible_cost_confirmed':True}))
    state=ok(client.get(f"/api/ai/profiles/{profile['id']}/tests"))[0]
    report['protocol_test']={'status':state['status'],'error_code':state.get('error_code','')};save()
    if state['status']!='ready':report.update(status='blocked',reason='真实协议测试未通过，未重复发送。');save();return
    profile=ok(client.put(f"/api/ai/profiles/{profile['id']}",json={'base_version':profile['current_version'],'changes':{'status':'enabled'}}))
    pid=ok(client.post('/api/projects',json={'name':'PR1五阶段真实模型合成抽样','domain':'软件工程与可复现性','style':'严谨'}))['project']['id']
    conn=app.db()
    from paperpilot.services.provider_config import bind_project
    with conn:
        revision=conn.execute('SELECT project_revision FROM projects WHERE id=?',(pid,)).fetchone()[0]
        bind_project(conn,pid,profile['id'],model,revision,profile['current_version'])
    conn.close()
    background='本项目是软件验证用的合成研究背景：比较单标签与双标签论文编辑中的版本冲突恢复。只有软件测试场景，尚未运行研究实验，没有实验数据或创新结论。资源仅一台开发机、现有浏览器和SQLite。'
    for definition in STAGES:
        stage=definition['id'];item={'stage_id':stage}
        try:
            startup=ok(client.get(f'/api/projects/{pid}/workflow/startup'))
            ok(client.post(f'/api/projects/{pid}/workflow/startup/commands',json={'request_id':uuid.uuid4().hex,'expected_workflow_version':startup['workflow_version'],
                'command':'save','stage_id':stage,'fields':{key:background+' 本字段：'+label for key,label in definition['fields'].items()}}))
            request={'stage_id':stage,'instruction':'仅提出本阶段可执行、可核对的草稿。不得声称有真实实验数据，不编造文献。每阶段最多两张候选，写清假设和未知项。'}
            url=f'/api/projects/{pid}/workflow/research-design'
            preview=ok(client.post(url+'/preview',json=request))
            response=client.post(url,json={**request,'request_id':uuid.uuid4().hex,'preview_fingerprint':preview['preview_fingerprint'],'consent_to_send':True,'possible_cost_confirmed':True})
            if response.status_code!=200:
                detail=response.json().get('detail',{});item.update(status='failed',http_status=response.status_code,error_code=detail.get('code'),reason=detail.get('message','')[:350])
            else:
                candidate=response.json();cards=candidate['cards']
                assert all(c['stage_id']==stage for c in cards)
                assert all(set(c['target_fields'])<=set(definition['fields']) for c in cards)
                assert all(c.get('limitations') and c['dedup_status']=='not_checked' for c in cards)
                item.update(status='passed',cards=cards,base_workflow_version=candidate['base_workflow_version'],scope='真实结构化输出与阶段字段/局限检查；科学有效性未验证')
        except Exception as error:
            item.update(status='blocked',error_type=type(error).__name__,reason=str(error)[:350])
        report['stages'].append(item);save();print(stage,item['status'],flush=True)
    report['status']='passed' if len(report['stages'])==5 and all(s['status']=='passed' for s in report['stages']) else 'partial'
    save();print('Real stages:',report['status'],flush=True)


if __name__=='__main__':main()
