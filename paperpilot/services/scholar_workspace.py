"""Project-owned bridge to the installed scholar-apprentice native research store."""
from __future__ import annotations

import hashlib
import copy
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile

from paperpilot.services.project_errors import ProjectServiceError
from paperpilot.services import agent_transport

GRAPH_ID = 'paperpilot'
_projects_dir = None
POLICY = """当前是 scholar-apprentice 领域研究与发现模式。
先调用 scholar_research inspect 读取本论文已有知识、路线和可引用来源，再承接作者问题。
按问题主动学习核心概念、理论前提、机制、边界、评价标准和失败案例。
复原代表工作的前作缺陷、原因假说、构造、机制、验证和局限，区分原论文与分析重构。
使用 search_academic 搜索真实最近邻和相反证据，read_knowledge 读取已授权资料。
检索服务不可用时保留缺口和下一动作，不能编造论文、DOI或声称新颖性已经成立。
用 scholar_research knowledge 持久保存知识图，plan 保存 goal→route→plan→task 研究路线。
图的 directions 是从当前材料归纳的研究方向，units 表示具体知识，edges 带有明确关系。
知识图最少含 version='0.1', purpose, directions[], sources[], units[], edges[], tasks[], bindings[]。
unit含id,title,kind(concept|claim|method|practice|observation|resource),content,scope对象,
direction_ids[],aliases[],lifecycle='candidate',epistemic_status='unassessed',evidence[]。
claim另含claim_type和conditions[]，evidence含source_id、locator、role(context|supports|challenges)。
sources仅填写inspect提供的来源id，服务端补齐真实来源。无来源的假说明确保留为待验证。
edge含id,source,target,type，关系可用requires_understanding/supports/challenges/explains/
contrasts_with/operationalizes/evaluated_by/applies_to/related_to；不可把箭头当作证明。
graph可含stage_draft={research_question,contribution,scope}三个字符串，由作者采纳后进入规划草稿。
每轮保留当前目标、可行竞争解释、证据缺口和最小判别动作，并将重要变化写回图和路线。
契约可通过contract创建，check_contract核对实际义务；图谱保存和本轮回答不代表整体研究完成。
contract含objective、deliverable='plan'、exclusions[]、requirements[]。每项义务含id,question,
criterion,phase,allowed_outcomes(supported|refuted|bounded_inconclusive)，五种phase须覆盖
framing、knowledge、challenge、verification、synthesis。来源由服务端绑定作者原始目标。
自产数值需要仪器落成和覆盖证据，本工具不执行数值实验。研究方案与已验证结果分别表述。
无需让作者手填研究字段。通过对话澄清必要条件，给出可审阅的研究结构和下一步。"""


def configure(projects_dir):
    global _projects_dir
    _projects_dir = projects_dir


