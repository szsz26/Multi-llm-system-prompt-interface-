import type { ProviderAdapter } from "../types.js";

export interface NamedResponse {
  label: string;
  text: string;
}

/**
 * Ask one chosen model to compare all the responses and summarize points of
 * agreement, disagreement, and anything unique to a single model. This is a
 * one-off call with no stored history, so it never pollutes any provider thread.
 */
export async function synthesize(
  prompt: string,
  responses: NamedResponse[],
  synthesizer: ProviderAdapter,
): Promise<string> {
  const sections = responses
    .filter((r) => r.text.trim())
    .map((r) => `### ${r.label}\n${r.text}`)
    .join("\n\n");

  const instruction = [
    "You are comparing answers from multiple AI assistants to the same prompt.",
    "",
    `Original prompt:\n"""${prompt}"""`,
    "",
    "Here are their responses:",
    "",
    sections,
    "",
    "Produce a concise comparison with these sections:",
    "1. **Consensus** — what they agree on.",
    "2. **Disagreements** — where they differ, and how.",
    "3. **Unique points** — anything raised by only one model (name it).",
    "4. **Bottom line** — a one-paragraph synthesized best answer.",
  ].join("\n");

  return synthesizer.send([{ role: "user", content: instruction }]);
}
