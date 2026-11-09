"""Google Gemini API (generativelanguage.googleapis.com), streamGenerateContent over SSE."""

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
    classify_http_error,
    deep_merge,
    parse_json,
)

_BLOCKED = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION", "IMAGE_SAFETY"}


class GeminiParser(StreamParser):
    def feed(self, sse: SSEEvent) -> list[AdapterEvent]:
        if not sse.data:
            return []
        d = parse_json(sse.data, self.provider)
        if "error" in d:
            err = d["error"] or {}
            raise classify_http_error(self.provider, int(err.get("code") or 500), sse.data)
        out: list[AdapterEvent] = []
        block = (d.get("promptFeedback") or {}).get("blockReason")
        if block:
            raise ProviderError(ErrorKind.CONTENT_BLOCKED, f"{self.provider}: prompt blocked ({block})")
        cands = d.get("candidates") or []
        if cands:
            c0 = cands[0]
            for part in (c0.get("content") or {}).get("parts") or []:
                if part.get("thought"):
                    continue  # thought summaries are not answer text
                if "text" in part:
                    out += self._text(part["text"])
            reason = c0.get("finishReason")
            if reason and reason != "FINISH_REASON_UNSPECIFIED":
                if reason in _BLOCKED and not self.saw_text:
                    raise ProviderError(ErrorKind.CONTENT_BLOCKED, f"{self.provider}: response blocked ({reason})")
                self.done = True
                out.append(ResponseMeta(finish_reason=reason))
        if d.get("modelVersion") or d.get("responseId"):
            out.append(ResponseMeta(model=d.get("modelVersion"), response_id=d.get("responseId")))
        u = d.get("usageMetadata")
        if u:
            outp = (u.get("candidatesTokenCount") or 0) + (u.get("thoughtsTokenCount") or 0)
            out.append(Usage(input_tokens=u.get("promptTokenCount"), output_tokens=outp or None))
        return out


class GeminiAdapter(SSEAdapter):
    capabilities = frozenset({'streaming', 'stop'})
    default_base_url = "https://generativelanguage.googleapis.com"
    default_key_envs = ("GEMINI_API_KEY", "GOOGLE_API_KEY")
    key_help_url = "https://aistudio.google.com/apikey"

    def auth_headers(self, key: str) -> dict[str, str]:
        return {"x-goog-api-key": key}

    @staticmethod
    def _model_path(model: str) -> str:
        return model.removeprefix("models/")

    def build(self, request: ChatRequest, key: str) -> tuple[str, dict[str, str], dict[str, Any]]:
        contents = [
            {"role": "model" if m.role == "assistant" else "user", "parts": [{"text": m.content}]}
            for m in request.messages
        ]
        body: dict[str, Any] = {"contents": contents}
        if request.system_prompt:
            body["systemInstruction"] = {"parts": [{"text": request.system_prompt}]}
        gen: dict[str, Any] = {}
        if request.temperature is not None:
            gen["temperature"] = request.temperature
        if request.max_tokens:
            gen["maxOutputTokens"] = request.max_tokens
        if gen:
            body["generationConfig"] = gen
        body = deep_merge(body, request.extra_body)
        url = f"{self.base_url}/v1beta/models/{self._model_path(request.model)}:streamGenerateContent?alt=sse"
        headers = {**self.auth_headers(key), "content-type": "application/json"}
        return url, headers, body

    def new_parser(self) -> StreamParser:
        return GeminiParser(self.provider_id)

    async def list_models(self, key: str) -> list[str] | None:
        ids: list[str] = []
        params: dict[str, Any] = {"pageSize": 1000}
        for _ in range(5):
            data = await self.get_json(f"{self.base_url}/v1beta/models", self.auth_headers(key), params)
            for m in data.get("models", []):
                if "generateContent" in (m.get("supportedGenerationMethods") or ["generateContent"]):
                    ids.append(self._model_path(m.get("name", "")))
            token = data.get("nextPageToken")
            if not token:
                break
            params["pageToken"] = token
        return ids
