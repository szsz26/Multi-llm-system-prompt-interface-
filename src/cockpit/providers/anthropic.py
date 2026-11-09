"""Anthropic Messages API (https://api.anthropic.com/v1/messages), streamed over SSE."""

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

API_VERSION = "2023-06-01"

_ERROR_KINDS = {
    "authentication_error": (ErrorKind.AUTH, False),
    "permission_error": (ErrorKind.AUTH, False),
    "not_found_error": (ErrorKind.MODEL_UNAVAILABLE, False),
    "rate_limit_error": (ErrorKind.RATE_LIMIT, True),
    "overloaded_error": (ErrorKind.SERVER, True),
    "api_error": (ErrorKind.SERVER, True),
    "invalid_request_error": (ErrorKind.BAD_REQUEST, False),
}


class AnthropicParser(StreamParser):
    def feed(self, sse: SSEEvent) -> list[AdapterEvent]:
        if not sse.data:
            return []
        d = parse_json(sse.data, self.provider)
        t = d.get("type") or sse.event
        if t == "message_start":
            msg = d.get("message") or {}
            u = msg.get("usage") or {}
            inp = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            return [ResponseMeta(model=msg.get("model"), response_id=msg.get("id")), Usage(input_tokens=inp or None)]
        if t == "content_block_delta":
            delta = d.get("delta") or {}
            if delta.get("type") == "text_delta":
                return self._text(delta.get("text", ""))
            return []  # thinking / signature / tool deltas are not part of the answer text
        if t == "message_delta":
            u = d.get("usage") or {}
            out: list[AdapterEvent] = [ResponseMeta(finish_reason=(d.get("delta") or {}).get("stop_reason"))]
            if "output_tokens" in u:
                out.append(Usage(output_tokens=u["output_tokens"]))
            return out
        if t == "message_stop":
            self.done = True
            return []
        if t == "error":
            err = d.get("error") or {}
            kind, retry = _ERROR_KINDS.get(err.get("type", ""), (ErrorKind.SERVER, True))
            raise ProviderError(kind, f"{self.provider}: {err.get('type')}: {err.get('message')}", retryable=retry and not self.saw_text)
        return []  # ping, content_block_start/stop


class AnthropicAdapter(SSEAdapter):
    capabilities = frozenset({'streaming', 'stop'})
    default_base_url = "https://api.anthropic.com"
    default_key_envs = ("ANTHROPIC_API_KEY",)
    key_help_url = "https://console.anthropic.com/settings/keys"

    def auth_headers(self, key: str) -> dict[str, str]:
        return {"x-api-key": key, "anthropic-version": API_VERSION}

    def build(self, request: ChatRequest, key: str) -> tuple[str, dict[str, str], dict[str, Any]]:
        body: dict[str, Any] = {
            "model": request.model,
            "max_tokens": request.max_tokens or 16000,  # required by this API
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "stream": True,
        }
        if request.system_prompt:
            body["system"] = request.system_prompt
        if request.temperature is not None:
            body["temperature"] = request.temperature
        body = deep_merge(body, request.extra_body)
        headers = {**self.auth_headers(key), "content-type": "application/json"}
        return f"{self.base_url}/v1/messages", headers, body

    def new_parser(self) -> StreamParser:
        return AnthropicParser(self.provider_id)

    async def list_models(self, key: str) -> list[str] | None:
        ids: list[str] = []
        params: dict[str, Any] = {"limit": 1000}
        for _ in range(5):
            data = await self.get_json(f"{self.base_url}/v1/models", self.auth_headers(key), params)
            ids += [m["id"] for m in data.get("data", []) if "id" in m]
            if not data.get("has_more"):
                break
            params["after_id"] = data.get("last_id")
        return ids
