"""§61 Round integrity, Toggle behavior, model/effort settings, identical-run cache, blind judging."""

import random

import pytest

from cockpit.analysis import build_packet
from cockpit.domain import AnalysisKind, CockpitError, RunStatus
from cockpit.engine import Selection

from .conftest import secret


def st(result):
    return {r.provider: (r.status, r.skip_reason) for r in result.runs}


def test_round_records_every_lane_and_metadata(harness):
    h = harness()
    h.send("Analyze X.", fresh=True)
    r2 = h.send("Regulatory risk?", selection=Selection(only=["claude", "openai", "gemini"]))
    s = st(r2)
    assert s["claude"][0] == RunStatus.COMPLETE
    assert s["grok"] == (RunStatus.SKIPPED, "not in --only")
    run = h.run_of(r2, "claude")
    assert run.model_requested == "claude-opus-5-5" and run.transport == "fake"
    assert run.started_at and run.ended_at and run.latency_ms is not None and run.input_text == "Regulatory risk?"
    assert r2.round.seq == 2 and r2.round.kind == "followup" and r2.round.branch_id == h.ctx.branch.id


def test_skipped_lane_stale_answer_never_compared(harness):
    h = harness()
    h.send("Round one", fresh=True)
    h.send("Round two", selection=Selection(exclude=["grok"]))
    h.run(h.engine.analyze(h.refresh(), AnalysisKind.ADJUDICATE, judge="claude"))
    packet = h.calls("claude")[-1].messages[0].content
    assert secret("grok") not in packet and "answer #1" not in packet
    assert packet.count("<response label=") == 4
    lc = h.engine.local_compare(h.ctx)
    assert any("Grok (excluded" in n for n in lc["notes"])


def test_packet_refuses_mixed_rounds(harness):
    h = harness(providers=["claude", "openai"])
    r1 = h.send("one", fresh=True)
    r2 = h.send("two")
    with pytest.raises(ValueError):
        build_packet(AnalysisKind.COMPARE, r2.round, [r1.runs[0], r2.runs[1]], prior_prompts=[])


def test_analysis_needs_two_completed_responses(harness):
    h = harness(providers=["claude", "openai"])
    h.send("one", fresh=True)
    h.send("two", selection=Selection(only=["claude"]))
    with pytest.raises(CockpitError, match="never substituted"):
        h.run(h.engine.analyze(h.refresh(), AnalysisKind.COMPARE, judge="claude"))
    assert h.run(h.engine.analyze(h.ctx, AnalysisKind.COMPARE, judge="claude", round_seq=1)).status == "complete"


def test_mute_persists_history_kept_and_continue_reentry(harness):
    h = harness(providers=["claude", "grok"])
    h.send("Q1", fresh=True)
    h.engine.set_enabled(h.ctx, ["grok"], False)
    assert st(h.send("Q2"))["grok"] == (RunStatus.SKIPPED, "muted")
    assert st(h.send("Q3"))["grok"] == (RunStatus.SKIPPED, "muted")
    h.engine.set_enabled(h.ctx, ["grok"], True)  # default re-entry: continue from where it stopped
    h.send("Q4")
    msgs = [m.content for m in h.calls("grok")[-1].messages]
    assert msgs[0] == "Q1" and msgs[1].startswith(secret("grok")) and msgs[2] == "Q4" and len(msgs) == 3


def test_unmute_fresh_reentry(harness):
    h = harness(providers=["claude", "grok"])
    h.send("Q1", fresh=True)
    h.engine.set_enabled(h.ctx, ["grok"], False)
    h.send("Q2")
    h.engine.set_enabled(h.ctx, ["grok"], True, reentry="fresh")
    h.send("Q3")
    assert [m.content for m in h.calls("grok")[-1].messages] == ["Q3"]


