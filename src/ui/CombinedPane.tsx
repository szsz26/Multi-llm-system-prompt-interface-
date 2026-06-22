import React from "react";
import { Box, Text } from "ink";
import { diffPair, responseStats } from "../compare/diff.js";

interface Props {
  /** providerId -> latest response text (only providers that answered). */
  responses: Record<string, string>;
  labels: Record<string, string>;
  synthesis?: string;
  synthesizing: boolean;
  /** The two provider ids currently being diffed (auto-selected). */
  diffPairIds: [string, string] | null;
}

/** Combined view: stats, AI synthesis, and a word-level diff of two answers. */
export function CombinedPane({ responses, labels, synthesis, synthesizing, diffPairIds }: Props) {
  const stats = responseStats(responses);
  const parts =
    diffPairIds && responses[diffPairIds[0]] != null && responses[diffPairIds[1]] != null
      ? diffPair(responses[diffPairIds[0]]!, responses[diffPairIds[1]]!)
      : null;

  return (
    <Box flexDirection="column" flexGrow={1} paddingX={1}>
      <Text bold>Combined view</Text>

      <Box marginTop={1} flexDirection="column">
        <Text bold color="cyan">Lengths</Text>
        {stats.length === 0 ? (
          <Text color="gray">No responses yet — send a prompt.</Text>
        ) : (
          stats.map((s) => (
            <Text key={s.providerId}>
              {(labels[s.providerId] ?? s.providerId).padEnd(16)} {s.words} words · {s.chars} chars
            </Text>
          ))
        )}
      </Box>

      <Box marginTop={1} flexDirection="column">
        <Text bold color="cyan">AI synthesis</Text>
        {synthesizing ? (
          <Text color="cyan">Comparing responses…</Text>
        ) : synthesis ? (
          <Text>{synthesis}</Text>
        ) : (
          <Text color="gray">Press [c] in nav mode to generate a comparison.</Text>
        )}
      </Box>

      {parts ? (
        <Box marginTop={1} flexDirection="column">
          <Text bold color="cyan">
            Diff: {labels[diffPairIds![0]] ?? diffPairIds![0]} (red) vs{" "}
            {labels[diffPairIds![1]] ?? diffPairIds![1]} (green)
          </Text>
          <Text>
            {parts.map((p, i) => (
              <Text
                key={i}
                color={p.removed ? "red" : p.added ? "green" : undefined}
                dimColor={!p.added && !p.removed}
              >
                {p.value}
              </Text>
            ))}
          </Text>
        </Box>
      ) : null}
    </Box>
  );
}
