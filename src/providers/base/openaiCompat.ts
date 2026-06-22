import type { Message } from "../../types.js";

export interface OpenAICompatOptions {
  baseUrl: string;
  apiKey: string;
  model: string;
  /** Optional system prompt prepended to the conversation. */
  system?: string;
  timeoutMs?: number;
}

/**
 * Call any OpenAI-compatible /chat/completions endpoint (Grok, Perplexity,
 * OpenAI). Passes the native messages array so multi-turn context works for
 * free — and only this provider's own history is ever included.
 */
export async function chatCompletion(
  history: Message[],
  opts: OpenAICompatOptions,
): Promise<string> {
  const messages = [
    ...(opts.system ? [{ role: "system" as const, content: opts.system }] : []),
    ...history.map((m) => ({ role: m.role, content: m.content })),
  ];

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), opts.timeoutMs ?? 180_000);

  try {
    const res = await fetch(`${opts.baseUrl}/chat/completions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${opts.apiKey}`,
      },
      body: JSON.stringify({ model: opts.model, messages }),
      signal: controller.signal,
    });

    if (!res.ok) {
      const body = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status} ${res.statusText}${body ? `: ${body.slice(0, 300)}` : ""}`);
    }

    const data = (await res.json()) as {
      choices?: { message?: { content?: string } }[];
    };
    const text = data.choices?.[0]?.message?.content;
    if (!text) throw new Error("Empty response from API");
    return text.trim();
  } finally {
    clearTimeout(timeout);
  }
}
