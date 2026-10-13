import { describe, expect, it } from "vitest";
import {
  BUILD_FIRST_CODE, BUILD_FIRST_STEPS, DOWNLOAD_NAME, DeviceLedger, SETUP_COMMAND, buildLine, chainCancellable, chainLine,
  stillStale,
  defaultName, downloadLabel, errorTarget, isExplicitClick, nameRefusal, needsBuildFirst, offersVex, parseBuild, parseDetection, parseDeviceRecord, parseLastBuilds,
  parseStatus, setupNote, slotRefusal, type BuildError, type BuildView,
} from "./vex";

const err = (over: Partial<BuildError> = {}): BuildError => ({
  file: "src/main.cpp", line: 12, col: 5, severity: "error", message: "expected ';'", in_project: true, ...over,
});

const build = (over: Partial<BuildView> = {}): BuildView => ({
  id: "0a1b2c3d", project_id: "p3", action: "build", state: "succeeded", terminal_id: "a0000001", exit_code: 0,
  errors: [], output: "", bin: { path: "build/override.bin", size: 56288, mtime: "2026-10-11T12:00:00+00:00" },
  started: "2026-10-11T12:00:00+00:00", ended: "2026-10-11T12:00:09+00:00", ...over,
});

describe("the download name mirrors the daemon's rule", () => {
  // jarvis/v2/vex.py DOWNLOAD_NAME: [A-Za-z0-9_][A-Za-z0-9_-]{0,19}, fullmatch, ASCII.
  it("takes letters, digits, _ and -, 1 to 20, never starting with -", () => {
    for (const ok of ["override", "a", "_x", "9", "auton-v2", "A_B-c", "x".repeat(20)]) {
      expect(nameRefusal(ok), ok).toBeNull();
      expect(DOWNLOAD_NAME.test(ok)).toBe(true);
    }
  });

  it("refuses what the daemon refuses (VX-1's list: -x, a b, a;b, Unicode, 21 characters, empty)", () => {
    for (const bad of ["", "-x", "--name", "a b", "a;b", "a\"b", "a&b", "a/b", "a\\b", "é", "ａ", "x".repeat(21),
      "a\n", "\na", "a\u200bb", "%PATH%", "a.bin", "a:b"]) {
      expect(nameRefusal(bad), JSON.stringify(bad)).not.toBeNull();
    }
    expect(nameRefusal("-x")).toMatch(/start with -/);
    expect(nameRefusal("x".repeat(21))).toMatch(/at most 20/);
  });

  it("is anchored at both ends (a trailing newline is not a name)", () => {
    expect(DOWNLOAD_NAME.test("ok\n")).toBe(false);
    expect(DOWNLOAD_NAME.test("ok;rm")).toBe(false);
  });

  it("defaults to the project's name, made to pass", () => {
    expect(defaultName("override")).toBe("override");
    expect(defaultName("My Robot 2026!")).toBe("My_Robot_2026_");
    expect(defaultName("-flag")).toBe("flag");
    expect(defaultName("a".repeat(30))).toBe("a".repeat(20));
    expect(defaultName("")).toBe("program");
    expect(defaultName("---")).toBe("program");
    for (const n of ["x y", "é", "-a b", "名前"]) expect(nameRefusal(defaultName(n))).toBeNull();
  });

  it("slots are whole numbers 1 to 8", () => {
    for (const s of [1, 4, 8]) expect(slotRefusal(s)).toBeNull();
    for (const s of [0, 9, -1, 1.5, "3", true, null, NaN]) expect(slotRefusal(s), String(s)).not.toBeNull();
  });
});

describe("download is the owner's click", () => {
  it("needs a trusted pointer click with the primary button", () => {
    expect(isExplicitClick({ isTrusted: true, detail: 1, button: 0 })).toBe(true);
  });

  it("a double or triple click's later clicks never count (L3: one download or none, never three)", () => {
    expect(isExplicitClick({ isTrusted: true, detail: 2, button: 0 })).toBe(false);
    expect(isExplicitClick({ isTrusted: true, detail: 3, button: 0 })).toBe(false);
  });

  it("never Enter or Space on a focused button (detail 0), a script's click, or another button", () => {
    expect(isExplicitClick({ isTrusted: true, detail: 0, button: 0 })).toBe(false);
    expect(isExplicitClick({ isTrusted: false, detail: 1, button: 0 })).toBe(false);
    expect(isExplicitClick({ isTrusted: false, detail: 0, button: 0 })).toBe(false);
    expect(isExplicitClick({ isTrusted: true, detail: 1, button: 1 })).toBe(false);
  });

  it("the button says what it does", () => {
    expect(downloadLabel(3)).toBe("Download to slot 3: replaces whatever is in slot 3 on the brain");
  });

  it("chains a build only on the daemon's build_first code, never on wording", () => {
    expect(needsBuildFirst(BUILD_FIRST_CODE)).toBe(true);
    expect(needsBuildFirst("build_first")).toBe(true);
    // The sentence alone is not the code: a refusal that only mentions it builds nothing.
    expect(needsBuildFirst("there is no build/override.bin yet; Build first")).toBe(false);
    expect(needsBuildFirst(undefined)).toBe(false);
    expect(needsBuildFirst(null)).toBe(false);
    expect(needsBuildFirst("Build_First")).toBe(false);
  });

  it("Build first is a build, then one rebuild — never a third (delta review MED-1)", () => {
    expect(BUILD_FIRST_STEPS).toEqual(["build", "rebuild"]);
    const said = stillStale("build/override.bin is older than the sources; Build first, then download");
    expect(said).toMatch(/^Nothing was downloaded/);
    expect(said).toContain("older than the sources; Build first, then download");
  });
});

