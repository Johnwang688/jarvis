// The keyword trie (Aho-Corasick) of sherpa-onnx's keyword spotter, ported from
// `context-graph.cc`: each keyword is a sequence of BPE token ids, a path through
// the trie earns `boost` per token while the decoder follows it, and a failed path
// falls back along `fail` links to the longest suffix that is still a prefix of
// some keyword. Only `ForwardOneStep` and `IsMatched` are used at decode time.

export class ContextState {
  next = new Map<number, ContextState>();
  fail: ContextState | null = null;
  output: ContextState | null = null;
  constructor(
    public token: number,
    public tokenScore: number,
    public nodeScore: number,
    public outputScore: number,
    public level = 0,
    public acThreshold = 0,
    public isEnd = false,
    public phrase = "",
  ) {}
}

export interface KeywordSpec {
  /** BPE token ids of the keyword. */
  ids: number[];
  /** What a hit is called. */
  phrase: string;
  /** Boost per token. */
  boost: number;
  /** Mean token probability the keyword must reach to fire. */
  threshold: number;
}

const f32 = Math.fround;

export class ContextGraph {
  readonly root = new ContextState(-1, 0, 0, 0);

  constructor(keywords: KeywordSpec[]) {
    this.root.fail = this.root;
    for (const k of keywords) {
      let node = this.root;
      for (let j = 0; j < k.ids.length; j++) {
        const token = k.ids[j];
        const isEndTok = j === k.ids.length - 1;
        const existing = node.next.get(token);
        if (!existing) {
          node.next.set(token, new ContextState(
            token, f32(k.boost), f32(node.nodeScore + k.boost),
            isEndTok ? f32(node.nodeScore + k.boost) : 0, j + 1,
            isEndTok ? k.threshold : 0, isEndTok, isEndTok ? k.phrase : "",
          ));
        } else {
          existing.tokenScore = Math.max(f32(k.boost), existing.tokenScore);
          existing.nodeScore = f32(node.nodeScore + existing.tokenScore);
          const end = isEndTok || existing.isEnd;
          existing.outputScore = end ? existing.nodeScore : 0;
          existing.isEnd = end;
          if (isEndTok) { existing.phrase = k.phrase; existing.acThreshold = k.threshold; }
        }
        node = node.next.get(token)!;
      }
    }
    this.fillFailOutput();
  }

  /** (score earned, next state) for following `token` from `state`. */
  forwardOneStep(state: ContextState, token: number): [number, ContextState] {
    let node: ContextState;
    let score: number;
    const direct = state.next.get(token);
    if (direct) {
      node = direct;
      score = node.tokenScore;
    } else {
      node = state.fail!;
      while (!node.next.has(token)) {
        node = node.fail!;
        if (node.token === -1) break; // the root
      }
      const hop = node.next.get(token);
      if (hop) node = hop;
      score = f32(node.nodeScore - state.nodeScore);
    }
    return [f32(score + node.outputScore), node];
  }

  /** The keyword that `state` completes, if it completes one. */
  isMatched(state: ContextState): ContextState | null {
    if (state.isEnd) return state;
    return state.output;
  }

  private fillFailOutput() {
    const queue: ContextState[] = [];
    for (const child of this.root.next.values()) { child.fail = this.root; queue.push(child); }
    while (queue.length) {
      const current = queue.shift()!;
      for (const [token, child] of current.next) {
        let fail = current.fail!;
        const direct = fail.next.get(token);
        if (direct) fail = direct;
        else {
          fail = fail.fail!;
          while (!fail.next.has(token)) {
            fail = fail.fail!;
            if (fail.token === -1) break;
          }
          const hop = fail.next.get(token);
          if (hop) fail = hop;
        }
        child.fail = fail;
        let output: ContextState | null = fail;
        while (output && !output.isEnd) {
          output = output.fail;
          if (output && output.token === -1) { output = null; break; }
        }
        child.output = output;
        child.outputScore = f32(child.outputScore + (output ? output.outputScore : 0));
        queue.push(child);
      }
    }
  }
}
