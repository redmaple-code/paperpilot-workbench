"""Simulated model, real persistent Agent and native scholar storage."""
import argparse
import json
import os
from pathlib import Path
import sys

import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def model(*args, **kwargs):
    messages = kwargs.get('messages') or []
    if not any('当前是 scholar-apprentice' in message.get('content', '') for message in messages if message['role'] == 'system'):
        return {'ok': True, 'content': '隔离模型回复，真实云模型尚未调用。', 'usage': {'total_tokens': 80}}
    last_user = max(index for index, message in enumerate(messages) if message['role'] == 'user')
    completed = [message for message in messages[last_user + 1:] if message['role'] == 'tool']
    action = 'inspect'
    arguments = {'action': action}
    if completed:
        inspected = json.loads(completed[0]['content'])['data']
        if len(completed) == 1:
            from tests.test_scholar_workspace import graph
            value = graph()
            source = inspected['available_sources'][0]
            value['sources'] = [{'id': source['id']}]
            value['units'][0]['evidence'] = [{'source_id': source['id'], 'locator': '作者在当前会话提出的目标', 'role': 'context'}]
            value['units'].extend([
                {'id': 'mechanism', 'kind': 'claim', 'title': '区分竞争机制', 'content': '候选解释需要有能区分它们的证据。',
                 'scope': {'object': '合成研究对象'}, 'direction_ids': ['d'], 'aliases': [], 'claim_type': 'hypothesis',
                 'conditions': ['固定研究对象与测量口径'], 'lifecycle': 'candidate', 'epistemic_status': 'unassessed', 'evidence': []},
                {'id': 'validation', 'kind': 'method', 'title': '最小判别对照', 'content': '先核查原文，再设计固定条件的判别对照。',
                 'scope': {'object': '合成验证方案'}, 'direction_ids': ['d'], 'aliases': [], 'lifecycle': 'candidate',
                 'epistemic_status': 'unassessed', 'evidence': []}])
            value['edges'] = [{'id': 'e1', 'source': 'mechanism', 'target': 'u', 'type': 'requires_understanding'},
                              {'id': 'e2', 'source': 'validation', 'target': 'mechanism', 'type': 'related_to'}]
            arguments = {'action': 'knowledge', 'expected_version': inspected['revision'], 'graph': value}
        elif len(completed) == 2:
            arguments = {'action': 'plan', 'expected_versions': {node['id']: node['version'] for node in inspected['routes']},
                'nodes': [{'id': 'goal', 'kind': 'goal', 'title': '明确可证伪的研究问题', 'acceptance': '有条件、有证据、有判别动作'},
                          {'id': 'route', 'kind': 'route', 'parent_id': 'goal', 'title': '竞争机理路线', 'rationale': '解释相同现象的不同可能原因'},
                          {'id': 'plan', 'kind': 'plan', 'parent_id': 'route', 'title': '固定条件对照', 'rationale': '比较可以改变判断的证据'},
                          {'id': 'task', 'kind': 'task', 'parent_id': 'plan', 'title': '核查最近邻与反例',
                           'spec': {'question': '哪些已有结果改变创新判断', 'method': '原始来源核对', 'acceptance': '支持与反证均有定位',
                                    'conditions': {'scope': '合成研究示例'}, 'inputs': []}, 'knowledge': [], 'depends_on': []}]}
        else:
            return {'ok': True, 'content': '我已将研究对象、竞争机制和判别方案写入研究图，并建立目标到任务的路线。\n这些都是待验证候选，下一步核查原始来源与最近邻。', 'usage': {'total_tokens': 120}}
    return {'ok': True, 'content': '', 'tool_calls': [{'id': 'scholar-' + str(len(completed)), 'type': 'function',
        'function': {'name': 'scholar_research', 'arguments': json.dumps(arguments, ensure_ascii=False)}}], 'usage': {'total_tokens': 100}}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--storage', required=True)
    parser.add_argument('--port', required=True, type=int)
    args = parser.parse_args()
    os.environ['PWA_DATA_DIR'] = args.storage
    os.environ['PWA_HOST_BUDGET_DB'] = str(Path(args.storage) / 'host-budget.db')
    import app
    from tests.provider_fixtures import install_default_provider_fixture
    app.call_ai = model
    app.init_db()
    install_default_provider_fixture()
    uvicorn.run(app.app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
