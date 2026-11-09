"""Export a chat as Markdown, JSON, or a directory tree (no lock-in; future Obsidian input)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .domain import RunStatus
from .render import render_analysis_sections, round_to_dict


def _chat_data(app: Any, ctx: Any) -> dict[str, Any]:
    st = app.store
    branches = st.list_branches(ctx.chat.id)
    rounds = st.list_rounds(ctx.chat.id)
    return {
        "project": ctx.project.name, "chat": ctx.chat.name, "created_at": ctx.chat.created_at,
        "branches": [{"id": b.id, "name": b.name, "parent_branch_id": b.parent_branch_id,
                      "fork_before_round": st.get_round_by_id(b.fork_round_id).seq if b.fork_round_id else None,
                      "rounds": [r.seq for r in st.lineage(b.id)]} for b in branches],
        "rounds": [{**round_to_dict(r, st.runs_for_round(r.id)),
                    "attempts": {run.provider: [{"n": a.n, "reason": a.reason, "status": a.status.value,
                                                 "model": a.model_reported or a.model_requested, "output": a.raw_output,
                                                 "error": a.error_message} for a in st.attempts_for_run(run.id)]
                                 for run in st.runs_for_round(r.id) if run.status != RunStatus.SKIPPED}}
                   for r in rounds],
        "analyses": [{"id": a.id, "round": r.seq, "kind": a.kind, "judge": a.judge_provider, "model": a.judge_model,
                      "blind": a.blind, "label_map": a.label_map, "status": a.status, "result": a.result}
                     for r in rounds for a in st.analyses_for_round(r.id)],
        "feedback_packets": [{"id": p.id, "round": st.get_round_by_id(p.round_id).seq, "disclosure": p.disclosure,
                              "status": p.status, "edited": p.edited, "content": p.content, "instruction": p.instruction}
                             for p in st.list_packets(ctx.chat.id)],
    }


def _round_md(app: Any, r: Any) -> str:
    out = [f"## Round {r.seq} · {r.kind} · {r.created_at[:19]}\n\n> {r.prompt}\n"]
    for run in app.store.runs_for_round(r.id):
        head = f"### {app.renderer.name(run.provider)} — {run.status.value}"
        if run.status == RunStatus.SKIPPED:
            out.append(f"{head} ({run.skip_reason})\n")
            continue
        out.append(f"{head} · {run.model_reported or run.model_requested} · {run.transport}/{run.billing}\n")
        out.append((run.raw_output or f"_{run.error_kind}: {run.error_message}_") + "\n")
    return "\n".join(out)


def _analysis_md(a: Any) -> str:
    body = "\n\n".join(f"**{t}**\n\n{b}" for t, b in render_analysis_sections(a.result or {})) if a.kind != "synthesis" \
        else (a.result or {}).get("synthesis", "")
    return f"## {a.kind} #{a.id} (judge {a.judge_provider}, {'blind' if a.blind else 'attributed'})\n\n{body}\n"


def export_text(app: Any, ctx: Any, fmt: str) -> str:
    if fmt == "json":
        return json.dumps(_chat_data(app, ctx), indent=2)
    out = [f"# {ctx.project.name} / {ctx.chat.name}\n"]
    for r in app.store.list_rounds(ctx.chat.id):
        out.append(_round_md(app, r))
        out += [_analysis_md(a) for a in app.store.analyses_for_round(r.id) if a.result]
    return "\n".join(out)


def export_dir(app: Any, ctx: Any, root: Path) -> Path:
    from .cli import write_private

    base = root / ctx.project.name / "chats" / ctx.chat.name
    data = _chat_data(app, ctx)
    write_private(base / "metadata.json", json.dumps(data, indent=2))
    write_private(base / "chat.md", export_text(app, ctx, "md"))
    for r in app.store.list_rounds(ctx.chat.id):
        write_private(base / "rounds" / f"{r.seq:03d}.md", _round_md(app, r))
        for a in app.store.analyses_for_round(r.id):
            folder = "syntheses" if a.kind == "synthesis" else "comparisons"
            write_private(base / folder / f"{a.id:04d}-round{r.seq:03d}-{a.kind}.md", _analysis_md(a))
    for p in app.store.list_packets(ctx.chat.id):
        write_private(base / "feedback" / f"{p.id:04d}-{p.status}.md", p.content + (f"\n\nINSTRUCTION: {p.instruction}" if p.instruction else ""))
    for b in data["branches"]:
        write_private(base / "branches" / f"{b['name']}.md",
                      f"# branch {b['name']}\n\nrounds: {', '.join(map(str, b['rounds']))}\n"
                      + (f"forked before round {b['fork_before_round']}\n" if b["fork_before_round"] else ""))
    write_private(root / ctx.project.name / "project.md", f"# {ctx.project.name}\n")
    return base
