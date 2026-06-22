import type { Message, ProviderAdapter, ProviderConfig, SessionAccessor } from "../types.js";
import { commandExists, latestUserPrompt, renderHistoryAsPrompt, runCli } from "./base/cli.js";

/**
 * Claude via the `claude` CLI in print mode. Uses native session resume
 * (`--resume <id>`) so context continues without us re-sending the transcript;
 * the session id only ever maps to Claude's own thread.
 */
export function createClaudeAdapter(cfg: ProviderConfig, session: SessionAccessor): ProviderAdapter {
  const command = cfg.command ?? "claude";
  return {
    id: "claude",
    label: "Claude",
    async isAvailable() {
      if (await commandExists(command)) return { ok: true };
      return { ok: false, reason: `'${command}' CLI not found on PATH` };
    },
    async send(history: Message[]) {
      const prompt = latestUserPrompt(history);
      const sessionId = session.get();
      const args = ["-p", "--output-format", "json"];
      if (cfg.model) args.push("--model", cfg.model);

      // First turn starts a session; later turns resume it.
      if (sessionId) args.push("--resume", sessionId);
      args.push(prompt);

      try {
        const { stdout } = await runCli(command, args);
        const parsed = JSON.parse(stdout) as { result?: string; session_id?: string };
        if (parsed.session_id) session.set(parsed.session_id);
        return (parsed.result ?? stdout).trim();
      } catch (err) {
        // If resume fails (e.g. expired session), retry once by replaying the
        // full isolated transcript as a fresh prompt.
        if (sessionId) {
          const { stdout } = await runCli(command, [
            "-p",
            "--output-format",
            "json",
            ...(cfg.model ? ["--model", cfg.model] : []),
            renderHistoryAsPrompt(history),
          ]);
          const parsed = JSON.parse(stdout) as { result?: string; session_id?: string };
          if (parsed.session_id) session.set(parsed.session_id);
          return (parsed.result ?? stdout).trim();
        }
        throw err;
      }
    },
  };
}
