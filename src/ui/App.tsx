import React, { useEffect, useMemo, useState } from "react";
import { Box, Text, useApp, useInput } from "ink";
import TextInput from "ink-text-input";
import type { AppConfig, ProviderAdapter, ProviderResponse, ProviderState } from "../types.js";
import type { ConversationStore } from "../conversation.js";
import { blast } from "../orchestrator.js";
import { synthesize, type NamedResponse } from "../compare/synthesize.js";
import { StatusBar } from "./StatusBar.js";
import { OverviewGrid } from "./OverviewGrid.js";
import { CombinedPane } from "./CombinedPane.js";

interface Props {
  adapters: ProviderAdapter[];
  store: ConversationStore;
  config: AppConfig;
}

export function App({ adapters, store, config }: Props) {
  const { exit } = useApp();
  const labels = useMemo(() => Object.fromEntries(adapters.map((a) => [a.id, a.label])), [adapters]);

  const [providers, setProviders] = useState<ProviderState[]>(
    adapters.map((a) => ({ id: a.id, label: a.label, available: false, active: true, status: "idle" })),
  );
  const [mode, setMode] = useState<"insert" | "nav">("insert");
  const [view, setView] = useState<"grid" | "combined">("grid");
  const [focusedCol, setFocusedCol] = useState(0);
  const [input, setInput] = useState("");
  const [busy, setBusy] = useState(false);
  const [responses, setResponses] = useState<Record<string, string>>({});
  const [partials, setPartials] = useState<Record<string, string>>({});
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [synthesis, setSynthesis] = useState<string | undefined>();
  const [synthesizing, setSynthesizing] = useState(false);
  const [lastPrompt, setLastPrompt] = useState("");
  const [, setTick] = useState(0);
  const bump = () => setTick((t) => t + 1);

  // Detect availability once on mount.
  useEffect(() => {
    let cancelled = false;
    (async () => {
      const checked = await Promise.all(
        adapters.map(async (a) => {
          const r = await a.isAvailable();
          return { id: a.id, ok: r.ok, reason: r.reason };
        }),
      );
      if (cancelled) return;
      setProviders((prev) =>
        prev.map((p) => {
          const c = checked.find((x) => x.id === p.id)!;
          return { ...p, available: c.ok, active: c.ok, unavailableReason: c.reason };
        }),
      );
    })();
    return () => {
      cancelled = true;
    };
  }, [adapters]);

  const setStatus = (id: string, status: ProviderState["status"]) =>
    setProviders((prev) => prev.map((p) => (p.id === id ? { ...p, status } : p)));

  const onUpdate = (
    id: string,
    update: Partial<ProviderResponse> & { status: "running" | "done" | "error" },
  ) => {
    setStatus(id, update.status);
    if (update.status === "running") {
      setPartials((p) => ({ ...p, [id]: "" }));
    }
    if (update.status === "done" && typeof update.text === "string") {
      setResponses((r) => ({ ...r, [id]: update.text! }));
      setPartials((p) => {
        const { [id]: _, ...rest } = p;
        return rest;
      });
      setErrors((e) => {
        const { [id]: _, ...rest } = e;
        return rest;
      });
    }
    if (update.status === "error" && update.error) {
      setErrors((e) => ({ ...e, [id]: update.error! }));
      setPartials((p) => {
        const { [id]: _, ...rest } = p;
        return rest;
      });
    }
    bump();
  };

  const onToken = (id: string, delta: string) => {
    setPartials((p) => ({ ...p, [id]: (p[id] ?? "") + delta }));
  };

  const activeAdapters = () =>
    adapters.filter((a) => {
      const p = providers.find((x) => x.id === a.id);
      return p?.available && p.active;
    });

  const submit = async (value: string) => {
    const prompt = value.trim();
    if (!prompt || busy) return;
    const targets = activeAdapters();
    if (targets.length === 0) return;
    setInput("");
    setLastPrompt(prompt);
    setSynthesis(undefined);
    setView("grid");
    setBusy(true);
    await blast(prompt, targets, store, onUpdate, onToken);
    setBusy(false);
    bump();
  };

  const runSynthesis = async () => {
    setView("combined");
    const named: NamedResponse[] = providers
      .filter((p) => responses[p.id]?.trim())
      .map((p) => ({ label: p.label, text: responses[p.id]! }));
    if (named.length < 2) {
      setSynthesis("Need at least 2 responses to compare. Send a prompt to multiple providers first.");
      return;
    }
    const synth =
      adapters.find((a) => {
        const p = providers.find((x) => x.id === a.id);
        return a.id === config.synthesisProvider && p?.available;
      }) ?? adapters.find((a) => providers.find((x) => x.id === a.id)?.available);
    if (!synth) {
      setSynthesis("No available provider to run the synthesis.");
      return;
    }
    setSynthesizing(true);
    try {
      const text = await synthesize(lastPrompt, named, synth);
      setSynthesis(text);
    } catch (err) {
      setSynthesis(`Synthesis failed: ${(err as Error).message}`);
    } finally {
      setSynthesizing(false);
    }
  };

  const toggleFocused = () =>
    setProviders((prev) =>
      prev.map((p, i) => (i === focusedCol && p.available ? { ...p, active: !p.active } : p)),
    );

  const resetFocused = () => {
    if (view === "combined") {
      store.reset();
      setResponses({});
      setSynthesis(undefined);
    } else {
      const id = providers[focusedCol]?.id;
      if (!id) return;
      store.reset(id);
      setResponses((r) => {
        const { [id]: _, ...rest } = r;
        return rest;
      });
    }
    bump();
  };

  useInput((key, special) => {
    if (special.escape) {
      if (view === "combined") setView("grid");
      else setMode("nav");
      return;
    }
    if (mode === "insert") return; // TextInput owns typing + Enter.

    if (key === "i") return setMode("insert");
    if (key === "q" || (special.ctrl && key === "c")) return exit();
    if (key === "g") return setView("grid");
    if (key === "c") return void runSynthesis();
    if (key === "r") return resetFocused();

    // Column navigation / selection only applies to the grid.
    if (special.leftArrow) return setFocusedCol((i) => Math.max(0, i - 1));
    if (special.rightArrow) return setFocusedCol((i) => Math.min(providers.length - 1, i + 1));
    if (/[1-9]/.test(key)) {
      const idx = parseInt(key, 10) - 1;
      if (idx < providers.length) {
        setFocusedCol(idx);
        setView("grid");
      }
      return;
    }
    if (key === "t" || key === " ") return toggleFocused();
  });

  const diffPairIds = useMemo<[string, string] | null>(() => {
    const withText = providers.filter((p) => responses[p.id]?.trim()).map((p) => p.id);
    return withText.length >= 2 ? [withText[0]!, withText[1]!] : null;
  }, [providers, responses]);

  const latestReply = (id: string): string | undefined => {
    const h = store.history(id);
    for (let i = h.length - 1; i >= 0; i--) if (h[i]!.role === "assistant") return h[i]!.content;
    return undefined;
  };

  const columns = providers.map((p) => ({
    provider: p,
    latest: latestReply(p.id),
    streaming: partials[p.id],
    error: errors[p.id],
  }));

  const activeCount = providers.filter((p) => p.available && p.active).length;

  return (
    <Box flexDirection="column" minHeight={20}>
      <Box flexGrow={1} flexDirection="column">
        {view === "combined" ? (
          <CombinedPane
            responses={responses}
            labels={labels}
            synthesis={synthesis}
            synthesizing={synthesizing}
            diffPairIds={diffPairIds}
          />
        ) : (
          <OverviewGrid columns={columns} focusedCol={focusedCol} />
        )}
      </Box>

      <Box paddingX={1}>
        <Text color={busy ? "yellow" : "magenta"}>{busy ? "⏳ " : "❯ "}</Text>
        <TextInput
          value={input}
          onChange={setInput}
          onSubmit={submit}
          focus={mode === "insert"}
          placeholder={
            mode === "insert"
              ? `Type a prompt → sends to ${activeCount} active model${activeCount === 1 ? "" : "s"} (Enter)`
              : "(nav mode — press i to type)"
          }
        />
      </Box>

      <StatusBar providers={providers} focusedCol={focusedCol} mode={mode} view={view} />
    </Box>
  );
}
