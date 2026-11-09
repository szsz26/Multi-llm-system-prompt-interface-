"""Provider streaming parsers, request shapes and error mapping - no network."""

import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from cockpit.config import BUILTIN_PROVIDERS
from cockpit.domain import ChatMessage
from cockpit.providers import api_config, build_adapter
from cockpit.providers.base import (
    ChatRequest,
    ErrorKind,
    NativeSession,
    ProviderError,
    ResponseMeta,
    SSEDecoder,
    TextDelta,
    Usage,
)
from cockpit.providers.claude_cli import ClaudeCLIParser
from cockpit.secrets import SecretResolver

KEYS = {
    "ANTHROPIC_API_KEY": "sk-ant-test-0000000000000000",
    "OPENAI_API_KEY": "sk-test-openai-000000000000",
    "GEMINI_API_KEY": "gem-test-key-123456",
    "XAI_API_KEY": "xai-test-0000000000000000",
    "PERPLEXITY_API_KEY": "pplx-test-0000000000000000",
}
HISTORY = [ChatMessage("user", "Q1"), ChatMessage("assistant", "A1"), ChatMessage("user", "Q2")]


def sse(*events: tuple[str | None, object]) -> bytes:
    out = []
    for name, data in events:
        if name:
            out.append(f"event: {name}")
        out.append("data: " + (data if isinstance(data, str) else json.dumps(data)))
        out.append("")
    return ("\n".join(out) + "\n").encode()


def make(pid: str, body: bytes = b"", status: int = 200, seen: list | None = None):
    cfg = api_config(replace(next(p for p in BUILTIN_PROVIDERS if p.id == pid)))
    adapter = build_adapter(cfg, SecretResolver(env=KEYS))

    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, content=body, headers={"content-type": "text/event-stream"})

    adapter.http_transport = httpx.MockTransport(handler)
    return adapter


def collect(adapter, messages=HISTORY, **kw):
    async def go():
        evs = []
        async for ev in adapter.stream(ChatRequest(messages=messages, model=adapter.config.model, **kw)):
            evs.append(ev)
        return evs

    evs = asyncio.run(go())
    text = "".join(e.text for e in evs if isinstance(e, TextDelta))
    return text, evs


def usage(evs):
    u = Usage()
    for e in evs:
        if isinstance(e, Usage):
            u.input_tokens = e.input_tokens if e.input_tokens is not None else u.input_tokens
            u.output_tokens = e.output_tokens if e.output_tokens is not None else u.output_tokens
            u.cost_usd = e.cost_usd if e.cost_usd is not None else u.cost_usd
    return u


def meta(evs, field):
    vals = [getattr(e, field) for e in evs if isinstance(e, ResponseMeta) and getattr(e, field)]
    return vals[-1] if vals else None


# ---------------------------------------------------------------- SSE decoder


def test_sse_decoder_multiline_comments_and_events():
    d = SSEDecoder()
    out = [d.feed(x) for x in [": keepalive", "event: foo", "data: a", "data: b", "", "data: c", ""]]
    evs = [e for e in out if e]
    assert evs[0].event == "foo" and evs[0].data == "a\nb"
    assert evs[1].event is None and evs[1].data == "c"


# ---------------------------------------------------------------- Anthropic


ANTHROPIC_STREAM = sse(
    ("message_start", {"type": "message_start", "message": {"id": "msg_1", "model": "claude-opus-5-5",
                                                           "usage": {"input_tokens": 10, "cache_read_input_tokens": 5}}}),
    ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
    ("ping", {"type": "ping"}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hmm"}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "Hello"}}),
    ("content_block_delta", {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " world"}}),
    ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}}),
    ("message_stop", {"type": "message_stop"}),
)


