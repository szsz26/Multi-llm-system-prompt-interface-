"""OpenAI-compatible Chat Completions streaming: xAI (Grok), Perplexity (Sonar), custom endpoints."""

from __future__ import annotations

import re
from typing import Any, ClassVar

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
    classify_http_error,
    deep_merge,
    parse_json,
)


class ChatCompletionsParser(StreamParser):
    def feed(self, sse: SSEEvent) -> list[AdapterEvent]:
        data = sse.data.strip()
        if not data:
            return []
        if data == "[DONE]":
            self.done = True
            return []
        d = parse_json(data, self.provider)
        if d.get("error"):
            err = d["error"] if isinstance(d["error"], dict) else {"message": d["error"]}
            status = int(err.get("code") or 500) if str(err.get("code") or "").isdigit() else 500
            e = classify_http_error(self.provider, status, data)
            e.retryable = e.retryable and not self.saw_text
            raise e
        out: list[AdapterEvent] = []
        extra: dict[str, Any] = {}
        for key in ("citations", "search_results"):  # Perplexity sources (final chunks)
            if d.get(key):
                extra[key] = d[key]
        if d.get("model") or d.get("id") or extra:
            out.append(ResponseMeta(model=d.get("model"), response_id=d.get("id"), extra=extra))
        for choice in d.get("choices") or []:
            if choice.get("index", 0) != 0:
                continue
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str):
                out += self._text(content)
            if choice.get("finish_reason"):
                self.done = True  # some servers omit [DONE]
                out.append(ResponseMeta(finish_reason=choice["finish_reason"]))
        u = d.get("usage")
        if isinstance(u, dict):
            cost = u.get("cost")
            if isinstance(cost, dict):
                cost = cost.get("total_cost")
            out.append(
                Usage(
                    input_tokens=u.get("prompt_tokens"),
                    output_tokens=u.get("completion_tokens"),
                    cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
                )
            )
        return out


class ChatCompletionsAdapter(SSEAdapter):
    capabilities = frozenset({'streaming', 'stop', 'reasoning_effort'})
    send_stream_options: ClassVar[bool] = True
    reasoning_param: ClassVar[str | None] = "reasoning_effort"

    @property
    def base_url(self) -> str:
        base = (self.config.base_url or self.default_base_url).rstrip("/")
        if not base:
            raise ProviderError(ErrorKind.NOT_CONFIGURED, f"{self.provider_id}: base_url is required")
        return base

    def build(self, request: ChatRequest, key: str) -> tuple[str, dict[str, str], dict[str, Any]]:
        msgs = [{"role": m.role, "content": m.content} for m in request.messages]
        if request.system_prompt:
            msgs.insert(0, {"role": "system", "content": request.system_prompt})
        body: dict[str, Any] = {"model": request.model, "messages": msgs, "stream": True}
        if self.send_stream_options:
            body["stream_options"] = {"include_usage": True}
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if request.max_tokens:
            body["max_tokens"] = request.max_tokens
        if request.reasoning and self.reasoning_param:
            body[self.reasoning_param] = request.reasoning
        body = deep_merge(body, request.extra_body)
        headers = {**self.auth_headers(key), "content-type": "application/json"}
        return f"{self.base_url}/chat/completions", headers, body

    def new_parser(self) -> StreamParser:
        return ChatCompletionsParser(self.provider_id)

    async def list_models(self, key: str) -> list[str] | None:
        try:
            data = await self.get_json(f"{self.base_url}/models", self.auth_headers(key))
        except ProviderError as e:
            if e.kind in (ErrorKind.MODEL_UNAVAILABLE, ErrorKind.BAD_REQUEST):  # no /models endpoint
                return None
            raise
        return [m["id"] for m in data.get("data", []) if "id" in m]


class OpenAICompatAdapter(ChatCompletionsAdapter):
    """Any OpenAI-compatible endpoint configured with base_url + api_key_env."""


class XAIAdapter(ChatCompletionsAdapter):
    capabilities = frozenset({'streaming', 'stop', 'reasoning_effort'})
    default_base_url = "https://api.x.ai/v1"
    default_key_envs = ("XAI_API_KEY",)
    key_help_url = "https://console.x.ai"


_THINK = re.compile(r"<think>.*?</think>\s*", re.DOTALL)


class PerplexityAdapter(ChatCompletionsAdapter):
    capabilities = frozenset({'streaming', 'stop', 'reasoning_effort', 'citations', 'web_search', 'reports_cost'})
    default_base_url = "https://api.perplexity.ai"
    default_key_envs = ("PERPLEXITY_API_KEY", "PPLX_API_KEY")
    key_help_url = "https://www.perplexity.ai/account/api"
    send_stream_options = False

    def postprocess(self, text: str) -> str:
        # sonar-reasoning models inline their chain of thought in <think> tags.
        return _THINK.sub("", text).strip()

    async def list_models(self, key: str) -> list[str] | None:
        return None  # no documented model-list endpoint; `doctor --live` verifies instead
