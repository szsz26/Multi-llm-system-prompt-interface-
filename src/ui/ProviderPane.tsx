import React from "react";
import { Box, Text } from "ink";
import type { Message, ProviderState } from "../types.js";

interface Props {
  provider: ProviderState;
  history: Message[];
  error?: string;
  /** Live partial text while the provider is streaming this turn. */
  streaming?: string;
}

function statusLabel(p: ProviderState): { text: string; color: string } {
  if (!p.available) return { text: "unavailable", color: "gray" };
  if (!p.active) return { text: "paused", color: "yellow" };
  switch (p.status) {
    case "running":
      return { text: "running…", color: "cyan" };
    case "error":
      return { text: "error", color: "red" };
    case "done":
      return { text: "done", color: "green" };
    default:
      return { text: "ready", color: "green" };
  }
}

/** Full-pane view of a single provider's isolated transcript. */
export function ProviderPane({ provider, history, error, streaming }: Props) {
  const status = statusLabel(provider);
  const isStreaming = provider.status === "running";
  return (
    <Box flexDirection="column" flexGrow={1} paddingX={1}>
      <Box>
        <Text bold>{provider.label} </Text>
        <Text color={status.color}>[{status.text}]</Text>
        {provider.unavailableReason ? (
          <Text color="gray"> — {provider.unavailableReason}</Text>
        ) : null}
      </Box>
      <Box marginTop={1} flexDirection="column">
        {history.length === 0 ? (
          <Text color="gray">No messages yet.</Text>
        ) : (
          history.map((m, i) => (
            <Box key={i} flexDirection="column" marginBottom={1}>
              <Text color={m.role === "user" ? "magenta" : "white"} bold>
                {m.role === "user" ? "You" : provider.label}
              </Text>
              <Text>{m.content}</Text>
            </Box>
          ))
        )}
        {isStreaming ? (
          <Box flexDirection="column" marginBottom={1}>
            <Text color="white" bold>
              {provider.label} <Text color="cyan">▌</Text>
            </Text>
            <Text>{streaming && streaming.length > 0 ? streaming : <Text color="gray">…thinking</Text>}</Text>
          </Box>
        ) : null}
        {error ? <Text color="red">⚠ {error}</Text> : null}
      </Box>
    </Box>
  );
}
