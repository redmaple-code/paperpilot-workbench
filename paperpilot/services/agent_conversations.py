"""Durable project-owned conversations and immutable turn targets.

No model calls occur here. API callers commit a turn before scheduling its run.
"""
from __future__ import annotations
import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from paperpilot.services.project_errors import ProjectServiceError

ACTIVE = ('queued', 'running', 'awaiting_human')
TERMINAL = ('succeeded', 'failed', 'blocked', 'cancelled', 'interrupted')
MODULES = {
    'research': ('研究问题与创新', 'STAGE-01', '负责领域学习、研究发现与知识证据图，使用 scholar-apprentice 研究工具。'),
    'outline': ('章节大纲', 'STAGE-02', '负责章节组织、章节目标和结构提案。'),
    'structure': ('文章结构', 'STAGE-03', '负责结构与论证顺序、章节衔接。'),
    'evidence': ('术语与证据准备', 'STAGE-04', '负责术语定义、证据来源、缺口和补充动作。'),
    'resources': ('工作量与资源计划', 'STAGE-05', '负责材料缺口、资源准备和可执行的工作计划。'),
    'logic': ('论证与逻辑', '', '负责主张、证据和论证依赖的核查。'),
    'delivery': ('检查与交付', '', '负责写作准备、检查与交付要求。'),
    'writing': ('正文写作', '', '负责当前正文的起草、润色与修订候选。'),
    'knowledge': ('知识与文献', '', '负责资料检索、文献整理和来源核对。'),
    'visual': ('图像工作台', '', '负责图示方案、图像资料与论文配图。'),
    'reading': ('PDF 阅读', '', '负责论文阅读、段落解释与阅读笔记。'),
}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def compact(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def fingerprint(value):
    return hashlib.sha256(compact(value).encode()).hexdigest()


def text_hash(value):
    return hashlib.sha256(value.encode('utf8')).hexdigest()


def initialize_schema(conn):
    conn.executescript('''
    CREATE TABLE IF NOT EXISTS agent_conversations(
      id TEXT PRIMARY KEY,project_id TEXT NOT NULL,title TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 0,
      consent_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS agent_conversations_project ON agent_conversations(project_id,updated_at);
    CREATE TABLE IF NOT EXISTS agent_messages(
      id TEXT PRIMARY KEY,project_id TEXT NOT NULL,conversation_id TEXT NOT NULL,seq INTEGER NOT NULL,
      role TEXT NOT NULL,text TEXT NOT NULL DEFAULT '',cards_json TEXT NOT NULL DEFAULT '[]',
      context_json TEXT NOT NULL DEFAULT '{}',run_id TEXT NOT NULL DEFAULT '',created_at TEXT NOT NULL,
      UNIQUE(conversation_id,seq));
    CREATE TABLE IF NOT EXISTS agent_turns(
      id TEXT PRIMARY KEY,project_id TEXT NOT NULL,conversation_id TEXT NOT NULL,request_id TEXT NOT NULL,
      input_fingerprint TEXT NOT NULL,run_id TEXT NOT NULL,target_json TEXT NOT NULL DEFAULT '{}',
      created_at TEXT NOT NULL,UNIQUE(project_id,conversation_id,request_id));
    CREATE TABLE IF NOT EXISTS agent_card_decisions(
      project_id TEXT NOT NULL,conversation_id TEXT NOT NULL,card_id TEXT NOT NULL,action TEXT NOT NULL,
      target_json TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(project_id,conversation_id,card_id));
    CREATE TABLE IF NOT EXISTS agent_resume_requests(
      run_id TEXT NOT NULL,request_id TEXT NOT NULL,input_hash TEXT NOT NULL,
      created_at TEXT NOT NULL,PRIMARY KEY(run_id,request_id));
    ''')
    conversation_columns = {r['name'] for r in conn.execute('PRAGMA table_info(agent_conversations)')}
    for name in ('parent_id', 'module_key'):
        if name not in conversation_columns:
            conn.execute(f"ALTER TABLE agent_conversations ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS agent_paper_root ON agent_conversations(project_id) WHERE module_key='paper' AND parent_id=''")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS agent_module_child ON agent_conversations(parent_id,module_key) WHERE parent_id!='' AND module_key!=''")
    columns={r['name'] for r in conn.execute('PRAGMA table_info(agent_runs)')}
    for name,kind in {
        'conversation_id':"TEXT NOT NULL DEFAULT ''",'turn_id':"TEXT NOT NULL DEFAULT ''",
        'input_json':"TEXT NOT NULL DEFAULT '{}'",'provider_json':"TEXT NOT NULL DEFAULT '{}'",
        'limits_json':"TEXT NOT NULL DEFAULT '{}'",'budget_json':"TEXT NOT NULL DEFAULT '{}'",
        'cancel_requested':"INTEGER NOT NULL DEFAULT 0",'lease_token':"TEXT NOT NULL DEFAULT ''",
        'lease_until':"REAL NOT NULL DEFAULT 0",'runtime_version':"INTEGER NOT NULL DEFAULT 2",
        'resume_count':"INTEGER NOT NULL DEFAULT 0",
    }.items():
        if name not in columns:
            conn.execute(f'ALTER TABLE agent_runs ADD COLUMN {name} {kind}')
            if name=='runtime_version':conn.execute('UPDATE agent_runs SET runtime_version=1')
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS agent_one_active_turn ON agent_runs(conversation_id) "
                 "WHERE conversation_id!='' AND status IN ('queued','running','awaiting_human')")


