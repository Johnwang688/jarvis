// Settings -> Voice: speech-to-text backend and the cloud-fallback switch (V13).
// The daemon owns both (`/stt/settings`); this reads them when it opens and after
// every change, and shows the daemon's answer rather than assuming it took.
import { useCallback, useEffect, useState, useSyncExternalStore } from "react";
import { api } from "../api";
import { STT_CHOICES, sttSummary, wakeNote, type SttChoice } from "../lib/sttSettings";
import { sttSettingsStore } from "../state/sttSettings";

const LABEL: Record<SttChoice, string> = { cloud: "Cloud", local: "Local", auto: "Auto" };
const HINT: Record<SttChoice, string> = {
  cloud: "OpenRouter's Parakeet, then Grok STT. Needs the network.",
  local: "Parakeet on this computer. Loads on first use and unloads when idle.",
  auto: "Local when it is installed, otherwise the cloud.",
};

export function SttSettingsPanel({ blocked = false }: { blocked?: boolean }) {
  // The window's one copy, shared with the wake-word recognizer's gate.
  const value = useSyncExternalStore(sttSettingsStore.subscribe, sttSettingsStore.snapshot);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const load = useCallback(() => api.sttSettings().then(v => { sttSettingsStore.apply(v); setError(""); })
    .catch(e => setError(e instanceof Error ? e.message : "The speech-to-text settings could not be read.")), []);
  useEffect(() => { void load(); }, [load]);
  const change = async (c: { backend?: SttChoice; cloud_fallback?: boolean }) => {
    if (blocked || busy) return;
    setBusy(true); setError("");
    try { sttSettingsStore.apply(await api.setSttSettings(c)); }
    catch (e) { setError(e instanceof Error ? e.message : "The setting could not be saved."); }
    finally { setBusy(false); }
  };
  const off = blocked || busy || value === null;
  return (
    <section className="stt-settings" data-testid="stt-settings" aria-label="Speech-to-text">
      <h3>Speech-to-text</h3>
      <div role="radiogroup" aria-label="Speech-to-text backend" className="stt-backends">
        {STT_CHOICES.map(c => (
          <label key={c}>
            <input type="radio" name="stt-backend" data-testid={`stt-backend-${c}`} value={c}
              checked={value?.backend === c} disabled={off}
              onChange={() => { void change({ backend: c }); }} />{" "}
            {LABEL[c]} <span className="muted">{HINT[c]}</span>
          </label>
        ))}
      </div>
      <label className="stt-fallback">
        <input type="checkbox" data-testid="stt-cloud-fallback" checked={value?.cloudFallback === true} disabled={off}
          onChange={e => { void change({ cloud_fallback: e.target.checked }); }} />{" "}
        Cloud fallback
      </label>
      <p className="muted">
        On: HUD dictation audio may be sent to the cloud when Cloud is chosen, or when local speech-to-text fails or is slow.
        Off: HUD dictation never uploads anything, and a local failure shows an error.
      </p>
      {value ? <p data-testid="stt-summary">{sttSummary(value)}</p> : null}
      {value ? <p data-testid="stt-wake-note">{wakeNote(value)}</p> : null}
      {value ? (
        <p className="muted" data-testid="stt-local-status">
          Local engine: {value.local.installed ? `installed · ${value.local.loaded ? "loaded" : "not loaded"} · ${value.local.threads} threads · unloads after ${value.local.idleUnloadMin} min idle` : `not installed (${value.local.reason || "unknown"})`}
          {!value.local.installed && value.local.fetch ? ` · install the package, then run: ${value.local.fetch}` : ""}
        </p>
      ) : null}
      {value?.error ? <p className="err" role="status" data-testid="stt-settings-error">{value.error}</p> : null}
      {error ? <p className="err" role="status" data-testid="stt-settings-fault">{error}</p> : null}
    </section>
  );
}
