"""Command-line interface. Every command is scriptable; the interactive shell
dispatches into these same handlers, which all drive the same engine.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import __version__, billing, log
from .config import CONFIG_TEMPLATE, CockpitConfig, load_config
from .domain import AnalysisKind, CockpitError, Disclosure, RunStatus
from .engine import Cockpit, Ctx, RoundResult, Selection
from .render import Renderer, round_to_dict
from .store import Store

DEFAULT_PROJECT, DEFAULT_CHAT = "default", "main"


@dataclass
class App:
    config: CockpitConfig
    store: Store
    engine: Cockpit
    renderer: Renderer
    project_override: str | None = None
    chat_override: str | None = None
    interactive: bool = False
    autosave_root: str | None = None  # where copies actually got saved (falls back if Documents is blocked)
    guide: bool = True  # shell: after each round, compare and ask what each AI gets next
    reader_on: bool = True  # shell: read answers on the reader page in the browser (/reader off = in the terminal)
    reader_page: str | None = None  # the reader page for the current chat, once written
    reader_shown: bool = False  # the browser was opened on it this session

    @property
    def project_name(self) -> str:
        return self.project_override or self.store.get_meta("current_project") or DEFAULT_PROJECT

    @property
    def chat_name(self) -> str:
        if self.chat_override:
            return self.chat_override
        if self.project_override and self.project_override != self.store.get_meta("current_project"):
            return DEFAULT_CHAT
        return self.store.get_meta("current_chat") or DEFAULT_CHAT

    def ctx(self, create: bool = True) -> Ctx:
        return self.engine.ctx(self.project_name, self.chat_name, create=create)

    def remember(self) -> None:
        if self.project_override or self.chat_override:
            self.store.set_meta("current_project", self.project_name)
            self.store.set_meta("current_chat", self.chat_name)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------


def read_prompt(args: argparse.Namespace, app: App, required: bool = True) -> str | None:
    if getattr(args, "file", None):
        return Path(args.file).expanduser().read_text()
    words = getattr(args, "prompt", None) or []
    if words == ["-"] or (not words and required and not app.interactive and not sys.stdin.isatty()):
        return sys.stdin.read()
    text = " ".join(words).strip()
    if not text and required:
        raise CockpitError("no prompt given (argument, -f FILE, or stdin)")
    return text or None


def read_block(prompt: str) -> str:
    """input() that keeps a multi-line paste together. Without this, every pasted line
    would be read as its own command - one pasted question became several rounds."""
    lines = [input(prompt)]
    try:
        import select

        while select.select([sys.stdin], [], [], 0.08)[0]:
            lines.append(input())
    except (ImportError, OSError, ValueError, EOFError):
        pass
    return "\n".join(lines)


def drain_input() -> None:
    """Throw away anything typed or pasted while a round was running, so it can't start new rounds."""
    try:
        import termios

        termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except Exception:  # noqa: BLE001 - not a terminal, or not POSIX
        pass


def saved_root(app: App) -> Path | None:
    folder = app.config.autosave_dir
    if not folder:
        return None
    if app.config.demo and not app.autosave_root:
        return app.config.home / "saved"  # demo chats never go into your Documents
    return Path(app.autosave_root or folder).expanduser()


def open_page(path: Path) -> bool:
    """Show a saved page in the default browser. False when there is no browser to open (or under tests)."""
    if os.environ.get("PYTEST_CURRENT_TEST") or os.environ.get("COCKPIT_NO_BROWSER"):
        return False
    from .providers.manual import ManualIO

    return ManualIO().open_url(path.as_uri())


def reading(app: App) -> bool:
    """True when answers are meant to be read on the reader page rather than in the terminal."""
    return bool(app.reader_on and app.config.reader and app.interactive and app.renderer.mode == "rich"
                and app.config.autosave_dir)


def _reader_page(app: App, ctx: Ctx, root: Path, name: str) -> Path | None:
    if not app.config.reader:
        return None
    from .reader import build_reader

    try:
        return build_reader(app, ctx, root, name)
    except OSError:
        raise
    except Exception as e:  # noqa: BLE001 - a display problem must never break a round
        log.event("reader_failed", error=f"{type(e).__name__}: {e}")
        app.renderer.out(f"(could not build the reader page: {type(e).__name__}: {e})", "yellow")
        return None


def autosave(app: App, ctx: Ctx, quiet: bool = False, force: bool = False) -> Path | None:
    """Keep a readable copy of the chat (answers, comparisons, what each AI was sent) in a normal folder:
    <folder>/<topic>.md and <topic>.html (the reader page), plus <folder>/<project>/chats/<topic>/ with every detail."""
    root = saved_root(app)
    if root is None or (not force and (app.renderer.mode == "json" or not (app.interactive or sys.stdout.isatty()))):
        return None
    from .export import export_dir, export_text

    ctx = app.engine.refresh(ctx)
    name = ctx.chat.name if ctx.project.name == DEFAULT_PROJECT else f"{ctx.project.name} - {ctx.chat.name}"
    name = re.sub(r'[\\/:*?"<>|]+', "-", name).strip() or "chat"
    for attempt in (root, Path("~/LLM Cockpit").expanduser()):
        try:
            export_dir(app, ctx, attempt)
            target = attempt / f"{name}.md"
            write_private(target, export_text(app, ctx, "md"))
            page = _reader_page(app, ctx, attempt, name)
        except OSError as e:  # e.g. macOS hasn't allowed Terminal into Documents
            if attempt == root:
                app.renderer.out(f"(can't save into {root}: {e.strerror or e}; saving in your home folder instead)", "yellow")
            continue
        app.autosave_root = str(attempt)
        app.reader_page = str(page) if page else None
        if page and reading(app) and not app.reader_shown:
            app.reader_shown = open_page(page)
        if not quiet:
            if page and app.reader_shown:
                app.renderer.out(f"Reader page (in your browser, updates itself): {page}", "dim")
            else:
                app.renderer.out(f"Saved: {page or target}   (type /saved to open the folder)", "dim")
        return target
    app.renderer.out("(could not save a copy of this chat anywhere - check your disk)", "red")
    return None


def selection(args: argparse.Namespace, app: App) -> Selection:
    return Selection(only=app.config.resolve_many(getattr(args, "only", None)),
                     exclude=app.config.resolve_many(getattr(args, "exclude", None)))