describe("one chain per window (M1)", () => {
  const c = (over = {}) => ({ label: "override", slot: 3, phase: "build" as const, cancelled: false, ...over });

  it("says what the running chain does, on which project and slot", () => {
    expect(chainLine(c())).toBe("Building override, then downloading to slot 3");
    expect(chainLine(c({ phase: "download" }))).toBe("Downloading override to slot 3…");
    expect(chainLine(c({ phase: "start" }))).toBe("Downloading override to slot 3…");
    expect(chainLine(c({ phase: "pause" }))).toMatch(/^Pausing the brain terminal, then downloading override to slot 3/);
    expect(chainLine(c({ cancelled: true }))).toMatch(/^Cancelling/);
    expect(chainLine(c({ label: "" }))).toBe("Building this project, then downloading to slot 3");
    expect(chainLine(c({ phase: "rebuild" }))).toBe("Rebuilding override from scratch, then downloading to slot 3");
    expect(chainCancellable(c({ phase: "rebuild" }))).toBe(true);
  });

  it("cancels until the download is sent, never mid-write", () => {
    expect(chainCancellable(c())).toBe(true);
    expect(chainCancellable(c({ phase: "start" }))).toBe(true);
    expect(chainCancellable(c({ phase: "pause" }))).toBe(true);
    expect(chainCancellable(c({ phase: "download" }))).toBe(false);
    expect(chainCancellable(c({ cancelled: true }))).toBe(false);
  });
});

describe("detection", () => {
  it("reads a VEX project", () => {
    const d = parseDetection({ project_id: "p3", vex: true, name: "override", language: "cpp", slot: 1,
      sdk_version: "V5_20240802_15_00_00", printf_float: false, python_main: null, on_windows_drive: true,
      builds: true }, "p3");
    expect(d.vex).toBe(true);
    expect(d.language).toBe("cpp");
    expect(d.slot).toBe(1);
    expect(d.builds).toBe(true);
    expect(offersVex(d)).toBe(true);
  });

  it("offers the view only for a VEX project, or one whose file can't be used (to say why)", () => {
    expect(offersVex(parseDetection({ vex: false, reason: "not a VEX project" }, "p1"))).toBe(false);
    expect(offersVex(parseDetection({ vex: false, malformed: true, reason: "x" }, "p1"))).toBe(true);
    expect(offersVex(parseDetection({ vex: false, pros: true, reason: "PROS" }, "p1"))).toBe(true);
    expect(offersVex(null)).toBe(false);
    expect(offersVex(parseDetection(null, "p1"))).toBe(false);
  });

  it("never takes a half-said project for a VEX one", () => {
    expect(parseDetection({ vex: true, language: "rust", slot: 1 }, "p").vex).toBe(false);
    expect(parseDetection({ vex: true, language: "cpp", slot: 9 }, "p").vex).toBe(false);
    expect(parseDetection({ vex: true, language: "cpp" }, "p").vex).toBe(false);
  });
});

