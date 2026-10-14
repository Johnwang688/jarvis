// Settings -> Voice -> Your voice (voice rework V12, plan 3.4 and 3.4.1).
//
// Enrollment: three prompted readings of about ten seconds with a live meter and
// quality checks; the daemon scores them against each other and against every
// built-in voice and proposes a threshold; verification switches on only after
// the owner accepts. Also the Guest voice switch (verification off until the owner
// turns it off), the threshold, and Delete (everything: readings, voiceprint and
// the Guest switch).
//
// The audio of a take lives in a `ReadingRecorder` until it is sent to the
// daemon's owner-only `/speaker/reading` or thrown away; nothing here stores or
// plays it, and no route sends it back.
import { useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { api } from "../api";
import { ReadingRecorder, type ReadingMeter } from "../lib/enroll";
import { speakerSummary, type Proposal, type SpeakerStatus, type SpeakerStore } from "../lib/speaker";

type Phase = "idle" | "recording" | "sending";

function Hist({ counts, label, tone }: { counts: number[]; label: string; tone: "own" | "other" }) {
  const top = Math.max(1, ...counts);
  return (
    <div className={`spk-hist ${tone}`} role="img" aria-label={label}>
      {counts.map((c, i) => <span key={i} style={{ height: `${Math.round((c / top) * 100)}%` }} />)}
    </div>
  );
}

export function SpeakerSettings({ store, listen, blocked = false }: {
  store: SpeakerStore;
  /** Take the microphone's 16 kHz frames until the returned function is called; null when the mic is not open. */
  listen: (fn: (frame: Float32Array) => void) => (() => void) | null;
  blocked?: boolean;
}) {
  const view = useSyncExternalStore(store.subscribe, store.snapshot);
  const status: SpeakerStatus | null = view.status;
  const [phase, setPhase] = useState<Phase>("idle");
  const [meter, setMeter] = useState<ReadingMeter | null>(null);
  const [problems, setProblems] = useState<string[]>([]);
  const [note, setNote] = useState("");
  const [proposal, setProposal] = useState<Proposal | null>(null);
  const [threshold, setThreshold] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [sure, setSure] = useState(false);
  const recorder = useRef<ReadingRecorder | null>(null);
  const stopListening = useRef<(() => void) | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => { void store.load(); }, [store]);

  const release = useCallback(() => {
    stopListening.current?.();
    stopListening.current = null;
    if (timer.current) clearInterval(timer.current);
    timer.current = null;
  }, []);
  // Leaving Settings mid-take drops the take.
  useEffect(() => () => { release(); recorder.current?.clear(); recorder.current = null; }, [release]);

  const fail = (e: unknown, fallback: string) => setError(e instanceof Error && e.message ? e.message : fallback);
  const idle = phase === "idle" && !busy;
  const off = blocked || !idle;

  const stop = useCallback(async () => {
    const rec = recorder.current;
    release();
    if (!rec) { setPhase("idle"); return; }
    const found = rec.problems();
    setMeter(rec.meter());
    if (found.length) {
      // Not worth uploading: say what is wrong and let the owner read it again.
      setProblems(found); setNote(""); rec.clear(); recorder.current = null; setPhase("idle");
      return;
    }
    setPhase("sending");
    try {
      const result = await api.speakerReading(rec.wav());
      setProblems(result?.problems ?? []);
      setNote(result?.accepted ? `Reading ${result.readings} kept.` : "");
      store.adopt(await api.speaker());
    } catch (e) { fail(e, "The reading could not be sent."); }
    finally { rec.clear(); recorder.current = null; setPhase("idle"); }
  }, [release, store]);

  const record = () => {
    setError(""); setProblems([]); setNote(""); setProposal(null);
    const rec = new ReadingRecorder(status?.quality);
    const unlisten = listen(frame => rec.push(frame));
    if (!unlisten) { setError("The microphone is not open, so nothing can be recorded. Allow it in the browser and reload."); return; }
    recorder.current = rec;
    stopListening.current = unlisten;
    setMeter(rec.meter());
    setPhase("recording");
    timer.current = setInterval(() => {
      setMeter(rec.meter());
      if (rec.full) void stop();
    }, 100);
  };

  const run = async (work: () => Promise<void>, fallback: string) => {
    setBusy(true); setError("");
    try { await work(); } catch (e) { fail(e, fallback); } finally { setBusy(false); }
  };

  const analyse = () => run(async () => {
    const p = await api.speakerPropose();
    if (!p) throw new Error("The daemon's answer was not understood.");
    setProposal(p); setThreshold(p.threshold.toFixed(2));
  }, "The readings could not be analysed.");

  const wanted = Number(threshold);
  const validThreshold = status !== null && Number.isFinite(wanted) && wanted >= status.thresholdMin && wanted <= status.thresholdMax;

  const accept = () => run(async () => {
    store.adopt(await api.speakerAccept(validThreshold ? wanted : undefined));
    setProposal(null); setNote("Verification is on: only your voice is accepted.");
  }, "The voiceprint could not be built.");

  const rebuild = () => run(async () => {
    const p = await api.speakerPropose();
    store.adopt(await api.speakerAccept(p?.threshold));
    setNote("The voiceprint was rebuilt from your readings.");
  }, "The voiceprint could not be rebuilt.");

  const save = () => run(async () => {
    store.adopt(await api.speakerThreshold(wanted));
    setNote("Threshold saved.");
  }, "The threshold could not be saved.");

  const remove = () => run(async () => {
    store.adopt(await api.speakerDelete());
    setProposal(null); setSure(false); setNote("Everything was deleted: the readings, the voiceprint and the Guest voice switch.");
  }, "Delete failed.");

  const prompt = useMemo(() => {
    const list = status?.prompts ?? [];
    return list.length ? list[(status?.readings ?? 0) % list.length] : "";
  }, [status?.prompts, status?.readings]);

  const canAnalyse = status !== null && status.readings >= status.readingsMin && status.installed;
  const canRecord = status !== null && status.readings < status.readingsMax;
  const enrolled = status?.state === "enrolled" || status?.state === "stale";
  const level = meter && Number.isFinite(meter.liveDb) ? Math.max(0, Math.min(1, (meter.liveDb + 60) / 54)) : 0;

  return (
    <section className="speaker-settings" data-testid="speaker-settings" aria-label="Your voice">
      <h3>Your voice</h3>
      <p>
        Enroll your voice so only you can dictate. It is checked on this computer before anything is sent for
        transcription, and the readings, the voiceprint and this switch stay in a private folder here, never uploaded.
      </p>
      {status ? <p data-testid="speaker-summary" role="status">{speakerSummary(status)}</p> : <p className="muted">Reading the voiceprint status…</p>}
      {status ? (
        <p className="muted" data-testid="speaker-engine">
          Speaker model: {status.installed ? `installed · ${status.loaded ? "loaded" : "not loaded"}` : `not installed (${status.reason || "unknown"})`}
          {!status.installed && status.fetch ? ` · install the package, then run: ${status.fetch}` : ""}
        </p>
      ) : null}

      <label className="speaker-guest">
        <input type="checkbox" data-testid="guest-switch" checked={view.guest} disabled={blocked || busy || status === null}
          onChange={e => { void store.setGuest(e.target.checked); }} />{" "}
        {status && !status.enrolled && !view.guest ? "Guest voice (no voice enrolled)" : "Guest voice"}
      </label>
      <p className="muted">
        On: anyone in the room can dictate and give "Jarvis …" commands; your voice is not checked. It stays on until
        you turn it off, through reloads and restarts, and the orb says GUEST VOICE the whole time. Voice activity detection,
        noise handling and approvals are unchanged: speech never answers an approval card.
      </p>
      {view.error ? <p className="err" role="status" data-testid="guest-error">{view.error}</p> : null}

      {status && canRecord ? (
        <div className="speaker-record">
          <h4>Reading {Math.min(status.readings + 1, status.readingsMax)}</h4>
          <p className="speaker-prompt" data-testid="speaker-prompt">{prompt}</p>
          <div className="speaker-meter" aria-hidden="true"><span data-testid="speaker-level" style={{ width: `${Math.round(level * 100)}%` }} /></div>
          {meter && phase !== "idle" ? (
            <p className="muted" data-testid="speaker-meter-text">
              {meter.seconds.toFixed(1)} s · speech {Math.round(meter.speech * 100)}%
              {Number.isFinite(meter.levelDb) ? ` · level ${meter.levelDb.toFixed(0)} dB` : ""}
            </p>
          ) : null}
          {phase === "recording"
            ? <button type="button" data-testid="speaker-stop" onClick={() => { void stop(); }}>Stop</button>
            : <button type="button" data-testid="speaker-record" disabled={off || status.readings >= status.readingsMax} onClick={record}>
              {phase === "sending" ? "Sending…" : "Record"}</button>}
          <span className="muted"> Sit where you usually talk to the computer and read at your usual pace, about ten seconds.</span>
          {problems.length ? <ul className="err" data-testid="speaker-problems">{problems.map(p => <li key={p}>{p}</li>)}</ul> : null}
        </div>
      ) : null}

      {status && status.readings > 0 ? (
        <p data-testid="speaker-readings">{status.readings} of {status.readingsMin} readings kept.</p>
      ) : null}

      {canAnalyse && !enrolled ? (
        <button type="button" data-testid="speaker-analyse" disabled={off} onClick={() => { void analyse(); }}>Check my readings</button>
      ) : null}
      {status && status.readings >= status.readingsMin && !status.installed ? (
        <p className="err">The speaker model is not installed, so the readings cannot be checked yet.</p>
      ) : null}

      {proposal ? (
        <div className="speaker-proposal" data-testid="speaker-proposal">
          <p>
            Proposed threshold <strong data-testid="speaker-proposed">{proposal.threshold.toFixed(2)}</strong>. Your readings
            score from {proposal.genuine.min.toFixed(2)}{" "}
            (weakest) to {proposal.genuine.median.toFixed(2)} (typical)
            {proposal.impostors.max !== null ? `; the closest of ${proposal.impostors.n} built-in voices scores ${proposal.impostors.max.toFixed(2)}` : "; no built-in voice could be scored"}.
            At this threshold about {Math.round(proposal.falseReject * 100)}% of your readings would be refused
            {proposal.falseAccept !== null ? ` and ${Math.round(proposal.falseAccept * 100)}% of built-in voices accepted` : ""}.
          </p>
          <div className="spk-dist">
            <div><span className="muted">You</span><Hist counts={proposal.genuine.hist} tone="own" label="Your readings' similarity scores" /></div>
            <div><span className="muted">Built-in voices</span><Hist counts={proposal.impostors.hist} tone="other" label="Built-in voices' similarity scores" /></div>
          </div>
          {proposal.warning ? <p className="err" data-testid="speaker-warning">{proposal.warning}</p> : null}
          <label>
            Threshold{" "}
            <input type="number" data-testid="speaker-threshold" step="0.01" min={status?.thresholdMin} max={status?.thresholdMax}
              value={threshold} onChange={e => setThreshold(e.target.value)} />
          </label>{" "}
          <button type="button" data-testid="speaker-accept" disabled={off || !validThreshold} onClick={() => { void accept(); }}>
            Accept and turn on</button>
        </div>
      ) : null}

      {status && enrolled ? (
        <div className="speaker-tune">
          {status.state === "stale" ? (
            <button type="button" data-testid="speaker-rebuild" disabled={off || !status.installed} onClick={() => { void rebuild(); }}>
              Rebuild the voiceprint</button>
          ) : (
            <>
              <label>
                Threshold{" "}
                <input type="number" data-testid="speaker-threshold-tune" step="0.01" min={status.thresholdMin} max={status.thresholdMax}
                  value={threshold !== "" ? threshold : String(status.threshold ?? status.thresholdDefault)} onChange={e => setThreshold(e.target.value)} />
              </label>{" "}
              <button type="button" data-testid="speaker-save" disabled={off || !validThreshold} onClick={() => { void save(); }}>Save</button>
              <p className="muted">
                Higher refuses more voices, and more of yours. Clips under {status.shortClipS} s (commands) use a threshold
                {" "}{status.shortRelax.toFixed(2)} lower.
                {status.last ? ` Last check: ${status.last.result}${status.last.score !== null ? ` (score ${status.last.score.toFixed(2)})` : ""}.` : ""}
              </p>
            </>
          )}
        </div>
      ) : null}

      {note ? <p role="status" data-testid="speaker-note">{note}</p> : null}
      {error ? <p className="err" role="status" data-testid="speaker-error">{error}</p> : null}

      {status && (status.readings > 0 || status.state !== "not_enrolled" || view.guest) ? (
        <div className="speaker-delete">
          {sure ? (
            <>
              <span>Delete the readings, the voiceprint and the Guest voice switch?</span>{" "}
              <button type="button" data-testid="speaker-delete-confirm" disabled={off} onClick={() => { void remove(); }}>Delete everything</button>{" "}
              <button type="button" data-testid="speaker-delete-cancel" onClick={() => setSure(false)}>Cancel</button>
            </>
          ) : <button type="button" data-testid="speaker-delete" disabled={off} onClick={() => setSure(true)}>Delete…</button>}
        </div>
      ) : null}
    </section>
  );
}
