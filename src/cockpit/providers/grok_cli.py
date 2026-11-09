"""Grok lane over the official Grok Build CLI (`grok`), billed to your SuperGrok / grok.com plan.

Format verified against grok 1.0.77 (`--output-format streaming-json`):
  {"type":"available_commands",...}          ignored
  {"type":"thought","data":"..."}            hidden reasoning - never stored as the answer
  {"type":"text","data":"..."}               answer deltas (real token streaming)
  {"type":"usage","usage":{...}}             token usage
  {"type":"end","stopReason":..,"sessionId":..,"usage":{..},...}   completion marker
  {"type":"error","message":"..."}           failure

Note: Grok performs server-side X/web searches on its own (seen as `tool_call`
events with `backend: true`); `--tools ""` and `--disable-web-search` do not stop
them. Cockpit records every search query on the run (`meta.searches`) so the
comparison can see that Grok's evidence may come from X posts.

Hardening (same idea as the Claude lane): built-in tools off (`--tools ""`),
web search off, no subagents, a neutral system prompt, an empty dedicated
working directory, and the prompt passed via a private 0600 file
(`--prompt-file`) - never on the command line. XAI_API_KEY is stripped so the
CLI cannot bill an API key instead of the subscription.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
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


def _usage(u: dict[str, Any]) -> Usage:
    inp = sum(int(u.get(k) or 0) for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
    return Usage(input_tokens=inp or None, output_tokens=u.get("output_tokens"))


class GrokParser:
    def __init__(self, provider: str, resuming: bool = False) -> None:
        self.provider = provider
        self.resuming = resuming
        self.saw_text = False
        self.done = False
        self.searches: list[str] = []  # Grok runs server-side X/web searches even with tools off; record them
        self.text_len = 0
        self.answer_from = 0  # text before the last tool call is narration ("I'll check..."), not the answer

    def feed_line(self, line: str) -> list[AdapterEvent]:
        line = line.strip()
        if not line.startswith("{"):
            return []
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return []
        t = d.get("type")
        if t == "text" and isinstance(d.get("data"), str):
            self.saw_text = True
            self.text_len += len(d["data"])
            return [TextDelta(d["data"])]
        if t == "tool_call":
            self.answer_from = self.text_len
            return []
        if t == "tool_call_update" and isinstance(d.get("rawOutput"), dict):
            raw = d["rawOutput"]
            action = raw.get("action") if isinstance(raw.get("action"), dict) else None
            if action and action.get("query"):  # web search: {"action": {"type": "search", "query": ..., "sources": [...]}}
                self.searches.append(f"web search: {action['query']}")
                return []
            try:  # X search: {"input": "{\"query\": ...}", "name": "x_keyword_search"}
                q = json.loads(raw.get("input") or "{}").get("query")
            except (json.JSONDecodeError, AttributeError):
                q = None
            if q:
                self.searches.append(f"{raw.get('name') or 'search'}: {q}")
            return []
        if t == "usage" and isinstance(d.get("usage"), dict):
            return [_usage(d["usage"])]
        if t == "end":
            stop = str(d.get("stopReason") or "end_turn")
            if stop in ("cancelled", "error", "refusal"):
                raise ProviderError(ErrorKind.INTERNAL, f"{self.provider}: Grok stopped before answering ({stop}); "
                                    "this can happen when it needs a tool it isn't allowed to use")
            self.done = True
            out: list[AdapterEvent] = []
            if d.get("sessionId"):
                out.append(NativeSession(str(d["sessionId"])))
            if isinstance(d.get("usage"), dict):
                out.append(_usage(d["usage"]))
            extra: dict[str, Any] = {}
            if self.searches:
                extra["searches"] = self.searches  # evidence came (partly) from these server-side searches
            if self.answer_from:
                extra["answer_from"] = self.answer_from
            if isinstance(d.get("total_cost_usd"), (int, float)):
                extra["api_equivalent_usd"] = d["total_cost_usd"]  # subscription: an estimate, not money spent
            model = None
            if isinstance(d.get("modelUsage"), dict) and d["modelUsage"]:
                model = next(iter(d["modelUsage"]))
            out.append(ResponseMeta(model=model or d.get("model"), finish_reason=d.get("stopReason") or "end_turn", extra=extra))
            return out
        if t == "error":
            msg = str(d.get("message") or "error")
            low = msg.lower()
            if self.resuming and "session" in low and ("no saved copy" in low or "not found" in low):
                raise ProviderError(ErrorKind.NATIVE_SESSION_LOST, f"{self.provider}: {msg}")
            raise classify_cli_error(self.provider, msg, "grok")
        return []  # thought, available_commands, anything new


class GrokCLIAdapter(ProviderAdapter):
    transport = "grok"
    capabilities = frozenset({"streaming", "native_session", "stop", "reasoning_effort", "subscription_auth", "web_search"})

    def __init__(self, config: Any, secrets: Any, workdir: Any = None) -> None:
        super().__init__(config, secrets)
        self.workdir = workdir
        self._version: str | None = None

    def binary(self) -> str | None:
        return which("grok", self.config.cli_path)

    def client_version(self) -> str | None:
        return self._version

    def build_argv(self, binary: str, request: ChatRequest, prompt_file: str, resume: str | None) -> list[str]:
        argv = [binary, "--prompt-file", prompt_file, "--output-format", "streaming-json",
                *(["--tools", "web_search,web_fetch", "--allow", "web_fetch"] if request.web_search
                  else ["--tools", "", "--disable-web-search"]),
                "--no-subagents",
                "--system-prompt-override", request.system_prompt or NEUTRAL_SYSTEM_PROMPT]
        if self.workdir:
            argv += ["--cwd", str(self.workdir)]
        if request.model and request.model != "default":
            argv += ["-m", validate_model_name(request.model)]
        if request.reasoning:
            argv += ["--reasoning-effort", request.reasoning]
        if resume:
            argv += ["--resume", resume]
        return argv

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
            raise ProviderError(ErrorKind.NOT_INSTALLED, f"{self.provider_id}: `grok` CLI not found")
        prompt = request.messages[-1].content if resume else render_replay(request.messages)
        tmpdir = (Path(self.workdir).parent if self.workdir else Path(tempfile.gettempdir())) / ".prompts"
        tmpdir.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, path = tempfile.mkstemp(prefix="grok-", suffix=".txt", dir=tmpdir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(prompt)
            parser = GrokParser(self.provider_id, resuming=bool(resume))
            res = CLIResult()
            async for line in run_cli(self.provider_id, self.build_argv(binary, request, path, resume), stdin_text=None,
                                      cwd=self.workdir or Path.cwd(), env=clean_env(self.config.strip_env),
                                      idle_timeout=self.config.idle_timeout, result=res):
                for ev in parser.feed_line(line):
                    yield ev
            if not parser.done:
                raise classify_cli_error(self.provider_id, res.stderr or f"exit code {res.returncode}", "grok")
        finally:
            Path(path).unlink(missing_ok=True)

    async def health_check(self, live: bool = False) -> HealthReport:
        report = self.new_report()
        binary = self.binary()
        report.installed = bool(binary)
        if not binary:
            report.remediation.append("install Grok Build: curl -fsSL https://x.ai/cli/install.sh | bash, "
                                      "then `grok login` (or `grok login --device-auth` on a remote machine)")
            return report
        self._version = await cli_version(binary)
        report.notes.append(f"client: {self._version or 'unknown version'}")
        try:
            proc = await asyncio.create_subprocess_exec(binary, "models", stdout=asyncio.subprocess.PIPE,
                                                        stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.DEVNULL,
                                                        env=clean_env(self.config.strip_env))
            out = (await asyncio.wait_for(proc.communicate(), timeout=30))[0].decode("utf-8", "replace")
        except (OSError, asyncio.TimeoutError):
            out = ""
        low = out.lower()
        if "logged in with grok.com" in low:
            report.authenticated = report.subscription_path = True
            report.notes.append("auth: grok.com account (subscription)")
            models = [ln.strip(" *-").split(" ")[0] for ln in out.splitlines() if ln.strip().startswith(("*", "-"))]
            if models and self.config.model != "default":
                report.model_available = self.config.model in models
                if not report.model_available:
                    report.suggested_models = models[:6]
        elif "api key" in low or "deployment key" in low:
            report.authenticated, report.subscription_path = True, False
            report.billing_warnings.append("Grok is signed in with an API/deployment key - runs would bill the API. "
                                           "Run `grok logout` then `grok login` with your SuperGrok account.")
        elif "log in" in low or "login" in low or "not logged" in low or "sign in" in low:
            report.authenticated = False
            report.remediation.append("run `grok login` (or `grok login --device-auth`) with your SuperGrok account")
        else:
            report.notes.append(f"could not read sign-in state from `grok models` ({out[:80]!r})")
        if live:
            await self.live_ping(report)
        return report
