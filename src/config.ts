import { readFileSync, existsSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import dotenv from "dotenv";
import type { AppConfig, ProviderConfig } from "./types.js";

dotenv.config();

const __dirname = dirname(fileURLToPath(import.meta.url));
/** Project root (one level up from src/ or dist/). */
export const PROJECT_ROOT = resolve(__dirname, "..");

const DEFAULT_CONFIG: AppConfig = {
  synthesisProvider: "claude",
  providers: {
    claude: { enabled: true, command: "claude" },
    codex: { enabled: true, command: "codex" },
    gemini: { enabled: true, command: "gemini" },
    grok: { enabled: true, model: "grok-4" },
    perplexity: { enabled: true, model: "sonar-pro" },
    mock: { enabled: false },
  },
};

/** Per-provider model override via env, e.g. GROK_MODEL, PERPLEXITY_MODEL. */
function modelEnvOverride(id: string): string | undefined {
  return process.env[`${id.toUpperCase()}_MODEL`];
}

export function loadConfig(): AppConfig {
  const path = resolve(PROJECT_ROOT, "config.json");
  let cfg: AppConfig = DEFAULT_CONFIG;

  if (existsSync(path)) {
    try {
      const parsed = JSON.parse(readFileSync(path, "utf8")) as Partial<AppConfig>;
      cfg = {
        synthesisProvider: parsed.synthesisProvider ?? DEFAULT_CONFIG.synthesisProvider,
        providers: { ...DEFAULT_CONFIG.providers, ...(parsed.providers ?? {}) },
      };
    } catch (err) {
      // Fall back to defaults but surface the problem.
      process.stderr.write(
        `Warning: could not parse config.json (${(err as Error).message}); using defaults.\n`,
      );
    }
  }

  // Apply env model overrides on top of config.json.
  for (const [id, pc] of Object.entries(cfg.providers)) {
    const override = modelEnvOverride(id);
    if (override) (pc as ProviderConfig).model = override;
  }

  return cfg;
}
