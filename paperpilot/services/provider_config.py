from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
import ipaddress
import os
from urllib.parse import urlparse
from typing import Any

from paperpilot.services.project_errors import ProjectServiceError
from paperpilot.services.visual_provider import validate_capabilities
from paperpilot.services import credential_vault


AUTH_MODES = {"none", "api_key", "environment", "temporary"}
PROFILE_STATES = {"draft", "enabled", "disabled", "revoked"}


def _stamp() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _safe_url(value: str) -> str:
    value = str(value or "").strip().rstrip("/")
    try:
        parsed=urlparse(value)
        valid_port=parsed.port
    except ValueError:
        raise ProjectServiceError(422,'invalid_provider_url','AI地址或端口无效，请填写HTTPS或本机回环地址。')
    if not value or '\\' in value or not re.match(r"^https?://[^\s]+$", value) or not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment or valid_port is False or (parsed.scheme!='https' and not local_endpoint(value)):
        raise ProjectServiceError(422, "invalid_provider_url", "AI地址须为HTTPS或本机回环HTTP，不允许嵌入用户名、密码、查询参数或片段。")
    return value


def local_endpoint(value):
    host=urlparse(value).hostname
    if host=='localhost':return True
    try:return ipaddress.ip_address(host or '').is_loopback
    except ValueError:return False


def validate_auth(base_url,auth_mode):
    if auth_mode=='none' and not local_endpoint(base_url):
        raise ProjectServiceError(422,'local_auth_required','免密钥模式仅用于本机回环地址；远程服务须配置认证。')


def resolve_credentials(conn,profile_id,version=None,*,temporary_key=None):
    row=conn.execute('SELECT * FROM ai_provider_profiles WHERE id=?',(profile_id,)).fetchone()
    if not row or row['status'] in {'disabled','revoked'}:
        raise ProjectServiceError(409,'provider_unavailable','提供方已停用或撤销，请重新选择并测试配置。')
    if version is None or version==row['current_version']:
        config=dict(row)
    else:
        snapshot=conn.execute('SELECT config_json FROM ai_provider_profile_versions WHERE profile_id=? AND version=?',(profile_id,version)).fetchone()
        if not snapshot:raise ProjectServiceError(409,'provider_version_missing','请求绑定的配置版本不可用，请重新预检。')
        config=json.loads(snapshot['config_json'])
    base=_safe_url(config['base_url']);mode=config['auth_mode'];validate_auth(base,mode)
    if mode=='api_key':key=credential_vault.load(conn,profile_id,config.get('credential_ref',''))
    elif mode=='environment':
        if os.environ.get('PWA_AI_BASE_URL','').rstrip('/')!=base:
            raise ProjectServiceError(409,'credential_endpoint_mismatch','环境密钥的接收方与当前配置不一致，请在此配置输入独立密钥。')
        key=os.environ.get('PWA_AI_API_KEY','')
        if not key:raise ProjectServiceError(409,'credential_unavailable','环境凭据不可用，请配置后重新测试。')
    elif mode=='none':key=''
    elif mode=='temporary' and temporary_key:key=str(temporary_key)
    else:raise ProjectServiceError(409,'credential_unavailable','此认证模式需要本次操作明确提供临时密钥。')
    bounds=json.loads(config['model_limits_json']) if 'model_limits_json' in config else config.get('model_limits',{})
    return credential_vault.RuntimeCredential(base,mode,key,profile_id,version or row['current_version'],bounds)


