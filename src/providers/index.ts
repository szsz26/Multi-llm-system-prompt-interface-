import type { AppConfig, ProviderAdapter } from "../types.js";
import type { ConversationStore } from "../conversation.js";
import { createClaudeAdapter } from "./claude.js";
import { createCodexAdapter } from "./codex.js";
import { createGeminiAdapter } from "./gemini.js";
import { createGrokAdapter } from "./grok.js";
import { createPerplexityAdapter } from "./perplexity.js";
import { createMockAdapter } from "./mock.js";

/** Build the set of enabled adapters from config (order is the display order). */
export function buildAdapters(config: AppConfig, store: ConversationStore): ProviderAdapter[] {
  const adapters: ProviderAdapter[] = [];
  const p = config.providers;

  const sessionFor = (id: string) => ({
    get: () => store.getSessionId(id),
    set: (sid: string) => store.setSessionId(id, sid),
  });

  if (p.claude?.enabled) adapters.push(createClaudeAdapter(p.claude, sessionFor("claude")));
  if (p.codex?.enabled) adapters.push(createCodexAdapter(p.codex));
  if (p.gemini?.enabled) adapters.push(createGeminiAdapter(p.gemini));
  if (p.grok?.enabled) adapters.push(createGrokAdapter(p.grok));
  if (p.perplexity?.enabled) adapters.push(createPerplexityAdapter(p.perplexity));
  if (p.mock?.enabled) adapters.push(createMockAdapter());

  return adapters;
}

/**
 * Offline demo: several distinct mock providers (no CLIs or keys needed) so you
 * can try the panes, toggles, streaming, diff and synthesis end-to-end. Each
 * gives slightly different wording so the diff/compare view is meaningful.
 */
export function buildDemoAdapters(): ProviderAdapter[] {
  const flavored: Array<[string, string, string]> = [
    ["mock-claude", "Claude (demo)", "I'd weigh the trade-offs carefully and favor clarity."],
    ["mock-openai", "OpenAI (demo)", "Here is a concise, structured take on the question."],
    ["mock-gemini", "Gemini (demo)", "Let me add a slightly different angle and an example."],
  ];
  return flavored.map(([id, label, flavor]) => ({ ...createMockAdapter(label, flavor), id }));
}
