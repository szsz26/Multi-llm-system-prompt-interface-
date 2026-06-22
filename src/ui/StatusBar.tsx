import React from "react";
import { Box, Text } from "ink";
import type { ProviderState } from "../types.js";

interface Props {
  providers: ProviderState[];
  activePane: number;
  mode: "insert" | "nav";
}

function toggleGlyph(p: ProviderState): { glyph: string; color: string } {
  if (!p.available) return { glyph: "⊘", color: "gray" };
  return p.active ? { glyph: "●", color: "green" } : { glyph: "○", color: "yellow" };
}

/** Bottom bar: per-provider toggle state + key hints. */
export function StatusBar({ providers, activePane, mode }: Props) {
  return (
    <Box flexDirection="column" borderStyle="single" borderColor="gray" paddingX={1}>
      <Box flexWrap="wrap">
        {providers.map((p, i) => {
          const { glyph, color } = toggleGlyph(p);
          const focused = i === activePane;
          return (
            <Box key={p.id} marginRight={2}>
              <Text color={color}>{glyph} </Text>
              <Text bold={focused} underline={focused} color={p.available ? undefined : "gray"}>
                {i + 1}:{p.label}
              </Text>
              {p.status === "running" ? <Text color="cyan"> …</Text> : null}
              {p.status === "error" ? <Text color="red"> !</Text> : null}
            </Box>
          );
        })}
        <Box>
          <Text bold={activePane === providers.length} underline={activePane === providers.length}>
            {providers.length + 1}:Combined
          </Text>
        </Box>
      </Box>
      <Box>
        <Text color="gray">
          {mode === "insert"
            ? "[Enter] send to active  ·  [Esc] nav mode"
            : "[i] type  ·  [1-9/←→] pane  ·  [t/space] toggle  ·  [c] compare  ·  [r] reset  ·  [q] quit"}
        </Text>
      </Box>
    </Box>
  );
}
