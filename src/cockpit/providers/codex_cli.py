"""OpenAI lane over the official Codex CLI (`codex exec --json`), billed to your ChatGPT plan.

What this is - and isn't: Codex is OpenAI's coding agent. It uses your ChatGPT
plan's allowance when signed in with `codex login` (ChatGPT), but it carries
Codex's own agent instructions, so its answers are NOT identical to the
ChatGPT app. Cockpit records transport="codex" on every run so this is never
hidden. There is no subscription-backed programmable surface for plain
ChatGPT chat that we could find; the API is the alternative (paid, opt-in).

Hardening: read-only sandbox, an empty dedicated working directory (-C),
`--skip-git-repo-check`, prompt on stdin, OPENAI_API_KEY/CODEX_API_KEY stripped
from the environment so it cannot silently bill an API key.

Event format verified against codex-cli 0.167: JSONL with `thread.started`
{thread_id}, `turn.started`, `item.completed` {item: {type: agent_message|
reasoning|command_execution|error, text}}, `turn.completed` {usage}, `turn.failed`
{error}, and transient `error` {message} reconnect notices. Codex emits whole
messages, not token deltas, so this lane "streams" per message.
"""

from __future__ import annotations

import asyncio
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


class CodexParser:
    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.saw_text = False
        self.done = False
        self.tools_used = 0
        self.notices: list[str] = []
        self.searches: list[str] = []
        self.text_len = 0
        self.answer_from = 0  # text before the last search/tool call is narration ("I'll check..."), not the answer

    def feed_line(self, line: str) -> list[AdapterEvent]:
        line = line.strip()
        if not line.startswith("{"):
            return []
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            return []
        t = d.get("type")
        if t == "thread.started" and d.get("thread_id"):
            return [NativeSession(str(d["thread_id"]))]
        if t == "item.completed":
            item = d.get("item") or {}
            it = item.get("type")
            if it == "agent_message" and item.get("text"):
                text = ("\n\n" if self.saw_text else "") + str(item["text"])
                self.saw_text = True
                self.text_len += len(text)
                return [TextDelta(text)]
            if it == "web_search":
                action = item.get("action") if isinstance(item.get("action"), dict) else {}
                q = item.get("query") or action.get("query") or action.get("url") or item.get("url")
                self.searches.append(f"web search: {q}" if q else "web search")
                self.answer_from = self.text_len
                return [ResponseMeta(extra={"searches": list(self.searches)})]
            if it in ("command_execution", "file_change", "mcp_tool_call"):
                self.tools_used += 1
                self.answer_from = self.text_len
                return [ResponseMeta(extra={"agent_tool_calls": self.tools_used})]
            if it == "error" and item.get("message"):
                self.notices.append(str(item["message"])[:200])
            return []
        if t == "error":
            self.notices.append(str(d.get("message"))[:200])  # usually a transient reconnect notice
            return []
        if t == "turn.completed":
            u = d.get("usage") or {}
            self.done = True
            extra = {"cached_input_tokens": u.get("cached_input_tokens")} if u.get("cached_input_tokens") else {}
            if self.answer_from:
                extra["answer_from"] = self.answer_from
            return [
                Usage(input_tokens=u.get("input_tokens"), output_tokens=u.get("output_tokens")),
                ResponseMeta(finish_reason="completed", extra=extra),
            ]
        if t == "turn.failed":
            err = d.get("error") or {}
            raise classify_cli_error(self.provider, str(err.get("message") or err or "turn failed"), "codex")
        return []


