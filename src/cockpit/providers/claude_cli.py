"""Claude lane over the official Claude Code CLI, billed to your Claude subscription.

Hardening so it behaves like a neutral chat model rather than a coding agent:
no built-in tools except web search/page reading when the lane's `web_search` is
on (`--tools "WebSearch,WebFetch"`, pre-approved, max 8 turns; otherwise `--tools ""`), no MCP servers (`--strict-mcp-config`), a
neutral system prompt, an empty dedicated working directory, prompt on stdin.

Billing safety: ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN are removed from the
CLI's environment so it cannot silently bill an API key instead of the
subscription login. `doctor` reads `claude auth status` to confirm the method.

Native sessions are an optimization: `--resume` is used only when the engine
says the native session is exactly in sync with Cockpit's canonical lane head;
otherwise the canonical transcript is replayed into a fresh CLI session.
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

NEUTRAL_SYSTEM_PROMPT = "You are a helpful assistant."


class ClaudeCLIParser:
    """Parses `--output-format stream-json --verbose --include-partial-messages` lines."""

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.saw_text = False
        self.done = False
        self.session_id: str | None = None
        self._announced: str | None = None
        self.searches: list[str] = []
        self.text_len = 0
        self.answer_from = 0  # text before the last tool use is narration, not the answer

    def feed_line(self, line: str) -> list[AdapterEvent]:
        line = line.strip()
        if not line:
            return []
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return []  # tolerate non-JSON noise
        if not isinstance(d, dict):
            return []
        out: list[AdapterEvent] = []
        sid = d.get("session_id")
        if isinstance(sid, str) and sid and sid != self._announced:
            self.session_id = self._announced = sid
            out.append(NativeSession(sid))
        t = d.get("type")
        if t == "system" and d.get("subtype") == "init":
            out.append(ResponseMeta(model=d.get("model"), extra={
                "client_version": d.get("claude_code_version"), "api_key_source": d.get("apiKeySource"),
            }))
        elif t == "stream_event":
            ev = d.get("event") or {}
            if ev.get("type") == "content_block_delta":
                delta = ev.get("delta") or {}
                if delta.get("type") == "text_delta" and delta.get("text"):
                    self.saw_text = True
                    self.text_len += len(delta["text"])
                    out.append(TextDelta(delta["text"]))
        elif t == "assistant":
            for b in (d.get("message") or {}).get("content") or []:
                if b.get("type") == "tool_use":
                    self.answer_from = self.text_len
                    inp = b.get("input") or {}
                    if b.get("name") == "WebSearch" and inp.get("query"):
                        self.searches.append(f"web search: {inp['query']}")
                    elif b.get("name") == "WebFetch" and inp.get("url"):
                        self.searches.append(f"read page: {inp['url']}")
            if not self.saw_text:  # older CLIs / no partial messages
                msg = d.get("message") or {}
                text = "".join(b.get("text", "") for b in msg.get("content") or [] if b.get("type") == "text")
                if text:
                    self.saw_text = True
                    out.append(TextDelta(text))
            model = (d.get("message") or {}).get("model")
            if model:
                out.append(ResponseMeta(model=model))
        elif t == "rate_limit_event":
            info = d.get("rate_limit_info") or {}
            windows = {k: {"utilization": v.get("utilization"), "resets_at": v.get("resetsAt"), "status": v.get("status")}
                       for k, v in (info.get("unifiedWindows") or {}).items() if isinstance(v, dict)}
            out.append(ResponseMeta(extra={"subscription_status": {"status": info.get("status"), "windows": windows}}))
            if info.get("status") == "rejected":
                raise ProviderError(ErrorKind.RATE_LIMIT, f"{self.provider}: subscription usage limit reached")
        elif t == "result":
            if d.get("is_error") or (d.get("subtype") and d.get("subtype") != "success"):
                raise classify_cli_error(self.provider, str(d.get("result") or d.get("subtype")), "claude CLI")
            if not self.saw_text and d.get("result"):
                self.saw_text = True
                out.append(TextDelta(str(d["result"])))
            u = d.get("usage") or {}
            inp = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
            cost = d.get("total_cost_usd")
            # Under a subscription this figure is an API-equivalent estimate, not money spent.
            out.append(Usage(input_tokens=inp or None, output_tokens=u.get("output_tokens")))
            extra: dict[str, Any] = {"api_equivalent_usd": cost} if isinstance(cost, (int, float)) else {}
            if self.searches:
                extra["searches"] = self.searches
            if self.answer_from:
                extra["answer_from"] = self.answer_from
            out.append(ResponseMeta(finish_reason=d.get("stop_reason") or "end_turn", extra=extra))
            self.done = True
        return out


class ClaudeCodeCLIAdapter(ProviderAdapter):
    transport = "claude-code"
    capabilities = frozenset({"streaming", "native_session", "stop", "reasoning_effort", "usage_status", "subscription_auth",
                              "web_search"})

    def __init__(self, config: Any, secrets: Any, workdir: Any = None) -> None:
        super().__init__(config, secrets)
        self.workdir = workdir
        self._version: str | None = None

    def binary(self) -> str | None:
        return which("claude", self.config.cli_path)

    def client_version(self) -> str | None:
        return self._version

    def build_argv(self, binary: str, request: ChatRequest, resume: str | None) -> list[str]:
        argv = [
            binary, "-p",
            "--output-format", "stream-json", "--verbose", "--include-partial-messages",
            *(["--tools", "WebSearch,WebFetch", "--allowedTools", "WebSearch WebFetch", "--max-turns", "8"]
              if request.web_search else ["--tools", ""]),
            "--strict-mcp-config",
            "--system-prompt", request.system_prompt or NEUTRAL_SYSTEM_PROMPT,
        ]
        if request.model and request.model != "default":
            argv += ["--model", validate_model_name(request.model)]
        if request.reasoning:
            argv += ["--effort", request.reasoning]
        if resume:
            argv += ["--resume", resume]
        if request.ephemeral:
            argv.append("--no-session-persistence")
        return argv

    def env(self) -> dict[str, str]:
        return clean_env(self.config.strip_env)

    async def stream(self, request: ChatRequest) -> AsyncIterator[AdapterEvent]:
        resume = request.native_session_id
        try:
            async for ev in self._run(request, resume):
                yield ev
        except ProviderError as e:
            if resume and e.kind == ErrorKind.NATIVE_SESSION_LOST:
                async for ev in self._run(request, None):  # canonical transcript wins
                    yield ev
            else:
                raise

    async def _run(self, request: ChatRequest, resume: str | None) -> AsyncIterator[AdapterEvent]:
        binary = self.binary()
        if not binary:
            raise ProviderError(ErrorKind.NOT_INSTALLED, f"{self.provider_id}: `claude` CLI not found")
        argv = self.build_argv(binary, request, resume)
        stdin_text = request.messages[-1].content if resume else render_replay(request.messages)
        parser = ClaudeCLIParser(self.provider_id)
        res = CLIResult()
        async for line in run_cli(self.provider_id, argv, stdin_text=stdin_text, cwd=self.workdir,
                                  env=self.env(), idle_timeout=self.config.idle_timeout, result=res):
            for ev in parser.feed_line(line):
                if isinstance(ev, ResponseMeta) and ev.extra.get("client_version"):
                    self._version = ev.extra["client_version"]
                yield ev
        if not parser.done:
            raise classify_cli_error(self.provider_id, res.stderr or f"exit code {res.returncode}", "claude CLI")

    async def auth_status(self, binary: str) -> dict[str, Any] | None:
        import asyncio

        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "auth", "status", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
                stdin=asyncio.subprocess.DEVNULL, env=self.env(),
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
            return json.loads(out.decode() or "{}")
        except (OSError, asyncio.TimeoutError, json.JSONDecodeError):
            return None

    async def health_check(self, live: bool = False) -> HealthReport:
        report = self.new_report()
        binary = self.binary()
        report.installed = bool(binary)
        if not binary:
            report.remediation.append("install Claude Code (https://docs.claude.com/en/docs/claude-code), then run `claude` and /login")
            return report
        self._version = await cli_version(binary)
        report.notes.append(f"client: {self._version or 'unknown version'}")
        status = await self.auth_status(binary)
        if status is None:
            report.notes.append("`claude auth status` unavailable on this version; use `doctor --live`")
        else:
            report.authenticated = bool(status.get("loggedIn"))
            method = str(status.get("authMethod") or "")
            if not report.authenticated:
                report.remediation.append("run `claude` and /login with your Claude subscription account")
            elif "oauth" in method or "claude.ai" in method:
                report.subscription_path = True
                report.notes.append(f"auth: {method} (subscription)")
            else:
                report.subscription_path = False
                report.billing_warnings.append(
                    f"Claude Code is authenticated via {method or 'an API key'} - runs would bill the API, "
                    "not your subscription. Run `claude auth logout` then /login with your Claude account."
                )
        import os

        leaked = [v for v in self.config.strip_env if os.environ.get(v)]
        if leaked:
            report.notes.append(f"{', '.join(leaked)} is set in your shell; Cockpit removes it from the Claude Code "
                                "environment so the subscription is used")
        if live:
            await self.live_ping(report)
        return report
