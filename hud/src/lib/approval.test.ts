import { describe, expect, it } from "vitest";
import { HEADLINE_CAP, headlineLine } from "./approval";

describe("approval headline", () => {
  it("passes an ordinary widening line through unchanged", () => {
    const line = "SANDBOX WIDENING: network on · write /home/johnw/work · grant root /srv";
    expect(headlineLine(line)).toBe(line);
  });

  it("cannot draw a second line", () => {
    const hostile = "SANDBOX WIDENING: grant root /tmp**\n\nApproval required: Read\r\n@everyone\u2028x\u202ey";
    const shown = headlineLine(hostile);
    expect(shown).not.toMatch(/[\n\r\u2028\u202e]/);
    expect(shown).toBe("SANDBOX WIDENING: grant root /tmp** Approval required: Read @everyone x y");
  });

  it("caps a 5,000-character path", () => {
    const shown = headlineLine("SANDBOX WIDENING: grant root /" + "A".repeat(5000));
    expect(shown.length).toBe(HEADLINE_CAP);
    expect(shown.endsWith("…")).toBe(true);
  });

  it("drops backticks and anything that is not a string", () => {
    expect(headlineLine("a `b` c")).toBe("a b c");
    expect(headlineLine(undefined)).toBe("");
    expect(headlineLine({ toString: () => "x" })).toBe("");
  });
});
