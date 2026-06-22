import type { Message, ProviderAdapter } from "../types.js";
import { latestUserPrompt } from "./base/cli.js";

/**
 * Offline provider for testing the fan-out, isolation, toggles and compare view
 * without any CLIs or API keys. Echoes the prompt and reports how many turns it
 * has seen in ITS OWN history (proving context isolation).
 */
export function createMockAdapter(label = "Mock"): ProviderAdapter {
  return {
    id: "mock",
    label,
    async isAvailable() {
      return { ok: true };
    },
    async send(history: Message[]) {
      const turns = history.filter((m) => m.role === "user").length;
      const prompt = latestUserPrompt(history);
      await new Promise((r) => setTimeout(r, 200 + Math.random() * 400));
      return `[${label}] turn #${turns}. You said: "${prompt}". (This is a mock reply; my isolated history has ${history.length} messages.)`;
    },
  };
}
