"""Image capability and per-attempt consent/cost admission; no credentials in snapshots."""
from __future__ import annotations

import json
import math
import os
from urllib.parse import urlparse

from paperpilot.services.project_errors import ProjectServiceError


def validate_capabilities(raw):
    if not isinstance(raw, dict) or len(raw) > 100:
        raise ProjectServiceError(422, "invalid_capabilities", "能力配置应按模型编号填写。")
    allowed = {"image_generation", "image_edit", "mask", "vision_analysis", "price_per_image", "price_per_check", "currency"}
    for model, cap in raw.items():
        if not isinstance(model, str) or not isinstance(cap, dict) or set(cap) - allowed:
            raise ProjectServiceError(422, "invalid_capabilities", "图像能力配置包含不支持的字段。")
        for key in {"image_generation", "image_edit", "mask", "vision_analysis"} & set(cap):
            if not isinstance(cap[key], bool):
                raise ProjectServiceError(422, "invalid_capabilities", "模型能力请使用 true 或 false。")
        for key in ("price_per_image", "price_per_check"):
            price = cap.get(key)
            if price is not None and (isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price < 0):
                raise ProjectServiceError(422, "invalid_capabilities", "单张/次费用应为非负有限数值，未知费用请留空。")
        if cap.get("currency", "USD") != "USD":
            raise ProjectServiceError(422, "invalid_capabilities", "当前预算以 USD 计；其他币种请先换算并核实。")
    return raw


def credentials(base_url, auth_mode):
    from paperpilot.services.credential_vault import current
    runtime=current(base_url)
    if runtime:
        if runtime.auth_mode!=auth_mode:
            raise ProjectServiceError(409,'credential_mode_mismatch','请求认证方式已变化，请重新预检。')
        return runtime.key
    local = urlparse(base_url).hostname in {"localhost", "127.0.0.1", "::1"}
    if auth_mode == "none" and local:
        return ""
    if auth_mode != "environment":
        raise ProjectServiceError(422, "credential_unavailable", "图像调用需要环境凭据，或本机免认证服务。请在模型配置中选择环境认证。")
    configured = os.environ.get("PWA_AI_BASE_URL", "").rstrip("/")
    if configured != base_url.rstrip("/"):
        raise ProjectServiceError(422, "credential_endpoint_mismatch", "环境凭据的服务地址与当前提供方不一致；请先核对环境配置。")
    key = os.environ.get("PWA_AI_API_KEY", "")
    if not key and not local:
        raise ProjectServiceError(422, "credential_unavailable", "图像服务缺少环境凭据，请先配置后重试。")
    return key


def gate(conn, run, payload, temporary_key=None):
    vision = run["selected_type"] == "vision_analysis"
    purpose = "vision" if vision else "drawing"
    p = binding(conn, run["project_id"], purpose)
    from paperpilot.services import provider_runtime
    if not p or not p['ai_profile_id']:raise ProjectServiceError(422,'provider_unavailable','请在AI模型配置中登记、测试并明确绑定此用途的模型。')
    profile=provider_runtime.configuration(conn,p['ai_profile_id'],p['ai_config_revision'])
    cap = profile['capabilities'].get(p["ai_model_id"], {})
    spec = json.loads(run["spec_json"])
    needed = "vision_analysis" if vision else "image_edit" if spec.get("input_asset_version") else "image_generation"
    if not cap.get(needed) or (spec.get("mask_version") and not cap.get("mask")):
        raise ProjectServiceError(422, "capability_missing", "所选模型未登记所需的图片生成、参考图或遮罩能力；请选择其他模型，不会自动改为整图生成。")
    provider_runtime.require_enabled(conn,p['ai_profile_id'],p['ai_config_revision'],p['ai_model_id'],needed,capacity=vision)
    price = cap.get("price_per_check" if vision else "price_per_image")
    if price is None and payload.get("hard_limit") is not None:
        raise ProjectServiceError(422, "budget_unverifiable", "模型费率未知，无法执行金额硬上限；没有发送外部请求。请核实费率或另选模型。")
    if price is not None and payload.get("hard_limit") is not None and price > payload["hard_limit"]:
        raise ProjectServiceError(422, "budget_exceeded", "预计单张费用超过本次金额上限，没有发送外部请求。")
    if price is None and not payload.get("unknown_price_confirmed"):
        raise ProjectServiceError(422, "unknown_price_confirmation_required", "此模型费率未知，请逐次确认费用不确定性后再执行。")
    if not payload.get("external_consent"):
        raise ProjectServiceError(422, "external_consent_required", "图片描述、参考图与遮罩将发送到所选提供方；请明确授权本次发送。")
    from paperpilot.services.provider_config import resolve_credentials
    resolve_credentials(conn,p['ai_profile_id'],p['ai_config_revision'],temporary_key=temporary_key)
    return {"adapter": "openai_vision_v1" if vision else "openai_images_v1", "profile_id": p['ai_profile_id'], "profile_version": p['ai_config_revision'], "model": p["ai_model_id"],
            "base_url": profile["base_url"], "auth_mode": profile["auth_mode"], "capabilities": cap, "model_limits": profile['model_limits'],
            "estimated_cost": price, "currency": "USD", "hard_limit": payload.get("hard_limit"),
            "external_consent": True, "unknown_price_confirmed": payload.get("unknown_price_confirmed", False), "purpose": purpose,'mode':needed,'config_revision':p['ai_config_revision'],'config_fingerprint':provider_runtime.fingerprint(profile)}


