// Settings -> Voice: read replies aloud. Its own switch, kept in this window
// (localStorage), and independent of the microphone's AUTO/REVIEW/OFF: turning
// the mic off no longer silences Jarvis (decision Q11).

export function ReadAloudSetting(props: { on: boolean; onChange: (on: boolean) => void }) {
  return (
    <section className="read-aloud" data-testid="read-aloud" aria-label="Read aloud">
      <h3>Read aloud</h3>
      <label>
        <input
          type="checkbox"
          data-testid="read-aloud-toggle"
          checked={props.on}
          onChange={(e) => props.onChange(e.target.checked)}
        />{" "}
        Read agent replies aloud, as they are written
      </label>
      <p>
        Code, diffs and tool calls are skipped. Uses the local voice only; nothing is sent to a cloud voice.
        The microphone&apos;s OFF does not affect this. Only the most recently focused window speaks.
      </p>
    </section>
  );
}

// Settings -> Voice: lower the Remote Browser's sound while Jarvis speaks (V15).
// A convenience kept in this window (localStorage), on by default. The daemon does
// the lowering, under a short lease this window renews only while speech is active.
export function DuckBrowserSetting(props: { on: boolean; onChange: (on: boolean) => void }) {
  return (
    <section className="read-aloud" data-testid="duck-browser" aria-label="Browser sound while speaking">
      <h3>Browser sound</h3>
      <label>
        <input
          type="checkbox"
          data-testid="duck-browser-toggle"
          checked={props.on}
          onChange={(e) => props.onChange(e.target.checked)}
        />{" "}
        Lower Browser sound while Jarvis speaks
      </label>
      <p>
        Remote Browser tabs only, by about 15 dB, and only while a reply is being read. It is put back
        when he stops, or within seconds if this window closes. Nothing is unmuted, other apps are left
        alone, and Direct pages are not affected. It needs the Browser&apos;s sound turned on in Browser settings.
      </p>
    </section>
  );
}

// Settings -> Voice: say when a sub-agent finishes or fails (SV-5). A convenience kept in this
// window (localStorage), on by default. Only a count and a status are ever said.
export function SubagentNoticesSetting(props: { on: boolean; onChange: (on: boolean) => void }) {
  return (
    <section className="read-aloud" data-testid="subagent-notices" aria-label="Sub-agent notices">
      <h3>Sub-agent notices</h3>
      <label>
        <input
          type="checkbox"
          data-testid="subagent-notices-toggle"
          checked={props.on}
          onChange={(e) => props.onChange(e.target.checked)}
        />{" "}
        Say when a sub-agent finishes or fails
      </label>
      <p>
        One short sentence for the chats that are read aloud, such as &ldquo;Two sub-agents finished.&rdquo;
        Never a sub-agent&apos;s name or words, and nothing when you stopped it yourself. Read-aloud must be on.
      </p>
    </section>
  );
}
