"""Generic subscription-CLI transport, configured entirely from config.toml.

Used for CLIs whose exact output format could not be verified while building
Cockpit (Grok Build `grok`, Google Antigravity `agy`). Presets ship with
``verified = false`` and `doctor` says so; nothing is claimed to work until
`cockpit doctor --live` has exercised it on your machine.

Config fields (ProviderConfig):
  command       argv template, e.g. ["grok", "-p", "{prompt}", "--output-format", "json"]
  model_args    appended when model != "default", e.g. ["-m", "{model}"]
  effort_args   appended when an effort is set, e.g. ["--effort", "{effort}"]
  resume_args   appended when resuming a native session, e.g. ["--resume", "{session_id}"]
  prompt_via    "arg" substitutes {prompt} as ONE argv element; "stdin" pipes it
  output_format "plain" (stdout is the answer, streamed as it arrives)
                "json"  (one JSON object at the end; text/session id extracted)
                "jsonl" (one JSON object per line; deltas/text/session id extracted)

Placeholders are substituted inside single argv elements only - no shell is
ever involved, so prompt text cannot inject commands.
"""

from __future__ import annotations

import json
from typing import Any, AsyncIterator

from .base import (
    AdapterEvent,
    ChatRequest,
    ErrorKind,
    HealthReport,
    NativeSession,
    ProviderAdapter,
    ProviderError,
    ResponseMeta,
    TextDelta,
    Usage,
    validate_model_name,
)
from .cli_common import CLIResult, classify_cli_error, clean_env, cli_version, render_replay, run_cli, which

TEXT_KEYS = ("result", "response", "text", "content", "output", "answer", "message")
SESSION_KEYS = ("sessionId", "session_id", "thread_id", "threadId", "conversation_id", "conversationId")
DELTA_KEYS = ("delta", "text_delta", "chunk")


def _find(obj: Any, keys: tuple[str, ...]) -> Any:
    if isinstance(obj, dict):
        for k in keys:
            v = obj.get(k)
            if isinstance(v, str) and v:
                return v
            if isinstance(v, dict):
                inner = _find(v, ("text", "content"))
                if inner:
                    return inner
    return None


def _usage(obj: Any) -> Usage | None:
    u = obj.get("usage") if isinstance(obj, dict) else None
    if not isinstance(u, dict):
        return None
    inp = u.get("input_tokens", u.get("prompt_tokens", u.get("inputTokens")))
    out = u.get("output_tokens", u.get("completion_tokens", u.get("outputTokens")))
    return Usage(input_tokens=inp, output_tokens=out)


def parse_json_output(provider: str, text: str) -> list[AdapterEvent]:
    """Parse a single final JSON object (tolerates log lines before it)."""
    start = text.find("{")
    obj = None
    while start != -1:
        try:
            obj = json.loads(text[start:])
            break
        except json.JSONDecodeError:
            start = text.find("{", start + 1)
    if not isinstance(obj, dict):
        raise ProviderError(ErrorKind.PROTOCOL, f"{provider}: expected a JSON object on stdout, got: {text[:200]!r}")
    if obj.get("is_error") or (obj.get("error") and not _find(obj, TEXT_KEYS)):
        raise classify_cli_error(provider, json.dumps(obj.get("error") or obj)[:400])
    out: list[AdapterEvent] = []
    sid = _find(obj, SESSION_KEYS)
    if sid:
        out.append(NativeSession(sid))
    body = _find(obj, TEXT_KEYS)
    if not body:
        raise ProviderError(ErrorKind.PROTOCOL, f"{provider}: no answer text in JSON keys {sorted(obj)[:12]}")
    out.append(TextDelta(body))
    if u := _usage(obj):
        out.append(u)
    out.append(ResponseMeta(model=obj.get("model"), finish_reason="completed"))
    return out


