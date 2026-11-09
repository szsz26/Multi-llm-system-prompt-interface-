"""API spending guard. Cockpit never spends API money silently.

Rules
-----
* API use is OFF by default (config ``[api] enabled = false``).
* A hard monthly limit must be set (> 0) before any API run.
* Mode ``per-run`` (default): every API run also needs an explicit per-run
  approval (``--allow-api`` / ``--via api``). Mode ``until-disabled``: approved
  until ``cockpit api disable``.
* A subscription lane that fails (rate limit, auth) is marked failed with the
  options shown to the user - it never switches to the API by itself.
* The limit can only be enforced if the cost is knowable: the model must have
  a ``[pricing]`` entry or the provider must report cost itself.

Runtime toggles from ``cockpit api enable/disable`` are stored in the DB and
override config defaults.
"""

from __future__ import annotations

from dataclasses import dataclass

from .config import CockpitConfig
from .domain import ChatMessage
from .store import Store

SELF_REPORTING_COST = {"perplexity"}  # API kinds whose responses include a cost figure


@dataclass
class ApiPolicy:
    enabled: bool
    monthly_limit_usd: float
    mode: str  # per-run | until-disabled


def policy(config: CockpitConfig, store: Store) -> ApiPolicy:
    enabled = store.get_meta("api_enabled")
    limit = store.get_meta("api_monthly_limit_usd")
    mode = store.get_meta("api_mode")
    return ApiPolicy(
        enabled=(enabled == "1") if enabled is not None else config.api.enabled,
        monthly_limit_usd=float(limit) if limit is not None else config.api.monthly_limit_usd,
        mode=mode or config.api.mode,
    )


def set_policy(store: Store, *, enabled: bool, limit: float | None = None, mode: str | None = None) -> None:
    store.set_meta("api_enabled", "1" if enabled else "0")
    if limit is not None:
        store.set_meta("api_monthly_limit_usd", str(float(limit)))
    if mode is not None:
        store.set_meta("api_mode", mode)


def estimate_tokens(messages: list[ChatMessage]) -> int:
    return max(1, sum(len(m.content) for m in messages) // 4)  # ~4 chars/token; rough by design


def estimate_cost(config: CockpitConfig, model: str, messages: list[ChatMessage],
                  max_output_tokens: int | None) -> tuple[float, float] | None:
    price = config.pricing.get(model)
    if not price:
        return None
    inp = estimate_tokens(messages) * price.input_per_mtok / 1e6
    lo_out, hi_out = 300, max_output_tokens or 8000
    return inp + lo_out * price.output_per_mtok / 1e6, inp + hi_out * price.output_per_mtok / 1e6


def check(
    config: CockpitConfig,
    store: Store,
    *,
    provider: str,
    model: str,
    messages: list[ChatMessage],
    approved_this_run: bool,
) -> tuple[str | None, tuple[float, float] | None]:
    """Return (block_reason or None, estimate). None reason = allowed."""
    pol = policy(config, store)
    cfg = config.providers[provider]
    est = estimate_cost(config, model, messages, cfg.max_tokens)
    if not pol.enabled:
        return "API use is off - `cockpit api enable --limit <usd>` to allow paid API runs", est
    if pol.monthly_limit_usd <= 0:
        return "no monthly API limit set - `cockpit api enable --limit <usd>`", est
    if pol.mode == "per-run" and not approved_this_run:
        cost = f" (est. ${est[0]:.3f}-${est[1]:.3f})" if est else ""
        return f"paid API run needs approval{cost} - add --allow-api, or `cockpit api enable --until-disabled`", est
    if est is None and cfg.kind not in SELF_REPORTING_COST:
        return (f"cannot enforce the monthly limit: no [pricing.\"{model}\"] entry in config.toml "
                "for this model"), None
    spent = store.api_spend()
    if spent >= pol.monthly_limit_usd:
        return f"monthly API limit reached (${spent:.2f} of ${pol.monthly_limit_usd:.2f})", est
    if est and spent + est[1] > pol.monthly_limit_usd:
        return (f"estimated cost up to ${est[1]:.3f} would exceed the remaining "
                f"${pol.monthly_limit_usd - spent:.2f} this month"), est
    return None, est


def actual_cost(config: CockpitConfig, model: str | None, input_tokens: int | None,
                output_tokens: int | None, reported: float | None) -> float | None:
    if reported is not None:
        return reported
    price = config.pricing.get(model or "")
    if price and input_tokens is not None and output_tokens is not None:
        return (input_tokens * price.input_per_mtok + output_tokens * price.output_per_mtok) / 1e6
    return None
