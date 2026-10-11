// The wiring behind the provider / model / effort chips (decisions 2026-10-06,
// part A), kept out of App so the window only mounts it.
//
// While composing, a choice lives on the compose row and nothing is sent; the
// first message carries it (`threadBody`). After that a change is a PATCH,
// and the chip shows only what the server holds: a refusal leaves it where it
// was, with the server's own words beside it.
//
// Several chats at once (WP-B): every chat pane has its own chips, for its own
// conversation (`chipFor(pane)`); the catalogue and the provider-default
// dialogs are the window's, and the catalogue's "use" puts the model on the
// pane it was opened from.

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Action, State } from "../state/store";
import type { ModelRow } from "../types";
import {
  chipState, defaultBody, patchBody, PROVIDER_LABELS, PROVIDERS, providerRefusal, visionNote,
  type Choice, type ThreadModels,
} from "../lib/threadmodel";
import type { ProviderName } from "../types";
import { PANE_NOS, type PaneNo } from "../lib/workspace";
import { CatalogPicker, ModelChip, ProviderDefaults } from "./ModelChip";
import { refusal, rosterIds } from "../lib/roster";

/** The chips' view of one pane's conversation (lib/threadmodel `chipState`). */
function paneChips(state: State, pane: PaneNo) {
  const c = state.chats[pane];
  return chipState({ threadId: c.threadId, compose: c.compose, threads: state.threads });
}

