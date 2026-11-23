"""Explicit fixture model. Agent, planning actions and scholar CLI run unchanged."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import uvicorn


def model(*args,**kwargs):
    from tests.ui_scholar_server import model as research_model
    from tests.ui_agent_server import model as writing_model
    messages=kwargs.get('messages') or []
    task=next((m.get('content','') for m in reversed(messages) if m['role']=='user'),'')
    task=task.split('\n论文完整结构',1)[0]
    system=messages[0].get('content','') if messages else ''
    if '产品专项委派' in task:
        if '你是论文总 Agent 下的「章节大纲」' in system:
            return {'ok':True,'content':'结构助理已核对章节目标与边界。','usage':{'total_tokens':60}}
        if any(m['role']=='tool' and m.get('name')=='delegate_agent' for m in messages):
            return {'ok':True,'content':'已汇总结构助理的专项结果。','usage':{'total_tokens':60}}
        return {'ok':True,'content':'安排结构助理核对。','tool_calls':[{'id':'product-delegate','type':'function','function':{'name':'delegate_agent','arguments':json.dumps({'module_key':'outline','instruction':'产品专项委派，核对章节目标与边界'})}}],'usage':{'total_tokens':60}}
    if '专题验证' in task:
        return {'ok':True,'content':'专题讨论已记录当前论文的研究边界。','usage':{'total_tokens':60}}
    if '形成方案' in task:
        from paperpilot.services.plan_state import STAGES
        match=re.search(r'阶段：(STAGE-\d+)',task)
        stage=next(s for s in STAGES if s['id']==(match.group(1) if match else 'STAGE-01'))
        last_user=max(i for i,m in enumerate(messages) if m['role']=='user')
        tools=[m for m in messages[last_user+1:] if m['role']=='tool']
        if not tools:
            name='business_context';arguments={}
        elif len(tools)==1:
            context=json.loads(tools[0]['content'])['data']
            name='propose_business';arguments={'operation':'planning','base_version':context['workflow_version'],'payload':{'stage_id':stage['id'],'mode':'confirm','fields':{key:'隔离测试方案，待作者核对的'+label for key,label in stage['fields'].items()}}}
        else:
            return {'ok':True,'content':'方案已整理，请核对后决定是否采纳。','usage':{'total_tokens':80}}
        return {'ok':True,'content':'我把讨论整理成一份可审阅的方案。','tool_calls':[{'id':'planning-'+str(len(tools)),'type':'function','function':{'name':name,'arguments':json.dumps(arguments,ensure_ascii=False)}}],'usage':{'total_tokens':80}}
    if '起草' in task or not kwargs.get('tools'):
        return writing_model(*args,**kwargs)
    return research_model(*args,**kwargs)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--storage',required=True);parser.add_argument('--port',type=int,default=8894);args=parser.parse_args()
    os.environ['PWA_DATA_DIR']=args.storage
    os.environ['PWA_HOST_BUDGET_DB']=str(Path(args.storage)/'host-budget.db')
    import app
    app.call_ai=model;app.init_db()
    from tests.provider_fixtures import install_default_provider_fixture
    install_default_provider_fixture()
    uvicorn.run(app.app,host='127.0.0.1',port=args.port,log_level='warning')


if __name__=='__main__':main()
