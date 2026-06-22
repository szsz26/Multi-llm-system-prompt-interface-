import { readFileSync, writeFileSync, existsSync, mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { PROJECT_ROOT } from "./config.js";
import type { Message } from "./types.js";

/**
 * Holds a separate, isolated message history per provider and (optionally)
 * persists it to disk so a session survives restarts. Each provider only ever
 * sees its own thread — this is what keeps contexts from blurring.
 */
export class ConversationStore {
  private histories: Record<string, Message[]> = {};
  /** Native session ids returned by some CLIs (e.g. claude --resume). */
  private sessionIds: Record<string, string> = {};
  private readonly file: string;

  constructor(sessionName = "default") {
    const dir = resolve(PROJECT_ROOT, ".sessions");
    if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
    this.file = resolve(dir, `${sessionName}.json`);
    this.load();
  }

  private load(): void {
    if (!existsSync(this.file)) return;
    try {
      const data = JSON.parse(readFileSync(this.file, "utf8"));
      this.histories = data.histories ?? {};
      this.sessionIds = data.sessionIds ?? {};
    } catch {
      // Corrupt session file — start fresh rather than crash.
      this.histories = {};
      this.sessionIds = {};
    }
  }

  private persist(): void {
    try {
      writeFileSync(
        this.file,
        JSON.stringify({ histories: this.histories, sessionIds: this.sessionIds }, null, 2),
      );
    } catch {
      // Persistence is best-effort; never crash the UI over it.
    }
  }

  history(providerId: string): Message[] {
    return this.histories[providerId] ?? [];
  }

  /** Append a user turn to one provider's isolated history. */
  addUser(providerId: string, content: string): void {
    (this.histories[providerId] ??= []).push({ role: "user", content });
    this.persist();
  }

  /** Append the assistant's reply to one provider's isolated history. */
  addAssistant(providerId: string, content: string): void {
    (this.histories[providerId] ??= []).push({ role: "assistant", content });
    this.persist();
  }

  getSessionId(providerId: string): string | undefined {
    return this.sessionIds[providerId];
  }

  setSessionId(providerId: string, id: string): void {
    this.sessionIds[providerId] = id;
    this.persist();
  }

  reset(providerId?: string): void {
    if (providerId) {
      delete this.histories[providerId];
      delete this.sessionIds[providerId];
    } else {
      this.histories = {};
      this.sessionIds = {};
    }
    this.persist();
  }
}
