import React from "react";
import { Box, Text } from "ink";
import type { ProviderState } from "../types.js";

interface Props {
  providers: ProviderState[];
  focusedCol: number;
  mode: "insert" | "nav";
  view: "grid" | "combined";
}

function toggleGlyph(p: ProviderState): { glyph: string; color: string } {
  if (!p.available) return { glyph: "⊘", color: "gray" };
  return p.active ? { glyph: "●", color: "green" } : { glyph: "○", color: "yellow" };
}

/** Bottom bar: per-provider toggle state + key hints. */
export function StatusBar({ providers, focusedCol, mode, view }: Props) {
  return (
    <Box flexDirection="column" borderStyle="single" borderColor="gray" paddingX={1}>
      <Box flexWrap="wrap">
        {providers.map((p, i) => {
          const { glyph, color } = toggleGlyph(p);
          const focused = view === "grid" && i === focusedCol;
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
          <Text bold={view === "combined"} underline={view === "combined"} color="magenta">
            [c]ompare
          </Text>
        </Box>
      </Box>
      <Box>
        <Text color="gray">
          {mode === "insert"
            ? "● on = gets the prompt  ·  [Enter] send  ·  [Esc] nav mode"
            : view === "combined"
              ? "[g/Esc] back to grid  ·  [r] reset all  ·  [i] type  ·  [q] quit"
              : "[←→/1-9] pick model  ·  [t/space] toggle on/off  ·  [c]ompare  ·  [r] reset  ·  [i] type  ·  [q] quit"}
        </Text>
      </Box>
    </Box>
  );
}