export function useThreadModel(state: State, dispatch: React.Dispatch<Action>, onRosterChange?: () => void) {
  const [models, setModels] = useState<ThreadModels | null>(null);
  // A refusal belongs to the pane and the conversation it was said about: a
  // pane that moves on to another conversation no longer shows it.
  const [errors, setErrors] = useState<Partial<Record<PaneNo, { key: string; text: string }>>>({});
  const [catalog, setCatalog] = useState<ModelRow[] | null>(null);
  const [roster, setRoster] = useState<string[]>([]);
  const [catalogOpen, setCatalogOpen] = useState(false);
  // "roster" when opened from the Model picker (pin/unpin only).
  const [catalogMode, setCatalogMode] = useState<"thread" | "roster">("thread");
  // The pane whose chip opened the catalogue: "use" puts that pane's thread on the model.
  const [catalogPane, setCatalogPane] = useState<PaneNo>(1);
  const [catalogError, setCatalogError] = useState("");
  // Claude's or Codex's default menu (2026-10-08), and its refusal.
  const [defaultsFor, setDefaultsFor] = useState<ProviderName | null>(null);
  const [defaultsError, setDefaultsError] = useState("");
  // Clicks read the state as it is when they land, never a render's closure.
  const now = useRef(state);
  now.current = state;

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

  const keyOf = (cs: ReturnType<typeof paneChips>) => `${cs.targetId ?? ""}|${cs.composing}`;
  // Clear a stale refusal when its pane's conversation changes.
  const keys = PANE_NOS.map((n) => keyOf(paneChips(state, n)));
  const keysNow = keys.join(",");
  useEffect(() => {
    setErrors((e) => {
      const stale = PANE_NOS.filter((n, i) => e[n] && e[n]!.key !== keys[i]);
      if (!stale.length) return e;
      const next = { ...e };
      for (const n of stale) delete next[n];
      return next;
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keysNow]);

  // Per thread: the sequence number of its latest model or effort change, and
  // of the latest one whose success has been drawn.
  const latest = useRef<Record<string, number>>({});
  const landed = useRef<Record<string, number>>({});
  /** `from`: the conversation an effort move was made on (the chip's key when
   * it started). The pane showing another by now drops it — never onto a
   * thread or compose row it was not made on (review of PR #30). */
  const change = useCallback(
    (pane: PaneNo, next: Choice, from?: string): Promise<void> | undefined => {
      const s = now.current;
      const cs = paneChips(s, pane);
      if (from !== undefined && keyOf(cs) !== from) return;
      const { choice, targetId, composing } = cs;
      const compose = s.chats[pane].compose;
      setErrors((e) => (e[pane] ? { ...e, [pane]: undefined } : e));
      if (composing && compose) {
        // Composing: kept here, sent with the first message, nothing before.
        dispatch({
          type: "chat", pane,
          patch: { compose: { ...compose, provider: next.provider, model: next.model, effort: next.effort } },
        });
        return;
      }
      if (!targetId || !choice) return;
      const body = patchBody(choice, next);
      if (!body) return;
      const key = keyOf(cs);
      // An older answer must not undo a newer one: the effort slider can send
      // two in quick succession, and the first answer can arrive last. So a
      // success is drawn unless a later change's success already was — but it
      // is drawn when the later one was refused or is still on its way, since
      // it is then what the server holds (an opened compose row has no
      // `thread_updated` to put it right; review of PR #30). Only the latest
      // change's refusal is said. The SSE `thread_updated` still carries every
      // change, in the order the daemon made them.
      const n = (latest.current[targetId] = (latest.current[targetId] || 0) + 1);
      const isLatest = () => latest.current[targetId] === n;
      return api
        .setThreadModel(targetId, body)
        .then((record) => {
          if ((landed.current[targetId] || 0) > n) return;
          landed.current[targetId] = n;
          dispatch({ type: "thread_patch", id: record.id, patch: record });
          // An opened-but-unsent thread's chips read the compose row: keep it
          // on what the server now holds — the row that holds that thread
          // **when the answer lands**, wherever it is by then, and nothing
          // else. Writing back the row captured before the request put a
          // fresh compose row (New thread pressed meanwhile) back on the old
          // thread (re-review of PR #27).
          const s2 = now.current;
          const holder = PANE_NOS.find((p) => s2.chats[p].compose?.openedId === targetId);
          const row = holder !== undefined ? s2.chats[holder].compose : null;
          if (holder !== undefined && row)
            dispatch({
              type: "chat", pane: holder,
              patch: { compose: { ...row, model: record.model ?? null, effort: record.effort ?? null } },
            });
        })
        .catch((e) => {
          if (!isLatest()) return;
          // Said in the pane still on that conversation, if any.
          const s2 = now.current;
          const at = PANE_NOS.find((p) => keyOf(paneChips(s2, p)) === key);
          if (at !== undefined) setErrors((all) => ({ ...all, [at]: { key, text: `Could not change model: ${e.message}` } }));
        });
    },
    [dispatch],
  );

  const openCatalog = useCallback((mode: "thread" | "roster" = "thread", pane: PaneNo = 1) => {
    setCatalogMode(mode);
    setCatalogPane(pane);
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

  /** The chips for one chat pane's conversation, or null when it has none to
   * show. `zoom` (percent) places the model popover. */
  const chipFor = (pane: PaneNo, zoom?: number) => {
    const cs = paneChips(state, pane);
    if (cs.choice === null) return null;
    // While composing, grey out a provider the chosen project's permission
    // profile cannot run, with the reason as its tooltip (POST /threads would
    // refuse it with the same reason).
    const compose = state.chats[pane].compose;
    const project = cs.composing ? state.projects.find((p) => p.id === compose?.projectId) : undefined;
    const refusals = Object.fromEntries(PROVIDERS.map((p) => [p, providerRefusal(models, p, project)]));
    const err = errors[pane];
    return (
      <ModelChip
        models={models}
        choice={cs.choice}
        providerEditable={cs.editable.provider}
        modelEditable={cs.editable.model}
        disabled={state.approvals.length > 0}
        error={err && err.key === keyOf(cs) ? err.text : ""}
        onChange={(next, from) => change(pane, next, from)}
        conversation={keyOf(cs)}
        onSearch={() => openCatalog("thread", pane)}
        refusals={refusals}
        onDefaults={(provider) => {
          setDefaultsError("");
          setDefaultsFor(provider);
        }}
        pane={pane}
        zoom={zoom}
      />
    );
  };

  /** Shown while an image is staged in a pane whose model cannot see one (A5). */
  const imageNoteFor = (pane: PaneNo) => {
    const { choice } = paneChips(state, pane);
    return choice ? visionNote(models, choice) : null;
  };

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
            const { choice } = paneChips(now.current, catalogPane);
            if (choice) change(catalogPane, { ...choice, model: id, effort: null });
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
    chipFor,
    picker,
    /** The catalogue or a provider-default dialog is open: an open picker,
     * for the mic's suppression and push-to-talk. */
    overlayOpen: catalogOpen || defaultsFor !== null,
    /** The catalogue for the Model picker's "Pin a model…". */
    openRosterCatalog: () => openCatalog("roster"),
    imageNoteFor,
  };
}