def run_live(app: App, factory: Callable[[Any], Any]) -> Any:
    with app.renderer.live() as sink:
        return asyncio.run(factory(sink))


def show_result(app: App, ctx: Ctx, rr: RoundResult) -> int:
    r = app.renderer
    compact = False
    if r.mode == "rich":
        if reading(app):  # write the reader page first; the terminal then only needs a short recap
            autosave(app, ctx, quiet=True)
            compact = app.reader_shown
        if compact:
            r.print_round_header(rr.round, ctx.branch.name)
            r.print_round_summary(rr.runs)
            r.out("Read the answers in your browser (the page updates itself). /read opens it again.", "bold")
        else:
            r.print_round(rr.round, rr.runs, ctx.branch.name)
    elif r.mode == "plain":
        r.print_round_summary(rr.runs)
    else:
        print(json.dumps({"type": "round_result", **round_to_dict(rr.round, rr.runs)}), flush=True)
    if rr.waiting:
        collect_manual(app, ctx, rr)
    lc = app.engine.local_compare(app.engine.refresh(ctx), rr.round.seq)  # lightweight comparison, every round
    if r.mode == "json":
        r.print_local_compare(lc)
    else:
        for n in lc["notes"]:
            r.out(f"• {n}", "yellow")
    autosave(app, ctx, quiet=compact and not rr.waiting)
    return rr.exit_code


def collect_manual(app: App, ctx: Ctx, rr: RoundResult) -> None:
    """One-click helper lanes: after the automatic lanes finish, pick up the answer you copied."""
    from .providers.manual import ManualIO

    r = app.renderer
    interactive = app.interactive or (sys.stdin.isatty() and sys.stdout.isatty())
    for run in rr.waiting:
        name = r.name(run.provider)
        if not interactive or r.mode == "json":
            r.out(f"✋ {name} is waiting for you: copy its answer, then run `cockpit lane paste {run.provider}`", "magenta")
            continue
        while True:
            typed = read_block(f"✋ {name}: copy its full answer, then press Enter here (or type s to paste later): ").strip()
            if typed.lower() in ("s", "skip", "later"):
                r.out(f"later: `cockpit lane paste {run.provider}`", "dim")
                break
            # pasted the answer right here instead of just pressing Enter: use what was pasted
            text = typed if len(typed) > 40 else ManualIO().paste()
            if text is None:
                r.out(f"no clipboard on this machine - use `cockpit lane paste {run.provider} --file answer.md`", "yellow")
                break
            if not text.strip() or text.strip() == (run.input_text or "").strip():
                r.out("your clipboard still holds the question, not the answer - copy the answer and try again", "yellow")
                continue
            done = app.engine.submit_manual(app.engine.refresh(ctx), run.provider, text, round_seq=rr.round.seq)
            r.print_run(done)
            break


def write_private(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)


def confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False
    return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")


def open_in_editor(app: App, text: str, name: str) -> str:
    path = app.config.home / "tmp" / name
    write_private(path, text)
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "vi"
    subprocess.run([*shlex.split(editor), str(path)], check=False)  # the user's own editor setting
    try:
        return path.read_text()
    finally:
        path.unlink(missing_ok=True)


# ----------------------------------------------------------------------
# setup / navigation
# ----------------------------------------------------------------------


def cmd_init(app: App, args: argparse.Namespace) -> int:
    path = app.config.config_path
    if path.exists() and not args.force:
        print(f"{path} already exists (use --force to overwrite)")
        return 1
    write_private(path, CONFIG_TEMPLATE)
    print(f"wrote {path}\nnext: sign in to each CLI, then run `cockpit doctor`")
    return 0


def cmd_doctor(app: App, args: argparse.Namespace) -> int:
    from .doctor import gather_reports, print_doctor

    pids = app.config.resolve_many(args.only) or app.config.provider_ids
    reports = asyncio.run(gather_reports(app.engine, pids, args.live))
    if app.renderer.mode == "json":
        print(json.dumps([r.__dict__ | {"ok": r.ok} for r in reports]))
        return 0 if all(r.ok for r in reports) else 1
    return 0 if print_doctor(app.renderer, app.config, app.store, reports, args.live) else 1


def cmd_providers(app: App, args: argparse.Namespace) -> int:
    rows = [[p.id, p.transport, p.billing.value, p.model, p.api_model or p.model,
             "on" if (p.web_search and "web_search" in app.engine.adapter(p.id).capabilities) else "off",
             "yes" if p.verified else "NO", ", ".join(p.aliases) or "-"] for p in app.config.providers.values()]
    app.renderer.print_table("Lanes", ["lane", "transport", "billing", "model", "api model", "web", "verified", "aliases"], rows)
    return 0


def cmd_projects(app: App, args: argparse.Namespace) -> int:
    cur = app.store.get_meta("current_project") or DEFAULT_PROJECT
    rows = [["*" if p["name"] == cur else "", p["name"], str(p["chats"])] for p in app.store.list_projects()]
    app.renderer.print_table("Projects", ["", "project", "chats"], rows)
    return 0


def cmd_project(app: App, args: argparse.Namespace) -> int:
    if args.action in ("create", "use"):
        ctx = app.engine.ctx(args.name, DEFAULT_CHAT)
        if args.action == "use" or not app.store.get_meta("current_project"):
            app.store.set_meta("current_project", ctx.project.name)
            app.store.set_meta("current_chat", DEFAULT_CHAT)
        print(f"project {ctx.project.name} ({'current' if args.action == 'use' else 'created'})")
        return 0
    p = app.store.get_project(args.name)
    if not p:
        raise CockpitError(f"no project {args.name!r}")
    if not (args.yes or confirm(f"Permanently delete project {p.name!r} and ALL its chats?")):
        raise CockpitError("not deleted (pass --yes)")
    app.store.delete_project(p.id)
    if app.store.get_meta("current_project") == p.name:
        app.store.set_meta("current_project", None)
        app.store.set_meta("current_chat", None)
    print(f"deleted project {p.name}")
    return 0