def test_only_exclude_do_not_mutate_state(harness):
    h = harness(providers=["claude", "openai", "grok"])
    h.send("x", selection=Selection(only=["claude"]))
    h.send("y", selection=Selection(exclude=["openai"]))
    assert all(h.engine.is_enabled(h.ctx.chat, p) for p in ("claude", "openai", "grok"))
    assert h.store.lane_settings(h.ctx.chat.id) == {}


def test_only_includes_muted_lane_for_one_round(harness):
    h = harness(providers=["claude", "grok"])
    h.engine.set_enabled(h.ctx, ["grok"], False)
    assert st(h.send("x", selection=Selection(only=["grok"])))["grok"][0] == RunStatus.COMPLETE
    assert not h.engine.is_enabled(h.ctx.chat, "grok")


def test_aliases_and_codex_is_not_a_provider(harness):
    h = harness()
    assert h.engine.config.resolve_many("chatgpt,xai,pplx") == ["openai", "grok", "perplexity"]
    with pytest.raises(CockpitError, match="transport"):
        h.engine.config.resolve_provider("codex")
    with pytest.raises(CockpitError, match="Did you mean"):
        h.engine.config.resolve_provider("gemni")


def test_model_and_effort_per_lane_recorded(harness):
    h = harness(providers=["claude", "openai"])
    h.engine.set_model(h.ctx, "claude", "claude-sonnet-5-5")
    h.engine.set_effort(h.ctx, "openai", "high")
    r = h.send("x")
    assert h.run_of(r, "claude").model_requested == "claude-sonnet-5-5"
    assert h.calls("claude")[-1].model == "claude-sonnet-5-5"
    assert h.run_of(r, "openai").effort == "high" and h.calls("openai")[-1].reasoning == "high"
    with pytest.raises(CockpitError):
        h.engine.set_model(h.ctx, "claude", "../../etc/passwd")


def test_effort_not_applied_where_unsupported_is_visible(harness):
    from cockpit.providers.fake import FakeAdapter

    h = harness(providers=["claude", "openai"])
    FakeAdapter.capabilities = frozenset({"streaming", "stop"})
    try:
        h.engine.set_effort(h.ctx, "claude", "max")
        r = h.send("x")
        run = h.run_of(r, "claude")
        assert run.effort is None and "not supported" in run.meta["notes"][0]
        assert h.calls("claude")[-1].reasoning is None
    finally:
        FakeAdapter.capabilities = frozenset({"streaming", "stop", "reasoning_effort", "citations"})


def test_identical_run_cache_warns_and_force_overrides(harness):
    h = harness(providers=["claude", "openai"])
    h.send("same question", fresh=True)
    r = h.send("same question", fresh=True)
    assert all(x.status == RunStatus.SKIPPED and "identical" in x.skip_reason for x in r.runs)
    assert len(h.calls("claude")) == 1
    r = h.send("same question", fresh=True, force=True)
    assert all(x.status == RunStatus.COMPLETE for x in r.runs)


def test_no_lanes_selected_creates_no_round(harness):
    h = harness(providers=["claude", "grok"])
    with pytest.raises(CockpitError, match="no lanes selected"):
        h.send("x", selection=Selection(exclude=["claude", "grok"]))
    assert h.store.list_rounds(h.ctx.chat.id) == []


def test_blind_labels_shuffled_and_mapping_stored(harness):
    h = harness()
    h.send("x", fresh=True)
    seen = set()
    for seed in range(8):
        a = h.run(h.engine.analyze(h.refresh(), AnalysisKind.COMPARE, judge="claude", rng=random.Random(seed)))
        assert {v["provider"] for v in a.label_map.values()} == set(h.engine.config.provider_ids)
        assert 'system="' not in a.prompt_text
        seen.add(a.label_map["A"]["provider"])
    assert len(seen) > 1
    att = h.run(h.engine.analyze(h.ctx, AnalysisKind.COMPARE, judge="claude", blind=False))
    assert 'system="Claude"' in att.prompt_text
