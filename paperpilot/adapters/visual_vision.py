"""Explicitly authorized vision review. Findings remain advisory, never scientific proof."""
from __future__ import annotations

import base64
import json
import requests

from paperpilot.services.project_errors import ProjectServiceError
from paperpilot.services.visual_provider import credentials


def analyze(admission, images: list[bytes], checks: list[str], *, request_post=None):
    from paperpilot.services import vision_capacity
    capacity = vision_capacity.admit(admission, images, checks)
    key = credentials(admission["base_url"], admission["auth_mode"])
    post = request_post or requests.post
    content = [{"type": "text", "text": vision_capacity.instruction(checks)}] + [{"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(b).decode(), "detail": "high"}} for b in images]
    try:
        response = post(admission["base_url"].rstrip("/") + "/chat/completions", headers={"Authorization": "Bearer " + key} if key else {},
            json={"model": admission["model"], "messages": [{"role": "user", "content": content}], "max_tokens": vision_capacity.OUTPUT_TOKENS, "temperature": 0}, timeout=(10, 90), allow_redirects=False, stream=True)
        try:
            if response.status_code >= 500 or response.status_code in {408, 409, 429} or 300 <= response.status_code < 400:
                raise ProjectServiceError(503, "provider_unknown", "视觉核验结果不明，请先核对提供方任务与费用。")
            if response.status_code >= 400:
                raise ProjectServiceError(422, "provider_rejected", "视觉模型拒绝请求，请检查所登记的视觉能力。")
            payload = bytearray()
            for chunk in response.iter_content(65536):
                payload.extend(chunk)
                if len(payload) > 1_000_000:
                    raise ValueError("oversized response")
            raw = json.loads(payload)
        finally:
            response.close()
        text = raw["choices"][0]["message"]["content"].strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        data = json.loads(text)
        findings = data["findings"]
        if not isinstance(findings, list) or not findings or len(findings) > 30:
            raise ValueError("invalid findings")
        clean = []
        for item in findings:
            if not isinstance(item, dict) or item.get("check") not in checks or item.get("status") not in {"warning", "failed", "unknown", "needs_human_review"}:
                raise ValueError("invalid finding")
            clean.append({"check": item["check"], "status": item["status"], "message": str(item.get("message", ""))[:3000],
                "location": str(item.get("location", "unlocated"))[:500], "limitations": str(item.get("limitations", ""))[:2000] + "；云模型建议，尚需人工核验。", "method": "cloud_vision"})
        for missing in set(checks) - {f["check"] for f in clean}:
            clean.append({"check": missing, "status": "unknown", "message": "模型未返回此项检查。", "location": "unlocated", "limitations": "未执行完成，不能认定通过。", "method": "cloud_vision"})
        from paperpilot.services.provider_runtime import safe_usage
        if key:
            for item in clean:
                for field in ('message','location','limitations'):item[field]=item[field].replace(key,'[REDACTED]')
        return {"findings": clean, "usage": {**safe_usage(raw.get('usage')),'external_calls':1}, "capacity": capacity}
    except ProjectServiceError:
        raise
    except (requests.RequestException, ValueError, TypeError, KeyError, IndexError) as exc:
        raise ProjectServiceError(503, "provider_unknown", "视觉请求中断或响应不符合检查格式；请先对账，不会自动重发。") from exc