def cmd_chats(app: App, args: argparse.Namespace) -> int:
    p = app.store.get_or_create_project(app.project_name)
    cur = app.store.get_meta("current_chat")
    rows = [["*" if c["name"] == cur and app.project_name == (app.store.get_meta("current_project") or DEFAULT_PROJECT) else "",
             c["name"], str(c["rounds"]), str(c["branches"]), (c["last_activity"] or "-")[:16].replace("T", " ")]
            for c in app.store.list_chats(p.id)]
    app.renderer.print_table(f"Chats in project {p.name}", ["", "chat", "rounds", "branches", "last activity"], rows)
    return 0


def cmd_chat(app: App, args: argparse.Namespace) -> int:
    if args.action in ("create", "use"):
        ctx = app.engine.ctx(app.project_name, args.name)
        app.store.set_meta("current_project", ctx.project.name)
        app.store.set_meta("current_chat", ctx.chat.name)
        print(f"chat {ctx.chat.name} in project {ctx.project.name} (current; lanes start fresh in a new chat)")
        return 0
    ctx = app.engine.ctx(app.project_name, args.name, create=False)
    if not (args.yes or confirm(f"Permanently delete chat {ctx.chat.name!r} and its transcripts?")):
        raise CockpitError("not deleted (pass --yes)")
    app.store.delete_chat(ctx.chat.id)
    if app.store.get_meta("current_chat") == ctx.chat.name:
        app.store.set_meta("current_chat", None)
    print(f"deleted chat {ctx.chat.name} (securely zeroed in the database file)")
    return 0


def cmd_branches(app: App, args: argparse.Namespace) -> int:
    ctx = app.ctx()
    branches = app.store.list_branches(ctx.chat.id)
    own = {b.id: sum(1 for r in app.store.list_rounds(ctx.chat.id) if r.branch_id == b.id) for b in branches}
    fork = {b.id: (app.store.get_round_by_id(b.fork_round_id).seq if b.fork_round_id else None) for b in branches}
    app.renderer.print_branches(branches, ctx.branch.id, own, fork)
    return 0


def cmd_branch(app: App, args: argparse.Namespace) -> int:
    ctx = app.engine.switch_branch(app.ctx(), args.name)
    print(f"current branch: {ctx.branch.name}")
    return 0


# ----------------------------------------------------------------------
# rounds
# ----------------------------------------------------------------------


def _send(app: App, args: argparse.Namespace, fresh: bool, sel: Selection | None = None) -> int:
    prompt = read_prompt(args, app)
    assert prompt is not None
    sel = sel or selection(args, app)
    app.remember()
    ctx = app.ctx()
    rr = run_live(app, lambda sink: app.engine.send(ctx, prompt, fresh=fresh, selection=sel, allow_api=args.allow_api,
                                                    force=args.force, first=args.first, sink=sink))
    return show_result(app, ctx, rr)


def cmd_blast(app: App, args: argparse.Namespace) -> int:
    return _send(app, args, fresh=True)


def cmd_followup(app: App, args: argparse.Namespace) -> int:
    return _send(app, args, fresh=False)


def cmd_lane(app: App, args: argparse.Namespace) -> int:
    pid = app.config.resolve_provider(args.provider)
    if args.action == "paste":
        from .providers.manual import ManualIO

        if args.file:
            text = Path(args.file).expanduser().read_text()
        elif args.prompt == ["-"] or (not sys.stdin.isatty() and not app.interactive):
            text = sys.stdin.read()
        else:
            text = ManualIO().paste()
            if text is None:
                raise CockpitError("no clipboard on this machine - use --file answer.md or pipe the answer on stdin")
        run = app.engine.submit_manual(app.ctx(), pid, text, round_seq=args.round)
        app.renderer.print_run(run)
        return 0
    return _send(app, args, fresh=args.fresh, sel=Selection(only=[pid]))


def cmd_edit(app: App, args: argparse.Namespace) -> int:
    prompt = read_prompt(args, app)
    assert prompt is not None
    ctx = app.ctx()
    holder: dict[str, Any] = {}

    async def go(sink: Any) -> RoundResult:
        new_ctx, rr = await app.engine.edit(ctx, args.round, prompt, selection=selection(args, app), context=args.context,
                                            allow_api=args.allow_api, first=args.first, sink=sink)
        holder["ctx"] = new_ctx
        return rr

    rr = run_live(app, go)
    return show_result(app, holder["ctx"], rr)


def cmd_retry(app: App, args: argparse.Namespace) -> int:
    ctx = app.ctx()
    rr = run_live(app, lambda sink: app.engine.retry(ctx, args.providers or None, round_seq=args.round,
                                                     via_api=args.via_api, allow_api=args.allow_api, sink=sink))
    return show_result(app, ctx, rr)


def cmd_rerun(app: App, args: argparse.Namespace) -> int:
    ctx = app.ctx()
    rr = run_live(app, lambda sink: app.engine.rerun(ctx, args.providers, round_seq=args.round,
                                                     via_api=args.via_api, allow_api=args.allow_api, sink=sink))
    return show_result(app, ctx, rr)


def cmd_attempts(app: App, args: argparse.Namespace) -> int:
    ctx = app.ctx()
    rnd = app.engine.round(ctx, args.round)
    pid = app.config.resolve_provider(args.provider)
    run = app.store.run_for(rnd.id, pid)
    if not run or run.status == RunStatus.SKIPPED:
        raise CockpitError(f"{pid} did not run in round {rnd.seq}")
    app.renderer.print_attempts(pid, rnd, app.store.attempts_for_run(run.id), run.current_attempt_id)
    return 0


def cmd_stop(app: App, args: argparse.Namespace) -> int:
    ctx = app.ctx()
    stopped = [p for p in app.config.resolve_many(args.providers) if app.engine.request_stop(ctx, p)]
    if not stopped:
        print("nothing running for those lanes in this chat")
        return 1
    print(f"stop requested: {', '.join(stopped)} (other lanes keep running)")
    return 0


def _toggle(app: App, args: argparse.Namespace, enabled: bool | None) -> int:
    ctx = app.ctx()
    pids = app.config.resolve_many(args.providers)
    if not pids:
        raise CockpitError("name at least one lane")
    reentry = "fresh" if getattr(args, "fresh", False) else "continue"
    for pid, on in app.engine.set_enabled(ctx, pids, enabled, reentry=reentry).items():
        how = " — re-enters with fresh context" if on and reentry == "fresh" else (" — continues from where it stopped" if on else "")
        print(f"{app.renderer.name(pid)}: {'active' if on else 'muted'} in chat {ctx.chat.name}{how} (history kept)")
    return 0