def get(conn, project_id, conversation_id):
    row=conn.execute('SELECT * FROM agent_conversations WHERE id=? AND project_id=?',(conversation_id,project_id)).fetchone()
    if row is None:raise ProjectServiceError(404,'conversation_not_found','此论文的会话不存在。')
    return dict(row)


def create(conn,project_id,title='新会话',parent_id='',module_key=''):
    if parent_id or module_key:
        if (module_key and module_key not in MODULES) or not parent_id:
            raise ProjectServiceError(422, 'invalid_agent_module', '请选择论文总对话下的有效模块。')
        parent = get(conn, project_id, parent_id)
        if parent['module_key'] != 'paper' or parent['parent_id']:
            raise ProjectServiceError(422, 'invalid_agent_parent', '模块子对话必须关联本论文的总对话。')
    cid=uuid.uuid4().hex;now=stamp()
    with conn:
        if module_key:
            conn.execute('BEGIN IMMEDIATE')
            old = conn.execute('SELECT id FROM agent_conversations WHERE project_id=? AND parent_id=? AND module_key=?', (project_id,parent_id,module_key)).fetchone()
            if old:
                return public(get(conn, project_id, old['id']))
        conn.execute('INSERT INTO agent_conversations(id,project_id,title,parent_id,module_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?)',
                     (cid,project_id,MODULES[module_key][0] if module_key else title,parent_id,module_key,now,now))
    return public(get(conn,project_id,cid))


def public(row):
    return {k:row[k] for k in ('id','project_id','title','revision','parent_id','module_key','created_at','updated_at')}


def workspace(conn, project_id, ensure=False):
    if ensure:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            root = conn.execute("SELECT * FROM agent_conversations WHERE project_id=? AND module_key='paper' AND parent_id=''", (project_id,)).fetchone()
            if root is None:
                # Retain the latest existing discussion as the paper's main conversation.
                root = conn.execute("SELECT * FROM agent_conversations WHERE project_id=? AND module_key='' AND parent_id='' ORDER BY updated_at DESC,id LIMIT 1", (project_id,)).fetchone()
                if root:
                    conn.execute("UPDATE agent_conversations SET module_key='paper',title='论文总对话' WHERE id=?", (root['id'],))
                    root_id = root['id']
                else:
                    root_id = uuid.uuid4().hex
                    conn.execute("INSERT INTO agent_conversations(id,project_id,title,module_key,created_at,updated_at) VALUES(?,?,'论文总对话','paper',?,?)", (root_id,project_id,stamp(),stamp()))
            else:
                root_id = root['id']
            for key, (title, _, _) in MODULES.items():
                conn.execute("INSERT OR IGNORE INTO agent_conversations(id,project_id,title,parent_id,module_key,created_at,updated_at) VALUES(?,?,?,?,?,?,?)", (uuid.uuid4().hex,project_id,title,root_id,key,stamp(),stamp()))
    rows = [public(dict(row)) for row in conn.execute('SELECT * FROM agent_conversations WHERE project_id=? ORDER BY updated_at DESC,id', (project_id,))]
    root = next((r for r in rows if r['module_key']=='paper' and not r['parent_id']), None)
    children = sorted([r for r in rows if root and r['parent_id']==root['id']], key=lambda r:list(MODULES).index(r['module_key']) if r['module_key'] in MODULES else len(MODULES))
    return {'root':root,'children':children,'other_conversations':[r for r in rows if r != root and r not in children]}


def identity(conversation):
    key = conversation.get('module_key', '')
    if key == 'paper':
        return {'module_key':key,'title':'论文总对话','role':'你是本论文的总 Agent，统筹研究、结构、材料、写作和交付。结合模块子对话进展帮助作者协调任务，明确模块产出与缺口。'}
    if key in MODULES:
        title, stage_id, role = MODULES[key]
        return {'module_key':key,'title':title,'stage_id':stage_id,'role':f'你是论文总 Agent 下的「{title}」子 Agent。{role} 保持本模块的连续上下文，参考总对话要求，跨模块任务向作者说明适合的子对话。'}
    if conversation.get('parent_id'):
        return {'module_key':'topic','title':conversation['title'],'role':f"你负责本论文的专题讨论「{conversation['title']}」。结合论文总对话目标与当前材料深入分析，将产出汇总为可核对的候选。"}
    return {}