class CommandCLIAdapter(ProviderAdapter):
    transport = "command"
    capabilities = frozenset({"streaming", "stop", "subscription_auth"})
    streaming_granularity = "chunks"

    def __init__(self, config: Any, secrets: Any, workdir: Any = None) -> None:
        super().__init__(config, secrets)
        self.workdir = workdir
        self._version: str | None = None
        caps = set(self.capabilities)
        if config.resume_args:
            caps.add("native_session")
        if config.effort_args:
            caps.add("reasoning_effort")
        self.capabilities = frozenset(caps)  # type: ignore[misc]

    def binary(self) -> str | None:
        return which(self.config.command[0], self.config.cli_path)

    def client_version(self) -> str | None:
        return self._version

    def build_argv(self, binary: str, request: ChatRequest, prompt: str, resume: str | None) -> list[str]:
        values = {"prompt": prompt if self.config.prompt_via == "arg" else "", "model": request.model or "",
                  "effort": request.reasoning or "", "session_id": resume or ""}

        def fill(parts: list[str]) -> list[str]:
            out = []
            for p in parts:
                if p == "{prompt}" and self.config.prompt_via != "arg":
                    continue
                out.append(p.format_map(values) if "{" in p else p)
            return out

        argv = [binary, *fill(self.config.command[1:])]
        if request.model and request.model != "default" and self.config.model_args:
            validate_model_name(request.model)
            argv += fill(self.config.model_args)
        if request.reasoning and self.config.effort_args:
            argv += fill(self.config.effort_args)
        if resume and self.config.resume_args:
            argv += fill(self.config.resume_args)
        return argv

    async def stream(self, request: ChatRequest) -> AsyncIterator[AdapterEvent]:
        binary = self.binary()
        if not binary:
            raise ProviderError(ErrorKind.NOT_INSTALLED, f"{self.provider_id}: `{self.config.command[0]}` not found")
        resume = request.native_session_id if self.config.resume_args else None
        prompt = request.messages[-1].content if resume else render_replay(request.messages)
        argv = self.build_argv(binary, request, prompt, resume)
        stdin_text = prompt if self.config.prompt_via == "stdin" else None
        res = CLIResult()
        fmt = self.config.output_format
        buf: list[str] = []
        saw_text = False
        async for chunk in run_cli(self.provider_id, argv, stdin_text=stdin_text, cwd=self.workdir,
                                   env=clean_env(self.config.strip_env), idle_timeout=self.config.idle_timeout,
                                   result=res, read_chunks=(fmt == "plain")):
            if fmt == "plain":
                saw_text = True
                yield TextDelta(chunk)
            elif fmt == "json":
                buf.append(chunk)
            else:  # jsonl
                for ev in self._jsonl(chunk, saw_text):
                    if isinstance(ev, TextDelta):
                        saw_text = True
                    yield ev
        if res.returncode not in (0, None):
            raise classify_cli_error(self.provider_id, res.stderr or "".join(buf)[-400:] or f"exit code {res.returncode}",
                                     self.config.command[0])
        if fmt == "json":
            for ev in parse_json_output(self.provider_id, "".join(buf)):
                yield ev
        elif not saw_text:
            raise ProviderError(ErrorKind.PROTOCOL, f"{self.provider_id}: CLI exited without any answer text")
        else:
            yield ResponseMeta(finish_reason="completed")

    def _jsonl(self, line: str, saw_text: bool) -> list[AdapterEvent]:
        line = line.strip()
        if not line.startswith("{"):
            return []
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            return []
        out: list[AdapterEvent] = []
        if sid := _find(obj, SESSION_KEYS):
            out.append(NativeSession(sid))
        kind = str(obj.get("type") or "")
        delta = _find(obj, DELTA_KEYS)
        if delta and ("delta" in kind or "chunk" in kind or not kind):
            out.append(TextDelta(delta))
        elif not saw_text and ("result" in kind or "final" in kind or "complete" in kind or "message" in kind):
            if body := _find(obj, TEXT_KEYS):
                out.append(TextDelta(body))
        if u := _usage(obj):
            out.append(u)
        if obj.get("is_error") or kind in ("error", "turn.failed"):
            raise classify_cli_error(self.provider_id, json.dumps(obj)[:400], self.config.command[0])
        return out

    async def health_check(self, live: bool = False) -> HealthReport:
        report = self.new_report()
        binary = self.binary()
        report.installed = bool(binary)
        name = self.config.command[0]
        if not binary:
            hint = {"agy": "install Google Antigravity CLI and sign in with your Google AI Pro/Ultra account"}.get(name, f"install `{name}`")
            report.remediation.append(hint)
            return report
        self._version = await cli_version(binary)
        report.notes.append(f"client: {self._version or 'unknown version'}")
        if not self.config.verified:
            report.notes.append(f"UNVERIFIED preset for `{name}`: flags/output format come from public docs, not a tested run. "
                                "Run `cockpit doctor --live` before relying on this lane.")
        report.notes.append("subscription vs API billing for this CLI cannot be detected automatically; "
                            "make sure it is signed in with your subscription account, not an API key")
        if live:
            await self.live_ping(report)
        return report
