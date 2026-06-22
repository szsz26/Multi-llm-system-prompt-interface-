import React, { useEffect, useMemo, useState } from "react";
import { Box, Text, useApp, useInput } from "ink";
import TextInput from "ink-text-input";
import type { AppConfig, ProviderAdapter, ProviderResponse, ProviderState } from "../types.js";
import type { ConversationStore } from "../conversation.js";
import { blast } from "../orchestrator.js";
import { synthesize, type NamedResponse } from "../compare/synthesize.js";
import { StatusBar } from "./StatusBar.js";
import { ProviderPane } from "./ProviderPane.js";
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
  const [activePane, setActivePane] = useState(0);
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
    setBusy(true);
    await blast(prompt, targets, store, onUpdate, onToken);
    setBusy(false);
    bump();
  };

  const runSynthesis = async () => {
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

  useInput((key, special) => {
    if (special.escape) {
      setMode("nav");
      return;
    }
    if (mode === "insert") return; // TextInput owns typing + Enter.

    if (key === "i") return setMode("insert");
    if (key === "q" || (special.ctrl && key === "c")) return exit();
    if (special.leftArrow) return setActivePane((i) => Math.max(0, i - 1));
    if (special.rightArrow) return setActivePane((i) => Math.min(providers.length, i + 1));
    if (/[1-9]/.test(key)) {
      const idx = parseInt(key, 10) - 1;
      if (idx <= providers.length) setActivePane(idx);
      return;
    }
    if (key === "t" || key === " ") {
      // Toggle the focused provider on/off (only if available).
      setProviders((prev) =>
        prev.map((p, i) => (i === activePane && p.available ? { ...p, active: !p.active } : p)),
      );
      return;
    }
    if (key === "c") return void runSynthesis();
    if (key === "r") {
      // Reset focused provider's conversation (or all on the combined pane).
      if (activePane < providers.length) {
        const id = providers[activePane]!.id;
        store.reset(id);
        setResponses((r) => {
          const { [id]: _, ...rest } = r;
          return rest;
        });
      } else {
        store.reset();
        setResponses({});
        setSynthesis(undefined);
      }
      bump();
      return;
    }
  });

  const diffPairIds = useMemo<[string, string] | null>(() => {
    const withText = providers.filter((p) => responses[p.id]?.trim()).map((p) => p.id);
    return withText.length >= 2 ? [withText[0]!, withText[1]!] : null;
  }, [providers, responses]);

  const onCombined = activePane >= providers.length;
  const focused = providers[activePane];

  return (
    <Box flexDirection="column" minHeight={20}>
      <Box flexGrow={1} flexDirection="column">
        {onCombined ? (
          <CombinedPane
            responses={responses}
            labels={labels}
            synthesis={synthesis}
            synthesizing={synthesizing}
            diffPairIds={diffPairIds}
          />
        ) : focused ? (
          <ProviderPane
            provider={focused}
            history={store.history(focused.id)}
            error={errors[focused.id]}
            streaming={partials[focused.id]}
          />
        ) : (
          <Text color="gray">No providers configured.</Text>
        )}
      </Box>

      <Box paddingX={1}>
        <Text color={busy ? "yellow" : "magenta"}>{busy ? "⏳ " : "❯ "}</Text>
        <TextInput
          value={input}
          onChange={setInput}
          onSubmit={submit}
          focus={mode === "insert"}
          placeholder={mode === "insert" ? "Type a prompt and press Enter…" : "(nav mode — press i to type)"}
        />
      </Box>

      <StatusBar providers={providers} activePane={activePane} mode={mode} />
    </Box>
  );
}
