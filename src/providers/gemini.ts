import type { Message, ProviderAdapter, ProviderConfig } from "../types.js";
import { commandExists, renderHistoryAsPrompt, runCli } from "./base/cli.js";

/**
 * Gemini via the `gemini` CLI (`gemini -p`). Non-interactive prompt mode is
 * stateless, so we replay this provider's own isolated transcript each turn.
 */
export function createGeminiAdapter(cfg: ProviderConfig): ProviderAdapter {
  const command = cfg.command ?? "gemini";
  return {
    id: "gemini",
    label: "Gemini",
    async isAvailable() {
      if (await commandExists(command)) return { ok: true };
      return { ok: false, reason: `'${command}' CLI not found on PATH` };
    },
    async send(history: Message[]) {
      const args: string[] = [];
      if (cfg.model) args.push("--model", cfg.model);
      args.push("-p", renderHistoryAsPrompt(history));
      const { stdout } = await runCli(command, args);
      return stdout.trim();
    },
  };
}