def test_anthropic_stream_and_request():
    seen = []
    text, evs = collect(make("claude", ANTHROPIC_STREAM, seen=seen), system_prompt="sys")
    assert text == "Hello world"  # thinking excluded
    assert usage(evs).input_tokens == 15 and usage(evs).output_tokens == 2
    assert meta(evs, "model") == "claude-opus-5-5" and meta(evs, "finish_reason") == "end_turn"
    req = seen[0]
    assert str(req.url) == "https://api.anthropic.com/v1/messages"
    assert req.headers["x-api-key"] == KEYS["ANTHROPIC_API_KEY"]
    assert req.headers["anthropic-version"]
    body = json.loads(req.content)
    assert body["messages"] == [{"role": "user", "content": "Q1"}, {"role": "assistant", "content": "A1"},
                                {"role": "user", "content": "Q2"}]
    assert body["system"] == "sys" and body["stream"] is True and body["max_tokens"]


def test_anthropic_overloaded_error_event_is_retryable():
    body = sse(("error", {"type": "error", "error": {"type": "overloaded_error", "message": "Overloaded"}}))
    with pytest.raises(ProviderError) as ei:
        collect(make("claude", body))
    assert ei.value.kind == ErrorKind.SERVER and ei.value.retryable


def test_truncated_stream_is_a_protocol_error_not_a_success():
    body = ANTHROPIC_STREAM.split(b"event: message_delta")[0]
    with pytest.raises(ProviderError) as ei:
        collect(make("claude", body))
    assert ei.value.kind == ErrorKind.PROTOCOL and not ei.value.retryable


@pytest.mark.parametrize("status,kind,retryable", [
    (401, ErrorKind.AUTH, False), (429, ErrorKind.RATE_LIMIT, True), (529, ErrorKind.SERVER, True),
    (404, ErrorKind.MODEL_UNAVAILABLE, False), (400, ErrorKind.BAD_REQUEST, False),
])
def test_http_errors_are_classified(status, kind, retryable):
    body = json.dumps({"error": {"type": "x", "message": f"failure {KEYS['ANTHROPIC_API_KEY']}"}}).encode()
    with pytest.raises(ProviderError) as ei:
        collect(make("claude", body, status=status))
    assert ei.value.kind == kind and ei.value.retryable == retryable
    assert KEYS["ANTHROPIC_API_KEY"] not in str(ei.value)  # keys are redacted from errors


# ---------------------------------------------------------------- OpenAI Responses


def test_openai_responses_stream_and_request():
    body = sse(
        ("response.created", {"type": "response.created", "response": {"id": "resp_1", "model": "gpt-5.5-2026-04-23"}}),
        ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "Hel"}),
        ("response.output_text.delta", {"type": "response.output_text.delta", "delta": "lo"}),
        ("response.completed", {"type": "response.completed", "response": {
            "id": "resp_1", "model": "gpt-5.5-2026-04-23", "usage": {"input_tokens": 12, "output_tokens": 3}}}),
    )
    seen = []
    adapter = make("openai", body, seen=seen)
    adapter.config.reasoning = "high"
    text, evs = collect(adapter, reasoning="high", system_prompt="sys")
    assert text == "Hello"
    assert meta(evs, "model") == "gpt-5.5-2026-04-23" and meta(evs, "response_id") == "resp_1"
    assert usage(evs).output_tokens == 3
    req = json.loads(seen[0].content)
    assert seen[0].url.path == "/v1/responses"
    assert seen[0].headers["authorization"] == f"Bearer {KEYS['OPENAI_API_KEY']}"
    assert req["store"] is False and req["instructions"] == "sys" and req["reasoning"] == {"effort": "high"}
    assert [m["role"] for m in req["input"]] == ["user", "assistant", "user"]
    assert "previous_response_id" not in req  # Cockpit owns the history


def test_openai_incomplete_is_flagged_truncated_and_failed_raises():
    body = sse((None, {"type": "response.output_text.delta", "delta": "x"}),
               (None, {"type": "response.incomplete", "response": {"incomplete_details": {"reason": "max_output_tokens"}, "usage": {}}}))
    _, evs = collect(make("openai", body))
    assert meta(evs, "finish_reason") == "max_output_tokens"
    body = sse((None, {"type": "response.failed", "response": {"error": {"code": "rate_limit_exceeded", "message": "slow"}}}))
    with pytest.raises(ProviderError) as ei:
        collect(make("openai", body))
    assert ei.value.kind == ErrorKind.RATE_LIMIT