describe("builds", () => {
  it("parses the daemon's record", () => {
    const b = parseBuild({ ...build(), errors: [err(), { junk: 1 }, err({ line: 0 })] })!;
    expect(b.errors).toHaveLength(1);
    expect(parseBuild({ ...build(), id: "../x" })).toBeNull();
    expect(parseBuild({ ...build(), state: "exploded" })).toBeNull();
  });

  it("keeps the latest output and accepts older daemons without a preview", () => {
    expect(parseBuild({ ...build(), output: "a".repeat(20_000) + "error: link failed" })!.output)
      .toBe(("a".repeat(20_000) + "error: link failed").slice(-16 * 1024));
    expect(parseBuild({ ...build(), output: undefined })!.output).toBe("");
  });

  it("names clean and rebuild in running and failed states", () => {
    expect(buildLine(build({ action: "clean", state: "running" })).text).toBe("Cleaning…");
    expect(buildLine(build({ action: "rebuild", state: "running" })).text).toBe("Rebuilding…");
    expect(buildLine(build({ action: "clean", state: "failed", exit_code: 2 })).text).toBe("✗ Clean failed (exit 2)");
    expect(buildLine(build({ action: "rebuild", state: "failed", exit_code: 1 })).text).toBe("✗ Rebuild failed (exit 1)");
  });

  it("says ✓, the errors, running or the timeout", () => {
    expect(buildLine(build()).text).toMatch(/^✓ Built build\/override\.bin/);
    expect(buildLine(build()).tone).toBe("ok");
    expect(buildLine(build({ state: "running" })).tone).toBe("busy");
    const failed = buildLine(build({ state: "failed", exit_code: 2, bin: null,
      errors: [err(), err({ severity: "warning" }), err()] }));
    expect(failed.tone).toBe("bad");
    expect(failed.text).toBe("✗ Build failed (exit 2): 2 errors, 1 warning");
    expect(buildLine(build({ state: "failed", exit_code: 2, errors: [] })).text).toBe("✗ Build failed (exit 2)");
    expect(buildLine(build({ state: "timed_out" })).text).toMatch(/5 minutes/);
    expect(buildLine(build({ action: "clean" })).text).toBe("✓ Cleaned");
    expect(buildLine(null).tone).toBe("none");
  });

  it("opens only a file inside the project, by a plain relative path", () => {
    expect(errorTarget(err())).toEqual({ path: "src/main.cpp", line: 12, col: 5 });
    expect(errorTarget(err({ in_project: false }))).toBeNull();
    for (const file of ["/usr/include/x.h", "C:/vex/sdk.h", "C:\\x.h", "../outside.cpp", "src/../../x.cpp",
      "src//x.cpp", "./../x.cpp", ".", "./", "", "src\\main.cpp", "a\u0000b"]) {
      expect(errorTarget(err({ file })), file).toBeNull();
    }
  });

  it("drops ./ so a path a compiler prints that way stays clickable, as the editor knows it", () => {
    expect(errorTarget(err({ file: "./src/main.cpp" }))).toEqual({ path: "src/main.cpp", line: 12, col: 5 });
    expect(errorTarget(err({ file: "src/./main.cpp" }))?.path).toBe("src/main.cpp");
    expect(errorTarget(err({ file: "./x.cpp" }))?.path).toBe("x.cpp");
  });
});

describe("setup", () => {
  const status = (over = {}) => parseStatus({ configured: false, hint: null, device: null, brain: null, ...over })!;

  it("names the one command when vex.json is missing", () => {
    const n = setupNote(status({ hint: "VEX is not set up: run `jarvis vex setup` in a terminal" }))!;
    expect(n.headline).toMatch(/isn't set up/);
    expect(n.detail).toBeNull();
    expect(SETUP_COMMAND).toBe("jarvis vex setup");
  });

  it("passes on the daemon's reason when setup must run again", () => {
    const n = setupNote(status({ hint: "the VEX tools changed since `jarvis vex setup` (make); run it again" }))!;
    expect(n.headline).toMatch(/again/);
    expect(n.detail).toMatch(/tools changed/);
  });

  it("says nothing once configured", () => {
    expect(setupNote(status({ configured: true }))).toBeNull();
    expect(setupNote(null)).toBeNull();
  });

  it("reads the device and the brain terminal", () => {
    const s = status({ configured: true, device: "brain", brain: "a0000002" });
    expect(s.device).toBe("brain");
    expect(s.brain).toBe("a0000002");
    expect(status({ device: "flash", brain: "../x" }).device).toBeNull();
    expect(status({ brain: "../x" }).brain).toBeNull();
  });
});

describe("device actions this window did not start", () => {
  it("match the window's own, once each, within the window of time", () => {
    const l = new DeviceLedger(1000);
    l.expect("download", 0);
    expect(l.heard("download", 100)).toBe(true);
    expect(l.heard("download", 200)).toBe(false);       // a second one is not ours
    l.expect("brain", 0);
    expect(l.heard("brain", 5000)).toBe(false);         // too late
    l.expect("info", 0);
    l.withdraw("info");
    expect(l.heard("info", 10)).toBe(false);            // the request failed; nothing of ours started
  });

  it("parses only the record's two fields", () => {
    expect(parseDeviceRecord({ action: "download", at: "t" })).toEqual({ action: "download", at: "t" });
    expect(parseDeviceRecord({ action: "flash" })).toBeNull();
    expect(parseDeviceRecord("x")).toBeNull();
  });
});

describe("last builds across a reload", () => {
  it("keeps ids only", () => {
    expect(parseLastBuilds(JSON.stringify({ p3: "0a1b2c3d", p4: "bad", "": "0a1b2c3d" }))).toEqual({ p3: "0a1b2c3d" });
    expect(parseLastBuilds("not json")).toEqual({});
    expect(parseLastBuilds(null)).toEqual({});
  });
});
