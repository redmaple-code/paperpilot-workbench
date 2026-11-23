"""Isolated real application with a deterministic model, never real-provider evidence."""
import argparse
import json
import os
import re
import tempfile
import time
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import uvicorn
import app


def model(action,prompt,content,selection,model,base_url=None,context='',max_tokens=None,**kwargs):
    messages=kwargs.get('messages') or []
    user=next((m.get('content','') for m in reversed(messages) if m['role']=='user'),'')
    if kwargs.get('tools'):
        task=re.search(r'作者补充：([^\n]*)',user)
        task=task.group(1) if task else user
        if '慢任务' in task:time.sleep(1.5)
        if '起草' in task:
            return {'ok':True,'content':'','tool_calls':[{'id':'draft-'+str(time.time_ns()),'type':'function','function':{'name':'write_draft','arguments':json.dumps({'instruction':task,'content':'作者材料：研究实验边界、数据约束和可验证性。'},ensure_ascii=False)}}],'usage':{'total_tokens':80}}
        answer='第二轮沿用第一轮边界，明确限制。' if '第二轮' in task else '第一轮的可见结论：保留实验边界。'
        return {'ok':True,'content':answer,'usage':{'total_tokens':80}}
    return {'ok':True,'content':'第二份草稿说明研究范围，所有结论仍需证据核对。' if '第二稿' in user else '第一份草稿说明实验边界，强调材料约束和可验证性。','usage':{'total_tokens':80}}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--port',type=int,default=8877);args=parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='paperpilot-agent-ui-') as path:
        root=Path(path);app.DB_PATH=root/'app.db';app.PROJECTS_DIR=root/'projects';app.PROJECTS_DIR.mkdir()
        os.environ['PWA_HOST_BUDGET_DB']=str(root/'budget.db')
        app.call_ai=model;app.init_db()
        from tests.provider_fixtures import install_default_provider_fixture
        install_default_provider_fixture()
        uvicorn.run(app.app,host='127.0.0.1',port=args.port,log_level='warning')


if __name__=='__main__':main()
