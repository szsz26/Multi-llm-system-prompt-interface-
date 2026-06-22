import { diffWords } from "diff";

export interface DiffPart {
  value: string;
  added?: boolean;
  removed?: boolean;
}

/**
 * Word-level diff between two provider responses. `removed` parts are unique to
 * `a`, `added` parts are unique to `b`, unmarked parts are shared.
 */
export function diffPair(a: string, b: string): DiffPart[] {
  return diffWords(a ?? "", b ?? "").map((p) => ({
    value: p.value,
    added: p.added,
    removed: p.removed,
  }));
}

/** A quick at-a-glance comparison stat per provider. */
export interface ResponseStat {
  providerId: string;
  chars: number;
  words: number;
}

export function responseStats(responses: Record<string, string>): ResponseStat[] {
  return Object.entries(responses).map(([providerId, text]) => ({
    providerId,
    chars: text.length,
    words: text.trim() ? text.trim().split(/\s+/).length : 0,
  }));
}
