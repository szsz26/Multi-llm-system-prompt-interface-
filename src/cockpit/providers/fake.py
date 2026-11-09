"""Fake provider for demo mode and tests: no network, no cost.

Behaviour is scriptable so tests can simulate slow, failing, hanging or
mid-stream-crashing providers, and inspect exactly what each provider received.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

from .base import (
    HealthReport,
    AdapterEvent,
    ChatRequest,
    ErrorKind,
    ProviderAdapter,
    ProviderError,
    ResponseMeta,
    TextDelta,
    Usage,
)

Reply = str | Callable[[ChatRequest], str]


@dataclass
class FakeBehavior:
    reply: Reply | None = None
    chunk_delay: float = 0.0
    start_delay: float = 0.0
    fail: ProviderError | None = None  # raised before any output
    fail_after_chunks: int | None = None  # raise mid-stream
    fail_times: int | None = None  # fail only the first N calls (for retry tests)
    hang: bool = False  # never respond (timeout tests)
    calls: list[ChatRequest] = field(default_factory=list)


_PERSONAS = {
    "claude": ("leans cautious", "regulatory exposure is the binding constraint"),
    "openai": ("leans structured", "unit economics decide the outcome"),
    "gemini": ("leans quantitative", "the market-size assumption drives everything"),
    "grok": ("leans contrarian", "the consensus view underrates execution risk"),
    "perplexity": ("leans source-driven", "recent filings matter more than narrative"),
}


def demo_reply(provider: str, request: ChatRequest) -> str:
    last = request.messages[-1].content
    task = re.search(r"<<cockpit-task:(\w+)>>", last)
    if task:
        return _demo_judge(task.group(1), last)
    prior = (len(request.messages) - 1) // 2
    style, view = _PERSONAS.get(provider, ("neutral", "it depends on the assumptions"))
    first_line = last.strip().splitlines()[0][:100] if last.strip() else ""
    return (
        f"**{provider} (demo)** - answering: _{first_line}_\n\n"
        f"I can see {prior} earlier exchange(s) in *my own* thread, and nothing from other models.\n\n"
        f"1. My core view ({style}): {view}.\n"
        f"2. Key assumption: the base case holds for the next 12-18 months.\n"
        f"3. What would change my mind: evidence contradicting point 1.\n\n"
        "This is fake output from demo mode; configure API keys and drop `--demo` for real models."
    )


def _demo_judge(kind: str, packet: str) -> str:
    labels = sorted(set(re.findall(r'<response label="([A-Z])"', packet)))
    if kind == "synthesis":
        return "## Synthesis (demo)\n\nCombining the best-supported points of " + ", ".join(labels) + "."
    half = max(1, len(labels) // 2)
    result = {
        "summary": "The analyses share a base case but split on what the binding constraint is.",
        "agreements": [{"point": "The base case holds for the next 12-18 months.", "responses": labels,
                        "why": "each assumes current conditions persist", "depends_on": "an unstated macro assumption"}],
        "disagreements": [{
            "topic": "What the binding constraint is", "type": "assumption", "significance": "high",
            "sides": [
                {"responses": labels[:half], "position": "regulation is the binding constraint",
                 "reasoning": "licensing timelines dominate everything else", "evidence": "none cited",
                 "assumptions": "regulators stay slow"},
                {"responses": labels[half:], "position": "economics or execution decide the outcome",
                 "reasoning": "margins and delivery risk swamp regulatory delay", "evidence": "none cited",
                 "assumptions": "licensing is obtainable"},
            ]}],
        "shared_logic": ["All reason from a 12-18 month base case."],
        "different_paths_same_conclusion": [],
        "assumptions_driving_divergence": ["How fast regulators move."],
        "factual_conflicts": [],
        "unsupported_claims": [{"claim": "Market size figure", "responses": labels[:1], "why": "no source given"}],
        "unique_insights": [{"response": labels[-1] if labels else "A", "insight": "Execution risk is under-weighted."}],
        "omissions": ["Nobody quantified the downside case."],
        "verification_needed": [{"question": "What is the current licensing timeline?", "why": "drives the split"}],
        "challenge": {"current_consensus": "The base case holds for 12-18 months.",
                      "strongest_case_against": "A rate shock or regulatory reversal would break the base case early.",
                      "what_would_falsify": "A licensing decision delayed beyond 12 months.",
                      "untested_assumptions": ["current conditions persist"]},
        "next_challenges": ["Challenge the 12-18 month base case directly."],
    }
    if kind == "adjudicate":
        result["likely_errors"] = []
        result["best_current_synthesis"] = "Base case likely holds; the binding constraint is unresolved."
    return json.dumps(result)


class FakeAdapter(ProviderAdapter):
    transport = "fake"
    capabilities = frozenset({"streaming", "stop", "reasoning_effort", "citations"})
    demo_chunk_delay = {"claude": 0.012, "openai": 0.02, "gemini": 0.03, "grok": 0.016, "perplexity": 0.025}
    demo_start_delay = {"claude": 0.2, "openai": 0.6, "gemini": 1.2, "grok": 0.4, "perplexity": 0.9}

    def __init__(self, config: Any, secrets: Any, behavior: FakeBehavior | None = None, demo: bool = True) -> None:
        super().__init__(config, secrets)
        if behavior is None:
            import os

            scale = float(os.environ.get("COCKPIT_FAKE_SPEED", "1") or 1)  # 0 = instant (tests)
            demo = demo and scale > 0
            behavior = FakeBehavior(
                chunk_delay=self.demo_chunk_delay.get(config.id, 0.02) * scale if demo else 0.0,
                start_delay=self.demo_start_delay.get(config.id, 0.3) * scale if demo else 0.0,
            )
        self.behavior = behavior

    async def stream(self, request: ChatRequest) -> AsyncIterator[AdapterEvent]:
        b = self.behavior
        b.calls.append(request)
        if b.start_delay:
            await asyncio.sleep(b.start_delay)
        if b.hang:
            await asyncio.sleep(10_000)
        if b.fail is not None and (b.fail_times is None or len(b.calls) <= b.fail_times):
            raise b.fail
        reply = b.reply if b.reply is not None else (lambda r: demo_reply(self.provider_id, r))
        text = reply(request) if callable(reply) else reply
        yield ResponseMeta(model=f"{request.model}", response_id=f"fake-{len(b.calls)}")
        chunks = re.findall(r"\S+\s*|\s+", text) or [""]
        for i, chunk in enumerate(chunks):
            if b.fail_after_chunks is not None and i >= b.fail_after_chunks:
                raise ProviderError(ErrorKind.NETWORK, f"{self.provider_id}: connection reset (simulated)")
            if b.chunk_delay:
                await asyncio.sleep(b.chunk_delay)
            yield TextDelta(chunk)
        yield Usage(
            input_tokens=sum(len(m.content.split()) for m in request.messages),
            output_tokens=len(text.split()),
        )
        yield ResponseMeta(finish_reason="stop")

    async def health_check(self, live: bool = False) -> HealthReport:
        r = self.new_report()
        r.credentials = r.authenticated = r.model_available = True
        r.notes.append("demo/fake provider - no network, no cost")
        return r