def cmd_mute(app: App, args: argparse.Namespace) -> int:
    return _toggle(app, args, False)


def cmd_unmute(app: App, args: argparse.Namespace) -> int:
    return _toggle(app, args, True)


def cmd_toggle(app: App, args: argparse.Namespace) -> int:
    return _toggle(app, args, None)


def cmd_model(app: App, args: argparse.Namespace) -> int:
    ctx = app.ctx()
    pid = app.config.resolve_provider(args.provider)
    if args.reset or args.model:
        app.engine.set_model(ctx, pid, None if args.reset else args.model)
    print(f"{app.renderer.name(pid)} model in chat {ctx.chat.name}: {app.engine.model_for(ctx.chat, pid)}")
    return 0


def cmd_effort(app: App, args: argparse.Namespace) -> int:
    ctx = app.ctx()
    pid = app.config.resolve_provider(args.provider)
    if args.reset or args.level:
        for w in app.engine.set_effort(ctx, pid, None if args.reset else args.level):
            app.renderer.err.print(f"[yellow]note:[/] {w}")
    print(f"{app.renderer.name(pid)} effort in chat {ctx.chat.name}: {app.engine.effort_for(ctx.chat, pid) or 'default'}")
    return 0


# ----------------------------------------------------------------------
# views
# ----------------------------------------------------------------------


def api_summary(app: App) -> dict[str, Any]:
    pol = billing.policy(app.config, app.store)
    return {"enabled": pol.enabled, "mode": pol.mode, "limit": pol.monthly_limit_usd, "spent": app.store.api_spend(),
            "unpriced": app.store.api_unpriced_runs()}


def cmd_status(app: App, args: argparse.Namespace) -> int:
    ctx = app.ctx()
    lin = app.engine.lineage(ctx)
    app.renderer.print_status(ctx, app.engine.overview(ctx), lin[-1] if lin else None, api_summary(app))
    return 0


def cmd_rounds(app: App, args: argparse.Namespace) -> int:
    ctx = app.ctx()
    names = {b.id: b.name for b in app.store.list_branches(ctx.chat.id)}
    rounds = app.store.list_rounds(ctx.chat.id) if args.all else app.engine.lineage(ctx)
    app.renderer.print_rounds([(r, app.store.runs_for_round(r.id), app.store.analyses_for_round(r.id), names.get(r.branch_id, "?"))
                               for r in rounds])
    return 0


def cmd_show(app: App, args: argparse.Namespace) -> int:
    ctx = app.ctx()
    rnd = app.engine.round(ctx, args.round)
    runs = app.store.runs_for_round(rnd.id)
    if args.provider:
        pid = app.config.resolve_provider(args.provider)
        runs = [r for r in runs if r.provider == pid]
    if args.input and app.renderer.mode != "json":
        for r in runs:
            if r.input_text:
                app.renderer.out(f"--- exact input sent to {app.renderer.name(r.provider)} ---", "bold")
                app.renderer.out(r.input_text)
    names = {b.id: b.name for b in app.store.list_branches(ctx.chat.id)}
    app.renderer.print_round(rnd, runs, names.get(rnd.branch_id))
    return 0


def cmd_thread(app: App, args: argparse.Namespace) -> int:
    ctx = app.ctx()
    pid = app.config.resolve_provider(args.provider)
    head = app.store.get_head(ctx.branch.id, pid).head_message_id
    app.renderer.print_thread(pid, app.store.history(ctx.chat.id, pid, head))
    return 0


def _analysis(app: App, args: argparse.Namespace, kind: AnalysisKind) -> int:
    ctx = app.ctx()
    a = run_live(app, lambda sink: app.engine.analyze(ctx, kind, judge=args.judge, round_seq=args.round,
                                                      blind=not args.attributed, allow_api=args.allow_api, sink=sink))
    app.renderer.print_analysis(a, app.store.get_round_by_id(a.round_id), reveal=getattr(args, "reveal", False))
    return 0 if a.status == "complete" else 1


def cmd_compare(app: App, args: argparse.Namespace) -> int:
    ctx = app.ctx()
    lc = app.engine.local_compare(ctx, args.round)
    app.renderer.print_local_compare(lc)
    if args.full and app.renderer.mode != "json":
        for run in lc["runs"]:
            if run.status != RunStatus.SKIPPED:
                app.renderer.print_run(run)
    if args.analyze:
        args.round = lc["round"].seq
        return _analysis(app, args, AnalysisKind.COMPARE)
    if app.renderer.mode != "json":
        app.renderer.out("--full prints every answer; --analyze asks a judge for agreements/disagreements/logic/challenge.", "dim")
    return 0


def cmd_adjudicate(app: App, args: argparse.Namespace) -> int:
    return _analysis(app, args, AnalysisKind.ADJUDICATE)


def cmd_synthesize(app: App, args: argparse.Namespace) -> int:
    return _analysis(app, args, AnalysisKind.SYNTHESIS)


def cmd_show_analysis(app: App, args: argparse.Namespace) -> int:
    ctx = app.ctx()
    a = app.store.get_analysis(args.id)
    rnd = app.store.get_round_by_id(a.round_id)
    if rnd.chat_id != ctx.chat.id:
        raise CockpitError(f"analysis {args.id} is not in chat {ctx.chat.name}")
    if args.prompt:
        print(a.prompt_text)
    else:
        app.renderer.print_analysis(a, rnd, reveal=args.reveal)
    return 0


# ----------------------------------------------------------------------
# feedback / iterate
# ----------------------------------------------------------------------


def _draft(app: App, ctx: Ctx, args: argparse.Namespace, instruction: str | None) -> Any:
    include = [x for s in (args.include or []) for x in s.split(",")]
    return run_live(app, lambda sink: app.engine.draft_feedback(
        ctx, round_seq=args.round, analysis_id=args.analysis, judge=args.judge,
        disclosure=Disclosure(args.disclosure), include=include, instruction=instruction,
        blind=not args.attributed, allow_api=args.allow_api, sink=sink))


