"""Perplexity one-click helper lane: you run it in your own browser; Cockpit hands off and collects."""

import pytest

from cockpit.config import load_config
from cockpit.domain import AnalysisKind, CockpitError, EventType, RunStatus
from cockpit.engine import Selection

from .conftest import FAKE_IO, secret


def pplx(res):
    return next(r for r in res.runs if r.provider == "perplexity")


def test_defaults_gemini_off_perplexity_helper(tmp_path):
    cfg = load_config(home=tmp_path)
    assert cfg.providers["gemini"].enabled_by_default is False
    p = cfg.providers["perplexity"]
    assert p.transport == "manual" and p.billing.value == "subscription" and p.api_model == "sonar-pro"


def test_handoff_copies_exact_prompt_and_opens_browser(harness):
    h = harness(providers=["claude", "perplexity"], manual_lanes=["perplexity"])
    res = h.send("Best on-ramps for Bolivia?", fresh=True)
    r = pplx(res)
    assert r.status == RunStatus.WAITING and h.run_of(res, "claude").status == RunStatus.COMPLETE
    assert FAKE_IO.copied == ["Best on-ramps for Bolivia?"]
    assert FAKE_IO.opened == ["https://www.perplexity.ai/search?q=Best%20on-ramps%20for%20Bolivia%3F"]
    assert r.meta["handoff"]["mode"] == "new_thread" and r.billing == "subscription"
    assert res.exit_code == 0  # waiting on you is not a failure
    assert any(e.type == EventType.RUN_WAITING for e in h.events)


def test_pasted_answer_lands_in_right_round_and_lane_only(harness):
    h = harness(providers=["claude", "perplexity"], manual_lanes=["perplexity"])
    h.send("Q1", fresh=True)
    run = h.engine.submit_manual(h.refresh(), "perplexity", "Perplexity says X [1] https://example.com/x")
    assert run.status == RunStatus.COMPLETE and run.output_text.startswith("Perplexity says X")
    assert h.store.history(h.ctx.chat.id, "claude", h.store.get_head(h.ctx.branch.id, "claude").head_message_id)[1].content.startswith(secret("claude"))
    h.send("Q2")
    assert "Perplexity says X" not in h.context_of("claude")  # no leakage into other lanes
    # follow-up: web conversation in sync -> only the new question is copied (paste into the same thread)
    assert FAKE_IO.copied[-1] == "Q2"
    r2 = h.store.run_for(h.engine.round(h.refresh()).id, "perplexity")
    assert r2.attempt.meta["handoff"]["mode"] == "continue_thread"
    hist = h.store.history(h.ctx.chat.id, "perplexity", r2.context_head_id)
    assert [m.content for m in hist] == ["Q1", "Perplexity says X [1] https://example.com/x"]


def test_pasted_answer_joins_comparison_and_judging(harness):
    h = harness(providers=["claude", "openai", "perplexity"], manual_lanes=["perplexity"])
    h.send("Q", fresh=True)
    with pytest.raises(CockpitError, match="can't run as an automatic judge"):
        h.run(h.engine.analyze(h.refresh(), AnalysisKind.COMPARE, judge="perplexity"))
    a = h.run(h.engine.analyze(h.ctx, AnalysisKind.COMPARE, judge="claude"))
    assert "perplexity" not in {v["provider"] for v in a.label_map.values()}  # waiting answer is never compared
    h.engine.submit_manual(h.ctx, "perplexity", "Answer with source https://example.com/a")
    a = h.run(h.engine.analyze(h.refresh(), AnalysisKind.COMPARE, judge="claude"))
    assert {v["provider"] for v in a.label_map.values()} == {"claude", "openai", "perplexity"}


def test_lane_waits_until_answered_then_continues(harness):
    h = harness(providers=["claude", "perplexity"], manual_lanes=["perplexity"])
    h.send("Q1", fresh=True)
    r = pplx(h.send("Q2"))
    assert r.status == RunStatus.SKIPPED and "waiting for your answer to round 1" in r.skip_reason
    h.engine.submit_manual(h.refresh(), "perplexity", "late answer")
    assert pplx(h.send("Q3")).status == RunStatus.WAITING


def test_paste_specific_round_and_errors(harness):
    h = harness(providers=["claude", "perplexity"], manual_lanes=["perplexity"])
    h.send("Q1", fresh=True)
    with pytest.raises(CockpitError, match="empty"):
        h.engine.submit_manual(h.refresh(), "perplexity", "   ")
    with pytest.raises(CockpitError, match="not waiting"):
        h.engine.submit_manual(h.ctx, "claude", "x")
    run = h.engine.submit_manual(h.ctx, "perplexity", "answer one", round_seq=1)
    assert run.status == RunStatus.COMPLETE
    with pytest.raises(CockpitError, match="not waiting"):
        h.engine.submit_manual(h.ctx, "perplexity", "again", round_seq=1)


def test_no_clipboard_saves_prompt_to_private_file(harness):
    import os
    import stat

    FAKE_IO.reset(clipboard=False, browser=False)
    h = harness(providers=["claude", "perplexity"], manual_lanes=["perplexity"])
    r = pplx(h.send("Question without clipboard", fresh=True))
    saved = r.meta["handoff"]["saved_to"]
    assert saved and open(saved).read() == "Question without clipboard"
    assert stat.S_IMODE(os.stat(saved).st_mode) == 0o600


def test_branch_edit_replays_history_for_manual_lane(harness):
    h = harness(providers=["claude", "perplexity"], manual_lanes=["perplexity"])
    h.send("R1", fresh=True)
    h.engine.submit_manual(h.refresh(), "perplexity", "P1")
    h.send("R2")
    h.engine.submit_manual(h.refresh(), "perplexity", "P2")
    h.run(h.engine.edit(h.refresh(), 2, "R2 edited", sink=h.sink))
    r = h.store.run_for(h.engine.round(h.refresh()).id, "perplexity")
    assert r.attempt.meta["handoff"]["mode"] == "replay"
    assert "R1" in FAKE_IO.copied[-1] and "P1" in FAKE_IO.copied[-1] and FAKE_IO.copied[-1].endswith("R2 edited")
    assert "P2" not in FAKE_IO.copied[-1]  # later rounds of the old branch never leak in


def test_only_excludes_manual_lane_cleanly(harness):
    h = harness(providers=["claude", "perplexity"], manual_lanes=["perplexity"])
    r = pplx(h.send("Q", fresh=True, selection=Selection(only=["claude"])))
    assert r.status == RunStatus.SKIPPED and FAKE_IO.copied == []
