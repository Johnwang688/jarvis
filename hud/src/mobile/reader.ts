// One read in flight per resource (PX-2, plan C §4.1). The phone's lists are
// read again by many triggers at once (an event, a reopened stream, coming
// back online, a tab shown): without a guard two reads of one route overlap,
// and whichever answers last wins, which is not always the newer one. So:
//
//   - at most ONE read of a resource is in flight;
//   - a trigger meanwhile is remembered, not started: it becomes exactly one
//     more read when the first ends, however many triggers came;
//   - only the latest answer is applied: an answer that arrives while a
//     re-read is already owed is stale and dropped (the re-read replaces it),
//     and `invalidate()` drops whatever is in flight (a stopped app, a new
//     session).
//
// `fetch` must report failure as a value (the app's loaders do); one that
// throws is treated as no answer.

export interface SerialReader {
  /** Ask for a read. The promise settles when no read is in flight or owed. */
  trigger(): Promise<void>;
  /** Drop the answer of any read in flight, and any owed re-read. */
  invalidate(): void;
  readonly busy: boolean;
}

export function serialRead<T>(fetch: () => Promise<T>, apply: (value: T) => void): SerialReader {
  let running: Promise<void> | null = null;
  let owed = false;
  let epoch = 0;

  async function loop(): Promise<void> {
    try {
      do {
        owed = false;
        const mine = epoch;
        let answer: { value: T } | null = null;
        try { answer = { value: await fetch() }; } catch { answer = null; }
        if (answer && mine === epoch && !owed) apply(answer.value);
      } while (owed);
    } finally {
      running = null;
    }
  }

  return {
    trigger() {
      if (running) { owed = true; return running; }
      running = loop();
      return running;
    },
    invalidate() { epoch += 1; owed = false; },
    get busy() { return running !== null; },
  };
}
