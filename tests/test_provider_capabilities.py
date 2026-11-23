import json
import copy
import pytest
from requests import ConnectTimeout
from paperpilot.adapters.openai_compat import call_chat_completion
from paperpilot.services.provider_capabilities import profile


@pytest.mark.parametrize("kind,expected_choice,padded", [("openai_compatible","required",False),("legacy_gateway","auto",True)])
def test_gateway_quirks_are_profile_scoped_and_do_not_mutate_history(kind, expected_choice, padded):
    messages = [{"role":"user","content":"中文 test"}]
    original = copy.deepcopy(messages)
    captured = {}
    def post(url, **kwargs):
        captured.update(json.loads(kwargs["data"].decode("utf-8")))
        raise ConnectTimeout("deterministic transport boundary")
    result = call_chat_completion("agent","","","","local",base_url="http://127.0.0.1:4321",messages=messages,
                                  tools=[{"type":"function","function":{"name":"read","parameters":{"type":"object"}}}],tool_choice="required",request_post=post,
                                  environ={"PWA_AI_CAPABILITY_PROFILE":kind})
    assert not result["ok"]
    assert captured["tool_choice"] == expected_choice
    assert (len(captured["messages"][0]["content"]) > len(original[0]["content"])) == padded
    assert messages == original


def test_tool_limit_is_known_profile_specific():
    assert profile("https://aispot.swj0227.icu/v1")["max_tools"] == 8
    assert profile("https://api.example.org/v1")["max_tools"] == 128
