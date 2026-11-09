"""Terminal rendering. Consumes engine events and store rows; no orchestration logic.

Modes: rich (live per-lane panels), plain (default when not a TTY: each answer
printed as soon as that lane finishes; progress on stderr), json (JSON lines).
"""

from __future__ import annotations

import json
import sys
import textwrap
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

from rich.console import Console, Group
from rich.live import Live
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .config import CockpitConfig
from .domain import CockpitEvent, EventType, RunStatus
from .store import Analysis, Attempt, Branch, FeedbackPacket, Round, Run

ICON = {
    "complete": ("✓", "green"), "failed": ("✕", "red"), "timeout": ("⏱", "yellow"), "stopped": ("■", "yellow"),
    "skipped": ("⏸", "dim"), "running": ("●", "cyan"), "queued": ("…", "dim"), "retrying": ("↻", "yellow"),
    "needs_reauth": ("🔑", "red"), "rate_limited": ("⧗", "yellow"), "waiting": ("✋", "magenta"),
}


def handoff_instructions(name: str, info: dict) -> str:
    """Plain-language next step for a one-click helper lane."""
    if info.get("saved_to"):
        where = f"The question is saved in {info['saved_to']} (no clipboard here) - copy it from there"
    else:
        where = "The question is on your clipboard"
    if info.get("mode") == "new_thread":
        step = (f"{name} opened in your browser with the question - press Enter there" if info.get("opened")
                else f"{where}; open {info.get('url') or name} and paste it as a NEW conversation")
    elif info.get("mode") == "continue_thread":
        step = f"{where}; paste it into the {name} conversation you are using for this chat"
    else:
        step = f"{where}; start a NEW {name} conversation and paste it (earlier context is included)"
    return f"{step}. Then copy {name}'s full answer."

HINTS = {
    "rate_limited": "subscription allowance exhausted: wait for reset · `cockpit mute {p}` · `cockpit retry {p}` later · "
                    "`cockpit retry {p} --via api` (paid, needs approval)",
    "needs_reauth": "sign in again with the provider's CLI (see `cockpit doctor`), then `cockpit retry {p}`",
    "timeout": "`cockpit retry {p}`",
    "failed": "`cockpit retry {p}`",
    "waiting": "copy its answer, then `cockpit lane paste {p}` (reads your clipboard; or --file answer.md)",
}


def hint_for(status: str, kind: str | None, provider: str) -> str | None:
    if kind == "not_installed":
        return "install/sign in to this lane's CLI (`cockpit doctor` shows how), or mute it"
    if kind == "not_configured":
        return "configure this lane's credentials (`cockpit doctor`), or mute it"
    h = HINTS.get(status)
    return h.format(p=provider) if h else None


def _fmt_ms(ms: int | None) -> str:
    return "-" if ms is None else (f"{ms / 1000:.1f}s" if ms >= 1000 else f"{ms}ms")


@dataclass
class _PState:
    status: str = "queued"
    text: str = ""
    model: str = ""
    started: float = field(default_factory=time.monotonic)
    detail: str = ""
    latency_ms: int | None = None
    tokens: int | None = None


