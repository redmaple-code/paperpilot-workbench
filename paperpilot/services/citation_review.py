"""Version-bound, interruptible citation review. Each explicit step dispatches at most one claim."""
import copy
import json
import os
import uuid

from paperpilot.services import build_sources, citation_support, citation_rules, citation_semantic
from paperpilot.services import provider_runtime, human_revision
from paperpilot.services.ai_jobs import estimate_tokens
from paperpilot.services.content_versions import version_map
from paperpilot.services.context import redact_sensitive_text
from paperpilot.services.project_errors import ProjectServiceError

compact = citation_rules.compact
digest = citation_rules.digest
stamp = citation_support._now


def initialize_schema(conn):
    citation_rules.initialize_schema(conn)
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS citation_review_tasks(
        id TEXT PRIMARY KEY, project_id TEXT NOT NULL, request_id TEXT NOT NULL,
        fingerprint TEXT NOT NULL, status TEXT NOT NULL, options_json TEXT NOT NULL,
        snapshot_json TEXT NOT NULL, provider_json TEXT NOT NULL, created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL, cleanup_state TEXT NOT NULL DEFAULT 'retained',
        UNIQUE(project_id,request_id));
      CREATE TABLE IF NOT EXISTS citation_review_items(
        task_id TEXT NOT NULL, claim_id TEXT NOT NULL, sequence INTEGER NOT NULL,
        status TEXT NOT NULL, attempt INTEGER NOT NULL DEFAULT 0,
        report_json TEXT NOT NULL, result_json TEXT NOT NULL DEFAULT '{}',
        error_code TEXT NOT NULL DEFAULT '', PRIMARY KEY(task_id,claim_id));
      CREATE TABLE IF NOT EXISTS citation_review_events(
        task_id TEXT NOT NULL, request_id TEXT NOT NULL, claim_id TEXT NOT NULL,
        action TEXT NOT NULL, fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL,
        created_at TEXT NOT NULL, PRIMARY KEY(task_id,request_id));
    ''')


def options(payload):
    return {k: v for k, v in payload.items() if k not in {'temporary_key','consent_to_send','preview_fingerprint','request_id'}}


def positive_env(name, default):
    try:
        return max(1, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def snapshot(conn, project_id, opts):
    versions = version_map(conn, project_id)
    files = [dict(r) | {'version': versions.get(r['path'], 0)} for r in conn.execute('SELECT path,content FROM files WHERE project_id=? ORDER BY path', (project_id,))]
    kind = opts.get('input_kind', 'draft')
    purpose = {'saved': 'submitted', 'candidate': 'candidate_preview', 'draft': 'draft_preview'}[kind]
    materialized, source = build_sources.materialize(conn, project_id, files, purpose,
        draft_source=opts.get('draft_source','all') if kind == 'draft' else 'all', candidate_id=opts.get('candidate_id'))
    overridden = {entry['path']:entry['kind'] for entry in source.get('entries',[])}
    materialized = [dict(file, input_kind=overridden.get(file['path'],'saved')) for file in materialized]
    selected = set(opts.get('target_paths', ['main.tex']))
    if selected - {f['path'] for f in materialized} or any(not p.lower().endswith(('.tex','.ltx')) for p in selected):
        raise ProjectServiceError(422,'citation_target_invalid','请明确选择当前工程内的TeX文件，未自动扩大范围。')
    reports = []
    for file in materialized:
        if file['path'] not in selected:
            continue
        report = citation_support.analyze(conn, project_id, content=file['content'], target_path=file['path'])
        for claim in report['claims']:
            claim['id'] = digest([file['path'], file['content'], claim['id']])[:24]
            claim['target_path'] = file['path']
            claim['base_version'] = file['version']
        reports.append(citation_rules.enrich(conn,project_id,report,opts.get('paper_language','auto'),opts.get('claim_types')))
    sources = sorted({s['source_id'] for r in reports for s in r['source_pool']})
    scope = opts.get('source_ids')
    scope = sources if scope is None else sorted(set(scope))
    if set(scope) - set(sources):
        raise ProjectServiceError(422,'citation_source_scope_invalid','所选来源不在当前论文来源快照中。')
    from paperpilot.services.citation_engineering import scan
    return {'input_kind':kind,'input_manifest':source,'files':materialized,'reports':reports,
        'engineering':scan(materialized, sorted(selected)), 'source_scope':scope,
        'dependencies':citation_rules.identity(conn,project_id), 'options':opts}


def prompt_for(report, scope):
    payload = citation_semantic.model_input(report,scope)
    text, count = redact_sensitive_text(compact(payload))
    prompt = citation_semantic.prompt_for(json.loads(text))
    prompt += ('\n附加协议citation-review-v3：每条finding必须提供condition_results数组，'
        '各项含index、status(supported|contradicted|partial|unknown)、source_ids、reason。'
        '仅有经精确引句锚定且可靠原文的来源可支持条件。另提供language_review对象，含auxiliary_translation、'
        'concept_differences数组(各项source_id、paper_term、source_term、reason)。译释仅为辅助，保留原文引句；'
        '优先采用context.terms中作者明确约定的术语，分开列出术语差异和科学事实。')
    return prompt, count


def preview(conn, project_id, payload):
    snap = snapshot(conn,project_id,options(payload))
    provider = provider_runtime.request_provider(conn,project_id)
    output = positive_env('PWA_AI_MAX_OUTPUT_TOKENS',4000)
    cap = positive_env('PWA_AI_MAX_JOB_TOKENS',16000)
    previews=[]
    for report in snap['reports']:
        for claim in report['claims']:
            item=copy.deepcopy(report);item['claims']=[claim]
            prompt, redactions = prompt_for(item,snap['source_scope'])
            tokens=estimate_tokens(prompt)+256
            provider_runtime.admit_tokens({'model_limits':provider['model_limits']},provider['model'],tokens,output)
            if tokens+output>cap:
                raise ProjectServiceError(413,'budget_exceeded','单项审查输入超过任务硬上限；请缩小来源范围。')
            previews.append({'claim_id':claim['id'],'path':claim['target_path'],'prompt':prompt,'estimated_input_tokens':tokens,'redactions':redactions})
    identity=digest({'snapshot':snap,'provider':provider,'output_limit':output,'token_cap':cap})
    return {'preview_fingerprint':identity,'input_kind':snap['input_kind'],'input_manifest':snap['input_manifest'],
        'input_files':[{'path':f['path'],'version':f['version'],'sha256':digest(f['content'])} for f in snap['files']],
        'engineering':snap['engineering'],'source_scope':snap['source_scope'],'claim_count':len(previews),
        'provider':{k:v for k,v in provider.items() if k not in {'api_key','key'}},'outbound_items':previews,
        'max_output_tokens':output,'max_job_tokens':cap,'_snapshot':snap,'_provider':provider}


def row_for(conn,pid,task_id):
    row=conn.execute('SELECT * FROM citation_review_tasks WHERE id=? AND project_id=?',(task_id,pid)).fetchone()
    if not row:raise ProjectServiceError(404,'citation_task_missing','当前论文没有此审查任务。')
    return row


def fresh(conn,row):
    if row['status']=='deleted':return False
    try:
        return preview(conn,row['project_id'],json.loads(row['options_json']))['preview_fingerprint']==row['fingerprint']
    except (ProjectServiceError,ValueError,KeyError):
        return False


def filtered_item(conn,pid,report):
    filtered=citation_support.filter_current_permissions(conn,pid,report)
    # Suggestions, condition reasons and translations may derive from any sent source.
    lost=any(s.get('permission_filtered') for s in filtered.get('source_pool',[]))
    if lost:
        for c in filtered.get('claims',[]):
            c['semantic_review']={};c['evidence_candidates']=[];c['language_review']={}
            c['condition_results']=[{'index':i,'condition':x,'status':'unknown','reason':'来源授权已撤回，派生内容已清理'} for i,x in enumerate(c.get('conditions',[]))]
    return filtered,lost


def read(conn,pid,task_id):
    row=row_for(conn,pid,task_id)
    if row['status']=='deleted':
        return {'id':task_id,'status':'deleted','fingerprint':row['fingerprint'],'cleanup_state':row['cleanup_state'],'items':[],'stale':True}
    items=[];lost=False
    for item in conn.execute('SELECT * FROM citation_review_items WHERE task_id=? ORDER BY sequence',(task_id,)).fetchall():
        report,removed=filtered_item(conn,pid,json.loads(item['result_json'] if item['result_json']!='{}' else item['report_json']))
        lost=lost or removed
        if removed:
            # Persist cleanup, including original per-item inputs, to avoid hidden copies.
            original,_=filtered_item(conn,pid,json.loads(item['report_json']))
            with conn:
                conn.execute('UPDATE citation_review_items SET report_json=?,result_json=? WHERE task_id=? AND claim_id=?',
                    (compact(original),compact(report),task_id,item['claim_id']))
        events=[json.loads(e['payload_json']) | {'action':e['action']} for e in conn.execute("SELECT * FROM citation_review_events WHERE task_id=? AND claim_id=? AND action IN ('retain','dismiss','needs_evidence','correct_type','handoff') ORDER BY created_at",(task_id,item['claim_id']))]
        items.append({'claim_id':item['claim_id'],'status':item['status'],'attempt':item['attempt'],'error_code':item['error_code'],'report':report,'dispositions':events})
    if lost:
        for item in items:
            item['dispositions']=[{'action':x['action'],'reason':'来源撤权，人工记录中的派生文本已清理'} for x in item['dispositions']]
        snap=json.loads(row['snapshot_json'])
        snap['reports']=[filtered_item(conn,pid,r)[0] for r in snap['reports']]
        with conn:
            conn.execute("UPDATE citation_review_tasks SET snapshot_json=?,cleanup_state='permission_filtered',updated_at=? WHERE id=?",(compact(snap),stamp(),task_id))
            conn.execute("UPDATE citation_review_events SET payload_json='{}' WHERE task_id=?",(task_id,))
    is_fresh=fresh(conn,row)
    return {'id':task_id,'status':row['status'],'fingerprint':row['fingerprint'],'stale':not is_fresh,
        'input_kind':json.loads(row['options_json']).get('input_kind','draft'),'cleanup_state':'permission_filtered' if lost else row['cleanup_state'],
        'options':json.loads(row['options_json']),
        'engineering':json.loads(row['snapshot_json']).get('engineering',{}),'items':items,
        'coverage':{'total':len(items),'checked':sum(i['status']=='completed' for i in items),
            'uncovered':[{'claim_id':i['claim_id'],'reason':i['error_code'] or i['status']} for i in items if i['status']!='completed']},
        'automatic_writes':False,'requires_human_review':True}


def create(conn,pid,payload):
    p=preview(conn,pid,payload)
    if not payload.get('consent_to_send') or payload.get('preview_fingerprint')!=p['preview_fingerprint']:
        raise ProjectServiceError(409,'citation_preview_stale','请查看完整逐项外发预览并明确授权；输入变化需重新预览。')
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        old=conn.execute('SELECT * FROM citation_review_tasks WHERE project_id=? AND request_id=?',(pid,payload['request_id'])).fetchone()
        if old:
            if old['fingerprint']!=p['preview_fingerprint']:raise ProjectServiceError(409,'idempotency_key_reused','此编号已用于不同审查输入。')
            task_id=old['id']
        else:
            task_id=uuid.uuid4().hex;now=stamp()
            conn.execute('INSERT INTO citation_review_tasks(id,project_id,request_id,fingerprint,status,options_json,snapshot_json,provider_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (task_id,pid,payload['request_id'],p['preview_fingerprint'],'queued',compact(options(payload)),compact(p['_snapshot']),compact(p['_provider']),now,now))
            n=0
            for report in p['_snapshot']['reports']:
                for claim in report['claims']:
                    item=copy.deepcopy(report);item['claims']=[claim];n+=1
                    reusable=conn.execute("SELECT i.result_json FROM citation_review_items i JOIN citation_review_tasks t ON t.id=i.task_id WHERE t.project_id=? AND t.fingerprint=? AND t.status!='deleted' AND i.claim_id=? AND i.status='completed' ORDER BY t.created_at DESC LIMIT 1",(pid,p['preview_fingerprint'],claim['id'])).fetchone()
                    conn.execute('INSERT INTO citation_review_items(task_id,claim_id,sequence,status,report_json,result_json) VALUES(?,?,?,?,?,?)',
                        (task_id,claim['id'],n,'completed' if reusable else 'pending',compact(item),reusable['result_json'] if reusable else '{}'))
            if not conn.execute("SELECT 1 FROM citation_review_items WHERE task_id=? AND status!='completed'",(task_id,)).fetchone():conn.execute("UPDATE citation_review_tasks SET status='completed' WHERE id=?",(task_id,))
    return read(conn,pid,task_id)


def apply_details(report,response):
    decoded=citation_semantic._decode_response(response)
    findings={f.get('claim_id'):f for f in decoded['findings'] if isinstance(f,dict)}
    for claim in report['claims']:
        finding=findings.get(claim['id'],{})
        valid={f['source_id'] for f in claim.get('semantic_review',{}).get('sources',[]) if f.get('full_text') and f.get('relation') in {'supports','partially_supports','contradicts'}
               and not next((s.get('evidence_limits') for s in report['source_pool'] if s['source_id']==f['source_id']),['unknown'])}
        returned={c.get('index'):c for c in finding.get('condition_results',[]) if isinstance(c,dict) and type(c.get('index')) is int}
        for condition in claim['condition_results']:
            value=returned.get(condition['index'],{})
            ids=value.get('source_ids',[])
            if value.get('status') in {'supported','contradicted','partial'} and isinstance(ids,list) and ids and all(isinstance(s,str) and s in valid for s in ids):
                condition.update({'status':value['status'],'source_ids':ids,'reason':str(value.get('reason',''))[:1600]})
        language=finding.get('language_review',{})
        if isinstance(language,dict):
            claim['language_review'].update({'status':'reviewed','auxiliary_translation':str(language.get('auxiliary_translation',''))[:6000],
                'concept_differences':[x for x in language.get('concept_differences',[]) if isinstance(x,dict) and x.get('source_id') in valid][:30]})
    return citation_rules.enforce_limits(report)


def step(conn,pid,task_id,payload,call):
    row=row_for(conn,pid,task_id)
    if row['status'] not in {'queued','running'}:return read(conn,pid,task_id)
    if payload.get('preview_fingerprint')!=row['fingerprint'] or not payload.get('consent_to_send') or not fresh(conn,row):
        raise ProjectServiceError(409,'citation_task_stale','审查输入、权限或配置已变化，请建立新预览；已完成项保留。')
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        if row_for(conn,pid,task_id)['status'] not in {'queued','running'}:return read(conn,pid,task_id)
        prior=conn.execute('SELECT action,fingerprint FROM citation_review_events WHERE task_id=? AND request_id=?',(task_id,payload['request_id'])).fetchone()
        if prior:
            if prior['action']!='step' or prior['fingerprint']!=row['fingerprint']:raise ProjectServiceError(409,'idempotency_key_reused','此编号已用于不同任务操作。')
            return read(conn,pid,task_id)
        if conn.execute("SELECT 1 FROM citation_review_items WHERE task_id=? AND status='running'",(task_id,)).fetchone():
            raise ProjectServiceError(409,'citation_item_running','已有一项在核验，请等待或明确取消后再恢复。')
        item=conn.execute("SELECT * FROM citation_review_items WHERE task_id=? AND status='pending' ORDER BY sequence LIMIT 1",(task_id,)).fetchone()
        if not item:
            conn.execute("UPDATE citation_review_tasks SET status='completed',updated_at=? WHERE id=?",(stamp(),task_id))
            return read(conn,pid,task_id)
        attempt=item['attempt']+1
        conn.execute("UPDATE citation_review_items SET status='running',attempt=? WHERE task_id=? AND claim_id=?",(attempt,task_id,item['claim_id']))
        conn.execute("UPDATE citation_review_tasks SET status='running',updated_at=? WHERE id=?",(stamp(),task_id))
        conn.execute('INSERT INTO citation_review_events VALUES(?,?,?,?,?,?,?)',(task_id,payload['request_id'],item['claim_id'],'step',row['fingerprint'],'{}',stamp()))
    report=json.loads(item['report_json']);scope=json.loads(row['snapshot_json'])['source_scope']
    prompt,_=prompt_for(report,scope)
    def guard():
        current=row_for(conn,pid,task_id)
        return current['status']=='running' and fresh(conn,current) and conn.execute('SELECT attempt FROM citation_review_items WHERE task_id=? AND claim_id=?',(task_id,item['claim_id'])).fetchone()['attempt']==attempt
    try:
        response=provider_runtime.execute_call(conn,pid,f'{task_id}:{item["claim_id"]}:{attempt}',json.loads(row['provider_json']),
            'citation_compare',prompt,max_tokens=positive_env('PWA_AI_MAX_OUTPUT_TOKENS',4000),max_job_tokens=positive_env('PWA_AI_MAX_JOB_TOKENS',16000),
            temporary_key=payload.get('temporary_key') or None,call=call,scope={'task_id':task_id,'claim_id':item['claim_id'],'fingerprint':row['fingerprint']},guard=guard)
        if not response.get('ok'):raise ProjectServiceError(409,response.get('error_code','provider_failed'),'本项未完成，已保存先前结果；恢复前请核对可能费用。')
        result=citation_semantic.apply_response(report,response.get('content',''),model=json.loads(row['provider_json'])['model'],allowed_source_ids=scope)
        result=apply_details(result,response.get('content',''))
        expected={c['id'] for c in result['claims']}
        returned={f.get('claim_id') for f in citation_semantic._decode_response(response.get('content',''))['findings'] if isinstance(f,dict)}
        if not expected.issubset(returned):
            raise ProjectServiceError(409,'citation_unverifiable_response','响应未覆盖指定论断，本项保持未完成。')
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            if not guard():
                changed=conn.execute("UPDATE citation_review_items SET status='interrupted',error_code='late_result_discarded' WHERE task_id=? AND claim_id=? AND attempt=?",(task_id,item['claim_id'],attempt))
                if changed.rowcount and row_for(conn,pid,task_id)['status']=='running':conn.execute("UPDATE citation_review_tasks SET status='partial' WHERE id=?",(task_id,))
            else:
                conn.execute("UPDATE citation_review_items SET status='completed',result_json=?,error_code='' WHERE task_id=? AND claim_id=?",(compact(result),task_id,item['claim_id']))
                remaining=conn.execute("SELECT 1 FROM citation_review_items WHERE task_id=? AND status!='completed'",(task_id,)).fetchone()
                conn.execute('UPDATE citation_review_tasks SET status=?,updated_at=? WHERE id=?',('queued' if remaining else 'completed',stamp(),task_id))
    except (ProjectServiceError,ValueError,TypeError,KeyError) as exc:
        with conn:
            changed=conn.execute("UPDATE citation_review_items SET status='failed',error_code=? WHERE task_id=? AND claim_id=? AND attempt=? AND status='running'",(exc.code if isinstance(exc,ProjectServiceError) else 'provider_response_invalid',task_id,item['claim_id'],attempt))
            if changed.rowcount:conn.execute("UPDATE citation_review_tasks SET status='partial',updated_at=? WHERE id=? AND status IN ('queued','running')",(stamp(),task_id))
    return read(conn,pid,task_id)


def command(conn,pid,task_id,action,payload):
    with conn:
        conn.execute('BEGIN IMMEDIATE');row=row_for(conn,pid,task_id)
        if row['status']=='deleted':return read(conn,pid,task_id)
        command_hash=digest({k:v for k,v in payload.items() if k!='temporary_key'})
        prior=conn.execute('SELECT action,fingerprint FROM citation_review_events WHERE task_id=? AND request_id=?',(task_id,payload['request_id'])).fetchone()
        if prior:
            if prior['action']!=action or prior['fingerprint']!=command_hash:raise ProjectServiceError(409,'idempotency_key_reused','此编号已用于另一操作。')
            return read(conn,pid,task_id)
        if action=='cancel':
            conn.execute("UPDATE citation_review_tasks SET status='cancelled',updated_at=? WHERE id=?",(stamp(),task_id))
        elif action=='resume':
            if row['status'] not in {'cancelled','partial','running'} or not payload.get('consent_to_send') or payload.get('preview_fingerprint')!=row['fingerprint'] or not fresh(conn,row):
                raise ProjectServiceError(409,'citation_resume_stale','请对同一输入重新预览并授权恢复；输入变化需建立新任务。')
            if conn.execute("SELECT 1 FROM citation_review_items WHERE task_id=? AND status IN ('failed','running','interrupted')",(task_id,)).fetchone() and not payload.get('duplicate_cost_confirmed'):
                raise ProjectServiceError(409,'duplicate_cost_confirmation_required','中断项远端结果/费用可能未知；请先核对并明确确认可能重复费用。')
            # Increment attempts on invalidation so any old in-flight response cannot land.
            conn.execute("UPDATE citation_review_items SET status='pending',attempt=attempt+1,error_code='' WHERE task_id=? AND status!='completed'",(task_id,))
            conn.execute("UPDATE citation_review_tasks SET status='queued',updated_at=? WHERE id=?",(stamp(),task_id))
        elif action=='delete':
            if not payload.get('confirm'):raise ProjectServiceError(422,'citation_delete_confirmation_required','请确认只永久清除此报告快照和派生内容，原论文及来源保留。')
            conn.execute('DELETE FROM citation_review_items WHERE task_id=?',(task_id,))
            conn.execute('DELETE FROM citation_review_events WHERE task_id=?',(task_id,))
            conn.execute("UPDATE citation_review_tasks SET status='deleted',options_json='{}',snapshot_json='{}',provider_json='{}',cleanup_state='purged',updated_at=? WHERE id=?",(stamp(),task_id))
        else:raise ProjectServiceError(422,'citation_command_invalid','未知审查命令。')
        if action!='delete':conn.execute('INSERT INTO citation_review_events VALUES(?,?,?,?,?,?,?)',(task_id,payload['request_id'],'',action,command_hash,'{}',stamp()))
    return read(conn,pid,task_id)


def disposition(conn,pid,task_id,claim_id,payload):
    with conn:
        conn.execute('BEGIN IMMEDIATE');row=row_for(conn,pid,task_id)
        if not fresh(conn,row):raise ProjectServiceError(409,'citation_task_stale','旧版本人工处置不继承到新版本，请重新审查。')
        item=conn.execute('SELECT * FROM citation_review_items WHERE task_id=? AND claim_id=?',(task_id,claim_id)).fetchone()
        if not item:raise ProjectServiceError(404,'citation_claim_missing','报告内没有此论断。')
        key=digest(payload)
        old=conn.execute('SELECT fingerprint FROM citation_review_events WHERE task_id=? AND request_id=?',(task_id,payload['request_id'])).fetchone()
        if old:
            if old['fingerprint']!=key:raise ProjectServiceError(409,'idempotency_key_reused','此编号已用于不同人工处置。')
            return {'ok':True,'idempotent_replay':True,'evidence_status_unchanged':True}
        if payload['action']=='handoff':
            if item['status']!='completed':raise ProjectServiceError(409,'citation_item_incomplete','未完成项不能交接建议。')
            report,_=filtered_item(conn,pid,json.loads(item['result_json']));claim=report['claims'][0]
            suggestion=claim.get('semantic_review',{}).get('suggestion',{})
            text=suggestion.get('text')
            if not text or not suggestion.get('quote_preserved') or not suggestion.get('citation_keys_valid'):
                raise ProjectServiceError(409,'citation_suggestion_invalid','没有可交接且保留原引句/引用键的建议。')
            snap=json.loads(row['snapshot_json']);file=next(f for f in snap['files'] if f['path']==claim['target_path'])
            if snap['input_kind']=='candidate':raise ProjectServiceError(409,'citation_candidate_read_only','候选预检只读，请先审阅候选后在当前草稿重新核验。')
            start,end=claim['char_start'],claim['char_end']
            if file['content'][start:end]!=claim['text']:raise ProjectServiceError(409,'citation_anchor_stale','论断锚点不匹配，未写入。')
            result=human_revision.save_draft(conn,pid,'file',file['path'],file['content'][:start]+text+file['content'][end:],file['version'],
                expected_draft_updated_at=payload.get('expected_draft_updated_at'),conditional=True)
            if not result.get('ok'):raise ProjectServiceError(409,'citation_draft_conflict','人工修订草稿已变化，请比较后重试。')
        if payload['action']=='correct_type' and not payload.get('claim_type'):
            raise ProjectServiceError(422,'citation_claim_type_required','请明确指定修正后的论断类型；仍需新预览核验。')
        conn.execute('INSERT INTO citation_review_events VALUES(?,?,?,?,?,?,?)',(task_id,payload['request_id'],claim_id,payload['action'],key,compact(payload),stamp()))
    return {'ok':True,'evidence_status_unchanged':True,'new_preview_required':payload['action']=='correct_type','automatic_write':False}
