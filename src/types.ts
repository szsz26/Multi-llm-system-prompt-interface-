/** A single turn in a conversation. */
export interface Message {
  role: "user" | "assistant";
  content: string;
}

/** Result of a single provider answering a single turn. */
export interface ProviderResponse {
  providerId: string;
  text: string;
  ok: boolean;
  error?: string;
  ms: number;
}

/** Live UI/runtime state for a provider. */
export interface ProviderState {
  id: string;
  label: string;
  /** CLI is on PATH or required API key is present. */
  available: boolean;
  /** Why it is unavailable (shown in the UI). */
  unavailableReason?: string;
  /** User toggle: included in the prompt blast when true (and available). */
  active: boolean;
  /** Latest run status for the pane header. */
  status: "idle" | "running" | "done" | "error";
}

/**
 * Common interface every provider adapter implements. An adapter receives ONLY
 * its own conversation history, so contexts never blur between providers.
 */
export interface ProviderAdapter {
  id: string;
  label: string;
  /** True if this provider can actually be used right now. */
  isAvailable(): Promise<{ ok: boolean; reason?: string }>;
  /** Answer the next turn given this provider's full, isolated history. */
  send(history: Message[]): Promise<string>;
}

/** Lets a CLI adapter persist/recall a native session id for resuming context. */
export interface SessionAccessor {
  get(): string | undefined;
  set(id: string): void;
}

/** Per-provider settings from config.json. */
export interface ProviderConfig {
  /** If false, the provider is never loaded. */
  enabled: boolean;
  /** Override the CLI binary name/path (CLI providers only). */
  command?: string;
  /** Model id (API providers, and CLI providers that accept --model). */
  model?: string;
  /** Force API mode even when a CLI exists (where supported). */
  useApi?: boolean;
}

export interface AppConfig {
  providers: Record<string, ProviderConfig>;
  /** Which provider id powers the AI synthesis/compare step. */
  synthesisProvider: string;
}
