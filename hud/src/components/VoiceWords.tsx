// Settings -> Voice: the words that send or cancel a dictated message (V10, decision V-1).
import { useState } from "react";
import { DEFAULT_WORDS, MAX_WORDS, SEND_WORD_SILENCE_MS, normalizeWords, parseWord, refusal, type Words } from "../lib/voiceWords";

type Which = "send" | "cancel";

function List(props: { which: Which; words: Words; onChange: (w: Words) => void; fail: (m: string) => void }) {
  const { which, words } = props;
  const [draft, setDraft] = useState("");
  const own = words[which];
  const other = words[which === "send" ? "cancel" : "send"];
  const add = () => {
    const why = refusal(draft, own, other);
    if (why) { props.fail(why); return; }
    props.fail("");
    props.onChange({ ...words, [which]: [...own, parseWord(draft)!] });
    setDraft("");
  };
  return (
    <div data-testid={`voice-words-${which}`}>
      <strong>{which === "send" ? "Send words" : "Cancel words"}</strong>
      <div className="voice-words-list" data-testid={`voice-words-${which}-list`}>
        {own.length ? own.map((w) => (
          <span className="voice-words-chip" key={w} data-testid="voice-words-chip">
            {w}
            <button type="button" aria-label={`Remove ${w}`} data-testid="voice-words-remove" onClick={() => { props.fail(""); props.onChange({ ...words, [which]: own.filter((x) => x !== w) }); }}>×</button>
          </span>
        )) : <em>none (this is off)</em>}
      </div>
      <div className="voice-words-add">
        <input type="text" value={draft} maxLength={40} aria-label={`Add a ${which} word`} data-testid={`voice-words-${which}-input`}
          disabled={!words.enabled || own.length >= MAX_WORDS}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => { e.stopPropagation(); if (e.key === "Enter") { e.preventDefault(); add(); } }}
          onKeyUp={(e) => e.stopPropagation()} />
        <button type="button" data-testid={`voice-words-${which}-add`} disabled={!words.enabled || own.length >= MAX_WORDS} onClick={add}>Add</button>
      </div>
    </div>
  );
}

export function VoiceWordsSetting(props: { words: Words; onChange: (words: Words) => void }) {
  const [error, setError] = useState("");
  const { words } = props;
  return (
    <section className="voice-words" data-testid="voice-words" aria-label="Send and cancel words">
      <h3>Send and cancel words</h3>
      <label>
        <input type="checkbox" checked={words.enabled} data-testid="voice-words-enabled"
          onChange={(e) => { setError(""); props.onChange({ ...words, enabled: e.target.checked }); }} />{" "}
        End a message with a word
      </label>
      <List which="send" words={words} onChange={props.onChange} fail={setError} />
      <List which="cancel" words={words} onChange={props.onChange} fail={setError} />
      <div className="voice-words-error" data-testid="voice-words-error" role="alert">{error}</div>
      <button type="button" data-testid="voice-words-reset" onClick={() => { setError(""); props.onChange(normalizeWords(null)); }}
        disabled={JSON.stringify(words) === JSON.stringify(DEFAULT_WORDS)}>Reset to the defaults</button>
      <p>
        Say a send word last to send at once, even when the box is in REVIEW (it sends what is in the box). Say a cancel
        word last to throw away what you dictated since the last send; Undo keeps it for ten seconds. A word counts only
        when it is the last thing you say and {SEND_WORD_SILENCE_MS} ms of quiet follows it, so &ldquo;over&rdquo; inside
        a sentence is just a word. Single words, up to {MAX_WORDS} in each list. Never answers an authorization card.
      </p>
    </section>
  );
}