# ---------------------------------------------------------------- Gemini


def test_gemini_stream_and_request():
    body = sse(
        (None, {"candidates": [{"content": {"role": "model", "parts": [{"text": "thinking...", "thought": True}]}}]}),
        (None, {"candidates": [{"content": {"role": "model", "parts": [{"text": "Hi"}]}}], "modelVersion": "gemini-3.7-flash"}),
        (None, {"candidates": [{"content": {"role": "model", "parts": [{"text": " there"}]}, "finishReason": "STOP"}],
                "usageMetadata": {"promptTokenCount": 7, "candidatesTokenCount": 2, "thoughtsTokenCount": 5},
                "modelVersion": "gemini-3.7-flash", "responseId": "r1"}),
    )
    seen = []
    text, evs = collect(make("gemini", body, seen=seen), system_prompt="sys")
    assert text == "Hi there"
    assert usage(evs).input_tokens == 7 and usage(evs).output_tokens == 7
    assert meta(evs, "finish_reason") == "STOP"
    req = seen[0]
    assert req.url.path == "/v1beta/models/gemini-3.7-flash:streamGenerateContent"
    assert req.url.params["alt"] == "sse"
    assert req.headers["x-goog-api-key"] == KEYS["GEMINI_API_KEY"]
    assert "key" not in req.url.params  # key never in the URL
    b = json.loads(req.content)
    assert [c["role"] for c in b["contents"]] == ["user", "model", "user"]
    assert b["systemInstruction"]["parts"][0]["text"] == "sys"


def test_gemini_safety_block():
    body = sse((None, {"candidates": [{"finishReason": "SAFETY"}]}))
    with pytest.raises(ProviderError) as ei:
        collect(make("gemini", body))
    assert ei.value.kind == ErrorKind.CONTENT_BLOCKED


# ---------------------------------------------------------------- xAI / Perplexity