def _send_packet(app: App, ctx: Ctx, pid: int, args: argparse.Namespace, instruction: str | None) -> int:
    rr = run_live(app, lambda sink: app.engine.send_feedback(ctx, pid, instruction=instruction, selection=selection(args, app),
                                                             allow_api=args.allow_api, sink=sink))
    return show_result(app, ctx, rr)


def cmd_feedback(app: App, args: argparse.Namespace) -> int:
    ctx = app.ctx()
    r = app.renderer
    if args.action == "draft":
        pk = _draft(app, ctx, args, args.instruction)
        r.print_packet(pk, app.store.get_round_by_id(pk.round_id))
        r.out(f"edit: cockpit feedback edit {pk.id}    send: cockpit feedback send {pk.id} [--only ...]", "dim")
        return 0
    if args.action == "list":
        rows = [[str(p.id), str(app.store.get_round_by_id(p.round_id).seq), p.disclosure, p.status, "yes" if p.edited else "",
                 str(app.store.get_round_by_id(p.sent_round_id).seq) if p.sent_round_id else "-"]
                for p in app.store.list_packets(ctx.chat.id)]
        r.print_table("Feedback packets", ["#", "from round", "disclosure", "status", "edited", "sent as round"], rows)
        return 0
    pk = app.engine.packet(ctx, args.id)
    if args.action == "show":
        r.print_packet(pk, app.store.get_round_by_id(pk.round_id))
        return 0
    if args.action == "edit":
        content = Path(args.file).expanduser().read_text() if args.file else (
            open_in_editor(app, pk.content, f"feedback-{pk.id}.md") if sys.stdin.isatty() else None)
        if content is None and args.instruction is None:
            raise CockpitError("nothing to change: pass --file FILE or --instruction, or run in a terminal to open $EDITOR")
        pk = app.engine.edit_feedback(ctx, pk.id, content=content, instruction=args.instruction)
        r.print_packet(pk, app.store.get_round_by_id(pk.round_id))
        return 0
    if args.action == "discard":
        app.store.update_packet(pk.id, status="discarded")
        print(f"discarded packet {pk.id}")
        return 0
    # send
    return _send_packet(app, ctx, pk.id, args, args.instruction)


def cmd_iterate(app: App, args: argparse.Namespace) -> int:
    """Draft -> review/edit -> confirm -> send. Never sends without the user's review."""
    ctx = app.ctx()
    instruction = read_prompt(args, app, required=False)
    pk = _draft(app, ctx, args, instruction)
    r = app.renderer
    r.print_packet(pk, app.store.get_round_by_id(pk.round_id))
    if not (sys.stdin.isatty() and sys.stdout.isatty()) and not app.interactive:
        r.out(f"Review it, then: cockpit feedback edit {pk.id} (optional) && cockpit feedback send {pk.id}", "bold")
        return 0
    while True:
        choice = input(f"[s]end packet #{pk.id} / [e]dit / [i]nstruction / [c]ancel (kept as draft)? ").strip().lower()
        if choice.startswith("e"):
            pk = app.engine.edit_feedback(ctx, pk.id, content=open_in_editor(app, pk.content, f"feedback-{pk.id}.md"))
            r.print_packet(pk, app.store.get_round_by_id(pk.round_id))
        elif choice.startswith("i"):
            pk = app.engine.edit_feedback(ctx, pk.id, instruction=input("instruction: ").strip() or None)
        elif choice.startswith("s"):
            return _send_packet(app, ctx, pk.id, args, pk.instruction)
        else:
            r.out(f"kept draft #{pk.id}; send later with `cockpit feedback send {pk.id}`")
            return 0


SHARE_CHOICES = {"1": "none", "2": "review", "3": "answers"}
SHARE_TEXT = {"none": "just your message", "review": "your message plus the comparison",
              "answers": "your message plus the comparison plus every AI's full answer"}
NEXT_MENU = """\
What should each AI get with your next message?
  1  just your message
  2  your message plus the comparison  (press Enter for this one)
  3  your message plus the comparison plus every AI's full answer
  4  choose separately for each AI
  x  go back to asking a fresh question
(No AI ever sees which AI wrote what.)"""


def read_message(prompt: str) -> str:
    """Several lines allowed: type or paste, then press Enter on an empty line to send."""
    lines: list[str] = []
    while True:
        chunk = read_block(prompt if not lines else "  ")
        if not chunk.strip():
            if lines:
                return "\n".join(lines).strip()
            return ""
        lines.append(chunk)


def _gist(text: str | None, width: int = 110) -> str:
    for line in (text or "").splitlines():
        line = re.sub(r"[#*_>`]+", "", line).strip()
        if line:
            return line if len(line) <= width else line[: width - 1] + "..."
    return ""


def clean_screen(app: App, ctx: Ctx, rnd: Any, analysis: Any) -> None:
    """A clean screen to type into: a short recap, where it's saved, and the options."""
    r = app.renderer
    if r.mode == "rich":
        r.console.clear()
    where = "the full answers and comparison are on the reader page" if reading(app) and app.reader_shown \
        else "scroll up for the full answers and comparison"
    r.out(f"{ctx.chat.name} - round {rnd.seq} done  ({where})", "bold")
    for run in app.store.runs_for_round(rnd.id):
        if run.status == RunStatus.SKIPPED:
            continue
        mark = "✓" if run.status == RunStatus.COMPLETE else "✕"
        r.out(f"  {mark} {r.name(run.provider)}: {_gist(run.output_text) or run.status.value}")
    if analysis is not None and analysis.result and analysis.result.get("summary"):
        r.out(f"  Comparison: {_gist(str(analysis.result['summary']), 140)}", "cyan")
    if app.reader_page:
        r.out(f"  Reader page: {app.reader_page}   (/read opens it)", "dim")
    elif root := saved_root(app):
        r.out(f"  Saved in: {root}   (/saved opens it)", "dim")
    r.out("")
    r.out(NEXT_MENU)


