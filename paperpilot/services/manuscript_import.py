"""Durable draft batches and authorized analyses for existing manuscripts."""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import uuid
from contextlib import closing

from paperpilot.services import agent_actions, manuscript_plan as plan, manuscript_sources as parser, provider_runtime
from paperpilot.services.ai_jobs import estimate_tokens
from paperpilot.services.project_errors import ProjectServiceError
from paperpilot.services.reference_library import normalize_title


def initialize_schema(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS manuscript_batches (
            id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE, fingerprint TEXT NOT NULL,
            name TEXT NOT NULL, target_mode TEXT NOT NULL, project_id TEXT,
            primary_source_id TEXT NOT NULL DEFAULT '', revision INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'staged', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS manuscript_sources (
            id TEXT PRIMARY KEY, batch_id TEXT NOT NULL REFERENCES manuscript_batches(id), parent_id TEXT,
            root_id TEXT NOT NULL, request_id TEXT NOT NULL, name TEXT NOT NULL, location TEXT NOT NULL,
            kind TEXT NOT NULL, sha256 TEXT NOT NULL, raw BLOB, fragments TEXT NOT NULL, warnings TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'active', created_at TEXT NOT NULL,
            UNIQUE(batch_id,request_id)
        );
        CREATE INDEX IF NOT EXISTS idx_manuscript_sources_batch ON manuscript_sources(batch_id, state, root_id);
        CREATE TABLE IF NOT EXISTS manuscript_analyses (
            batch_id TEXT NOT NULL REFERENCES manuscript_batches(id), request_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL, input_fingerprint TEXT NOT NULL, status TEXT NOT NULL,
            result TEXT NOT NULL DEFAULT '{}', owner_pid INTEGER NOT NULL, owner_token TEXT NOT NULL,
            created_at TEXT NOT NULL, PRIMARY KEY(batch_id,request_id)
        );
        CREATE TABLE IF NOT EXISTS manuscript_apply_receipts (
            batch_id TEXT NOT NULL REFERENCES manuscript_batches(id), request_id TEXT NOT NULL,
            fingerprint TEXT NOT NULL, result TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY(batch_id,request_id)
        );
        CREATE TABLE IF NOT EXISTS manuscript_applied (
            batch_id TEXT NOT NULL REFERENCES manuscript_batches(id), item_id TEXT NOT NULL,
            module TEXT NOT NULL, title TEXT NOT NULL, result TEXT NOT NULL, evidence TEXT NOT NULL,
            PRIMARY KEY(batch_id,item_id)
        );
    ''')


def recover_analyses(conn):
    from paperpilot.services.tex_runtime import owner_state

    begin(conn)
    for analysis in conn.execute("SELECT batch_id,request_id,owner_pid,owner_token FROM manuscript_analyses WHERE status='running'").fetchall():
        if owner_state(dict(analysis))=='dead':
            conn.execute("UPDATE manuscript_analyses SET status='interrupted' WHERE batch_id=? AND request_id=? AND status='running'",
                         (analysis['batch_id'],analysis['request_id']))


def error(status, code, message):
    return ProjectServiceError(status, code, message)


def begin(conn):
    if not conn.in_transaction:
        conn.execute('BEGIN IMMEDIATE')


def batch(conn, batch_id):
    row = conn.execute('SELECT * FROM manuscript_batches WHERE id=?', (batch_id,)).fetchone()
    if row is None:
        raise error(404,'manuscript_batch_missing','找不到此论文导入批次。')
    return dict(row)


def writable(conn, batch_id):
    row = batch(conn,batch_id)
    if row['status']=='cancelled':
        raise error(409,'manuscript_cancelled','此批次已取消。')
    if row['project_id']:
        agent_actions.require_writable(conn,row['project_id'])
    return row


def source_row(conn, batch_id, source_id, *, include_raw=False):
    columns = '*' if include_raw else 'id,batch_id,parent_id,root_id,kind,name,location,sha256,fragments,warnings,state'
    row = conn.execute(f'SELECT {columns} FROM manuscript_sources WHERE batch_id=? AND id=? AND state=?',
                       (batch_id,source_id,'active')).fetchone()
    if not row:
        raise error(404,'manuscript_source_missing','来源不存在、已移除或属于其他批次。')
    return dict(row)


def public_source(row):
    result = dict(row)
    result['fragments'] = json.loads(result['fragments'])
    result['warnings'] = json.loads(result['warnings'])
    return result


def sources(conn, batch_id):
    return [public_source(row) for row in conn.execute('''SELECT id,parent_id,root_id,name,kind,location,sha256,fragments,warnings,state
        FROM manuscript_sources WHERE batch_id=? AND state='active' ORDER BY created_at,rowid''',(batch_id,))]


def applied(conn,batch_id):
    return [dict(row) | {'result':json.loads(row['result']),'evidence':json.loads(row['evidence'])}
            for row in conn.execute('SELECT item_id,module,title,result,evidence FROM manuscript_applied WHERE batch_id=? ORDER BY rowid',(batch_id,))]


def view(conn, batch_id):
    row = batch(conn,batch_id)
    materials = sources(conn,batch_id)
    return {k:row[k] for k in ('id','name','target_mode','project_id','primary_source_id','revision','status','created_at','updated_at')} | {
        'sources':materials,'main_options':main_options(materials),'adopted':applied(conn,batch_id),
        'receipts':[json.loads(value['result']) for value in conn.execute(
            'SELECT result FROM manuscript_apply_receipts WHERE batch_id=? ORDER BY rowid DESC LIMIT 10',(batch_id,))],
        'analyses':[dict(value) | {'result':json.loads(value['result'])} for value in conn.execute(
            'SELECT request_id,status,result FROM manuscript_analyses WHERE batch_id=? ORDER BY rowid DESC LIMIT 10',(batch_id,))]}


def list_batches(conn):
    return [dict(row) for row in conn.execute('''SELECT id,name,target_mode,project_id,revision,status,created_at,updated_at
        FROM manuscript_batches ORDER BY updated_at DESC,id LIMIT 100''')]


def create(conn, request_id, mode, name, project_id=None):
    begin(conn)
    if not name.strip() and mode=='new':
        raise error(422,'manuscript_name_required','请输入新论文名称。')
    if mode=='existing' and not project_id or mode=='new' and project_id:
        raise error(422,'manuscript_target_invalid','请选择新建论文或一个有效的已有工作区。')
    digest = plan.key(mode,name.strip(),project_id)
    prior = conn.execute('SELECT id,fingerprint FROM manuscript_batches WHERE request_id=?',(request_id,)).fetchone()
    if prior:
        if prior['fingerprint']!=digest:
            raise error(409,'idempotency_key_reused','批次请求编号已用于另一份导入。')
        return view(conn,prior['id'])
    if project_id:
        project=agent_actions.require_writable(conn,project_id)
        name=project['name']
    identity=uuid.uuid4().hex
    stamp=provider_runtime.stamp()
    conn.execute('''INSERT INTO manuscript_batches(id,request_id,fingerprint,name,target_mode,project_id,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?)''',(identity,request_id,digest,name.strip(),mode,project_id,stamp,stamp))
    return view(conn,identity)


def touch(conn,batch_id):
    conn.execute('UPDATE manuscript_batches SET revision=revision+1,updated_at=? WHERE id=?',(provider_runtime.stamp(),batch_id))


def _fragments(identity, material):
    return [value | {'id':plan.key(identity,index,value['location'])}
            for index,value in enumerate(material.fragments)]


def upload(conn, batch_id, request_id, name, raw):
    writable(conn,batch_id)
    digest=hashlib.sha256(raw).hexdigest()
    prior=conn.execute('SELECT * FROM manuscript_sources WHERE batch_id=? AND request_id=?',(batch_id,request_id)).fetchone()
    if prior:
        if prior['sha256']!=digest or prior['name']!=name or prior['state']!='active':
            raise error(409,'idempotency_key_reused','此上传编号已用于其他来源或已被移除。')
        return view(conn,batch_id)
    extracted=parser.parse(name,raw)
    begin(conn)
    writable(conn,batch_id)
    prior=conn.execute('SELECT * FROM manuscript_sources WHERE batch_id=? AND request_id=?',(batch_id,request_id)).fetchone()
    if prior:
        if prior['sha256']!=digest or prior['name']!=name or prior['state']!='active':
            raise error(409,'idempotency_key_reused','此上传编号已用于其他来源或已被移除。')
        return view(conn,batch_id)
    existing=conn.execute('''SELECT COUNT(*),COALESCE(SUM(length(raw)),0) FROM manuscript_sources
        WHERE batch_id=? AND state='active' ''',(batch_id,)).fetchone()
    if existing[0]+len(extracted)>parser.MAX_SOURCES or existing[1]+sum(len(item.raw) for item in extracted)>parser.MAX_BATCH_BYTES:
        raise error(413,'manuscript_batch_capacity','本批次的文件数量或体积达到上限，请移除不需保留的材料。')
    root=uuid.uuid4().hex
    paths={}
    for index,item in enumerate(extracted):
        identity=root if not index else uuid.uuid4().hex
        parent=paths.get(item.parent) or (root if index else None)
        paths[item.name]=identity
        conn.execute('''INSERT INTO manuscript_sources(id,batch_id,parent_id,root_id,request_id,name,location,kind,sha256,raw,fragments,warnings,created_at)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)''',(identity,batch_id,parent,root,request_id if not index else f'{root}:{index}',item.name,
            item.location,item.kind,hashlib.sha256(item.raw).hexdigest(),item.raw,plan.dump(_fragments(identity,item)),plan.dump(item.warnings),provider_runtime.stamp()))
    touch(conn,batch_id)
    return view(conn,batch_id)


def main_options(materials):
    options = [value for value in materials if value['kind'] in plan.MAIN_KINDS and
               (not value['parent_id'] or value['kind']=='tex') and any(p['text'].strip() or p['heading'] for p in value['fragments'])]
    if any(value['parent_id'] and value['kind']=='tex' for value in options):
        roots = [value for value in options if not value['parent_id'] or value['name'].lower().endswith('/main.tex') or value['name'].lower()=='main.tex'
                 or any('\\documentclass' in piece['text'] for piece in value['fragments'])]
        if roots:
            options=roots
    return [{'id':value['id'],'name':value['name']} for value in options]


def select_primary(conn,batch_id,source_id):
    begin(conn)
    row=writable(conn,batch_id)
    if source_id not in {o['id'] for o in main_options(sources(conn,batch_id))}:
        raise error(422,'manuscript_primary_invalid','请选择一份可读取的稿件或工程主文件。')
    if row['primary_source_id']!=source_id:
        conn.execute('UPDATE manuscript_batches SET primary_source_id=? WHERE id=?',(source_id,batch_id))
        touch(conn,batch_id)
    return view(conn,batch_id)


def primary(row,materials):
    options=main_options(materials)
    if row['primary_source_id'] in {value['id'] for value in options}:
        return row['primary_source_id']
    if len(options)==1:
        return options[0]['id']
    if len(options)>1:
        raise error(409,'manuscript_primary_required','本批次有多份主稿，请先选择正文主源。')
    return ''


def revise_fragment(conn,batch_id,source_id,fragment_id,text,revision):
    begin(conn)
    row=writable(conn,batch_id)
    if revision!=row['revision']:
        raise error(409,'manuscript_stale','批次已变化，请刷新来源后再校对。')
    source=public_source(source_row(conn,batch_id,source_id))
    value=next((f for f in source['fragments'] if f['id']==fragment_id),None)
    if not value or source['kind'] in {'tex','bibliography','support','table'}:
        raise error(422,'fragment_not_editable','此来源使用原始文件，请上传修正版本。')
    if sum(len(f['text']) for f in source['fragments'])-len(value['text'])+len(text)>parser.MAX_TEXT:
        raise error(413,'source_capacity','校对文本超出容量，请拆分原文件。')
    value.update(text=text,mode='corrected')
    value.pop('issue',None)
    conn.execute('UPDATE manuscript_sources SET fragments=? WHERE id=?',(plan.dump(source['fragments']),source_id))
    touch(conn,batch_id)
    return view(conn,batch_id)


def retry_page(conn,batch_id,source_id,page,revision):
    writable(conn,batch_id)
    source=source_row(conn,batch_id,source_id,include_raw=True)
    if source['kind']!='pdf':
        raise error(422,'manuscript_pdf_required','按页重试仅适用于 PDF。')
    material=parser.parse(source['name'],source['raw'],selected_page=page)[0]
    begin(conn)
    row=writable(conn,batch_id)
    if row['revision']!=revision:
        raise error(409,'manuscript_stale','批次已变化，请刷新后再重试。')
    source=public_source(source_row(conn,batch_id,source_id))
    new=[f for f in source['fragments'] if f.get('page')!=page]
    for index,piece in enumerate(material.fragments):
        new.append(piece | {'id':plan.key(source_id,'retry',page,index)})
    new.sort(key=lambda f:f.get('page',0))
    conn.execute('UPDATE manuscript_sources SET fragments=? WHERE id=?',(plan.dump(new),source_id))
    touch(conn,batch_id)
    return view(conn,batch_id)


def removal_preview(conn,batch_id,source_id=None):
    row=batch(conn,batch_id)
    entries=sources(conn,batch_id)
    used=applied(conn,batch_id)
    referenced={ref['source_id'] for value in used for ref in value['evidence']}
    if source_id:
        source_row(conn,batch_id,source_id)
        selected={source_id}
        for item in entries:
            if item['parent_id'] in selected:
                selected.add(item['id'])
    else:
        protected=set(referenced)
        for item in reversed(entries):
            if item['id'] in protected and item['parent_id']:
                protected.add(item['parent_id'])
        selected={item['id'] for item in entries}-protected
    impact=[{'item_id':a['item_id'],'module':a['module'],'title':a['title'],'result':a['result']}
            for a in used if any(ref['source_id'] in selected for ref in a['evidence'])]
    removal=[{'id':i['id'],'name':i['name']} for i in entries if i['id'] in selected]
    return {'sources':removal,'impact':impact,'fingerprint':plan.key(row['revision'],source_id,removal,impact)}


def remove(conn,batch_id,fp,source_id=None):
    begin(conn)
    row=writable(conn,batch_id)
    preview=removal_preview(conn,batch_id,source_id)
    if fp!=preview['fingerprint']:
        raise error(409,'manuscript_stale','来源或依赖有变化，请重新核对再清理。')
    selected={entry['id'] for entry in preview['sources']}
    conn.executemany('UPDATE manuscript_sources SET state=?,raw=NULL,fragments=?,warnings=? WHERE id=?',
                     [('removed','[]','[]',sid) for sid in selected])
    if row['primary_source_id'] in selected:
        conn.execute('UPDATE manuscript_batches SET primary_source_id=? WHERE id=?',('',batch_id))
    if not source_id:
        conn.execute('UPDATE manuscript_batches SET status=? WHERE id=?',('cancelled',batch_id))
    # Previous model payloads may contain material from the removed sources.
    for analysis in conn.execute('SELECT request_id,result FROM manuscript_analyses WHERE batch_id=?',(batch_id,)).fetchall():
        result=json.loads(analysis['result'])
        result['items']=[item for item in result.get('items',[]) if not any(ref['source_id'] in selected for ref in item['evidence'])]
        conn.execute('UPDATE manuscript_analyses SET result=? WHERE batch_id=? AND request_id=?',(plan.dump(result),batch_id,analysis['request_id']))
    touch(conn,batch_id)
    return view(conn,batch_id)


def _provider(conn,row):
    if row['project_id']:
        return provider_runtime.request_provider(conn,row['project_id'])
    default=conn.execute("SELECT * FROM ai_provider_defaults WHERE id='default'").fetchone()
    if not default:
        raise error(409,'provider_binding_required','新论文须先设置已测试的默认文本模型。仍可使用本地解析与预览。')
    return provider_runtime.snapshot_for(conn,default['profile_id'],default['config_revision'],default['model_id'])


def _model_scope(conn,batch_id,indices=None):
    row=writable(conn,batch_id)
    materials=sources(conn,batch_id)
    selected=primary(row,materials)
    chunks=plan.packages(materials)
    total=len(chunks)
    indices=indices if indices is not None else list(range(min(total,3)))
    if len(indices)>6 or len(set(indices))!=len(indices) or any(i<0 or i>=total for i in indices):
        raise error(422,'manuscript_chunk_invalid','请选取有效片段范围，一次最多 6 批。')
    selected_batches=[]
    for index in indices:
        fragments=[{**fragment,'id':f'F{number}'} for number,fragment in enumerate(chunks[index],1)]
        payload=plan.dump([{key:fragment[key] for key in ('id','name','location','heading','text')}
                           for fragment in fragments])
        selected_batches.append({'index':index,'fragments':fragments,'payload':payload})
    snapshot=None
    issue=''
    try:
        snapshot=_provider(conn,row)
        from paperpilot.services.provider_config import resolve_credentials
        if snapshot['auth_mode']=='temporary':
            raise error(409,'provider_temporary_unsupported','请选择已配置持久凭据的提供方或本地模型。')
        resolve_credentials(conn,snapshot['profile_id'],snapshot['config_revision'])
        output=min(2400,snapshot['model_limits'][snapshot['model']]['output_tokens'])
        for batch in selected_batches:
            provider_runtime.admit_tokens(snapshot,snapshot['model'],estimate_tokens(plan.SYSTEM_PROMPT+batch['payload'])+256,output)
    except ProjectServiceError as exc:
        issue=exc.message
        output=0
    published = {k:snapshot[k] for k in ('name','model','base_url','profile_id','config_revision')} if snapshot else None
    input_fp=plan.key(selected,chunks)
    fp=plan.key(row['id'],row['revision'],input_fp,indices,snapshot,plan.SYSTEM_PROMPT,[batch['payload'] for batch in selected_batches])
    notices=[{'index':i,'sources':list(dict.fromkeys(p['name'] for p in c)),'characters':sum(len(p['text']) for p in c)}
             for i,c in enumerate(chunks)]
    public={'fingerprint':fp,'source_fingerprint':input_fp,'selected_indices':indices,'total_chunks':total,'chunks':notices,
            'provider':published,'allowed':bool(indices and snapshot and not issue),'unavailable':issue,
            'selected':selected_batches,'prompt':plan.SYSTEM_PROMPT,
            'reserved_output_per_chunk':output}
    return public,snapshot,row


def model_preview(conn,batch_id,indices=None):
    return _model_scope(conn,batch_id,indices)[0]


def model_run(database,batch_id,request_id,fp,indices,call=None):
    from paperpilot.services.tex_runtime import birth_token, owner_state

    with closing(database()) as conn:
        with conn:
            begin(conn)
            row=writable(conn,batch_id)
            old=conn.execute('SELECT * FROM manuscript_analyses WHERE batch_id=? AND request_id=?',(batch_id,request_id)).fetchone()
            if old:
                if old['fingerprint']!=fp:
                    raise error(409,'idempotency_key_reused','请求编号已用于另一个预览。')
                if old['status']=='running' and owner_state(dict(old))=='dead':
                    conn.execute("UPDATE manuscript_analyses SET status='interrupted' WHERE batch_id=? AND request_id=?",(batch_id,request_id))
                    return view(conn,batch_id)
                return view(conn,batch_id)
            for active in conn.execute("SELECT * FROM manuscript_analyses WHERE batch_id=? AND status='running'",(batch_id,)).fetchall():
                if owner_state(dict(active))!='dead':
                    raise error(409,'manuscript_analysis_running','此批次还有模型任务在执行，请先查看任务状态。')
                conn.execute("UPDATE manuscript_analyses SET status='interrupted' WHERE batch_id=? AND request_id=?",
                             (batch_id,active['request_id']))
            checked,snapshot,row=_model_scope(conn,batch_id,indices)
            if fp!=checked['fingerprint']:
                raise error(409,'manuscript_stale','来源、模型或发送范围已经改变，请重新查看预览。')
            if not checked['allowed']:
                raise error(409,'manuscript_model_unavailable',checked['unavailable'] or '没有可发送的正文片段。')
            conn.execute('''INSERT INTO manuscript_analyses(batch_id,request_id,fingerprint,input_fingerprint,status,owner_pid,owner_token,created_at)
                VALUES(?,?,?,?,?,?,?,?)''',(batch_id,request_id,fp,checked['source_fingerprint'],'running',os.getpid(),birth_token(os.getpid()) or '',provider_runtime.stamp()))
        result={'items':[],'outcomes':[]}

        def unchanged():
            try:
                current=writable(conn,batch_id)
                return current['revision']==row['revision'] and _provider(conn,current)==snapshot
            except ProjectServiceError:
                return False

        for selection in checked['selected']:
            index=selection['index']
            if not unchanged():
                result['outcomes'].append({'index':index,'status':'stale','message':'来源或接收方已变更，余下片段未发送。'})
                break
            try:
                response=provider_runtime.execute_call(conn,row['project_id'] or batch_id,f'manuscript:{batch_id}:{request_id}:{index}',
                    snapshot,'manuscript_import',plan.SYSTEM_PROMPT,selection['payload'],max_tokens=checked['reserved_output_per_chunk'],
                    guard=unchanged,scope={'batch':batch_id,'preview':fp,'index':index},call=call)
                if not response.get('ok'):
                    status='unknown' if response.get('error_code')=='provider_result_unknown' else 'failed'
                    result['outcomes'].append({'index':index,'status':status,'message':'模型未返回可用结果，发送状态见记录。'})
                else:
                    parsed=plan.model_items(response['content'],selection['fragments'])
                    result['items'].extend(parsed)
                    result['outcomes'].append({'index':index,'status':'succeeded','items':len(parsed),'usage':response.get('usage',{})})
            except ProjectServiceError as exc:
                result['outcomes'].append({'index':index,'status':'failed','message':exc.message})
            except (ValueError, KeyError, AttributeError, TypeError):
                result['outcomes'].append({'index':index,'status':'failed','message':'模型响应无法解析，请核对后重新分析此批。'})
            except OSError:
                result['outcomes'].append({'index':index,'status':'unknown','message':'模型通信中断，请先核对发送状态。'})
            with conn:
                begin(conn)
                if not unchanged():
                    result['items']=[]
                    conn.execute("UPDATE manuscript_analyses SET status='stale',result=? WHERE batch_id=? AND request_id=?",(plan.dump(result),batch_id,request_id))
                    return view(conn,batch_id)
                conn.execute('UPDATE manuscript_analyses SET result=? WHERE batch_id=? AND request_id=?',(plan.dump(result),batch_id,request_id))
        with conn:
            begin(conn)
            if not unchanged():
                result['items']=[]
                status='stale'
            else:
                status='succeeded' if all(v['status']=='succeeded' for v in result['outcomes']) else 'partial'
            conn.execute('UPDATE manuscript_analyses SET status=?,result=? WHERE batch_id=? AND request_id=?',(status,plan.dump(result),batch_id,request_id))
        return view(conn,batch_id)


def current_model_candidates(conn, row, materials, main):
    fingerprint=plan.key(main,plan.packages(materials))
    for analysis in conn.execute('''SELECT result FROM manuscript_analyses WHERE batch_id=? AND input_fingerprint=?
        AND status IN ('succeeded','partial','interrupted') ORDER BY rowid''',(row['id'],fingerprint)):
        yield from json.loads(analysis['result']).get('items',[])


def candidates(conn,row,materials):
    main=primary(row,materials)
    initial=plan.local_plan(materials,main,lambda sid:source_row(conn,row['id'],sid,include_raw=True)['raw'])
    keyed_references={(ref['source_id'],normalize_title(item['data']['title']))
                      for item in initial if item['kind']=='reference' and item['data'].get('cite_key')
                      for ref in item['evidence']}
    items={item['id']:item for item in initial}
    for item in current_model_candidates(conn,row,materials,main):
        if item['kind']=='reference' and any((ref['source_id'],normalize_title(item['data']['title'])) in keyed_references
                                             for ref in item['evidence']):
            continue
        items[item['id']]=item
    for module in plan.MODULES:
        if not any(item['module']==module and item['kind']!='note' for item in items.values()):
            title='当前来源没有可采用的内容'
            item=plan.candidate(module,'note',title,{'text':'请补充资料或分析相应来源，保留此模块的待补项。'},[],status='missing')
            items[item['id']]=item
    return list(items.values())
