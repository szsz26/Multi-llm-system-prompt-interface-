"""§61 Stop, Retry, Rerun, Provider failure, Streaming."""

import asyncio

import pytest

from cockpit.domain import CockpitError, EventType, RunStatus
from cockpit.engine import Selection
from cockpit.providers.base import ErrorKind, ProviderError
from cockpit.providers.fake import FakeBehavior


def runs(result):
    return {r.provider: r for r in result.runs}


def test_mixed_failures_contained_and_status_mapping(harness):
    h = harness(behaviors={
        "gemini": FakeBehavior(fail=ProviderError(ErrorKind.AUTH, "gemini: login expired")),
        "openai": FakeBehavior(fail=ProviderError(ErrorKind.RATE_LIMIT, "usage limit reached")),
        "perplexity": FakeBehavior(hang=True),
        "grok": FakeBehavior(reply="partial words here and more", fail_after_chunks=2),
    }, timeout=0.5)
    r = runs(h.send("Q", fresh=True))
    assert r["claude"].status == RunStatus.COMPLETE
    assert r["gemini"].status == RunStatus.NEEDS_REAUTH
    assert r["openai"].status == RunStatus.RATE_LIMITED
    assert r["perplexity"].status == RunStatus.TIMEOUT
    assert r["grok"].status == RunStatus.FAILED and r["grok"].output_text == "partial words "
    for pid in ("gemini", "openai", "perplexity", "grok"):
        assert h.store.get_head(h.ctx.branch.id, pid).head_message_id is None  # nothing committed


def test_retry_affects_only_target_lane_and_keeps_attempts(harness):
    h = harness(providers=["claude", "openai", "gemini"],
                behaviors={"gemini": FakeBehavior(fail=ProviderError(ErrorKind.SERVER, "boom"), fail_times=1)}, max_retries=0)
    h.send("Q", fresh=True)
    before = {p: len(h.calls(p)) for p in ("claude", "openai")}
    res = h.run(h.engine.retry(h.refresh(), ["gemini"], sink=h.sink))
    g = runs(res)["gemini"]
    assert g.status == RunStatus.COMPLETE and g.attempt_count == 2 and g.n == 2
    assert {p: len(h.calls(p)) for p in ("claude", "openai")} == before
    atts = h.store.attempts_for_run(g.id)
    assert [a.status for a in atts] == [RunStatus.FAILED, RunStatus.COMPLETE]


def test_rerun_successful_lane_preserves_both_attempts(harness):
    replies = iter(["first answer", "second answer"])
    h = harness(providers=["claude", "openai"], behaviors={"claude": FakeBehavior(reply=lambda r: next(replies))})
    h.send("Q", fresh=True)
    res = h.run(h.engine.rerun(h.refresh(), ["claude"], sink=h.sink))
    c = runs(res)["claude"]
    atts = h.store.attempts_for_run(c.id)
    assert [a.output_text for a in atts] == ["first answer", "second answer"]
    assert c.current_attempt_id == atts[1].id
    h.send("next")
    ctx = [m.content for m in h.calls("claude")[-1].messages]
    assert ctx == ["Q", "second answer", "next"]  # lane continues from the current attempt only


def test_failed_rerun_never_replaces_good_answer(harness):
    h = harness(providers=["claude", "openai"], max_retries=0)
    h.send("Q", fresh=True)
    h.behaviors["claude"].fail = ProviderError(ErrorKind.SERVER, "down")
    res = h.run(h.engine.rerun(h.refresh(), ["claude"], sink=h.sink))
    c = runs(res)["claude"]
    assert c.status == RunStatus.COMPLETE and c.n == 1 and c.attempt_count == 2


def test_rerun_refused_once_lane_moved_on(harness):
    h = harness(providers=["claude", "openai"])
    h.send("Q1", fresh=True)
    h.send("Q2")
    with pytest.raises(CockpitError, match="moved on"):
        h.run(h.engine.rerun(h.refresh(), ["claude"], round_seq=1, sink=h.sink))


