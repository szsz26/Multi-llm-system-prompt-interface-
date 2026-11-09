"""Interactive cockpit: a thin loop over the same CLI handlers and engine.

Plain text is sent as a follow-up into each active lane's own context (the
first prompt of a chat is a blast). Slash commands map 1:1 onto CLI commands.
"""

from __future__ import annotations

import argparse
import shlex
from typing import Any

from rich.text import Text

HELP = """\
Type a prompt to send it to every active lane (each keeps its own context).
  /new <prompt>               fresh lane context (blast)
  /only claude,gemini <p>     one-round subset (not persisted)     /except grok <p>
  /lane claude <p>            one lane only
  /paste perplexity           attach the answer you copied from Perplexity (one-click helper lane)
  /mute grok   /unmute grok [--fresh]   /toggle gemini            persistent per chat
  /stop gemini                (from another terminal while a round runs: `cockpit stop gemini`)
  /retry [lane]   /rerun claude   /attempts claude
  /compare [--full] [--analyze]   /adjudicate [--judge openai] [--reveal]   /synth
  /iterate [instruction]      draft feedback -> review/edit -> send
  /feedback list|show N|edit N|send N
  /edit <round> <new prompt>  rerun from an earlier prompt on a new branch (old branch kept)
  /branches   /branch use <name>   /rounds [--all]   /show [--round N]   /thread claude
  /model claude <id>   /effort openai high   /status   /api   /doctor
  /project use <name>  /chat use <name>  /chats  /projects  /export --dir out   /help  /quit
Ctrl-C interrupts the running round (nothing partial is added to any lane)."""

SHORTCUTS = {"new": "blast", "synth": "synthesize", "q": "quit", "exit": "quit", "pause": "mute", "resume": "unmute",
             "session": "use"}
NO_CHAT = {"doctor", "providers", "lanes", "projects", "project", "chats", "init", "api", "use", "chat"}


def _header(app: Any) -> Text:
    ctx = app.ctx()
    lin = app.engine.lineage(ctx)
    t = Text()
    t.append(f"{ctx.project.name} / {ctx.chat.name}", style="bold")
    t.append(f"   branch {ctx.branch.name}   round {lin[-1].seq if lin else 0}\n")
    for row in app.engine.overview(ctx):
        mark, style = ("[x]", "green") if row["enabled"] else ("[ ]", "dim")
        t.append(f"{mark} {row['name']}  ", style=style)
    return t


def to_argv(line: str) -> list[str] | None:
    """Translate one shell line to CLI argv. None means quit."""
    if not line.startswith("/"):
        return ["followup", "--", line]
    head, _, rest = line[1:].partition(" ")
    cmd = SHORTCUTS.get(head, head)
    if cmd == "quit":
        return None
    if cmd in ("only", "except"):
        provs, _, prompt = rest.strip().partition(" ")
        return ["followup", "--only" if cmd == "only" else "--exclude", provs, "--", prompt]
    if cmd == "blast":
        return ["blast", "--", rest]
    if cmd == "lane":
        provider, _, prompt = rest.strip().partition(" ")
        return ["lane", "prompt", provider, "--", prompt]
    if cmd == "paste":
        return ["lane", "paste", *shlex.split(rest)]
    if cmd == "edit":
        seq, _, prompt = rest.strip().partition(" ")
        return ["edit", seq, "--", prompt]
    return [cmd, *shlex.split(rest)]


def run_shell(app: Any, parser: argparse.ArgumentParser) -> int:
    from .cli import dispatch

    try:
        import readline

        hist = app.config.home / "shell_history"
        try:
            readline.read_history_file(hist)
        except OSError:
            pass
    except ImportError:
        readline, hist = None, None  # type: ignore[assignment]

    app.interactive = True
    con = app.renderer.console
    con.print("[bold]LLM Cockpit[/]" + ("  [yellow](demo mode: fake lanes)[/]" if app.config.demo else "") + "   /help")
    app.remember()
    while True:
        con.print(_header(app))
        try:
            line = input("› ").strip()
        except (EOFError, KeyboardInterrupt):
            con.print()
            break
        if not line:
            continue
        if line in ("/help", "/?", "help"):
            con.print(HELP, markup=False)
            continue
        try:
            argv = to_argv(line)
        except ValueError as e:
            con.print(f"error: {e}", markup=False)
            continue
        if argv is None:
            break
        if argv[0] == "followup" and not line.startswith("/") and not app.engine.lineage(app.ctx()):
            argv[0] = "blast"  # first prompt of a chat
        try:
            args = parser.parse_args(argv)
        except SystemExit:
            continue
        if getattr(args, "func", None) is None:
            continue
        dispatch(app, args)
        # project/chat switches made inside the shell become the shell's context
        app.project_override = app.store.get_meta("current_project") or app.project_override
        app.chat_override = app.store.get_meta("current_chat") or app.chat_override
    if readline is not None and hist is not None:
        try:
            readline.write_history_file(hist)
        except OSError:
            pass
    return 0
