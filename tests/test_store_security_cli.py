"""Migrations, storage security, secrets, and the CLI end to end (demo lanes, no network)."""

import json
import os
import sqlite3
import stat
import sys

import pytest

from cockpit.cli import main
from cockpit.providers.base import ProviderError, validate_model_name
from cockpit.secrets import SecretResolver, redact
from cockpit.store import SCHEMA_VERSION, Store

V1_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE sessions (id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL, meta_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE session_providers (session_id INTEGER NOT NULL, provider TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, model TEXT, PRIMARY KEY (session_id, provider));
CREATE TABLE conversations (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, provider TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1, head_message_id INTEGER, native_session_id TEXT, native_head_id INTEGER, created_at TEXT NOT NULL);
CREATE TABLE messages (id INTEGER PRIMARY KEY, conversation_id INTEGER NOT NULL, parent_id INTEGER, role TEXT NOT NULL, content TEXT NOT NULL, run_id INTEGER, created_at TEXT NOT NULL);
CREATE TABLE rounds (id INTEGER PRIMARY KEY, session_id INTEGER NOT NULL, seq INTEGER NOT NULL, parent_round_id INTEGER, kind TEXT NOT NULL, prompt TEXT NOT NULL, source_analysis_id INTEGER, context_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL, completed_at TEXT);
CREATE TABLE provider_runs (id INTEGER PRIMARY KEY, round_id INTEGER NOT NULL, provider TEXT NOT NULL, status TEXT NOT NULL, skip_reason TEXT, conversation_id INTEGER, context_head_id INTEGER, assistant_message_id INTEGER, input_text TEXT, output_text TEXT, error_kind TEXT, error_message TEXT, transport TEXT, model_requested TEXT, model_reported TEXT, native_id TEXT, attempts INTEGER NOT NULL DEFAULT 0, started_at TEXT, ended_at TEXT, first_token_ms INTEGER, latency_ms INTEGER, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, meta_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE analyses (id INTEGER PRIMARY KEY, round_id INTEGER NOT NULL, kind TEXT NOT NULL, judge_provider TEXT NOT NULL, judge_model TEXT, blind INTEGER NOT NULL, label_map_json TEXT NOT NULL, prompt_text TEXT NOT NULL, raw_output TEXT, result_json TEXT, status TEXT NOT NULL, error_message TEXT, input_tokens INTEGER, output_tokens INTEGER, cost_usd REAL, latency_ms INTEGER, created_at TEXT NOT NULL);
INSERT INTO meta VALUES ('schema_version', '1'), ('current_session', 'iris');
INSERT INTO sessions VALUES (1, 'iris', '2026-11-01T00:00:00', '{}');
INSERT INTO session_providers VALUES (1, 'grok', 0, NULL);
INSERT INTO conversations VALUES (1, 1, 'claude', 1, 2, NULL, NULL, '2026-11-01T00:00:00');
INSERT INTO messages VALUES (1, 1, NULL, 'user', 'v1 question', 1, 't'), (2, 1, 1, 'assistant', 'v1 answer', 1, 't');
INSERT INTO rounds VALUES (1, 1, 1, NULL, 'blast', 'v1 question', NULL, '[]', 't', 't'), (2, 1, 2, 1, 'iterate', 'revise', NULL, '[]', 't', 't');
INSERT INTO provider_runs (id, round_id, provider, status, conversation_id, context_head_id, assistant_message_id, input_text, output_text, transport, model_requested)
  VALUES (1, 1, 'claude', 'complete', 1, NULL, 2, 'v1 question', 'v1 answer', 'api', 'claude-opus-5-5'),
         (2, 1, 'grok', 'skipped', NULL, NULL, NULL, NULL, NULL, NULL, NULL),
         (3, 2, 'claude', 'error', 1, 2, NULL, 'revise', NULL, 'api', 'claude-opus-5-5');
INSERT INTO analyses VALUES (1, 1, 'adjudicate', 'claude', 'm', 1, '{}', 'p', 'r', NULL, 'complete', NULL, NULL, NULL, NULL, NULL, 't');
"""


def test_v1_database_migrates_to_v2(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript(V1_SCHEMA)
    con.close()
    s = Store(db)
    assert s.get_meta("schema_version") == str(SCHEMA_VERSION)
    chat = s.get_chat(s.get_project("default").id, "iris")
    assert chat and s.get_meta("current_chat") == "iris"
    assert s.lane_settings(chat.id)["grok"]["enabled"] is False
    head = s.get_head(chat.current_branch_id, "claude").head_message_id
    assert [m.content for m in s.history(chat.id, "claude", head)] == ["v1 question", "v1 answer"]
    r1, r2 = s.list_rounds(chat.id)
    assert r2.kind == "feedback"
    runs = {r.provider: r for r in s.runs_for_round(r1.id)}
    assert runs["claude"].output_text == "v1 answer" and runs["claude"].billing == "api"
    assert runs["grok"].status.value == "skipped" and runs["grok"].attempt is None
    assert s.runs_for_round(r2.id)[0].status.value == "failed"
    assert s.analyses_for_round(r1.id)[0].kind == "adjudicate"
    Store(db)  # reopening is a no-op


def test_newer_schema_refused(tmp_path):
    s = Store(tmp_path / "x.db")
    s.set_meta("schema_version", "99")
    s.close()
    from cockpit.domain import CockpitError

    with pytest.raises(CockpitError, match="newer"):
        Store(tmp_path / "x.db")


def test_database_is_private(tmp_path):
    os.umask(0o022)
    Store(tmp_path / "sub" / "c.db")
    assert stat.S_IMODE(os.stat(tmp_path / "sub" / "c.db").st_mode) == 0o600
    assert stat.S_IMODE(os.stat(tmp_path / "sub").st_mode) == 0o700


@pytest.mark.parametrize("bad", ["../../etc/passwd", "gpt 5", "a;rm -rf", "", "x" * 200, "$(id)"])
def test_model_names_validated(bad):
    with pytest.raises(ProviderError):
        validate_model_name(bad)


def test_secrets():
    r = SecretResolver(env={"B": "sk-ant-abcdefghijklmnopqrstu"})
    assert r.resolve("claude", ["A", "B"]) == "sk-ant-abcdefghijklmnopqrstu"
    assert "abcdefghijkl" not in redact("key sk-ant-abcdefghijklmnopqrstu inside")
    with pytest.raises(ValueError):
        SecretResolver(env={}).resolve("x", [], "op read secret")  # type: ignore[arg-type]
    assert SecretResolver(env={}).resolve("y", [], [sys.executable, "-c", "print('key-123456789')"]) == "key-123456789"


# ---------------------------------------------------------------- CLI end to end


@pytest.fixture
def cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("COCKPIT_HOME", str(tmp_path))
    monkeypatch.delenv("COCKPIT_DEMO", raising=False)

    def run(*argv):
        code = main(["--demo", "--plain", *argv])
        out = capsys.readouterr()
        return code, out.out, out.err

    return run


def last_json(out):
    return json.loads(out.strip().splitlines()[-1])


def test_cli_v1_success_loop(cli, tmp_path):
    assert cli("project", "create", "iris")[0] == 0
    assert cli("chat", "use", "Bolivia Treasury")[0] == 0
    code, out, err = cli("blast", "Best on/off-ramp structures for Bolivia?")
    assert code == 0 and "=== Claude" in out and "=== Perplexity" in out
    assert cli("mute", "grok", "perplexity")[0] == 0
    code, out, err = cli("followup", "Assume direct banking relationships.")
    assert code == 0 and "skipped (muted)" in err
    st = last_json(cli("--json", "status")[1])
    assert (st["project"], st["chat"], st["branch"], st["round"]) == ("iris", "Bolivia Treasury", "main", 2)
    assert {l["provider"]: l["enabled"] for l in st["lanes"]}["grok"] is False and st["api"]["enabled"] is False

    a = last_json(cli("--json", "adjudicate", "--judge", "openai", "--reveal")[1])
    assert a["status"] == "complete" and a["blind"] and len(a["label_map"]) == 2  # gemini is off by default
    assert a["result"]["challenge"]["strongest_case_against"]

    pk = last_json(cli("--json", "feedback", "draft", "--analysis", str(a["id"]))[1])
    assert pk["disclosure"] == "anonymized" and pk["status"] == "draft"
    f = tmp_path / "edited.md"
    f.write_text("EDITED PACKET TEXT")
    assert cli("feedback", "edit", str(pk["id"]), "--file", str(f), "--instruction", "Revise.")[0] == 0
    code, out, err = cli("feedback", "send", str(pk["id"]), "--exclude", "gemini")
    assert code == 0 and "excluded (--exclude)" in err
    sent = last_json(cli("--json", "show", "--provider", "claude")[1])
    assert sent["runs"][0]["input_text"] == "EDITED PACKET TEXT\n\nUSER'S NEXT INSTRUCTION\nRevise."

    code, out, err = cli("rerun", "claude")
    assert code == 0
    att = json.loads(cli("--json", "attempts", "claude")[1])
    assert [x["current"] for x in att] == [False, True]

    code, out, err = cli("edit", "2", "Assume NO banking relationships.", "--only", "claude,openai")
    assert code == 0 and "created branch b2" in err
    rounds = json.loads(cli("--json", "rounds")[1])
    assert [r["round"] for r in rounds] == [1, 4]
    assert len(json.loads(cli("--json", "rounds", "--all")[1])) == 4
    assert cli("branch", "use", "main")[0] == 0 and [r["round"] for r in json.loads(cli("--json", "rounds")[1])] == [1, 2, 3]

    assert cli("export", "--dir", str(tmp_path / "out"))[0] == 0
    base = tmp_path / "out" / "iris" / "chats" / "Bolivia Treasury"
    assert (base / "metadata.json").exists() and (base / "rounds" / "001.md").exists()
    assert stat.S_IMODE(os.stat(base / "metadata.json").st_mode) == 0o600

    assert cli("chat", "delete", "Bolivia Treasury", "--yes")[0] == 0
    db = Store(tmp_path / "demo.db")
    for t in ("rounds", "runs", "attempts", "messages", "analyses", "feedback_packets", "lane_heads", "lanes", "usage_events"):
        assert db.db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] == 0, t
    # only the project's empty default chat (and its main branch) remain
    assert db.db.execute("SELECT name FROM chats").fetchall()[0][0] == "main"
    assert db.db.execute("SELECT COUNT(*) FROM branches").fetchone()[0] == 1


def test_cli_api_commands(cli):
    code, _, err = cli("api", "enable")
    assert code == 1 and "hard monthly limit" in err
    assert cli("api", "enable", "--limit", "20")[0] == 0
    s = last_json(cli("--json", "api")[1])
    assert s == {"enabled": True, "mode": "per-run", "limit": 20.0, "spent": 0.0, "unpriced": 0}
    assert cli("api", "disable")[0] == 0 and last_json(cli("--json", "api")[1])["enabled"] is False


def test_cli_iterate_never_sends_without_review(cli):
    cli("blast", "Q")
    code, out, err = cli("iterate", "Revise please.")
    assert code == 0 and "feedback send" in out
    rounds = json.loads(cli("--json", "rounds")[1])
    assert len(rounds) == 1  # nothing was sent


def test_cli_errors_friendly(cli):
    cli("blast", "x", "--only", "claude")
    code, _, err = cli("adjudicate")
    assert code == 1 and "needs at least 2" in err
    code, _, err = cli("blast", "x", "--only", "codex")
    assert code == 1 and "transport" in err
    code, _, err = cli("stop", "claude")
    assert code == 1


def test_shell_translation():
    from cockpit.shell import to_argv

    assert to_argv("what about -x?") == ["followup", "--", "what about -x?"]
    assert to_argv("/only claude,gemini dig") == ["followup", "--only", "claude,gemini", "--", "dig"]
    assert to_argv("/lane claude hello there") == ["lane", "prompt", "claude", "--", "hello there"]
    assert to_argv("/edit 3 new words") == ["edit", "3", "--", "new words"]
    assert to_argv("/pause grok") == ["mute", "grok"]
    assert to_argv("/quit") is None
