# Architecture review of the "Overnight Build" spec (v2)

Status legend: **built** = implemented and covered by automated tests · **verified live** = also exercised against the real vendor tool in development · **deferred** = designed for, not built.

## 1. Executive recommendation

Keep the spec's shape, with four corrections:

1. **Subscription-first is right, but every subscription path is a coding-agent CLI.** Claude Code, Codex, Grok Build and Antigravity are the only official programmable surfaces billed to consumer plans. Cockpit runs them hardened as neutral chat backends: no tools, no MCP, an empty working directory, stdin prompts, and API-key environment variables stripped. Codex still carries its own agent instructions, so its answers are not ChatGPT's. Cockpit records the transport on every run so this is never hidden.
2. **Perplexity has no programmatic subscription path, and its terms forbid automating the website.** Cockpit's default is a *one-click helper*: you run the question in your own browser and Cockpit copies the question and collects the answer. That uses your Pro plan and stays within the terms. The paid API remains an opt-in alternative.
3. **Vendor terms are a moving risk.** In 2026 Anthropic barred third-party harnesses from using subscription OAuth tokens. Cockpit never touches tokens: it runs the vendor's own client on your machine, which is the most defensible route available. Even so, check each vendor's current terms, and keep the API transport available per lane.
4. **The comparison engine cannot "detect truth".** It can make agreement *legible*. Cockpit computes source dependency deterministically, so three responses citing one source count as one source. It forces a challenge section in every analysis, and it flags head-count-only majorities. Semantic judgement stays with a switchable, blind, stateless judge model.

## 2. What I agree with

Lane isolation as the foundation. Cockpit owning the canonical transcript. Rounds as the unit of comparison. Branching instead of destructive edits. Mute vs. stop vs. retry vs. rerun as distinct operations. Blind judging by default. Editable feedback with disclosure levels. API spending that is off by default. One local process plus SQLite. Fakes so tests cost $0. Orchestration before UI.

## 3. What I changed or challenged

| Spec item | Change and reason |
|---|---|
| "Official subscription-backed programmable interface" for OpenAI | The only one is Codex (a coding agent). It is used, labelled `transport=codex`, and documented as not equal to ChatGPT. The default model is `default`, so Codex picks and reports its current one. On 0.167 that was `gpt-6.1-sol`, not the `gpt-5.5` I first assumed. |
| Gemini via "current Google CLI" | Antigravity (`agy`) replaced Gemini CLI for AI Pro/Ultra in June 2026. The preset ships as **unverified**: its flags come from public docs, it was not installed here, and `doctor` says so. |
| Grok CLI | The Grok Build preset (`grok -p ... --output-format json`, `--resume`) is **unverified** for the same reason. The generic `command` transport means you can fix flags in config with no code change. |
| Bounded automatic retry | Exactly one visible retry, **only** for network/server errors before any output. Never for rate limits, because that would burn subscription allowance. |
| Stop one lane | Built two ways. In-process cancellation, plus `cockpit stop gemini` from another terminal: it writes a stop request the running round polls, and only that lane's process is killed. |
| Comparison every round | A **free local comparison** runs automatically after every round: status, models, sources, shared-source warnings, and which lanes were not compared and why. AI comparison/adjudication is explicit (`compare --analyze`, `adjudicate`), so allowance isn't spent invisibly. |
| Hard monthly API limit | Enforced strictly. An API run is refused unless the cost is knowable (a `[pricing]` entry exists, or the provider reports cost itself, like Perplexity). An unknown price would make the "hard" limit fake. |
| Retry vs. rerun after the lane moved on | Refused with a pointer to `cockpit edit N`. Re-answering an old round in place would silently rewrite history the lane has already built on. |

## 4. Minimal install requirements (Mac)

**Required:** Python 3.11+, git, this repository (`pip install -e .`, which pulls only `httpx` and `rich`). SQLite ships with Python.

**Per lane, only if you use that lane:**
- **Claude:** the Claude Code CLI, signed in with your Claude subscription (`claude`, then `/login`).
- **OpenAI:** the Codex CLI (`npm i -g @openai/codex`, which needs Node.js), then `codex login` with ChatGPT.
- **Grok:** Grok Build (`curl -fsSL https://x.ai/cli/install.sh | bash`), signed in with SuperGrok.
- **Gemini:** Google Antigravity CLI (`agy`), signed in with Google AI Pro/Ultra.
- **Perplexity:** an API key, only if you approve paid API use.

**Not required:** Docker, Redis, Postgres, Nginx, any queue, a vector DB, Playwright, a Node server, Obsidian or MCP servers.

## 5. Lane transport table

