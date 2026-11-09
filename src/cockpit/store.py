"""SQLite persistence for operational state (schema v2, with migrations).

Model
-----
projects > chats > branches > rounds > runs > attempts

* Cockpit owns the canonical transcript. ``messages`` form a tree per lane
  (chat + provider); a lane's position on a branch is ``lane_heads``.
  Provider-native session ids are cached on the head as an optimization only.
* Branching = a new ``branches`` row that forks *before* an existing round. Its
  lane heads are reconstructed from the parent lineage; later rounds of the
  old branch never enter it. Nothing is ever rewritten.
* ``runs`` is the (round, lane) slot; ``attempts`` holds every execution of
  it (initial, retry, rerun). ``runs.current_attempt_id`` is the latest
  successful attempt (or the latest attempt if none succeeded).
* A message is only written when an attempt completes. Failed / stopped
  attempts keep their partial output for inspection only.
* Long-term knowledge/memory does NOT live here (see memory.py).
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .domain import ChatMessage, CockpitError, RunStatus

SCHEMA_VERSION = 2

SCHEMA_V2 = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chats (
    id                 INTEGER PRIMARY KEY,
    project_id         INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name               TEXT NOT NULL,
    current_branch_id  INTEGER,
    created_at         TEXT NOT NULL,
    UNIQUE (project_id, name)
);

CREATE TABLE IF NOT EXISTS branches (
    id                INTEGER PRIMARY KEY,
    chat_id           INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    name              TEXT NOT NULL,
    parent_branch_id  INTEGER,
    fork_round_id     INTEGER,   -- this branch diverges immediately BEFORE this round of the parent
    created_at        TEXT NOT NULL,
    UNIQUE (chat_id, name)
);

-- Persistent per-chat lane settings: mute, model, effort.
CREATE TABLE IF NOT EXISTS lanes (
    chat_id   INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    provider  TEXT NOT NULL,
    enabled   INTEGER NOT NULL DEFAULT 1,
    model     TEXT,
    effort    TEXT,
    PRIMARY KEY (chat_id, provider)
);

-- Where each lane currently is on each branch.
CREATE TABLE IF NOT EXISTS lane_heads (
    branch_id          INTEGER NOT NULL REFERENCES branches(id) ON DELETE CASCADE,
    provider           TEXT NOT NULL,
    head_message_id    INTEGER,
    native_session_id  TEXT,
    native_head_id     INTEGER,
    PRIMARY KEY (branch_id, provider)
);

CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY,
    chat_id     INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    provider    TEXT NOT NULL,
    parent_id   INTEGER,
    role        TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content     TEXT NOT NULL,
    attempt_id  INTEGER,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS messages_lane ON messages(chat_id, provider);

CREATE TABLE IF NOT EXISTS rounds (
    id                    INTEGER PRIMARY KEY,
    chat_id               INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    branch_id             INTEGER NOT NULL,
    seq                   INTEGER NOT NULL,
    parent_round_id       INTEGER,
    kind                  TEXT NOT NULL,
    prompt                TEXT NOT NULL,
    feedback_packet_id    INTEGER,
    edited_from_round_id  INTEGER,
    context_json          TEXT NOT NULL DEFAULT '[]',
    created_at            TEXT NOT NULL,
    completed_at          TEXT,
    UNIQUE (chat_id, seq)
);

-- One row per lane per round, including skips (with the reason).
CREATE TABLE IF NOT EXISTS runs (
    id                  INTEGER PRIMARY KEY,
    round_id            INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
    provider            TEXT NOT NULL,
    status              TEXT NOT NULL,
    skip_reason         TEXT,
    input_text          TEXT,
    context_head_id     INTEGER,
    current_attempt_id  INTEGER,
    UNIQUE (round_id, provider)
);

CREATE TABLE IF NOT EXISTS attempts (
    id                    INTEGER PRIMARY KEY,
    run_id                INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    n                     INTEGER NOT NULL,
    reason                TEXT NOT NULL,        -- initial | retry | rerun
    status                TEXT NOT NULL,
    transport             TEXT,
    billing               TEXT,
    model_requested       TEXT,
    model_reported        TEXT,
    effort                TEXT,
    native_id             TEXT,
    client_version        TEXT,
    input_hash            TEXT,
    raw_output            TEXT,                 -- exact provider output, never rewritten
    output_text           TEXT,                 -- normalized text committed to the lane
    error_kind            TEXT,
    error_message         TEXT,
    started_at            TEXT,
    ended_at              TEXT,
    first_token_ms        INTEGER,
    latency_ms            INTEGER,
    input_tokens          INTEGER,
    output_tokens         INTEGER,
    cost_usd              REAL,                 -- real API spend only
    assistant_message_id  INTEGER,
    meta_json             TEXT NOT NULL DEFAULT '{}',
    UNIQUE (run_id, n)
);
CREATE INDEX IF NOT EXISTS attempts_hash ON attempts(input_hash);

CREATE TABLE IF NOT EXISTS analyses (
    id              INTEGER PRIMARY KEY,
    round_id        INTEGER NOT NULL REFERENCES rounds(id) ON DELETE CASCADE,
    kind            TEXT NOT NULL,
    judge_provider  TEXT NOT NULL,
    judge_model     TEXT,
    blind           INTEGER NOT NULL,
    label_map_json  TEXT NOT NULL,
    prompt_text     TEXT NOT NULL,
    raw_output      TEXT,
    result_json     TEXT,
    status          TEXT NOT NULL,
    error_message   TEXT,
    input_tokens    INTEGER,
    output_tokens   INTEGER,
    cost_usd        REAL,
    latency_ms      INTEGER,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS feedback_packets (
    id             INTEGER PRIMARY KEY,
    chat_id        INTEGER NOT NULL REFERENCES chats(id) ON DELETE CASCADE,
    round_id       INTEGER NOT NULL,
    analysis_id    INTEGER,
    disclosure     TEXT NOT NULL,
    included_json  TEXT NOT NULL DEFAULT '[]',
    content        TEXT NOT NULL,
    instruction    TEXT,
    status         TEXT NOT NULL,                -- draft | sent | discarded
    edited         INTEGER NOT NULL DEFAULT 0,
    sent_round_id  INTEGER,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_events (
    id             INTEGER PRIMARY KEY,
    chat_id        INTEGER,
    attempt_id     INTEGER,
    analysis_id    INTEGER,
    provider       TEXT NOT NULL,
    transport      TEXT,
    billing        TEXT NOT NULL,
    model          TEXT,
    input_tokens   INTEGER,
    output_tokens  INTEGER,
    cost_usd       REAL,
    meta_json      TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL
);

-- Cross-process stop: `cockpit stop gemini` in another terminal.
CREATE TABLE IF NOT EXISTS stop_requests (
    id          INTEGER PRIMARY KEY,
    round_id    INTEGER NOT NULL,
    provider    TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    handled_at  TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


# --------------------------------------------------------------------------
# Row types
# --------------------------------------------------------------------------


@dataclass
class Project:
    id: int
    name: str
    created_at: str


@dataclass
class Chat:
    id: int
    project_id: int
    name: str
    current_branch_id: int | None
    created_at: str


@dataclass
class Branch:
    id: int
    chat_id: int
    name: str
    parent_branch_id: int | None
    fork_round_id: int | None
    created_at: str


@dataclass
class LaneHead:
    branch_id: int
    provider: str
    head_message_id: int | None
    native_session_id: str | None
    native_head_id: int | None


@dataclass
class Round:
    id: int
    chat_id: int
    branch_id: int
    seq: int
    parent_round_id: int | None
    kind: str
    prompt: str
    feedback_packet_id: int | None
    edited_from_round_id: int | None
    context: list[Any]
    created_at: str
    completed_at: str | None


@dataclass
class Attempt:
    id: int
    run_id: int
    n: int
    reason: str
    status: RunStatus
    transport: str | None
    billing: str | None
    model_requested: str | None
    model_reported: str | None
    effort: str | None
    native_id: str | None
    client_version: str | None
    input_hash: str | None
    raw_output: str | None
    output_text: str | None
    error_kind: str | None
    error_message: str | None
    started_at: str | None
    ended_at: str | None
    first_token_ms: int | None
    latency_ms: int | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    assistant_message_id: int | None
    meta: dict[str, Any]


@dataclass
class Run:
    """A lane's slot in a round, joined with its current attempt (if any)."""

    id: int
    round_id: int
    provider: str
    status: RunStatus
    skip_reason: str | None
    input_text: str | None
    context_head_id: int | None
    current_attempt_id: int | None
    attempt: Attempt | None = None
    attempt_count: int = 0

    # convenience passthroughs used by renderers and analysis
    def __getattr__(self, name: str) -> Any:
        if name in _ATTEMPT_FIELDS:
            a = self.__dict__.get("attempt")
            if a is None:
                return {} if name == "meta" else None
            return getattr(a, name)
        raise AttributeError(name)


