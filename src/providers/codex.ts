import type { Message, ProviderAdapter, ProviderConfig } from "../types.js";
import { commandExists, renderHistoryAsPrompt, runCli, runCliStream } from "./base/cli.js";

/**
 * OpenAI via the `codex` CLI in non-interactive mode (`codex exec`). The
 * non-interactive exec is stateless, so we replay this provider's own isolated
 * transcript each turn to continue its train of thought (no other model's
 * answers are ever included).
 */
export function createCodexAdapter(cfg: ProviderConfig): ProviderAdapter {
  const command = cfg.command ?? "codex";
  return {
    id: "codex",
    label: "OpenAI (Codex)",
    async isAvailable() {
      if (await commandExists(command)) return { ok: true };
      return { ok: false, reason: `'${command}' CLI not found on PATH` };
    },
    async send(history: Message[], onToken) {
      const args = ["exec"];
      if (cfg.model) args.push("--model", cfg.model);
      args.push(renderHistoryAsPrompt(history));
      if (onToken) {
        const { stdout } = await runCliStream(command, args, { onChunk: onToken });
        return stdout.trim();
      }
      const { stdout } = await runCli(command, args);
      return stdout.trim();
    },
  };
}
