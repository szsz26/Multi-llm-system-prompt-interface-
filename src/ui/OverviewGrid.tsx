import React from "react";
import { Box, Text } from "ink";
import type { ProviderState } from "../types.js";

interface Column {
  provider: ProviderState;
  /** Latest assistant reply (from the store) for this provider. */
  latest?: string;
  /** Live streaming text while running. */
  streaming?: string;
  error?: string;
}

interface Props {
  columns: Column[];
  focusedCol: number;
}

function glyph(p: ProviderState): { g: string; color: string } {
  if (!p.available) return { g: "⊘", color: "gray" };
  return p.active ? { g: "●", color: "green" } : { g: "○", color: "yellow" };
}

function statusText(p: ProviderState): { text: string; color: string } {
  if (!p.available) return { text: "unavailable", color: "gray" };
  if (!p.active) return { text: "paused", color: "yellow" };
  switch (p.status) {
    case "running":
      return { text: "…streaming", color: "cyan" };
    case "error":
      return { text: "error", color: "red" };
    case "done":
      return { text: "done", color: "green" };
    default:
      return { text: "ready", color: "green" };
  }
}

/** All providers shown side-by-side as live columns; one prompt fills them all. */
export function OverviewGrid({ columns, focusedCol }: Props) {
  if (columns.length === 0) return <Text color="gray">No providers configured.</Text>;

  return (
    <Box flexDirection="row" flexGrow={1}>
      {columns.map((col, i) => {
        const { provider: p } = col;
        const focused = i === focusedCol;
        const dim = !p.available || !p.active;
        const st = statusText(p);
        const { g, color } = glyph(p);
        const body =
          p.status === "running"
            ? col.streaming && col.streaming.length > 0
              ? col.streaming
              : "…thinking"
            : col.error
              ? `⚠ ${col.error}`
              : (col.latest ?? "");

        return (
          <Box
            key={p.id}
            flexDirection="column"
            flexGrow={1}
            flexBasis={0}
            marginRight={i < columns.length - 1 ? 1 : 0}
            borderStyle={focused ? "round" : "single"}
            borderColor={focused ? "magenta" : dim ? "gray" : "cyan"}
            paddingX={1}
          >
            <Box>
              <Text color={color}>{g} </Text>
              <Text bold dimColor={dim}>
                {i + 1}:{p.label}
              </Text>
            </Box>
            <Text color={st.color}>[{st.text}]</Text>
            <Box marginTop={1}>
              <Text dimColor={dim} color={col.error ? "red" : undefined} wrap="wrap">
                {body || "—"}
              </Text>
            </Box>
          </Box>
        );
      })}
    </Box>
  );
}
