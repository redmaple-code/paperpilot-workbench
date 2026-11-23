from __future__ import annotations

import json
import copy
import os
from typing import Any, Callable
from urllib.parse import urlparse

from requests import RequestException, Timeout, ConnectTimeout

from paperpilot.services.conversion import ai_url
from paperpilot.services.credential_vault import current as runtime_credential
from paperpilot.services.project_errors import ProjectServiceError
from paperpilot.services import agent_transport


def call_chat_completion(
    action: str,
    prompt: str,
    content: str,
    selection: str,
    model: str | None,
    base_url: str | None = None,
    context: str = "",
    request_post: Callable[..., Any] | None = None,
    environ: dict[str, str] | None = None,
    max_tokens: int | None = None,
    stream: bool = False,
    messages: list[dict[str, Any]] | None = None,
    tools: list[dict[str, Any]] | None = None,
    tool_choice: str | None = None,
) -> dict[str, Any]:
    env = environ if environ is not None else os.environ
    post = request_post
    if post is None:
        import requests

        post = requests.post
    base = (base_url or env.get("PWA_AI_BASE_URL", "")).strip()
    chosen = model or env.get("PWA_AI_MODEL", "deepseek-chat")
    host = urlparse(base).hostname if base else None
    local_endpoint = host in {"localhost", "127.0.0.1", "::1"}
    try:
        from paperpilot.services.provider_config import _safe_url,local_endpoint as is_loopback
        base=_safe_url(base)
        local_endpoint=is_loopback(base)
        credential=runtime_credential(base)
        if credential:
            key=credential.key
        elif local_endpoint:
            key=''
        elif base.rstrip('/')==env.get('PWA_AI_BASE_URL','').rstrip('/'):
            key=env.get('PWA_AI_API_KEY','')
        else:
            return {'ok':False,'error_code':'credential_endpoint_mismatch','error':'目标地址没有所属凭据；请绑定并测试该提供方配置。'}
    except ProjectServiceError as exc:
        return {'ok':False,'error_code':exc.code,'error':exc.message}
    if not base or (not key and not local_endpoint):
        return {"ok": False, "error": "AI 尚未配置。请在 .env 设置 PWA_AI_BASE_URL、PWA_AI_API_KEY 和 PWA_AI_MODEL。"}
    instruction = {
        "write": "根据论文上下文生成一段可编辑的学术写作草稿。",
        "companion": "作为自动伴写助手，结合当前段落和论文上下文，提出下一句或下一小段的候选文本，并保持术语、语气和论证方向一致。",
        "follow": "跟随用户当前句子继续写作，尽量保持用户的句式、术语和节奏，不改变原有事实。",
        "follow_write": "在用户选定的局部范围内跟随原文继续写作，使用论文整体结构和逻辑，输出可审阅的候选文本。",
        "continue": "沿着当前内容自然续写下一段。",
        "edit": "只修改选中的内容，保持事实和引用不变，并说明修改重点。",
        "rewrite": "只重写用户选定的范围，保持事实和引用不变，输出可审阅的候选文本。",
        "expand_or_condense": "按用户要求扩写或压缩选定范围，保持事实和引用不变。",
        "style_or_format": "调整选定范围的语气或格式，不改变事实、术语含义或引用。",
        "term_logic_check": "检查选定内容中的术语和逻辑一致性，返回建议供用户审阅，不直接改写其他内容。",
        "citation_compare": "将选定内容与提供的参考文献上下文进行对照，标出可核对的问题，不伪造文献。",
        "explain_or_suggest": "解释选定内容并提出可审阅的修改建议，不直接替换正文。",
        "outline": "生成清晰的论文写作大纲。",
        "compare": "分析写作内容与参考文献的一致性风险。",
    }.get(action, "协助完成论文写作任务。")
    user = f"任务：{instruction}\n用户补充：{prompt}\n论文上下文：\n{context}\n当前内容：\n{content}\n选中内容：\n{selection}"
    body = {
        "model": chosen,
        "messages": copy.deepcopy(messages) if messages is not None else [
            {"role": "system", "content": "你是严谨的学术写作助手。不要伪造参考文献；不确定时明确说明。输出可编辑结果。"},
            {"role": "user", "content": user},
        ],
        "temperature": 0.3,
    }
    if tools:
        from paperpilot.services.provider_capabilities import profile
        capabilities = profile(base, chosen, env)
        if len(tools) > capabilities["max_tools"]:
            return {"ok": False, "error_code": "provider_tool_limit", "error": "当前提供方工具数量超限，请缩小任务工具集合。"}
        # The gateway rejects short tool-bearing requests ("unmarshal ... EOF")
        # even though the same payload passes once it is longer. A padding line
        # keeps the request above that floor without altering its meaning.
        # Always pad: the gateway rejects short tool-bearing requests
        # regardless of how small the measured body is.
        if capabilities["pad_tool_context"]:
            filler = ("\n\n工具输出只作为待审阅资料，保持来源和版本边界；"
                      "任何结构与资源修改需由作者预览并明确应用。") * 12
            # Extend the last user turn instead of appending another: two
            # consecutive user messages also trip the gateway.
            for message in reversed(body["messages"]):
                if message.get("role") == "user":
                    message["content"] = str(message.get("content") or "") + filler
                    break
            else:
                body["messages"].append({"role": "user", "content": filler})
        body["tools"] = tools
        # This gateway does not accept tool_choice="required"; "auto" plus a
        # single offered tool already forces the call in practice.
        body["tool_choice"] = "auto" if tool_choice is None or (tool_choice == "required" and not capabilities["supports_required"]) else tool_choice
    if max_tokens is not None:
        body["max_tokens"] = max(1, int(max_tokens))
    if stream:
        if tools:
            return {'ok': False, 'error_code': 'provider_tools_stream_unsupported',
                    'error': '工具调用不支持流式；请关闭流式后重试。'}
        body['stream']=True
        body['stream_options']={'include_usage':True}
    if credential and chosen in credential.model_limits:
        from paperpilot.services.provider_runtime import admit_tokens
        from paperpilot.services.ai_jobs import estimate_tokens
        try:admit_tokens({'model_limits':credential.model_limits},chosen,estimate_tokens(json.dumps(body,ensure_ascii=False)),int(body.get('max_tokens',4000)))
        except ProjectServiceError as exc:return {'ok':False,'error_code':exc.code,'error':exc.message}
    provider_usage: dict[str, Any] = {}
    tool_calls = []
    response=None
    try:
        headers = {"Content-Type": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        # Send UTF-8 bytes, not json=: requests escapes non-ASCII to \uXXXX
        # and this gateway then fails to unmarshal the body ("unexpected EOF").
        payload_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
        response = post(ai_url(base), data=payload_bytes, headers=headers,
                        timeout=agent_transport.timeout(float(env.get("PWA_AI_TIMEOUT", "90"))), allow_redirects=False, stream=True)
        if 300 <= response.status_code < 400:
            return {'ok':False,'error_code':'provider_redirect_rejected','error':'服务返回重定向，未向其他地址发送密钥或论文。请核对准确地址后重新测试。'}
        if response.status_code >= 400:
            detail = ""
            try:
                raw = response.text if not hasattr(response, "iter_content") else bytes(response.iter_content(4096)).decode("utf-8", "replace")
                parsed = json.loads(raw)
                detail = str((parsed.get("error") or {}).get("message") or parsed.get("message") or "")[:200]
            except Exception:
                detail = ""
            if response.status_code == 400:
                # Surface the offending message shape inline: 400s here are almost
                # always a malformed tool exchange, which is invisible from the code.
                try:
                    roles = [str(m.get("role")) + ("+tc" if m.get("tool_calls") else "") for m in (body.get("messages") or [])]
                    detail = (detail + " | messages=" + ",".join(roles)
                              + " | tool_choice=" + str(body.get("tool_choice"))
                              + " | tools=" + str(len(body.get("tools") or [])))[:400]
                except Exception:
                    pass
            return {"ok": False, 'error_code':'provider_auth_failed' if response.status_code in {401,403} else 'provider_rate_limited' if response.status_code==429 else 'provider_unavailable_transient' if response.status_code in {502,503,504} else 'provider_model_missing' if response.status_code==404 else 'provider_request_invalid' if response.status_code==400 else 'provider_rejected', "error": f"AI 服务返回 HTTP {response.status_code}" + (f"：{detail}" if detail else "") + "。不会切换提供方。"}
        if hasattr(response,'iter_content'):
            maximum=min(16*1024*1024,max(1024*1024,int(body.get('max_tokens',4000))*32+65536));payload=bytearray()
            for chunk in response.iter_content(65536):
                if agent_transport.expired():
                    raise Timeout('Agent overall request deadline exceeded')
                payload.extend(chunk)
                if len(payload)>maximum:return {'ok':False,'error_code':'provider_result_unknown','error':'响应超过可安全处理的大小，结果和费用可能不明；未自动重发，请先核对服务记录。'}
            response_text=bytes(payload).decode('utf-8')
        else:response_text=response.text
        is_stream=response.headers.get("content-type", "").startswith("text/event-stream") or response_text.lstrip().startswith("data:")
        if stream and not is_stream:
            return {'ok':False,'error_code':'provider_stream_unsupported','error':'所选模型没有返回兼容的流式响应，此生成方式测试未通过。'}
        tool_calls = []
        if is_stream:
            chunks = []
            for line in response_text.splitlines():
                if not line.startswith("data:"):
                    continue
                raw = line[5:].strip()
                if not raw or raw == "[DONE]":
                    continue
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(event.get("usage"), dict):
                    provider_usage = event["usage"]
                choice = (event.get("choices") or [{}])[0]
                delta = choice.get("delta", {})
                chunks.append(delta.get("content") or choice.get("message", {}).get("content", ""))
            text = "".join(chunks)
        else:
            data = json.loads(response_text)
            message = (data.get("choices") or [{}])[0].get("message") or {}
            text = message.get("content") or ""
            tool_calls = message.get("tool_calls") or []
            if isinstance(data.get("usage"), dict):
                provider_usage = data["usage"]
        if not text and not tool_calls:
            return {"ok": False, "error": "AI 服务返回了空内容。"}
        from paperpilot.services.provider_runtime import safe_usage
        if key:
            text = text.replace(key, '[REDACTED]')
        result = {"ok": True, "model": chosen, "content": text, "usage": safe_usage(provider_usage)}
        if tool_calls:
            result["tool_calls"] = tool_calls
            result["finish_reason"] = (data.get("choices") or [{}])[0].get("finish_reason", "")
        return result
    except ConnectTimeout:
        return {'ok':False,'error_code':'provider_connect_failed','error':'连接超时；未自动重发，请检查服务后重新预检。'}
    except (Timeout, RequestException, TimeoutError):
        return {'ok':False,'error_code':'provider_result_unknown','error':'远端结果和费用可能不明；没有自动重发。重新发起可能重复执行或收费，请先核对提供方记录。'}
    except (ValueError,TypeError,AttributeError,IndexError):
        return {'ok':False,'error_code':'provider_protocol_error','error':'AI响应不符合受支持协议；没有自动重发或转向其他服务。'}
    finally:
        if response is not None and hasattr(response,'close'):response.close()
