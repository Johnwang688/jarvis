import { useEffect, useState } from "react";
import { span } from "../../Status";
import { useVoice } from "./useVoice";
import { controlHelper, STATES, type VoiceSetting, type VoiceSettings, type VoiceTurn } from "./voiceApi";

const MIC_TEST_MS = 10_000;

function seconds(value: number | null, divisor = 1) {
  return value == null ? "—" : `${(value / divisor).toFixed(1)} s`;
}

function elapsed(value: number | null) {
  return value == null ? "—" : `${Math.round(value)} ms`;
}

function heardAt(turn: VoiceTurn) {
  return `${span(Date.now() / 1000 - turn.heard_at)} ago`;
}

function Recent({ turns }: { turns: VoiceTurn[] }) {
  return (
    <details className="voice-recent">
      <summary>Recent</summary>
      {turns.length === 0 && <p>No turns yet.</p>}
      {turns.map((turn) => (
        <details key={turn.heard_at} className="voice-turn">
          <summary><span>{heardAt(turn)} · <bdi>{turn.text}</bdi></span></summary>
          <dl className="voice-timings">
            <dt>Silence</dt><dd>{elapsed(turn.silence_ms)}</dd>
            <dt>Decode</dt><dd>{elapsed(turn.decode_ms)}</dd>
            <dt>First sound</dt><dd>{seconds(turn.first_audio_ms, 1000)}</dd>
            <dt>First sentence</dt><dd>{seconds(turn.first_say_s)}</dd>
            <dt>Turn end</dt><dd>{seconds(turn.end_s)}</dd>
            <dt>Sentences</dt><dd>{turn.sentence_count}</dd>
          </dl>
          {turn.tools.length > 0 && (
            <ul className="voice-tools">
              {turn.tools.map((tool, position) => (
                <li key={position}><span aria-label={tool.ok ? "Succeeded" : "Failed"} role="img">
                  {tool.ok ? "✓" : "✗"}</span> {tool.name}</li>
              ))}
            </ul>
          )}
          {turn.approval_asked && <p>Asked for approval</p>}
        </details>
      ))}
    </details>
  );
}

function Settings({ settings, saving, save }: {
  settings: VoiceSettings;
  saving: boolean;
  save: (name: VoiceSetting, value: number) => Promise<void>;
}) {
  const [speed, setSpeed] = useState<number>();
  const [followUp, setFollowUp] = useState<string>();
  const [error, setError] = useState<{ name: VoiceSetting; message: string }>();

  async function commit(name: VoiceSetting, value: number) {
    if (saving) return;
    setError(undefined);
    try {
      if (!Number.isFinite(value)) throw new Error("Enter the follow-up time in seconds.");
      await save(name, value);
    } catch (error) {
      setError({ name, message: error instanceof Error ? error.message : "Voice setting was refused." });
    } finally {
      if (name === "speed") setSpeed(undefined);
      else setFollowUp(undefined);
    }
  }

  function releaseSpeed(input: HTMLInputElement) {
    if (speed !== undefined) void commit("speed", input.valueAsNumber);
  }

  function releaseFollowUp(input: HTMLInputElement) {
    if (followUp !== undefined) void commit("follow_up_s", input.valueAsNumber);
  }

  return (
    <fieldset className="voice-settings">
      <legend>Settings</legend>
      <label htmlFor="voice-speed">Speed</label>
      <output className="voice-speed-value" htmlFor="voice-speed">{settings.speed.value.toFixed(2)}×</output>
      <input id="voice-speed" type="range" min={settings.speed.min} max={settings.speed.max} step={0.05}
        value={speed ?? settings.speed.value} aria-valuetext={`${(speed ?? settings.speed.value).toFixed(2)}×`}
        aria-disabled={saving} aria-describedby={error?.name === "speed" ? "voice-setting-error" : undefined}
        onChange={(event) => { if (!saving) setSpeed(event.currentTarget.valueAsNumber); }}
        onPointerDown={(event) => event.currentTarget.setPointerCapture(event.pointerId)}
        onPointerUp={(event) => releaseSpeed(event.currentTarget)}
        onPointerCancel={() => setSpeed(undefined)}
        onKeyUp={(event) => releaseSpeed(event.currentTarget)}
        onBlur={(event) => releaseSpeed(event.currentTarget)} />
      {error?.name === "speed" && <p id="voice-setting-error" className="voice-error" role="status">{error.message}</p>}
      <label htmlFor="voice-follow">Follow-up (seconds)</label>
      <input id="voice-follow" type="number" min={settings.follow_up_s.min} max={settings.follow_up_s.max}
        step="any" value={followUp ?? settings.follow_up_s.value} readOnly={saving}
        aria-describedby={error?.name === "follow_up_s" ? "voice-setting-error" : undefined}
        onChange={(event) => setFollowUp(event.currentTarget.value)}
        onBlur={(event) => releaseFollowUp(event.currentTarget)}
        onKeyDown={(event) => {
          if (event.key === "Enter") releaseFollowUp(event.currentTarget);
        }} />
      {error?.name === "follow_up_s" && <p id="voice-setting-error" className="voice-error" role="status">{error.message}</p>}
    </fieldset>
  );
}

