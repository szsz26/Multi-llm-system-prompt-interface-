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
 *
 * If `onToken` is supplied, requests a streamed response and emits content
 * deltas as they arrive, returning the full text at the end.
 */
export async function chatCompletion(
  history: Message[],
  opts: OpenAICompatOptions,
  onToken?: (delta: string) => void,
): Promise<string> {
  const messages = [
    ...(opts.system ? [{ role: "system" as const, content: opts.system }] : []),
    ...history.map((m) => ({ role: m.role, content: m.content })),
  ];

  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), opts.timeoutMs ?? 180_000);
  const stream = Boolean(onToken);

  try {
    const res = await fetch(`${opts.baseUrl}/chat/completions`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${opts.apiKey}`,
      },
      body: JSON.stringify({ model: opts.model, messages, stream }),
      signal: controller.signal,
    });

    if (!res.ok) {
      const body = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status} ${res.statusText}${body ? `: ${body.slice(0, 300)}` : ""}`);
    }

    if (!stream) {
      const data = (await res.json()) as { choices?: { message?: { content?: string } }[] };
      const text = data.choices?.[0]?.message?.content;
      if (!text) throw new Error("Empty response from API");
      return text.trim();
    }

    return await readSseStream(res, onToken!);
  } finally {
    clearTimeout(timeout);
  }
}

/** Parse a Server-Sent-Events chat-completion stream into full text. */
async function readSseStream(res: Response, onToken: (delta: string) => void): Promise<string> {
  if (!res.body) throw new Error("No response body to stream");
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let full = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });

    let idx: number;
    while ((idx = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (!line.startsWith("data:")) continue;
      const payload = line.slice(5).trim();
      if (payload === "[DONE]") continue;
      try {
        const json = JSON.parse(payload) as { choices?: { delta?: { content?: string } }[] };
        const delta = json.choices?.[0]?.delta?.content;
        if (delta) {
          full += delta;
          onToken(delta);
        }
      } catch {
        // Ignore keep-alive / non-JSON lines.
      }
    }
  }

  if (!full) throw new Error("Empty streamed response from API");
  return full.trim();
}
