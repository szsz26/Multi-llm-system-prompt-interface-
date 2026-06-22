import type { Message, ProviderAdapter, ProviderConfig } from "../types.js";
import { chatCompletion } from "./base/openaiCompat.js";

/** Grok via the x.ai API (OpenAI-compatible). No official CLI, so API-backed. */
export function createGrokAdapter(cfg: ProviderConfig): ProviderAdapter {
  const apiKey = process.env.XAI_API_KEY ?? "";
  const model = cfg.model ?? "grok-4";
  return {
    id: "grok",
    label: "Grok",
    async isAvailable() {
      if (apiKey) return { ok: true };
      return { ok: false, reason: "XAI_API_KEY not set" };
    },
    async send(history: Message[]) {
      return chatCompletion(history, {
        baseUrl: "https://api.x.ai/v1",
        apiKey,
        model,
      });
    },
  };
}