| Lane | Transport (default) | Billing | Streaming | Native resume | Effort | Status |
|---|---|---|---|---|---|---|
| Claude | Claude Code CLI `-p --output-format stream-json` | subscription (auth verified via `claude auth status`) | tokens | `--resume`, only when in sync | `--effort` | **verified live** |
| OpenAI | Codex CLI `exec --json` | ChatGPT plan (`codex login status`) | per message | `exec resume <id>` | `model_reasoning_effort` | event format **verified** on 0.167; sign-in not possible here |
| Gemini (off by default) | `agy -p` (command transport) | Google AI plan | chunks | none (transcript replay) | none | **unverified preset** |
| Grok | Grok Build CLI `--output-format streaming-json`, prompt via private file | SuperGrok / grok.com (checked via `grok models`) | tokens | `--resume <sessionId>` | `--reasoning-effort` | **verified live** (1.0.77); runs server-side X searches regardless of flags, which are recorded per run |
| Perplexity | **one-click helper** (`manual`): you run it in your own browser and Cockpit copies the question and collects the answer | your Pro plan | n/a | your web thread (kept in sync; history replayed if not) | n/a | built and tested (fake clipboard and browser); paste path verified |
| Perplexity (alt.) | Sonar API | **paid API, opt-in** | tokens | n/a | `reasoning_effort` | built (mocked). Check first: Perplexity has been moving to its "Agent API" |
| any lane | `--via api` on retry/rerun, or `transport = "api"` | paid API, opt-in | tokens | n/a | where supported | built (mocked) |

### Web search (v0.3.1)

Live web search is on by default for the Claude, Codex and Grok lanes, billed to your subscriptions. The searches each lane ran are stored in `meta.searches` and shown under its answer. Cited URLs feed the free local source analysis.

| Lane | How it's switched on |
|---|---|
| Claude | `--tools WebSearch,WebFetch` + `--allowedTools` + `--max-turns 8` (verified live) |
| Codex | `codex --search exec ...` (argument parsing verified) |
| Grok | `--tools web_search,web_fetch --allow web_fetch`. Verified live. Without pre-approval, headless Grok cancels when it wants to read a page. |

Narration before the final answer is trimmed from the committed text (raw output kept). Judges never search.

## 6. Core data model

`projects › chats › branches › rounds › runs (one per lane per round, including skips with their reason) › attempts`

Supporting tables:
- **Messages:** `messages` is a per-lane parent-linked tree. `lane_heads` holds each lane's position per branch and caches its native session.
- **Lane state:** `lanes` holds mute/model/effort per chat.
- **Analysis and feedback:** `analyses` (compare/adjudicate/synthesis), `feedback_packets` (draft → sent, editable, with a disclosure level).
- **Accounting:** `usage_events` (billing class, tokens, real API cost only, subscription window status) and `stop_requests`.
- **Migrations:** schema is versioned. A real v1→v2 migration ships and is tested.

## 7. Adapter interface

```python
class ProviderAdapter:
    transport: str                      # claude-code | codex | command | api | fake
    capabilities: frozenset[str]        # streaming, native_session, fork, reasoning_effort, stop,
                                        # citations, usage_status, subscription_auth, reports_cost, ...
    streaming_granularity: str          # tokens | message | chunks
    def stream(ChatRequest) -> AsyncIterator[TextDelta | NativeSession | Usage | ResponseMeta]
    async def health_check(live: bool) -> HealthReport   # installed, signed in, subscription_path, billing_warnings
    def postprocess(raw) -> str          # normalized text; raw is stored separately and never rewritten
```

How the other spec operations map:
- **cancel:** asyncio cancellation, which kills the subprocess or closes the stream.
- **resume:** `ChatRequest.native_session_id`, set by the engine only when the native session is provably in sync.
- **fork:** done by Cockpit through branching, never by the vendor.
- **list_models:** part of `health_check`.
- **attach_asset:** deferred.

## 8. Branch/edit strategy

- **Edit:** `cockpit edit N "new prompt"` creates a branch forked immediately before round N and switches to it. The old branch is untouched.
- **Lane reconstruction:** each lane's head on the new branch comes from the rounds before N on the lineage. The rule is: the latest completed answer of that lane, or its context head if its last run failed. Lanes that were skipped are handled correctly.
- **No leakage:** later rounds of the old branch never enter the new one. Native sessions are never reused across a fork; the canonical transcript is replayed instead.
- **Options:** `--only` reruns selected lanes; `--context fresh` starts lanes clean. `branches` and `branch use` let you navigate.
- **Not built:** compact-context reconstruction (see §10).

## 9. Comparison and anti-consensus strategy