def bind_request(conn, pid, payload):
    payload = dict(payload)
    cid = payload.get('conversation_id')
    role = identity(get(conn,pid,cid)) if cid else {}
    if role:
        payload['research_profile'] = 'scholar_apprentice' if role['module_key']=='research' else ''
        if role.get('stage_id'):
            payload['stage_id'] = role['stage_id']
        if role['module_key'] == 'paper':
            from paperpilot.services.plan_state import plan_from_snapshot
            from paperpilot.services.workflow import current_snapshot
            from paperpilot.services.agent_runtime import _writing_intent
            plan = plan_from_snapshot(current_snapshot(conn, pid))
            payload['stage_id'] = plan['stages'][plan['current_index']]['id']
            if payload['stage_id'] == 'STAGE-01' and not _writing_intent(payload.get('instruction', '')):
                payload['research_profile'] = 'scholar_apprentice'
    return payload


def related_context(conn, pid, conversation):
    """Bounded visible progress, never an untracked model-generated summary."""
    key = conversation.get('module_key', '')
    if key == 'paper':
        related = conn.execute('SELECT id,title,revision,module_key FROM agent_conversations WHERE project_id=? AND parent_id=? ORDER BY module_key', (pid,conversation['id'])).fetchall()
    elif conversation.get('parent_id'):
        related = conn.execute('SELECT id,title,revision,module_key FROM agent_conversations WHERE project_id=? AND id=?', (pid,conversation['parent_id'])).fetchall()
    else:
        return []
    result = []
    character_limit = 160 if key == 'paper' else 800
    for item in related:
        messages = conn.execute("""SELECT role,text FROM agent_messages WHERE project_id=? AND conversation_id=?
            AND text!='' AND COALESCE(json_extract(context_json,'$.result_local'),0)=0 ORDER BY seq DESC LIMIT 2""", (pid,item['id'])).fetchall()
        active = conn.execute("SELECT status FROM agent_runs WHERE project_id=? AND conversation_id=? ORDER BY created_at DESC LIMIT 1", (pid,item['id'])).fetchone()
        result.append(dict(item) | {'messages':[{'role':m['role'],'text':m['text'][:character_limit]} for m in reversed(messages)],'last_run_status':active['status'] if active else ''})
    return result


def list_all(conn,project_id):
    return [public(dict(r)) for r in conn.execute('SELECT * FROM agent_conversations WHERE project_id=? ORDER BY updated_at DESC,id LIMIT 100',(project_id,))]


