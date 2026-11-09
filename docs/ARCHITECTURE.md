# LLM Cockpit: internals (v2)

For the design review, transport table and decisions, see [REVIEW.md](REVIEW.md).

## Components

```
cli.py (argparse) ── shell.py (REPL → same handlers)          render.py: rich live / plain / JSONL
        │ CockpitEvent stream                                  export.py: md / json / directory tree
        ▼                                                      doctor.py: clients, sign-in, billing path
engine.py (Cockpit) ── navigation, lanes, targets, rounds, branches, attempts, stop, judges, feedback
   │        │          billing.py   API guard (off by default, hard monthly limit, per-run approval)
   │        │          analysis.py  blind packets, judge schema, normalization, anti-consensus pass
   │        │          sources.py   URL normalization, shared-source dedup (local, free)
   │        │          feedback.py  packet rendering per disclosure level, anti-convergence text
   │        │          memory.py    MemoryProvider extension point (no-op today)
   │        ▼
   │   store.py (SQLite v2 + migrations)
   ▼
providers/  claude_cli · codex_cli · command_cli (generic) · anthropic · openai · gemini · openai_compat · fake
            cli_common: argv-only subprocesses, env stripping, private workdirs, kill-on-cancel
```

## Lifecycle of a round

1. **Targets.** `plan_targets` combines persistent mute state with the one-round `--only`/`--exclude` selection and any eligibility rules (feedback rounds). Every lane gets a run row: included, or skipped with a reason.
2. **Per included lane:**
   - **Context:** read the lane's head on the current branch (`None` for blast) and its own history (a recursive walk of the message tree, scoped to chat and provider).
   - **Guards:** check the API guard if the lane bills an API, then the identical-run cache.
   - **Native resume:** pass the lane's native session only if `native_head_id == context head`.
3. **Run.** All lanes start concurrently. Each run creates an attempt and streams through `_collect`:
   - the total timeout and the adapter's idle timeout both apply;
   - there is one visible retry, only for network/server errors and only before any output.
4. **Watcher.** A watcher task polls `stop_requests` (cross-process stop) and handles `--first N`.
5. **Success.**
   - **Store output:** raw and normalized text are stored on the attempt.
   - **Log usage:** a `usage_events` row is written. Real cost is recorded only for API billing; subscription window status goes in its metadata.
   - **Commit:** `commit_exchange` appends the user and assistant messages under the parent and moves the lane head with a **compare-and-swap**. If another process moved the lane meanwhile, nothing is written and the run is marked `conflict`.
6. **Failure.** The attempt keeps any partial output, and nothing enters the lane. If an earlier attempt of this run succeeded, it stays current.
7. **After the round.** A local comparison prints, and the memory hook observes the round.

## Branching algorithm

`edit(N)`:
1. Create a branch with `parent = current branch` and `fork_round = N`.
2. For each lane, `head_before(rounds on lineage with seq < N)`. Walk back to that lane's latest non-skipped run:
   - if it completed, use its current attempt's answer;
   - if it failed, use the context head it started from;
   - if there is none, use `None`.
3. Switch the chat's current branch and run the edited prompt there.

`lineage(branch)` = the parent's lineage before the fork round, plus the branch's own rounds. Round numbers are unique per chat; a branch shows only its lineage.

## Attempts

- **Rerun/retry eligibility:** allowed only while the lane head still equals the run's current answer (or its context head, if the run failed). Otherwise you are told to branch with `edit`.
- **Where a new attempt attaches:** under the run's original context head (as a sibling). The head moves to it on success.
- **What gets sent:** native sessions are not reused for reruns; the transcript is replayed.

## Billing guard

`billing.check()` returns a reason or nothing:
- API off → blocked;
- no limit set → blocked;
- per-run mode without `--allow-api` / `--via api` → blocked;
- no price known (and the provider doesn't report cost) → blocked;
- limit reached, or the estimate would exceed the remainder → blocked.

Blocked lanes become skipped runs with that reason. Runtime toggles live in the DB meta table and override config.

## Memory extension point (future Obsidian)

`MemoryProvider.retrieve(ContextQuery)` is called before blast/follow-up rounds:
- Items are rendered into one delimited block prepended **identically** to every lane.
- The items are stored on `rounds.context_json`, and the exact text each lane received is on `runs.input_text`.

`on_round_complete(RoundRecord)` observes completed rounds only. Writing knowledge back must be an explicit, user-approved action implemented inside the memory package, against its own store. The directory export (`export --dir`) is a ready-made input for a vault.

## Security

- **Subprocesses:** argv lists only. Prompts go via stdin where the CLI allows it; otherwise as a single argv element, never through a shell.
- **Inputs:** model names are validated.
- **Billing safety:** API-key env vars are stripped from subscription CLIs. Each CLI runs in its own empty 0700 working directory.
- **Secrets:** keys are read from env or an argv `api_key_command` only, are never stored, and are redacted from all errors.
- **Storage:**
  - the DB is 0600 inside a 0700 directory, and the process runs with `umask 077`;
  - `secure_delete` is on, with a WAL checkpoint after deletes;
  - exports are 0600;
  - logs (opt-in via `COCKPIT_LOG`) carry metadata only.
- **Judge input:** response text cannot close its own `<response>` tag. Judge instructions treat responses as data.
- **Not done:** encryption at rest (use FileVault).

## Deferred

Context checkpoints and compact mode; catch-up re-entry packets; per-response normalized extraction; files/assets; Google Drive; web UI (the engine is UI-agnostic and event-driven, so it can be served from the same process); multi-judge; challenger-lane mode; Obsidian memory; encryption at rest; browser automation (not recommended).
