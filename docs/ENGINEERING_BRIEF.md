# LLM Cockpit: engineering review brief (v0.3.2)

## 1. What it is
A local, single-process tool. One prompt goes to several AI "lanes" at once (Claude, ChatGPT/Codex, Grok, Perplexity, optionally Gemini). Each lane keeps its own isolated conversation. Cockpit then compares the answers, runs a blind judge, and sends user-reviewed feedback back. Its goal is to replace manually copy-pasting between the vendors' chat apps, and to use the user's existing **subscriptions** rather than paid APIs wherever an official path exists.

- **Stack:** Python 3.11+, SQLite (stdlib), `httpx`, `rich`. No server, queue or Docker.
- **Size:** ~7,250 lines across 25 modules in `src/cockpit/`; 130 tests in `tests/`.
- **Interface:** CLI first (`cockpit ...`), plus an interactive shell (`cockpit`) that uses the same handlers. The engine is UI-agnostic and event-driven, so a web UI can sit on top later.
- **Code:** delivered as the archive `llm-cockpit-v0.3.2.tar.gz` (not yet in a git repo).

## 2. How one round flows
```
user prompt ─► engine.send()
                 ├─ plan_targets()       mute state + one-round --only/--exclude → run or skip(reason) per lane
                 ├─ _plan_runs()         per lane: own history on this branch, API guard, identical-run cache
                 ├─ _execute()           all lanes concurrently (asyncio) + _watch() for stop / --first N
                 │     └─ _run_one()     adapter.stream() → normalized events → commit_exchange() (CAS)
                 └─ local_compare()      free: statuses, shared-source warnings, who wasn't compared
lanes = subprocesses: claude | codex | grok   (each talks only to its own vendor)
Perplexity = manual: prompt to clipboard + opens user's browser; answer pasted back
```
Models never see each other's output. Cross-model content reaches a lane only through a **feedback packet** that the user drafts, reviews and edits before it is sent.

## 3. Lanes (transports)
| Lane | Invocation (argv; never a shell string) | Auth / billing | Status |
|---|---|---|---|
| Claude | `claude -p --output-format stream-json --verbose --include-partial-messages --tools WebSearch,WebFetch --allowedTools "WebSearch WebFetch" --max-turns 8 --strict-mcp-config --system-prompt "You are a helpful assistant." [--model] [--effort] [--resume ID] [--no-session-persistence]`. Prompt on **stdin**. | Claude subscription login (OAuth). `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` stripped from the child env. `doctor` reads `claude auth status`. | **Verified live:** streaming, resume, rerun, effort, web search, 5h/7d allowance capture |
| ChatGPT | `codex [--search] exec --json --skip-git-repo-check -s read-only --color never -C <empty dir> [-m] [-c model_reasoning_effort=…] [--ephemeral] [resume ID] -`. Prompt on stdin. | ChatGPT plan via `codex login` (device code works). `OPENAI_API_KEY`/`CODEX_API_KEY` stripped. | **Verified live** on codex-cli 0.167 with ChatGPT sign-in: streaming, resume (follow-up remembered), web search, narration trimming. Codex is a coding agent, so its answers are not ChatGPT's. |
| Grok | `grok --prompt-file <0600 tmp> --output-format streaming-json --tools web_search,web_fetch --allow web_fetch --no-subagents --system-prompt-override "…" --cwd <empty dir> [-m] [--reasoning-effort] [--resume ID]` | SuperGrok/grok.com login (device code). `XAI_API_KEY` stripped. | **Verified live** on grok 1.0.77. Runs **server-side X searches that no flag disables**; every query is recorded in `meta.searches`. |
| Perplexity | `manual` lane: copy prompt and open `perplexity.ai/search?q=`; user pastes the answer (`cockpit lane paste perplexity`). Optional paid Sonar API lane. | User's own browser session; Cockpit never touches the site. | Built and tested with a fake clipboard and browser; paste path checked here with a labelled placeholder. Perplexity's terms forbid automating the site. |
| Gemini | `agy -p "<prompt>"` (generic `command` transport) | Google AI Pro | **Unverified preset; off by default** |
| API adapters | Anthropic Messages, OpenAI Responses, Gemini, xAI, Perplexity. Raw `httpx` SSE. | Paid; off unless approved (see §5) | Parsers tested against mocked streams only |

Shared CLI plumbing lives in `providers/cli_common.py` (`run_cli`, `clean_env`): idle timeout, kill-on-cancel, UTF-8-safe chunking, a private empty workdir per lane.

Narration printed before a model's final answer ("I'll search…") is trimmed from the committed text through the `answer_from` marker. The raw output is always stored.

## 4. Data model (`store.py`, schema v2, migration from v1 included)
`projects › chats › branches › rounds › runs (one per lane per round, including skips with reason) › attempts`

Supporting tables:
- `messages`: a per-lane parent-linked tree.
- `lane_heads`: each lane's position per branch, plus a cached native session id.
- `lanes`: mute/model/effort per chat.
- `analyses`, `feedback_packets`, `usage_events`, `stop_requests`.

Native vendor sessions are a cache. Cockpit's tree is the source of truth; a native session is resumed only when its head equals Cockpit's head, and otherwise the transcript is replayed.