def test_stop_one_lane_from_another_process_others_continue(harness):
    h = harness(providers=["claude", "openai", "gemini"], behaviors={"openai": FakeBehavior(hang=True)}, timeout=30)

    async def go():
        task = asyncio.create_task(h.engine.send(h.refresh(), "Q", fresh=True, sink=h.sink))
        for _ in range(200):
            await asyncio.sleep(0.02)
            # simulates `cockpit stop openai` from another terminal: only writes to the DB
            if h.engine.request_stop(h.ctx, "openai"):
                break
        return await task

    r = runs(asyncio.run(go()))
    assert r["openai"].status == RunStatus.STOPPED and "stopped by user" in r["openai"].error_message
    assert r["claude"].status == RunStatus.COMPLETE and r["gemini"].status == RunStatus.COMPLETE


def test_first_n_stops_remaining_lanes(harness):
    h = harness(providers=["claude", "openai", "gemini"],
                behaviors={"gemini": FakeBehavior(start_delay=5)}, timeout=30)
    r = runs(h.send("Q", fresh=True, first=2))
    assert r["gemini"].status == RunStatus.STOPPED and "--first 2" in r["gemini"].error_message


def test_transient_error_retried_once_visibly_rate_limit_not_retried(harness, monkeypatch):
    async def fast(_):
        return None

    monkeypatch.setattr("cockpit.engine.asyncio.sleep", fast)
    h = harness(providers=["claude", "openai"], behaviors={
        "claude": FakeBehavior(fail=ProviderError(ErrorKind.NETWORK, "reset", retryable=True), fail_times=1),
        "openai": FakeBehavior(fail=ProviderError(ErrorKind.RATE_LIMIT, "429", retryable=True), fail_times=1),
    }, max_retries=1)
    r = runs(h.send("Q", fresh=True))
    assert r["claude"].status == RunStatus.COMPLETE and len(h.calls("claude")) == 2
    assert r["openai"].status == RunStatus.RATE_LIMITED and len(h.calls("openai")) == 1  # don't burn allowance
    assert sum(e.type == EventType.RUN_RETRYING for e in h.events) == 1


def test_no_retry_after_text_streamed(harness):
    h = harness(providers=["claude", "openai"], behaviors={"openai": FakeBehavior(reply="a b c d", fail_after_chunks=2)})
    h.send("Q", fresh=True)
    assert len(h.calls("openai")) == 1


def test_streaming_is_independent_per_lane(harness):
    h = harness(providers=["claude", "openai"], behaviors={"openai": FakeBehavior(start_delay=0.3)})
    h.send("Q", fresh=True)
    order = [(e.type, e.provider) for e in h.events if e.type in (EventType.DELTA, EventType.RUN_COMPLETE)]
    claude_done = order.index((EventType.RUN_COMPLETE, "claude"))
    first_openai = next(i for i, x in enumerate(order) if x[1] == "openai")
    assert claude_done < first_openai  # the fast lane finished before the slow one even started streaming


def test_empty_response_not_committed(harness):
    h = harness(providers=["claude", "openai"], behaviors={"openai": FakeBehavior(reply="")})
    r = runs(h.send("Q", fresh=True))
    assert r["openai"].error_kind == "empty_response"
    assert h.store.get_head(h.ctx.branch.id, "openai").head_message_id is None


def test_lane_prompt_only_one_lane(harness):
    h = harness(providers=["claude", "openai"])
    r = runs(h.send("just you", selection=Selection(only=["openai"])))
    assert r["claude"].status == RunStatus.SKIPPED and r["openai"].status == RunStatus.COMPLETE


def test_narration_before_tool_use_is_not_the_committed_answer(harness):
    from cockpit.providers.base import ResponseMeta, TextDelta

    h = harness(providers=["claude", "openai"])
    adapter = h.engine.adapter("claude")

    async def stream(req):
        yield TextDelta("I'll search the web first. ")
        yield TextDelta("The answer is 42.")
        yield ResponseMeta(finish_reason="stop", extra={"answer_from": len("I'll search the web first. ")})

    adapter.stream = stream
    r = runs(h.send("Q", fresh=True))["claude"]
    assert r.output_text == "The answer is 42." and r.raw_output.startswith("I'll search")
    assert h.store.history(h.ctx.chat.id, "claude", h.store.get_head(h.ctx.branch.id, "claude").head_message_id)[1].content == "The answer is 42."
