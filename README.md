# LLM Cockpit

One prompt, many independent model **lanes** (Claude, OpenAI, Gemini, Grok, Perplexity). It is subscription-first, keeps every lane's context isolated, makes disagreement legible instead of letting models drift into agreement, lets you edit old prompts as branches, and **never spends API money without your approval**.

```
PROMPT → SELECT LANES → INDEPENDENT ANSWERS → ROUND → FREE LOCAL COMPARISON
      → (optional blind judge) → EDITABLE FEEDBACK → YOU CHOOSE → NEXT ROUND
```

- **Spec review and decisions:** [docs/REVIEW.md](docs/REVIEW.md)
- **Internals:** [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)

## Install (Mac)

**Required:** Python 3.11+ and git.

```bash
git clone <this repo> llm-cockpit && cd llm-cockpit
```

```bash
python3 -m venv .venv && . .venv/bin/activate && pip install -e ".[dev]"
```

**Per lane, only what you use.** Each lane runs the vendor's official CLI, signed in with **your subscription**:

| Lane | Install | Sign in |
|---|---|---|
| Claude | Claude Code (official installer) | `claude`, then `/login` with your Claude plan |
| OpenAI | `npm i -g @openai/codex` (needs Node.js) | `codex login` with ChatGPT |
| Grok | `curl -fsSL https://x.ai/cli/install.sh \| bash` | `grok login` (add `--device-auth` on a remote machine) |
| Gemini *(off by default)* | Google Antigravity CLI (`agy`) | sign in with Google AI Pro/Ultra; `cockpit unmute gemini` per chat |
| Perplexity | nothing to install: **one-click helper** | sign in to perplexity.ai in your own browser as usual |

**Perplexity, the one-click helper.** Perplexity offers no sanctioned way for programs to use a Pro subscription, and its terms forbid automated use of the website. So Cockpit never automates it:
1. Cockpit copies the round's exact question to your clipboard and opens Perplexity in your own browser.
2. You press Enter there.
3. When the other lanes finish, you copy Perplexity's answer and press Enter in Cockpit (or later: `cockpit lane paste perplexity`).

It then joins the round, comparisons, reviews and feedback like any other lane, with its own separate conversation. There's no API cost. To make it fully automatic instead, switch that lane to the paid API (`transport = "api"`) under a hard monthly cap.

**Web search is on for every automatic lane** (Claude, ChatGPT/Codex, Grok), using your subscriptions:
- **Claude** gets only its web-search and page-reading tools, pre-approved and capped at 8 steps.
- **Codex** runs with `--search`.
- **Grok** gets its web-search and page-fetch tools.

Each answer lists the searches that model ran, and any narration before the final answer ("I'll search for...") is trimmed (the raw output is kept). Turn search off for a lane with `web_search = false` in `config.toml`. Judges never search: they evaluate only the answers they're given.

**Grok searches X on its own.** Grok runs server-side X/web searches even with tools off. Cockpit records every search it ran under the answer, so you can see when its evidence comes from X posts.

Then:

```bash
cockpit init && cockpit doctor
```

`doctor` checks each lane:
- whether the client is installed and signed in;
- **whether it will bill your subscription or an API key**, for Claude and Codex;
- which presets are unverified.

`cockpit doctor --live` sends one tiny real prompt per lane, which uses a little allowance.

> **Verified for real:** Claude (Claude Code), ChatGPT (Codex 0.167) and Grok (Grok Build 1.0.77), all with subscription sign-in. The Gemini preset is **unverified**: if `doctor --live` fails, adjust `command` / `output_format` for that lane in `config.toml`.

**Try it with no accounts at all:**

```bash
cockpit --demo
```

## The v1 loop

```bash
cockpit project create iris
```

```bash
cockpit chat use "Bolivia Treasury"
```

```bash
cockpit blast "What are the best on/off-ramp structures for Bolivia?"
```

All lanes stream concurrently. A free local comparison prints after every round.

```bash
cockpit mute grok perplexity
```

```bash
cockpit followup "Assume the telco already has direct banking relationships."
```

```bash
cockpit unmute grok
```

Grok continues from its own last context. Add `--fresh` to re-enter clean instead.

```bash
cockpit compare --full
```

```bash
cockpit adjudicate --judge openai
```

This is a blind, stateless judge. You get agreements, typed disagreements with each side's reasoning, evidence and assumptions, a **mandatory challenge** section, and source-dependency warnings.

```bash
cockpit iterate "Give me your revised recommendation."
```

This drafts a feedback packet. You review, edit and approve it before anything is sent.

```bash
cockpit edit 2 "Assume NO direct banking relationships."
```

This creates a new branch from before round 2. The old branch is preserved.

