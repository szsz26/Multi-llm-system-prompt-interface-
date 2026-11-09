"""Core value types shared by the engine, providers, storage and UI.

Nothing in this module knows about any specific provider or any UI.

Vocabulary
----------
Project  > Chat  > Branch  > Round  > Run (one per lane per round)  > Attempt
Lane     = one provider's independent conversation inside a Chat (per Branch head).
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ChatMessage:
    """One turn in a provider-neutral conversation."""

    role: str  # "user" | "assistant"
    content: str


class RunStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETE = "complete"
    FAILED = "failed"
    TIMEOUT = "timeout"
    STOPPED = "stopped"
    SKIPPED = "skipped"
    NEEDS_REAUTH = "needs_reauth"
    RATE_LIMITED = "rate_limited"
    WAITING = "waiting"  # one-click helper lane: waiting for you to paste the answer

    @property
    def failed(self) -> bool:
        return self in (
            RunStatus.FAILED, RunStatus.TIMEOUT, RunStatus.STOPPED,
            RunStatus.NEEDS_REAUTH, RunStatus.RATE_LIMITED,
        )


class RoundKind(str, enum.Enum):
    BLAST = "blast"  # fresh lane context for every targeted lane
    FOLLOWUP = "followup"  # continue each lane's own context
    FEEDBACK = "feedback"  # user-reviewed feedback packet + instruction into each lane's own context


class AnalysisKind(str, enum.Enum):
    COMPARE = "compare"  # descriptive: what the answers say, where and why they differ
    ADJUDICATE = "adjudicate"  # evaluative: likely errors, verification, challenge
    SYNTHESIS = "synthesis"  # explicitly requested final answer


class Disclosure(str, enum.Enum):
    """How much cross-model material a feedback packet reveals."""

    SUMMARY = "summary"
    ANONYMIZED = "anonymized"  # summary + strongest anonymized argument per side (default)
    SELECTED = "selected"  # summary + selected full responses
    EVERYTHING = "everything"  # summary + every full response
    CUSTOM = "custom"  # user writes the payload


class Billing(str, enum.Enum):
    SUBSCRIPTION = "subscription"
    API = "api"
    NONE = "none"  # fake/demo


class EventType(str, enum.Enum):
    ROUND_STARTED = "round_started"
    RUN_SKIPPED = "run_skipped"
    RUN_STARTED = "run_started"
    RUN_RETRYING = "run_retrying"
    DELTA = "delta"
    SESSION_CREATED = "session_created"  # provider-native session/thread id observed
    USAGE_UPDATE = "usage_update"
    RUN_COMPLETE = "run_complete"
    RUN_ERROR = "run_error"
    RUN_STOPPED = "run_stopped"
    RUN_WAITING = "run_waiting"  # manual lane handed to the user
    ROUND_COMPLETE = "round_complete"
    JUDGE_STARTED = "judge_started"
    JUDGE_DELTA = "judge_delta"
    JUDGE_COMPLETE = "judge_complete"
    NOTICE = "notice"


@dataclass
class CockpitEvent:
    """Normalized event emitted by the engine to whatever UI is attached."""

    type: EventType
    provider: str | None = None
    round_seq: int | None = None
    text: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "type": self.type.value,
            "provider": self.provider,
            "round": self.round_seq,
            "text": self.text,
            **({"data": self.data} if self.data else {}),
        }


class CockpitError(Exception):
    """User-facing error (bad arguments, missing chat, integrity violation...)."""