class _LiveRound:
    def __init__(self, renderer: "Renderer") -> None:
        self.r = renderer
        self.states: dict[str, _PState] = {}
        self.skipped: dict[str, str] = {}
        self.round_seq: int | None = None
        self.kind = ""
        self.judge: _PState | None = None
        self.judge_label = ""

    def __call__(self, ev: CockpitEvent) -> None:
        p = ev.provider or ""
        if ev.type == EventType.ROUND_STARTED:
            self.round_seq, self.kind = ev.round_seq, f"{ev.data.get('kind', '')} · branch {ev.data.get('branch', '')}"
            for pid in ev.data.get("targets", []):
                self.states.setdefault(pid, _PState())
        elif ev.type == EventType.RUN_SKIPPED:
            self.skipped[p] = ev.text
        elif ev.type == EventType.RUN_STARTED:
            st = self.states.setdefault(p, _PState())
            st.status, st.model, st.started = "running", ev.data.get("model", ""), time.monotonic()
            bits = [ev.data.get("transport", ""), ev.data.get("billing", "")]
            if ev.data.get("attempt", 1) > 1:
                bits.append(f"attempt {ev.data['attempt']}")
            if ev.data.get("granularity") == "message":
                bits.append("streams per message")
            st.detail = " · ".join(b for b in bits if b)
        elif ev.type == EventType.DELTA:
            st = self.states[p]
            st.status, st.text = "running", st.text + ev.text
        elif ev.type == EventType.RUN_RETRYING:
            self.states[p].status = "retrying"
            self.states[p].detail = f"retry {ev.data.get('attempt')} in {ev.data.get('delay')}s"
        elif ev.type == EventType.RUN_COMPLETE:
            st = self.states[p]
            st.status, st.latency_ms, st.tokens = "complete", ev.data.get("latency_ms"), ev.data.get("output_tokens")
            st.model = ev.data.get("model") or st.model
            st.detail = "TRUNCATED" if ev.data.get("truncated") else ""
        elif ev.type == EventType.RUN_ERROR:
            st = self.states[p]
            st.status, st.detail = ev.data.get("status", "failed"), f"{ev.data.get('kind')}: {ev.text}"
        elif ev.type == EventType.RUN_STOPPED:
            self.states[p].status, self.states[p].detail = "stopped", ev.text
        elif ev.type == EventType.RUN_WAITING:
            st = self.states.setdefault(p, _PState())
            st.status, st.model = "waiting", "your browser"
            st.text = handoff_instructions(self.r.name(p), ev.data)
        elif ev.type == EventType.JUDGE_STARTED:
            self.judge = _PState(status="running", model=ev.data.get("model", ""))
            self.judge_label = f"{p} · {ev.text}" + (" · blind" if ev.data.get("blind") else "")
        elif ev.type == EventType.JUDGE_DELTA and self.judge:
            self.judge.text += ev.text
        elif ev.type == EventType.JUDGE_COMPLETE and self.judge:
            self.judge.status = "complete" if ev.data.get("status") == "complete" else "failed"
        elif ev.type == EventType.NOTICE:
            self.r.err.print(f"[yellow]note[/] {escape(ev.text)}")

    def _panel(self, title: str, st: _PState, lines: int, width: int, show_tail: bool = True) -> Panel:
        icon, color = ICON.get(st.status, ("?", "white"))
        elapsed = st.latency_ms if st.latency_ms is not None else int((time.monotonic() - st.started) * 1000)
        sub = f"{st.status} {_fmt_ms(elapsed)}" + (f" · {st.tokens} tok" if st.tokens else "") + (f" · {st.detail[:70]}" if st.detail else "")
        if show_tail:
            wrapped: list[str] = []
            for line in st.text.splitlines() or [""]:
                wrapped += textwrap.wrap(line, max(10, width - 4)) or [""]
            body = Text("\n".join(wrapped[-lines:]) if st.text else ("waiting…" if st.status in ("running", "queued") else ""))
        else:
            body = Text(f"receiving… {len(st.text)} chars" if st.status == "running" else st.status)
        return Panel(body, title=f"[{color}]{icon}[/] [bold]{escape(title)}[/] [dim]{escape(st.model)}[/]", title_align="left",
                     subtitle=f"[{color}]{escape(sub)}[/]", subtitle_align="right", border_style=color, height=lines + 2)

    def __rich__(self) -> Group:
        c = self.r.console
        n = max(1, len(self.states) + (1 if self.judge else 0))
        lines = max(2, min(14, max(8, c.height - 4) // n - 2))
        parts: list[Any] = []
        if self.round_seq is not None:
            parts.append(Text(f"Round {self.round_seq} · {self.kind}", style="bold"))
        for pid, st in self.states.items():
            parts.append(self._panel(self.r.name(pid), st, lines, c.width))
        if self.judge:
            parts.append(self._panel(f"Judge {self.judge_label}", self.judge, lines, c.width, show_tail="synthesis" in self.judge_label))
        if self.skipped:
            parts.append(Text("  ".join(f"⏸ {self.r.name(p)} ({why})" for p, why in self.skipped.items()), style="dim"))
        return Group(*parts)


class _PlainSink:
    def __init__(self, renderer: "Renderer") -> None:
        self.r = renderer
        self.buf: dict[str, str] = {}

    def __call__(self, ev: CockpitEvent) -> None:
        p, err = ev.provider or "", self.r.err
        if ev.type == EventType.ROUND_STARTED:
            err.print(f"round {ev.round_seq} ({ev.data.get('kind')}, branch {ev.data.get('branch')}) → "
                      f"{', '.join(ev.data.get('targets', [])) or '(none)'}")
        elif ev.type == EventType.RUN_SKIPPED:
            err.print(f"  ⏸ {p}: skipped ({escape(ev.text)})")
        elif ev.type == EventType.DELTA:
            self.buf[p] = self.buf.get(p, "") + ev.text
        elif ev.type == EventType.RUN_RETRYING:
            err.print(f"  ↻ {p}: {escape(ev.text)} (retry {ev.data.get('attempt')})")
        elif ev.type == EventType.RUN_ERROR:
            st = ev.data.get("status", "failed")
            err.print(f"  {ICON.get(st, ('✕',))[0]} {p}: {st}: {escape(ev.text)}")
            hint = hint_for(st, ev.data.get("kind"), p)
            if hint:
                err.print(f"     → {hint}")
        elif ev.type == EventType.RUN_STOPPED:
            err.print(f"  ■ {p}: {escape(ev.text)}")
        elif ev.type == EventType.RUN_WAITING:
            err.print(f"  ✋ {p}: waiting for you - {escape(handoff_instructions(self.r.name(p), ev.data))}")
            if ev.text:
                print(f"--- question for {self.r.name(p)} ---\n{ev.text}\n", flush=True)
        elif ev.type == EventType.RUN_COMPLETE:
            err.print(f"  ✓ {p}: {_fmt_ms(ev.data.get('latency_ms'))}")
            head = " · ".join(x for x in (self.r.name(p), ev.data.get("model") or "", _fmt_ms(ev.data.get("latency_ms"))) if x)
            streamed = self.buf.pop(p, "")
            final = ev.data.get("answer")  # committed answer: narration before the last search is trimmed
            print(f"=== {head} ===\n{final if isinstance(final, str) and final else streamed}\n", flush=True)
            if ev.data.get("searches"):
                print("Searches this model ran:\n" + "\n".join(f"  • {q}" for q in ev.data["searches"]) + "\n",
                      flush=True)
        elif ev.type == EventType.NOTICE:
            err.print(f"  note: {escape(ev.text)}")
        elif ev.type == EventType.JUDGE_STARTED:
            err.print(f"{p} running {ev.text} ({'blind' if ev.data.get('blind') else 'attributed'})…")


class Renderer:
    def __init__(self, config: CockpitConfig, mode: str = "auto", raw: bool = False, no_color: bool = False) -> None:
        self.config = config
        if mode == "auto":
            mode = "rich" if sys.stdout.isatty() else "plain"
        self.mode = mode
        self.raw = raw
        self.console = Console(no_color=no_color, highlight=False)
        self.err = Console(stderr=True, no_color=no_color, highlight=False)

    def name(self, pid: str) -> str:
        p = self.config.providers.get(pid)
        return p.display_name if p else pid

    def out(self, text: str = "", style: str | None = None) -> None:
        if self.mode == "rich":
            self.console.print(Text(text, style=style or ""))
        elif self.mode == "plain":
            print(text)

    @contextmanager
    def live(self) -> Iterator[Any]:
        if self.mode == "json":
            yield lambda ev: print(json.dumps(ev.to_dict()), flush=True)
            return
        if self.mode == "plain":
            yield _PlainSink(self)
            return
        view = _LiveRound(self)
        with Live(view, console=self.console, refresh_per_second=10, transient=True, vertical_overflow="crop"):
            yield view

    # ------------------------------------------------------------------
    def run_header(self, run: Run) -> str:
        icon, _ = ICON.get(run.status.value, ("?", ""))
        if run.status == RunStatus.SKIPPED:
            return f"{icon} {self.name(run.provider)} — skipped ({run.skip_reason})"
        bits = [f"{icon} {self.name(run.provider)}", run.model_reported or run.model_requested or ""]
        if run.effort:
            bits.append(f"effort {run.effort}")
        bits.append(f"{run.transport}/{run.billing}" if run.transport else "")
        if run.latency_ms is not None:
            bits.append(_fmt_ms(run.latency_ms))
        if run.output_tokens:
            bits.append(f"{run.output_tokens} tok")
        if run.cost_usd is not None:
            bits.append(f"${run.cost_usd:.4f} API")
        if run.attempt_count > 1:
            bits.append(f"attempt {run.n} of {run.attempt_count}")
        if (run.meta or {}).get("truncated"):
            bits.append("TRUNCATED")
        return " · ".join(b for b in bits if b)

    def print_run(self, run: Run) -> None:
        if self.mode == "json":
            return
        if run.status == RunStatus.SKIPPED:
            self.out(self.run_header(run), "dim")
            return
        body = run.output_text or ""
        if run.status == RunStatus.WAITING:
            info = (run.meta or {}).get("handoff") or {}
            body = "Waiting for you. " + handoff_instructions(self.name(run.provider), info) + \
                "\n→ " + HINTS["waiting"].format(p=run.provider)
        elif run.status != RunStatus.COMPLETE:
            body = f"[{run.status.value}] {run.error_kind}: {run.error_message}"
            if run.output_text:
                body += f"\n\n(partial output, NOT added to the lane)\n{run.output_text}"
            hint = hint_for(run.status.value, run.error_kind, run.provider)
            if hint:
                body += "\n\n→ " + hint
        meta = run.meta or {}
        cites = meta.get("citations") or [s.get("url") for s in meta.get("search_results") or [] if isinstance(s, dict)]
        if self.mode == "plain":
            print(f"=== {self.run_header(run)} ===\n{body}\n")
            if cites:
                print("Sources:\n" + "\n".join(f"  [{i}] {u}" for i, u in enumerate(cites, 1)) + "\n")
            if meta.get("searches"):
                print("Searches this model ran:\n" + "\n".join(f"  • {q}" for q in meta["searches"]) + "\n")
            return
        icon, color = ICON.get(run.status.value, ("?", "white"))
        content: Any = Text(body) if (self.raw or run.status != RunStatus.COMPLETE) else Markdown(body)
        if cites:
            content = Group(content, Text("\nSources:\n" + "\n".join(f"[{i}] {u}" for i, u in enumerate(cites, 1)), style="dim"))
        if meta.get("searches"):
            content = Group(content, Text("\nSearches this model ran:\n" + "\n".join(f"• {q}" for q in meta["searches"]), style="dim"))
        self.console.print(Panel(content, title=escape(self.run_header(run)), title_align="left", border_style=color))

    def print_round(self, rnd: Round, runs: list[Run], branch: str | None = None) -> None:
        if self.mode == "json":
            print(json.dumps({"type": "round", **round_to_dict(rnd, runs)}), flush=True)
            return
        self.print_round_header(rnd, branch)
        for run in runs:
            if run.status != RunStatus.SKIPPED:
                self.print_run(run)
        self.print_round_summary(runs)

    def print_round_header(self, rnd: Round, branch: str | None = None) -> None:
        line = f"Round {rnd.seq} · {rnd.kind}" + (f" · branch {branch}" if branch else "") + f" · {rnd.created_at[:19].replace('T', ' ')} UTC"
        if rnd.feedback_packet_id:
            line += f" · feedback packet #{rnd.feedback_packet_id}"
        if rnd.edited_from_round_id:
            line += " · edited prompt"
        if self.mode == "rich":
            self.console.rule(f"[bold]{escape(line)}")
            self.console.print(Text(f"› {rnd.prompt}", style="bold"))
        else:
            print(f"# {line}\n> {rnd.prompt}\n")

    def print_round_summary(self, runs: list[Run]) -> None:
        if self.mode == "json":
            return
        for r in runs:
            icon, color = ICON.get(r.status.value, ("?", "white"))
            if r.status == RunStatus.SKIPPED:
                desc = f"skipped ({r.skip_reason})"
            elif r.status == RunStatus.COMPLETE:
                desc = f"complete · {_fmt_ms(r.latency_ms)}" + (f" · attempt {r.n}/{r.attempt_count}" if r.attempt_count > 1 else "")
            elif r.status == RunStatus.WAITING:
                desc = "waiting for you to paste its answer"
            else:
                desc = f"{r.status.value} · {r.error_kind}: {(r.error_message or '')[:90]}"
            if self.mode == "rich":
                self.console.print(f"[{color}]{icon}[/] {escape(self.name(r.provider)):12} [{color}]{escape(desc)}[/]")
            else:
                print(f"  {icon} {self.name(r.provider):12} {desc}")

    def print_local_compare(self, lc: dict[str, Any]) -> None:
        if self.mode == "json":
            rep = lc["sources"]
            print(json.dumps({"type": "local_compare", "round": lc["round"].seq, "notes": lc["notes"],
                              "sources": rep.per_response, "shared_sources": rep.shared}))
            return
        rnd: Round = lc["round"]
        t = Table(title=f"Round {rnd.seq} · {rnd.kind} — only this round's responses", title_justify="left")
        for c in ("lane", "status", "model", "effort", "via", "latency", "words", "sources", "attempts"):
            t.add_column(c)
        rep = lc["sources"]
        for r in lc["runs"]:
            icon, color = ICON.get(r.status.value, ("?", "white"))
            words = len((r.output_text or "").split()) if r.status == RunStatus.COMPLETE else 0
            t.add_row(self.name(r.provider), f"[{color}]{icon} {r.status.value}[/]", escape(r.model_reported or r.model_requested or "-"),
                      r.effort or "-", f"{r.transport or '-'}", _fmt_ms(r.latency_ms), str(words or "-"),
                      str(len(rep.per_response.get(r.provider, [])) or "-"), str(r.attempt_count or "-"))
        if self.mode == "rich":
            self.console.print(t)
        else:
            print(f"Round {rnd.seq} ({rnd.kind})")
            for r in lc["runs"]:
                print(f"  {self.name(r.provider):12} {r.status.value:12} {r.model_reported or r.model_requested or '-'}")
        for n in lc["notes"]:
            self.out(f"• {n}", "yellow")

    def print_status(self, ctx: Any, overview: list[dict[str, Any]], last: Round | None, api: dict[str, Any]) -> None:
        if self.mode == "json":
            print(json.dumps({"project": ctx.project.name, "chat": ctx.chat.name, "branch": ctx.branch.name,
                              "round": last.seq if last else None, "lanes": overview, "api": api}))
            return
        head = f"Project: {ctx.project.name}   Chat: {ctx.chat.name}   Branch: {ctx.branch.name}   Round: {last.seq if last else 0}"
        t = Table(title=head, title_justify="left", title_style="bold")
        for col in ("", "lane", "model", "effort", "transport", "billing", "web", "turns", "~ctx tok", "last", "allowance"):
            t.add_column(col)
        for row in overview:
            sub = row.get("subscription_status") or {}
            win = ", ".join(f"{k} {round((v.get('utilization') or 0) * 100)}%" for k, v in (sub.get("windows") or {}).items()
                            if v.get("utilization") is not None)
            allowance = (sub.get("status") or "") + (f" ({win})" if win else "") if sub else ("unknown" if row["billing"] == "subscription" else "-")
            t.add_row("[green]\\[x][/]" if row["enabled"] else "[dim]\\[ ][/]",
                      escape(row["name"]) + ("" if row["enabled"] else " [dim](muted)[/]") + ("" if row["verified"] else " [yellow]*[/]"),
                      escape(row["model"]), row["effort"] or "-", row["transport"], row["billing"],
                      "on" if row.get("web") else "off", str(row["turns"]),
                      str(row["context_tokens"]), f"#{row['last_round']}" if row["last_round"] else "-", escape(allowance))
        self.console.print(t)
        if any(not r["verified"] for r in overview):
            self.out("* unverified CLI preset - run `cockpit doctor --live` before relying on it", "yellow")
        self.out(f"API: {'ON' if api['enabled'] else 'off'} · mode {api['mode']} · ${api['spent']:.2f} of "
                 f"${api['limit']:.2f} this month" + (f" · {api['unpriced']} unpriced run(s)" if api.get("unpriced") else ""), "dim")

    def print_table(self, title: str, columns: list[str], rows: list[list[str]]) -> None:
        if self.mode == "json":
            print(json.dumps([dict(zip(columns, r)) for r in rows]))
            return
        if self.mode == "plain":
            print(title)
            for r in rows:
                print("  " + "  ".join(str(x) for x in r))
            return
        t = Table(title=title, title_justify="left")
        for c in columns:
            t.add_column(c)
        for r in rows:
            t.add_row(*[escape(str(x)) for x in r])
        self.console.print(t)

    def print_rounds(self, rounds: list[tuple[Round, list[Run], list[Analysis], str]]) -> None:
        if self.mode == "json":
            print(json.dumps([{**round_to_dict(r, runs), "branch": b} for r, runs, _, b in rounds]))
            return
        rows = []
        for rnd, runs, analyses, branch in rounds:
            marks = " ".join(f"{ICON.get(r.status.value, ('?',))[0]}{r.provider[:3]}" for r in runs)
            prompt = rnd.prompt.replace("\n", " ")
            rows.append([str(rnd.seq), branch, rnd.kind, rnd.created_at[5:16].replace("T", " "),
                         prompt[:44] + ("…" if len(prompt) > 44 else ""), marks,
                         ", ".join(f"#{a.id} {a.kind}" for a in analyses) or "-"])
        self.print_table("Rounds", ["#", "branch", "kind", "when (UTC)", "prompt", "lanes", "analyses"], rows)

    def print_attempts(self, provider: str, rnd: Round, attempts: list[Attempt], current: int | None) -> None:
        if self.mode == "json":
            print(json.dumps([{"n": a.n, "reason": a.reason, "status": a.status.value, "model": a.model_reported or a.model_requested,
                               "current": a.id == current, "output": a.output_text, "error": a.error_message} for a in attempts]))
            return
        for a in attempts:
            mark = "← current" if a.id == current else ""
            title = f"{self.name(provider)} · round {rnd.seq} · attempt {a.n} ({a.reason}) · {a.status.value} {mark}"
            body = a.output_text if a.status == RunStatus.COMPLETE else f"{a.error_kind}: {a.error_message}"
            if self.mode == "rich":
                self.console.print(Panel(Text(body or ""), title=escape(title), title_align="left",
                                         border_style="green" if mark else "dim"))
            else:
                print(f"=== {title} ===\n{body}\n")

    def print_branches(self, branches: list[Branch], current: int, rounds_by_branch: dict[int, int],
                       fork_seq: dict[int, int | None]) -> None:
        rows = []
        for b in branches:
            origin = f"from {next((x.name for x in branches if x.id == b.parent_branch_id), '?')} before round {fork_seq.get(b.id)}" if b.parent_branch_id else "root"
            rows.append(["*" if b.id == current else "", b.name, origin, str(rounds_by_branch.get(b.id, 0))])
        self.print_table("Branches", ["", "branch", "origin", "own rounds"], rows)

    # ------------------------------------------------------------------
    def print_analysis(self, a: Analysis, rnd: Round, *, reveal: bool) -> None:
        if self.mode == "json":
            print(json.dumps({"type": "analysis", "id": a.id, "round": rnd.seq, "kind": a.kind, "judge": a.judge_provider,
                              "model": a.judge_model, "blind": a.blind, "status": a.status,
                              "label_map": a.label_map if (reveal or not a.blind) else None,
                              "result": a.result, "error": a.error_message}))
            return
        names = {lbl: self.name(v["provider"]) for lbl, v in a.label_map.items()}
        header = (f"{a.kind.capitalize()} #{a.id} · round {rnd.seq} · judge {self.name(a.judge_provider)} "
                  f"({a.judge_model}) · {'blind' if a.blind else 'attributed'}")
        if self.mode == "rich":
            self.console.rule(f"[bold]{escape(header)}")
        else:
            print(f"## {header}")
        if a.status == "error":
            self.out(f"judge failed: {a.error_message}", "red")
            return
        if a.result is None:
            self.out("judge output was not valid JSON; raw output:", "yellow")
            self.out(a.raw_output or "")
            return
        res = a.result
        if a.kind == "synthesis":
            out = res.get("synthesis", "")
            self.console.print(Markdown(out)) if self.mode == "rich" and not self.raw else print(out)
            return
        for risk in res.get("_consensus_risks", []):
            self.out(f"⚠ {risk}", "bold yellow")
        if res.get("_missing_challenge"):
            self.out("⚠ The judge omitted the mandatory challenge section - treat this analysis as incomplete.", "bold red")
        for title, body in render_analysis_sections(res):
            if self.mode == "rich":
                self.console.print(Text(title, style="bold cyan" if title.startswith("CHALLENGE") else "bold"))
                self.console.print(Text(textwrap.indent(body, "  ")))
            else:
                print(f"{title}\n{textwrap.indent(body, '  ')}\n")
        if a.blind and not reveal:
            self.out(f"Labels {', '.join(names)} are blind. --reveal shows which lane is which.", "dim")
        else:
            self.out("  ".join(f"{lbl} = {n}" for lbl, n in names.items()), "dim")

    def print_packet(self, pk: FeedbackPacket, src: Round) -> None:
        if self.mode == "json":
            print(json.dumps({"type": "feedback_packet", "id": pk.id, "round": src.seq, "status": pk.status,
                              "disclosure": pk.disclosure, "edited": pk.edited, "content": pk.content,
                              "instruction": pk.instruction}))
            return
        title = (f"Feedback packet #{pk.id} · from round {src.seq} · {pk.disclosure} · {pk.status}"
                 + (" · edited" if pk.edited else ""))
        if self.mode == "rich":
            self.console.print(Panel(Text(pk.content), title=escape(title), title_align="left", border_style="cyan"))
        else:
            print(f"=== {title} ===\n{pk.content}\n")
        if pk.instruction:
            self.out(f"Instruction: {pk.instruction}", "bold")

    def print_thread(self, pid: str, messages: list[Any]) -> None:
        if self.mode == "json":
            print(json.dumps([{"role": m.role, "content": m.content} for m in messages]))
            return
        if not messages:
            self.out(f"{self.name(pid)} has no context on this branch yet.")
            return
        for i, m in enumerate(messages):
            who = "you" if m.role == "user" else self.name(pid)
            if self.mode == "rich":
                self.console.print(Panel(Text(m.content) if m.role == "user" or self.raw else Markdown(m.content),
                                         title=f"{i // 2 + 1}. {escape(who)}", title_align="left",
                                         border_style="blue" if m.role == "user" else "green"))
            else:
                print(f"--- {i // 2 + 1}. {who} ---\n{m.content}\n")


def _refs(item: dict[str, Any]) -> str:
    refs = item.get("responses") or ([item["response"]] if item.get("response") else [])
    return f"  [{', '.join(map(str, refs))}]" if refs else ""


def _ev(e: dict[str, Any] | None) -> str:
    if not e or not e.get("responses"):
        return ""
    return f"  ({e['responses']} response(s), {e['independent_sources']} independent source line(s))"


def render_analysis_sections(res: dict[str, Any]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []

    def add(title: str, lines: list[str]) -> None:
        lines = [ln for ln in lines if ln and ln.strip()]
        if lines:
            out.append((title, "\n".join(lines)))

    add("SUMMARY", [res.get("summary", "")])
    add("AGREEMENTS", [f"• {a.get('point')}{_refs(a)}{_ev(a.get('_evidence'))}"
                       + (f"\n    why: {a['why']}" if a.get("why") else "")
                       + (f"\n    depends on: {a['depends_on']}" if a.get("depends_on") else "")
                       for a in res.get("agreements", []) if isinstance(a, dict)])
    dl: list[str] = []
    for d in res.get("disagreements", []):
        if not isinstance(d, dict):
            continue
        dl.append(f"• {d.get('topic')}  <{d.get('type', 'unclassified')}>" + (f" significance: {d['significance']}" if d.get("significance") else ""))
        for s in d.get("sides", []):
            if isinstance(s, dict):
                dl.append(f"    - {s.get('position')}{_refs(s)}{_ev(s.get('_evidence'))}")
                for k in ("reasoning", "evidence", "assumptions"):
                    if s.get(k):
                        dl.append(f"        {k}: {s[k]}")
    add("DISAGREEMENTS", dl)
    add("SHARED LOGIC", [f"• {x}" for x in res.get("shared_logic", [])])
    add("SAME CONCLUSION, DIFFERENT REASONING", [f"• {x}" for x in res.get("different_paths_same_conclusion", [])])
    add("ASSUMPTIONS DRIVING DIVERGENCE", [f"• {x}" for x in res.get("assumptions_driving_divergence", [])])
    add("FACTUAL CONFLICTS", [f"• {f.get('claim')}{_refs(f)}" + (f" — {f['note']}" if f.get("note") else "")
                              for f in res.get("factual_conflicts", []) if isinstance(f, dict)])
    add("UNSUPPORTED CLAIMS", [f"• {u.get('claim')}{_refs(u)}" + (f" — {u['why']}" if u.get("why") else "")
                               for u in res.get("unsupported_claims", []) if isinstance(u, dict)])
    add("LIKELY ERRORS", [f"• {e.get('claim')}{_refs(e)} — {e.get('reason', '')} ({e.get('confidence', '?')})"
                          for e in res.get("likely_errors", []) if isinstance(e, dict)])
    add("UNIQUE INSIGHTS", [f"• {u.get('insight')}{_refs(u)}" for u in res.get("unique_insights", []) if isinstance(u, dict)])
    add("OMISSIONS", [f"• {x}" for x in res.get("omissions", [])])
    add("NEEDS VERIFICATION", [f"• {v.get('question')}" + (f" — {v['why']}" if v.get("why") else "")
                               for v in res.get("verification_needed", []) if isinstance(v, dict)])
    ch = res.get("challenge") or {}
    add("CHALLENGE (mandatory)", [
        f"Current consensus: {ch.get('current_consensus')}" if ch.get("current_consensus") else "",
        f"Strongest case against: {ch.get('strongest_case_against')}" if ch.get("strongest_case_against") else "",
        f"What would falsify it: {ch.get('what_would_falsify')}" if ch.get("what_would_falsify") else "",
        *[f"Untested assumption: {u}" for u in ch.get("untested_assumptions", [])]])
    add("CHALLENGE NEXT", [f"• {x}" for x in res.get("next_challenges", [])])
    add("BEST CURRENT SYNTHESIS", [res.get("best_current_synthesis", "")])
    return out


def round_to_dict(rnd: Round, runs: list[Run]) -> dict[str, Any]:
    return {
        "round": rnd.seq, "kind": rnd.kind, "prompt": rnd.prompt, "branch_id": rnd.branch_id,
        "created_at": rnd.created_at, "completed_at": rnd.completed_at,
        "feedback_packet_id": rnd.feedback_packet_id, "edited_from_round_id": rnd.edited_from_round_id,
        "runs": [
            {"provider": r.provider, "status": r.status.value, "skip_reason": r.skip_reason,
             "attempt": r.n, "attempts": r.attempt_count, "transport": r.transport, "billing": r.billing,
             "model_requested": r.model_requested, "model_reported": r.model_reported, "effort": r.effort,
             "client_version": r.client_version, "native_id": r.native_id, "latency_ms": r.latency_ms,
             "first_token_ms": r.first_token_ms, "input_tokens": r.input_tokens, "output_tokens": r.output_tokens,
             "cost_usd": r.cost_usd, "error_kind": r.error_kind, "error_message": r.error_message,
             "input_text": r.input_text, "output_text": r.output_text, "raw_output": r.raw_output, "meta": r.meta}
            for r in runs
        ],
    }
