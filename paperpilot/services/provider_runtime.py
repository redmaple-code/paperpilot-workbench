"""Versioned provider admission and explicit, persisted generation tests."""
from __future__ import annotations
import hashlib
import json
import math
import uuid
import os
import time
from datetime import datetime, timezone

from paperpilot.services.project_errors import ProjectServiceError

MODES={'chat','stream','image_generation','image_edit','vision_analysis'}


def stamp():return datetime.now(timezone.utc).isoformat()
def compact(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def initialize_schema(conn):
    columns={r['name'] for r in conn.execute('PRAGMA table_info(ai_provider_profiles)')}
    for name,declaration in [('model_limits_json',"TEXT NOT NULL DEFAULT '{}'"),('revoked_before',"TEXT NOT NULL DEFAULT ''")]:
        if name not in columns:conn.execute(f'ALTER TABLE ai_provider_profiles ADD COLUMN {name} {declaration}')
    conn.executescript('''
      CREATE TABLE IF NOT EXISTS ai_provider_tests(
        id TEXT PRIMARY KEY,profile_id TEXT NOT NULL REFERENCES ai_provider_profiles(id),
        config_revision INTEGER NOT NULL,config_fingerprint TEXT NOT NULL,model_id TEXT NOT NULL,
        mode TEXT NOT NULL,status TEXT NOT NULL,error_code TEXT NOT NULL DEFAULT '',
        request_id TEXT NOT NULL UNIQUE,request_fingerprint TEXT NOT NULL,
        usage_json TEXT NOT NULL DEFAULT '{}',created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
      CREATE INDEX IF NOT EXISTS idx_provider_tests_ready ON ai_provider_tests(profile_id,config_fingerprint,model_id,mode,status);
      CREATE TABLE IF NOT EXISTS ai_provider_defaults(
        id TEXT PRIMARY KEY,profile_id TEXT NOT NULL,config_revision INTEGER NOT NULL,
        model_id TEXT NOT NULL,version INTEGER NOT NULL,updated_at TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS ai_provider_audit(
        id TEXT PRIMARY KEY,project_id TEXT,request_id TEXT NOT NULL,profile_id TEXT NOT NULL,
        config_revision INTEGER NOT NULL,model_id TEXT NOT NULL,action TEXT NOT NULL,
        context_hash TEXT NOT NULL,scope_hash TEXT NOT NULL,status TEXT NOT NULL,
        error_code TEXT NOT NULL DEFAULT '',usage_json TEXT NOT NULL DEFAULT '{}',
        elapsed_ms INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,updated_at TEXT NOT NULL);
      CREATE UNIQUE INDEX IF NOT EXISTS idx_provider_audit_request ON ai_provider_audit(project_id,request_id,action);
    ''')
    if 'pricing_status' not in {r['name'] for r in conn.execute('PRAGMA table_info(ai_provider_audit)')}:
        conn.execute("ALTER TABLE ai_provider_audit ADD COLUMN pricing_status TEXT NOT NULL DEFAULT 'unknown'")
    if 'claim_token' not in {r['name'] for r in conn.execute('PRAGMA table_info(ai_provider_tests)')}:
        conn.execute("ALTER TABLE ai_provider_tests ADD COLUMN claim_token TEXT NOT NULL DEFAULT ''")
    test_columns={r['name'] for r in conn.execute('PRAGMA table_info(ai_provider_tests)')}
    for name,declaration in [('owner_pid','INTEGER NOT NULL DEFAULT 0'),('owner_token',"TEXT NOT NULL DEFAULT ''")]:
        if name not in test_columns:conn.execute(f'ALTER TABLE ai_provider_tests ADD COLUMN {name} {declaration}')


def limits(raw,models):
    if not isinstance(raw,dict) or set(raw)-set(models):raise ProjectServiceError(422,'invalid_model_limits','容量配置只能包含已登记模型。')
    result={}
    for model,value in raw.items():
        if not isinstance(value,dict) or set(value)-{'input_tokens','output_tokens','source','vision','pricing'}:
            raise ProjectServiceError(422,'invalid_model_limits','请填写模型输入/输出上限和可核对的容量来源。')
        for field in ('input_tokens','output_tokens'):
            number=value.get(field)
            if not isinstance(number,int) or isinstance(number,bool) or not 1<=number<=10_000_000:
                raise ProjectServiceError(422,'invalid_model_limits','输入/输出上限须为正整数；未知时留空并暂停正式生成。')
        source=value.get('source')
        if not isinstance(source,str) or not source.strip() or len(source)>1000:
            raise ProjectServiceError(422,'capacity_source_required','请说明保守容量上限的来源；用户填写将标注为未实测。')
        if 'vision' in value:
            vision=value['vision']
            if not isinstance(vision,dict) or set(vision)!={'tokens_per_image','max_width','max_height','source'}:
                raise ProjectServiceError(422,'invalid_vision_limits','视觉容量须包含单图保守token上界、适用最大宽高及来源；未知可留空，正式视觉发送将暂停。')
            for field,maximum in [('tokens_per_image',10_000_000),('max_width',1600),('max_height',1600)]:
                number=vision.get(field)
                if not isinstance(number,int) or isinstance(number,bool) or not 1<=number<=maximum:
                    raise ProjectServiceError(422,'invalid_vision_limits','单图token上界须为正整数，适用宽高须在1～1600像素；请依据所选模型high detail文档填写。')
            if not isinstance(vision.get('source'),str) or not vision['source'].strip() or len(vision['source'])>1000:
                raise ProjectServiceError(422,'vision_capacity_source_required','请填写所选视觉模型high detail、适用尺寸的单图保守token上界来源。')
            value={**value,'vision':{**vision,'source':vision['source'].strip()}}
        if 'pricing' in value:
            pricing = value['pricing']
            if not isinstance(pricing,dict) or set(pricing) != {'input_per_million','output_per_million','currency','source'}:
                raise ProjectServiceError(422,'invalid_text_pricing','文本费率须包含每百万输入/输出token费用、币种和来源。')
            if any(isinstance(pricing[k],bool) or not isinstance(pricing[k],(int,float)) or not math.isfinite(pricing[k]) or pricing[k]<0 for k in ('input_per_million','output_per_million')) or pricing['currency'] not in {'USD','CNY','EUR'} or not isinstance(pricing['source'],str) or not pricing['source'].strip() or len(pricing['source'])>1000:
                raise ProjectServiceError(422,'invalid_text_pricing','文本费率须非负、有限且有可核对来源；币种须为USD、CNY或EUR。')
        result[model]={**value,'source':source.strip()}
    return result


def configuration(conn,profile_id,version=None):
    row=conn.execute('SELECT * FROM ai_provider_profiles WHERE id=?',(profile_id,)).fetchone()
    if not row:raise ProjectServiceError(404,'provider_profile_missing','提供方配置不存在。')
    version=version or row['current_version']
    if version==row['current_version']:
        config={'name':row['name'],'base_url':row['base_url'],'auth_mode':row['auth_mode'],'credential_ref':row['credential_ref'],
            'models':json.loads(row['models_json']),'capabilities':json.loads(row['capabilities_json']),
            'model_limits':json.loads(row['model_limits_json']),'status':row['status']}
    else:
        prior=conn.execute('SELECT config_json FROM ai_provider_profile_versions WHERE profile_id=? AND version=?',(profile_id,version)).fetchone()
        if not prior:raise ProjectServiceError(409,'provider_version_missing','指定配置版本不存在，请重新选择并预检。')
        config=json.loads(prior['config_json'])
        config.setdefault('status','draft');config.setdefault('model_limits',{});config.setdefault('credential_ref','')
    from paperpilot.services.provider_capabilities import profile as transport_profile
    import os
    config.setdefault('capabilities', {})
    for model in config['models']:
        transport = transport_profile(config['base_url'], model, os.environ)
        config['capabilities'][model] = {**config['capabilities'].get(model, {}),
            **{key: transport[key] for key in ('max_tools', 'supports_required', 'encoding', 'retry_policy')},
            'transport_profile': transport['id']}
    return {**config,'profile_id':profile_id,'config_revision':version,'global_status':row['status'],'revoked_before':row['revoked_before']}


def fingerprint(config):
    # Presentation and capacity annotations do not change the transport being tested.
    fields={k:config.get(k) for k in ('base_url','auth_mode','credential_ref','models','capabilities')}
    transport_keys={'image_generation','image_edit','mask','vision_analysis'}
    fields['capabilities']={model:{k:v for k,v in cap.items() if k in transport_keys and v} for model,cap in (config.get('capabilities') or {}).items() if any(v for k,v in cap.items() if k in transport_keys)}
    if config.get('auth_mode')=='environment':fields['environment_credential_identity']=hashlib.sha256(os.environ.get('PWA_AI_API_KEY','').encode()).hexdigest()
    return hashlib.sha256(compact(fields).encode()).hexdigest()


def tested(conn,config,model,mode):
    return bool(conn.execute("SELECT 1 FROM ai_provider_tests WHERE profile_id=? AND config_fingerprint=? AND model_id=? AND mode=? AND status='ready' AND created_at>? LIMIT 1",
        (config['profile_id'],fingerprint(config),model,mode,config.get('revoked_before',''))).fetchone())


def require_enabled(conn,profile_id,version,model,mode='chat',*,capacity=False):
    config=configuration(conn,profile_id,version)
    if config['global_status'] in {'disabled','revoked'} or config['status']!='enabled':
        raise ProjectServiceError(409,'provider_not_enabled','此配置版本未启用或已被停用/撤销；请测试并明确启用有效版本。')
    if model not in config['models']:raise ProjectServiceError(422,'model_not_registered','此版本没有登记所选模型，请选择已登记且通过相关生成测试的模型。')
    if not tested(conn,config,model,mode):raise ProjectServiceError(409,'generation_test_required','所选模型尚未通过此生成方式的真实测试；请在模型配置中明确测试，可能产生少量费用。')
    if capacity and model not in config['model_limits']:
        raise ProjectServiceError(409,'model_capacity_unknown','模型容量未知，未发送请求。请填写有来源的保守输入/输出上限，再重新预览。')
    return config


def enabled_for_any_mode(conn,config):
    return any(tested(conn,config,model,mode) for model in config['models'] for mode in MODES)


def admit_tokens(config,model,input_tokens,output_tokens):
    bounds=config.get('model_limits',{}).get(model)
    if not bounds:raise ProjectServiceError(409,'model_capacity_unknown','模型容量未知，请填写有来源的上限并重新预览。')
    margin=max(128,math.ceil(input_tokens*.05))
    if output_tokens>bounds['output_tokens'] or input_tokens+output_tokens+margin>bounds['input_tokens']:
        raise ProjectServiceError(422,'model_capacity_exceeded','完整请求、输出预留和安全余量超过模型上限；请分段、选择更大模型或明确处理资料，不会静默删减必需上下文。')
    return {'estimated_input_tokens':input_tokens,'reserved_output_tokens':output_tokens,'safety_margin_tokens':margin,'capacity_source':bounds['source'],'capacity_verification':'user_provided_unverified'}


def request_provider(conn,project_id,model=None):
    p=conn.execute('SELECT ai_profile_id,ai_config_revision,ai_model_id FROM projects WHERE id=?',(project_id,)).fetchone()
    if not p or not p['ai_profile_id']:
        raise ProjectServiceError(409,'provider_binding_required','论文尚未绑定已测试模型；请打开AI配置，登记并测试模型后明确绑定，再重新预览。')
    chosen=model or p['ai_model_id']
    return snapshot_for(conn,p['ai_profile_id'],p['ai_config_revision'],chosen)


def snapshot_for(conn,profile_id,version,model):
    config=require_enabled(conn,profile_id,version,model,capacity=True)
    return {k:config[k] for k in ('profile_id','config_revision','name','base_url','auth_mode','model_limits')}|{'model':model,'config_fingerprint':fingerprint(config),'mode':'chat'}


def verify_request(conn,snapshot):
    config=require_enabled(conn,snapshot['profile_id'],snapshot['config_revision'],snapshot['model'],snapshot.get('mode','chat'),capacity=True)
    if fingerprint(config)!=snapshot['config_fingerprint']:
        raise ProjectServiceError(409,'provider_snapshot_stale','请求的配置快照已失效，未发送；请重新预览。')
    return config


def preview_admission(conn,project_id,model,input_estimate,output_limit):
    try:
        snapshot=request_provider(conn,project_id,model)
        if snapshot['auth_mode']!='temporary':
            from paperpilot.services.provider_config import resolve_credentials
            resolve_credentials(conn,snapshot['profile_id'],snapshot['config_revision'])
        config=verify_request(conn,snapshot)
        capacity=admit_tokens(config,model,input_estimate+256,output_limit)
        return {'generation_allowed':True,'provider':snapshot,'capacity':capacity,'provider_error':'','provider_error_code':''}
    except ProjectServiceError as exc:
        return {'generation_allowed':False,'provider_error':exc.message,'provider_error_code':exc.code}


def set_default(conn,profile_id,config_revision,model_id,expected_version):
    if not conn.in_transaction:conn.execute('BEGIN IMMEDIATE')
    old=conn.execute("SELECT * FROM ai_provider_defaults WHERE id='default'").fetchone()
    if (old['version'] if old else 0)!=expected_version:raise ProjectServiceError(409,'default_version_conflict','默认配置已变化，请刷新后核对。')
    require_enabled(conn,profile_id,config_revision,model_id,capacity=True)
    conn.execute("INSERT INTO ai_provider_defaults VALUES('default',?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET profile_id=excluded.profile_id,config_revision=excluded.config_revision,model_id=excluded.model_id,version=excluded.version,updated_at=excluded.updated_at",
        (profile_id,config_revision,model_id,expected_version+1,stamp()))
    return {'version':expected_version+1,'profile_id':profile_id,'config_revision':config_revision,'model_id':model_id,'applies_to':'new_papers_only'}


def apply_default(conn,project_id):
    row=conn.execute("SELECT * FROM ai_provider_defaults WHERE id='default'").fetchone()
    if not row:return
    try:require_enabled(conn,row['profile_id'],row['config_revision'],row['model_id'],capacity=True)
    except ProjectServiceError:return  # Paper creation stays usable; configuration UI explains invalid defaults.
    conn.execute('UPDATE projects SET ai_profile_id=?,ai_config_revision=?,ai_model_id=? WHERE id=?',(row['profile_id'],row['config_revision'],row['model_id'],project_id))


def safe_usage(value):
    keys={'prompt_tokens','completion_tokens','total_tokens','input_tokens','output_tokens','cached_tokens','reasoning_tokens','estimated_input_tokens','estimated_output_tokens','external_calls','images_generated','images'}
    return {k:v for k,v in (value or {}).items() if k in keys and isinstance(v,(int,float)) and not isinstance(v,bool) and math.isfinite(v) and v>=0} if isinstance(value,dict) else {}


def begin_audit(conn,audit_id,project_id,request_id,snapshot,action,context_hash,scope):
    t=stamp()
    conn.execute('INSERT INTO ai_provider_audit(id,project_id,request_id,profile_id,config_revision,model_id,action,context_hash,scope_hash,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
        (audit_id,project_id,request_id,snapshot['profile_id'],snapshot['config_revision'],snapshot['model'],action,context_hash,hashlib.sha256(compact(scope).encode()).hexdigest(),'admitted',t,t))
    if snapshot.get('estimated_cost') is not None:conn.execute("UPDATE ai_provider_audit SET pricing_status='estimated' WHERE id=?",(audit_id,))


def finish_audit(conn,audit_id,status,error_code='',usage=None,elapsed_ms=0):
    conn.execute('UPDATE ai_provider_audit SET status=?,error_code=?,usage_json=?,elapsed_ms=?,updated_at=? WHERE id=?',
        (status,error_code or '',compact(safe_usage(usage)),max(0,int(elapsed_ms)),stamp(),audit_id))


def execute_call(conn,project_id,request_id,snapshot,action,prompt,content='',selection='',context='',*,max_tokens=4000,max_job_tokens=16000,temporary_key=None,call=None,scope=None,guard=None):
    """Shared admission for synchronous advisory consumers, never manuscript writes."""
    from paperpilot.services.context import redact_sensitive_text
    from paperpilot.services.ai_jobs import estimate_tokens
    from paperpilot.services.provider_config import resolve_credentials
    from paperpilot.services.credential_vault import scoped
    from paperpilot.adapters.openai_compat import call_chat_completion
    values=[redact_sensitive_text(v)[0] for v in (prompt,content,selection,context)]
    try:max_chars=max(1,int(os.environ.get('PWA_AI_MAX_INPUT_CHARS','400000')))
    except ValueError:max_chars=400000
    if sum(len(v) for v in values)>max_chars:raise ProjectServiceError(413,'input_too_large','完整请求超过输入字符上限，请缩小范围后重新预览；未发送。')
    estimate=estimate_tokens(''.join(values))+256
    config=verify_request(conn,snapshot)
    admit_tokens(config,snapshot['model'],estimate,max_tokens)
    if estimate+max_tokens>max_job_tokens:raise ProjectServiceError(422,'budget_exceeded','完整请求和输出预留超过任务硬上限，未发送；请缩小操作范围。')
    credential=resolve_credentials(conn,snapshot['profile_id'],snapshot['config_revision'],temporary_key=temporary_key)
    aid=uuid.uuid4().hex;started=time.perf_counter()
    with conn:
        if not conn.in_transaction:conn.execute('BEGIN IMMEDIATE')
        prior=conn.execute('SELECT status,scope_hash FROM ai_provider_audit WHERE project_id=? AND request_id=? AND action=?',(project_id,request_id,action)).fetchone()
        if prior:
            same=prior['scope_hash']==hashlib.sha256(compact(scope or {}).encode()).hexdigest()
            raise ProjectServiceError(409,'advisory_request_already_sent' if same else 'idempotency_key_reused','此请求已有发送/处理中记录，不会自动重复发送；请先核对结果和可能费用，变更配置或输入时创建新预览。')
        begin_audit(conn,aid,project_id,request_id,snapshot,action,hashlib.sha256(compact(values).encode()).hexdigest(),scope or {})
    try:
        verify_request(conn,snapshot)
        if guard and not guard():raise ProjectServiceError(409,'context_permission_stale','发送前正文、来源或授权已变化，请重新预览。')
        with scoped(credential):
            result=(call or call_chat_completion)(action,values[0],values[1],values[2],snapshot['model'],snapshot['base_url'],values[3],max_tokens=max_tokens)
        if not isinstance(result,dict):result={'ok':False,'error_code':'provider_protocol_error','error':'模型响应格式无效，请核对兼容协议。'}
        if credential.key:
            for field in ('content','error'):
                if isinstance(result.get(field),str):result[field]=result[field].replace(credential.key,'[REDACTED]')
        result['usage']=safe_usage(result.get('usage'))
        status='succeeded' if result.get('ok') else 'unknown' if result.get('error_code')=='provider_result_unknown' else 'failed'
        with conn:finish_audit(conn,aid,status,result.get('error_code',''),result.get('usage'),(time.perf_counter()-started)*1000)
        return result
    except Exception as exc:
        with conn:finish_audit(conn,aid,'failed',exc.code if isinstance(exc,ProjectServiceError) else 'provider_error',elapsed_ms=(time.perf_counter()-started)*1000)
        raise
    finally:temporary_key=None;credential=None


def public_test(row):
    value=dict(row)
    if value.get('status') in {'queued','running'}:
        from paperpilot.services.tex_runtime import owner_state
        value['recovery_state']=owner_state(value)
    value.pop('request_fingerprint',None);value.pop('config_fingerprint',None)
    value.pop('claim_token',None)
    value.pop('owner_pid',None);value.pop('owner_token',None)
    value['usage']=json.loads(value.pop('usage_json'))
    return value


def queue_test(conn,profile_id,payload):
    if not conn.in_transaction:conn.execute('BEGIN IMMEDIATE')
    config=configuration(conn,profile_id,payload['expected_version'])
    if config['config_revision']!=conn.execute('SELECT current_version FROM ai_provider_profiles WHERE id=?',(profile_id,)).fetchone()[0]:
        raise ProjectServiceError(409,'provider_version_conflict','配置版本已变化，请刷新并重新核对测试接收方。')
    mode=payload['mode'];model=payload['model_id']
    if mode not in MODES or model not in config['models']:raise ProjectServiceError(422,'invalid_test_target','请选择已登记模型及受支持的生成方式。')
    if mode in {'image_generation','image_edit','vision_analysis'} and not config['capabilities'].get(model,{}).get(mode):
        raise ProjectServiceError(422,'capability_not_registered','先在草稿登记此模型的相关图像用途并保存，再明确测试此方式。')
    if not payload.get('consent_to_send') or not payload.get('possible_cost_confirmed'):
        raise ProjectServiceError(422,'test_consent_required','生成测试会向所选服务发送固定无论文短输入，并可能收费；请明确授权本次测试。')
    if config['global_status'] in {'disabled','revoked'}:
        raise ProjectServiceError(409,'provider_unavailable','配置已停用/撤销；请先创建待测试草稿。')
    fp=hashlib.sha256(compact({'profile':profile_id,**payload}).encode()).hexdigest()
    old=conn.execute('SELECT * FROM ai_provider_tests WHERE request_id=?',(payload['request_id'],)).fetchone()
    if old:
        if old['request_fingerprint']!=fp:raise ProjectServiceError(409,'idempotency_key_reused','测试请求编号已有其他输入，请核对后使用新编号。')
        return public_test(old)|{'idempotent_replay':True}
    from paperpilot.services.tex_runtime import current_owner
    try:owner=current_owner()
    except OSError as exc:raise ProjectServiceError(503,'worker_identity_unavailable','无法确认测试进程身份，未发送测试；请检查系统权限后重试。') from exc
    tid=uuid.uuid4().hex;t=stamp()
    conn.execute('INSERT INTO ai_provider_tests(id,profile_id,config_revision,config_fingerprint,model_id,mode,status,request_id,request_fingerprint,created_at,updated_at,owner_pid,owner_token) VALUES(?,?,?,?,?,?,\'queued\',?,?,?,?,?,?)',
        (tid,profile_id,config['config_revision'],fingerprint(config),model,mode,payload['request_id'],fp,t,t,owner['owner_pid'],owner['owner_token']))
    return public_test(conn.execute('SELECT * FROM ai_provider_tests WHERE id=?',(tid,)).fetchone())


def execute_messages(conn, project_id, request_id, snapshot, action, messages, *, tools=None, tool_choice=None,
                     max_tokens=2000, scope=None, guard=None, temporary_key=None, call=None):
    """Same admission/audit path as execute_call, but for multi-turn messages + tools."""
    from paperpilot.services.context import redact_sensitive_text
    from paperpilot.services.ai_jobs import estimate_tokens
    from paperpilot.services.provider_config import resolve_credentials
    from paperpilot.services.credential_vault import scoped
    from paperpilot.adapters.openai_compat import call_chat_completion
    flattened = json.dumps(messages, ensure_ascii=False, default=str)
    redacted, _ = redact_sensitive_text(flattened)
    try: max_chars = max(1, int(os.environ.get('PWA_AI_MAX_INPUT_CHARS', '400000')))
    except ValueError: max_chars = 400000
    if len(redacted) > max_chars:
        raise ProjectServiceError(413, 'input_too_large', '完整请求超过输入字符上限，请缩小范围后重新预览；未发送。')
    estimate = estimate_tokens(redacted) + 256
    config = verify_request(conn, snapshot)
    admit_tokens(config, snapshot['model'], estimate, max_tokens)
    credential = resolve_credentials(conn, snapshot['profile_id'], snapshot['config_revision'], temporary_key=temporary_key)
    aid = uuid.uuid4().hex
    started = time.perf_counter()
    with conn:
        if not conn.in_transaction: conn.execute('BEGIN IMMEDIATE')
        prior = conn.execute('SELECT status,scope_hash FROM ai_provider_audit WHERE project_id=? AND request_id=? AND action=?',
                             (project_id, request_id, action)).fetchone()
        if prior:
            same = prior['scope_hash'] == hashlib.sha256(compact(scope or {}).encode()).hexdigest()
            raise ProjectServiceError(409, 'advisory_request_already_sent' if same else 'idempotency_key_reused',
                '此请求已有发送/处理中记录，不会自动重复发送；请先核对结果和可能费用。')
        begin_audit(conn, aid, project_id, request_id, snapshot, action,
                    hashlib.sha256(redacted.encode()).hexdigest(), scope or {})
    try:
        verify_request(conn, snapshot)
        if guard and not guard():
            raise ProjectServiceError(409, 'context_permission_stale', '发送前阶段内容或授权已变化，请重新预览。')
        with scoped(credential):
            result = (call or call_chat_completion)(
                action, '', '', '', snapshot['model'], snapshot['base_url'],
                messages=messages, tools=tools, tool_choice=tool_choice, max_tokens=max_tokens)
        if not isinstance(result, dict):
            result = {'ok': False, 'error_code': 'provider_protocol_error', 'error': '模型响应格式无效。'}
        if credential.key:
            for field in ('content', 'error'):
                if isinstance(result.get(field), str):
                    result[field] = result[field].replace(credential.key, '[REDACTED]')
        result['usage'] = safe_usage(result.get('usage'))
        status = 'succeeded' if result.get('ok') else 'unknown' if result.get('error_code') == 'provider_result_unknown' else 'failed'
        with conn:
            finish_audit(conn, aid, status, result.get('error_code', ''), result.get('usage'),
                         (time.perf_counter() - started) * 1000)
        return result
    except Exception as exc:
        with conn:
            finish_audit(conn, aid, 'failed',
                         exc.code if isinstance(exc, ProjectServiceError) else 'provider_error',
                         elapsed_ms=(time.perf_counter() - started) * 1000)
        raise
    finally:
        temporary_key = None
        credential = None


def perform_test(config,model,mode,runtime):
    """Use the actual adapters; only the fixed test input is ever sent."""
    from paperpilot.services.credential_vault import scoped
    with scoped(runtime):
        if mode in {'chat','stream'}:
            from paperpilot.adapters.openai_compat import call_chat_completion
            # DeepSeek v4 may spend the first tokens on reasoning before emitting OK.
            budget=256 if model.lower().startswith('deepseek-v4-') else 16
            budget=min(budget,config.get('model_limits',{}).get(model,{}).get('output_tokens',budget))
            result=call_chat_completion('write','Reply with the single word OK.','Protocol test. No manuscript data.','',model,config['base_url'],max_tokens=budget,stream=mode=='stream')
            if not result.get('ok'):raise ProjectServiceError(502,result.get('error_code','generation_test_failed'),result.get('error','生成测试失败。'))
            return result.get('usage',{})
        from io import BytesIO
        from PIL import Image
        buffer=BytesIO();Image.new('RGB',(64,64),'white').save(buffer,format='PNG');picture=buffer.getvalue()
        admission={'base_url':config['base_url'],'auth_mode':config['auth_mode'],'model':model}
        if mode=='vision_analysis':
            from paperpilot.adapters.visual_vision import analyze
            # This authorized, fixed 64px protocol fixture is not a manuscript
            # capacity verification. Never use its synthetic bounds for business.
            admission['model_limits']={model:{'input_tokens':100000,'output_tokens':1600,'source':'fixed protocol fixture only',
                'vision':{'tokens_per_image':10000,'max_width':64,'max_height':64,'source':'fixed protocol fixture only; not measured model capacity'}}}
            return analyze(admission,[picture],['readability'])['usage']
        from paperpilot.adapters.visual_image import generate
        return generate(admission,{'prompt':'A plain blue circle on a white background, protocol test.','size':'1024x1024'}, {'values':{}},reference=('test.png',picture,'image/png') if mode=='image_edit' else None)['usage']


def process_test(database,profile_id,test_id,temporary_key=None):
    conn=database();config=None;claim=uuid.uuid4().hex
    try:
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            row=conn.execute('SELECT * FROM ai_provider_tests WHERE id=? AND profile_id=?',(test_id,profile_id)).fetchone()
            if not row or row['status']!='queued':return
            config=configuration(conn,profile_id,row['config_revision'])
            if config['global_status'] in {'disabled','revoked'} or fingerprint(config)!=row['config_fingerprint']:
                conn.execute("UPDATE ai_provider_tests SET status='stale',error_code='configuration_stale',updated_at=? WHERE id=?",(stamp(),test_id));return
            conn.execute("UPDATE ai_provider_tests SET status='running',claim_token=?,updated_at=? WHERE id=?",(claim,stamp(),test_id))
        from paperpilot.services.provider_config import resolve_credentials
        runtime=resolve_credentials(conn,profile_id,row['config_revision'],temporary_key=temporary_key)
        usage=perform_test(config,row['model_id'],row['mode'],runtime)
        with conn:
            conn.execute('BEGIN IMMEDIATE')
            live=configuration(conn,profile_id,row['config_revision'])
            owner=conn.execute('SELECT status,claim_token FROM ai_provider_tests WHERE id=?',(test_id,)).fetchone()
            if not owner or owner['status']!='running' or owner['claim_token']!=claim:return
            valid=live['global_status'] not in {'disabled','revoked'} and live.get('revoked_before','')<row['created_at']
            status='ready' if valid else 'stale'
            conn.execute('UPDATE ai_provider_tests SET status=?,usage_json=?,updated_at=? WHERE id=?',(status,compact(usage),stamp(),test_id))
            if row['config_revision']==conn.execute('SELECT current_version FROM ai_provider_profiles WHERE id=?',(profile_id,)).fetchone()[0]:
                conn.execute('UPDATE ai_provider_profiles SET last_test_status=?,last_tested_at=? WHERE id=?',(status,stamp(),profile_id))
    except Exception as exc:
        conn.rollback()
        with conn:
            code=exc.code if isinstance(exc,ProjectServiceError) else 'generation_test_failed'
            conn.execute("UPDATE ai_provider_tests SET status='failed',error_code=?,updated_at=? WHERE id=? AND ((status='running' AND claim_token=?) OR (status='queued' AND claim_token=''))",(code,stamp(),test_id,claim))
    finally:
        temporary_key=None
        conn.close()
