"""Transport quirks belong to a provider profile, never to every model."""
from urllib.parse import urlparse


def profile(base_url, model="", environ=None):
    env = environ or {}
    host = (urlparse(base_url).hostname or "").casefold()
    name = env.get("PWA_AI_CAPABILITY_PROFILE", "")
    legacy = name == "legacy_gateway" or (not name and host == "aispot.swj0227.icu")
    return {"id": "legacy_gateway" if legacy else "openai_compatible", "schema_version": 1,
            "max_tools": 8 if legacy else 128, "supports_required": not legacy,
            "encoding": "utf-8", "pad_tool_context": legacy,
            "retry_policy": "only_proven_not_executed", "model": model}
