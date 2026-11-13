"""After-round flow: you choose what each AI gets next; pasted text stays one prompt; readable autosave."""

import os
import sys

import pytest

from cockpit.domain import AnalysisKind, CockpitError, RoundKind
from cockpit.feedback import ANTI_CONVERGENCE

from .conftest import secret


def _compared(h):
    h.send("Q", fresh=True)
    return h.run(h.engine.analyze(h.refresh(), AnalysisKind.ADJUDICATE, judge="claude"))


def test_each_ai_gets_what_you_chose_in_one_round(harness):
    h = harness(providers=["claude", "openai", "grok"])
    a = _compared(h)
    share = {"claude": "none", "openai": "review", "grok": "answers"}
    res = h.run(h.engine.send_next(h.refresh(), "Now assume no bank account.", share, analysis_id=a.id, sink=h.sink))
    assert res.round.kind == RoundKind.FEEDBACK.value and {r.provider for r in res.runs if r.status.value == "complete"} == set(share)
    got = {p: h.calls(p)[-1].messages[-1].content for p in share}
    assert got["claude"] == "Now assume no bank account."
    assert ANTI_CONVERGENCE in got["openai"] and got["openai"].endswith("Now assume no bank account.")
    assert not any(secret(p) in got["openai"] for p in share)  # comparison only, no full answers
    assert all(secret(p) in got["grok"] for p in share)  # every full answer, anonymized
    assert not any(n in got["grok"] for n in ("Claude", "OpenAI", "ChatGPT", "Grok"))
    assert len({r.round_id for r in res.runs}) == 1  # one round: the next comparison compares like with like


def test_next_round_never_runs_without_a_message_or_feedback(harness):
    h = harness(providers=["claude", "openai"])
    a = _compared(h)
    with pytest.raises(CockpitError, match="nothing to send"):
        h.run(h.engine.send_next(h.refresh(), "", {"claude": "none", "openai": "none"}, analysis_id=a.id))
    res = h.run(h.engine.send_next(h.refresh(), None, {"claude": "review", "openai": "none"}, analysis_id=a.id))
    skipped = {r.provider: r.skip_reason for r in res.runs if r.status.value == "skipped"}
    assert "nothing to send" in skipped["openai"] and "Give your revised answer in full." in h.calls("claude")[-1].messages[-1].content


def test_lane_that_did_not_answer_only_gets_your_message(harness):
    from cockpit.engine import Selection

    h = harness(providers=["claude", "openai", "grok"])
    h.send("Q", fresh=True, selection=Selection(only=["claude", "openai"]))
    a = h.run(h.engine.analyze(h.refresh(), AnalysisKind.ADJUDICATE, judge="claude"))
    h.run(h.engine.send_next(h.refresh(), "go on", {p: "review" for p in ("claude", "openai", "grok")}, analysis_id=a.id))
    assert h.calls("grok")[-1].messages[-1].content == "go on"


def test_pasted_lines_stay_one_prompt(monkeypatch):
    from cockpit.cli import read_block

    r, w = os.pipe()
    os.write(w, b"first line\nsecond line\nthird line\n")
    os.close(w)
    with os.fdopen(r) as fake_stdin:
        monkeypatch.setattr(sys, "stdin", fake_stdin)
        assert read_block("") == "first line\nsecond line\nthird line"


def test_export_shows_what_each_ai_was_sent(harness, tmp_path):
    from types import SimpleNamespace

    from cockpit.export import export_dir

    h = harness(providers=["claude", "openai"])
    a = _compared(h)
    h.run(h.engine.send_next(h.refresh(), "follow", {"claude": "review", "openai": "none"}, analysis_id=a.id))
    app = SimpleNamespace(store=h.store, renderer=SimpleNamespace(name=lambda p: p.capitalize()))
    base = export_dir(app, h.refresh(), tmp_path)
    md = (base / "chat.md").read_text()
    assert "What Claude was sent" in md and "What Openai was sent" not in md


def test_autosave_falls_back_when_documents_is_blocked(tmp_path, monkeypatch):
    import argparse
    import asyncio

    import cockpit.export as export
    from cockpit.cli import autosave, make_app

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    app = make_app(argparse.Namespace(home=str(tmp_path / "data"), demo=True, mode="plain"))
    app.config.demo = False  # demo mode skips saving; we only want its fake lanes
    app.interactive = True
    blocked = tmp_path / "Documents" / "LLM Cockpit"
    app.config.autosave_dir = str(blocked)
    real = export.export_dir

    def refuse_documents(a, c, root):
        if root == blocked:  # what macOS does when Terminal isn't allowed into Documents
            raise PermissionError(1, "Operation not permitted")
        return real(a, c, root)

    monkeypatch.setattr(export, "export_dir", refuse_documents)
    ctx = app.ctx()
    asyncio.run(app.engine.send(ctx, "first question", fresh=True))
    target = autosave(app, ctx)
    assert target == tmp_path / "home" / "LLM Cockpit" / "main.md"
    assert "first question" in target.read_text()
    assert (target.parent / "default" / "chats" / "main" / "chat.md").exists()
    assert not blocked.exists()