def test_grok_chat_completions_stream():
    body = sse(
        (None, {"id": "c1", "model": "grok-4.6", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Yo"}}]}),
        (None, {"id": "c1", "model": "grok-4.6", "choices": [{"index": 0, "delta": {"reasoning_content": "hidden"}}]}),
        (None, {"id": "c1", "model": "grok-4.6", "choices": [{"index": 0, "delta": {"content": "!"}, "finish_reason": "stop"}]}),
        (None, {"id": "c1", "choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 2}}),
        (None, "[DONE]"),
    )
    seen = []
    text, evs = collect(make("grok", body, seen=seen), system_prompt="sys")
    assert text == "Yo!"
    assert usage(evs).input_tokens == 9
    req = json.loads(seen[0].content)
    assert str(seen[0].url) == "https://api.x.ai/v1/chat/completions"
    assert req["messages"][0] == {"role": "system", "content": "sys"}
    assert req["stream_options"] == {"include_usage": True}


def test_perplexity_citations_think_stripping_and_cost():
    body = sse(
        (None, {"id": "p1", "model": "sonar-pro", "choices": [{"index": 0, "delta": {"content": "<think>plan</think>Answer [1]"}}]}),
        (None, {"id": "p1", "model": "sonar-pro", "citations": ["https://example.com/a"],
                "search_results": [{"title": "A", "url": "https://example.com/a"}],
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 3, "cost": {"total_cost": 0.0061}}}),
    )
    seen = []
    adapter = make("perplexity", body, seen=seen)
    text, evs = collect(adapter)
    assert adapter.postprocess(text) == "Answer [1]"
    extras = {}
    for e in evs:
        if isinstance(e, ResponseMeta):
            extras.update(e.extra)
    assert extras["citations"] == ["https://example.com/a"]
    assert usage(evs).cost_usd == pytest.approx(0.0061)
    assert "stream_options" not in json.loads(seen[0].content)


def test_mid_stream_error_chunk():
    body = sse((None, {"choices": [{"index": 0, "delta": {"content": "par"}}]}),
               (None, {"error": {"message": "internal", "code": 500}}))
    with pytest.raises(ProviderError) as ei:
        collect(make("grok", body))
    assert ei.value.kind == ErrorKind.SERVER and not ei.value.retryable  # text already streamed


# ---------------------------------------------------------------- health checks


def test_health_check_reports_model_availability():
    def handler(request):
        return httpx.Response(200, json={"data": [{"id": "gpt-5.5"}, {"id": "gpt-5.5-pro"}]})

    adapter = make("openai")
    adapter.http_transport = httpx.MockTransport(handler)
    r = asyncio.run(adapter.health_check())
    assert r.credentials and r.authenticated and r.model_available and r.ok
    adapter.config.model = "gpt-4-old"
    r = asyncio.run(adapter.health_check())
    assert r.model_available is False and "gpt-5.5" in r.suggested_models and not r.ok


def test_health_check_missing_and_rejected_keys():
    cfg = api_config(replace(BUILTIN_PROVIDERS[0]))
    r = asyncio.run(build_adapter(cfg, SecretResolver(env={})).health_check())
    assert r.credentials is False and "ANTHROPIC_API_KEY" in r.remediation[0]
    adapter = make("claude")
    adapter.http_transport = httpx.MockTransport(lambda req: httpx.Response(401, json={"error": {"message": "invalid x-api-key"}}))
    r = asyncio.run(adapter.health_check())
    assert r.authenticated is False and not r.ok


# ---------------------------------------------------------------- Claude Code CLI


# Captured from `claude -p --output-format stream-json --verbose --include-partial-messages` (v2.1).
CLI_LINES = [
    '{"type":"system","subtype":"init","cwd":"/tmp/x","session_id":"a2ef40cb-441a-4ee4-bdbb-1a035179a328","tools":[],"model":"claude-opus-5-5","claude_code_version":"2.1.318"}',
    '{"type":"system","subtype":"status","status":"requesting","session_id":"a2ef40cb-441a-4ee4-bdbb-1a035179a328"}',
    '{"type":"stream_event","event":{"type":"message_start","message":{}},"session_id":"a2ef40cb-441a-4ee4-bdbb-1a035179a328"}',
    '{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"hello"}},"session_id":"a2ef40cb-441a-4ee4-bdbb-1a035179a328"}',
    '{"type":"stream_event","event":{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":" there"}},"session_id":"a2ef40cb-441a-4ee4-bdbb-1a035179a328"}',
    '{"type":"assistant","message":{"model":"claude-opus-5-5","content":[{"type":"text","text":"hello there"}]},"session_id":"a2ef40cb-441a-4ee4-bdbb-1a035179a328"}',
    '{"type":"rate_limit_event","rate_limit_info":{"status":"allowed"},"session_id":"a2ef40cb-441a-4ee4-bdbb-1a035179a328"}',
    'not json noise',
    '{"type":"result","subtype":"success","is_error":false,"result":"hello there","stop_reason":"end_turn","session_id":"a2ef40cb-441a-4ee4-bdbb-1a035179a328","total_cost_usd":0.0065,"usage":{"input_tokens":2,"cache_creation_input_tokens":791,"cache_read_input_tokens":555,"output_tokens":5}}',
]


def test_claude_cli_parser_extracts_text_session_and_usage():
    p = ClaudeCLIParser("claude")
    evs = [e for line in CLI_LINES for e in p.feed_line(line)]
    assert "".join(e.text for e in evs if isinstance(e, TextDelta)) == "hello there"  # not duplicated
    sessions = [e.session_id for e in evs if isinstance(e, NativeSession)]
    assert sessions == ["a2ef40cb-441a-4ee4-bdbb-1a035179a328"]  # announced once
    assert p.done
    u = usage(evs)
    assert u.input_tokens == 1348 and u.output_tokens == 5
    assert u.cost_usd is None  # subscription: no money spent...
    extra = {k: v for e in evs if isinstance(e, ResponseMeta) for k, v in e.extra.items()}
    assert extra["api_equivalent_usd"] == pytest.approx(0.0065)  # ...the CLI's figure is kept as an estimate only


def test_claude_cli_parser_without_partial_messages_falls_back_to_assistant_message():
    p = ClaudeCLIParser("claude")
    lines = [CLI_LINES[0], CLI_LINES[5], CLI_LINES[8]]
    evs = [e for line in lines for e in p.feed_line(line)]
    assert "".join(e.text for e in evs if isinstance(e, TextDelta)) == "hello there"


def test_claude_cli_parser_error_result():
    p = ClaudeCLIParser("claude")
    with pytest.raises(ProviderError) as ei:
        p.feed_line('{"type":"result","subtype":"error_during_execution","is_error":true,"result":"Invalid API key · Please run /login","session_id":"s"}')
    assert ei.value.kind == ErrorKind.AUTH


def test_claude_cli_rate_limit_event_and_rejection():
    p = ClaudeCLIParser("claude")
    evs = p.feed_line('{"type":"rate_limit_event","rate_limit_info":{"status":"allowed","unifiedWindows":'
                      '{"five_hour":{"utilization":0.21,"resetsAt":1,"status":"allowed"}}}}')
    status = evs[0].extra["subscription_status"]
    assert status["status"] == "allowed" and status["windows"]["five_hour"]["utilization"] == 0.21
    with pytest.raises(ProviderError) as ei:
        p.feed_line('{"type":"rate_limit_event","rate_limit_info":{"status":"rejected"}}')
    assert ei.value.kind == ErrorKind.RATE_LIMIT


# ---------------------------------------------------------------- Codex CLI (format verified on codex-cli 0.167)

from cockpit.providers.codex_cli import CodexParser  # noqa: E402
from cockpit.providers.command_cli import parse_json_output  # noqa: E402

CODEX_LINES = [
    '{"type":"thread.started","thread_id":"01a1bc54-5edf-75c2-a6d9-6991b847fb4b"}',
    '{"type":"turn.started"}',
    '2026-11-08T18:03:28.612378Z ERROR codex_api::endpoint::responses_websocket: failed to connect',
    '{"type":"error","message":"Reconnecting... 2/5 (stream disconnected before completion)"}',
    '{"type":"item.completed","item":{"id":"item_0","type":"reasoning","text":"thinking"}}',
    '{"type":"item.completed","item":{"id":"item_1","type":"command_execution","command":"ls"}}',
    '{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"La Paz."}}',
    '{"type":"turn.completed","usage":{"input_tokens":120,"cached_input_tokens":100,"output_tokens":4}}',
]


def test_codex_parser():
    p = CodexParser("openai")
    evs = [e for line in CODEX_LINES for e in p.feed_line(line)]
    assert [e.session_id for e in evs if isinstance(e, NativeSession)] == ["01a1bc54-5edf-75c2-a6d9-6991b847fb4b"]
    assert "".join(e.text for e in evs if isinstance(e, TextDelta)) == "La Paz."  # reasoning excluded
    assert p.done and usage(evs).output_tokens == 4 and p.tools_used == 1
    assert any("Reconnecting" in n for n in p.notices)  # transient errors are notices, not failures


def test_codex_turn_failed_classified():
    p = CodexParser("openai")
    with pytest.raises(ProviderError) as ei:
        p.feed_line('{"type":"turn.failed","error":{"message":"You have hit your usage limit"}}')
    assert ei.value.kind == ErrorKind.RATE_LIMIT
    with pytest.raises(ProviderError) as ei:
        CodexParser("openai").feed_line('{"type":"turn.failed","error":{"message":"401 Unauthorized"}}')
    assert ei.value.kind == ErrorKind.AUTH


def test_command_json_output_parsing():
    evs = parse_json_output("grok", 'log line\n{"sessionId":"s-1","result":"Hello","usage":{"input_tokens":3,"output_tokens":1}}')
    assert [e.session_id for e in evs if isinstance(e, NativeSession)] == ["s-1"]
    assert "".join(e.text for e in evs if isinstance(e, TextDelta)) == "Hello"
    with pytest.raises(ProviderError) as ei:
        parse_json_output("grok", '{"unexpected": 1}')
    assert ei.value.kind == ErrorKind.PROTOCOL
    with pytest.raises(ProviderError) as ei:
        parse_json_output("grok", '{"is_error": true, "error": "Not logged in"}')
    assert ei.value.kind == ErrorKind.AUTH


# ---------------------------------------------------------------- Grok Build CLI (format captured from grok 1.0.77)

from cockpit.providers.grok_cli import GrokParser  # noqa: E402

GROK_LINES = [
    '{"type":"available_commands","tools":["read_file"],"commands":["compact"]}',
    '{"type":"thought","data":"The user wants a reply"}',
    '{"type":"text","data":"hello"}',
    '{"type":"text","data":" there"}',
    '{"type":"usage","usage":{"input_tokens":7394,"output_tokens":417,"cache_read_input_tokens":9344,"cache_creation_input_tokens":0,"reasoning_tokens":415}}',
    '{"type":"end","stopReason":"end_turn","sessionId":"01a1bc8b-b8d9-7f71-b2a3-ed97ae55520b","usage":{"input_tokens":7394,"cache_read_input_tokens":9344,"output_tokens":417},"total_cost_usd":0.0095,"modelUsage":{"grok-4.7-build":{"inputTokens":7394}}}',
]


def test_grok_parser():
    p = GrokParser("grok")
    evs = [e for line in GROK_LINES for e in p.feed_line(line)]
    assert "".join(e.text for e in evs if isinstance(e, TextDelta)) == "hello there"  # thoughts excluded
    assert [e.session_id for e in evs if isinstance(e, NativeSession)] == ["01a1bc8b-b8d9-7f71-b2a3-ed97ae55520b"]
    assert p.done and usage(evs).input_tokens == 16738 and usage(evs).cost_usd is None
    assert meta(evs, "model") == "grok-4.7-build"


def test_grok_errors():
    with pytest.raises(ProviderError) as ei:
        GrokParser("grok").feed_line('{"type":"error","message":"Couldn\'t set model \'x\': Invalid params: \\"unknown model id\\"."}')
    assert ei.value.kind == ErrorKind.MODEL_UNAVAILABLE
    with pytest.raises(ProviderError) as ei:
        GrokParser("grok", resuming=True).feed_line('{"type":"error","message":"No saved copy of session abc was found remotely."}')
    assert ei.value.kind == ErrorKind.NATIVE_SESSION_LOST


def test_grok_server_side_searches_recorded():
    p = GrokParser("grok")
    lines = [
        '{"type":"tool_call","toolCallId":"c1","title":"X search:","kind":"search","rawInput":{"variant":"XSearch","backend":true}}',
        '{"type":"tool_call_update","toolCallId":"c1","status":"completed","rawOutput":{"input":"{\\"query\\":\\"boliviano peg 2026\\"}","name":"x_keyword_search"}}',
        '{"type":"text","data":"No."}',
        '{"type":"end","stopReason":"end_turn","sessionId":"s"}',
    ]
    evs = [e for line in lines for e in p.feed_line(line)]
    extra = {k: v for e in evs if isinstance(e, ResponseMeta) for k, v in e.extra.items()}
    assert extra["searches"] == ["x_keyword_search: boliviano peg 2026"]


def test_claude_cli_records_web_searches():
    p = ClaudeCLIParser("claude")
    lines = [
        CLI_LINES[0],
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"WebSearch","input":{"query":"boliviano peg 2026"}}]},"session_id":"a2ef40cb-441a-4ee4-bdbb-1a035179a328"}',
        '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"WebFetch","input":{"url":"https://example.com/bcb","prompt":"x"}}]},"session_id":"a2ef40cb-441a-4ee4-bdbb-1a035179a328"}',
        CLI_LINES[3], CLI_LINES[8],
    ]
    evs = [e for line in lines for e in p.feed_line(line)]
    extra = {k: v for e in evs if isinstance(e, ResponseMeta) for k, v in e.extra.items()}
    assert extra["searches"] == ["web search: boliviano peg 2026", "read page: https://example.com/bcb"]