def skill_info():
    explicit = os.environ.get('PWA_SCHOLAR_APPRENTICE_DIR')
    locations = [Path(explicit)] if explicit else [
        Path.home() / '.codex/skills/scholar-apprentice',
        Path.home() / '.agents/skills/scholar-apprentice',
    ]
    folder = next((path for path in locations if (path / 'scripts/research_cli.py').is_file()), None)
    if folder is None:
        return {'available': False, 'name': 'scholar-apprentice', 'reason': '请安装 scholar-apprentice 或配置其目录。'}
    fingerprints = {}
    for name in ['SKILL.md', 'scripts/research_store.py', 'references/research-contract.md',
                 'references/domain-knowledge-model.md', 'references/research-route-reconstruction.md']:
        path = folder / name
        if path.is_file():
            fingerprints[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    identity = hashlib.sha256(json.dumps(fingerprints, sort_keys=True).encode()).hexdigest()
    return {'available': True, 'name': 'scholar-apprentice', 'identity': identity,
            'native_store': 'research_cli / structure.db', '_folder': str(folder), 'source_hashes': fingerprints}


def public_skill():
    return {key: value for key, value in skill_info().items() if not key.startswith('_')}


def workspace(project_id):
    if _projects_dir is None or not re.fullmatch(r'[a-f0-9]{12}', project_id):
        raise ProjectServiceError(422, 'research_workspace_invalid', '研究工作区身份无效。')
    root = Path(_projects_dir()).resolve()
    paper = root / project_id
    destination = paper / 'research'
    if paper.is_symlink() or destination.is_symlink() or not destination.resolve().is_relative_to(root):
        raise ProjectServiceError(409, 'research_workspace_invalid', '研究目录需位于当前论文目录内。')
    for child in ['db', 'requests', 'origin', '研究档案']:
        if (destination / child).is_symlink():
            raise ProjectServiceError(409, 'research_workspace_invalid', '研究目录中的存储路径需为本地目录。')
    if (destination / 'db/structure.db').is_symlink():
        raise ProjectServiceError(409, 'research_workspace_invalid', '研究数据库需属于当前论文工作区。')
    return destination


def snapshot(project_id):
    result = {'revision': 0, 'graph': {}, 'routes': [], 'contracts': [], 'skill': public_skill()}
    contracts = workspace(project_id) / '研究档案/研究合同'
    if contracts.is_dir():
        for path in sorted(contracts.glob('*/contract.json'))[-10:]:
            contract = json.loads(path.read_text(encoding='utf8'))
            result['contracts'].append({key: contract.get(key) for key in ['id', 'objective', 'deliverable', 'requirements']})
    path = workspace(project_id) / 'db/structure.db'
    if not path.is_file():
        return result
    conn = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('BEGIN')
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'research_graphs' in tables:
            graph = conn.execute('SELECT * FROM research_graphs WHERE id=?', (GRAPH_ID,)).fetchone()
            if graph:
                result['revision'] = graph['version']
                result['graph'] = json.loads(graph['metadata'])
                for section in ['directions', 'sources', 'units', 'edges', 'tasks', 'bindings', 'research_cases', 'workflow_patterns']:
                    result['graph'][section] = [json.loads(row[0]) for row in conn.execute(
                        'SELECT payload FROM research_graph_items WHERE graph_id=? AND section=? ORDER BY ordinal', (GRAPH_ID, section))]
        if 'research_nodes' in tables:
            result['routes'] = [json.loads(row['payload']) | {'version': row['version']} for row in conn.execute(
                'SELECT payload,version FROM research_nodes ORDER BY ordinal,id')]
    finally:
        conn.close()
    return result


def available_sources(conn, project_id, conversation_id, state=None):
    output = {}
    if conversation_id:
        owner = conn.execute('SELECT project_id,parent_id FROM agent_conversations WHERE id=?', (conversation_id,)).fetchone()
        if not owner or owner[0] != project_id:
            raise ProjectServiceError(404, 'conversation_not_found', '研究会话不属于当前论文。')
        root_id = owner['parent_id'] or conversation_id
        for row in conn.execute("""SELECT m.id,m.text,m.conversation_id FROM agent_messages m JOIN agent_conversations c
            ON c.id=m.conversation_id WHERE m.project_id=? AND c.project_id=? AND m.role='user'
            AND COALESCE(json_extract(m.context_json,'$.result_local'),0)=0
            AND (c.id=? OR c.parent_id=?) ORDER BY m.created_at DESC,m.seq DESC LIMIT 40""", (project_id,project_id,root_id,root_id)):
            identity = 'author:' + row['id']
            output[identity] = {'id': identity, 'title': row['text'][:80], 'kind': 'author_instruction',
                                'ref': 'conversation:' + row['conversation_id'] + '/message:' + row['id']}
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='agent_attachments'").fetchone():
            for row in conn.execute("""SELECT id,filename,sha256 FROM agent_attachments
                WHERE project_id=? AND conversation_id=? AND status='active' AND result_bytes IS NULL
                AND lower(filename) NOT LIKE '%.csv' AND lower(filename) NOT LIKE '%.tsv'
                ORDER BY created_at DESC LIMIT 10""", (project_id,conversation_id)):
                identity='attachment:'+row['id']
                output[identity]={'id':identity,'title':row['filename'],'kind':'uploaded_document','ref':identity,'version':row['sha256']}
    for row in conn.execute("SELECT c.id,c.title,s.version FROM corpus c JOIN corpus_source_versions s ON s.corpus_id=c.id WHERE c.project_id=? AND s.permission_state='authorized' AND s.status='active' ORDER BY s.version DESC LIMIT 50", (project_id,)):
        identity = f"corpus:{row['id']}:v{row['version']}"
        output[identity] = {'id': identity, 'title': row['title'], 'kind': 'authorized_corpus', 'ref': identity}
    for row in conn.execute('SELECT id,title,cite_key FROM refs WHERE project_id=? LIMIT 50', (project_id,)):
        identity = 'reference:' + row['id']
        output[identity] = {'id': identity, 'title': row['title'], 'kind': 'reference_metadata', 'ref': row['cite_key']}
    for item in (state or {}).get('knowledge', []):
        if item.get('search_id') and item.get('id'):
            identity = f"search:{item['search_id']}:{item['id']}"
            output[identity] = {'id': identity, 'title': item.get('title', ''), 'kind': 'search_pointer',
                                'ref': identity, 'access': 'metadata_only'}
    return output


def _native(project_id, script, arguments, payload=None):
    info = skill_info()
    if not info['available']:
        raise ProjectServiceError(503, 'scholar_unavailable', info['reason'])
    folder = workspace(project_id)
    folder.mkdir(parents=True, exist_ok=True)
    command = [sys.executable, '-X', 'utf8', str(Path(info['_folder']) / 'scripts' / script)]
    if payload is not None:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        requests = folder / 'requests'
        requests.mkdir(exist_ok=True)
        request = requests / (digest + '.json')
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf8', dir=requests, delete=False) as pending:
            pending.write(encoded)
        os.replace(pending.name, request)
        location = request.relative_to(folder).as_posix() if script == 'research_gate.py' else str(request)
        arguments = [*arguments, '--file', location]
    if script == 'research_gate.py':
        command += [arguments[0], '--ws', str(folder), *arguments[1:]]
    else:
        command += ['--ws', str(folder), *arguments]
    try:
        process = subprocess.run(command, capture_output=True, text=True, encoding='utf8',
                                 timeout=agent_transport.timeout(25), creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        response = json.loads(process.stdout)
    except subprocess.TimeoutExpired as exc:
        raise ProjectServiceError(503, 'scholar_timeout', '研究存储响应超时，请读取当前状态后继续。') from exc
    except (OSError, ValueError) as exc:
        raise ProjectServiceError(503, 'scholar_unavailable', '学徒研究接口暂时不可用，请核对 skill 安装。') from exc
    if not response.get('ok', script == 'research_gate.py' and 'error' not in response):
        raise ProjectServiceError(409, 'scholar_validation', str(response.get('error') or response.get('errors') or response)[:1500])
    return response


def _validate_graph(graph, sources, previous):
    if not isinstance(graph, dict) or len(json.dumps(graph, ensure_ascii=False)) > 48000:
        raise ProjectServiceError(422, 'research_graph_invalid', '研究图需为容量内的结构化对象。')
    for section, cap in [('units', 32), ('edges', 64), ('directions', 12), ('sources', 40)]:
        if not isinstance(graph.get(section), list) or len(graph[section]) > cap:
            raise ProjectServiceError(422, 'research_graph_invalid', f'请将 {section} 控制在 {cap} 项以内。')
    graph = copy.deepcopy(graph)
    old_sources = {source['id']: source for source in previous.get('sources', [])
                   if source.get('kind') in {'author_instruction', 'search_pointer'}}
    known = old_sources | sources
    selected = []
    for reference in graph['sources']:
        identity = reference.get('id') if isinstance(reference, dict) else None
        if identity not in known:
            raise ProjectServiceError(422, 'research_source_unknown', '来源须引用当前论文材料、真实检索结果或会话原文。')
        selected.append(known[identity])
    graph['sources'] = selected
    for unit in graph['units']:
        if unit.get('lifecycle') != 'candidate' or unit.get('epistemic_status') not in ('unassessed', 'contested'):
            raise ProjectServiceError(422, 'research_review_required', 'AI 工作图中的知识须保留候选与待核验状态。')
    draft = graph.get('stage_draft', {})
    if not isinstance(draft, dict) or set(draft) - {'research_question', 'contribution', 'scope'} or any(
            not isinstance(value, str) or len(value) > 16000 for value in draft.values()):
        raise ProjectServiceError(422, 'research_draft_invalid', '研究方案只包含研究问题、创新候选和研究范围。')
    return graph


def agent_snapshot(conn, project_id, conversation_id, state=None):
    current = snapshot(project_id)
    sources = available_sources(conn, project_id, conversation_id, state)
    unavailable = [source for source in current['graph'].get('sources', [])
                   if source.get('kind') in {'authorized_corpus', 'reference_metadata'} and source['id'] not in sources]
    if unavailable:
        current.update(graph={}, routes=[], contracts=[], omitted=['已有研究图包含撤权或不可用的来源，本轮需从当前授权材料重建。'])
    return current, sources


def _contract(args, context, request_id):
    pid = context['project_id']
    if context.get('stage_id') != 'STAGE-01':
        raise ProjectServiceError(409, 'research_stage_changed', '请在研究问题阶段建立研究合同。')
    origin = context['conn'].execute("SELECT id,text FROM agent_messages WHERE project_id=? AND conversation_id=? AND role='user' ORDER BY seq LIMIT 1",
                                     (pid, context.get('conversation_id', ''))).fetchone()
    if not origin:
        raise ProjectServiceError(422, 'research_origin_missing', '研究合同需要当前会话的作者原始目标。')
    folder = workspace(pid)
    source = folder / 'origin' / (origin['id'] + '.txt')
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(origin['text'], encoding='utf8')
    contract = dict(args.get('contract') or {})
    contract.update(id='research-' + request_id[:20], origin=[{'path': source.relative_to(folder).as_posix(),
                    'sha256': hashlib.sha256(source.read_bytes()).hexdigest(), 'locator': '当前会话最初的作者目标'}])
    prior = folder / '研究档案/研究合同' / contract['id'] / 'contract.json'
    if prior.is_file() and json.loads(prior.read_text(encoding='utf8')) == contract:
        return {'id': contract['id'], 'already_committed': True}
    return _native(pid, 'research_gate.py', ['seal'], contract)


def handle(args, context):
    from paperpilot.services.agent_actions import require_writable
    conn, pid = context['conn'], context['project_id']
    require_writable(conn, pid)
    if (context.get('state') or {}).get('research_profile') != 'scholar_apprentice':
        raise ProjectServiceError(409, 'research_mode_required', '请在领域研究对话中调用学徒研究工具。')
    current, sources = agent_snapshot(conn, pid, context.get('conversation_id', ''), context.get('state'))
    action = args['action']
    if action == 'inspect':
        return {'ok': True, **current, 'available_sources': list(sources.values())}
    request_id = hashlib.sha256((context.get('run_id', '') + json.dumps(args, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()
    if action == 'knowledge':
        graph = _validate_graph(args.get('graph'), sources, current['graph'])
        result = _native(pid, 'research_cli.py', ['knowledge'], {'request_id': request_id, 'graph_id': GRAPH_ID,
                         'expected_version': args.get('expected_version', 0), 'graph': graph})
    elif action == 'plan':
        nodes = args.get('nodes')
        if not isinstance(nodes, list) or not 1 <= len(nodes) <= 40:
            raise ProjectServiceError(422, 'research_plan_invalid', '研究路线需包含1至40个目标、路线、方案或任务节点。')
        result = _native(pid, 'research_cli.py', ['plan'], {'request_id': request_id, 'nodes': nodes,
                         'expected_versions': args.get('expected_versions', {})})
    elif action == 'contract':
        result = _contract(args, context, request_id)
    elif action == 'check_contract':
        identity = args.get('contract_id', '')
        if not re.fullmatch(r'research-[a-f0-9]{20}', identity):
            raise ProjectServiceError(422, 'research_contract_invalid', '请使用本论文已建立的合同编号。')
        folder = workspace(pid)
        state = args.get('contract_state') or {}
        requests = folder / 'requests'
        requests.mkdir(parents=True, exist_ok=True)
        path = requests / (request_id + '-state.json')
        path.write_text(json.dumps(state, ensure_ascii=False), encoding='utf8')
        result = _native(pid, 'research_gate.py', ['check', '--run', identity, '--state', path.relative_to(folder).as_posix()])
    else:
        raise ProjectServiceError(422, 'research_action_invalid', '请使用已登记的研究操作。')
    return {'ok': True, 'native_result': result, **snapshot(pid), 'note': '候选研究状态已持久保存，规划确认与科学验证分别执行。'}


def get_tools():
    from paperpilot.services.agent_tools import Tool, WRITE_CANDIDATE
    return [Tool('scholar_research', '调用 scholar-apprentice 原生研究知识库。先inspect读版本和来源，再knowledge保存候选图，plan保存研究路线。',
        {'type': 'object', 'properties': {'action': {'type': 'string', 'enum': ['inspect', 'knowledge', 'plan', 'contract', 'check_contract']},
         'expected_version': {'type': 'integer', 'minimum': 0}, 'graph': {'type': 'object'},
         'nodes': {'type': 'array', 'items': {'type': 'object'}, 'maxItems': 40}, 'expected_versions': {'type': 'object'},
         'contract': {'type': 'object'}, 'contract_id': {'type': 'string'}, 'contract_state': {'type': 'object'}},
         'required': ['action'], 'additionalProperties': False}, handle, side_effect=WRITE_CANDIDATE,
         timeout_ms=30000, max_calls_per_run=8, result_cap=48000, intents=('scholar',))]