- **Local, free, every round:** URL extraction and normalization (strips `www.`, tracking parameters and fragments), shared-source groups, uncited lanes, and an explicit "not compared" list.
- **Judge packet:** blind shuffled labels and the local source analysis ("A, B, C all cite X — ONE source"). The rules include *majority is not evidence*. Disagreements are classified by type (factual, assumption, logic, values, time horizon, evidence quality, confidence). The judge must give each side's reasoning, evidence and assumptions, and a **mandatory challenge section**; if it's missing, the analysis is flagged.
- **Deterministic post-pass:** every agreement and disagreement side is annotated with its independent evidence lines. Risks are raised for "broad agreement on one shared source", "agreement with no sources", and "larger side has no more independent evidence".
- **Feedback packets:** never include provider names or judge labels. They always end with the visible anti-convergence instruction, and you review and edit before sending.

## 10. Context-efficiency strategy

**Built:**
- **Fewer lanes per round:** skinny lanes (mute, or `--only` for one round).
- **Less to resend:** edit-and-branch instead of corrective turns, and delta feedback (packets, not transcripts).
- **Cheaper runs:** per-lane effort, `--first N` early stop, and native-session resume, which avoids replaying the transcript when in sync.
- **No wasted allowance:** an identical-run cache (`--force` to repeat), and judge calls only when asked, each one visible.
- **Visibility:** a context meter (`status` shows turns and ~tokens per lane).

**Deferred:** compact checkpoints (§16), catch-up re-entry packets (§13), and per-response normalized extraction (claims/assumptions per response would add a model call per lane per round).

## 11. Build sequence

| Phase | Status | Notes |
|---|---|---|
| 0, 1 (architecture, core engine, fakes, CLI) | **done** | |
| 2 (real lanes) | Claude **verified live**; Codex format verified; Grok/Antigravity presets unverified; API adapters done (mocked) | |
| 3 (mute, lane prompts, retry, rerun, stop, branch/edit) | **done** | |
| 4 (raw + local compare, switchable blind judge, mandatory challenge) | **done** | |
| 5 (feedback packets, editing, disclosure, selective recipients) | **done** | |
| 6 (context efficiency) | partly done | Checkpoints and catch-up are next |
| 7–10 (files, web UI, Google Drive, Obsidian) | deferred | The memory interface and directory export are in place |

## 12. Risks and unresolved items

- **Vendor terms for programmatic use of consumer plans.** Most acute for Anthropic; also Codex, Grok and Antigravity. Mitigation: official clients only, local only, no token handling, and an API transport per lane if a vendor objects.
- **Unverified presets:** Grok Build and Antigravity. Run `cockpit doctor --live` and adjust `command`, `resume_args` and `output_format` in config if needed.
- **Codex as the OpenAI lane** is not ChatGPT, and the read-only sandbox still allows read-only shell commands. Cockpit runs it in an empty directory and records `agent_tool_calls` in run metadata when it uses tools.
- **Subscription allowance visibility:** real only for Claude (5-hour and 7-day window utilization from the CLI). Unknown for the others, and shown as unknown rather than invented.
- **Perplexity lane cost:** costs money whenever used. It is off until you enable the API with a hard limit.

## §62 answers (short)

1. **Transports:** see §5.
2. **Which use your subscription:** Claude Code, Codex, Grok Build and Antigravity — when signed in with the plan account, not an API key. `doctor` checks this for Claude and Codex.
3. **Which can't:** Perplexity.
4. **Mac installs:** see §4.
5. **Eliminated dependencies:** everything except `httpx` and `rich`.
6. **One process?** Yes. A second terminal can still `cockpit stop`.
7. **Native sessions vs. canonical history:** native sessions are a cache, used only when in sync. Cockpit's tree is the truth.
8. **Branching without native fork:** reconstruct lane heads from lineage and replay the transcript into a fresh native session.
9. **Stop per provider:** cancel that lane's task, which kills its subprocess or closes its HTTP stream. Works across processes via `stop_requests`.
10. **No unapproved spend:** API is off by default; a hard monthly limit and per-run approval are required; a price must be known; a failure never falls back to the API (tested).
11. **False consensus:** see §9.
12. **Smallest proof of the v1 success test:** this build. Claude, ChatGPT and Grok live, plus fakes for the full loop; 130 tests.
13. **Deferred features:** see §10–11.
14. **Browser automation:** not built. It would violate most providers' terms or trip anti-bot systems, breaks on UI changes, and needs credential handling Cockpit deliberately avoids.
15. **Expensive to reverse:** letting vendors own history, a linear message schema, or auto-falling-back to paid APIs. All avoided.
