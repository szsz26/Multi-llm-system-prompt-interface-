import type { Message, ProviderAdapter, ProviderConfig } from "../types.js";
import { chatCompletion } from "./base/openaiCompat.js";

/** Perplexity via its API (OpenAI-compatible). No official CLI, so API-backed. */
export function createPerplexityAdapter(cfg: ProviderConfig): ProviderAdapter {
  const apiKey = process.env.PERPLEXITY_API_KEY ?? "";
  const model = cfg.model ?? "sonar-pro";
  return {
    id: "perplexity",
    label: "Perplexity",
    async isAvailable() {
      if (apiKey) return { ok: true };
      return { ok: false, reason: "PERPLEXITY_API_KEY not set" };
    },
    async send(history: Message[]) {
      return chatCompletion(history, {
        baseUrl: "https://api.perplexity.ai",
        apiKey,
        model,
      });
    },
  };
}
