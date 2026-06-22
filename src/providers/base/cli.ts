import { execa } from "execa";
import type { Message } from "../../types.js";

/** Check whether a command exists on PATH. */
export async function commandExists(cmd: string): Promise<boolean> {
  try {
    await execa("which", [cmd]);
    return true;
  } catch {
    return false;
  }
}

/**
 * Flatten an isolated history into a single prompt string for stateless CLIs.
 * Only contains THIS provider's own turns, so no cross-provider leakage.
 */
export function renderHistoryAsPrompt(history: Message[]): string {
  if (history.length === 1) return history[0]!.content;
  return history
    .map((m) => (m.role === "user" ? `User: ${m.content}` : `Assistant: ${m.content}`))
    .join("\n\n");
}

/** The latest user turn (the prompt we are answering now). */
export function latestUserPrompt(history: Message[]): string {
  for (let i = history.length - 1; i >= 0; i--) {
    if (history[i]!.role === "user") return history[i]!.content;
  }
  return "";
}

export interface RunResult {
  stdout: string;
  stderr: string;
}

/** Run a CLI command, feeding the prompt via argument or stdin. */
export async function runCli(
  command: string,
  args: string[],
  opts: { input?: string; timeoutMs?: number } = {},
): Promise<RunResult> {
  const result = await execa(command, args, {
    input: opts.input,
    timeout: opts.timeoutMs ?? 180_000,
    reject: true,
  });
  return { stdout: result.stdout ?? "", stderr: result.stderr ?? "" };
}
