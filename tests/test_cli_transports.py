"""Subscription CLI transports end to end, through the engine, with fake binaries.

Verifies: prompt never in argv when stdin is possible, neutral hardening flags,
dedicated empty working dir, API-key env vars stripped (no silent API
billing), native-session resume only when in sync, fallback replay when the
vendor lost the session, ephemeral judge runs.
"""

import asyncio
import json
import os
import stat
import sys
import textwrap

import pytest

from cockpit.config import load_config
from cockpit.domain import AnalysisKind, RunStatus
from cockpit.engine import Cockpit, Selection
from cockpit.store import Store

FAKE = textwrap.dedent(f"""\
    #!{sys.executable}
    import json, os, sys, uuid
    name = os.path.basename(sys.argv[0])
    argv = sys.argv[1:]
    stdin = "" if sys.stdin.isatty() else sys.stdin.read()
    if "--prompt-file" in argv:
        stdin = open(argv[argv.index("--prompt-file") + 1]).read()
    env = {{k: os.environ.get(k) for k in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "XAI_API_KEY")}}
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps({{"name": name, "argv": argv, "stdin": stdin, "cwd": os.getcwd(), "env": env}}) + "\\n")
    lose = os.environ.get("FAKE_LOSE")
    if name == "claude":
        resume = argv[argv.index("--resume") + 1] if "--resume" in argv else None
        if resume and lose:
            print(json.dumps({{"type": "result", "subtype": "error_during_execution", "is_error": True,
                              "result": "No conversation found with session ID: " + resume}})); sys.exit(1)
        sid = resume or str(uuid.uuid4())
        print(json.dumps({{"type": "system", "subtype": "init", "session_id": sid, "model": "claude-opus-5-5",
                          "claude_code_version": "9.9.9"}}))
        for w in ["Hi", " there"]:
            print(json.dumps({{"type": "stream_event", "session_id": sid,
                              "event": {{"type": "content_block_delta", "delta": {{"type": "text_delta", "text": w}}}}}}), flush=True)
        print(json.dumps({{"type": "result", "subtype": "success", "is_error": False, "result": "Hi there", "session_id": sid,
                          "total_cost_usd": 0.01, "usage": {{"input_tokens": 3, "output_tokens": 2}}}}))
    elif name == "codex":
        resume = argv[argv.index("resume") + 1] if "resume" in argv else None
        tid = resume or str(uuid.uuid4())
        print(json.dumps({{"type": "thread.started", "thread_id": tid}}))
        print(json.dumps({{"type": "turn.started"}}))
        print(json.dumps({{"type": "item.completed", "item": {{"type": "agent_message", "text": "codex says hi"}}}}))
        print(json.dumps({{"type": "turn.completed", "usage": {{"input_tokens": 5, "output_tokens": 3}}}}))
    else:  # grok streaming-json (format captured from grok 1.0.77)
        sid = argv[argv.index("--resume") + 1] if "--resume" in argv else str(uuid.uuid4())
        print(json.dumps({{"type": "available_commands", "tools": ["read_file"]}}))
        print(json.dumps({{"type": "thought", "data": "HIDDEN REASONING"}}))
        for w in ["grok", " says", " hi"]:
            print(json.dumps({{"type": "text", "data": w}}), flush=True)
        print(json.dumps({{"type": "end", "stopReason": "end_turn", "sessionId": sid, "total_cost_usd": 0.0095,
                          "usage": {{"input_tokens": 9, "output_tokens": 3}}, "modelUsage": {{"grok-4.7-build": {{}}}}}}))
""")


