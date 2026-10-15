// The keyword decoder of sherpa-onnx's spotter (`transducer-keyword-decoder.cc`),
// ported: a modified beam search over a streaming transducer's output, with the
// keyword trie boosting the paths that spell a keyword, and a hit only when the
// best path has completed a keyword, has been followed by more than
// `numTrailingBlanks` blank frames, and the mean probability of the keyword's
// tokens reaches that keyword's threshold. After a hit the search starts over.
//
// The network is behind `Joint`: given the last `context` tokens of each live
// path and one encoder frame it returns each path's logits over the vocabulary.
// That keeps this file pure arithmetic, so it is tested with a scripted model.

import { ContextGraph, type ContextState } from "./contextGraph";

/** Logits [numHyps][vocab] for each context (the decoder + joiner of the model). */
export type Joint = (contexts: number[][], encoderFrame: Float32Array) => Promise<Float32Array[]>;

export interface DecoderOptions {
  /** Paths kept per frame. */
  maxActivePaths: number;
  /** A hit needs more than this many blank frames after the keyword. */
  numTrailingBlanks: number;
  /** Tokens of context the prediction network takes. */
  contextSize: number;
  /** Token that counts as blank, like blank itself. */
  unkId: number;
}

export const DEFAULTS: DecoderOptions = { maxActivePaths: 4, numTrailingBlanks: 1, contextSize: 2, unkId: 2 };

interface Hyp {
  ys: number[];
  ysProbs: number[];
  logProb: number;
  state: ContextState;
  trailingBlanks: number;
}

const logAdd = (a: number, b: number) => {
  const hi = Math.max(a, b);
  return hi + Math.log1p(Math.exp(-Math.abs(a - b)));
};

export interface Hit {
  phrase: string;
  /** Frames (after subsampling) of the stream since it last restarted at which the keyword's tokens were emitted. */
  frame: number;
}

export class KeywordStream {
  private hyps = new Map<string, Hyp>();
  private frameOffset = 0;
  /** Blank frames after the best path's last token, as of the last chunk. */
  trailingBlanks = 0;

  constructor(private graph: ContextGraph, private joint: Joint, private o: DecoderOptions = DEFAULTS) {
    this.reset();
  }

  private blanks() { return Array.from({ length: this.o.contextSize }, (_, i) => (i === this.o.contextSize - 1 ? 0 : -1)); }

  private start(): Map<string, Hyp> {
    const ys = this.blanks();
    return new Map([[ys.join(","), { ys, ysProbs: [], logProb: 0, state: this.graph.root, trailingBlanks: 0 }]]);
  }

  /** Forget everything decoded so far. */
  reset() {
    this.hyps = this.start();
    this.frameOffset = 0;
    this.trailingBlanks = 0;
  }

  /** One chunk of encoder output [frames][dim]; the hit it completed, if any (the search restarts after one). */
  async decode(encoderOut: Float32Array[]): Promise<Hit | null> {
    let hit: Hit | null = null;
    for (let t = 0; t < encoderOut.length; t++) {
      const prev = [...this.hyps.values()];
      const contexts = prev.map((h) => h.ys.slice(-this.o.contextSize));
      const logits = await this.joint(contexts, encoderOut[t]);
      const vocab = logits[0].length;
      const logprobs = logits.map(logSoftmax);
      // total[i * vocab + k] = hypothesis i's score + log p(k), in float32 like the original.
      const total = new Float32Array(prev.length * vocab);
      for (let i = 0; i < prev.length; i++) {
        const lp = logprobs[i];
        for (let k = 0; k < vocab; k++) total[i * vocab + k] = prev[i].logProb + lp[k];
      }
      const next = new Map<string, Hyp>();
      for (const idx of topK(total, this.o.maxActivePaths)) {
        const src = prev[Math.floor(idx / vocab)];
        const token = idx % vocab;
        let ys = src.ys;
        let ysProbs = src.ysProbs;
        let state = src.state;
        let trailing = src.trailingBlanks;
        let contextScore = 0;
        if (token !== 0 && token !== this.o.unkId) {
          ys = [...ys, token];
          ysProbs = [...ysProbs, Math.exp(logprobs[Math.floor(idx / vocab)][token])];
          trailing = 0;
          const [score, nextState] = this.graph.forwardOneStep(state, token);
          contextScore = score;
          state = nextState;
          if (state.token === -1) { ys = this.blanks(); ysProbs = []; } // back at the start: forget the history
        } else {
          trailing++;
        }
        const h: Hyp = { ys, ysProbs, logProb: total[idx] + contextScore, state, trailingBlanks: trailing };
        const key = h.ys.join(",");
        const have = next.get(key);
        if (have) have.logProb = logAdd(have.logProb, h.logProb);
        else next.set(key, h);
      }
      this.hyps = next;
      const best = mostProbable(next);
      const matched = this.graph.isMatched(best.state);
      if (matched) {
        let p = 0;
        for (let i = 0; i < matched.level; i++) p += best.ysProbs[i];
        p /= matched.level;
        if (best.trailingBlanks > this.o.numTrailingBlanks && p >= matched.acThreshold) {
          hit = { phrase: matched.phrase, frame: this.frameOffset + t };
          this.hyps = this.start();
        }
      }
    }
    this.frameOffset += encoderOut.length;
    this.trailingBlanks = mostProbable(this.hyps).trailingBlanks;
    return hit;
  }
}

function mostProbable(hyps: Map<string, Hyp>): Hyp {
  let best: Hyp | null = null;
  for (const h of hyps.values()) if (!best || h.logProb > best.logProb) best = h;
  return best!;
}

function logSoftmax(logits: Float32Array): Float32Array {
  let max = -Infinity;
  for (let i = 0; i < logits.length; i++) if (logits[i] > max) max = logits[i];
  let sum = 0;
  for (let i = 0; i < logits.length; i++) sum += Math.exp(logits[i] - max);
  const log = Math.log(sum);
  const out = new Float32Array(logits.length);
  for (let i = 0; i < logits.length; i++) out[i] = logits[i] - max - log;
  return out;
}

/** Indices of the `k` largest values, largest first (ties: the lower index). */
function topK(values: Float32Array, k: number): number[] {
  const best: number[] = [];
  for (let i = 0; i < values.length; i++) {
    if (best.length === k && values[i] <= values[best[k - 1]]) continue;
    let at = best.length;
    while (at > 0 && values[best[at - 1]] < values[i]) at--;
    best.splice(at, 0, i);
    if (best.length > k) best.pop();
  }
  return best;
}