def next_step(app: App) -> None:
    """After a round: compare the answers, then YOU decide what each AI gets next. Nothing is sent
    until you type your message and confirm, so rounds never run on by themselves."""
    r = app.renderer
    while True:
        ctx = app.ctx()
        lin = app.engine.lineage(ctx)
        if not lin:
            return
        rnd = lin[-1]
        if len(app.engine.completed_runs(rnd)) < 2:
            r.out("(Fewer than 2 answers this round, so there's nothing to compare. Type your next question below.)", "dim")
            return
        done_before = [a for a in app.store.analyses_for_round(rnd.id) if a.result and a.kind != "synthesis"]
        analysis = done_before[-1] if done_before else None
        if analysis is None:
            r.out(f"Comparing the answers (judge: {r.name(app.config.judge)}, blind - Ctrl-C to skip)...", "dim")
            try:
                analysis = run_live(app, lambda sink: app.engine.analyze(ctx, AnalysisKind.ADJUDICATE, round_seq=rnd.seq,
                                                                         sink=sink))
                if reading(app) and app.reader_shown and analysis.result:
                    r.out(f"Comparison: {_gist(str(analysis.result.get('summary', '')), 200) or 'done'}", "cyan")
                    r.out("The full comparison is the 'At a glance' box on the reader page.", "dim")
                else:
                    r.print_analysis(analysis, rnd, reveal=True)
            except CockpitError as e:
                r.out(f"no comparison this round: {e}", "yellow")
            except KeyboardInterrupt:
                drain_input()
                r.out("comparison skipped", "yellow")
            autosave(app, ctx, quiet=True)
            if analysis is not None and not analysis.result:
                analysis = None
            try:
                where = "in your browser" if reading(app) and app.reader_shown else "above"
                if read_block(f"\nRead the answers {where}. Press Enter for a clean screen to type your next message "
                              "(or x for a fresh question): ").strip().lower().startswith("x"):
                    return
            except (EOFError, KeyboardInterrupt):
                return
        clean_screen(app, ctx, rnd, analysis)
        lanes = [t.provider for t in app.engine.plan_targets(ctx.chat, Selection()) if t.included]
        try:
            choice = read_block("choose 1, 2, 3, 4 or x: ").strip().lower() or "2"
        except (EOFError, KeyboardInterrupt):
            return
        if choice.startswith(("x", "q", "/")):
            r.out("OK - type a fresh question below (or /help).", "dim")
            return
        if choice == "4":
            share = {}
            for pid in lanes:
                c = read_block(f"  {r.name(pid)}: 1 just your message / 2 plus comparison / 3 plus comparison and answers "
                               "[Enter = 2]: ").strip()
                share[pid] = SHARE_CHOICES.get(c or "2", "review")
        elif choice in SHARE_CHOICES:
            share = {pid: SHARE_CHOICES[choice] for pid in lanes}
        else:
            r.out("type 1, 2, 3, 4 or x", "yellow")
            continue
        if analysis is None and any(v != "none" for v in share.values()):
            r.out("there's no comparison to share this round, so each AI gets just your message", "yellow")
            share = {pid: "none" for pid in lanes}
        r.out("\nYour message - type or paste it. Press Enter on an empty line when you're done.\n"
              "(Leave it empty and press Enter to just ask them to revise their answers.)", "bold")
        try:
            note = read_message("› ")
        except (EOFError, KeyboardInterrupt):
            return
        for pid in lanes:
            r.out(f"  {r.name(pid)} gets: {SHARE_TEXT[share.get(pid, 'none')]}")
        if read_block("Send this round? [Y/n] ").strip().lower().startswith("n"):
            continue
        try:
            rr = run_live(app, lambda sink: app.engine.send_next(
                ctx, note or None, share, analysis_id=analysis.id if analysis else None, sink=sink))
        except CockpitError as e:
            r.out(f"error: {e}", "red")
            continue
        except KeyboardInterrupt:
            drain_input()
            r.out("interrupted (nothing partial was added to any AI)", "yellow")
            return
        show_result(app, ctx, rr)


# ----------------------------------------------------------------------
# API guard, export, shell
# ----------------------------------------------------------------------


def cmd_api(app: App, args: argparse.Namespace) -> int:
    if args.action == "enable":
        pol = billing.policy(app.config, app.store)
        limit = args.limit if args.limit is not None else pol.monthly_limit_usd
        if limit <= 0:
            raise CockpitError("set a hard monthly limit: `cockpit api enable --limit 20`")
        billing.set_policy(app.store, enabled=True, limit=limit, mode="until-disabled" if args.until_disabled else "per-run")
    elif args.action == "disable":
        billing.set_policy(app.store, enabled=False, mode="per-run")
    s = api_summary(app)
    if app.renderer.mode == "json":
        print(json.dumps(s))
    else:
        print(f"API use: {'ON' if s['enabled'] else 'OFF'} · mode {s['mode']} · limit ${s['limit']:.2f}/month · "
              f"spent ${s['spent']:.2f} this month" + (f" · {s['unpriced']} run(s) with unknown cost" if s["unpriced"] else ""))
        if s["enabled"] and s["mode"] == "per-run":
            print("each paid API run still needs --allow-api (or --via api on retry/rerun)")
    return 0


def cmd_export(app: App, args: argparse.Namespace) -> int:
    from .export import export_dir, export_text

    ctx = app.ctx(create=False)
    if args.dir:
        root = export_dir(app, ctx, Path(args.dir).expanduser())
        print(f"exported to {root} (files mode 600)")
        return 0
    text = export_text(app, ctx, args.format)
    if args.output:
        write_private(Path(args.output).expanduser(), text)
        print(f"exported chat {ctx.chat.name} to {args.output} (mode 600)")
    else:
        print(text)
    return 0


def cmd_read(app: App, args: argparse.Namespace) -> int:
    """Write the reader page for this chat and show it in the browser."""
    if not app.config.autosave_dir:
        raise CockpitError("saving is turned off (autosave_dir in config.toml), so there is no reader page")
    if not app.config.reader:
        raise CockpitError("the reader page is turned off (reader = false in config.toml)")
    autosave(app, app.ctx(create=False), quiet=True, force=True)
    if not app.reader_page:
        raise CockpitError("could not write the reader page")
    page = Path(app.reader_page)
    opened = False if getattr(args, "no_open", False) else open_page(page)
    app.reader_shown = app.reader_shown or opened
    if app.renderer.mode == "json":
        print(json.dumps({"reader_page": str(page), "opened": opened}))
    else:
        print(f"Reader page: {page}" + ("" if opened else "   (open this file in your browser)"))
    return 0


