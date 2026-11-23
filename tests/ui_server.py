from __future__ import annotations

import argparse
from contextlib import closing
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import app


def fake_call_ai(action, prompt, content, selection, model, base_url=None, context="", max_tokens=None):
    if action == 'citation_compare' and '附加协议citation-review-v3' in prompt:
        data=json.loads(prompt.split('审查输入 JSON：\n',1)[1].split('\n附加协议',1)[0])
        findings=[]
        for claim in data['claims']:
            findings.append({'claim_id':claim['claim_id'],'sources':[{'source_id':s['source_id'],'relation':'insufficient_evidence','role':'metadata_only','rationale':'隔离测试：摘要和元数据不能证明全文支持'} for s in data['sources']],
                'condition_results':[],'language_review':{'auxiliary_translation':'隔离测试辅助译释，不能替代原文引句','concept_differences':[]},
                'suggestion':{'text':claim['text'],'reasons':['需人工复核'],'meaning_change_text':''}})
        return {'ok':True,'content':json.dumps({'findings':findings}),'usage':{'total_tokens':100,'completion_tokens':50}}
    if prompt.startswith('将所给术语建议归入指定领域'):
        return {'ok': True, 'content': json.dumps({'suggested_term': '资源术语', 'suggested_definition': '待人工确认定义', 'suggested_aliases': '', 'confidence': .35, 'basis': '隔离测试的低置信度建议'})}
    if prompt.startswith('为当前结构化大纲提出节点修改'):
        data = json.loads(prompt.rsplit('\n', 1)[-1])
        operations = [{'op': 'update_node', 'id': data['nodes'][0]['id'], 'changes': {'title': '人工选择后的引言'}}] if data['nodes'] else []
        operations.append({'op': 'add_node', 'node': {'id': 'ui-suggested-methods', 'title': '不选则不添加的方法'}})
        return {'ok': True, 'content': json.dumps({'operations': operations})}
    if action == "term_logic_check":
        return {"ok": True, "model": model or "ui-test", "content": json.dumps({"operations": []})}
    return {"ok": True, "model": model or "ui-test", "content": "AI candidate text"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--workspace-copy", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="paperpilot-ui-") as temporary:
        root = Path(temporary)
        app.DB_PATH = root / "paperpilot.db"
        app.PROJECTS_DIR = root / "projects"
        if args.workspace_copy:
            source = (args.workspace_copy / "paperpilot.db").resolve(strict=True)
            with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as original, \
                    closing(sqlite3.connect(app.DB_PATH)) as snapshot:
                original.backup(snapshot)
            shutil.copytree(args.workspace_copy / "projects", app.PROJECTS_DIR)
        else:
            app.PROJECTS_DIR.mkdir()
        app.call_ai = fake_call_ai
        os.environ['PWA_HOST_BUDGET_DB'] = str(root / 'host-budget.sqlite3')
        app.init_db()
        if not args.workspace_copy:
            from tests.provider_fixtures import install_default_provider_fixture
            install_default_provider_fixture()
        uvicorn.run(app.app, host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
