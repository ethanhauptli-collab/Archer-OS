"""Provider request/response handling, offline.

The Anthropic test drives the real SDK against a mocked HTTP transport, so the
request body and headers are exactly what would go over the wire.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from roughcut.providers import ImagePart, ProviderError, TextPart, estimate_cost, extract_json
from roughcut.providers.openai_compat import OpenAICompatProvider

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"], "additionalProperties": False}


def _sse(text: str, stop_reason: str = "end_turn") -> bytes:
    events = [
        ("message_start", {"type": "message_start", "message": {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "content": [], "stop_reason": None, "stop_sequence": None, "usage": {"input_tokens": 1200, "output_tokens": 1, "cache_read_input_tokens": 800, "cache_creation_input_tokens": 0}}}),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": 42}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


def _anthropic_provider(handler):
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    from roughcut.providers.anthropic_provider import AnthropicProvider

    client = anthropic.Anthropic(
        api_key="test-key",
        base_url="https://api.anthropic.test",
        http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)),
        max_retries=0,
    )
    return AnthropicProvider(effort="high", client=client), httpx2


def test_anthropic_request_shape_and_parse():
    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        seen["headers"] = dict(request.headers)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=_sse('{"ok": true}'))

    provider, httpx2 = _anthropic_provider(handler)
    out = provider.complete_json(
        "system text",
        [TextPart("media", cache=True), ImagePart(b"\xff\xd8jpeg"), TextPart("brief")],
        SCHEMA,
        schema_name="edit_plan",
        purpose="edit plan",
    )
    assert out == {"ok": True}
    body = seen["body"]
    assert body["model"] == "claude-opus-5-5"
    assert body["stream"] is True
    assert body["system"] == "system text"
    assert body["output_config"] == {"format": {"type": "json_schema", "schema": SCHEMA}, "effort": "high"}
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in seen["headers"]["anthropic-beta"]
    content = body["messages"][0]["content"]
    assert content[0] == {"type": "text", "text": "media", "cache_control": {"type": "ephemeral"}}
    assert content[1]["type"] == "image" and content[1]["source"]["media_type"] == "image/jpeg"
    assert "thinking" not in body  # Opus 5.5 thinks adaptively by default; effort controls depth
    call = provider.usage.calls[0]
    assert (call.input_tokens, call.cache_read_tokens, call.output_tokens) == (1200, 800, 42)
    assert estimate_cost(provider.usage) == pytest.approx(1200 * 4 / 1e6 + 800 * 0.2 / 1e6 + 42 * 20 / 1e6)


def test_anthropic_refusal_and_truncation_raise():
    replies = iter([_sse("", "refusal"), _sse('{"ok": tr', "max_tokens")])

    def handler(request):
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=next(replies))

    provider, httpx2 = _anthropic_provider(handler)
    with pytest.raises(ProviderError, match="declined"):
        provider.complete_json("s", [TextPart("x")], SCHEMA, schema_name="n", purpose="edit plan")
    with pytest.raises(ProviderError, match="max_tokens"):
        provider.complete_json("s", [TextPart("x")], SCHEMA, schema_name="n", purpose="edit plan")


def test_anthropic_auth_error_is_friendly():
    def handler(request):
        return httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})

    provider, httpx2 = _anthropic_provider(handler)
    with pytest.raises(ProviderError, match="ANTHROPIC_API_KEY"):
        provider.complete_json("s", [TextPart("x")], SCHEMA, schema_name="n", purpose="edit plan")


def test_anthropic_retries_without_fallbacks_if_rejected():
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        if "fallbacks" in body:
            return httpx2.Response(400, json={"type": "error", "error": {"type": "invalid_request_error", "message": "fallbacks: not enabled for this organization"}})
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=_sse('{"ok": false}'))

    provider, httpx2 = _anthropic_provider(handler)
    assert provider.complete_json("s", [TextPart("x")], SCHEMA, schema_name="n", purpose="p") == {"ok": False}
    assert len(calls) == 2 and "fallbacks" not in calls[1]


class _FakeOpenAI:
    """Mimics client.chat.completions.create; fails the first N modes."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=reply))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )


def test_openai_compat_uses_json_schema_first():
    fake = _FakeOpenAI(['{"ok": true}'])
    p = OpenAICompatProvider("some-model", client=fake)
    assert p.complete_json("s", [TextPart("hi")], SCHEMA, schema_name="plan", purpose="p") == {"ok": True}
    req = fake.requests[0]
    assert req["response_format"]["type"] == "json_schema"
    assert req["messages"][1]["content"] == "hi"  # text-only content stays a plain string for local servers


def test_openai_compat_degrades_for_local_servers():
    fake = _FakeOpenAI([RuntimeError("response_format json_schema unsupported"), "Sure! ```json\n{\"ok\": false}\n```"])
    p = OpenAICompatProvider("llama", name="ollama", client=fake)
    assert p.complete_json("s", [TextPart("hi"), ImagePart(b"img")], SCHEMA, schema_name="plan", purpose="p") == {"ok": False}
    second = fake.requests[1]
    assert second["response_format"] == {"type": "json_object"}
    assert "JSON schema" in second["messages"][0]["content"]
    assert second["messages"][1]["content"][1]["type"] == "image_url"


def test_openai_compat_gives_up_with_useful_error():
    fake = _FakeOpenAI([RuntimeError("a"), RuntimeError("b"), "no json here"])
    p = OpenAICompatProvider("m", client=fake)
    with pytest.raises(ProviderError, match="could not produce"):
        p.complete_json("s", [TextPart("hi")], SCHEMA, schema_name="plan", purpose="p")