def append_message(conn,project_id,cid,role,text='',cards=None,context=None,run_id=''):
    seq=conn.execute('SELECT COALESCE(MAX(seq),0)+1 FROM agent_messages WHERE conversation_id=?',(cid,)).fetchone()[0]
    mid=uuid.uuid4().hex
    conn.execute('INSERT INTO agent_messages(id,project_id,conversation_id,seq,role,text,cards_json,context_json,run_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                 (mid,project_id,cid,seq,role,text,compact(cards or []),compact(context or {}),run_id,stamp()))
    conn.execute('UPDATE agent_conversations SET revision=revision+1,updated_at=? WHERE id=?',(stamp(),cid))
    return mid


def messages(conn,pid,cid,cursor=0,limit=50,tail=False,before_seq=None):
    conversation=get(conn,pid,cid);limit=max(1,min(int(limit),100))
    if tail or before_seq:
        rows=list(conn.execute('SELECT * FROM agent_messages WHERE project_id=? AND conversation_id=? AND seq<? ORDER BY seq DESC LIMIT ?',
                     (pid,cid,before_seq or 9223372036854775807,limit+1)).fetchall())
        has_previous=len(rows)>limit
        rows=list(reversed(rows[:limit]))
    else:
        rows=conn.execute('SELECT * FROM agent_messages WHERE project_id=? AND conversation_id=? AND seq>? ORDER BY seq LIMIT ?',
                          (pid,cid,cursor,limit+1)).fetchall()
        has_previous=False
    out=[]
    for row in rows[:limit]:
        d=dict(row);d['cards']=json.loads(d.pop('cards_json'));d['context_snapshot']=json.loads(d.pop('context_json'));out.append(d)
    active=conn.execute("SELECT id,status FROM agent_runs WHERE project_id=? AND conversation_id=? AND status IN ('queued','running','awaiting_human') ORDER BY created_at DESC LIMIT 1",(pid,cid)).fetchone()
    return {'conversation':public(conversation),'messages':out,'next_cursor':out[-1]['seq'] if out else cursor,
            'has_more':len(rows)>limit,'has_previous':has_previous,'active_run':dict(active) if active else None,
            'decisions':{r['card_id']:r['action'] for r in conn.execute('SELECT card_id,action FROM agent_card_decisions WHERE project_id=? AND conversation_id=?',(pid,cid))}}


def history(conn,pid,cid):
    get(conn,pid,cid)
    rows=conn.execute('SELECT role,text,cards_json,context_json FROM agent_messages WHERE project_id=? AND conversation_id=? ORDER BY seq DESC LIMIT 80',(pid,cid)).fetchall()
    out=[]
    for row in reversed(rows):
        if json.loads(row['context_json']).get('result_local'):
            continue
        text=row['text']
        cards=[card for card in json.loads(row['cards_json']) if card.get('kind') != 'result_work']
        if cards:text+='\n候选：'+compact(cards)
        if text:out.append({'role':row['role'] if row['role'] in ('user','assistant') else 'user','content':text})
    decisions=conn.execute('SELECT card_id,action FROM agent_card_decisions WHERE project_id=? AND conversation_id=? ORDER BY created_at DESC LIMIT 20',(pid,cid)).fetchall()
    if decisions:out.append({'role':'user','content':'作者已明确的候选决定：'+compact([dict(d) for d in decisions])})
    return out


def validate_target(pid,target):
    target=dict(target or {})
    if target.get('project_id') and target['project_id']!=pid:
        raise ProjectServiceError(409,'target_project_changed','目标属于另一篇论文；请重新选择。')
    path=target.get('file_path','')
    if path and (path.startswith(('/','\\')) or ':' in path or '..' in path.replace('\\','/').split('/')):
        raise ProjectServiceError(422,'invalid_target','目标文件路径无效。')
    if target.get('selection_end',0)<target.get('selection_start',0):
        raise ProjectServiceError(422,'invalid_selection','选区范围无效。')
    target['project_id']=pid
    return target


def finish_message(conn,row,result,status,error=''):
    cid=row['conversation_id']
    if not cid:return
    attempt=int(row['resume_count'] or 0)
    if any(json.loads(r['context_json']).get('attempt',0)==attempt for r in conn.execute("SELECT context_json FROM agent_messages WHERE run_id=? AND role='assistant'",(row['id'],))):return
    cards=result.get('cards',[])
    target=json.loads(row['input_json']).get('context_snapshot',{})
    for card in cards:
        card.update(project_id=row['project_id'],conversation_id=cid,run_id=row['id'],schema_version=1)
        if card.get('kind')=='draft':card['target']=target
        for key,value in {'status':'proposed','title':card.get('operator') or card.get('idea') or '助手建议',
                          'summary':card.get('text') or card.get('gap') or '',
                          'source_refs':[{'kind':'agent_run','id':row['id']}], 'target':target,
                          'base_version':target.get('file_hash') or 0,
                          'limitations':'AI建议，尚需作者核对材料与来源。','actions':[]}.items():
            card.setdefault(key,value)
    text=result.get('text') or result.get('notes') or ('已完成。' if status=='succeeded' else error or '运行已停止，已有结果保留。')
    append_message(conn,row['project_id'],cid,'assistant',text,cards,context={'attempt':attempt},run_id=row['id'])


def decide_card(conn,pid,cid,card_id,payload):
    get(conn,pid,cid)
    card=None
    for row in conn.execute('SELECT cards_json FROM agent_messages WHERE project_id=? AND conversation_id=?',(pid,cid)):
        card=next((c for c in json.loads(row['cards_json']) if c['id']==card_id),None)
        if card:break
    if card is None:raise ProjectServiceError(404,'card_not_found','此会话没有这条候选。')
    target=card.get('target') if isinstance(card.get('target'),dict) else {}
    if payload['action'] in ('accepted','applied') and target.get('file_path')!=payload.get('file_path'):
        raise ProjectServiceError(409,'target_changed','候选目标已变化，未记录错误应用。')
    with conn:
        old=conn.execute('SELECT action FROM agent_card_decisions WHERE project_id=? AND conversation_id=? AND card_id=?',(pid,cid,card_id)).fetchone()
        if old and old['action']!=payload['action'] and not (old['action']=='accepted' and payload['action'] in ('applied','rejected')):
            raise ProjectServiceError(409,'decision_conflict','候选已经有不同决定，请先核对。')
        conn.execute('INSERT INTO agent_card_decisions VALUES(?,?,?,?,?,?) ON CONFLICT(project_id,conversation_id,card_id) DO UPDATE SET action=excluded.action,target_json=excluded.target_json,created_at=excluded.created_at',
                     (pid,cid,card_id,payload['action'],compact(payload),stamp()))
    return {'ok':True,'action':payload['action'],'card_id':card_id}