def test_codex_records_web_search_items():
    p = CodexParser("openai")
    evs = p.feed_line('{"type":"item.completed","item":{"id":"i1","type":"web_search","query":"boliviano exchange rate"}}')
    assert evs[0].extra["searches"] == ["web search: boliviano exchange rate"] and p.tools_used == 0



def test_codex_narration_before_search_is_marked():
    # Real codex-cli 0.167 output (ChatGPT sign-in, --search), trimmed results
    lines = [
        '{"type":"thread.started","thread_id":"01a1c0be-ddd5-7e03-aa73-05cbeca1e241"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"I\u2019ll check timeanddate.com.\\n"}}',
        '{"type":"item.started","item":{"id":"item_1","type":"web_search","query":"","action":{"type":"other"}}}',
        '{"type":"item.completed","item":{"id":"item_1","type":"web_search","query":"site.timeanddate.com current date today UTC","action":{"type":"search","query":"site.timeanddate.com current date today UTC"},"results":[]}}',
        '{"type":"item.completed","item":{"id":"item_2","type":"agent_message","text":"Today is Monday, November 9, 2026 (UTC)."}}',
        '{"type":"turn.completed","usage":{"input_tokens":33027,"cached_input_tokens":24576,"output_tokens":100}}',
    ]
    p = CodexParser("openai")
    evs = [e for line in lines for e in p.feed_line(line)]
    text = "".join(e.text for e in evs if isinstance(e, TextDelta))
    extra = {k: v for e in evs if isinstance(e, ResponseMeta) for k, v in e.extra.items()}
    assert extra["searches"] == ["web search: site.timeanddate.com current date today UTC"]
    assert text[extra["answer_from"]:].strip() == "Today is Monday, November 9, 2026 (UTC)."


