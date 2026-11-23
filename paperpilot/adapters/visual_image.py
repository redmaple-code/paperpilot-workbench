"""OpenAI-compatible image endpoints. No text endpoint fallback or automatic retry."""
from __future__ import annotations

import base64
import io
import requests
from PIL import Image

from paperpilot.services.project_errors import ProjectServiceError
from paperpilot.services.visual_assets import MAX_BYTES, compact
from paperpilot.services.visual_provider import credentials


def generate(admission, spec, plan, *, reference=None, mask=None, request_post=None):
    post = request_post or requests.post
    key = credentials(admission["base_url"], admission["auth_mode"])
    headers = {"Authorization": "Bearer " + key} if key else {}
    prompt = spec["prompt"] + "\nVisual style: " + compact(plan["values"])
    endpoint = admission["base_url"].rstrip("/") + ("/images/edits" if reference else "/images/generations")
    body = {"model": admission["model"], "prompt": prompt, "size": spec["size"], "n": 1, "response_format": "b64_json"}
    kwargs = {"headers": headers, "timeout": (10, 90), "allow_redirects": False, "stream": True}
    if reference:
        files = {"image": reference}
        if mask:
            files["mask"] = mask
        kwargs.update(data=body, files=files)
    else:
        kwargs["json"] = body
    try:
        response = post(endpoint, **kwargs)
        try:
            if response.status_code >= 500 or response.status_code in {408, 409, 429} or 300 <= response.status_code < 400:
                raise ProjectServiceError(503, "provider_unknown", "图像服务结果不明，请先对账，不会自动重发。")
            if response.status_code >= 400:
                raise ProjectServiceError(422, "provider_rejected", f"图像服务拒绝请求（HTTP {response.status_code}），请检查模型和输入。")
            payload = bytearray()
            for chunk in response.iter_content(65536):
                payload.extend(chunk)
                if len(payload) > MAX_BYTES * 4 // 3 + 65536:
                    raise ProjectServiceError(503, "provider_unknown", "提供方响应超过安全大小；请对账并改用较小图像。")
            import json
            data = json.loads(payload)
        finally:
            response.close()
        encoded = (data.get("data") or [{}])[0].get("b64_json")
        if not encoded:
            raise ProjectServiceError(503, "provider_unknown", "提供方没有返回内嵌图片；请检查兼容能力并对账，不会自动访问外部图片链接。")
        content = base64.b64decode(encoded, validate=True)
        if len(content) > MAX_BYTES:
            raise ValueError("image too large")
        with Image.open(io.BytesIO(content)) as im:
            suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp"}.get(im.format)
        if not suffix:
            raise ValueError("unsupported image")
        from paperpilot.services.provider_runtime import safe_usage
        return {"content": content, "filename": "generated" + suffix, "usage": {**safe_usage(data.get('usage')),'external_calls':1} if isinstance(data.get('usage'),dict) else {"images": 1, "external_calls":1,"cost_status": "provider_usage_unavailable"}}
    except ProjectServiceError:
        raise
    except (requests.RequestException, ValueError, TypeError, IndexError, KeyError, OSError) as exc:
        raise ProjectServiceError(503, "provider_unknown", "图像请求已中断或结果无法验证；请先对账，不会自动再次调用。") from exc
