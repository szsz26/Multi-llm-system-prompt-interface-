"""Orchestration engine. UI-agnostic: the CLI, the shell and a future web UI drive this.

Invariants enforced here:

* Lane isolation - a lane's request is built only from that lane's own
  messages on the current branch plus the new user message. Cross-model content
  enters a lane only via a user-reviewed feedback packet.
* Round integrity - every round records every lane (run or skip + reason);
  comparison uses only completed runs of one round; nothing is substituted
  from another round or branch.
* Branching, not rewriting - editing an old prompt creates a new branch whose
  lane heads are reconstructed from rounds before the edited one. Later rounds
  of the old branch never leak in. The old branch is preserved.
* Attempts are preserved - retry/rerun add attempts; a failed rerun never
  replaces a good answer.
* Persistent vs one-shot selection - mute/unmute mutate chat state;
  --only/--exclude never do.
* Judges are stateless and visible - they never touch any lane.
* No silent API spend - see billing.py.
* Failures are contained - one lane failing or being stopped never affects another.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import json
import logging
import random
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from . import billing, log
from .analysis import annotate_evidence, build_packet, extract_json, normalize_result
from .config import CockpitConfig, ProviderConfig
from .domain import (
    AnalysisKind, Billing, ChatMessage, CockpitError, CockpitEvent, Disclosure, EventType, RoundKind, RunStatus,
)
from .feedback import DEFAULT_INSTRUCTION, compose_message, render_packet
from .memory import ContextQuery, MemoryProvider, NullMemoryProvider, RoundRecord, render_context_block
from .providers import api_config, build_adapter
from .providers.cli_common import render_replay
from .providers.base import (
    ChatRequest, ErrorKind, NativeSession, ProviderAdapter, ProviderError, ResponseMeta, TextDelta, Usage,
    validate_model_name,
)
from .secrets import SecretResolver
from .sources import SourceReport, analyze_sources
from .store import Analysis, Branch, Chat, FeedbackPacket, Project, Round, Run, Store, now_iso

EventSink = Callable[[CockpitEvent], None]
AdapterFactory = Callable[[ProviderConfig], ProviderAdapter]

WEB_THREAD = "web-thread"  # marks a manual lane whose web conversation is in sync with Cockpit's history
TRUNCATION_REASONS = {"max_tokens", "length", "MAX_TOKENS", "max_output_tokens"}
ERROR_STATUS = {ErrorKind.AUTH: RunStatus.NEEDS_REAUTH, ErrorKind.RATE_LIMIT: RunStatus.RATE_LIMITED,
                ErrorKind.TIMEOUT: RunStatus.TIMEOUT}
_NAME_RE = re.compile(r"^[\w][\w .:\-]{0,99}$")


def _noop(_: CockpitEvent) -> None:
    pass


@dataclass
class Selection:
    """One-round lane override. Never persisted."""

    only: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)


@dataclass
class Target:
    provider: str
    included: bool
    reason: str | None = None


@dataclass
class Ctx:
    project: Project
    chat: Chat
    branch: Branch


@dataclass
class RunPlan:
    run_id: int
    provider: str
    reason: str  # initial | retry | rerun
    expected_head: int | None  # CAS: lane head must still be this at commit time
    parent_id: int | None  # where the new (user, assistant) pair attaches
    history: list[ChatMessage]
    input_text: str
    model: str
    effort: str | None
    native_session_id: str | None
    via_api: bool = False
    new_thread: bool = False


@dataclass
class RoundResult:
    round: Round
    runs: list[Run]

    @property
    def attempted(self) -> list[Run]:
        return [r for r in self.runs if r.status != RunStatus.SKIPPED]

    @property
    def waiting(self) -> list[Run]:
        return [r for r in self.runs if r.status == RunStatus.WAITING]

    @property
    def exit_code(self) -> int:
        att = [r for r in self.attempted if r.status != RunStatus.WAITING]  # waiting on you is not a failure
        ok = [r for r in att if r.status == RunStatus.COMPLETE]
        if att and len(ok) == len(att):
            return 0
        return 2 if ok else 1


@dataclass
class _Collected:
    text: str = ""
    model: str | None = None
    response_id: str | None = None
    finish_reason: str | None = None
    native_session_id: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cost_usd: float | None = None
    first_token_ms: int | None = None
    extra: dict[str, Any] = field(default_factory=dict)


def input_hash(transport: str, model: str, effort: str | None, system: str | None, messages: list[ChatMessage]) -> str:
    payload = json.dumps([transport, model, effort, system, [(m.role, m.content) for m in messages]])
    return hashlib.sha256(payload.encode()).hexdigest()


class Cockpit:
    def __init__(self, config: CockpitConfig, store: Store, *, adapter_factory: AdapterFactory | None = None,
                 memory: MemoryProvider | None = None, secrets: SecretResolver | None = None) -> None:
        self.config = config
        self.store = store
        self.memory: MemoryProvider = memory or NullMemoryProvider()
        secrets = secrets or SecretResolver()
        self._factory = adapter_factory or (lambda cfg: build_adapter(cfg, secrets, config))
        self._adapters: dict[tuple[str, bool], ProviderAdapter] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._task_rounds: dict[str, int] = {}
        self._stop_reasons: dict[tuple[int, str], str] = {}
        self._completed: set[tuple[int, str]] = set()

    # ------------------------------------------------------------------
    # Navigation: projects, chats, branches
    # ------------------------------------------------------------------

    @staticmethod
    def _check_name(kind: str, name: str) -> str:
        name = name.strip()
        if not _NAME_RE.match(name):
            raise CockpitError(f"{kind} name must be 1-100 letters, digits, spaces or . _ : -")
        return name

    def ctx(self, project: str, chat: str, *, create: bool = True) -> Ctx:
        project, chat = self._check_name("project", project), self._check_name("chat", chat)
        p = self.store.get_or_create_project(project) if create else self.store.get_project(project)
        if p is None:
            raise CockpitError(f"no project named {project!r} (see `cockpit projects`)")
        c = self.store.get_chat(p.id, chat)
        if c is None:
            if not create:
                raise CockpitError(f"no chat named {chat!r} in project {project!r} (see `cockpit chats`)")
            c = self.store.create_chat(p.id, chat)
        return Ctx(p, c, self.store.get_branch(c.current_branch_id))  # type: ignore[arg-type]

    def refresh(self, ctx: Ctx) -> Ctx:
        chat = self.store.get_chat_by_id(ctx.chat.id)
        return Ctx(ctx.project, chat, self.store.get_branch(chat.current_branch_id))  # type: ignore[arg-type]

    def switch_branch(self, ctx: Ctx, ref: str) -> Ctx:
        b = self.store.find_branch(ctx.chat.id, ref)
        self.store.set_current_branch(ctx.chat.id, b.id)
        return self.refresh(ctx)

    def lineage(self, ctx: Ctx) -> list[Round]:
        return self.store.lineage(ctx.branch.id)

    def round(self, ctx: Ctx, seq: int | None = None) -> Round:
        if seq is None:
            lin = self.lineage(ctx)
            if not lin:
                raise CockpitError("this chat has no rounds yet - start with `cockpit blast \"...\"`")
            return lin[-1]
        return self.store.get_round(ctx.chat.id, seq)

    # ------------------------------------------------------------------
    # Lanes
    # ------------------------------------------------------------------

    def adapter(self, pid: str, via_api: bool = False) -> ProviderAdapter:
        key = (pid, via_api)
        if key not in self._adapters:
            cfg = self.config.providers[pid]
            self._adapters[key] = self._factory(api_config(cfg) if via_api else cfg)
        return self._adapters[key]

    def lane_config(self, pid: str, via_api: bool = False) -> ProviderConfig:
        cfg = self.config.providers[pid]
        return api_config(cfg) if via_api else cfg

    def is_enabled(self, chat: Chat, pid: str) -> bool:
        st = self.store.lane_settings(chat.id).get(pid)
        return st["enabled"] if st else self.config.providers[pid].enabled_by_default

    def model_for(self, chat: Chat, pid: str, via_api: bool = False) -> str:
        cfg = self.config.providers[pid]
        if via_api and cfg.transport != "api":
            return cfg.api_model or cfg.model
        st = self.store.lane_settings(chat.id).get(pid) or {}
        if st.get("model"):
            return st["model"]
        return (cfg.api_model or cfg.model) if cfg.transport == "api" else cfg.model

    def effort_for(self, chat: Chat, pid: str) -> str | None:
        st = self.store.lane_settings(chat.id).get(pid) or {}
        return st.get("effort") or self.config.providers[pid].effort

    def set_enabled(self, ctx: Ctx, pids: list[str], enabled: bool | None, *, reentry: str = "continue") -> dict[str, bool]:
        """Mute/unmute (enabled=None toggles). History is never touched; `fresh` re-entry resets the lane head."""
        out = {}
        for pid in pids:
            new = (not self.is_enabled(ctx.chat, pid)) if enabled is None else enabled
            self.store.set_lane(ctx.chat.id, pid, enabled=new)
            if new and reentry == "fresh":
                self.store.set_head(ctx.branch.id, pid, None)
            out[pid] = new
        return out

    def set_model(self, ctx: Ctx, pid: str, model: str | None) -> None:
        if model is not None:
            try:
                validate_model_name(model)
            except ProviderError as e:
                raise CockpitError(e.message) from None
        self.store.set_lane(ctx.chat.id, pid, model=model)

    def set_effort(self, ctx: Ctx, pid: str, effort: str | None) -> list[str]:
        self.store.set_lane(ctx.chat.id, pid, effort=effort)
        warnings = []
        if effort and "reasoning_effort" not in self.adapter(pid).capabilities:
            warnings.append(f"{pid}'s transport ({self.adapter(pid).transport}) has no effort control; "
                            "the setting is stored but will not be applied")
        return warnings

    def overview(self, ctx: Ctx) -> list[dict[str, Any]]:
        lin = self.lineage(ctx)
        rows = []
        for pid, cfg in self.config.providers.items():
            head = self.store.get_head(ctx.branch.id, pid).head_message_id
            hist = self.store.history(ctx.chat.id, pid, head)
            last = None
            for r in reversed(lin):
                run = self.store.run_for(r.id, pid)
                if run and run.status == RunStatus.COMPLETE:
                    last = r.seq
                    break
            adapter = self.adapter(pid)
            rows.append({
                "provider": pid, "name": cfg.display_name, "enabled": self.is_enabled(ctx.chat, pid),
                "model": self.model_for(ctx.chat, pid), "effort": self.effort_for(ctx.chat, pid),
                "transport": cfg.transport, "billing": cfg.billing.value, "verified": cfg.verified,
                "turns": len(hist) // 2, "context_tokens": sum(len(m.content) for m in hist) // 4,
                "last_round": last, "capabilities": sorted(adapter.capabilities),
                "web": bool(cfg.web_search and "web_search" in adapter.capabilities)
                or (adapter.transport == "manual" and "web_search" in adapter.capabilities),
                "subscription_status": self.store.latest_usage_meta(pid, "subscription_status"),
            })
        return rows

    def cancel(self, pid: str) -> bool:
        """Stop one lane's in-flight run in this process."""
        task = self._tasks.get(pid)
        if task and not task.done():
            self._stop_reasons[(self._task_rounds[pid], pid)] = "stopped by user"
            task.cancel()
            return True
        return False

    def request_stop(self, ctx: Ctx, pid: str) -> bool:
        """Stop a lane that is running in ANY process (`cockpit stop gemini` from another terminal)."""
        for rnd, run in self.store.running_runs(ctx.chat.id):
            if run.provider == pid:
                self.store.request_stop(rnd.id, pid)
                return True
        return False

    # ------------------------------------------------------------------
    # Targets
    # ------------------------------------------------------------------

    def plan_targets(self, chat: Chat, selection: Selection, ineligible: dict[str, str] | None = None) -> list[Target]:
        only = self.config.resolve_many(selection.only)
        exclude = self.config.resolve_many(selection.exclude)
        targets = []
        for pid in self.config.provider_ids:
            enabled = self.is_enabled(chat, pid)
            if pid in exclude:
                targets.append(Target(pid, False, "excluded (--exclude)"))
            elif only and pid not in only:
                targets.append(Target(pid, False, "not in --only"))
            elif ineligible and pid in ineligible:
                targets.append(Target(pid, False, ineligible[pid]))
            elif not only and not enabled:
                targets.append(Target(pid, False, "muted"))
            else:
                targets.append(Target(pid, True, "muted; included via --only for this round" if (only and not enabled) else None))
        if not any(t.included for t in targets):
            raise CockpitError("no lanes selected for this round (" + "; ".join(f"{t.provider}: {t.reason}" for t in targets) + ")")
        return targets

    # ------------------------------------------------------------------
    # Rounds: blast / follow-up / lane prompt / edit (branch)
    # ------------------------------------------------------------------

    async def send(self, ctx: Ctx, prompt: str, *, fresh: bool, selection: Selection | None = None,
                   allow_api: bool = False, force: bool = False, first: int | None = None,
                   sink: EventSink = _noop, edited_from: int | None = None) -> RoundResult:
        if not prompt.strip():
            raise CockpitError("prompt is empty")
        targets = self.plan_targets(ctx.chat, selection or Selection())
        kind = RoundKind.BLAST if fresh else RoundKind.FOLLOWUP
        included = [t.provider for t in targets if t.included]
        items = await self.memory.retrieve(ContextQuery(ctx.project.name, ctx.chat.name, prompt, kind.value, included))
        input_text = render_context_block(items, prompt)
        rnd = self.store.create_round(ctx.chat.id, ctx.branch.id, kind.value, prompt, edited_from_round_id=edited_from,
                                      context=[i.to_dict() for i in items])
        plans = self._plan_runs(ctx, rnd, targets, input_text, fresh=fresh, force=force, allow_api=allow_api, sink=sink)
        return await self._execute(ctx, rnd, plans, sink, first=first)

    async def edit(self, ctx: Ctx, round_seq: int, prompt: str, *, selection: Selection | None = None,
                   context: str = "continue", allow_api: bool = False, first: int | None = None,
                   sink: EventSink = _noop) -> tuple[Ctx, RoundResult]:
        """Edit an earlier prompt: new branch forked immediately before that round; old branch preserved."""
        target = self.store.get_round(ctx.chat.id, round_seq)
        lin = self.lineage(ctx)
        if target.id not in {r.id for r in lin}:
            raise CockpitError(f"round {round_seq} is not on the current branch ({ctx.branch.name}); "
                               "switch with `cockpit branch use <name>` first")
        prior = [r for r in lin if r.seq < target.seq]
        nb = self.store.create_branch(ctx.chat.id, ctx.branch.id, target.id)
        for pid in self.config.provider_ids:
            self.store.set_head(nb.id, pid, None if context == "fresh" else self._head_before(prior, pid))
        self.store.set_current_branch(ctx.chat.id, nb.id)
        new_ctx = self.refresh(ctx)
        sink(CockpitEvent(EventType.NOTICE, None, None,
                          f"created branch {nb.name} from before round {round_seq}; branch {ctx.branch.name} preserved"))
        fresh = context == "fresh" or target.kind == RoundKind.BLAST.value
        res = await self.send(new_ctx, prompt, fresh=fresh, selection=selection, allow_api=allow_api, force=True,
                              first=first, sink=sink, edited_from=target.id)
        return new_ctx, res

    def _head_before(self, prior: list[Round], pid: str) -> int | None:
        """Reconstruct where a lane stood just before a round, from the rounds on the lineage."""
        for r in reversed(prior):
            run = self.store.run_for(r.id, pid)
            if run is None or run.status == RunStatus.SKIPPED:
                continue
            if run.status == RunStatus.COMPLETE and run.attempt:
                return run.attempt.assistant_message_id
            return run.context_head_id
        return None

    def _api_block(self, pid: str, via_api: bool, model: str, messages: list[ChatMessage], allow_api: bool) -> str | None:
        if self.lane_config(pid, via_api).billing != Billing.API:
            return None
        reason, _ = billing.check(self.config, self.store, provider=pid, model=model, messages=messages,
                                  approved_this_run=allow_api)
        return reason

    def _plan_runs(self, ctx: Ctx, rnd: Round, targets: list[Target], default_input: str, *, fresh: bool,
                   force: bool, allow_api: bool, sink: EventSink,
                   per_lane: dict[str, str] | None = None) -> list[RunPlan]:
        plans: list[RunPlan] = []
        skipped: dict[str, str] = {}

        def skip(pid: str, reason: str) -> None:
            self.store.add_run(rnd.id, pid, status=RunStatus.SKIPPED, skip_reason=reason)
            skipped[pid] = reason

        for t in targets:
            pid = t.provider
            if not t.included:
                skip(pid, t.reason or "skipped")
                continue
            input_text = (per_lane or {}).get(pid, default_input)  # a lane may get its own message this round
            head = self.store.get_head(ctx.branch.id, pid)
            ctx_head = None if fresh else head.head_message_id
            history = self.store.history(ctx.chat.id, pid, ctx_head)
            model = self.model_for(ctx.chat, pid)
            effort = self.effort_for(ctx.chat, pid)
            messages = [*history, ChatMessage("user", input_text)]
            block = self._api_block(pid, False, model, messages, allow_api)
            if block:
                skip(pid, f"API: {block}")
                continue
            adapter = self.adapter(pid)
            if adapter.transport == "manual" and (w := self.store.waiting_run(ctx.chat.id, pid)):
                skip(pid, f"waiting for your answer to round {w[0].seq} (`cockpit lane paste {pid}`)")
                continue
            h = input_hash(adapter.transport, model, effort, self.config.system_prompt_for(pid), messages)
            if not force and (hit := self.store.find_identical(ctx.chat.id, h)):
                skip(pid, f"identical to {hit[1]} in round {hit[0]} (use --force to run again)")
                continue
            if fresh:
                self.store.set_head(ctx.branch.id, pid, None)  # fresh lane context; old messages stay in the tree
            run_id = self.store.add_run(rnd.id, pid, status=RunStatus.QUEUED, input_text=input_text, context_head_id=ctx_head)
            native = head.native_session_id if (not fresh and head.native_head_id is not None
                                                and head.native_head_id == ctx_head) else None
            plans.append(RunPlan(run_id, pid, "initial", ctx_head, ctx_head, history, input_text, model, effort, native,
                                 new_thread=ctx_head is None))
        sink(CockpitEvent(EventType.ROUND_STARTED, None, rnd.seq, rnd.prompt,
                          {"kind": rnd.kind, "branch": ctx.branch.name, "targets": [p.provider for p in plans],
                           "skipped": skipped}))
        for pid, reason in skipped.items():
            sink(CockpitEvent(EventType.RUN_SKIPPED, pid, rnd.seq, reason))
        return plans

    # ------------------------------------------------------------------
    # Retry / rerun (new attempts, all preserved)
    # ------------------------------------------------------------------

    async def rerun(self, ctx: Ctx, providers: list[str] | None = None, *, round_seq: int | None = None,
                    only_failed: bool = False, via_api: bool = False, allow_api: bool = False,
                    sink: EventSink = _noop) -> RoundResult:
        rnd = self.round(ctx, round_seq)
        if rnd.id not in {r.id for r in self.lineage(ctx)}:
            raise CockpitError(f"round {rnd.seq} is not on the current branch")
        pids = self.config.resolve_many(providers) if providers else [
            r.provider for r in self.store.runs_for_round(rnd.id) if r.status.failed]
        plans: list[RunPlan] = []
        problems: list[str] = []
        for pid in pids:
            run = self.store.run_for(rnd.id, pid)
            if run is None or run.status == RunStatus.SKIPPED:
                problems.append(f"{pid} was not run in round {rnd.seq} - use `cockpit lane prompt {pid} ...`")
                continue
            if only_failed and not run.status.failed:
                continue
            head = self.store.get_head(ctx.branch.id, pid)
            expected = run.attempt.assistant_message_id if (run.status == RunStatus.COMPLETE and run.attempt) else run.context_head_id
            if head.head_message_id != expected:
                problems.append(f"{pid} has moved on since round {rnd.seq}; branch instead: `cockpit edit {rnd.seq} \"...\"`")
                continue
            model = self.model_for(ctx.chat, pid, via_api)
            history = self.store.history(ctx.chat.id, pid, run.context_head_id)
            messages = [*history, ChatMessage("user", run.input_text or rnd.prompt)]
            block = self._api_block(pid, via_api, model, messages, allow_api or via_api)
            if block:
                problems.append(f"{pid}: API: {block}")
                continue
            native = None
            if not via_api and head.native_head_id is not None and head.native_head_id == run.context_head_id:
                native = head.native_session_id
            plans.append(RunPlan(run.id, pid, "retry" if run.status.failed else "rerun", head.head_message_id,
                                 run.context_head_id, history, run.input_text or rnd.prompt, model,
                                 self.effort_for(ctx.chat, pid), native, via_api=via_api,
                                 new_thread=run.context_head_id is None))
        for p in problems:
            sink(CockpitEvent(EventType.NOTICE, None, rnd.seq, p))
        if not plans:
            raise CockpitError(f"nothing to {'retry' if only_failed else 'rerun'} in round {rnd.seq}"
                               + (": " + "; ".join(problems) if problems else ""))
        sink(CockpitEvent(EventType.ROUND_STARTED, None, rnd.seq, rnd.prompt,
                          {"kind": f"{rnd.kind} ({plans[0].reason})", "branch": ctx.branch.name,
                           "targets": [p.provider for p in plans], "skipped": {}}))
        return await self._execute(ctx, rnd, plans, sink)

    async def retry(self, ctx: Ctx, providers: list[str] | None = None, **kw: Any) -> RoundResult:
        return await self.rerun(ctx, providers, only_failed=True, **kw)

    # ------------------------------------------------------------------
    # Execution
    # ------------------------------------------------------------------

    async def _execute(self, ctx: Ctx, rnd: Round, plans: list[RunPlan], sink: EventSink,
                       first: int | None = None) -> RoundResult:
        tasks = {p.provider: asyncio.create_task(self._run_one(ctx, rnd, p, sink), name=f"run:{p.provider}") for p in plans}
        self._tasks.update(tasks)
        self._task_rounds.update({pid: rnd.id for pid in tasks})
        watcher = asyncio.create_task(self._watch(rnd, tasks, first)) if tasks else None
        try:
            if tasks:
                await asyncio.gather(*tasks.values(), return_exceptions=True)
        finally:
            if watcher:
                watcher.cancel()
            for pid in tasks:
                self._tasks.pop(pid, None)
            self.store.complete_round(rnd.id)
            runs = self.store.runs_for_round(rnd.id)
            sink(CockpitEvent(EventType.ROUND_COMPLETE, None, rnd.seq, "",
                              {"statuses": {r.provider: r.status.value for r in runs}}))
        result = RoundResult(self.store.get_round_by_id(rnd.id), runs)
        try:
            await self.memory.on_round_complete(RoundRecord(
                ctx.project.name, ctx.chat.name, rnd.seq, rnd.kind, rnd.prompt,
                {r.provider: r.output_text or "" for r in runs if r.status == RunStatus.COMPLETE}))
        except Exception:  # memory must never break the cockpit
            log.event("memory_hook_failed", logging.WARNING, exc_info=True)
        return result

    async def _watch(self, rnd: Round, tasks: dict[str, asyncio.Task[None]], first: int | None) -> None:
        """Cross-process stop requests and early stop (--first N)."""
        while any(not t.done() for t in tasks.values()):
            for sid, pid in self.store.pending_stops(rnd.id):
                self.store.mark_stop_handled(sid)
                t = tasks.get(pid)
                if t and not t.done():
                    self._stop_reasons[(rnd.id, pid)] = "stopped by user"
                    t.cancel()
            if first:
                done = sum(1 for pid in tasks if (rnd.id, pid) in self._completed)
                if done >= first:
                    for pid, t in tasks.items():
                        if not t.done():
                            self._stop_reasons[(rnd.id, pid)] = f"stopped early (--first {first})"
                            t.cancel()
            await asyncio.sleep(0.2)

    async def _collect(self, adapter: ProviderAdapter, cfg: ProviderConfig, request: ChatRequest,
                       on_delta: Callable[[str], None], on_retry: Callable[[ProviderError, int, float], None],
                       on_native: Callable[[str], None] = lambda s: None) -> _Collected:
        """Stream one response: total timeout; one visible retry only for transient errors before any output."""
        t0 = time.monotonic()
        attempt = 0
        async with asyncio.timeout(cfg.timeout):
            while True:
                col = _Collected()
                try:
                    async for ev in adapter.stream(request):
                        if isinstance(ev, TextDelta):
                            if col.first_token_ms is None:
                                col.first_token_ms = int((time.monotonic() - t0) * 1000)
                            col.text += ev.text
                            on_delta(ev.text)
                        elif isinstance(ev, NativeSession):
                            col.native_session_id = ev.session_id
                            on_native(ev.session_id)
                        elif isinstance(ev, Usage):
                            col.input_tokens = ev.input_tokens if ev.input_tokens is not None else col.input_tokens
                            col.output_tokens = ev.output_tokens if ev.output_tokens is not None else col.output_tokens
                            col.cost_usd = ev.cost_usd if ev.cost_usd is not None else col.cost_usd
                        elif isinstance(ev, ResponseMeta):
                            col.model = ev.model or col.model
                            col.response_id = ev.response_id or col.response_id
                            col.finish_reason = ev.finish_reason or col.finish_reason
                            col.extra.update(ev.extra)
                    return col
                except ProviderError as e:
                    transient = e.kind in (ErrorKind.SERVER, ErrorKind.NETWORK, ErrorKind.TIMEOUT)
                    if e.retryable and transient and not col.text and attempt < cfg.max_retries:
                        attempt += 1
                        delay = min(2.0 ** attempt, 20.0) * random.uniform(0.75, 1.25)
                        on_retry(e, attempt, delay)
                        await asyncio.sleep(delay)
                        continue
                    raise

    async def _run_one(self, ctx: Ctx, rnd: Round, plan: RunPlan, sink: EventSink) -> None:
        pid = plan.provider
        t0 = time.monotonic()
        partial: list[str] = []
        prev = self.store.get_run(plan.run_id)

        def emit(t: EventType, text: str = "", **data: Any) -> None:
            sink(CockpitEvent(t, pid, rnd.seq, text, data))

        adapter = self.adapter(pid, plan.via_api)
        cfg = adapter.config
        effort = plan.effort if "reasoning_effort" in adapter.capabilities else None
        notes = []
        if plan.effort and effort is None:
            notes.append(f"effort '{plan.effort}' not supported by the {adapter.transport} transport; not applied")
        system = self.config.system_prompt_for(pid)
        request = ChatRequest(
            messages=[*plan.history, ChatMessage("user", plan.input_text)], model=plan.model, system_prompt=system,
            temperature=cfg.temperature, max_tokens=cfg.max_tokens, reasoning=effort, extra_body=cfg.extra_body,
            native_session_id=plan.native_session_id,
            web_search=bool(cfg.web_search and "web_search" in adapter.capabilities),
        )
        if adapter.transport == "manual":
            self._handoff(ctx, rnd, plan, adapter, prev, request, emit)
            return
        attempt = self.store.add_attempt(
            plan.run_id, plan.reason, status=RunStatus.RUNNING, transport=adapter.transport, billing=cfg.billing.value,
            model_requested=plan.model, effort=effort, started_at=now_iso(),
            input_hash=input_hash(adapter.transport, plan.model, effort, system, request.messages),
            meta={"notes": notes} if notes else {},
        )
        self.store.set_run_status(plan.run_id, RunStatus.RUNNING)
        emit(EventType.RUN_STARTED, model=plan.model, transport=adapter.transport, billing=cfg.billing.value,
             attempt=attempt.n, reason=plan.reason, new_thread=plan.new_thread, prior_turns=len(plan.history) // 2,
             effort=effort, granularity=adapter.streaming_granularity)
        for n in notes:
            emit(EventType.NOTICE, n)

        def on_delta(text: str) -> None:
            partial.append(text)
            emit(EventType.DELTA, text)

        try:
            col = await self._collect(
                adapter, cfg, request, on_delta,
                on_retry=lambda e, n, d: emit(EventType.RUN_RETRYING, e.message, attempt=n, delay=round(d, 1)),
                on_native=lambda sid: emit(EventType.SESSION_CREATED, sid),
            )
        except asyncio.CancelledError:
            reason = self._stop_reasons.pop((rnd.id, pid), None)
            self._fail(plan, attempt.id, prev, t0, RunStatus.STOPPED, "stopped", reason or "interrupted", "".join(partial))
            emit(EventType.RUN_STOPPED, reason or "interrupted")
            if reason is None:
                raise
            return
        except TimeoutError:
            msg = f"no complete response within {cfg.timeout:.0f}s"
            self._fail(plan, attempt.id, prev, t0, RunStatus.TIMEOUT, "timeout", msg, "".join(partial))
            emit(EventType.RUN_ERROR, msg, kind="timeout", status="timeout")
            return
        except ProviderError as e:
            status = ERROR_STATUS.get(e.kind, RunStatus.FAILED)
            self._fail(plan, attempt.id, prev, t0, status, e.kind.value, e.message, "".join(partial))
            emit(EventType.RUN_ERROR, e.message, kind=e.kind.value, status=status.value,
                 billing=cfg.billing.value, transport=adapter.transport)
            return
        except Exception as e:  # adapter bug or unknown format: contained
            log.event("run_crashed", logging.ERROR, exc_info=True, provider=pid)
            msg = f"internal error: {type(e).__name__}: {e}"
            self._fail(plan, attempt.id, prev, t0, RunStatus.FAILED, "internal", msg, "".join(partial))
            emit(EventType.RUN_ERROR, msg, kind="internal", status="failed")
            return

        raw = col.text
        cut = col.extra.pop("answer_from", None)  # drop "I'll search for..." narration before the final answer
        text = adapter.postprocess(raw[cut:].lstrip() if isinstance(cut, int) and 0 < cut < len(raw) else raw)
        latency = int((time.monotonic() - t0) * 1000)
        meta: dict[str, Any] = {k: v for k, v in {"finish_reason": col.finish_reason, **col.extra}.items() if v}
        if notes:
            meta["notes"] = notes
        if plan.new_thread:
            meta["new_thread"] = True
        truncated = col.finish_reason in TRUNCATION_REASONS
        if truncated:
            meta["truncated"] = True
        cost = (billing.actual_cost(self.config, col.model or plan.model, col.input_tokens, col.output_tokens, col.cost_usd)
                if cfg.billing == Billing.API else None)
        self.store.update_attempt(
            attempt.id, raw_output=raw, output_text=text, model_reported=col.model,
            native_id=col.native_session_id or col.response_id, input_tokens=col.input_tokens,
            output_tokens=col.output_tokens, cost_usd=cost, first_token_ms=col.first_token_ms, latency_ms=latency,
            ended_at=now_iso(), client_version=adapter.client_version(), meta=meta,
        )
        self.store.add_usage(
            chat_id=ctx.chat.id, attempt_id=attempt.id, provider=pid, transport=adapter.transport,
            billing=cfg.billing.value, model=col.model or plan.model, input_tokens=col.input_tokens,
            output_tokens=col.output_tokens, cost_usd=cost,
            meta={k: meta[k] for k in ("subscription_status", "api_equivalent_usd") if k in meta},
        )
        if not text.strip():
            self._fail(plan, attempt.id, prev, t0, RunStatus.FAILED, "empty_response", "provider returned an empty response", raw)
            emit(EventType.RUN_ERROR, "empty response", kind="empty_response", status="failed")
            return
        native = col.native_session_id if ("native_session" in adapter.capabilities and not plan.via_api) else None
        msg_id = self.store.commit_exchange(
            attempt_id=attempt.id, run_id=plan.run_id, chat_id=ctx.chat.id, provider=pid, branch_id=ctx.branch.id,
            expected_head=plan.expected_head, parent_id=plan.parent_id, user_text=plan.input_text,
            assistant_text=text, native_session_id=native,
        )
        if msg_id is None:
            msg = "lane changed while this run was in flight; answer kept on the attempt but NOT added to the lane"
            self._fail(plan, attempt.id, prev, t0, RunStatus.FAILED, "conflict", msg, raw)
            emit(EventType.RUN_ERROR, msg, kind="conflict", status="failed")
            return
        self._completed.add((rnd.id, pid))
        log.event("run_complete", provider=pid, round=rnd.seq, model=col.model or plan.model, latency_ms=latency,
                  transport=adapter.transport, billing=cfg.billing.value)
        emit(EventType.RUN_COMPLETE, "", model=col.model or plan.model, latency_ms=latency, attempt=attempt.n,
             first_token_ms=col.first_token_ms, input_tokens=col.input_tokens, output_tokens=col.output_tokens,
             truncated=truncated, finish_reason=col.finish_reason, cost_usd=cost, billing=cfg.billing.value,
             answer=text, searches=meta.get("searches") or [])

    # ------------------------------------------------------------------
    # One-click helper (manual) lanes
    # ------------------------------------------------------------------

    def _handoff(self, ctx: Ctx, rnd: Round, plan: RunPlan, adapter: Any, prev: Run, request: ChatRequest,
                 emit: Callable[..., None]) -> None:
        """Give the prompt to the user (clipboard + their browser); the lane waits for their pasted answer."""
        if plan.parent_id is None:
            mode, text = "new_thread", plan.input_text
        elif plan.native_session_id == WEB_THREAD:
            mode, text = "continue_thread", plan.input_text
        else:  # web conversation is not in sync (branch, fresh re-entry, rerun): start a new one with the history
            mode, text = "replay", render_replay(request.messages)
        h = adapter.handoff(text, mode)
        saved = None
        if not h.copied:
            saved = self.config.home / "handoff" / f"chat{ctx.chat.id}-round{rnd.seq}-{plan.provider}.md"
            saved.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            fd = os.open(saved, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
        info = {"mode": mode, "url": h.url, "copied": h.copied, "opened": h.opened, "saved_to": str(saved) if saved else None}
        attempt = self.store.add_attempt(
            plan.run_id, plan.reason, status=RunStatus.WAITING, transport=adapter.transport,
            billing=adapter.config.billing.value, model_requested=plan.model, started_at=now_iso(),
            meta={"handoff": info},
        )
        if not (prev.attempt and prev.attempt.status == RunStatus.COMPLETE):
            self.store.set_run_status(plan.run_id, RunStatus.WAITING, current_attempt_id=attempt.id)
        else:
            self.store.set_run_status(plan.run_id, RunStatus.COMPLETE)
        emit(EventType.RUN_WAITING, h.text if not (h.copied or saved) else "", attempt=attempt.n, **info)

    def submit_manual(self, ctx: Ctx, pid: str, text: str, *, round_seq: int | None = None) -> Run:
        """Attach the answer you copied from the provider's app to the waiting round."""
        text = (text or "").strip()
        if not text:
            raise CockpitError("the pasted answer is empty - copy the full answer first")
        if round_seq is not None:
            rnd = self.store.get_round(ctx.chat.id, round_seq)
            run = self.store.run_for(rnd.id, pid)
            atts = [a for a in self.store.attempts_for_run(run.id) if a.status == RunStatus.WAITING] if run else []
            if not run or not atts:
                raise CockpitError(f"{pid} is not waiting for an answer in round {round_seq}")
            att = atts[-1]
        else:
            w = self.store.waiting_run(ctx.chat.id, pid)
            if not w:
                raise CockpitError(f"{pid} is not waiting for an answer in this chat")
            rnd, run, att = w
        head = self.store.get_head(rnd.branch_id, pid).head_message_id
        expected = run.attempt.assistant_message_id if (run.status == RunStatus.COMPLETE and run.attempt) else run.context_head_id
        if head != expected:
            raise CockpitError(f"{pid}'s lane has moved on since round {rnd.seq}; this answer can't be attached there")
        started = att.started_at
        latency = None
        if started:
            from datetime import datetime, timezone
            latency = int((datetime.now(timezone.utc) - datetime.fromisoformat(started)).total_seconds() * 1000)
        self.store.update_attempt(att.id, raw_output=text, output_text=text, model_reported="perplexity web (your settings)"
                                  if pid == "perplexity" else "web (your settings)", ended_at=now_iso(), latency_ms=latency)
        msg = self.store.commit_exchange(
            attempt_id=att.id, run_id=run.id, chat_id=ctx.chat.id, provider=pid, branch_id=rnd.branch_id,
            expected_head=head, parent_id=run.context_head_id, user_text=run.input_text or rnd.prompt,
            assistant_text=text, native_session_id=WEB_THREAD,
        )
        if msg is None:
            raise CockpitError(f"{pid}'s lane changed while attaching; nothing was written")
        self.store.add_usage(chat_id=ctx.chat.id, attempt_id=att.id, provider=pid, transport="manual",
                             billing="subscription", model="web")
        return self.store.get_run(run.id)

    def _fail(self, plan: RunPlan, attempt_id: int, prev: Run, t0: float, status: RunStatus, kind: str,
              message: str, partial: str) -> None:
        self.store.update_attempt(
            attempt_id, status=status, error_kind=kind, error_message=message, raw_output=partial or None,
            output_text=partial or None, ended_at=now_iso(), latency_ms=int((time.monotonic() - t0) * 1000),
        )
        if prev.attempt and prev.attempt.status == RunStatus.COMPLETE:
            self.store.set_run_status(plan.run_id, RunStatus.COMPLETE)  # a failed rerun never replaces a good answer
        else:
            self.store.set_run_status(plan.run_id, status, current_attempt_id=attempt_id)
        log.event("run_failed", logging.WARNING, provider=plan.provider, status=status.value, kind=kind)

    # ------------------------------------------------------------------
    # Comparison (local, every round) and analyses (judge runs)
    # ------------------------------------------------------------------

    def completed_runs(self, rnd: Round) -> list[Run]:
        return [r for r in self.store.runs_for_round(rnd.id) if r.status == RunStatus.COMPLETE]

    @staticmethod
    def sources_for(runs: list[Run]) -> SourceReport:
        return analyze_sources({
            r.provider: (r.output_text, (r.meta or {}).get("citations") or (r.meta or {}).get("search_results") or [])
            for r in runs
        })

    def local_compare(self, ctx: Ctx, round_seq: int | None = None) -> dict[str, Any]:
        """Free, deterministic comparison of ONE round. No model call, no semantic claims."""
        rnd = self.round(ctx, round_seq)
        runs = self.store.runs_for_round(rnd.id)
        done = [r for r in runs if r.status == RunStatus.COMPLETE]
        rep = self.sources_for(done)
        name = lambda p: self.config.providers[p].display_name if p in self.config.providers else p  # noqa: E731
        notes: list[str] = []
        for src, who in rep.shared:
            notes.append(f"{', '.join(map(name, who))} all cite {src}: one underlying source, not {len(who)} confirmations.")
        if rep.uncited and len(done) > 1:
            notes.append(f"No sources cited by: {', '.join(map(name, rep.uncited))}.")
        absent = [f"{name(r.provider)} ({r.skip_reason or r.status.value})" for r in runs if r.status != RunStatus.COMPLETE]
        if absent:
            notes.append("Not compared (never substituted from other rounds): " + ", ".join(absent) + ".")
        if len(done) < 2:
            notes.append(f"Only {len(done)} completed response(s): nothing to compare yet.")
        return {"round": rnd, "runs": runs, "sources": rep, "notes": notes}

    async def analyze(self, ctx: Ctx, kind: AnalysisKind, *, judge: str | None = None, round_seq: int | None = None,
                      blind: bool = True, allow_api: bool = False, sink: EventSink = _noop,
                      rng: random.Random | None = None) -> Analysis:
        rnd = self.round(ctx, round_seq)
        runs = self.completed_runs(rnd)
        need = max(2, self.config.min_responses_for_analysis)
        if len(runs) < need:
            raise CockpitError(
                f"round {rnd.seq} has {len(runs)} completed response(s); {kind.value} needs at least {need}. "
                "Responses from other rounds are never substituted - retry failed lanes or start a new round.")
        judge_pid = self.config.resolve_provider(judge or self.config.judge)
        adapter = self.adapter(judge_pid)
        if adapter.transport == "manual":
            raise CockpitError(f"{judge_pid} is a one-click helper lane (you paste its answers), so it can't run as an "
                               "automatic judge - pick another judge with --judge")
        cfg = adapter.config
        model = self.model_for(ctx.chat, judge_pid)
        lin = self.store.lineage(rnd.branch_id)
        prior = [r.prompt for r in lin if r.seq < rnd.seq][-5:]
        notes = None
        if kind == AnalysisKind.SYNTHESIS:
            prev = [a for a in self.store.analyses_for_round(rnd.id) if a.result and a.kind != "synthesis"]
            if prev:
                notes = {k: v for k, v in prev[-1].result.items() if not k.startswith("_")}  # type: ignore[union-attr]
        sources = self.sources_for(runs)
        packet = build_packet(kind, rnd, runs, prior_prompts=prior, sources=sources, blind=blind, rng=rng,
                              reviewer_notes=notes,
                              display_names={p: c.display_name for p, c in self.config.providers.items()})
        request = ChatRequest(messages=[ChatMessage("user", packet.text)], model=model, max_tokens=cfg.max_tokens,
                              reasoning=self.effort_for(ctx.chat, judge_pid) if "reasoning_effort" in adapter.capabilities else None,
                              extra_body=cfg.extra_body, ephemeral=True)
        block = self._api_block(judge_pid, False, model, request.messages, allow_api)
        if block:
            raise CockpitError(f"judge {judge_pid} runs over the API: {block}")
        label = f"judge:{judge_pid}"
        sink(CockpitEvent(EventType.JUDGE_STARTED, label, rnd.seq, kind.value,
                          {"model": model, "blind": blind, "responses": len(runs), "billing": cfg.billing.value}))
        t0 = time.monotonic()
        base = dict(round_id=rnd.id, kind=kind.value, judge_provider=judge_pid, judge_model=model, blind=blind,
                    label_map=packet.label_map, prompt_text=packet.text)
        try:
            col = await self._collect(
                adapter, cfg, request, on_delta=lambda t: sink(CockpitEvent(EventType.JUDGE_DELTA, label, rnd.seq, t)),
                on_retry=lambda e, n, d: sink(CockpitEvent(EventType.NOTICE, label, rnd.seq, f"retrying judge: {e.message}")))
        except (ProviderError, TimeoutError) as e:
            msg = e.message if isinstance(e, ProviderError) else f"judge timed out after {cfg.timeout:.0f}s"
            a = self.store.save_analysis(**base, status="error", error_message=msg,
                                         latency_ms=int((time.monotonic() - t0) * 1000))
            sink(CockpitEvent(EventType.JUDGE_COMPLETE, label, rnd.seq, msg, {"status": "error", "analysis_id": a.id}))
            return a
        result: dict[str, Any] | None
        status = "complete"
        if kind == AnalysisKind.SYNTHESIS:
            result = {"synthesis": col.text.strip()}
        else:
            obj = extract_json(col.text)
            if obj is None:
                result, status = None, "unparsed"
            else:
                result = normalize_result(kind, obj)
                annotate_evidence(result, packet.label_map, sources)
        cost = (billing.actual_cost(self.config, col.model or model, col.input_tokens, col.output_tokens, col.cost_usd)
                if cfg.billing == Billing.API else None)
        a = self.store.save_analysis(**base, raw_output=col.text, result=result, status=status,
                                     input_tokens=col.input_tokens, output_tokens=col.output_tokens, cost_usd=cost,
                                     latency_ms=int((time.monotonic() - t0) * 1000))
        self.store.add_usage(chat_id=ctx.chat.id, analysis_id=a.id, provider=judge_pid, transport=adapter.transport,
                             billing=cfg.billing.value, model=col.model or model, input_tokens=col.input_tokens,
                             output_tokens=col.output_tokens, cost_usd=cost)
        sink(CockpitEvent(EventType.JUDGE_COMPLETE, label, rnd.seq, "", {"status": status, "analysis_id": a.id}))
        return a

    # ------------------------------------------------------------------
    # Feedback packets: draft -> (edit) -> send
    # ------------------------------------------------------------------

    async def draft_feedback(self, ctx: Ctx, *, round_seq: int | None = None, analysis_id: int | None = None,
                             judge: str | None = None, disclosure: Disclosure = Disclosure.ANONYMIZED,
                             include: list[str] | None = None, instruction: str | None = None, blind: bool = True,
                             allow_api: bool = False, sink: EventSink = _noop) -> FeedbackPacket:
        analysis: Analysis | None = None
        if analysis_id is not None:
            analysis = self.store.get_analysis(analysis_id)
            rnd = self.store.get_round_by_id(analysis.round_id)
            if rnd.chat_id != ctx.chat.id:
                raise CockpitError(f"analysis {analysis_id} belongs to a different chat")
            if round_seq is not None and rnd.seq != round_seq:
                raise CockpitError(f"analysis {analysis_id} is for round {rnd.seq}, not round {round_seq}")
        else:
            rnd = self.round(ctx, round_seq)
        if disclosure != Disclosure.CUSTOM and analysis is None:
            reusable = [a for a in self.store.analyses_for_round(rnd.id) if a.result and a.kind != "synthesis"]
            analysis = reusable[-1] if reusable else await self.analyze(
                ctx, AnalysisKind.ADJUDICATE, judge=judge, round_seq=rnd.seq, blind=blind, allow_api=allow_api, sink=sink)
        if disclosure != Disclosure.CUSTOM and (analysis is None or not analysis.result or analysis.kind == "synthesis"):
            raise CockpitError(f"analysis {analysis.id if analysis else '?'} has no structured result to build feedback from "
                               f"(status {analysis.status if analysis else '-'}); try another judge or --disclosure custom")
        done = self.completed_runs(rnd)
        by_label = {}
        if analysis:
            by_run = {r.id: r for r in done}
            by_label = {lbl: by_run[v["run_id"]] for lbl, v in analysis.label_map.items() if v["run_id"] in by_run}
        chosen: list[Run] = []
        if disclosure == Disclosure.EVERYTHING:
            chosen = list(by_label.values()) or done
        elif disclosure == Disclosure.SELECTED:
            if not include:
                raise CockpitError("--disclosure selected needs --include (labels like A,C or lane names)")
            for item in include:
                key = item.strip()
                if key.upper() in by_label:
                    chosen.append(by_label[key.upper()])
                else:
                    pid = self.config.resolve_provider(key)
                    match = [r for r in done if r.provider == pid]
                    if not match:
                        raise CockpitError(f"{pid} has no completed response in round {rnd.seq}")
                    chosen.append(match[0])
        content = render_packet(analysis.result if analysis else None, disclosure,
                                list(analysis.label_map) if analysis else [], chosen)
        return self.store.create_packet(chat_id=ctx.chat.id, round_id=rnd.id, analysis_id=analysis.id if analysis else None,
                                        disclosure=disclosure.value, included=[r.provider for r in chosen],
                                        content=content, instruction=instruction)

    def packet(self, ctx: Ctx, packet_id: int) -> FeedbackPacket:
        pk = self.store.get_packet(packet_id)
        if pk.chat_id != ctx.chat.id:
            raise CockpitError(f"feedback packet {packet_id} belongs to a different chat")
        return pk

    def edit_feedback(self, ctx: Ctx, packet_id: int, *, content: str | None = None,
                      instruction: str | None = None) -> FeedbackPacket:
        pk = self.packet(ctx, packet_id)
        if pk.status != "draft":
            raise CockpitError(f"packet {packet_id} is {pk.status}; draft a new one")
        fields: dict[str, Any] = {}
        if content is not None and content != pk.content:
            if not content.strip():
                raise CockpitError("packet content is empty")
            fields.update(content=content, edited=1)
        if instruction is not None:
            fields["instruction"] = instruction
        if fields:
            self.store.update_packet(packet_id, **fields)
        return self.store.get_packet(packet_id)

    def feedback_eligibility(self, ctx: Ctx, src: Round) -> dict[str, str]:
        """Lanes may only receive feedback about a round whose answer is still their latest context."""
        runs = {r.provider: r for r in self.store.runs_for_round(src.id)}
        ineligible = {}
        for pid in self.config.provider_ids:
            run = runs.get(pid)
            head = self.store.get_head(ctx.branch.id, pid).head_message_id
            if run is None or run.status != RunStatus.COMPLETE or not run.attempt:
                ineligible[pid] = f"did not answer round {src.seq}"
            elif head != run.attempt.assistant_message_id:
                ineligible[pid] = f"lane has moved past round {src.seq}"
        return ineligible

    async def send_feedback(self, ctx: Ctx, packet_id: int, *, instruction: str | None = None,
                            selection: Selection | None = None, allow_api: bool = False, first: int | None = None,
                            sink: EventSink = _noop) -> RoundResult:
        pk = self.packet(ctx, packet_id)
        if pk.status != "draft":
            raise CockpitError(f"packet {packet_id} is already {pk.status}"
                               + (f" (round {self.store.get_round_by_id(pk.sent_round_id).seq})" if pk.sent_round_id else ""))
        src = self.store.get_round_by_id(pk.round_id)
        targets = self.plan_targets(ctx.chat, selection or Selection(), self.feedback_eligibility(ctx, src))
        instr = instruction if instruction is not None else pk.instruction
        message = compose_message(pk.content, instr)
        rnd = self.store.create_round(ctx.chat.id, ctx.branch.id, RoundKind.FEEDBACK.value,
                                      instr or DEFAULT_INSTRUCTION, feedback_packet_id=pk.id)
        self.store.update_packet(pk.id, status="sent", sent_round_id=rnd.id, instruction=instr)
        plans = self._plan_runs(ctx, rnd, targets, message, fresh=False, force=True, allow_api=allow_api, sink=sink)
        return await self._execute(ctx, rnd, plans, sink, first=first)

    SHARE_LEVELS = ("none", "review", "answers")

    async def send_next(self, ctx: Ctx, note: str | None, share: dict[str, str], *, analysis_id: int | None = None,
                        selection: Selection | None = None, allow_api: bool = False,
                        sink: EventSink = _noop) -> RoundResult:
        """The next round, where each lane gets your message plus what YOU chose to share with it:
        'none' = just your message, 'review' = the anonymized comparison of the last round,
        'answers' = the comparison plus every answer from the last round (anonymized).
        All lanes run in ONE round, so the following comparison still compares like with like."""
        note = (note or "").strip() or None
        src = self.round(ctx)
        analysis = self.store.get_analysis(analysis_id) if analysis_id is not None else None
        if analysis is not None and analysis.round_id != src.id:
            raise CockpitError(f"that comparison is for an older round, not round {src.seq}")
        ineligible = self.feedback_eligibility(ctx, src)
        done = {r.id: r for r in self.completed_runs(src)}
        packets: dict[str, FeedbackPacket] = {}

        def packet_for(level: str) -> FeedbackPacket:
            if level not in packets:
                if analysis is None or not analysis.result or analysis.kind == "synthesis":
                    raise CockpitError("there is no comparison of the last round to share; choose 'just my message'")
                disc = Disclosure.EVERYTHING if level == "answers" else Disclosure.ANONYMIZED
                chosen = ([done[v["run_id"]] for v in analysis.label_map.values() if v["run_id"] in done]
                          if level == "answers" else [])
                packets[level] = self.store.create_packet(
                    chat_id=ctx.chat.id, round_id=src.id, analysis_id=analysis.id, disclosure=disc.value,
                    included=[r.provider for r in chosen],
                    content=render_packet(analysis.result, disc, list(analysis.label_map), chosen), instruction=note)
            return packets[level]

        targets = self.plan_targets(ctx.chat, selection or Selection())
        per_lane: dict[str, str] = {}
        planned: list[Target] = []
        for t in targets:
            level = share.get(t.provider, "none")
            if level not in self.SHARE_LEVELS:
                raise CockpitError(f"unknown share level {level!r} for {t.provider}")
            if t.included and level != "none" and t.provider in ineligible:
                level = "none"  # it did not answer the round being reviewed: it only gets your message
            if t.included and level != "none":
                per_lane[t.provider] = compose_message(packet_for(level).content, note)
            elif t.included and note:
                per_lane[t.provider] = note
            elif t.included:
                t = Target(t.provider, False, "nothing to send it (no message and nothing shared)")
            planned.append(t)
        if not per_lane:
            raise CockpitError("nothing to send: type a message or share the comparison with at least one AI")
        kind = RoundKind.FEEDBACK if packets else RoundKind.FOLLOWUP
        first_pk = next(iter(packets.values()), None)
        rnd = self.store.create_round(ctx.chat.id, ctx.branch.id, kind.value, note or DEFAULT_INSTRUCTION,
                                      feedback_packet_id=first_pk.id if first_pk else None)
        for pk in packets.values():
            self.store.update_packet(pk.id, status="sent", sent_round_id=rnd.id, instruction=note)
        plans = self._plan_runs(ctx, rnd, planned, note or DEFAULT_INSTRUCTION, fresh=False, force=True,
                                allow_api=allow_api, sink=sink, per_lane=per_lane)
        return await self._execute(ctx, rnd, plans, sink)
