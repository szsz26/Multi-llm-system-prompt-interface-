import type { Message, ProviderAdapter } from "../types.js";
import { latestUserPrompt } from "./base/cli.js";

/**
 * Offline provider for testing the fan-out, isolation, toggles and compare view
 * without any CLIs or API keys. Echoes the prompt and reports how many turns it
 * has seen in ITS OWN history (proving context isolation).
 */
export function createMockAdapter(label = "Mock", flavor = ""): ProviderAdapter {
  return {
    id: "mock",
    label,
    async isAvailable() {
      return { ok: true };
    },
    async send(history: Message[], onToken) {
      const turns = history.filter((m) => m.role === "user").length;
      const prompt = latestUserPrompt(history);
      const reply =
        `[${label}] turn #${turns}. You asked: "${prompt}". ` +
        (flavor ? `${flavor} ` : "") +
        `My isolated history has ${history.length} messages, so I only ever see my own thread.`;
      if (onToken) {
        // Emit word-by-word to exercise the streaming UI offline.
        let acc = "";
        for (const word of reply.split(" ")) {
          await new Promise((r) => setTimeout(r, 25));
          const chunk = (acc ? " " : "") + word;
          acc += chunk;
          onToken(chunk);
        }
        return reply;
      }
      await new Promise((r) => setTimeout(r, 200 + Math.random() * 400));
      return reply;
    },
  };
}