_ATTEMPT_FIELDS = {
    "model_requested", "model_reported", "effort", "native_id", "raw_output", "output_text", "error_kind",
    "error_message", "started_at", "ended_at", "first_token_ms", "latency_ms", "input_tokens",
    "output_tokens", "cost_usd", "assistant_message_id", "meta", "transport", "billing", "client_version", "n",
}


@dataclass
class Analysis:
    id: int
    round_id: int
    kind: str
    judge_provider: str
    judge_model: str | None
    blind: bool
    label_map: dict[str, dict[str, Any]]
    prompt_text: str
    raw_output: str | None
    result: dict[str, Any] | None
    status: str
    error_message: str | None
    input_tokens: int | None
    output_tokens: int | None
    cost_usd: float | None
    latency_ms: int | None
    created_at: str


@dataclass
class FeedbackPacket:
    id: int
    chat_id: int
    round_id: int
    analysis_id: int | None
    disclosure: str
    included: list[str]
    content: str
    instruction: str | None
    status: str
    edited: bool
    sent_round_id: int | None
    created_at: str
    updated_at: str


def _round(row: sqlite3.Row) -> Round:
    d = dict(row)
    d["context"] = json.loads(d.pop("context_json") or "[]")
    return Round(**d)


def _attempt(row: sqlite3.Row) -> Attempt:
    d = dict(row)
    d["status"] = RunStatus(d["status"])
    d["meta"] = json.loads(d.pop("meta_json") or "{}")
    return Attempt(**d)


