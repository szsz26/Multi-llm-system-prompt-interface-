# Multi-LLM Prompt & Compare

Send **one prompt to many LLMs at once** (Claude, OpenAI/Codex, Gemini, Grok,
Perplexity), see every response in one interactive terminal view, compare the
differences, and **continue each model in its own isolated context** — no
cross-contamination between providers.

Built as an interactive TUI (Node.js + TypeScript + [Ink](https://github.com/vadimdemedes/ink)).

## Features

- **One prompt → all models.** Type once, "blast" it to every active provider in parallel.
- **Live streaming.** Responses stream into each pane token-by-token as they arrive
  (SSE for the API providers; `stream-json`/stdout for the CLIs).
- **One clean view.** A pane per model plus a **Combined** pane.
- **Per-LLM toggle.** Pause/resume any model with a keypress; the blast only hits
  the ones that are toggled on. Paused models keep their history and can be
  resumed later.
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

## Install

```bash
npm install
cp .env.example .env   # fill in keys you have (Grok/Perplexity); rest can stay blank
```

## Run

```bash
npm run dev            # uses the "default" session
npm run dev myproject  # named session (separate saved histories)
```

Or build and run the compiled binary:

```bash
npm run build
node dist/index.js
```

## Keys

The UI has two modes (vim-style) so typing never collides with shortcuts.

**Insert mode (default — for typing):**
- `Enter` — send the prompt to all **active** providers
- `Esc` — switch to nav mode

**Nav mode:**
- `i` — back to typing
- `1`–`9` / `←` `→` — switch pane (each provider + Combined)
- `t` or `space` — toggle the focused provider on/off (pause)
- `c` — generate the AI comparison/synthesis (Combined pane)
- `r` — reset the focused provider's conversation (or all, on Combined)
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
