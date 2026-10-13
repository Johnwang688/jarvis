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