export function VoicePanel() {
  const [testing, setTesting] = useState(false);
  const { snapshot, unreachable, saving, save, refresh } = useVoice(testing);
  const [helperBusy, setHelperBusy] = useState(false);
  const [helperResult, setHelperResult] = useState<{ message: string; failed: boolean }>();
  const [micHint, setMicHint] = useState(false);
  const connected = snapshot?.helper.connected === true;

  useEffect(() => {
    if (!testing) return;
    const timer = setTimeout(() => setTesting(false), MIC_TEST_MS);
    return () => clearTimeout(timer);
  }, [testing]);

  async function changeHelper() {
    if (helperBusy) return;
    setHelperBusy(true);
    try {
      const message = await controlHelper(connected ? "stop" : "start");
      setHelperResult({ message, failed: false });
    } catch (error) {
      const message = error instanceof Error ? error.message : "Voice helper request failed.";
      setHelperResult({ message, failed: true });
    } finally {
      refresh();
      setHelperBusy(false);
    }
  }

  function testMic() {
    if (testing) return;
    setMicHint(!connected);
    if (connected) setTesting(true);
  }

  const latest = snapshot?.turns[0];
  const state = unreachable ? "unavailable" : snapshot?.state;
  return (
    <section className="agent-panel voice-panel" data-panel="voice" aria-labelledby="panel-voice">
      <div className="voice-top">
        <h2 id="panel-voice">Voice</h2>
        {state && <span className="voice-state" data-state={state}>{STATES[state]}</span>}
      </div>
      {unreachable && <p className="voice-error" role="status">Voice status is unavailable.</p>}
      {!snapshot && !unreachable && <p>Checking…</p>}
      {snapshot && (
        <>
          <div className="voice-helper">
            <span>{connected ? "Helper connected" : "Helper not running"}</span>
            <button type="button" aria-disabled={helperBusy} onClick={() => void changeHelper()}>
              {connected ? "Stop" : "Start"}
            </button>
            {connected && <small>{snapshot.helper.canceller ? "echo cancelling on" : "no echo cancelling"}</small>}
          </div>
          {helperResult && (
            <p role="status" className={helperResult.failed ? "voice-error" : undefined}>{helperResult.message}</p>
          )}
          <div className="voice-last">
            <div className="voice-last-line">
              <h3>Last heard</h3>
              {latest && <p className="voice-transcript"><bdi>{latest.text}</bdi></p>}
            </div>
            <div className="voice-last-line">
              {latest && <small>{heardAt(latest)} ·</small>}
              <p>heard → first sound {seconds(latest?.first_audio_ms ?? null, 1000)}</p>
            </div>
          </div>
          <details className="voice-details">
            <summary>Details and settings</summary>
            <div className="voice-details-body">
              <p className="voice-engines">
                Transcriber · {snapshot.transcriber.name ?? snapshot.transcriber.reason}
                {snapshot.transcriber.name && snapshot.transcriber.reason && ` · ${snapshot.transcriber.reason}`}
                <br />Voice · {snapshot.voice.name ?? snapshot.voice.reason}
              </p>
              <Recent turns={snapshot.turns} />
              <Settings settings={snapshot.settings} saving={saving} save={save} />
              <div className="voice-mic">
                <button type="button" aria-disabled={testing} onClick={testMic}>Mic test</button>
                {testing && connected && (
                  <meter aria-label="Microphone level" min={0} max={255} value={snapshot.helper.level ?? 0} />
                )}
                {!connected && (micHint || testing) && <p role="status">Start the helper first</p>}
              </div>
            </div>
          </details>
        </>
      )}
    </section>
  );
}
