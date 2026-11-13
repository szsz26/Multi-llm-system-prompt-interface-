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

**After every round (interactive mode)**, Cockpit compares the answers with a blind judge and shows you the result with names. Press Enter and you get a **clean screen**: a one-line recap per AI, where the chat is saved, and these options (also shown as a reminder at every prompt):

| Choice | What each AI gets with your next message |
|---|---|
| 1 | just your message |
| 2 (Enter) | your message plus the comparison |
| 3 | your message plus the comparison plus every AI's full answer |
| 4 | choose separately for each AI |
| x | nothing; go back to asking a fresh question |

No AI ever sees which AI wrote what. Then type or paste your message (several lines are fine) and press Enter on an empty line. Nothing is sent until you confirm, so rounds never run on by themselves. `/guide off` turns this off, `/clear` cleans the screen. Pasting several lines at the normal prompt sends them as **one** prompt, and Ctrl-C stops a round and throws away anything typed meanwhile.

**The reader page: read in your browser, type in the Terminal.** After each round Cockpit writes a formatted page and opens it in your browser (once; after that the page updates itself). It is laid out for reading, the way a chat app shows answers:

- large, well-spaced text, light or dark to match your computer;
- real tables, formatted code with a Copy button, math, and images from the web;
- **artifacts**: when an AI writes a standalone piece (a web page or small interactive tool, a chart, a diagram, a drawing, a long document) it appears as a card with a live preview, a Code tab, Copy and Expand. Each one is also saved as its own file;
- an **At a glance** box built from the comparison: where the AIs agree, where they differ and why (with names), weak claims, the strongest challenge, what to ask next;
- each AI in its own section, a bar to jump between them, and a **Side by side** button for wide screens. Older rounds are folded.

While the reader page is open, the Terminal shows only a short recap, the options and a clean place to type. `/read` opens the page again; `/reader off` puts full answers back in the Terminal; `reader = false` in `config.toml` turns it off for good. No extra tools or accounts are needed: it is one file on your computer, opened in your own browser.

So that answers read like chat answers instead of terse terminal replies, every AI gets one **visible** instruction (`chat_style` in `config.toml`; `""` turns it off): answer as you would in your chat app, well formatted, and put standalone pieces in labelled blocks so they can be shown as artifacts. Judges never get it.

What the reader page cannot do: pictures generated by an AI (the command-line tools don't offer image generation) and Word/Excel files. Diagrams, charts and drawings an AI writes as code do work.

Safety: nothing an AI writes runs in the page itself. Artifacts run in sandboxed frames that cannot reach the page, your files, or the network (beyond loading libraries from three script CDNs); drawings are shown as images.

**Your saved copy.** These conversations do not appear in the ChatGPT, Claude or Grok apps' history: the official command-line tools Cockpit uses don't write there. Instead, after every round Cockpit saves a readable copy in **Documents → LLM Cockpit**: `<topic>.md` (the whole conversation) plus a folder with every round, comparison and exactly what each AI was sent. Type `/saved` to open that folder. If your Mac doesn't let Terminal into Documents, Cockpit saves to **LLM Cockpit** in your home folder instead and says so. Start a separate topic with `/chat use <topic>`; each topic is its own file. Change or turn saving off with `autosave_dir` in `config.toml`.

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
- **No hidden prompts:** the only instruction sent is the visible `chat_style` line (identical for every lane, editable, `""` turns it off) plus any visible `system_prompt` you set. Every lane gets your prompt verbatim.

## Tests

```bash
pytest
```

141 tests run on fake lanes and mocked HTTP, so they cost nothing. They cover isolation, round and branch integrity, mute vs. one-shot selection, stop/retry/rerun, judge isolation, blind labels, false consensus (the spec's §54 fixtures), feedback editing and disclosure, the API billing guard, every parser, CLI transports (via fake `claude`, `codex` and `grok` binaries), migrations, file permissions and secure deletion.
