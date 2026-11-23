"""Red Maple Pi loop with host-owned I/O, checkpoints and one parent budget."""
from __future__ import annotations
import json
from pathlib import Path
import queue
import shutil
import subprocess
import threading
import uuid
import atexit

from paperpilot.services import agent_conversations as conversations, agent_tools
from paperpilot.services.agent_execution import ExecutionStopped

WORKER = Path(__file__).resolve().parents[2] / 'agent-base/src/paperpilot-worker.js'
VERSION = 'redmaple-paperpilot-1'
_IDLE = queue.LifoQueue(maxsize=4)
_PROCESSES = set()
_POOL_LOCK = threading.Lock()
_WARMED = False


def _terminate(process):
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill(); process.wait(timeout=5)
    process.stdin.close(); process.stdout.close()
    with _POOL_LOCK:
        _PROCESSES.discard(process)


def _close_workers():
    for process in list(_PROCESSES):
        _terminate(process)


atexit.register(_close_workers)


def _worker(node):
    while True:
        try:
            process, output = _IDLE.get_nowait()
        except queue.Empty:
            break
        if process.poll() is None:
            return process, output
        _terminate(process)
    process = subprocess.Popen([node, str(WORKER)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    with _POOL_LOCK:
        _PROCESSES.add(process)
    output = queue.Queue()
    def read():
        try:
            for line in process.stdout:
                output.put(json.loads(line))
        except (ValueError, OSError):
            output.put({'kind': 'fatal', 'payload': {'message': '红枫通信中断。'}})
        finally:
            output.put(None)
    threading.Thread(target=read, daemon=True).start()
    return process, output


def initialize_schema(conn):
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS agent_subtasks(
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, run_id TEXT NOT NULL,
        conversation_id TEXT NOT NULL, module_key TEXT NOT NULL, instruction TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'running', result_json TEXT NOT NULL DEFAULT '{}',
        state_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS agent_steering(
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, run_id TEXT NOT NULL,
        request_id TEXT NOT NULL, text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
        created_at TEXT NOT NULL, UNIQUE(run_id,request_id));
    ''')


def install_tools():
    from paperpilot.services import workspace_catalog
    for tool in workspace_catalog.tools():
        if tool.name not in agent_tools.REGISTRY:
            agent_tools.register(tool)
    if 'delegate_agent' not in agent_tools.REGISTRY:
        agent_tools.register(agent_tools.Tool('delegate_agent',
            '安排专项子任务并获得结果。主 Agent 负责汇总，子任务共用本轮预算。module_key可选research/outline/knowledge/writing/visual/delivery。',
            {'type': 'object', 'properties': {
                'module_key': {'type': 'string', 'enum': ['research', 'outline', 'knowledge', 'writing', 'visual', 'delivery']},
                'instruction': {'type': 'string', 'minLength': 1, 'maxLength': 2000}},
             'required': ['module_key', 'instruction'], 'additionalProperties': False},
            lambda args, context: context['delegate'](args), timeout_ms=300000,
            max_calls_per_run=6, retries=0, result_cap=16000))


def warm_workers():
    """Prepare the Pi workers when an author opens a workspace."""
    global _WARMED
    with _POOL_LOCK:
        warm = not _WARMED
        _WARMED = True
    if warm and shutil.which('node'):
        def prepare():
            process, output = _worker(shutil.which('node'))
            try:
                message=output.get(timeout=60)
                if message and message.get('kind')=='ready':
                    _IDLE.put_nowait((process,output))
                else:
                    _terminate(process)
            except (queue.Empty,queue.Full):
                _terminate(process)
        for _ in range(2):
            threading.Thread(target=prepare,daemon=True).start()


def execute(state, ctx, ledger, invoke_model, *, task_id='', checkpoint=None):
    from paperpilot.services import agent_runtime, innovation_points, scholar_workspace
    conn = ctx.limits['conn']
    node = shutil.which('node')
    if not node or not WORKER.exists():
        raise ExecutionStopped('agent_runtime_unavailable', '红枫运行环境不可用，请检查 Node 与 agent-base 安装。')
    system = agent_runtime.SYSTEM_PROMPT + '\n' + (state.get('agent_identity') or {}).get('role', '')
    system += '\n使用paper_tools查找完整能力目录。授权内可委派专项子任务，最终由你汇总成果及未解决问题。'
    if state.get('research_profile') == 'scholar_apprentice':
        system += '\n' + scholar_workspace.POLICY
    tools = [t for t in agent_tools.REGISTRY.values() if not task_id or t.name != 'delegate_agent']
    if state.get('research_profile') != 'scholar_apprentice':
        tools = [t for t in tools if t.name != 'scholar_research']
    if state.get('_result_grouping'):
        tools = [t for t in tools if t.name == 'group_result_library']
    saved_node = state.get('redmaple')
    reconciliations = {}
    if saved_node:
        for key, call in saved_node.get('calls', {}).items():
            if call.get('status') not in ('pending','unknown'):
                continue
            if call['kind'] == 'model':
                host_key = 'redmaple:' + key
            else:
                step=saved_node.get('runs',{}).get(task_id or ctx.run_id,{}).get('nextModel',0)
                host_key=f"tool:{step}:{call.get('toolCallId','')}"
                if task_id:
                    host_key=task_id+':'+host_key
            recorded = conn.execute("SELECT result_json FROM agent_call_ledger WHERE run_id=? AND call_key=? AND status='succeeded'", (ctx.run_id,host_key)).fetchone()
            if recorded:
                response=json.loads(recorded['result_json'])
                if call['kind'] != 'model':
                    packet={k:response.get(k,{} if k=='data' else '') for k in ('ok','error_code','error','data')}
                    reconciliations[key]={'content':[{'type':'text','text':conversations.compact(packet)}],
                                          'details':packet,'isError':not bool(response.get('ok'))}
                    data=response.get('data') or {}
                    cards=list(data.get('cards') or [])
                    if data.get('proposal'):
                        cards.append(data['proposal'])
                    if data.get('draft'):
                        cards.append({'id':uuid.uuid5(uuid.NAMESPACE_URL,ctx.run_id+host_key).hex,'kind':'draft','text':data['draft'],'target':state.get('target',{})})
                    known={card.get('id') for card in state.get('candidates') or []}
                    state.setdefault('candidates',[]).extend(card for card in cards if card.get('id') not in known)
                    continue
                content=[{'type':'text','text':response.get('content','')}]
                for tool in response.get('tool_calls') or []:
                    args=tool['function'].get('arguments') or '{}'
                    content.append({'type':'toolCall','id':tool['id'],'name':tool['function']['name'], 'arguments':json.loads(args) if isinstance(args,str) else args})
                reconciliations[key]={'role':'assistant','content':content,'api':'openai-completions','provider':'paperpilot',
                                      'model':json.loads(ledger.row()['provider_json']).get('model','paper-agent'),
                                      'usage':{'totalTokens':response.get('usage',{}).get('total_tokens',1),'cost':{'total':0}},
                                      'stopReason':'toolUse' if response.get('tool_calls') else 'stop','timestamp':0}
    active = ['business_context', 'propose_business', 'read_stage', 'write_draft',
              'scholar_research' if state.get('research_profile') == 'scholar_apprentice' else 'business_search',
              'paper_tools' if task_id else 'delegate_agent']
    if any(word in agent_runtime._task_text(state) for word in ('数据', '算例', '图表', '剖面', 'CSV', 'TSV')):
        active.insert(0, 'prepare_result')
    if innovation_points.discussion_requested(state.get('instruction', '')):
        active.insert(0, 'propose_innovation')
    if state.get('_result_grouping'):
        active = ['group_result_library']
    capacity=max(1,min(8,int(ctx.limits.get('max_tools',8))))
    declarations=[dict(name=t.name,description=t.description,parameters=t.parameters) for t in tools]
    if capacity<3:
        declarations=[dict(name='paper_tool',description='调用论文工具。name选择具体能力，arguments填写该工具所需参数。先用paper_tools查询用途与入口。',host_dispatch=True,
                           parameters={'type':'object','properties':{'name':{'type':'string','enum':[t.name for t in tools]},'arguments':{'type':'object'}},'required':['name','arguments']})]
    config = dict(model=json.loads(ledger.row()['provider_json']).get('model', 'paper-agent'), system=system,
                  history=state.get('messages') or [], snapshot=saved_node,
                  reconciliations=reconciliations,
                  instruction=agent_runtime._task_text(state) + '\n' + state.get('material_text', '') +
                    '\n已批准长期记忆\n' + conversations.compact(state.get('memory_items') or []),
                  requestId=task_id or ctx.run_id, maxTools=capacity,
                  activeTools=[name for name in active if any(t.name == name for t in tools)][:capacity-2] if capacity>=3 else [],
                  limits=dict(modelCalls=ledger.limits['model_calls'], toolCalls=ledger.limits['tool_calls'],
                              tokens=ledger.limits['tokens'], seconds=max(1, ledger.remaining_seconds())),
                  tools=declarations)
    process, output = _worker(node)
    completed = False
    def reply(message, result):
        process.stdin.write(conversations.compact({'id': message['id'], 'result': result}) + '\n')
        process.stdin.flush()

    try:
        process.stdin.write(conversations.compact(config) + '\n'); process.stdin.flush()
        while True:
            ledger.check()
            try:
                message = output.get(timeout=.2)
            except queue.Empty:
                continue
            if message is None:
                raise ExecutionStopped('agent_runtime_interrupted', '红枫运行中断，已保存检查点与执行记录。')
            kind, payload = message['kind'], message.get('payload') or {}
            if kind == 'ready':
                continue
            if kind == 'fatal':
                raise ExecutionStopped(payload.get('code', 'agent_runtime_failed'), payload.get('message', '红枫运行未完成。'))
            if kind == 'checkpoint':
                state['redmaple'] = payload['state']
                if checkpoint:
                    checkpoint(state)
                else:
                    step = conn.execute('SELECT COALESCE(MAX(superstep),-1)+1 FROM agent_checkpoints WHERE run_id=?', (ctx.run_id,)).fetchone()[0]
                    agent_runtime._checkpoint(conn, ctx.run_id, state, step, 'redmaple')
                result = {'ok': True}
            elif kind == 'model':
                result = invoke_model(payload['messages'], 'redmaple:' + payload['key'], payload.get('tools'))
            elif kind == 'tool':
                next_model=(state.get('redmaple') or {}).get('runs',{}).get(task_id or ctx.run_id,{}).get('nextModel',0)
                current = dict(state, _supersteps=next_model, _task_id=task_id,
                               tool_calls=[{'id': payload['id'], 'function': {'name': payload['name'], 'arguments': payload['args']}}])
                delta, _ = agent_runtime._node_act(current, ctx)
                for name in ('candidates', 'steps', 'knowledge'):
                    state.setdefault(name, []).extend(delta.get(name) or [])
                raw = delta['messages'][-1]['content']
                try:
                    result = json.loads(raw)
                except json.JSONDecodeError:
                    result = {'ok':True,'data':{'preview':raw},'truncated':True}
            elif kind == 'steering':
                result = {'items': []}
                if not task_id:
                    rows = conn.execute("SELECT * FROM agent_steering WHERE run_id=? AND status='queued' ORDER BY created_at,id", (ctx.run_id,)).fetchall()
                    result['items'] = [{'id':row['id'],'text':row['text']} for row in rows]
            elif kind == 'steering_ack':
                with conn:
                    for identity in payload.get('ids') or []:
                        conn.execute("UPDATE agent_steering SET status='received' WHERE id=? AND run_id=?", (identity,ctx.run_id))
                ctx.log('steering_received', {'count':len(payload.get('ids') or [])})
                result={'ok':True}
            elif kind == 'complete':
                state['final'] = agent_runtime._extract_final(payload.get('text', '')) or {'text': payload.get('text', '')}
                finalized, _ = agent_runtime._node_finalize(state,ctx)
                state['candidates'] = finalized['candidates']
                state['_stop'] = 'final' if payload['status'] == 'succeeded' else payload.get('errorCode') or payload['status']
                if payload.get('errorMessage'):
                    state['final']['error'] = payload['errorMessage']
                reply(message, {'ok': True})
                completed = True
                return state
            else:
                raise ExecutionStopped('agent_protocol_failed', '红枫返回了未知事件，已有结果保留。')
            reply(message, result)
    finally:
        # Wait for close() to finish before a different host turn can claim this worker.
        if completed:
            try:
                message = output.get(timeout=5)
                if message and message.get('kind') == 'ready':
                    _IDLE.put_nowait((process, output))
                else:
                    _terminate(process)
            except (queue.Empty, queue.Full):
                _terminate(process)
        else:
            _terminate(process)


def delegate(args, state, ctx, ledger, invoke_model):
    from paperpilot.services import agent_runtime, workflow
    from paperpilot.services.plan_state import plan_from_snapshot
    from paperpilot.services.agent_graph import RunContext
    conn, pid = ctx.limits['conn'], ctx.project_id
    task_id = uuid.uuid4().hex
    key, instruction = args['module_key'], args['instruction']
    workspace = conversations.workspace(conn, pid)
    topic = next((i for i in workspace['children'] if i['module_key'] == key), None)
    cid = topic['id'] if topic else ctx.limits['conversation_id']
    child = {k: v for k, v in state.items() if k in ('stage_id', 'stage_fields', 'target', 'material_text', 'memory_items')}
    child.update(goal=instruction, instruction=instruction, agent_identity=conversations.identity(topic or {}),
                 messages=[], candidates=[], steps=[], knowledge=[], research_profile=state.get('research_profile', '') if key == 'research' else '')
    if key == 'research':
        child['research_profile'] = 'scholar_apprentice'
    if child['agent_identity'].get('stage_id'):
        child['stage_id'] = child['agent_identity']['stage_id']
        plan = plan_from_snapshot(workflow.current_snapshot(conn,pid))
        child['stage_fields'] = next(stage['fields'] for stage in plan['stages'] if stage['id']==child['stage_id'])
    child_ctx = RunContext(run_id=ctx.run_id, project_id=pid, stage_id=child['stage_id'], limits=ctx.limits,
                           emit=lambda kind,payload:ctx.log(kind,payload))
    with conn:
        conn.execute('INSERT INTO agent_subtasks(id,project_id,run_id,conversation_id,module_key,instruction,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)',
                     (task_id, pid, ctx.run_id, cid, key, instruction, conversations.stamp(), conversations.stamp()))
    ctx.log('subtask_started', {'id': task_id, 'module_key': key, 'instruction': instruction, 'conversation_id': cid})
    def save(value):
        with conn:
            conn.execute('UPDATE agent_subtasks SET state_json=?,updated_at=? WHERE id=?', (conversations.compact(value), conversations.stamp(), task_id))
    status, result = 'failed', {}
    try:
        execute(child, child_ctx, ledger, invoke_model, task_id=task_id, checkpoint=save)
        status = 'succeeded' if child.get('_stop') == 'final' else 'blocked'
        result = {'text': child.get('final', {}).get('text', ''), 'cards': child.get('candidates') or [], 'stop_reason': child.get('_stop')}
        return {'task_id': task_id, 'module_key': key, 'status': status, 'conversation_id': cid, **result}
    except ExecutionStopped as exc:
        status = 'cancelled' if exc.code == 'cancelled' else 'blocked'
        result = {'error_code': exc.code, 'text': str(exc)}
        raise
    finally:
        with conn:
            conn.execute('UPDATE agent_subtasks SET status=?,result_json=?,updated_at=? WHERE id=?',
                         (status, conversations.compact(result), conversations.stamp(), task_id))
            conversations.append_message(conn, pid, cid, 'assistant', '专项任务\n' + instruction + '\n\n' + result.get('text', ''),
                                         cards=result.get('cards') or [], context={'subtask_id': task_id, 'parent_run_id': ctx.run_id})
        ctx.log('subtask_finished', {'id': task_id, 'status': status, 'module_key': key, 'conversation_id': cid})
