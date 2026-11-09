"""§61 API billing guard: never silently spend; never auto-switch from subscription to API."""

import pytest

from cockpit import billing
from cockpit.config import Pricing
from cockpit.domain import AnalysisKind, CockpitError, RunStatus
from cockpit.providers.base import ErrorKind, ProviderError
from cockpit.providers.fake import FakeBehavior


def pplx(res):
    return next(r for r in res.runs if r.provider == "perplexity")


def test_api_lane_skipped_while_api_off(harness):
    h = harness(providers=["claude", "perplexity"], api_lanes=["perplexity"])
    r = pplx(h.send("Q", fresh=True))
    assert r.status == RunStatus.SKIPPED and "API use is off" in r.skip_reason
    assert h.calls("perplexity") == []


def test_per_run_approval_required_then_cost_recorded(harness):
    h = harness(providers=["claude", "perplexity"], api_lanes=["perplexity"])
    billing.set_policy(h.store, enabled=True, limit=5.0)
    h.engine.config.pricing["sonar-pro"] = Pricing(3.0, 15.0)
    r = pplx(h.send("Q", fresh=True))
    assert r.status == RunStatus.SKIPPED and "--allow-api" in r.skip_reason
    r = pplx(h.send("Q2", fresh=True, allow_api=True))
    assert r.status == RunStatus.COMPLETE and r.billing == "api" and r.cost_usd and r.cost_usd > 0
    assert h.store.api_spend() == pytest.approx(r.cost_usd)


def test_unpriced_model_blocks_enforcement(harness):
    h = harness(providers=["claude", "grok"], api_lanes=["grok"])
    billing.set_policy(h.store, enabled=True, limit=5.0, mode="until-disabled")
    r = next(x for x in h.send("Q", fresh=True).runs if x.provider == "grok")
    assert r.status == RunStatus.SKIPPED and "cannot enforce the monthly limit" in r.skip_reason


def test_monthly_limit_is_hard(harness):
    h = harness(providers=["claude", "perplexity"], api_lanes=["perplexity"])
    billing.set_policy(h.store, enabled=True, limit=1.0, mode="until-disabled")
    h.engine.config.pricing["sonar-pro"] = Pricing(3.0, 15.0)
    h.store.add_usage(provider="perplexity", billing="api", cost_usd=1.0)
    r = pplx(h.send("Q", fresh=True))
    assert r.status == RunStatus.SKIPPED and "limit reached" in r.skip_reason


def test_until_disabled_mode_and_disable(harness):
    h = harness(providers=["claude", "perplexity"], api_lanes=["perplexity"])
    h.engine.config.pricing["sonar-pro"] = Pricing(1.0, 1.0)
    billing.set_policy(h.store, enabled=True, limit=10.0, mode="until-disabled")
    assert pplx(h.send("Q", fresh=True)).status == RunStatus.COMPLETE
    billing.set_policy(h.store, enabled=False)
    assert pplx(h.send("Q2", fresh=True, allow_api=True)).status == RunStatus.SKIPPED


def test_subscription_failure_never_falls_back_to_api(harness):
    calls_api = []

    h = harness(providers=["claude", "openai"],
                behaviors={"openai": FakeBehavior(fail=ProviderError(ErrorKind.RATE_LIMIT, "usage limit reached"))})
    billing.set_policy(h.store, enabled=True, limit=100.0, mode="until-disabled")  # even with API fully enabled
    orig = h.engine._factory
    h.engine._factory = lambda cfg: (calls_api.append(cfg.id) if cfg.transport == "api" else None) or orig(cfg)
    r = next(x for x in h.send("Q", fresh=True).runs if x.provider == "openai")
    assert r.status == RunStatus.RATE_LIMITED and r.billing != "api"
    assert calls_api == []


def test_retry_via_api_needs_enabled_api(harness):
    h = harness(providers=["claude", "openai"],
                behaviors={"openai": FakeBehavior(fail=ProviderError(ErrorKind.RATE_LIMIT, "limit"), fail_times=1)})
    h.send("Q", fresh=True)
    with pytest.raises(CockpitError, match="API use is off"):
        h.run(h.engine.retry(h.refresh(), ["openai"], via_api=True))
    billing.set_policy(h.store, enabled=True, limit=10.0)
    h.engine.config.pricing["gpt-5.5"] = Pricing(1.0, 1.0)
    res = h.run(h.engine.retry(h.refresh(), ["openai"], via_api=True))
    o = next(x for x in res.runs if x.provider == "openai")
    assert o.status == RunStatus.COMPLETE and o.billing == "api" and o.model_requested == "gpt-5.5"


def test_judge_on_api_lane_needs_approval(harness):
    h = harness(providers=["claude", "openai", "perplexity"], api_lanes=["perplexity"])
    h.send("Q", fresh=True)
    with pytest.raises(CockpitError, match="judge perplexity runs over the API"):
        h.run(h.engine.analyze(h.refresh(), AnalysisKind.COMPARE, judge="perplexity"))
