"""Author-message decisions bind to a preview actually displayed by the client."""
import json
import re
from paperpilot.services import agent_actions, agent_conversations as store
from paperpilot.services.project_errors import ProjectServiceError


def initialize_schema(conn):
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS agent_preview_views(
        project_id TEXT NOT NULL, conversation_id TEXT NOT NULL, action_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL, PRIMARY KEY(project_id,conversation_id,action_id));
      CREATE TABLE IF NOT EXISTS agent_author_decisions(
        project_id TEXT NOT NULL, conversation_id TEXT NOT NULL, request_id TEXT NOT NULL,
        input_hash TEXT NOT NULL, result_json TEXT NOT NULL,
        PRIMARY KEY(project_id,conversation_id,request_id));
    ''')


def displayed(conn, pid, cid, action_id, fingerprint):
    store.get(conn, pid, cid)
    row = agent_actions.read(conn, pid, action_id)
    if row['conversation_id'] != cid or row['status'] != 'previewed' or row['preview_fingerprint'] != fingerprint:
        raise ProjectServiceError(409, 'preview_stale', '方案预览已变化，请重新审阅。')
    conn.execute('INSERT OR REPLACE INTO agent_preview_views VALUES(?,?,?,?)', (pid,cid,action_id,fingerprint))
    return {'ok':True}


def intent(text):
    text = text.strip().rstrip('。！!')
    if text in {'确认', '确认采用', '确认采纳', '确认这份方案', '就按这个', '确认使用', '采用这份方案', '采纳', '应用', '按这个执行'}:
        return 'accept'
    if text in {'不采纳', '拒绝这份方案', '拒绝候选', '放弃这份方案'}:
        return 'reject'
    if re.fullmatch(r'(采用|采纳|确认)第[一二两三四五六七八九十\d]+份.{0,12}', text):
        return 'accept'
    if re.fullmatch(r'只(接受|采纳|采用)前[一二两三四五六七八九十\d]+[点项条]', text):
        return 'partial'
    return ''


def number(text):
    match = re.search(r'[一二两三四五六七八九十\d]+', text)
    value = match.group() if match else ''
    if value.isdigit():
        return int(value)
    return {'一':1,'二':2,'两':2,'三':3,'四':4,'五':5,'六':6,'七':7,'八':8,'九':9,'十':10}.get(value,0)


def resolve(conn, pid, data, adapters):
    cid, text, request_id = data['conversation_id'], data['instruction'], data['request_id']
    store.get(conn,pid,cid)
    kind = intent(text)
    if not kind:
        return {'handled':False}
    digest = store.fingerprint(data)
    old = conn.execute('SELECT * FROM agent_author_decisions WHERE project_id=? AND conversation_id=? AND request_id=?', (pid,cid,request_id)).fetchone()
    if old:
        if old['input_hash'] != digest:
            raise ProjectServiceError(409,'idempotency_key_reused','确认请求编号已用于其他输入。')
        return json.loads(old['result_json']) | {'idempotent_replay':True}
    rows = [dict(r) for r in conn.execute("SELECT * FROM agent_actions WHERE project_id=? AND conversation_id=? AND status IN ('proposed','previewed') ORDER BY created_at,id", (pid,cid))]
    selected = data.get('action_id')
    ordinal = number(text) if '第' in text else 0
    type_names = {'大纲':'outline','研究方案':'planning','研究候选':'research','术语':'term','文献':'reference','论证':'logic','语料':'corpus'}
    for label, operation in type_names.items():
        if ordinal and label in text:
            rows = [row for row in rows if row['operation']==operation]
    if selected:
        rows = [r for r in rows if r['id'] == selected]
    elif ordinal:
        rows = rows[ordinal-1:ordinal]
    if len(rows) != 1:
        return {'handled':True,'status':'choose_target','message':'请选择要处理的具体方案。' if rows else '当前没有可处理的方案，请先生成并审阅候选。',
                'choices':[agent_actions.card(r) for r in rows]}
    row = rows[0]
    viewed = conn.execute('SELECT fingerprint FROM agent_preview_views WHERE project_id=? AND conversation_id=? AND action_id=?', (pid,cid,row['id'])).fetchone()
    if not viewed or row['status'] != 'previewed' or viewed['fingerprint'] != row['preview_fingerprint']:
        return {'handled':True,'status':'review_required','message':'请先查看当前方案差异，再确认采纳。','card':agent_actions.card(row)}
    if kind == 'partial':
        payload = json.loads(row['payload_json'])
        count = number(text)
        length = len(payload.get('operations') or payload.get('fields') or [])
        if row['operation'] not in ('outline','planning') or not 0 < count <= length:
            return {'handled':True,'status':'clarify_scope','message':'请指明要保留的具体内容，Agent 会据此整理新的候选。'}
        preview = agent_actions.preview(conn,pid,row['id'],{'selected_indices':list(range(count))},adapters)
        store.append_message(conn,pid,cid,'user',text,context={'action_id':row['id'],'decision_kind':'partial'})
        store.append_message(conn,pid,cid,'assistant','已按要求缩小范围，请审阅后确认。')
        result = {'handled':True,'status':'review_required','message':'已缩小范围，请审阅后再次确认。',
                  'card':agent_actions.card(agent_actions.read(conn,pid,row['id'])),'preview':preview}
    else:
        store.append_message(conn,pid,cid,'user',text,context={'action_id':row['id'],'preview_fingerprint':row['preview_fingerprint']})
        if kind == 'reject':
            result = {'handled':True, **agent_actions.reject(conn,pid,row['id'])}
        else:
            response = agent_actions.apply(conn,pid,row['id'],dict(request_id=request_id,expected_version=row['base_version'],
                                           action_fingerprint=row['preview_fingerprint'],accepted=True),adapters)
            result = {'handled':True, **response}
    conn.execute('INSERT INTO agent_author_decisions VALUES(?,?,?,?,?)', (pid,cid,request_id,digest,store.compact(result)))
    return result
