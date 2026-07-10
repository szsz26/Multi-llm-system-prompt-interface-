# Multi-LLM Prompt & Compare

Send **one prompt to many LLMs at once** (Claude, OpenAI/Codex, Gemini, Grok,
Perplexity), see every response in one interactive terminal view, compare the
differences, and **continue each model in its own isolated context** — no
cross-contamination between providers.

Built as an interactive TUI (Node.js + TypeScript + [Ink](https://github.com/vadimdemedes/ink)).

## Features

- **One prompt → all models.** Type once, "blast" it to every active provider in parallel.
- **Side-by-side columns.** Every model is a live column on screen at the same
  time, so a single prompt visibly fills them all — no switching panes to see who
  answered. A **Combined** view adds the compare/synthesis.
- **Live streaming.** Responses stream into each column token-by-token as they
  arrive (SSE for the API providers; `stream-json`/stdout for the CLIs).
- **Per-LLM toggle = the selector.** Whatever is toggled **on** (green ●) gets
  the next prompt; toggle a model **off** (○) to exclude it. Paused models keep
  their history and can be resumed later.
- **Isolated context per model.** Each provider has its own conversation thread.
  A follow-up continues each model's own train of thought — it never sees another
  model's answers.
- **Compare.** Combined pane shows a word-level **textual diff** plus an
  **AI-generated synthesis** (consensus / disagreements / unique points).
- **Graceful degradation.** Any provider whose CLI or API key is missing is
  shown as unavailable and skipped — use whatever subset you have.
- **Persistent sessions.** Per-provider history is saved to `.sessions/` so you
  can pick up where you left off.

## How each provider is reached

| Provider   | Backend                | Requirement                          |
|------------|------------------------|--------------------------------------|
| Claude     | `claude` CLI           | `claude` on PATH (native session resume) |
| OpenAI     | `codex` CLI (`exec`)   | `codex` on PATH                      |
| Gemini     | `gemini` CLI (`-p`)    | `gemini` on PATH                     |
| Grok       | x.ai HTTP API          | `XAI_API_KEY` (no official CLI)      |
| Perplexity | Perplexity HTTP API    | `PERPLEXITY_API_KEY` (no official CLI) |

## Windows quick start (non-technical, step by step)

Everything happens in **one PowerShell window** — paste one line, press Enter,
wait for it to finish, then the next line.

**One-time installs (point-and-click, from your browser):**
1. Node.js — https://nodejs.org/en/download → click the **LTS** Windows installer, run it, Next through the defaults.
2. Git — https://git-scm.com/download/win → run it, Next through the defaults.
3. Close and reopen PowerShell so it sees the new programs.

**Allow scripts to run (one time):** Windows blocks npm by default. Paste this,
press Enter, then type `Y`:
```powershell
Set-ExecutionPolicy -Scope CurrentUser -ExecutionPolicy RemoteSigned
```

**Download and start:**
```powershell
git clone https://github.com/szsz26/Multi-llm-system-prompt-interface-.git
cd Multi-llm-system-prompt-interface-
git checkout claude/multi-ai-cli-compare-clcgne
npm install
npm run demo
```
`npm run demo` opens the app with three offline demo models — no accounts needed.
Press `q` to quit.

**Get updates later:** `cd` into the folder, then `git pull`, `npm install`, run again.

## Install (Mac/Linux or already comfortable in a terminal)

```bash
npm install
cp .env.example .env   # fill in keys you have (Grok/Perplexity); rest can stay blank
```

## Try it instantly (no keys or CLIs)

```bash
npm install
npm run demo
```

Demo mode runs three offline mock providers so you can see the whole flow —
panes, per-LLM toggles, live streaming, the diff and the AI synthesis — without
installing any CLI or setting any API key. Send a prompt, switch panes with the
number keys, pause one with `t`, then open the Combined pane and press `c`.

## Connect your real accounts

Claude, ChatGPT, and Gemini each connect through their official command-line
tools. Install each one, then log in **with your existing account** (no API key
needed). Run these in the same window:

**Claude**
```bash
npm install -g @anthropic-ai/claude-code
claude          # opens your browser to log in with your Claude account
```

**ChatGPT (OpenAI)**
```bash
npm install -g @openai/codex
codex           # sign in with your ChatGPT account
```

**Gemini**
```bash
npm install -g @google/gemini-cli
gemini          # opens your browser to log in with your Google account
```

**Grok & Perplexity** have no login-based CLI — connect them with API keys in a
`.env` file: copy `.env.example` to `.env` and paste in `XAI_API_KEY` and/or
`PERPLEXITY_API_KEY`.

Then start the real app:

## Run (real providers)

```bash
npm run dev            # uses the "default" session
npm run dev myproject  # named session (separate saved histories)
```

Each tool you connected shows a green ● in the status bar; anything not installed
shows a grey ⊘ and is skipped.

Or build and run the compiled binary:

```bash
npm run build
node dist/index.js
```

## Keys

The UI has two modes (vim-style) so typing never collides with shortcuts.

**Insert mode (default — for typing):**
- `Enter` — send the prompt to **every toggled-on model at once**
- `Esc` — switch to nav mode

**Nav mode (grid):**
- `i` — back to typing
- `←` `→` / `1`–`9` — highlight a model's column
- `t` or `space` — toggle the highlighted model on/off (on = gets the prompt)
- `c` — open the **Combined** compare view (agreements, differences, diff)
- `r` — reset the highlighted model's conversation
- `q` — quit

**Nav mode (combined view):**
- `g` or `Esc` — back to the grid
- `r` — reset all conversations
- `q` — quit

## Configuration

`config.json` controls which providers load, CLI command overrides, default
models, and which provider powers the synthesis:

```json
{
  "synthesisProvider": "claude",
  "providers": {
    "claude": { "enabled": true, "command": "claude" },
    "codex": { "enabled": true, "command": "codex" },
    "gemini": { "enabled": true, "command": "gemini" },
    "grok": { "enabled": true, "model": "grok-4" },
    "perplexity": { "enabled": true, "model": "sonar-pro" },
    "mock": { "enabled": false }
  }
}
```

Model can also be overridden per-provider via env, e.g. `GROK_MODEL`,
`PERPLEXITY_MODEL`, `CLAUDE_MODEL`.

### Offline testing

Set `"mock": { "enabled": true }` to add an offline echo provider that needs no
CLI or key — handy for trying the fan-out, toggles, and compare view.

## Notes

The CLI flags for `claude`, `codex`, and `gemini` may need small tweaks for your
installed versions. Adapters are isolated in `src/providers/` — one file per
provider — so they're easy to adjust.
