import { describe, expect, it } from "vitest";
import {
  BUILD_FIRST_CODE, brainView, type BrainInput, DOWNLOAD_NAME, DeviceLedger, OUTPUT_TABS, SETUP_COMMAND, buildLine, buildMark, chainLine,
  chainStepOf, chainSteps, chainStoppable, downloadButtonLabel, downloadMark, helpText, isSourcePath, isTrustedPress,
  markGlyph, nextTab, outputTabFor, stillStale, stoppedDownloadNote,
  defaultName, downloadLabel, downloadProgress, errorTarget, isExplicitClick, nameRefusal, needsBuildFirst, offersVex, parseBuild, parseDetection, parseDeviceRecord, parseLastBuilds,
  parseStatus, parseDownload, parseConnection, setupNote, slotRefusal, type BuildError, type BuildView,
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

  it("the button says where it goes; what it replaces is its help (owner decision 2)", () => {
    expect(downloadButtonLabel(3)).toBe("Download to slot 3");
    expect(downloadLabel(3)).toBe("Download to slot 3: replaces whatever is in slot 3 on the brain");
    expect(helpText("download", 3)).toContain("replaces whatever is in slot 3 on the brain");
    expect(helpText("download", 3)).toMatch(/Mouse click only/);
    expect(helpText("slot", 5)).toContain("replaces whatever is in slot 5");
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

  it("still Build first after a build and a rebuild is said, never looped (delta review MED-1)", () => {
    const said = stillStale("build/override.bin is older than the sources; Build first, then download");
    expect(said).toMatch(/^Nothing was downloaded/);
    expect(said).toContain("older than the sources; Build first, then download");
  });
});

describe("one chain per window (M1): clean, build, download (owner decision 3)", () => {
  const c = (over = {}) => ({ label: "override", slot: 3, phase: "build" as const, cancelled: false, ...over });

  it("says what the running chain does, on which project and slot", () => {
    expect(chainLine(c({ phase: "clean" }))).toBe("Cleaning override, then building and downloading to slot 3");
    expect(chainLine(c())).toBe("Building override, then downloading to slot 3");
    expect(chainLine(c({ phase: "download" }))).toBe("Downloading override to slot 3…");
    expect(chainLine(c({ phase: "start" }))).toBe("Downloading override to slot 3…");
    expect(chainLine(c({ phase: "pause" }))).toMatch(/^Pausing the brain terminal, then downloading override to slot 3/);
    expect(chainLine(c({ cancelled: true }))).toMatch(/^Stopping/);
    expect(chainLine(c({ label: "" }))).toBe("Building this project, then downloading to slot 3");
    expect(chainLine(c({ phase: "rebuild" }))).toBe("Rebuilding override from scratch, then downloading to slot 3");
  });

  it("lists its steps: done, running, to come; a rebuild only once it happens", () => {
    expect(chainSteps(c({ phase: "clean", done: [] }))).toEqual([
      { step: "clean", state: "busy" }, { step: "build", state: "todo" }, { step: "download", state: "todo" }]);
    expect(chainSteps(c({ phase: "build", done: ["clean"] })).map((s) => s.state)).toEqual(["done", "busy", "todo"]);
    expect(chainSteps(c({ phase: "pause", done: ["clean", "build"] })).map((s) => s.state)).toEqual(["done", "done", "busy"]);
    expect(chainSteps(c({ phase: "rebuild", done: ["clean", "build"] })).map((s) => s.step))
      .toEqual(["clean", "build", "rebuild", "download"]);
    expect(chainStepOf("start")).toBe("clean");
    expect(chainStepOf("pause")).toBe("download");
  });

  it("Stop is offered for the whole chain, the download included (owner decision 6)", () => {
    for (const phase of ["start", "clean", "build", "pause", "rebuild", "download"] as const) {
      expect(chainStoppable(c({ phase }))).toBe(true);
    }
    expect(chainStoppable(c({ cancelled: true }))).toBe(false);
  });

  it("a stopped download says the slot is not known, and what to do", () => {
    const said = stoppedDownloadNote(4);
    expect(said).toMatch(/^Download stopped: slot 4 may hold an incomplete program/);
    expect(said).toContain("Download again before running it");
  });
});

describe("Stop is a trusted press (owner decision 6)", () => {
  it("takes a pointer click or Enter/Space on the button, never a script's or a dispatched click", () => {
    expect(isTrustedPress({ isTrusted: true, button: 0 })).toBe(true);
    expect(isTrustedPress({ isTrusted: false, button: 0 })).toBe(false);
    expect(isTrustedPress({ isTrusted: true, button: 2 })).toBe(false);
  });
});

describe("state marks (VX-UI-2)", () => {
  const base = { starting: false, following: true, stopped: false, editedAt: null, cleanedAt: null };
  const ended = Date.parse("2026-10-11T12:00:09+00:00");

  it("idle, running, ✓ and ✗ with words", () => {
    expect(buildMark("build", { ...base, record: null })).toEqual({ state: "idle", text: "Build: not run in this window" });
    expect(buildMark("clean", { ...base, record: null, starting: true }).state).toBe("busy");
    expect(buildMark("build", { ...base, record: build({ state: "running" }) }).state).toBe("busy");
    expect(buildMark("build", { ...base, record: build() })).toEqual({ state: "ok", text: "Build: succeeded" });
    const failed = buildMark("build", { ...base, record: build({ state: "failed", errors: [err(), err()] }) });
    expect(failed).toEqual({ state: "failed", text: "Build: failed (2 errors)" });
    expect(buildMark("rebuild", { ...base, record: build({ action: "rebuild", state: "timed_out" }) }).text)
      .toBe("Rebuild: timed out");
    expect(buildMark("build", { ...base, stopped: true, record: build({ state: "failed" }) }).text).toBe("Build: stopped");
  });

  it("stale after an edit or a clean (advisory), never for Clean itself", () => {
    expect(buildMark("build", { ...base, record: build(), editedAt: ended + 1 })).toEqual(
      { state: "stale", text: "Build: succeeded, edited since" });
    expect(buildMark("build", { ...base, record: build(), editedAt: ended - 1 }).state).toBe("ok");
    expect(buildMark("rebuild", { ...base, record: build({ action: "rebuild" }), cleanedAt: ended + 5 }).text)
      .toBe("Rebuild: succeeded, cleaned since");
    expect(buildMark("clean", { ...base, record: build({ action: "clean" }), editedAt: ended + 1 }).state).toBe("ok");
  });

  it("unknown when a running record is no longer followed", () => {
    expect(buildMark("build", { ...base, following: false, record: build({ state: "running" }) }).state).toBe("unknown");
  });

  it("Download: running, in the slot, older build, stopped, unknown", () => {
    const d = { busy: false, endedAt: 100, builtAt: 50 };
    expect(downloadMark({ ...d, downloaded: null }).state).toBe("idle");
    expect(downloadMark({ ...d, busy: true, downloaded: null }).state).toBe("busy");
    expect(downloadMark({ ...d, downloaded: { slot: 3, ok: true } })).toEqual({ state: "ok", text: "Download: in slot 3" });
    expect(downloadMark({ ...d, builtAt: 150, downloaded: { slot: 3, ok: true } }).state).toBe("stale");
    expect(downloadMark({ ...d, downloaded: { slot: 3, ok: false, stopped: true } }).text).toMatch(/stopped, slot 3 may be incomplete/);
    expect(downloadMark({ ...d, downloaded: { slot: 3, ok: null } }).state).toBe("unknown");
  });

  it("never amber: no mark state means held, and each draws a glyph beside its colour", () => {
    const states = ["idle", "busy", "ok", "failed", "stale", "unknown"] as const;
    expect(states.map(markGlyph)).toEqual(["", "", "✓", "✗", "✓", "?"]);
  });

  it("an edit counts only for what a build compiles or obeys (the daemon's SOURCE_SUFFIXES)", () => {
    for (const p of ["src/main.cpp", "include/pid.hpp", "makefile", "vex/mkrules.mk", "a/B.H"]) expect(isSourcePath(p)).toBe(true);
    for (const p of ["notes.txt", "README.md", "src/main.cpp~", ".hpp", "sim/field.html"]) expect(isSourcePath(p)).toBe(false);
  });
});

describe("output tabs", () => {
  it("an action selects its tab", () => {
    expect(outputTabFor("clean")).toBe("build");
    expect(outputTabFor("rebuild")).toBe("build");
    expect(outputTabFor("download")).toBe("download");
    expect(outputTabFor("info")).toBe("device");
    expect(outputTabFor("programs")).toBe("device");
    expect(outputTabFor("usb")).toBe("device");
    expect(outputTabFor("brain")).toBe("brain");
  });

  it("arrow keys, Home and End move along the tablist", () => {
    expect(OUTPUT_TABS).toEqual(["build", "download", "brain", "device"]);
    expect(nextTab(OUTPUT_TABS, "build", "ArrowRight")).toBe("download");
    expect(nextTab(OUTPUT_TABS, "build", "ArrowLeft")).toBe("device");
    expect(nextTab(OUTPUT_TABS, "device", "ArrowRight")).toBe("build");
    expect(nextTab(OUTPUT_TABS, "download", "Home")).toBe("build");
    expect(nextTab(OUTPUT_TABS, "download", "End")).toBe("device");
    expect(nextTab(OUTPUT_TABS, "download", "Enter")).toBeNull();
  });

  it("the Brain tab says it connects only on a click (owner's answer to §0.1 item 5)", () => {
    expect(helpText("tab-brain", 1)).toMatch(/connects only when you click/);
    expect(helpText("stop", 1)).toMatch(/never stops the program on the brain/);
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
    expect(buildLine(build({ state: "failed", exit_code: 129 }), true).text).toMatch(/^✗ Build stopped/);
    expect(buildLine(build(), true).text).toMatch(/^✓ Built/);
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

describe("bounded VEX feedback records", () => {
  it("parses Download outcomes without inferring success from an exit code", () => {
    const d = {id: "a0000001", project_id: "p3", slot: 3, state: "running", terminal_id: "b0000001",
      exit_code: 0, output: "x".repeat(20000), started: "", ended: null};
    expect(parseDownload(d)?.state).toBe("running");
    expect(parseDownload(d)?.output.length).toBe(16384);
    expect(parseDownload({...d, slot: 9})).toBeNull();
    expect(parseDownload({...d, state: "done"})).toBeNull();
  });
  it("USB unknown is distinct from disconnected and history has a strict bound", () => {
    const c = {brain: null, controller: false, state: "busy", checked_at: null, message: "busy", events: []};
    expect(parseConnection(c)?.brain).toBeNull();
    expect(parseConnection(c)?.controller).toBe(false);
    expect(parseConnection({...c, brain: 0})).toBeNull();
    expect(parseConnection({...c, events: new Array(33).fill({device: "brain", connected: false, at: ""})})).toBeNull();
    expect(parseConnection({...c, events: [{device: "radio", connected: true, at: ""}]})).toBeNull();
  });
});

describe("download progress reads vexcom's own progress line", () => {
  it("follows the live format, a number per 10% and a dot per 2%", () => {
    const head = "Device: COM3\nvexcom utility V1.0.2b2\ndownload :\n";
    expect(downloadProgress(head)).toBeNull();
    expect(downloadProgress(head + "0")).toBeNull();
    expect(downloadProgress(head + "0..")).toBe(4);
    expect(downloadProgress(head + "0....10....20....30.")).toBe(32);
    expect(downloadProgress(head + "0....10....20....30....40....50....60....70....80....\n90....100")).toBe(100);
    expect(downloadProgress(head + "0....10....20....30....40....50....60....70....80....90....100\n")).toBe(100);
  });
  it("never reads a half-written number, so the bar does not jump back", () => {
    expect(downloadProgress("0....10....20....30....40....50....60....70....80....90....10")).toBe(98);
    expect(downloadProgress("0....1")).toBe(8);
    expect(downloadProgress("0....10....2")).toBe(18);
    expect(downloadProgress("0....10....20")).toBe(18);
    expect(downloadProgress("0....10....20.")).toBe(22);
  });
  it("takes the last progress line, so a later phase starts again", () => {
    expect(downloadProgress("0....10....100\nverify :\n0....10....")).toBe(18);
  });
  it("reads only a line of numbers and dots, never other text", () => {
    for (const text of ["", "Downloading User Program", "slot 3: 90%", "error 100", "100", "0....10 done", "1.0.2", "192.168.1.1"]) {
      expect(downloadProgress(text)).toBeNull();
    }
  });
});

describe("brainView: the Brain tab's states (VX-UI-4a; owner decision 5)", () => {
  const base: BrainInput = {
    configured: true, blocked: false, chain: null, terminal: null, asking: false, session: null, exitCode: null,
    shownIn: null, error: "", closed: "", device: null, reading: false,
  };
  const v = (o: Partial<BrainInput> = {}) => brainView({ ...base, ...o });

  it("not connected: Connect offered and on, nothing drawn, and it says it never connects by itself", () => {
    const b = v();
    expect(b.state).toBe("off");
    expect(b.connect).toEqual({ shown: true, off: "" });
    expect(b.terminal).toBe(false);
    expect(b.disconnect.shown).toBe(false);
    expect(b.text).toMatch(/never connects by itself/);
    expect(b.badge.state).toBe("idle");
  });

  it("Connect is off, with the reason, under a card, during a chain, a device read or a download", () => {
    expect(v({ blocked: true }).connect.off).toMatch(/approval card/);
    expect(v({ chain: { paused: false } }).connect.off).toMatch(/download holds the robot's port/);
    expect(v({ reading: true }).connect.off).toMatch(/device read/);
    expect(v({ device: "info" }).connect.off).toMatch(/device read/);
    expect(v({ device: "download" }).connect.off).toMatch(/download holds/);
    expect(v({ configured: false }).state).toBe("unconfigured");
    expect(v({ configured: false }).connect.off).toMatch(/jarvis vex setup/);
    // A USB check is not refused here: the daemon waits for it (bounded) before the brain's reservation.
    expect(v({ device: "connection" }).connect.off).toBe("");
  });

  it("connecting: the request on its way, then the session attaching", () => {
    expect(v({ asking: true }).state).toBe("connecting");
    expect(v({ asking: true }).connect.shown).toBe(false);
    const attaching = v({ terminal: "c0000001", session: "connecting" });
    expect(attaching.state).toBe("connecting");
    expect(attaching.terminal).toBe(true);
    expect(attaching.disconnect).toEqual({ shown: true, close: false });
    expect(v({ terminal: "c0000001", session: null }).state).toBe("connecting");
  });

  it("connected: live output drawn, Disconnect offered, no Connect", () => {
    const b = v({ terminal: "c0000001", session: "attached" });
    expect(b.state).toBe("connected");
    expect(b.terminal).toBe(true);
    expect(b.connect.shown).toBe(false);
    expect(b.disconnect).toEqual({ shown: true, close: false });
    expect(b.badge).toEqual({ state: "ok", text: "live" });
  });

  it("paused: a download closed it; Connect shown but off, and it says it never reopens by itself", () => {
    const b = v({ chain: { paused: true } });
    expect(b.state).toBe("paused");
    expect(b.connect.shown).toBe(true);
    expect(b.connect.off).not.toBe("");
    expect(b.text).toMatch(/Paused for the download/);
    expect(b.text).toMatch(/never reopens by itself/);
    expect(b.badge.state).toBe("unknown");                  // never amber, never green
  });

  it("after a download: off again, saying why, with Connect on", () => {
    const b = v({ closed: "The brain terminal was closed for the download. Click Connect in the Brain tab to open it again." });
    expect(b.state).toBe("off");
    expect(b.text).toMatch(/closed for the download/);
    expect(b.connect).toEqual({ shown: true, off: "" });
  });

  it("ended: the program exited (its code said), or the link was lost; Connect again and Close", () => {
    const exited = v({ terminal: "c0000001", session: "exited", exitCode: 3 });
    expect(exited.state).toBe("ended");
    expect(exited.text).toMatch(/exited with code 3/);
    expect(exited.connect.shown).toBe(true);
    expect(exited.disconnect).toEqual({ shown: true, close: true });
    expect(exited.terminal).toBe(true);                      // its last output stays readable
    expect(exited.badge.state).toBe("failed");
    for (const session of ["ended", "closed"]) expect(v({ terminal: "c0000001", session }).state).toBe("ended");
  });

  it("error: the daemon's refusal in its own words, and Connect again", () => {
    const b = v({ error: "The brain terminal is using the brain's port; close it first. One device action at a time." });
    expect(b.state).toBe("error");
    expect(b.text).toMatch(/^The brain terminal is using/);
    expect(b.connect).toEqual({ shown: true, off: "" });
  });

  it("shown elsewhere: a pane or another VEX view says where, drawn nowhere here; another window is drawn (its Show it here)", () => {
    const pane = v({ terminal: "c0000001", session: "attached", shownIn: { kind: "pane", slot: 3 } });
    expect(pane.state).toBe("elsewhere");
    expect(pane.text).toMatch(/shown in pane 3/);
    expect(pane.terminal).toBe(false);
    const other = v({ terminal: "c0000001", session: "attached", shownIn: { kind: "vex", slot: 1 } });
    expect(other.text).toMatch(/VEX view in pane 1/);
    expect(other.terminal).toBe(false);
    const away = v({ terminal: "c0000001", session: "idle", shownIn: { kind: "window" } });
    expect(away.state).toBe("elsewhere");
    expect(away.terminal).toBe(true);
    expect(away.text).toMatch(/another window/);
  });

  it("taken or kept by another window: its own words, drawn (the strip's Take it back / Ask again), never a spinner", () => {
    const taken = v({ terminal: "c0000001", session: "taken" });
    expect(taken.state).toBe("taken");
    expect(taken.text).toMatch(/Taken by another window/);
    expect(taken.text).toMatch(/Take it back/);
    expect(taken.terminal).toBe(true);
    expect(taken.badge.state).not.toBe("busy");
    expect(taken.connect.shown).toBe(false);
    const kept = v({ terminal: "c0000001", session: "refused" });
    expect(kept.state).toBe("taken");
    expect(kept.text).toMatch(/kept it/);
    expect(kept.badge.state).not.toBe("busy");
    const waiting = v({ terminal: "c0000001", session: "waiting" });
    expect(waiting.text).toMatch(/Asking the window/);
  });

  it("lost: this window's socket gave up, the link may be open — Reconnect, and Disconnect (never Close)", () => {
    const lost = v({ terminal: "c0000001", session: "lost" });
    expect(lost.state).toBe("lost");
    expect(lost.reconnect).toBe(true);
    expect(lost.connect.shown).toBe(false);
    expect(lost.disconnect).toEqual({ shown: true, close: false });
    expect(lost.text).toMatch(/may still be open/);
    for (const session of ["attached", "exited", "taken", null]) {
      expect(v({ terminal: "c0000001", session }).reconnect).toBe(false);
    }
  });

  it("no state ever offers Connect as on while a terminal is open, a chain runs or a card is up", () => {
    const sessions = [null, "idle", "connecting", "attached", "waiting", "taken", "refused", "exited", "ended", "lost",
      "closed"];
    for (const session of sessions) {
      for (const chain of [null, { paused: false }, { paused: true }]) {
        for (const blocked of [false, true]) {
          for (const terminal of [null, "c0000001"]) {
            const b = v({ session: terminal ? session : null, chain, blocked, terminal });
            const on = b.connect.shown && !b.connect.off;
            if (on) {
              expect(blocked).toBe(false);
              expect(chain).toBeNull();
              expect(!terminal || ["exited", "ended", "closed"].includes(session ?? "")).toBe(true);
            }
          }
        }
      }
    }
  });
});