def cmd_shell(app: App, args: argparse.Namespace) -> int:
    from .shell import run_shell

    return run_shell(app, build_parser())


# ----------------------------------------------------------------------
# parser
# ----------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    S = argparse.SUPPRESS
    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("global options")
    g.add_argument("-P", "--project", default=S, help="project (default: current)")
    g.add_argument("-c", "--chat", "-s", "--session", dest="chat", default=S, help="chat (default: current)")
    g.add_argument("--demo", action="store_true", default=S, help="fake lanes and a separate demo database")
    g.add_argument("--version", action="version", version=f"cockpit {__version__}")
    g.add_argument("--home", default=S, help="directory for config + data (or COCKPIT_HOME)")
    g.add_argument("--config", default=S, help="path to config.toml")
    g.add_argument("--plain", dest="mode", action="store_const", const="plain", default=S)
    g.add_argument("--json", dest="mode", action="store_const", const="json", default=S)
    g.add_argument("--raw", action="store_true", default=S, help="raw text instead of Markdown")
    g.add_argument("--no-color", action="store_true", default=S)

    sel = argparse.ArgumentParser(add_help=False)
    sel.add_argument("--only", action="append", metavar="L[,L]", help="this round only: just these lanes (includes muted ones)")
    sel.add_argument("--exclude", action="append", metavar="L[,L]", help="this round only: leave these lanes out")

    spend = argparse.ArgumentParser(add_help=False)
    spend.add_argument("--allow-api", action="store_true", help="approve paid API use for this run (within the monthly limit)")

    runopts = argparse.ArgumentParser(add_help=False)
    runopts.add_argument("--force", action="store_true", help="run even if an identical run already exists")
    runopts.add_argument("--first", type=int, metavar="N", help="stop the remaining lanes once N have answered")

    prompt = argparse.ArgumentParser(add_help=False)
    prompt.add_argument("prompt", nargs="*", help="prompt text ('-' or omit to read stdin)")
    prompt.add_argument("-f", "--file", help="read the prompt from a file")

    judge = argparse.ArgumentParser(add_help=False)
    judge.add_argument("--judge", help="lane to use as judge/analyzer (default from config)")
    judge.add_argument("--round", type=int, help="round number (default: latest on this branch)")
    judge.add_argument("--attributed", action="store_true", help="show lane names to the judge (default: blind)")

    fb = argparse.ArgumentParser(add_help=False)
    fb.add_argument("--analysis", type=int, help="build from this stored analysis")
    fb.add_argument("--disclosure", choices=[d.value for d in Disclosure], default="anonymized")
    fb.add_argument("--include", action="append", metavar="A,C|lane", help="full responses to include (selected)")

    p = argparse.ArgumentParser(prog="cockpit", parents=[common],
                                description="One prompt, many independent model lanes: compare, challenge, iterate, branch.")
    sub = p.add_subparsers(dest="command", metavar="<command>")

    def add(name: str, fn: Callable[[App, argparse.Namespace], int], help: str, parents: list[Any] = [],  # noqa: B006
            aliases: list[str] = []) -> argparse.ArgumentParser:  # noqa: B006
        sp = sub.add_parser(name, help=help, parents=[common, *parents], aliases=aliases, description=help)
        sp.set_defaults(func=fn)
        return sp

    add("shell", cmd_shell, "interactive cockpit (also: `cockpit` with no command)")
    add("init", cmd_init, "write a starter config.toml").add_argument("--force", action="store_true")
    sp = add("doctor", cmd_doctor, "check clients, sign-in, billing path and models")
    sp.add_argument("--live", action="store_true", help="send a tiny real request through each lane")
    sp.add_argument("--only", action="append")
    add("providers", cmd_providers, "list lanes, transports and billing", aliases=["lanes"])

    add("projects", cmd_projects, "list projects")
    sp = add("project", cmd_project, "create / use / delete a project")
    sp.add_argument("action", choices=("create", "use", "delete"))
    sp.add_argument("name")
    sp.add_argument("--yes", action="store_true")
    add("chats", cmd_chats, "list chats in the current project")
    sp = add("chat", cmd_chat, "create / use / delete a chat (fresh lanes per chat)")
    sp.add_argument("action", choices=("create", "use", "delete"))
    sp.add_argument("name")
    sp.add_argument("--yes", action="store_true")
    sp = add("use", cmd_chat, "switch to a chat (creates it)")
    sp.add_argument("name")
    sp.set_defaults(action="use")
    add("branches", cmd_branches, "list branches of the current chat")
    sp = add("branch", cmd_branch, "switch branch: `cockpit branch use <name>`")
    sp.add_argument("action", choices=("use",))
    sp.add_argument("name")

    add("blast", cmd_blast, "send to selected lanes with FRESH lane context", [prompt, sel, spend, runopts], aliases=["new"])
    add("followup", cmd_followup, "send into each selected lane's OWN context", [prompt, sel, spend, runopts], aliases=["ask", "f"])
    sp = add("lane", cmd_lane, "one lane: `lane prompt claude \"...\"` · `lane paste perplexity` (attach your copied answer)",
             [spend, runopts])
    sp.add_argument("action", choices=("prompt", "paste"))
    sp.add_argument("--round", type=int, help="(paste) round to attach the answer to (default: the one waiting)")
    sp.add_argument("provider")
    sp.add_argument("prompt", nargs="*")
    sp.add_argument("-f", "--file")
    sp.add_argument("--fresh", action="store_true", help="start this lane fresh")
    sp = add("edit", cmd_edit, "edit an earlier prompt and rerun from there on a NEW branch (old branch kept)",
             [sel, spend])
    sp.add_argument("round", type=int)
    sp.add_argument("prompt", nargs="*", help="the edited prompt ('-' or omit to read stdin)")
    sp.add_argument("-f", "--file")
    sp.add_argument("--context", choices=("continue", "fresh"), default="continue",
                    help="continue = reconstructed lane context before that round; fresh = start lanes clean")
    sp.add_argument("--first", type=int)

    for name, fn, hlp in (("mute", cmd_mute, "mute lanes in this chat (history kept)"),
                          ("unmute", cmd_unmute, "unmute lanes (continue where they stopped, or --fresh)"),
                          ("toggle", cmd_toggle, "flip lanes between muted/active")):
        sp = add(name, fn, hlp, aliases={"mute": ["pause"], "unmute": ["resume"]}.get(name, []))
        sp.add_argument("providers", nargs="+")
        if name != "mute":
            sp.add_argument("--fresh", action="store_true", help="re-enter with a clean context instead of continuing")
    sp = add("stop", cmd_stop, "stop a running lane (works from another terminal); others keep going")
    sp.add_argument("providers", nargs="+")
    for name, fn, hlp in (("retry", cmd_retry, "re-run failed lanes of a round (new attempt)"),
                          ("rerun", cmd_rerun, "re-run lanes even after success; all attempts are kept")):
        sp = add(name, fn, hlp, [spend])
        sp.add_argument("providers", nargs="*" if name == "retry" else "+")
        sp.add_argument("--round", type=int)
        sp.add_argument("--via-api", action="store_true", help="use the provider's paid API for this attempt (needs API enabled)")
    sp = add("attempts", cmd_attempts, "show every attempt of a lane in a round")
    sp.add_argument("provider")
    sp.add_argument("--round", type=int)

    add("status", cmd_status, "project, chat, branch, lanes, allowance and API budget")
    sp = add("rounds", cmd_rounds, "rounds on the current branch")
    sp.add_argument("--all", action="store_true", help="every branch")
    sp = add("show", cmd_show, "a round's responses")
    sp.add_argument("--round", type=int)
    sp.add_argument("--provider")
    sp.add_argument("--input", action="store_true", help="also print the exact input each lane received")
    sp = add("thread", cmd_thread, "one lane's own context on this branch")
    sp.add_argument("provider")

    sp = add("compare", cmd_compare, "free local comparison of ONE round; --analyze for a judge comparison", [judge, spend])
    sp.add_argument("--full", action="store_true")
    sp.add_argument("--analyze", action="store_true")
    sp.add_argument("--reveal", action="store_true")
    sp = add("adjudicate", cmd_adjudicate, "stateless blind judge: conflicts, errors, verification, challenge", [judge, spend])
    sp.add_argument("--reveal", action="store_true")
    add("synthesize", cmd_synthesize, "explicitly requested final answer from a round", [judge, spend], aliases=["synth"])
    sp = add("show-analysis", cmd_show_analysis, "show a stored analysis")
    sp.add_argument("id", type=int)
    sp.add_argument("--reveal", action="store_true")
    sp.add_argument("--prompt", action="store_true", help="print the exact packet the judge received")

    sp = add("feedback", cmd_feedback, "feedback packets: draft | show | edit | send | list | discard", [judge, fb, sel, spend])
    sp.add_argument("action", choices=("draft", "show", "edit", "send", "list", "discard"))
    sp.add_argument("id", type=int, nargs="?")
    sp.add_argument("--file", help="(edit) replace the packet text with this file")
    sp.add_argument("--instruction", help="your next instruction, appended after the packet")
    add("iterate", cmd_iterate, "draft feedback from the latest round, review/edit it, then send", [prompt, judge, fb, sel, spend])

    sp = add("model", cmd_model, "show or set a lane's model in this chat")
    sp.add_argument("provider")
    sp.add_argument("model", nargs="?")
    sp.add_argument("--reset", action="store_true")
    sp = add("effort", cmd_effort, "show or set a lane's reasoning effort in this chat")
    sp.add_argument("provider")
    sp.add_argument("level", nargs="?")
    sp.add_argument("--reset", action="store_true")
    sp = add("api", cmd_api, "paid API guard: status | enable --limit USD [--until-disabled] | disable")
    sp.add_argument("action", choices=("status", "enable", "disable"), nargs="?", default="status")
    sp.add_argument("--limit", type=float)
    sp.add_argument("--until-disabled", action="store_true")
    sp = add("export", cmd_export, "export the chat (Markdown / JSON / directory tree)")
    sp.add_argument("--format", choices=("md", "json"), default="md")
    sp.add_argument("-o", "--output")
    sp.add_argument("--dir", help="write a project/chat directory tree here")
    sp = add("read", cmd_read, "open the reader page: this chat formatted for reading in your browser")
    sp.add_argument("--no-open", action="store_true", help="write the page but do not open the browser")
    sp = add("delete-chat", cmd_chat, "permanently delete a chat", aliases=["delete-session"])
    sp.add_argument("name")
    sp.add_argument("--yes", action="store_true")
    sp.set_defaults(action="delete")
    return p


