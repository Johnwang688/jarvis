// The wiring behind the provider / model / effort chips (decisions 2026-10-06,
// part A), kept out of App so the window only mounts it.
//
// While composing, a choice lives on the compose row and nothing is sent; the
// first message carries it (`threadBody`). After that a change is a PATCH,
// and the chip shows only what the server holds: a refusal leaves it where it
// was, with the server's own words beside it.

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Action, State } from "../state/store";
import type { ModelRow } from "../types";
import {
  chipState, defaultBody, patchBody, PROVIDER_LABELS, PROVIDERS, providerRefusal, visionNote,
  type Choice, type ThreadModels,
} from "../lib/threadmodel";
import type { ProviderName } from "../types";
import { CatalogPicker, ModelChip, ProviderDefaults } from "./ModelChip";
import { refusal, rosterIds } from "../lib/roster";

export function useThreadModel(state: State, dispatch: React.Dispatch<Action>, onRosterChange?: () => void) {
  const [models, setModels] = useState<ThreadModels | null>(null);
  const [error, setError] = useState("");
  const [catalog, setCatalog] = useState<ModelRow[] | null>(null);
  const [roster, setRoster] = useState<string[]>([]);
  const [catalogOpen, setCatalogOpen] = useState(false);
  // "roster" when opened from the Model picker (pin/unpin only).
  const [catalogMode, setCatalogMode] = useState<"thread" | "roster">("thread");
  const [catalogError, setCatalogError] = useState("");
  // Claude's or Codex's default menu (2026-10-08), and its refusal.
  const [defaultsFor, setDefaultsFor] = useState<ProviderName | null>(null);
  const [defaultsError, setDefaultsError] = useState("");

  const reload = useCallback(async () => {
    try {
      setModels(await api.threadModels());
    } catch {
      /* the chip shows ids without decoration */
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  // The open thread, or the one a compose row opened whose first send failed
  // (lib/threadmodel `chipState`): the chips stay on screen for the retry
  // instead of vanishing between "composing" and "a thread".
  const { choice, targetId, composing, editable } = chipState(state);
  // Clear a stale refusal when the conversation changes.
  useEffect(() => setError(""), [targetId, composing]);

  // While composing, grey out a provider the chosen project's permission
  // profile cannot run, with the reason as its tooltip (POST /threads would
  // refuse it with the same reason).
  const project = composing ? state.projects.find((p) => p.id === state.compose?.projectId) : undefined;
  const refusals = Object.fromEntries(PROVIDERS.map((p) => [p, providerRefusal(models, p, project)]));

  // Per thread: the sequence number of its latest model or effort change.
  const latest = useRef<Record<string, number>>({});
  const change = useCallback(
    (next: Choice): Promise<void> | undefined => {
      setError("");
      if (composing && state.compose) {
        // Composing: kept here, sent with the first message, nothing before.
        dispatch({
          type: "patch",
          patch: { compose: { ...state.compose, provider: next.provider, model: next.model, effort: next.effort } },
        });
        return;
      }
      if (!targetId || !choice) return;
      const body = patchBody(choice, next);
      if (!body) return;
      const opened = state.compose?.openedId === targetId ? state.compose : null;
      // Only this thread's latest change may write what it answers: the
      // effort slider can send two in quick succession, and an older answer
      // arriving late must not move it back. The SSE `thread_updated` still
      // carries every change, in the order the daemon made them.
      const n = (latest.current[targetId] = (latest.current[targetId] || 0) + 1);
      const isLatest = () => latest.current[targetId] === n;
      return api
        .setThreadModel(targetId, body)
        .then((record) => {
          if (!isLatest()) return;
          dispatch({ type: "thread_patch", id: record.id, patch: record });
          // An opened-but-unsent thread's chips read the compose row: keep it
          // on what the server now holds.
          if (opened)
            dispatch({ type: "patch", patch: { compose: { ...opened, model: record.model ?? null, effort: record.effort ?? null } } });
        })
        .catch((e) => {
          if (isLatest()) setError(`Could not change model: ${e.message}`);
        });
    },
    [composing, state.compose, choice, targetId, dispatch],
  );

  const openCatalog = useCallback((mode: "thread" | "roster" = "thread") => {
    setCatalogMode(mode);
    setCatalogOpen(true);
    setCatalogError("");
    api
      .catalog()
      .then((r: any) => {
        setCatalog(r.models || []);
        setRoster(Array.isArray(r.roster) ? r.roster : []);
      })
      .catch((e) => {
        setCatalog([]);
        setCatalogError(e.message);
      });
  }, []);

  const pin = useCallback(
    async (id: string) => {
      await api.addModel(id);
      setRoster((r) => (r.includes(id) ? r : [...r, id]));
      await reload();
      onRosterChange?.();
    },
    [reload, onRosterChange],
  );

  const unpin = useCallback(
    async (id: string) => {
      const r = await api.removeModel(id);
      setRoster(rosterIds(r));
      await reload();
      onRosterChange?.();
    },
    [reload, onRosterChange],
  );

  // Set (or with "" reset) a provider's default. The answer is the new
  // `GET /thread-models`, so every default thread's label moves at once; the
  // `model` event that follows tells the other windows.
  const setDefault = useCallback((provider: ProviderName, model: string, effort: string) => {
    setDefaultsError("");
    const label = PROVIDER_LABELS[provider] || provider;
    const what = model ? `set ${model} as the ${label} default` : `reset the ${label} default`;
    api
      .setProviderDefault(defaultBody(provider, model, effort))
      .then((payload) => setModels(payload))
      .catch((e) => setDefaultsError(refusal(what, e)));
  }, []);

  const chip =
    choice !== null ? (
      <ModelChip
        models={models}
        choice={choice}
        providerEditable={editable.provider}
        modelEditable={editable.model}
        disabled={state.approvals.length > 0}
        error={error}
        onChange={change}
        onSearch={() => openCatalog("thread")}
        refusals={refusals}
        onDefaults={(provider) => {
          setDefaultsError("");
          setDefaultsFor(provider);
        }}
      />
    ) : null;

  const defaults = defaultsFor ? (
    <ProviderDefaults
      models={models}
      provider={defaultsFor}
      error={defaultsError}
      onSet={(model, effort) => setDefault(defaultsFor, model, effort)}
      onReset={() => setDefault(defaultsFor, "", "")}
      onClose={() => setDefaultsFor(null)}
    />
  ) : null;

  const catalogPicker = catalogOpen ? (
    <CatalogPicker
      catalog={catalog}
      roster={roster}
      error={catalogError}
      pinOnly={catalogMode === "roster"}
      onPin={(id) => pin(id).catch((e) => setCatalogError(refusal(`pin ${id}`, e)))}
      onUnpin={(id) => {
        setCatalogError("");
        unpin(id).catch((e) => setCatalogError(refusal(`unpin ${id}`, e)));
      }}
      onUse={(id) =>
        pin(id)
          .then(() => {
            setCatalogOpen(false);
            if (choice) change({ ...choice, model: id, effort: null });
          })
          .catch((e) => setCatalogError(e.message))
      }
      onClose={() => setCatalogOpen(false)}
    />
  ) : null;

  const picker = catalogPicker || defaults ? <>{catalogPicker}{defaults}</> : null;

  return {
    models,
    reload,
    chip,
    picker,
    /** The catalogue or a provider-default dialog is open: an open picker,
     * for the mic's suppression and push-to-talk. */
    overlayOpen: catalogOpen || defaultsFor !== null,
    /** The catalogue for the Model picker's "Pin a model…". */
    openRosterCatalog: () => openCatalog("roster"),
    imageNote: choice ? visionNote(models, choice) : null,
  };
}