def _analysis(row: sqlite3.Row) -> Analysis:
    d = dict(row)
    d["blind"] = bool(d["blind"])
    d["label_map"] = json.loads(d.pop("label_map_json") or "{}")
    rj = d.pop("result_json")
    d["result"] = json.loads(rj) if rj else None
    return Analysis(**d)


def _packet(row: sqlite3.Row) -> FeedbackPacket:
    d = dict(row)
    d["included"] = json.loads(d.pop("included_json") or "[]")
    d["edited"] = bool(d["edited"])
    return FeedbackPacket(**d)


_ATTEMPT_UPDATABLE = {
    "status", "transport", "billing", "model_requested", "model_reported", "effort", "native_id",
    "client_version", "input_hash", "raw_output", "output_text", "error_kind", "error_message", "started_at",
    "ended_at", "first_token_ms", "latency_ms", "input_tokens", "output_tokens", "cost_usd",
    "assistant_message_id", "meta",
}


# --------------------------------------------------------------------------
# Migrations
# --------------------------------------------------------------------------


def _migrate_v1_to_v2(db: sqlite3.Connection) -> None:
    """v1 (sessions/conversations/provider_runs) -> v2. IDs are preserved."""
    for t in ("sessions", "session_providers", "conversations", "messages", "rounds", "provider_runs", "analyses"):
        db.execute(f"ALTER TABLE {t} RENAME TO v1_{t}")
    for stmt in SCHEMA_V2.split(";"):  # not executescript(): that would COMMIT mid-migration
        if stmt.strip():
            db.execute(stmt)
    ts = now_iso()
    db.execute("INSERT INTO projects(id, name, created_at) VALUES (1, 'default', ?)", (ts,))
    db.execute("INSERT INTO chats(id, project_id, name, current_branch_id, created_at) "
               "SELECT id, 1, name, id, created_at FROM v1_sessions")
    db.execute("INSERT INTO branches(id, chat_id, name, created_at) SELECT id, id, 'main', created_at FROM v1_sessions")
    db.execute("INSERT INTO lanes(chat_id, provider, enabled, model) "
               "SELECT session_id, provider, enabled, model FROM v1_session_providers")
    db.execute("""INSERT INTO messages(id, chat_id, provider, parent_id, role, content, attempt_id, created_at)
                  SELECT m.id, c.session_id, c.provider, m.parent_id, m.role, m.content, m.run_id, m.created_at
                  FROM v1_messages m JOIN v1_conversations c ON c.id = m.conversation_id""")
    db.execute("""INSERT INTO lane_heads(branch_id, provider, head_message_id, native_session_id, native_head_id)
                  SELECT session_id, provider, head_message_id, native_session_id, native_head_id
                  FROM v1_conversations WHERE active = 1""")
    db.execute("""INSERT INTO rounds(id, chat_id, branch_id, seq, parent_round_id, kind, prompt, context_json,
                                     created_at, completed_at)
                  SELECT id, session_id, session_id, seq, parent_round_id,
                         CASE kind WHEN 'iterate' THEN 'feedback' ELSE kind END, prompt, context_json,
                         created_at, completed_at FROM v1_rounds""")
    status = ("CASE status WHEN 'error' THEN 'failed' WHEN 'cancelled' THEN 'stopped' "
              "WHEN 'pending' THEN 'failed' WHEN 'running' THEN 'failed' ELSE status END")
    db.execute(f"""INSERT INTO runs(id, round_id, provider, status, skip_reason, input_text, context_head_id,
                                    current_attempt_id)
                   SELECT id, round_id, provider, {status}, skip_reason, input_text, context_head_id,
                          CASE status WHEN 'skipped' THEN NULL ELSE id END FROM v1_provider_runs""")
    db.execute(f"""INSERT INTO attempts(id, run_id, n, reason, status, transport, billing, model_requested,
                                        model_reported, native_id, raw_output, output_text, error_kind,
                                        error_message, started_at, ended_at, first_token_ms, latency_ms,
                                        input_tokens, output_tokens, cost_usd, assistant_message_id, meta_json)
                   SELECT id, id, 1, 'initial', {status}, transport,
                          CASE transport WHEN 'api' THEN 'api' WHEN 'fake' THEN 'none' ELSE 'subscription' END,
                          model_requested, model_reported, native_id, output_text, output_text, error_kind,
                          error_message, started_at, ended_at, first_token_ms, latency_ms, input_tokens,
                          output_tokens, cost_usd, assistant_message_id, meta_json
                   FROM v1_provider_runs WHERE status != 'skipped'""")
    db.execute("INSERT INTO analyses SELECT * FROM v1_analyses")
    for t in ("analyses", "provider_runs", "rounds", "messages", "conversations", "session_providers", "sessions"):
        db.execute(f"DROP TABLE v1_{t}")
    cur = db.execute("SELECT value FROM meta WHERE key = 'current_session'").fetchone()
    if cur:
        db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('current_project', 'default')")
        db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('current_chat', ?)", (cur[0],))
        db.execute("DELETE FROM meta WHERE key = 'current_session'")