@pytest.fixture
def env(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name in ("claude", "codex", "grok"):
        b = bindir / name
        b.write_text(FAKE)
        b.chmod(b.stat().st_mode | stat.S_IEXEC)
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ['PATH']}")
    for k, v in (("ANTHROPIC_API_KEY", "sk-ant-should-not-leak-0000"), ("OPENAI_API_KEY", "sk-should-not-leak-0000"),
                 ("XAI_API_KEY", "xai-should-not-leak-00000000")):
        monkeypatch.setenv(k, v)
    config = load_config(home=tmp_path)
    config.providers = {k: v for k, v in config.providers.items() if k in ("claude", "openai", "grok")}
    engine = Cockpit(config, Store(tmp_path / "db.sqlite"))
    ctx = engine.ctx("p", "c")

    def calls(name=None):
        rows = [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []
        return [r for r in rows if name is None or r["name"] == name]

    return engine, ctx, calls, tmp_path


DANGEROUS = 'Explain: "; rm -rf ~ ; $(whoami) `id` && echo pwned'


def test_all_three_cli_lanes_complete_with_subscription_billing(env):
    engine, ctx, calls, home = env
    res = asyncio.run(engine.send(ctx, DANGEROUS, fresh=True))
    by = {r.provider: r for r in res.runs}
    assert all(r.status == RunStatus.COMPLETE for r in res.runs), [(r.provider, r.error_message) for r in res.runs]
    assert by["claude"].output_text == "Hi there" and by["openai"].output_text == "codex says hi"
    assert by["grok"].output_text == "grok says hi" and "HIDDEN" not in by["grok"].raw_output
    assert by["grok"].model_reported == "grok-4.7-build" and by["grok"].meta["api_equivalent_usd"] == 0.0095
    assert {r.billing for r in res.runs} == {"subscription"} and all(r.cost_usd is None for r in res.runs)
    assert by["claude"].client_version == "9.9.9" and by["claude"].meta["api_equivalent_usd"] == 0.01


def test_api_keys_stripped_so_cli_cannot_bill_api(env):
    engine, ctx, calls, _ = env
    asyncio.run(engine.send(ctx, "hi", fresh=True))
    assert calls("claude")[0]["env"]["ANTHROPIC_API_KEY"] is None
    assert calls("codex")[0]["env"]["OPENAI_API_KEY"] is None
    assert calls("grok")[0]["env"]["XAI_API_KEY"] is None


def test_prompt_handling_and_hardening(env):
    engine, ctx, calls, home = env
    asyncio.run(engine.send(ctx, DANGEROUS, fresh=True))
    c = calls("claude")[0]
    assert c["stdin"] == DANGEROUS and all("whoami" not in a for a in c["argv"])
    # web search on by default: only web tools, pre-approved, bounded turns; nothing else (no shell, no files)
    assert c["argv"][c["argv"].index("--tools") + 1] == "WebSearch,WebFetch" and "--strict-mcp-config" in c["argv"]
    assert c["argv"][c["argv"].index("--allowedTools") + 1] == "WebSearch WebFetch"
    assert c["argv"][c["argv"].index("--max-turns") + 1] == "8"
    assert c["argv"][c["argv"].index("--system-prompt") + 1] == "You are a helpful assistant."
    assert c["cwd"] == str(home / "workspaces" / "claude")
    x = calls("codex")[0]
    assert x["stdin"] == DANGEROUS and x["argv"][-1] == "-"
    assert x["argv"][0] == "--search" and x["argv"][1] == "exec"  # global flag must precede `exec`
    assert x["argv"][x["argv"].index("-s") + 1] == "read-only" and x["argv"][x["argv"].index("-C") + 1] == str(home / "workspaces" / "openai")
    assert "-m" not in x["argv"]  # model "default": Codex picks (and reports) its own default
    g = calls("grok")[0]
    assert g["stdin"] == DANGEROUS and all("whoami" not in a for a in g["argv"])  # prompt via private file
    assert g["argv"][g["argv"].index("--tools") + 1] == "web_search,web_fetch" and "--disable-web-search" not in g["argv"]
    assert g["argv"][g["argv"].index("--allow") + 1] == "web_fetch"  # pre-approved: headless runs can't ask
    assert g["cwd"] == str(home / "workspaces" / "grok")
    assert not list((home / "workspaces" / ".prompts").glob("*"))  # prompt file removed afterwards


def test_native_resume_when_in_sync_and_effort(env):
    engine, ctx, calls, _ = env
    engine.set_effort(ctx, "claude", "high")
    engine.set_effort(ctx, "openai", "low")
    asyncio.run(engine.send(ctx, "first", fresh=True))
    asyncio.run(engine.send(ctx, "second", fresh=False))
    c, x, g = calls("claude")[1], calls("codex")[1], calls("grok")[1]
    first_sid = engine.store.get_head(ctx.branch.id, "claude").native_session_id
    assert "--resume" in c["argv"] and c["stdin"] == "second" and c["argv"][c["argv"].index("--effort") + 1] == "high"
    assert "resume" in x["argv"] and x["stdin"] == "second" and 'model_reasoning_effort="low"' in x["argv"]
    assert "--resume" in g["argv"] and g["stdin"] == "second"
    assert first_sid


def test_lost_native_session_falls_back_to_canonical_transcript(env, monkeypatch):
    engine, ctx, calls, _ = env
    asyncio.run(engine.send(ctx, "first", fresh=True, selection=Selection(only=["claude"])))
    monkeypatch.setenv("FAKE_LOSE", "1")
    res = asyncio.run(engine.send(ctx, "second", fresh=False, selection=Selection(only=["claude"])))
    assert next(r for r in res.runs if r.provider == "claude").status == RunStatus.COMPLETE
    replay = calls("claude")[-1]
    assert "--resume" not in replay["argv"] and "first" in replay["stdin"] and replay["stdin"].endswith("second")


def test_judge_runs_are_ephemeral(env):
    engine, ctx, calls, _ = env
    asyncio.run(engine.send(ctx, "q", fresh=True))
    head = engine.store.get_head(ctx.branch.id, "claude")
    asyncio.run(engine.analyze(ctx, AnalysisKind.SYNTHESIS, judge="claude"))
    asyncio.run(engine.analyze(ctx, AnalysisKind.SYNTHESIS, judge="openai"))
    assert "--no-session-persistence" in calls("claude")[-1]["argv"] and "--resume" not in calls("claude")[-1]["argv"]
    assert "--ephemeral" in calls("codex")[-1]["argv"] and "resume" not in calls("codex")[-1]["argv"]
    assert engine.store.get_head(ctx.branch.id, "claude") == head


def test_missing_cli_reported_not_faked(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))
    config = load_config(home=tmp_path)
    config.providers = {k: v for k, v in config.providers.items() if k in ("gemini", "grok")}
    config.providers["gemini"].enabled_by_default = True
    engine = Cockpit(config, Store(tmp_path / "db.sqlite"))
    res = asyncio.run(engine.send(engine.ctx("p", "c"), "q", fresh=True))
    assert {r.error_kind for r in res.runs} == {"not_installed"}
    report = asyncio.run(engine.adapter("gemini").health_check())
    assert report.installed is False and report.verified_preset is False and report.remediation


def test_web_search_can_be_turned_off_per_lane(env):
    engine, ctx, calls, _ = env
    for pid in ("claude", "openai", "grok"):
        engine.config.providers[pid].web_search = False
    asyncio.run(engine.send(ctx, "offline please", fresh=True))
    c, x, g = calls("claude")[0], calls("codex")[0], calls("grok")[0]
    assert c["argv"][c["argv"].index("--tools") + 1] == "" and "--allowedTools" not in c["argv"]
    assert "--search" not in x["argv"]
    assert "--disable-web-search" in g["argv"]


def test_judges_never_search_the_web(env):
    engine, ctx, calls, _ = env
    asyncio.run(engine.send(ctx, "q", fresh=True))
    asyncio.run(engine.analyze(ctx, AnalysisKind.SYNTHESIS, judge="claude"))
    asyncio.run(engine.analyze(ctx, AnalysisKind.SYNTHESIS, judge="openai"))
    judge_c, judge_x = calls("claude")[-1], calls("codex")[-1]
    assert judge_c["argv"][judge_c["argv"].index("--tools") + 1] == ""
    assert "--search" not in judge_x["argv"]