def make_app(args: argparse.Namespace) -> App:
    home = Path(args.home).expanduser() if getattr(args, "home", None) else None
    demo = bool(getattr(args, "demo", False) or os.environ.get("COCKPIT_DEMO"))
    config = load_config(Path(args.config).expanduser() if getattr(args, "config", None) else None, demo=demo, home=home)
    log.configure(config.home)
    store = Store(config.db_path)
    renderer = Renderer(config, mode=getattr(args, "mode", "auto"), raw=getattr(args, "raw", False),
                        no_color=getattr(args, "no_color", False) or bool(os.environ.get("NO_COLOR")))
    return App(config, store, Cockpit(config, store), renderer,
               project_override=getattr(args, "project", None), chat_override=getattr(args, "chat", None))


def dispatch(app: App, args: argparse.Namespace) -> int:
    for k in ("allow_api", "force", "first", "via_api"):
        if not hasattr(args, k):
            setattr(args, k, None if k == "first" else False)
    if args.func is cmd_feedback and args.action not in ("draft", "list") and args.id is None:
        app.renderer.err.print("[red]error:[/] feedback " + args.action + " needs a packet id")
        return 1
    try:
        return int(args.func(app, args) or 0)
    except CockpitError as e:
        app.renderer.err.print(f"[red]error:[/] {e}")
        return 1
    except KeyboardInterrupt:
        app.renderer.err.print("[yellow]interrupted[/] (running lanes marked stopped; nothing partial was added to any lane)")
        return 130


def main(argv: list[str] | None = None) -> int:
    os.umask(0o077)  # transcripts, exports and logs are private by default
    args = build_parser().parse_args(argv)
    if not getattr(args, "command", None):
        args.func = cmd_shell
    try:
        app = make_app(args)
    except CockpitError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    try:
        return dispatch(app, args)
    finally:
        app.store.close()


if __name__ == "__main__":
    sys.exit(main())
