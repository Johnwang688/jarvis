import type { DenoiseFailCode, FailCode } from "./protocol";

/** A detector failure with a closed-set reason; never carries engine text. */
export class FailError extends Error {
  constructor(readonly code: FailCode) {
    super(code);
    this.name = "FailError";
  }
}

/** The same for the noise-suppression stage, whose failures never stop the VAD. */
export class DenoiseFailError extends Error {
  constructor(readonly code: DenoiseFailCode) {
    super(code);
    this.name = "DenoiseFailError";
  }
}
