"""Explicit real-provider verification on an isolated synthetic manuscript.

Credentials are loaded into this process only; never emitted in reports.
"""
import argparse
import json
import os
import sys
import tempfile
import time
import uuid
from datetime import datetime,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from dotenv import dotenv_values
from fastapi.testclient import TestClient
import app
from paperpilot.services import provider_config,provider_runtime


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--env-file',required=True);parser.add_argument('--output',required=True);args=parser.parse_args()
    out=Path(args.output);out.parent.mkdir(parents=True,exist_ok=True)
    values=dotenv_values(args.env_file)
    for key in ('PWA_AI_BASE_URL','PWA_AI_API_KEY','PWA_AI_MODEL'):
        if values.get(key):os.environ[key]=values[key]
    os.environ['PWA_AI_TIMEOUT']='90'
    model=os.environ.get('PWA_AI_MODEL','deepseek-chat')
    report={'mode':'real_external','executed_at':datetime.now(timezone.utc).isoformat(),'model':model,'synthetic_manuscript':True,'turns':[],'status':'running'}
    def save():out.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf8',newline='\n')
    save()
    try:
        with tempfile.TemporaryDirectory(prefix='paperpilot-agent-real-') as path:
            root=Path(path);app.DB_PATH=root/'app.db';app.PROJECTS_DIR=root/'projects';app.PROJECTS_DIR.mkdir();os.environ['PWA_HOST_BUDGET_DB']=str(root/'budget.db');app.init_db()
            conn=app.db()
            with conn:
                profile=provider_config.create_profile(conn,{'name':'01真实联调（隔离）','base_url':os.environ['PWA_AI_BASE_URL'],
                    'auth_mode':'environment','models':[model],'request_id':'real-profile',
                    'model_limits':{model:{'input_tokens':32000,'output_tokens':4000,'source':'本次操作者保守调用上限；非容量实测结论'}}},'real-profile')
                test=provider_runtime.queue_test(conn,profile['id'],{'expected_version':1,'model_id':model,'mode':'chat','request_id':'real-protocol',
                    'consent_to_send':True,'possible_cost_confirmed':True})
            conn.close()
            provider_runtime.process_test(app.db,profile['id'],test['id'])
            conn=app.db();actual=conn.execute('SELECT status,error_code FROM ai_provider_tests WHERE id=?',(test['id'],)).fetchone();report['protocol_test']=dict(actual);save()
            if actual['status']!='ready':raise RuntimeError('real_provider_not_ready:'+actual['error_code'])
            with conn:
                enabled=provider_config.update_profile(conn,profile['id'],1,{'status':'enabled'})
                provider_runtime.set_default(conn,profile['id'],enabled['current_version'],model,0)
            conn.close()
            with TestClient(app.app) as client:
                def api(path,body=None,method='POST'):
                    r=client.request(method,path,json=body)
                    if r.status_code!=200:raise RuntimeError('api_'+str(r.status_code)+':'+str(r.json().get('detail',{}).get('code','')))
                    return r.json()
                pid=api('/api/projects',{'name':'真实Agent验证 · 合成边界研究','domain':'计算方法'})['project']['id']
                base=f'/api/projects/{pid}'
                # Seed only synthetic setup material; Agent generation remains in planning state.
                conn=app.db()
                with conn:conn.execute('UPDATE outline SET content=? WHERE project_id=?',('引言：说明模型适用边界。\n方法：用合成案例检验边界条件，不宣称实验已经完成。\n结果：仅记录待验证的预期。\n讨论：说明局限。',pid))
                conn.close()
                cid=api(base+'/agent/conversations',{'title':'真实连续对话验证'})['id']
                questions=['请实际调用read_outline读取当前大纲，然后用两句话概括章节目标。不检索外网，不生成创新候选。',
                           '沿用上一轮的大纲讨论，方法章节最重要的边界是什么？用一句话回答。',
                           '请调用write_draft起草一段引言，讨论合成案例验证的适用边界。不要编造数值或文献，不要修改文件。']
                for i,question in enumerate(questions,1):
                    request={'conversation_id':cid,'instruction':question,'context_snapshot':{'project_id':pid}}
                    preview=api(base+'/agent/preview',request)
                    run=api(base+'/agent/runs',request|{'request_id':'real-turn-'+str(i),'preview_fingerprint':preview['preview_fingerprint'],
                        'consent_to_send':True,'possible_cost_confirmed':True})
                    deadline=time.monotonic()+310
                    while time.monotonic()<deadline:
                        done=api(base+'/agent/runs/'+run['id'],method='GET')
                        if done['status'] not in ('queued','running'):
                            # Let the worker finish its journal/lease cleanup before temp storage is removed.
                            conn=app.db();lease=conn.execute('SELECT lease_token FROM agent_runs WHERE id=?',(run['id'],)).fetchone()[0];conn.close()
                            if not lease:break
                        time.sleep(.5)
                    else:raise RuntimeError('real_run_timeout')
                    row={'turn':i,'instruction':question,'status':done['status'],'error_code':done['error_code'],
                         'budget':done['budget'],'tools':[s['tool'] for s in done['steps'] if s['tool']],
                         'result':done['result'],'context_manifest':preview['context_manifest']}
                    report['turns'].append(row);save();print(json.dumps({'turn':i,'status':done['status'],'tools':row['tools'],'model_calls':done['budget'].get('model_calls')},ensure_ascii=False),flush=True)
                    if done['status']!='succeeded':raise RuntimeError('real_turn_failed:'+str(done['error_code']))
                assert 'read_outline' in report['turns'][0]['tools']
                assert report['turns'][1]['context_manifest']['history_messages']>=2
                assert 'write_draft' in report['turns'][2]['tools']
                assert any(c.get('kind')=='draft' and len(c.get('text',''))>=12 for c in report['turns'][2]['result']['cards'])
                assert report['turns'][2]['budget']['model_calls']>=2
                # No manuscript file writes are performed by generation.
                assert api(base,method='GET')['files'][0]['content'].startswith('\\documentclass')
            report['status']='passed'
    except Exception as exc:
        report['status']='blocked' if not report['turns'] else 'failed'
        # Deliberately do not stringify transport exceptions or configuration/credential objects.
        report['failure_type']=type(exc).__name__
        if isinstance(exc,RuntimeError) and str(exc).startswith(('real_','api_')):report['failure_code']=str(exc)
        save();print(json.dumps({'status':report['status'],'failure_type':report['failure_type'],'failure_code':report.get('failure_code','')},ensure_ascii=False),flush=True)
        return 1
    finally:save()
    print(json.dumps({'status':report['status'],'turns':len(report['turns'])},ensure_ascii=False))
    return 0


if __name__=='__main__':sys.exit(main())
