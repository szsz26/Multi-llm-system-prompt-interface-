import type { Message, ProviderAdapter, ProviderConfig, SessionAccessor } from "../types.js";
import {
  commandExists,
  latestUserPrompt,
  makeLineSplitter,
  renderHistoryAsPrompt,
  runCli,
  runCliStream,
} from "./base/cli.js";

/**
 * Claude via the `claude` CLI in print mode. Uses native session resume
 * (`--resume <id>`) so context continues without us re-sending the transcript;
 * the session id only ever maps to Claude's own thread.
 */
export function createClaudeAdapter(cfg: ProviderConfig, session: SessionAccessor): ProviderAdapter {
  const command = cfg.command ?? "claude";
  return {
    id: "claude",
    label: "Claude",
    async isAvailable() {
      if (await commandExists(command)) return { ok: true };
      return { ok: false, reason: `'${command}' CLI not found on PATH` };
    },
    async send(history: Message[], onToken) {
      const prompt = latestUserPrompt(history);
      const sessionId = session.get();

      if (onToken) {
        return sendStreaming(command, cfg, prompt, sessionId, history, session, onToken);
      }

      const args = ["-p", "--output-format", "json"];
      if (cfg.model) args.push("--model", cfg.model);

      // First turn starts a session; later turns resume it.
      if (sessionId) args.push("--resume", sessionId);
      args.push(prompt);

      try {
        const { stdout } = await runCli(command, args);
        const parsed = JSON.parse(stdout) as { result?: string; session_id?: string };
        if (parsed.session_id) session.set(parsed.session_id);
        return (parsed.result ?? stdout).trim();
      } catch (err) {
        // If resume fails (e.g. expired session), retry once by replaying the
        // full isolated transcript as a fresh prompt.
        if (sessionId) {
          const { stdout } = await runCli(command, [
            "-p",
            "--output-format",
            "json",
            ...(cfg.model ? ["--model", cfg.model] : []),
            renderHistoryAsPrompt(history),
          ]);
          const parsed = JSON.parse(stdout) as { result?: string; session_id?: string };
          if (parsed.session_id) session.set(parsed.session_id);
          return (parsed.result ?? stdout).trim();
        }
        throw err;
      }
    },
  };
}

/**
 * Streamed Claude turn using `--output-format stream-json`. Emits text deltas
 * as they arrive and captures the session id from the final `result` event.
 */
async function sendStreaming(
  command: string,
  cfg: ProviderConfig,
  prompt: string,
  sessionId: string | undefined,
  history: Message[],
  session: SessionAccessor,
  onToken: (delta: string) => void,
): Promise<string> {
  const buildArgs = (resume?: string, text = prompt) => {
    const args = [
      "-p",
      "--output-format",
      "stream-json",
      "--verbose",
      "--include-partial-messages",
    ];
    if (cfg.model) args.push("--model", cfg.model);
    if (resume) args.push("--resume", resume);
    args.push(text);
    return args;
  };

  const run = async (resume?: string, text = prompt): Promise<string> => {
    let full = "";
    let gotDelta = false;
    let resultText: string | undefined;

    const onLine = (line: string) => {
      let obj: any;
      try {
        obj = JSON.parse(line);
      } catch {
        return;
      }
      // Token-level deltas (with --include-partial-messages).
      if (obj.type === "stream_event" && obj.event?.type === "content_block_delta") {
        const delta = obj.event.delta?.text;
        if (delta) {
          gotDelta = true;
          full += delta;
          onToken(delta);
        }
        return;
      }
      // Whole assistant message (fallback when deltas aren't emitted).
      if (obj.type === "assistant" && !gotDelta) {
        const blocks = obj.message?.content ?? [];
        const text = blocks
          .filter((b: any) => b.type === "text")
          .map((b: any) => b.text)
          .join("");
        if (text) {
          full += text;
          onToken(text);
        }
        return;
      }
      if (obj.type === "result") {
        if (obj.session_id) session.set(obj.session_id);
        if (typeof obj.result === "string") resultText = obj.result;
      }
    };

    await runCliStream(command, buildArgs(resume, text), { onChunk: makeLineSplitter(onLine) });
    return (resultText ?? full).trim();
  };

  try {
    return await run(sessionId);
  } catch (err) {
    // Resume failed — replay the isolated transcript as a fresh session.
    if (sessionId) return run(undefined, renderHistoryAsPrompt(history));
    throw err;
  }
}