## 5. Invariants and where they're enforced
| Invariant | Where |
|---|---|
| Lane isolation (history built from that lane's tree only) | `Store.history`, `engine._plan_runs` |
| Round integrity: comparisons use only completed runs of one round | `analysis.build_packet` (raises on mixed rounds), `engine.analyze` |
| Commit is compare-and-swap on the lane head (concurrent processes can't interleave) | `Store.commit_exchange` |
| Failed, stopped or partial output is never committed | `engine._run_one` / `_fail` |
| A failed rerun never replaces a good answer; all attempts kept | `engine._fail`, `attempts` table |
| Editing an old prompt creates a branch; lane heads are rebuilt from earlier rounds | `engine.edit`, `engine._head_before`, `Store.lineage` |
| Judges are stateless (single message, ephemeral, no web, no lane writes) | `engine.analyze` |
| Feedback only reaches lanes whose latest answer is the reviewed round | `engine.feedback_eligibility` |
| No silent API spend: off by default, hard monthly cap, per-run approval, known price, no fallback | `billing.check`, `engine._api_block` |
| Mute persists; `--only`/`--exclude` never do | `engine.plan_targets` |

## 6. Anti-false-consensus
- **Local and free:** URL extraction and normalization; responses citing the same source count as **one** evidence line (`sources.py`).
- **Judge:** blind, with shuffled labels. Its schema has typed disagreements, each side's reasoning, evidence and assumptions, and a **mandatory challenge section**. A missing section is flagged. Response tags are escaped against injection (`analysis.build_packet`, `_escape`).
- **Deterministic post-pass:** `annotate_evidence` counts independent sources per agreement and per disagreement side, and flags head-count-only majorities.
- **Feedback packets:** `feedback.render_packet` strips labels and provider names and appends a visible anti-convergence instruction.

## 7. Security
- **Processes and files:** argv-only subprocesses; prompts via stdin or a 0600 temp file; empty per-lane working directories.
- **Tools:** Claude and Grok get web tools only. Codex runs in a read-only sandbox, but it can still run read-only shell commands; these are logged.
- **Credentials:** API keys stripped from subscription CLIs. Keys come only from env or an argv `api_key_command`, are never stored, and are redacted in errors (`secrets.redact`).
- **Storage:** DB 0600 in a 0700 directory; `umask 077`; `PRAGMA secure_delete`; WAL checkpoint after deletes. Exports are 0600.
- **Input validation:** model names checked (`validate_model_name`).
- **Not done:** encryption at rest.

## 8. Policy and terms risks
- **Anthropic:** in 2026 it restricted third-party tools from using subscription OAuth. Cockpit only runs Anthropic's own CLI locally and never handles tokens, but this pattern is close to what was restricted.
- **Other vendors:** similar uncertainty for Codex, Grok Build and Antigravity being driven by another program.
- **Perplexity:** its terms forbid bots and automated queries, hence the manual lane.
- **Grok:** answers may rest on X posts.

## 9. Testing
130 tests, no network, $0 cost.

| File | Tests | Covers |
|---|---|---|
| `test_isolation.py` | 7 | lane, chat and project isolation |
| `test_rounds_toggles.py` | 14 | round integrity, mute vs one-shot selection |
| `test_branching.py` | 7 | edit and branch reconstruction |
| `test_attempts_stop_failures.py` | 13 | stop, retry, rerun, failures, streaming order |
| `test_judge_feedback.py` | 14 | judge isolation, the spec's false-consensus fixtures, feedback disclosure and editing |
| `test_billing.py` | 8 | API guard |
| `test_manual_lane.py` | 9 | Perplexity helper |
| `test_cli_transports.py` | 9 | fake `claude` / `codex` / `grok` binaries assert argv, stdin, env stripping, resume, fallback |
| `test_parsers.py` | 30 | every stream parser, using real captured output where available |
| `test_store_security_cli.py` | 10 | migration, permissions, CLI end-to-end |

**Live runs in this session (real subscriptions):**
- Claude, ChatGPT and Grok, in parallel rounds (three-way round on 9 Nov 2026: all three cited the central bank's Bs 11.81/USD for 7–9 Nov).
- Memory follow-ups and reruns.
- A blind adjudication with Grok as judge.
- Web-search rounds: Claude and Grok (and later ChatGPT) found and cited the central bank's rate (Bs 11.81/USD, 7–9 Nov 2026). Without web search they had disagreed.

## 10. Open questions for the reviewer
1. Is wrapping vendor CLIs the right long-term transport, given vendor-terms risk and CLI output-format drift? Each parser is isolated in one adapter file.
2. Is `commit_exchange`'s compare-and-swap sufficient for two Cockpit processes on one database?
3. Is branch reconstruction in `_head_before` correct for lanes that were skipped or failed before the fork?
4. Is the `answer_from` narration trimming safe, or could it cut a real answer?
5. Error classification in `cli_common.classify_cli_error` is keyword-based. Is it acceptable?
6. Codex's read-only sandbox still allows read-only shell commands. Is that acceptable?
7. Grok's X search can't be turned off. Should that lane be labelled differently in comparisons?
8. Deferred work: context checkpoints, catch-up packets, file attachments, web UI, Obsidian memory, encryption at rest.

## 11. Run it
```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
cockpit --demo          # fake lanes, no accounts needed
pytest                  # 130 tests
cockpit doctor --live   # real lanes: install and sign in to each CLI first
```

More detail: `docs/ARCHITECTURE.md` (internals) and `docs/REVIEW.md` (decisions against the original spec).
