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

/**
 * Like runCli, but streams stdout: `onChunk` is called with each chunk as it
 * arrives. Returns the fully accumulated stdout/stderr when the process exits.
 */
export async function runCliStream(
  command: string,
  args: string[],
  opts: { input?: string; timeoutMs?: number; onChunk: (chunk: string) => void },
): Promise<RunResult> {
  const sub = execa(command, args, {
    input: opts.input,
    timeout: opts.timeoutMs ?? 180_000,
    buffer: false,
    reject: true,
  });

  let stdout = "";
  let stderr = "";
  sub.stdout?.on("data", (d: Buffer) => {
    const s = d.toString();
    stdout += s;
    opts.onChunk(s);
  });
  sub.stderr?.on("data", (d: Buffer) => {
    stderr += d.toString();
  });

  await sub;
  return { stdout, stderr };
}

/** Splits a stream of arbitrary chunks into complete lines for JSONL parsing. */
export function makeLineSplitter(onLine: (line: string) => void): (chunk: string) => void {
  let buffer = "";
  return (chunk: string) => {
    buffer += chunk;
    let idx: number;
    while ((idx = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, idx).trim();
      buffer = buffer.slice(idx + 1);
      if (line) onLine(line);
    }
  };
}