def _profile(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "name": row["name"], "base_url": row["base_url"],
        "auth_mode": row["auth_mode"], "credential_state": row["credential_state"],
        "status": row["status"], "current_version": int(row["current_version"]),
        "models": json.loads(row["models_json"] or "[]"),
        "capabilities": json.loads(row["capabilities_json"] or "{}"),
        "model_limits": json.loads(row['model_limits_json'] or '{}'),
        "last_test_status": row["last_test_status"], "last_tested_at": row["last_tested_at"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def list_profiles(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [_profile(row) for row in conn.execute("SELECT * FROM ai_provider_profiles ORDER BY name,id")]


def create_profile(conn: sqlite3.Connection, payload: dict[str, Any], request_id: str) -> dict[str, Any]:
    if not conn.in_transaction:conn.execute('BEGIN IMMEDIATE')
    existing = conn.execute("SELECT * FROM ai_provider_profiles WHERE request_id=?", (request_id,)).fetchone()
    if existing:
        fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if existing["request_fingerprint"] != fingerprint:
            raise ProjectServiceError(409, "idempotency_key_reused", "request_id 已用于不同的提供方配置。")
        return _profile(existing) | {"idempotent_replay": True}
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ProjectServiceError(422, "provider_name_required", "提供方名称不能为空。")
    auth_mode = str(payload.get("auth_mode") or "environment")
    if auth_mode not in AUTH_MODES:
        raise ProjectServiceError(422, "invalid_auth_mode", "认证模式无效。")
    base_url = _safe_url(payload.get("base_url") or "http://127.0.0.1:8000/v1")
    validate_auth(base_url,auth_mode)
    models = sorted({str(x).strip() for x in (payload.get("models") or []) if str(x).strip()})
    if any(len(m)>200 or any(ord(c)<32 or ord(c)==127 for c in m) for m in models):raise ProjectServiceError(422,'invalid_model_id','模型编号应为不含控制字符的文本，最长200字符；请复制服务端的实际编号。')
    capabilities = validate_capabilities(payload.get("capabilities", {}))
    from paperpilot.services.provider_runtime import limits
    model_limits=limits(payload.get('model_limits',{}),models)
    if set(capabilities) - set(models):
        raise ProjectServiceError(422, "model_not_registered", "能力配置中的模型必须先加入模型列表。")
    secret = str(payload.get("api_key") or "")
    if secret and auth_mode!='api_key':raise ProjectServiceError(422,'unexpected_credential','密钥输入仅用于受保护持久化模式；临时密钥应在本次测试或生成时输入。')
    if auth_mode == "api_key" and not secret:
        raise ProjectServiceError(422, "api_key_required", "api_key 认证模式需要一次性输入密钥。")
    stamp = _stamp()
    profile_id = uuid.uuid4().hex
    fingerprint = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    credential_state = "configured" if secret or auth_mode in {"none", "environment"} else "temporary_required" if auth_mode=='temporary' else "missing"
    conn.execute(
        "INSERT INTO ai_provider_profiles(id,name,base_url,auth_mode,credential_state,credential_digest,status,current_version,models_json,capabilities_json,last_test_status,last_tested_at,request_id,request_fingerprint,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (profile_id, name, base_url, auth_mode, credential_state, hashlib.sha256(secret.encode()).hexdigest() if secret else "", "draft", 1, json.dumps(models, ensure_ascii=False), json.dumps(capabilities), "not_tested", None, request_id, fingerprint, stamp, stamp),
    )
    conn.execute('UPDATE ai_provider_profiles SET model_limits_json=? WHERE id=?',(json.dumps(model_limits,ensure_ascii=False),profile_id))
    conn.execute(
        "INSERT INTO ai_provider_profile_versions(profile_id,version,config_json,created_at) VALUES(?,?,?,?)",
        (profile_id, 1, json.dumps({"name": name, "base_url": base_url, "auth_mode": auth_mode, "models": models, "capabilities": capabilities, "credential_state": credential_state,'model_limits':model_limits,'status':'draft'}, ensure_ascii=False), stamp),
    )
    if auth_mode=='api_key':
        ref=credential_vault.save(conn,profile_id,secret,stamp)
        conn.execute('UPDATE ai_provider_profiles SET credential_ref=? WHERE id=?',(ref,profile_id))
        stored=json.loads(conn.execute('SELECT config_json FROM ai_provider_profile_versions WHERE profile_id=? AND version=1',(profile_id,)).fetchone()[0])
        stored['credential_ref']=ref
        conn.execute('UPDATE ai_provider_profile_versions SET config_json=? WHERE profile_id=? AND version=1',(json.dumps(stored,ensure_ascii=False),profile_id))
    return _profile(conn.execute("SELECT * FROM ai_provider_profiles WHERE id=?", (profile_id,)).fetchone())


def update_profile(conn: sqlite3.Connection, profile_id: str, base_version: int, changes: dict[str, Any]) -> dict[str, Any]:
    if not conn.in_transaction:conn.execute('BEGIN IMMEDIATE')
    row = conn.execute("SELECT * FROM ai_provider_profiles WHERE id=?", (profile_id,)).fetchone()
    if row is None:
        raise ProjectServiceError(404, "provider_profile_missing", "AI 提供方配置不存在。")
    if int(row["current_version"]) != base_version:
        raise ProjectServiceError(409, "provider_version_conflict", "配置已变化，请刷新后重试。")
    allowed = {"name", "base_url", "auth_mode", "models", "status", "capabilities", "api_key", "model_limits"}
    if set(changes) - allowed:
        raise ProjectServiceError(422, "invalid_provider_field", "配置包含不支持的字段。")
    name = str(changes.get("name", row["name"])).strip()
    if not name:raise ProjectServiceError(422,'provider_name_required','配置名称不能为空。')
    base_url = _safe_url(changes.get("base_url", row["base_url"]))
    auth_mode = str(changes.get("auth_mode", row["auth_mode"]))
    status = str(changes.get("status", row["status"]))
    if auth_mode not in AUTH_MODES or status not in PROFILE_STATES:
        raise ProjectServiceError(422, "invalid_provider_state", "配置状态或认证模式无效。")
    validate_auth(base_url,auth_mode)
    if auth_mode=='api_key' and status not in {'disabled','revoked'} and not changes.get('api_key') and (row['auth_mode']!='api_key' or not row['credential_ref'] or base_url!=row['base_url']):
        raise ProjectServiceError(422,'api_key_required','更改接收方或认证方式须重新输入此提供方密钥并测试。')
    models = sorted({str(x).strip() for x in changes.get("models", json.loads(row["models_json"] or "[]")) if str(x).strip()})
    if any(len(m)>200 or any(ord(c)<32 or ord(c)==127 for c in m) for m in models):raise ProjectServiceError(422,'invalid_model_id','模型编号应为不含控制字符的文本，最长200字符。')
    capabilities = validate_capabilities(changes.get("capabilities", json.loads(row["capabilities_json"] or "{}")))
    from paperpilot.services import provider_runtime
    model_limits=provider_runtime.limits(changes.get('model_limits',json.loads(row['model_limits_json'] or '{}')),models)
    if set(capabilities) - set(models):
        raise ProjectServiceError(422, "model_not_registered", "能力配置中的模型必须先加入模型列表。")
    version = base_version + 1
    stamp = _stamp()
    if status=='revoked' and changes.get('api_key'):raise ProjectServiceError(422,'unexpected_credential','撤销不需要新密钥；若要重新配置，请明确保存为新草稿并重新测试。')
    ref=credential_vault.save(conn,profile_id,str(changes['api_key']),stamp) if changes.get('api_key') and auth_mode=='api_key' else row['credential_ref'] if auth_mode=='api_key' and status!='revoked' else ''
    if changes.get('api_key') and auth_mode!='api_key':raise ProjectServiceError(422,'unexpected_credential','请在受保护持久化模式输入新密钥，或在本次操作中明确使用临时密钥。')
    candidate={'profile_id':profile_id,'base_url':base_url,'auth_mode':auth_mode,'credential_ref':ref,'models':models,'capabilities':capabilities,'model_limits':model_limits,'revoked_before':row['revoked_before']}
    previous=provider_runtime.configuration(conn,profile_id,base_version)
    transport_changed=provider_runtime.fingerprint(candidate)!=provider_runtime.fingerprint(previous)
    if transport_changed and status not in {'disabled','revoked'}:status='draft'
    if status=='enabled' and not provider_runtime.enabled_for_any_mode(conn,candidate):
        raise ProjectServiceError(409,'generation_test_required','配置尚未通过明确授权的真实生成测试，不能启用；模型列表或本地字段检查不能替代生成测试。')
    conn.execute("UPDATE ai_provider_profiles SET name=?,base_url=?,auth_mode=?,status=?,current_version=?,models_json=?,capabilities_json=?,updated_at=? WHERE id=?", (name, base_url, auth_mode, status, version, json.dumps(models, ensure_ascii=False), json.dumps(capabilities), stamp, profile_id))
    conn.execute('UPDATE ai_provider_profiles SET credential_ref=?,credential_state=?,model_limits_json=?,last_test_status=? WHERE id=?',(ref,'configured' if ref or auth_mode in {'none','environment'} else 'temporary_required' if auth_mode=='temporary' else 'missing',json.dumps(model_limits,ensure_ascii=False),'not_tested' if transport_changed else row['last_test_status'],profile_id))
    if status in {'disabled','revoked'}:conn.execute('UPDATE ai_provider_profiles SET revoked_before=? WHERE id=?',(stamp,profile_id))
    if status=='revoked':
        conn.execute('DELETE FROM ai_provider_credentials WHERE profile_id=?',(profile_id,))
        conn.execute("UPDATE ai_provider_profiles SET credential_state='revoked',credential_digest='' WHERE id=?",(profile_id,))
    conn.execute("INSERT INTO ai_provider_profile_versions(profile_id,version,config_json,created_at) VALUES(?,?,?,?)", (profile_id, version, json.dumps({"name": name, "base_url": base_url, "auth_mode": auth_mode, "models": models, "capabilities": capabilities, "status": status,'credential_ref':ref,'model_limits':model_limits}, ensure_ascii=False), stamp))
    return _profile(conn.execute("SELECT * FROM ai_provider_profiles WHERE id=?", (profile_id,)).fetchone())


def bind_project(conn: sqlite3.Connection, project_id: str, profile_id: str, model_id: str, base_project_revision: int, config_revision: int | None = None) -> dict[str, Any]:
    if not conn.in_transaction:conn.execute('BEGIN IMMEDIATE')
    profile = conn.execute("SELECT * FROM ai_provider_profiles WHERE id=?", (profile_id,)).fetchone()
    project = conn.execute("SELECT project_revision FROM projects WHERE id=?", (project_id,)).fetchone()
    if profile is None or project is None:
        raise ProjectServiceError(404, "provider_binding_missing", "论文或提供方配置不存在。")
    if int(project["project_revision"]) != base_project_revision:
        raise ProjectServiceError(409, "project_version_conflict", "论文配置已变化，请刷新后重试。")
    from paperpilot.services.provider_runtime import require_enabled
    version=config_revision or profile['current_version']
    require_enabled(conn,profile_id,version,model_id)
    stamp = _stamp()
    conn.execute("UPDATE projects SET ai_profile_id=?,ai_model_id=?,ai_config_revision=?,project_revision=project_revision+1,updated_at=? WHERE id=?", (profile_id, model_id, version, stamp, project_id))
    return {"project_id": project_id, "profile_id": profile_id, "model_id": model_id, "config_revision": version, "project_revision": int(project["project_revision"]) + 1}


def project_binding(conn: sqlite3.Connection, project_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT p.ai_profile_id,p.ai_model_id,p.ai_config_revision,pr.name,pr.base_url,pr.status,pr.current_version FROM projects p LEFT JOIN ai_provider_profiles pr ON pr.id=p.ai_profile_id WHERE p.id=?", (project_id,)).fetchone()
    if not row or not row["ai_profile_id"]:
        return None
    result = dict(row)
    result["profile_id"] = result["ai_profile_id"]
    result["model_id"] = result["ai_model_id"]
    from paperpilot.services import provider_runtime
    config=provider_runtime.configuration(conn,result['profile_id'],result['ai_config_revision'])
    result.update(name=config['name'],base_url=config['base_url'],auth_mode=config['auth_mode'],status=config['status'] if config['global_status'] not in {'disabled','revoked'} else config['global_status'],current_version=config['config_revision'],latest_version=row['current_version'],model_limits=config['model_limits'],generation_test_passed=provider_runtime.tested(conn,config,result['model_id'],'chat'))
    return result


def test_profile(conn: sqlite3.Connection, profile_id: str) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM ai_provider_profiles WHERE id=?", (profile_id,)).fetchone()
    if row is None:
        raise ProjectServiceError(404, "provider_profile_missing", "AI 提供方配置不存在。")
    status = "configuration_valid_only" if row["credential_state"] in {"configured","temporary_required"} and json.loads(row["models_json"] or "[]") else "blocked"
    stamp = _stamp()
    return {"profile_id": profile_id, "status": status, "tested_at": stamp, 'external_calls':0,"message": "本地字段检查通过，尚未测试模型生成，不能据此启用。请选择模型和方式，核对可能费用后授权生成测试。" if status == "configuration_valid_only" else "配置缺少模型或可用凭据，未发送外部请求。"}
