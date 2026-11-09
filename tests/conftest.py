from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from cockpit.config import load_config
from cockpit.domain import CockpitEvent
from cockpit.engine import Cockpit, Ctx
from cockpit.providers.base import ChatRequest
from cockpit.providers.fake import FakeAdapter, FakeBehavior
from cockpit.providers.manual import ManualAdapter


class FakeIO:
    """Stands in for the user's clipboard and browser."""

    def __init__(self) -> None:
        self.reset()

    def reset(self, clipboard: bool = True, browser: bool = True) -> None:
        self.copied: list[str] = []
        self.opened: list[str] = []
        self.has_clipboard, self.has_browser = clipboard, browser

    def copy(self, text: str) -> bool:
        if self.has_clipboard:
            self.copied.append(text)
        return self.has_clipboard

    def paste(self) -> str | None:
        return self.copied[-1] if self.has_clipboard and self.copied else None

    def open_url(self, url: str) -> bool:
        if self.has_browser:
            self.opened.append(url)
        return self.has_browser

    def available(self) -> dict[str, bool]:
        return {"clipboard": self.has_clipboard, "browser": self.has_browser}


FAKE_IO = FakeIO()
from cockpit.store import Store

PROVIDERS = ["claude", "openai", "gemini", "grok", "perplexity"]


def secret(pid: str) -> str:
    """Token only `pid`'s answers contain - used to detect leakage."""
    return f"SECRET-{pid.upper()}"


def echo_reply(pid: str):
    def reply(req: ChatRequest) -> str:
        last = req.messages[-1].content
        task = re.search(r"<<cockpit-task:(\w+)>>", last)
        if task:
            return JUDGE_OVERRIDE.get("fn", default_judge)(task.group(1), last)
        n = (len(req.messages) + 1) // 2
        return f"{secret(pid)} answer #{n} to: {last[:60]}"

    return reply


JUDGE_OVERRIDE: dict[str, Any] = {}


def default_judge(kind: str, packet: str) -> str:
    labels = re.findall(r'<response label="([A-Z])"', packet)
    if kind == "synthesis":
        return "Final synthesized answer."
    return "Here is my analysis:\n```json\n" + json.dumps({
        "summary": "Response A was the best. They mostly agree.",
        "agreements": [{"point": "they agree on Y", "responses": labels, "why": "same base case", "depends_on": "assumption Q"}],
        "disagreements": [{"topic": "X", "type": "assumption", "sides": [
            {"responses": labels[:1], "position": "X is high", "reasoning": "REASONING-SIDE-1", "evidence": "", "assumptions": ""},
            {"responses": labels[1:], "position": "X is low", "reasoning": "REASONING-SIDE-2", "evidence": "", "assumptions": ""}]}],
        "verification_needed": [{"question": "Re-check assumption X", "why": "it drives the split"}],
        "challenge": {"current_consensus": "Y", "strongest_case_against": "Y fails if Q is false",
                      "what_would_falsify": "Q false", "untested_assumptions": ["Q"]},
        "next_challenges": ["Challenge Q"],
        "likely_errors": [], "best_current_synthesis": "synth",
    }) + "\n```"


@dataclass
class Harness:
    engine: Cockpit
    store: Store
    behaviors: dict[str, FakeBehavior]
    ctx: Ctx
    events: list[CockpitEvent] = field(default_factory=list)

    def sink(self, ev: CockpitEvent) -> None:
        self.events.append(ev)

    def run(self, coro: Any) -> Any:
        return asyncio.run(coro)

    def chat(self, name: str, project: str = "p") -> Ctx:
        return self.engine.ctx(project, name)

    def refresh(self) -> Ctx:
        self.ctx = self.engine.refresh(self.ctx)
        return self.ctx

    def send(self, prompt: str, ctx: Ctx | None = None, fresh: bool = False, **kw: Any):
        return self.run(self.engine.send(ctx or self.refresh(), prompt, fresh=fresh, sink=self.sink, **kw))

    def calls(self, pid: str) -> list[ChatRequest]:
        return self.behaviors[pid].calls

    def context_of(self, pid: str, i: int = -1) -> str:
        return "\n".join(m.content for m in self.calls(pid)[i].messages)

    def run_of(self, result: Any, pid: str) -> Any:
        return next(r for r in result.runs if r.provider == pid)


@pytest.fixture(autouse=True)
def _instant_fakes(monkeypatch):
    monkeypatch.setenv("COCKPIT_FAKE_SPEED", "0")
    JUDGE_OVERRIDE.clear()
    FAKE_IO.reset()


@pytest.fixture
def harness(tmp_path: Path):
    def make(behaviors: dict[str, FakeBehavior] | None = None, providers: list[str] | None = None,
             api_lanes: list[str] | None = None, manual_lanes: list[str] | None = None, **cfg: Any) -> Harness:
        config = load_config(home=tmp_path)
        if providers:
            config.providers = {k: v for k, v in config.providers.items() if k in providers}
        for p in config.providers.values():
            p.transport = "api" if p.id in (api_lanes or []) else ("manual" if p.id in (manual_lanes or []) else "fake")
            p.verified = True
            p.enabled_by_default = True
            for k, v in cfg.items():
                setattr(p, k, v)
        behaviors = dict(behaviors or {})
        for pid in config.providers:
            behaviors.setdefault(pid, FakeBehavior())
            if behaviors[pid].reply is None:
                behaviors[pid].reply = echo_reply(pid)
        store = Store(tmp_path / "test.db")

        def factory(c):
            if c.transport == "manual":
                return ManualAdapter(c, None, io=FAKE_IO)
            return FakeAdapter(c, None, behavior=behaviors[c.id], demo=False)

        engine = Cockpit(config, store, adapter_factory=factory)
        return Harness(engine, store, behaviors, engine.ctx("p", "c1"))

    return make
