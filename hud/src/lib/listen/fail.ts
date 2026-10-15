import type { FailCode } from "./protocol";

/** A detector failure with a closed-set reason; never carries engine text. */
export class FailError extends Error {
  constructor(readonly code: FailCode) {
    super(code);
    this.name = "FailError";
  }
}
