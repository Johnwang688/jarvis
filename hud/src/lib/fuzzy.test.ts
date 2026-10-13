import { describe, expect, it } from "vitest";
import { fuzzyFilter, fuzzyMatch, normalizeQuery, splitLine } from "./fuzzy";

const PATHS = [
  "src/main.cpp", "src/domain.cpp", "include/main.h", "src/autons/skills.cpp", "README.md",
  "src/subsystems/intake.cpp", "src/subsystems/drivetrain.cpp", "include/subsystems/drivetrain.h",
  ".github/workflows/ci.yml", "docs/maintenance.md", "Makefile", "src/MotorGroup.cpp",
];
const top = (q: string, n = 3) => fuzzyFilter(q, PATHS, (p) => p, n).map((r) => r.item);

describe("fuzzyMatch", () => {
  it("matches characters in order, case and spaces ignored", () => {
    expect(fuzzyMatch("smc", "src/main.cpp")).not.toBeNull();
    expect(fuzzyMatch("S M C", "src/main.cpp")).not.toBeNull();
    expect(fuzzyMatch("cms", "src/main.cpp")).toBeNull();
    expect(fuzzyMatch("zzz", "src/main.cpp")).toBeNull();
  });

  it("an empty query matches everything with no positions", () => {
    expect(fuzzyMatch("", "x")).toEqual({ score: 0, positions: [] });
    expect(normalizeQuery("  a B ")).toBe("ab");
  });

  it("positions point at the matched characters", () => {
    const hit = fuzzyMatch("main", "src/main.cpp")!;
    expect(hit.positions).toEqual([4, 5, 6, 7]);
  });

  it("prefers word starts", () => {
    const hit = fuzzyMatch("mg", "src/MotorGroup.cpp")!;
    expect(hit.positions).toEqual([4, 9]);
  });
});

describe("fuzzyFilter", () => {
  it("ranks the file's own name above a spread over folders", () => {
    expect(top("main")[0]).toBe("src/main.cpp");
    expect(top("main").slice(0, 2).sort()).toEqual(["include/main.h", "src/main.cpp"]);
    expect(top("drivh")[0]).toBe("include/subsystems/drivetrain.h");
    expect(top("intake")[0]).toBe("src/subsystems/intake.cpp");
  });

  it("a name start beats a match inside a word", () => {
    const order = top("main", 5);
    expect(order.indexOf("src/main.cpp")).toBeLessThan(order.indexOf("src/domain.cpp"));
    expect(order.indexOf("src/main.cpp")).toBeLessThan(order.indexOf("docs/maintenance.md"));
  });

  it("an empty query keeps the order; the limit holds", () => {
    expect(top("", 2)).toEqual(PATHS.slice(0, 2));
    expect(fuzzyFilter("s", PATHS, (p) => p, 4)).toHaveLength(4);
  });

  it("finds dotfiles", () => {
    expect(top("ciyml")[0]).toBe(".github/workflows/ci.yml");
  });

  it("is fast enough for 50,000 paths", () => {
    const many = Array.from({ length: 50_000 }, (_, i) => `src/dir${i % 97}/sub${i % 13}/file${i}.cpp`);
    const t = performance.now();
    const got = fuzzyFilter("dir5fil", many, (p) => p, 50);
    expect(got.length).toBe(50);
    expect(performance.now() - t).toBeLessThan(1500);
  });
});

describe("splitLine", () => {
  it("reads :line and :line:col", () => {
    expect(splitLine("main.cpp:42")).toEqual({ query: "main.cpp", line: 42, col: null });
    expect(splitLine("main.cpp:42:7")).toEqual({ query: "main.cpp", line: 42, col: 7 });
    expect(splitLine(":12")).toEqual({ query: "", line: 12, col: null });
    expect(splitLine("main.cpp")).toEqual({ query: "main.cpp", line: null, col: null });
    expect(splitLine("main.cpp:0")).toEqual({ query: "main.cpp:0", line: null, col: null });
    expect(splitLine("a b")).toEqual({ query: "a b", line: null, col: null });
  });
});
