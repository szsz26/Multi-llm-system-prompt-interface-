import type { ProviderAdapter, ProviderResponse } from "./types.js";
import type { ConversationStore } from "./conversation.js";

/**
 * Fan one prompt out to the given (already filtered to active+available)
 * providers in parallel. Each provider gets ONLY its own isolated history.
 * `onUpdate` is called as each provider starts and finishes so the UI can
 * stream status per pane.
 */
export async function blast(
  prompt: string,
  providers: ProviderAdapter[],
  store: ConversationStore,
  onUpdate: (id: string, update: Partial<ProviderResponse> & { status: "running" | "done" | "error" }) => void,
): Promise<ProviderResponse[]> {
  const runs = providers.map(async (provider): Promise<ProviderResponse> => {
    const start = Date.now();
    onUpdate(provider.id, { status: "running" });
    store.addUser(provider.id, prompt);
    try {
      const text = await provider.send(store.history(provider.id));
      store.addAssistant(provider.id, text);
      const res: ProviderResponse = { providerId: provider.id, text, ok: true, ms: Date.now() - start };
      onUpdate(provider.id, { status: "done", text, ok: true, ms: res.ms });
      return res;
    } catch (err) {
      const error = (err as Error).message ?? String(err);
      const res: ProviderResponse = { providerId: provider.id, text: "", ok: false, error, ms: Date.now() - start };
      onUpdate(provider.id, { status: "error", error, ok: false, ms: res.ms });
      return res;
    }
  });

  return Promise.all(runs);
}