MIGRATIONS = {2: _migrate_v1_to_v2}


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------


class Store:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        memory = str(path) == ":memory:"
        if not memory:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not self.path.exists():
                fd = os.open(self.path, os.O_CREAT | os.O_WRONLY, 0o600)  # private before SQLite touches it
                os.close(fd)
        self.db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.execute("PRAGMA secure_delete = ON")
        self.db.execute("PRAGMA busy_timeout = 5000")
        if not memory:
            self.db.execute("PRAGMA journal_mode = WAL")
        self._migrate()

    def _migrate(self) -> None:
        has_meta = self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
        if not has_meta:
            self.db.executescript(SCHEMA_V2)
            self.set_meta("schema_version", str(SCHEMA_VERSION))
            return
        version = int(self.get_meta("schema_version") or 1)
        if version > SCHEMA_VERSION:
            raise CockpitError(f"database schema v{version} is newer than this Cockpit (v{SCHEMA_VERSION}); upgrade Cockpit")
        while version < SCHEMA_VERSION:
            version += 1
            self.db.execute("PRAGMA foreign_keys = OFF")
            with self.tx() as db:
                MIGRATIONS[version](db)
                db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES ('schema_version', ?)", (str(version),))
            self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA_V2)  # idempotent: adds any new tables/indexes

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield self.db
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        else:
            self.db.execute("COMMIT")

    def _one(self, sql: str, *args: Any) -> sqlite3.Row | None:
        return self.db.execute(sql, args).fetchone()

    # --- meta -----------------------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self._one("SELECT value FROM meta WHERE key = ?", key)
        return row["value"] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        if value is None:
            self.db.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))

    # --- projects & chats ----------------------------------------------

    def get_project(self, name: str) -> Project | None:
        row = self._one("SELECT * FROM projects WHERE name = ?", name)
        return Project(**dict(row)) if row else None

    def get_or_create_project(self, name: str) -> Project:
        p = self.get_project(name)
        if p:
            return p
        self.db.execute("INSERT INTO projects(name, created_at) VALUES (?, ?)", (name, now_iso()))
        return self.get_project(name)  # type: ignore[return-value]

    def list_projects(self) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT p.*, (SELECT COUNT(*) FROM chats c WHERE c.project_id = p.id) AS chats FROM projects p ORDER BY name"
        ).fetchall()
        return [dict(r) for r in rows]

    def get_chat(self, project_id: int, name: str) -> Chat | None:
        row = self._one("SELECT * FROM chats WHERE project_id = ? AND name = ?", project_id, name)
        return Chat(**dict(row)) if row else None

    def get_chat_by_id(self, chat_id: int) -> Chat:
        row = self._one("SELECT * FROM chats WHERE id = ?", chat_id)
        if not row:
            raise CockpitError(f"chat {chat_id} not found")
        return Chat(**dict(row))

    def create_chat(self, project_id: int, name: str) -> Chat:
        ts = now_iso()
        with self.tx() as db:
            cid = db.execute("INSERT INTO chats(project_id, name, created_at) VALUES (?, ?, ?)",
                             (project_id, name, ts)).lastrowid
            bid = db.execute("INSERT INTO branches(chat_id, name, created_at) VALUES (?, 'main', ?)", (cid, ts)).lastrowid
            db.execute("UPDATE chats SET current_branch_id = ? WHERE id = ?", (bid, cid))
        return self.get_chat_by_id(int(cid))  # type: ignore[arg-type]

    def list_chats(self, project_id: int) -> list[dict[str, Any]]:
        rows = self.db.execute(
            """SELECT c.*, (SELECT COUNT(*) FROM rounds r WHERE r.chat_id = c.id) AS rounds,
                      (SELECT COUNT(*) FROM branches b WHERE b.chat_id = c.id) AS branches,
                      (SELECT MAX(created_at) FROM rounds r WHERE r.chat_id = c.id) AS last_activity
               FROM chats c WHERE c.project_id = ? ORDER BY COALESCE(last_activity, c.created_at) DESC""",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_chat(self, chat_id: int) -> None:
        with self.tx() as db:
            rounds = "SELECT id FROM rounds WHERE chat_id = ?"
            runs = f"SELECT id FROM runs WHERE round_id IN ({rounds})"
            db.execute(f"DELETE FROM attempts WHERE run_id IN ({runs})", (chat_id,))
            db.execute(f"DELETE FROM runs WHERE round_id IN ({rounds})", (chat_id,))
            db.execute(f"DELETE FROM analyses WHERE round_id IN ({rounds})", (chat_id,))
            db.execute(f"DELETE FROM stop_requests WHERE round_id IN ({rounds})", (chat_id,))
            db.execute("DELETE FROM usage_events WHERE chat_id = ?", (chat_id,))
            for t in ("rounds", "messages", "feedback_packets", "lanes"):
                db.execute(f"DELETE FROM {t} WHERE chat_id = ?", (chat_id,))
            db.execute("DELETE FROM lane_heads WHERE branch_id IN (SELECT id FROM branches WHERE chat_id = ?)", (chat_id,))
            db.execute("DELETE FROM branches WHERE chat_id = ?", (chat_id,))
            db.execute("DELETE FROM chats WHERE id = ?", (chat_id,))
        self._checkpoint()

    def delete_project(self, project_id: int) -> None:
        for c in self.list_chats(project_id):
            self.delete_chat(c["id"])
        self.db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        self._checkpoint()

    def _checkpoint(self) -> None:
        if str(self.path) != ":memory:":
            self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # flush deleted pages out of the WAL

    # --- branches -------------------------------------------------------

    def get_branch(self, branch_id: int) -> Branch:
        row = self._one("SELECT * FROM branches WHERE id = ?", branch_id)
        if not row:
            raise CockpitError(f"branch {branch_id} not found")
        return Branch(**dict(row))

    def find_branch(self, chat_id: int, ref: str) -> Branch:
        row = self._one("SELECT * FROM branches WHERE chat_id = ? AND (name = ? OR CAST(id AS TEXT) = ?)", chat_id, ref, ref)
        if not row:
            raise CockpitError(f"no branch {ref!r} in this chat (see `cockpit branches`)")
        return Branch(**dict(row))

    def list_branches(self, chat_id: int) -> list[Branch]:
        return [Branch(**dict(r)) for r in self.db.execute("SELECT * FROM branches WHERE chat_id = ? ORDER BY id", (chat_id,))]

    def create_branch(self, chat_id: int, parent_branch_id: int, fork_round_id: int, name: str | None = None) -> Branch:
        n = self._one("SELECT COUNT(*) AS n FROM branches WHERE chat_id = ?", chat_id)["n"]  # type: ignore[index]
        name = name or f"b{n + 1}"
        bid = self.db.execute(
            "INSERT INTO branches(chat_id, name, parent_branch_id, fork_round_id, created_at) VALUES (?, ?, ?, ?, ?)",
            (chat_id, name, parent_branch_id, fork_round_id, now_iso()),
        ).lastrowid
        return self.get_branch(int(bid))  # type: ignore[arg-type]

    def set_current_branch(self, chat_id: int, branch_id: int) -> None:
        self.db.execute("UPDATE chats SET current_branch_id = ? WHERE id = ?", (branch_id, chat_id))

    def lineage(self, branch_id: int) -> list[Round]:
        """Rounds visible on a branch, oldest first: parent prefix (before fork) + own rounds."""
        b = self.get_branch(branch_id)
        own = [_round(r) for r in self.db.execute("SELECT * FROM rounds WHERE branch_id = ? ORDER BY seq", (b.id,))]
        if b.parent_branch_id is None:
            return own
        fork = self.get_round_by_id(b.fork_round_id) if b.fork_round_id else None
        prefix = [r for r in self.lineage(b.parent_branch_id) if fork is None or r.seq < fork.seq]
        return prefix + own

    # --- lanes ----------------------------------------------------------

    def lane_settings(self, chat_id: int) -> dict[str, dict[str, Any]]:
        rows = self.db.execute("SELECT provider, enabled, model, effort FROM lanes WHERE chat_id = ?", (chat_id,))
        return {r["provider"]: {"enabled": bool(r["enabled"]), "model": r["model"], "effort": r["effort"]} for r in rows}

    def set_lane(self, chat_id: int, provider: str, **fields: Any) -> None:
        allowed = {"enabled", "model", "effort"}
        if set(fields) - allowed:
            raise ValueError(f"unknown lane fields {set(fields) - allowed}")
        self.db.execute("INSERT OR IGNORE INTO lanes(chat_id, provider) VALUES (?, ?)", (chat_id, provider))
        for k, v in fields.items():
            self.db.execute(f"UPDATE lanes SET {k} = ? WHERE chat_id = ? AND provider = ?",
                            (int(v) if k == "enabled" else v, chat_id, provider))

    def get_head(self, branch_id: int, provider: str) -> LaneHead:
        row = self._one("SELECT * FROM lane_heads WHERE branch_id = ? AND provider = ?", branch_id, provider)
        return LaneHead(**dict(row)) if row else LaneHead(branch_id, provider, None, None, None)

    def set_head(self, branch_id: int, provider: str, head_id: int | None,
                 native_session_id: str | None = None, native_head_id: int | None = None) -> None:
        self.db.execute(
            """INSERT INTO lane_heads(branch_id, provider, head_message_id, native_session_id, native_head_id)
               VALUES (?, ?, ?, ?, ?) ON CONFLICT(branch_id, provider) DO UPDATE SET
               head_message_id = excluded.head_message_id, native_session_id = excluded.native_session_id,
               native_head_id = excluded.native_head_id""",
            (branch_id, provider, head_id, native_session_id, native_head_id),
        )

    def history(self, chat_id: int, provider: str, head_id: int | None) -> list[ChatMessage]:
        """Walk parent links from head to root within ONE lane; oldest first."""
        if head_id is None:
            return []
        rows = self.db.execute(
            """WITH RECURSIVE chain(id, parent_id, role, content, depth) AS (
                   SELECT id, parent_id, role, content, 0 FROM messages
                   WHERE id = ? AND chat_id = ? AND provider = ?
                   UNION ALL
                   SELECT m.id, m.parent_id, m.role, m.content, c.depth + 1
                   FROM messages m JOIN chain c ON m.id = c.parent_id
                   WHERE m.chat_id = ? AND m.provider = ?
               ) SELECT role, content FROM chain ORDER BY depth DESC""",
            (head_id, chat_id, provider, chat_id, provider),
        ).fetchall()
        return [ChatMessage(r["role"], r["content"]) for r in rows]

    def commit_exchange(
        self, *, attempt_id: int, run_id: int, chat_id: int, provider: str, branch_id: int,
        expected_head: int | None, parent_id: int | None, user_text: str, assistant_text: str,
        native_session_id: str | None,
    ) -> int | None:
        """Append (user, assistant) under ``parent_id`` and move the lane head there.

        Compare-and-swap on the lane head: if it is no longer ``expected_head``
        (another process advanced this lane), nothing is written.
        """
        ts = now_iso()
        with self.tx() as db:
            if self.get_head(branch_id, provider).head_message_id != expected_head:
                return None
            u = db.execute(
                "INSERT INTO messages(chat_id, provider, parent_id, role, content, attempt_id, created_at) VALUES (?, ?, ?, 'user', ?, ?, ?)",
                (chat_id, provider, parent_id, user_text, attempt_id, ts),
            ).lastrowid
            a = db.execute(
                "INSERT INTO messages(chat_id, provider, parent_id, role, content, attempt_id, created_at) VALUES (?, ?, ?, 'assistant', ?, ?, ?)",
                (chat_id, provider, u, assistant_text, attempt_id, ts),
            ).lastrowid
            self.set_head(branch_id, provider, a, native_session_id, a if native_session_id else None)
            db.execute("UPDATE attempts SET assistant_message_id = ?, status = 'complete' WHERE id = ?", (a, attempt_id))
            db.execute("UPDATE runs SET current_attempt_id = ?, status = 'complete' WHERE id = ?", (attempt_id, run_id))
        return int(a)  # type: ignore[arg-type]

    # --- rounds, runs, attempts ----------------------------------------

    def create_round(self, chat_id: int, branch_id: int, kind: str, prompt: str, *,
                     feedback_packet_id: int | None = None, edited_from_round_id: int | None = None,
                     context: list[Any] | None = None) -> Round:
        with self.tx() as db:
            seq = (db.execute("SELECT MAX(seq) FROM rounds WHERE chat_id = ?", (chat_id,)).fetchone()[0] or 0) + 1
            lin = self.lineage(branch_id)
            rid = db.execute(
                """INSERT INTO rounds(chat_id, branch_id, seq, parent_round_id, kind, prompt, feedback_packet_id,
                                      edited_from_round_id, context_json, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (chat_id, branch_id, seq, lin[-1].id if lin else None, kind, prompt, feedback_packet_id,
                 edited_from_round_id, json.dumps(context or []), now_iso()),
            ).lastrowid
        return self.get_round_by_id(int(rid))  # type: ignore[arg-type]

    def complete_round(self, round_id: int) -> None:
        self.db.execute("UPDATE rounds SET completed_at = ? WHERE id = ?", (now_iso(), round_id))

    def get_round_by_id(self, round_id: int) -> Round:
        row = self._one("SELECT * FROM rounds WHERE id = ?", round_id)
        if not row:
            raise CockpitError(f"round id {round_id} not found")
        return _round(row)

    def get_round(self, chat_id: int, seq: int) -> Round:
        row = self._one("SELECT * FROM rounds WHERE chat_id = ? AND seq = ?", chat_id, seq)
        if not row:
            raise CockpitError(f"round {seq} not found in this chat")
        return _round(row)

    def list_rounds(self, chat_id: int) -> list[Round]:
        return [_round(r) for r in self.db.execute("SELECT * FROM rounds WHERE chat_id = ? ORDER BY seq", (chat_id,))]

    def add_run(self, round_id: int, provider: str, *, status: RunStatus, skip_reason: str | None = None,
                input_text: str | None = None, context_head_id: int | None = None) -> int:
        return int(self.db.execute(
            "INSERT INTO runs(round_id, provider, status, skip_reason, input_text, context_head_id) VALUES (?, ?, ?, ?, ?, ?)",
            (round_id, provider, status.value, skip_reason, input_text, context_head_id),
        ).lastrowid)  # type: ignore[arg-type]

    def set_run_status(self, run_id: int, status: RunStatus, current_attempt_id: int | None = None) -> None:
        if current_attempt_id is None:
            self.db.execute("UPDATE runs SET status = ? WHERE id = ?", (status.value, run_id))
        else:
            self.db.execute("UPDATE runs SET status = ?, current_attempt_id = ? WHERE id = ?",
                            (status.value, current_attempt_id, run_id))

    def add_attempt(self, run_id: int, reason: str, **fields: Any) -> Attempt:
        with self.tx() as db:
            n = (db.execute("SELECT MAX(n) FROM attempts WHERE run_id = ?", (run_id,)).fetchone()[0] or 0) + 1
            aid = db.execute("INSERT INTO attempts(run_id, n, reason, status) VALUES (?, ?, ?, ?)",
                             (run_id, n, reason, RunStatus.QUEUED.value)).lastrowid
        if fields:
            self.update_attempt(int(aid), **fields)  # type: ignore[arg-type]
        return self.get_attempt(int(aid))  # type: ignore[arg-type]

    def update_attempt(self, attempt_id: int, **fields: Any) -> None:
        bad = set(fields) - _ATTEMPT_UPDATABLE
        if bad:
            raise ValueError(f"unknown attempt fields: {bad}")
        cols, vals = [], []
        for k, v in fields.items():
            if k == "meta":
                k, v = "meta_json", json.dumps(v)
            elif isinstance(v, RunStatus):
                v = v.value
            cols.append(f"{k} = ?")
            vals.append(v)
        self.db.execute(f"UPDATE attempts SET {', '.join(cols)} WHERE id = ?", (*vals, attempt_id))

    def get_attempt(self, attempt_id: int) -> Attempt:
        row = self._one("SELECT * FROM attempts WHERE id = ?", attempt_id)
        if not row:
            raise CockpitError(f"attempt {attempt_id} not found")
        return _attempt(row)

    def attempts_for_run(self, run_id: int) -> list[Attempt]:
        return [_attempt(r) for r in self.db.execute("SELECT * FROM attempts WHERE run_id = ? ORDER BY n", (run_id,))]

    def _run(self, row: sqlite3.Row) -> Run:
        d = dict(row)
        d["status"] = RunStatus(d["status"])
        run = Run(**d)
        if run.current_attempt_id:
            run.attempt = self.get_attempt(run.current_attempt_id)
        run.attempt_count = self._one("SELECT COUNT(*) AS n FROM attempts WHERE run_id = ?", run.id)["n"]  # type: ignore[index]
        return run

    def get_run(self, run_id: int) -> Run:
        row = self._one("SELECT * FROM runs WHERE id = ?", run_id)
        if not row:
            raise CockpitError(f"run {run_id} not found")
        return self._run(row)

    def run_for(self, round_id: int, provider: str) -> Run | None:
        row = self._one("SELECT * FROM runs WHERE round_id = ? AND provider = ?", round_id, provider)
        return self._run(row) if row else None

    def runs_for_round(self, round_id: int) -> list[Run]:
        return [self._run(r) for r in self.db.execute("SELECT * FROM runs WHERE round_id = ? ORDER BY id", (round_id,))]

    def find_identical(self, chat_id: int, input_hash: str) -> tuple[int, str] | None:
        row = self._one(
            """SELECT r.seq, ru.provider FROM attempts a JOIN runs ru ON ru.id = a.run_id JOIN rounds r ON r.id = ru.round_id
               WHERE r.chat_id = ? AND a.input_hash = ? AND a.status = 'complete' ORDER BY a.id DESC LIMIT 1""",
            chat_id, input_hash,
        )
        return (row["seq"], row["provider"]) if row else None

    # --- analyses -------------------------------------------------------

    def save_analysis(self, **f: Any) -> Analysis:
        cur = self.db.execute(
            """INSERT INTO analyses(round_id, kind, judge_provider, judge_model, blind, label_map_json, prompt_text,
                                    raw_output, result_json, status, error_message, input_tokens, output_tokens,
                                    cost_usd, latency_ms, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (f["round_id"], f["kind"], f["judge_provider"], f.get("judge_model"), int(f["blind"]),
             json.dumps(f["label_map"]), f["prompt_text"], f.get("raw_output"),
             json.dumps(f["result"]) if f.get("result") is not None else None, f["status"], f.get("error_message"),
             f.get("input_tokens"), f.get("output_tokens"), f.get("cost_usd"), f.get("latency_ms"), now_iso()),
        )
        return self.get_analysis(int(cur.lastrowid))  # type: ignore[arg-type]

    def get_analysis(self, analysis_id: int) -> Analysis:
        row = self._one("SELECT * FROM analyses WHERE id = ?", analysis_id)
        if not row:
            raise CockpitError(f"analysis {analysis_id} not found")
        return _analysis(row)

    def analyses_for_round(self, round_id: int, kind: str | None = None) -> list[Analysis]:
        sql, args = "SELECT * FROM analyses WHERE round_id = ?", [round_id]
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        return [_analysis(r) for r in self.db.execute(sql + " ORDER BY id", args)]

    # --- feedback packets ----------------------------------------------

    def create_packet(self, *, chat_id: int, round_id: int, analysis_id: int | None, disclosure: str,
                      included: list[str], content: str, instruction: str | None) -> FeedbackPacket:
        ts = now_iso()
        pid = self.db.execute(
            """INSERT INTO feedback_packets(chat_id, round_id, analysis_id, disclosure, included_json, content,
                                            instruction, status, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)""",
            (chat_id, round_id, analysis_id, disclosure, json.dumps(included), content, instruction, ts, ts),
        ).lastrowid
        return self.get_packet(int(pid))  # type: ignore[arg-type]

    def get_packet(self, packet_id: int) -> FeedbackPacket:
        row = self._one("SELECT * FROM feedback_packets WHERE id = ?", packet_id)
        if not row:
            raise CockpitError(f"feedback packet {packet_id} not found")
        return _packet(row)

    def update_packet(self, packet_id: int, **fields: Any) -> None:
        allowed = {"content", "instruction", "status", "sent_round_id", "edited"}
        if set(fields) - allowed:
            raise ValueError("bad packet fields")
        fields["updated_at"] = now_iso()
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.db.execute(f"UPDATE feedback_packets SET {cols} WHERE id = ?", (*fields.values(), packet_id))

    def list_packets(self, chat_id: int) -> list[FeedbackPacket]:
        return [_packet(r) for r in self.db.execute("SELECT * FROM feedback_packets WHERE chat_id = ? ORDER BY id", (chat_id,))]

    # --- usage / billing ------------------------------------------------

    def add_usage(self, **f: Any) -> None:
        self.db.execute(
            """INSERT INTO usage_events(chat_id, attempt_id, analysis_id, provider, transport, billing, model,
                                        input_tokens, output_tokens, cost_usd, meta_json, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (f.get("chat_id"), f.get("attempt_id"), f.get("analysis_id"), f["provider"], f.get("transport"),
             f["billing"], f.get("model"), f.get("input_tokens"), f.get("output_tokens"), f.get("cost_usd"),
             json.dumps(f.get("meta") or {}), now_iso()),
        )

    def api_spend(self, month: str | None = None, provider: str | None = None) -> float:
        month = month or datetime.now(timezone.utc).strftime("%Y-%m")
        sql = "SELECT COALESCE(SUM(cost_usd), 0) FROM usage_events WHERE billing = 'api' AND substr(created_at, 1, 7) = ?"
        args: list[Any] = [month]
        if provider:
            sql += " AND provider = ?"
            args.append(provider)
        return float(self.db.execute(sql, args).fetchone()[0])

    def api_unpriced_runs(self, month: str | None = None) -> int:
        month = month or datetime.now(timezone.utc).strftime("%Y-%m")
        return int(self.db.execute(
            "SELECT COUNT(*) FROM usage_events WHERE billing = 'api' AND cost_usd IS NULL AND substr(created_at, 1, 7) = ?",
            (month,)).fetchone()[0])

    def latest_usage_meta(self, provider: str, key: str) -> dict[str, Any] | None:
        for row in self.db.execute("SELECT meta_json, created_at FROM usage_events WHERE provider = ? ORDER BY id DESC LIMIT 20", (provider,)):
            meta = json.loads(row["meta_json"] or "{}")
            if key in meta:
                return {**meta[key], "observed_at": row["created_at"]}
        return None

    # --- stop requests --------------------------------------------------

    def request_stop(self, round_id: int, provider: str) -> None:
        self.db.execute("INSERT INTO stop_requests(round_id, provider, created_at) VALUES (?, ?, ?)", (round_id, provider, now_iso()))

    def pending_stops(self, round_id: int) -> list[tuple[int, str]]:
        return [(r["id"], r["provider"]) for r in self.db.execute(
            "SELECT id, provider FROM stop_requests WHERE round_id = ? AND handled_at IS NULL", (round_id,))]

    def mark_stop_handled(self, stop_id: int) -> None:
        self.db.execute("UPDATE stop_requests SET handled_at = ? WHERE id = ?", (now_iso(), stop_id))

    def waiting_run(self, chat_id: int, provider: str) -> tuple[Round, Run, Attempt] | None:
        """Latest manual-lane attempt still waiting for the user's pasted answer."""
        row = self._one(
            """SELECT r.id AS round_id, ru.id AS run_id, a.id AS attempt_id FROM attempts a
               JOIN runs ru ON ru.id = a.run_id JOIN rounds r ON r.id = ru.round_id
               WHERE r.chat_id = ? AND ru.provider = ? AND a.status = 'waiting' ORDER BY a.id DESC LIMIT 1""",
            chat_id, provider)
        if not row:
            return None
        return self.get_round_by_id(row["round_id"]), self.get_run(row["run_id"]), self.get_attempt(row["attempt_id"])

    def running_runs(self, chat_id: int) -> list[tuple[Round, Run]]:
        rows = self.db.execute(
            """SELECT ru.id AS run_id, r.id AS round_id FROM runs ru JOIN rounds r ON r.id = ru.round_id
               WHERE r.chat_id = ? AND ru.status IN ('running', 'queued') ORDER BY r.seq DESC""", (chat_id,)).fetchall()
        return [(self.get_round_by_id(r["round_id"]), self.get_run(r["run_id"])) for r in rows]
