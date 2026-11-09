"""OpenAI Responses API (https://api.openai.com/v1/responses), streamed over SSE.

Cockpit owns conversation state, so we send the full history as `input` with
`store: false` instead of chaining `previous_response_id`. That keeps OpenAI
on exactly the same footing as every other provider and keeps transcripts
portable and reproducible.
"""

from __future__ import annotations

from typing import Any

from .base import (
    AdapterEvent,
    ChatRequest,
    ErrorKind,
    ProviderError,
    ResponseMeta,
    SSEAdapter,
    SSEEvent,
    StreamParser,
    Usage,
    deep_merge,
    parse_json,
)


def _classify(provider: str, code: str | None, message: str | None, saw_text: bool) -> ProviderError:
    code = (code or "").lower()
    msg = f"{provider}: {code or 'error'}: {message}"
    if "rate_limit" in code or "quota" in code:
        return ProviderError(ErrorKind.RATE_LIMIT, msg, retryable=not saw_text)
    if "server" in code or "overloaded" in code:
        return ProviderError(ErrorKind.SERVER, msg, retryable=not saw_text)
    if "model" in code:
        return ProviderError(ErrorKind.MODEL_UNAVAILABLE, msg)
    if "auth" in code or "api_key" in code:
        return ProviderError(ErrorKind.AUTH, msg)
    return ProviderError(ErrorKind.BAD_REQUEST, msg)


class OpenAIResponsesParser(StreamParser):
    def feed(self, sse: SSEEvent) -> list[AdapterEvent]:
        if not sse.data or sse.data == "[DONE]":
            return []
        d = parse_json(sse.data, self.provider)
        t = d.get("type") or sse.event or ""
        if t == "response.output_text.delta":
            return self._text(d.get("delta", ""))
        if t in ("response.created", "response.in_progress"):
            r = d.get("response") or {}
            return [ResponseMeta(model=r.get("model"), response_id=r.get("id"))]
        if t in ("response.completed", "response.incomplete"):
            r = d.get("response") or {}
            u = r.get("usage") or {}
            reason = "completed"
            if t == "response.incomplete":
                reason = (r.get("incomplete_details") or {}).get("reason") or "incomplete"
            self.done = True
            return [
                ResponseMeta(model=r.get("model"), response_id=r.get("id"), finish_reason=reason),
                Usage(input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens")),
            ]
        if t == "response.failed":
            err = (d.get("response") or {}).get("error") or {}
            raise _classify(self.provider, err.get("code"), err.get("message"), self.saw_text)
        if t == "error":
            err = d.get("error") if isinstance(d.get("error"), dict) else d
            raise _classify(self.provider, err.get("code") or err.get("type"), err.get("message"), self.saw_text)
        return []


class OpenAIResponsesAdapter(SSEAdapter):
    capabilities = frozenset({'streaming', 'stop', 'reasoning_effort'})
    default_base_url = "https://api.openai.com/v1"
    default_key_envs = ("OPENAI_API_KEY",)
    key_help_url = "https://platform.openai.com/api-keys"

    def build(self, request: ChatRequest, key: str) -> tuple[str, dict[str, str], dict[str, Any]]:
        body: dict[str, Any] = {
            "model": request.model,
            "input": [{"role": m.role, "content": m.content} for m in request.messages],
            "stream": True,
            "store": False,
        }
        if request.system_prompt:
            body["instructions"] = request.system_prompt
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.max_tokens:
            body["max_output_tokens"] = request.max_tokens
        if request.reasoning:
            body["reasoning"] = {"effort": request.reasoning}
        body = deep_merge(body, request.extra_body)
        headers = {**self.auth_headers(key), "content-type": "application/json"}
        return f"{self.base_url}/responses", headers, body

    def new_parser(self) -> StreamParser:
        return OpenAIResponsesParser(self.provider_id)

    async def list_models(self, key: str) -> list[str] | None:
        data = await self.get_json(f"{self.base_url}/models", self.auth_headers(key))
        return [m["id"] for m in data.get("data", []) if "id" in m]