def test_openai_compat_requires_model():
    with pytest.raises(ProviderError, match="--model"):
        OpenAICompatProvider("", client=_FakeOpenAI([]))


@pytest.mark.parametrize(
    "text,expected",
    [('{"a": 1}', {"a": 1}), ('```json\n{"a": 2}\n```', {"a": 2}), ('Here you go: {"a": 3} hope that helps', {"a": 3})],
)
def test_extract_json(text, expected):
    assert extract_json(text) == expected


def test_compatible_server_without_a_key_does_not_crash(monkeypatch):
    pytest.importorskip("openai")
    from roughcut.providers import make_provider

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    p = make_provider("openai-compatible", model="local", base_url="http://localhost:1234/v1", api_key_env="OPENAI_API_KEY")
    assert p.client.api_key == "none"


def test_openai_without_a_key_is_a_provider_error(monkeypatch):
    pytest.importorskip("openai")
    from roughcut.providers import make_provider

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="openai"):
        make_provider("openai", model="some-model")


# ------------------------------------------------------------ bad keys


@pytest.mark.parametrize(
    "env,expect",
    [
        ({}, "No Claude API key is set"),
        ({"ANTHROPIC_API_KEY": "sk-ant-oat01-abcdefghijklmnopqrstuvwxyz"}, "sign-in token, not an API key"),
        ({"ANTHROPIC_API_KEY": "my-key-1234567890abcdefgh"}, "doesn't look like an Anthropic API key"),
        ({"ANTHROPIC_API_KEY": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"}, "rejected the key sk-ant-api03…wxyz"),
        ({"ANTHROPIC_API_KEY": "sk-ant-api03-x" * 3, "ANTHROPIC_BASE_URL": "http://localhost:8080"}, "ANTHROPIC_BASE_URL is set"),
    ],
)
def test_key_problem_hints(env, expect):
    from roughcut.providers.anthropic_provider import key_problem_hint

    assert expect in key_problem_hint(env)


def test_verify_turns_401_into_an_auth_error(monkeypatch):
    from roughcut.providers import ProviderAuthError

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-abcdefghijklmnopqrstuvwxyz")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx2.Response(401, json={"type": "error", "error": {"type": "authentication_error", "message": "invalid x-api-key"}})

    provider, httpx2 = _anthropic_provider(handler)
    with pytest.raises(ProviderAuthError, match="rejected the key sk-ant-api03"):
        provider.verify()
    assert seen == ["/v1/models/claude-opus-5-5"]


def test_verify_ok_and_unknown_model():
    ok = {"type": "model", "id": "claude-opus-5-5", "display_name": "Claude Opus 5.5", "created_at": "2026-01-01T00:00:00Z"}

    def handler(request):
        if request.url.path.endswith("claude-opus-5-5"):
            return httpx2.Response(200, json=ok)
        return httpx2.Response(404, json={"type": "error", "error": {"type": "not_found_error", "message": "model not found"}})

    provider, httpx2 = _anthropic_provider(handler)
    assert provider.verify() == "Claude Opus 5.5"
    provider.model = "claude-nope"
    with pytest.raises(ProviderError, match="isn't available"):
        provider.verify()


def test_missing_credentials_are_an_auth_error(monkeypatch):
    anthropic = pytest.importorskip("anthropic")
    from roughcut.providers import ProviderAuthError
    from roughcut.providers.anthropic_provider import AnthropicProvider

    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    provider = AnthropicProvider(client=anthropic.Anthropic(base_url="http://127.0.0.1:9", max_retries=0))
    with pytest.raises(ProviderAuthError, match="No Claude API key"):
        provider.verify()


@pytest.mark.parametrize("given,sent", [("sonnet", "claude-sonnet-5-5"), ("claude-sonnet-5-5", "claude-sonnet-5-5"), ("Opus", "claude-opus-5-5"), (None, "claude-opus-5-5")])
def test_sonnet_and_aliases_reach_the_api(given, sent):
    anthropic = pytest.importorskip("anthropic")
    httpx2 = pytest.importorskip("httpx2")
    from roughcut.providers.anthropic_provider import AnthropicProvider

    seen = {}

    def handler(request):
        seen["body"] = json.loads(request.content)
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=_sse('{"ok": true}'))

    client = anthropic.Anthropic(api_key="k", base_url="https://api.anthropic.test", http_client=anthropic.DefaultHttpxClient(transport=httpx2.MockTransport(handler)), max_retries=0)
    provider = AnthropicProvider(model=given, effort="high", client=client)
    provider.complete_json("s", [TextPart("x")], SCHEMA, schema_name="n", purpose="p", max_tokens=128000)
    body = seen["body"]
    assert body["model"] == sent
    assert body["output_config"]["effort"] == "high" and body["max_tokens"] == 128000
    assert body["fallbacks"] == "default"  # Sonnet 5.5 accepts the "default" form on the Claude API
    assert "thinking" not in body and "temperature" not in body


def test_haiku_gets_no_effort():
    def handler(request):
        seen.append(json.loads(request.content))
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=_sse('{"ok": true}'))

    seen = []
    provider, httpx2 = _anthropic_provider(handler)
    provider.model = "claude-haiku-4-5"
    provider.complete_json("s", [TextPart("x")], SCHEMA, schema_name="n", purpose="p")
    assert "effort" not in seen[0]["output_config"]