def test_codex_no_answer_marker_without_tools():
    p = CodexParser("openai")
    evs = p.feed_line('{"type":"item.completed","item":{"id":"i","type":"agent_message","text":"Sucre."}}')
    evs += p.feed_line('{"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}')
    assert all("answer_from" not in e.extra for e in evs if isinstance(e, ResponseMeta))

def test_grok_web_search_events_recorded():
    p = GrokParser("grok")
    lines = [
        '{"type":"tool_call","toolCallId":"w1","title":"Web search:","kind":"search","rawInput":{"variant":"WebSearch","backend":true}}',
        '{"type":"tool_call_update","toolCallId":"w1","status":"completed","rawOutput":{"action":{"type":"search","query":"Bolivia official exchange rate November 2026","sources":[{"type":"url","url":"https://www.bcb.gob.bo/"}]}}}',
        '{"type":"text","data":"About 11.8."}',
        '{"type":"end","stopReason":"end_turn","sessionId":"s"}',
    ]
    evs = [e for line in lines for e in p.feed_line(line)]
    extra = {k: v for e in evs if isinstance(e, ResponseMeta) for k, v in e.extra.items()}
    assert extra["searches"] == ["web search: Bolivia official exchange rate November 2026"]


def test_grok_narration_before_tools_is_marked_and_cancel_is_failure():
    p = GrokParser("grok")
    lines = [
        '{"type":"text","data":"I will check the central bank. "}',
        '{"type":"tool_call","toolCallId":"f1","title":"Fetch","kind":"fetch"}',
        '{"type":"text","data":"The rate is 11.81."}',
        '{"type":"end","stopReason":"end_turn","sessionId":"s"}',
    ]
    evs = [e for line in lines for e in p.feed_line(line)]
    extra = {k: v for e in evs if isinstance(e, ResponseMeta) for k, v in e.extra.items()}
    full = "".join(e.text for e in evs if isinstance(e, TextDelta))
    assert full[extra["answer_from"]:] == "The rate is 11.81."
    p2 = GrokParser("grok")
    p2.feed_line('{"type":"text","data":"I will check."}')
    with pytest.raises(ProviderError, match="stopped before answering"):
        p2.feed_line('{"type":"end","stopReason":"cancelled","sessionId":"s"}')
