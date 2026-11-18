// Settings -> Voice: read replies aloud. Its own switch, kept in this window
// (localStorage), and independent of the microphone's AUTO/REVIEW/OFF: turning
// the mic off no longer silences Jarvis (decision Q11).

export function ReadAloudSetting(props: { on: boolean; onChange: (on: boolean) => void }) {
  return (
    <div className="settings-row" data-testid="read-aloud">
      <label className="settings-check">
        <input
          type="checkbox"
          data-testid="read-aloud-toggle"
          checked={props.on}
          onChange={(e) => props.onChange(e.target.checked)}
        />
        <span>Read replies aloud</span>
      </label>
      <p>
        The most recently focused window reads reply text using a local voice, even with the microphone off.
        It skips code, diffs and tool calls.
      </p>
    </div>
  );
}

// Settings -> Voice: lower the Remote Browser's sound while Jarvis speaks (V15).
// A convenience kept in this window (localStorage), on by default. The daemon does
// the lowering, under a short lease this window renews only while speech is active.
export function DuckBrowserSetting(props: { on: boolean; onChange: (on: boolean) => void }) {
  return (
    <div className="settings-row" data-testid="duck-browser">
      <label className="settings-check">
        <input
          type="checkbox"
          data-testid="duck-browser-toggle"
          checked={props.on}
          onChange={(e) => props.onChange(e.target.checked)}
        />
        <span>Lower Browser sound while Jarvis speaks</span>
      </label>
      <p>
        Lowers Remote tab audio by about 15 dB during read-aloud and restores it afterwards.
        Requires sound to be enabled in Browser settings.
      </p>
    </div>
  );
}

// Settings -> Voice: say when a sub-agent finishes or fails (SV-5). A convenience kept in this
// window (localStorage), on by default. Only a count and a status are ever said.
export function SubagentNoticesSetting(props: { on: boolean; onChange: (on: boolean) => void }) {
  return (
    <div className="settings-row" data-testid="subagent-notices">
      <label className="settings-check">
        <input
          type="checkbox"
          data-testid="subagent-notices-toggle"
          checked={props.on}
          onChange={(e) => props.onChange(e.target.checked)}
        />
        <span>Announce when sub-agents finish or fail</span>
      </label>
      <p>
        Read-aloud announces the count and outcome. It skips sub-agents you stop yourself.
      </p>
    </div>
  );
}
