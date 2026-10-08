// The wiring behind the provider / model / effort chips (decisions 2026-10-06,
// part A), kept out of App so the window only mounts it.
//
// While composing, a choice lives on the compose row and nothing is sent; the
// first message carries it (`threadBody`). After that a change is a PATCH,
// and the chip shows only what the server holds: a refusal leaves it where it
// was, with the server's own words beside it.

import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import type { Action, State } from "../state/store";
import type { ModelRow, Thread } from "../types";
import {
  chipEditable, choiceOf, composeChoice, patchBody, PROVIDERS, providerRefusal, visionNote,
  type Choice, type ThreadModels,
} from "../lib/threadmodel";
import { CatalogPicker, ModelChip } from "./ModelChip";

export function useThreadModel(state: State, dispatch: React.Dispatch<Action>, onRosterChange?: () => void) {
  const [models, setModels] = useState<ThreadModels | null>(null);
  const [error, setError] = useState("");
  const [catalog, setCatalog] = useState<ModelRow[] | null>(null);
  const [roster, setRoster] = useState<string[]>([]);
  const [catalogOpen, setCatalogOpen] = useState(false);
  const [catalogError, setCatalogError] = useState("");

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

  const thread: Thread | null = state.threadId ? state.threads.find((t) => t.id === state.threadId) || null : null;
  const composing = !!state.compose && !state.compose.openedId;
  // Clear a stale refusal when the conversation changes.
  useEffect(() => setError(""), [state.threadId, composing]);

  const choice: Choice | null = composing ? composeChoice(state.compose) : thread ? choiceOf(thread) : null;
  const editable = chipEditable(thread, composing);
  // While composing, grey out a provider the chosen project's permission
  // profile cannot run, with the reason as its tooltip (POST /threads would
  // refuse it with the same reason).
  const project = composing ? state.projects.find((p) => p.id === state.compose?.projectId) : undefined;
  const refusals = Object.fromEntries(PROVIDERS.map((p) => [p, providerRefusal(models, p, project)]));

  const change = useCallback(
    (next: Choice) => {
      setError("");
      if (composing && state.compose) {
        // Composing: kept here, sent with the first message, nothing before.
        dispatch({
          type: "patch",
          patch: { compose: { ...state.compose, provider: next.provider, model: next.model, effort: next.effort } },
        });
        return;
      }
      if (!thread) return;
      const body = patchBody(choiceOf(thread), next);
      if (!body) return;
      api
        .setThreadModel(thread.id, body)
        .then((record) => dispatch({ type: "thread_patch", id: record.id, patch: record }))
        .catch((e) => setError(`Could not change model: ${e.message}`));
    },
    [composing, state.compose, thread, dispatch],
  );

  const openCatalog = useCallback(() => {
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
        onSearch={openCatalog}
        refusals={refusals}
      />
    ) : null;

  const picker = catalogOpen ? (
    <CatalogPicker
      catalog={catalog}
      roster={roster}
      error={catalogError}
      onPin={(id) => pin(id).catch((e) => setCatalogError(e.message))}
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

  return {
    models,
    reload,
    chip,
    picker,
    imageNote: choice ? visionNote(models, choice) : null,
  };
}