def verify_gate(conn, project_id, admission):
    from paperpilot.services import provider_runtime
    try:
        mode=admission.get('mode','vision_analysis' if admission.get('purpose')=='vision' else 'image_generation')
        profile=provider_runtime.require_enabled(conn,admission['profile_id'],admission['profile_version'],admission['model'],mode,capacity=mode=='vision_analysis')
        if provider_runtime.fingerprint(profile)!=admission.get('config_fingerprint'):raise ValueError('stale')
    except (ProjectServiceError,ValueError) as exc:raise ProjectServiceError(409,'consent_stale','固定提供方版本、生成测试或授权已失效；未改用其他配置，请重新核对并创建新任务。') from exc


def available(conn, project_id, purpose="drawing"):
    p = binding(conn, project_id, purpose)
    profile = conn.execute("SELECT * FROM ai_provider_profiles WHERE id=?", (p["ai_profile_id"],)).fetchone()
    from paperpilot.services import provider_runtime
    config=provider_runtime.configuration(conn,p['ai_profile_id'],p['ai_config_revision']) if profile else None
    return {"profile_id": p["ai_profile_id"], "model": p["ai_model_id"], "provider_name": config['name'] if config else "未绑定",
            "capabilities": config['capabilities'].get(p["ai_model_id"], {}) if config else {},
            "status": config['global_status'] if config and config['global_status'] in {'disabled','revoked'} else config['status'] if config else "unavailable", "currency": "USD",'profile_version':p['ai_config_revision']}


def binding(conn, project_id, purpose):
    if purpose not in {"drawing", "vision"}:
        raise ProjectServiceError(422, "invalid_purpose", "请选择绘图或视觉核验用途。")
    row = conn.execute("SELECT profile_id AS ai_profile_id,model_id AS ai_model_id,profile_version AS ai_config_revision FROM project_visual_provider_bindings WHERE project_id=? AND purpose=?", (project_id, purpose)).fetchone()
    if row:
        return row
    return conn.execute("SELECT ai_profile_id,ai_model_id,ai_config_revision FROM projects WHERE id=?", (project_id,)).fetchone()


def bind(conn, project_id, payload):
    from paperpilot.services.visual_jobs import begin
    begin(conn)
    project = conn.execute("SELECT project_revision FROM projects WHERE id=?", (project_id,)).fetchone()
    if project["project_revision"] != payload["expected_project_revision"]:
        raise ProjectServiceError(409, "project_version_conflict", "论文配置已变化，请刷新后重新绑定。")
    from paperpilot.services import provider_runtime
    profile=provider_runtime.configuration(conn,payload['profile_id'],payload.get('config_revision'))
    cap = profile['capabilities'].get(payload["model_id"], {})
    needed = ["vision_analysis"] if payload["purpose"] == "vision" else ["image_generation", "image_edit"]
    if not any(cap.get(key) for key in needed):
        raise ProjectServiceError(422, "capability_missing", "所选模型没有登记此用途的能力。")
    mode=next((mode for mode in needed if cap.get(mode) and provider_runtime.tested(conn,profile,payload['model_id'],mode)),needed[0])
    provider_runtime.require_enabled(conn,payload['profile_id'],profile['config_revision'],payload['model_id'],mode)
    conn.execute("INSERT INTO project_visual_provider_bindings VALUES(?,?,?,?,?,1) ON CONFLICT(project_id,purpose) DO UPDATE SET profile_id=excluded.profile_id,model_id=excluded.model_id,profile_version=excluded.profile_version,revision=project_visual_provider_bindings.revision+1", (project_id, payload["purpose"], payload['profile_id'], payload["model_id"], profile['config_revision']))
    conn.execute("UPDATE projects SET project_revision=project_revision+1 WHERE id=?", (project_id,))
    return {"project_id": project_id, "purpose": payload["purpose"], "profile_id": payload['profile_id'], "model_id": payload["model_id"], "profile_version": profile['config_revision'], "project_revision": project["project_revision"]+1, "writing_binding_changed": False}