```bash
cockpit synthesize --judge claude
```

Synthesis only runs when you ask for it.

Interactive mode: run `cockpit`, then type prompts, or `/help` for slash commands (`/mute grok`, `/only claude,gemini ...`, `/iterate`, `/edit 3 ...`).

**After every round (interactive mode)**, Cockpit compares the answers with a blind judge and shows you the result with names. Then it asks what each AI should get with your next message:

| Choice | What each AI gets |
|---|---|
| 1 | just your message |
| 2 (Enter) | your message + the comparison (anonymous) |
| 3 | your message + the comparison + every AI's full answer (anonymous) |
| 4 | you choose 1, 2 or 3 separately for each AI |
| x | nothing; back to the normal prompt |

Nothing is sent until you type your message and confirm, so rounds never run on by themselves. `/guide off` turns this off. Pasting several lines sends them as **one** prompt, and Ctrl-C stops a round and throws away anything typed meanwhile.

**Your saved copy.** These conversations do not appear in the ChatGPT, Claude or Grok apps' history: the official command-line tools Cockpit uses don't write there. Instead, Cockpit keeps a readable copy of every chat in `~/Documents/LLM Cockpit/<project>/chats/<chat>/` (`chat.md` plus per-round files, comparisons and what each AI was sent), refreshed after every round. Change or turn it off with `autosave_dir` in `config.toml`.

## Commands

| Area | Commands |
|---|---|
| Workspace | `projects` · `project create\|use\|delete NAME` · `chats` · `chat create\|use\|delete NAME` · `use NAME` |
| Prompting | `blast` (fresh lane context) · `followup` / `ask` (each lane's own context) · `lane prompt claude "..."` · `--only a,b` / `--exclude c` (this round only) · `--first N` (stop the rest once N have answered) · `--force` (repeat an identical run) |
| Lanes | `lane paste perplexity [--file F]` (attach the answer you copied) · `mute` / `unmute [--fresh]` / `toggle` (persistent per chat) · `model claude <id>` · `effort openai high` · `providers` |
| Control | `stop gemini` (works from another terminal; others keep going) · `retry [lane]` · `rerun claude` (all attempts kept) · `attempts claude` |
| History | `rounds [--all]` · `show [--round N] [--input]` · `thread claude` · `branches` · `branch use NAME` · `edit N "..." [--only ...] [--context fresh]` |
| Analysis | `compare [--full] [--analyze]` · `adjudicate [--judge X] [--attributed] [--reveal]` · `synthesize` · `show-analysis ID [--prompt]` |
| Feedback | `iterate` · `feedback draft [--disclosure summary\|anonymized\|selected\|everything\|custom] [--include A,C]` · `feedback edit ID [--file F]` · `feedback send ID [--only ...] [--instruction ...]` · `feedback list\|show\|discard` |
| Money | `api` (status) · `api enable --limit 20 [--until-disabled]` · `api disable` · `--allow-api` · `retry X --via api` |
| Data | `status` · `export [--format md\|json] [-o file] [--dir out/]` · `delete-chat NAME --yes` |

Global flags: `-P/--project`, `-c/--chat`, `--demo`, `--plain`, `--json`, `--raw`.

**Exit codes:**

| Code | Meaning |
|---|---|
| 0 | every lane completed |
| 1 | nothing completed, or an error |
| 2 | partial success |
| 130 | interrupted |

## Guarantees (each covered by tests)

- **Isolation:** a lane only ever sees its own context on the current branch. Other models' content reaches it only through a feedback packet you reviewed.
- **Rounds:** each round records every lane, and comparisons never mix rounds or branches.
- **Branches:** editing an old prompt creates a branch. The old path is kept, and later rounds never leak into the new branch.
- **Attempts:** every attempt is kept, and a failed rerun never replaces a good answer.
- **Judges** never enter any lane's history.
- **No silent spending:** paid API use is off by default. It needs a hard monthly limit plus per-run approval (or `--until-disabled`). A subscription lane that hits its limit is marked `rate_limited` and **never** falls back to the API. Cockpit also strips API-key environment variables from subscription CLIs so they can't bill an API key behind your back.
- **No hidden prompts:** no system prompt is sent unless you set a visible one. Every lane gets the same prompt verbatim.

## Tests

```bash
pytest
```

135 tests run on fake lanes and mocked HTTP, so they cost nothing. They cover isolation, round and branch integrity, mute vs. one-shot selection, stop/retry/rerun, judge isolation, blind labels, false consensus (the spec's §54 fixtures), feedback editing and disclosure, the API billing guard, every parser, CLI transports (via fake `claude`, `codex` and `grok` binaries), migrations, file permissions and secure deletion.