class CodexCLIAdapter(ProviderAdapter):
    transport = "codex"
    capabilities = frozenset({"streaming", "native_session", "stop", "reasoning_effort", "subscription_auth", "fork",
                              "web_search"})
    streaming_granularity = "message"

    def __init__(self, config: Any, secrets: Any, workdir: Any = None) -> None:
        super().__init__(config, secrets)
        self.workdir = workdir
        self._version: str | None = None

    def binary(self) -> str | None:
        return which("codex", self.config.cli_path)

    def client_version(self) -> str | None:
        return self._version

    def build_argv(self, binary: str, request: ChatRequest, resume: str | None) -> list[str]:
        argv = [binary, *(["--search"] if request.web_search else []),  # global flag: must precede `exec`
                "exec", "--json", "--skip-git-repo-check", "-s", "read-only", "--color", "never"]
        if self.workdir:
            argv += ["-C", str(self.workdir)]
        if request.model and request.model != "default":
            argv += ["-m", validate_model_name(request.model)]
        if request.reasoning:
            argv += ["-c", f'model_reasoning_effort="{request.reasoning}"']
        if request.system_prompt:  # the visible chat-style instruction (never set for judges)
            argv += ["-c", f"developer_instructions={json.dumps(request.system_prompt)}"]
        if request.ephemeral:
            argv.append("--ephemeral")
        if resume:
            argv += ["resume", resume]
        argv.append("-")  # prompt from stdin
        return argv

    async def stream(self, request: ChatRequest) -> AsyncIterator[AdapterEvent]:
        resume = request.native_session_id
        try:
            async for ev in self._run(request, resume):
                yield ev
        except ProviderError as e:
            if resume and e.kind == ErrorKind.NATIVE_SESSION_LOST:
                async for ev in self._run(request, None):
                    yield ev
            else:
                raise

    async def _run(self, request: ChatRequest, resume: str | None) -> AsyncIterator[AdapterEvent]:
        binary = self.binary()
        if not binary:
            raise ProviderError(ErrorKind.NOT_INSTALLED, f"{self.provider_id}: `codex` CLI not found (npm i -g @openai/codex)")
        argv = self.build_argv(binary, request, resume)
        stdin_text = request.messages[-1].content if resume else render_replay(request.messages)
        parser = CodexParser(self.provider_id)
        res = CLIResult()
        async for line in run_cli(self.provider_id, argv, stdin_text=stdin_text, cwd=self.workdir,
                                  env=clean_env(self.config.strip_env), idle_timeout=self.config.idle_timeout, result=res):
            for ev in parser.feed_line(line):
                yield ev
        if not parser.done:
            detail = " | ".join(parser.notices[-3:]) or res.stderr or f"exit code {res.returncode}"
            raise classify_cli_error(self.provider_id, detail, "codex")

    async def login_status(self, binary: str) -> str:
        try:
            proc = await asyncio.create_subprocess_exec(
                binary, "login", "status", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                stdin=asyncio.subprocess.DEVNULL, env=clean_env(self.config.strip_env),
            )
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
            return out.decode("utf-8", "replace").strip()
        except (OSError, asyncio.TimeoutError):
            return ""

    async def health_check(self, live: bool = False) -> HealthReport:
        report = self.new_report()
        binary = self.binary()
        report.installed = bool(binary)
        if not binary:
            report.remediation.append("install the Codex CLI (`npm i -g @openai/codex`), then `codex login` with your ChatGPT account")
            return report
        self._version = await cli_version(binary)
        report.notes.append(f"client: {self._version or 'unknown version'}")
        report.notes.append("Codex is OpenAI's coding agent: answers come with Codex's agent instructions, not ChatGPT's")
        status = await self.login_status(binary)
        low = status.lower()
        if "chatgpt" in low:
            report.authenticated = report.subscription_path = True
            report.notes.append(f"auth: {status.splitlines()[-1][:80]}")
        elif "api key" in low:
            report.authenticated, report.subscription_path = True, False
            report.billing_warnings.append("Codex is logged in with an API key - runs would bill the API. "
                                           "Run `codex logout` then `codex login` with your ChatGPT account.")
        elif "not logged in" in low:
            report.authenticated = False
            report.remediation.append("run `codex login` and sign in with ChatGPT")
        else:
            report.notes.append(f"could not read `codex login status` ({status[:80]!r})")
        if live:
            await self.live_ping(report)
        return report
