// The VEX store's download chain, on its own (VX-2 delta review): the view's
// disabled buttons hide these rules, so they are held here without them.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const buildFirst = (sentence = "build/override.bin is older than the sources; Build first, then download") =>
  Object.assign(new Error(sentence), { status: 409, body: { error: sentence, code: "build_first" } });

const h = vi.hoisted(() => ({
  downloads: [] as { pid: string; slot: number; name: string }[],
  builds: [] as { pid: string; action: string }[],
  download: null as null | ((pid: string) => Promise<{ terminal_id: string }>),
  brain: null as string | null,
  /** The daemon's device reservation as `/vex/status` says it (`device`). */
  device: null as string | null,
  brainOpened: 0,
  closed: [] as string[],
  buildState: null as null | ((id: string) => any),
  order: [] as string[],
}));

vi.mock("../api", () => ({
  api: {
    vexStatus: async () => ({ configured: true, hint: null, vexcom_version: null, extension_version: null,
                              sdks: null, templates: false, device: h.device, brain: h.brain }),
    vexDownload: (pid: string, body: { slot: number; name: string }) => {
      h.downloads.push({ pid, ...body });
      h.order.push("download");
      return h.download!(pid).then(r => ({...r, download_id: r.terminal_id}));
    },
    vexDownloadState: async (id: string) => ({id, project_id: h.downloads.at(-1)?.pid ?? "p3", slot: 3,
      state: "succeeded", terminal_id: id, exit_code: 0, output: "Finished", started: "", ended: ""}),
    vexConnection: async () => ({brain: true, controller: false, state: "ready", checked_at: "", message: "", events: []}),
    vexBuild: async (pid: string, action: string) => {
      h.builds.push({ pid, action });
      h.order.push(action);
      return { build_id: `0000000${h.builds.length}`, terminal_id: `a000000${h.builds.length}` };
    },
    vexBuildState: async (id: string) => h.buildState ? h.buildState(id) : ({
      id, project_id: "p3", action: "build", state: "succeeded", terminal_id: null, exit_code: 0, errors: [],
      bin: null, started: "", ended: "",
    }),
    closeTerminal: async (id: string) => {
      h.closed.push(id);
      h.order.push(`close ${id}`);
      if (id === h.brain) h.brain = null;
      return { ok: true };
    },
    vexBrain: async () => {
      h.brainOpened += 1;
      h.brain = "c0000002";
      return { terminal_id: "c0000002", started: true };
    },
    vexProject: async () => ({}),
    // The daemon answers a device read only once it has freed the device.
    vexInfo: async (read: string) => {
      h.device = null;
      return { text: "{}", exit_code: 0, read };
    },
  },
}));
vi.mock("../components/Terminal", () => ({
  terminals: {
    showProgram: async () => {},
    adoptProgram: async () => true,
    gone: () => {},
    refresh: async () => {},
    row: () => ({ exited: true, exit_code: 0 }),
    sessions: new Map(),
  },
}));

import { VexStore } from "./vex";
import { brainView, type DownloadView } from "../lib/vex";
import { api } from "../api";
import { terminals } from "../components/Terminal";

afterEach(() => {
  vi.restoreAllMocks();
  vi.useRealTimers();
});

beforeEach(() => {
  h.downloads.length = 0;
  h.builds.length = 0;
  h.download = null;
  h.brain = null;
  h.device = null;
  h.brainOpened = 0;
  h.closed.length = 0;
  h.buildState = null;
  h.order.length = 0;
});

describe("build feedback", () => {
  it("reports clean immediately while its start request waits and ignores repeat clicks", async () => {
    const store = new VexStore();
    let release!: (v: { build_id: string; terminal_id: string }) => void;
    const start = vi.spyOn(api, "vexBuild").mockImplementationOnce(() => new Promise((res) => { release = res; }));
    const show = vi.spyOn(terminals, "showProgram");
    const work = store.build("p3", "clean");
    expect(store.run("p3").startingBuild?.action).toBe("clean");
    expect(store.run("p3").startingBuild?.started).toBeGreaterThan(0);
    await store.build("p3", "rebuild");
    expect(start).toHaveBeenCalledTimes(1);
    release({ build_id: "00000009", terminal_id: "a0000009" });
    await work;
    expect(show).toHaveBeenCalledWith("a0000009");
    expect(store.run("p3").startingBuild).toBeNull();
    expect(store.run("p3").busy).toBeNull();
  });

  it("puts startup failures beside the build controls and allows another try", async () => {
    const store = new VexStore();
    vi.spyOn(api, "vexBuild").mockRejectedValueOnce(new Error("make.exe could not start"));
    await store.build("p3", "rebuild");
    expect(store.run("p3").buildNote).toBe("make.exe could not start");
    expect(store.run("p3").startingBuild).toBeNull();
    expect(store.run("p3").busy).toBeNull();
    await store.build("p3", "build");
    expect(store.run("p3").buildNote).toBe("");
  });

  it("shows failed status reads while retrying, then clears the warning on recovery", async () => {
    vi.useFakeTimers();
    const store = new VexStore();
    const state = vi.spyOn(api, "vexBuildState").mockRejectedValueOnce(new Error("Network offline"));
    const work = store.build("p3", "build");
    await vi.advanceTimersByTimeAsync(0);
    expect(store.run("p3").buildNote).toContain("Network offline");
    expect(store.run("p3").buildNote).toContain("Retrying");
    expect(store.run("p3").busy).toBe("build");
    await vi.advanceTimersByTimeAsync(1200);
    await work;
    expect(state).toHaveBeenCalledTimes(2);
    expect(store.run("p3").buildNote).toBe("");
    expect(store.run("p3").busy).toBeNull();
  });

  it("can recheck after monitoring expires and clears forgotten restored builds", async () => {
    vi.useFakeTimers();
    const store = new VexStore();
    const running = { id: "00000001", project_id: "p3", action: "build", state: "running" as const,
      terminal_id: "a0000001", exit_code: null, errors: [], output: "compiling", bin: null, started: "", ended: null };
    const state = vi.spyOn(api, "vexBuildState").mockResolvedValue(running);
    const work = store.build("p3", "build");
    await vi.advanceTimersByTimeAsync(6 * 60_000 + 600);
    await work;
    expect(store.run("p3").build?.state).toBe("running");
    expect(store.monitoringBuild("p3")).toBe(false);
    state.mockResolvedValueOnce({ ...running, state: "succeeded", exit_code: 0 });
    await store.refreshBuild("p3");
    expect(store.run("p3").build?.state).toBe("succeeded");

    // A later build expires too, then is forgotten by a daemon restart.
    state.mockResolvedValue({ ...running, id: "00000002", action: "clean" });
    const second = store.build("p3", "clean");
    await vi.advanceTimersByTimeAsync(6 * 60_000 + 600);
    await second;
    state.mockRejectedValueOnce(Object.assign(new Error("build not found"), { status: 404 }));
    await store.refreshBuild("p3");
    expect(store.run("p3").build).toBeNull();
    expect(store.run("p3").buildNote).toMatch(/no longer knows/);
    expect(store.monitoringBuild("p3")).toBe(false);
    state.mockResolvedValueOnce({ ...running, id: "00000003", state: "succeeded", exit_code: 0 });
    await store.build("p3", "rebuild");
    expect(store.run("p3").build?.state).toBe("succeeded");
  });

  it("resumes expired monitoring on focus refresh", async () => {
    vi.useFakeTimers();
    const store = new VexStore();
    const running = { id: "00000001", project_id: "p3", action: "build", state: "running" as const,
      terminal_id: "a0000001", exit_code: null, errors: [], output: "", bin: null, started: "", ended: null };
    const state = vi.spyOn(api, "vexBuildState").mockResolvedValue(running);
    await store.refreshStatus();
    const work = store.build("p3", "build");
    await vi.advanceTimersByTimeAsync(6 * 60_000 + 600);
    await work;
    state.mockResolvedValueOnce({ ...running, state: "succeeded", exit_code: 0 });
    store.want("p3", true);
    await vi.advanceTimersByTimeAsync(0);
    expect(store.run("p3").build?.state).toBe("succeeded");
    expect(store.monitoringBuild("p3")).toBe(false);
  });
});

describe("one download chain per window (review M1), held by the store itself", () => {
  it("refuses a second download for any project while one runs, and says why", async () => {
    const store = new VexStore();
    let release!: (v: { terminal_id: string }) => void;
    h.download = () => new Promise((res) => { release = res; });
    const first = store.download("p3", 1, "override", "override");
    await vi.waitFor(() => expect(h.downloads).toHaveLength(1));
    expect(store.chain?.projectId).toBe("p3");
    // No button in the way: the store's own refusal.
    await store.download("p5", 2, "flywheel", "flywheel");
    expect(h.downloads).toHaveLength(1);
    expect(store.run("p5").note).toMatch(/already running for override/);
    expect(store.run("p5").busy).toBeNull();
    release({ terminal_id: "b0000001" });
    await first;
    expect(store.chain).toBeNull();
    // Once it has ended, the other project's goes.
    h.download = async () => ({ terminal_id: "b0000002" });
    await store.download("p5", 2, "flywheel", "flywheel");
    expect(h.downloads.map((d) => d.pid)).toEqual(["p3", "p5"]);
  });
});

describe("one click is clean, build, download (owner decision 3, 2026-10-15)", () => {
  it("cleans and builds before anything is sent, then downloads", async () => {
    const store = new VexStore();
    h.download = async () => ({ terminal_id: "b0000009" });
    await store.download("p3", 1, "override", "override");
    expect(h.order).toEqual(["clean", "build", "download"]);
    expect(store.run("p3").downloaded?.ok).toBe(true);
  });

  it("a failed clean stops the chain before anything reaches the robot", async () => {
    const store = new VexStore();
    h.download = async () => ({ terminal_id: "b0000009" });
    h.buildState = (id) => ({ id, project_id: "p3", action: "clean", state: "failed", terminal_id: null, exit_code: 2,
      errors: [], bin: null, started: "", ended: "" });
    await store.download("p3", 1, "override", "override");
    expect(h.order).toEqual(["clean"]);
    expect(h.downloads).toEqual([]);
    expect(store.run("p3").note).toBe("The clean did not succeed, so nothing was downloaded.");
    expect(store.chain).toBeNull();
  });

  it("a failed build stops it too", async () => {
    const store = new VexStore();
    h.download = async () => ({ terminal_id: "b0000009" });
    h.buildState = (id) => ({ id, project_id: "p3", action: id === "00000002" ? "build" : "clean",
      state: id === "00000002" ? "failed" : "succeeded", terminal_id: null, exit_code: id === "00000002" ? 2 : 0,
      errors: [], bin: null, started: "", ended: "" });
    await store.download("p3", 1, "override", "override");
    expect(h.order).toEqual(["clean", "build"]);
    expect(h.downloads).toEqual([]);
    expect(store.run("p3").note).toMatch(/build did not succeed, so nothing was downloaded/);
  });

  it("the brain terminal is let go of the port only after the build succeeded", async () => {
    const store = new VexStore();
    h.brain = "c0000001";
    h.download = async () => ({ terminal_id: "b0000004" });
    await store.download("p3", 3, "override", "override");
    expect(h.order).toEqual(["clean", "build", "close c0000001", "download"]);
    // Never opened again by itself (VX-UI-4a, owner decision 5): the Brain tab offers Connect.
    await new Promise((r) => setTimeout(r, 50));
    expect(h.brainOpened).toBe(0);
    expect(store.brainTerminal).toBeNull();
    expect(store.brainClosed).toMatch(/closed for the download\. Click Connect/);
  });
});

describe("Build first after the chain's build (delta review MED-1): one rebuild, never a second", () => {
  it("still stale after the build is followed by one rebuild, then the download", async () => {
    const store = new VexStore();
    const answers = [buildFirst(), null];
    h.download = async () => {
      const a = answers.shift();
      if (a) throw a;
      return { terminal_id: "b0000009" };
    };
    await store.download("p3", 1, "override", "override");
    expect(h.builds.map((b) => b.action)).toEqual(["clean", "build", "rebuild"]);
    expect(h.downloads).toHaveLength(2);
    expect(store.run("p3").downloaded?.ok).toBe(true);
  });

  it("still stale after the rebuild: stops, says the daemon's sentence, sends nothing more", async () => {
    const store = new VexStore();
    h.download = async () => {
      throw buildFirst();
    };
    await store.download("p3", 1, "override", "override");
    expect(h.builds.map((b) => b.action)).toEqual(["clean", "build", "rebuild"]);
    expect(h.downloads).toHaveLength(2);
    expect(store.run("p3").note).toMatch(/after a build and a rebuild/);
    expect(store.run("p3").note).toContain("Build first, then download");
    expect(store.chain).toBeNull();
  });

  it("a refusal without the code builds nothing more", async () => {
    const store = new VexStore();
    h.download = async () => {
      throw Object.assign(new Error("A download is running. One device action at a time."), {
        status: 409, body: { error: "A download is running. One device action at a time." },
      });
    };
    await store.download("p3", 1, "override", "override");
    expect(h.builds.map((b) => b.action)).toEqual(["clean", "build"]);
    expect(store.run("p3").note).toMatch(/One device action/);
  });
});

describe("what stopped a chain is what it says (delta review NIT)", () => {
  it("a crash is a crash, not an approval card", async () => {
    const store = new VexStore();
    h.download = async () => {
      store.crash();
      throw buildFirst();
    };
    await store.download("p3", 1, "override", "override");
    expect(h.builds.map((b) => b.action)).toEqual(["clean", "build"]);
    expect(store.run("p3").note).toMatch(/window hit an error/);
    expect(store.run("p3").note).not.toMatch(/approval card/);
  });

  it("a card that comes and goes while the download runs keeps the brain terminal closed", async () => {
    const store = new VexStore();
    h.brain = "c0000001";
    h.download = async () => {
      store.setBlocked(true);
      store.setBlocked(false);
      return { terminal_id: "b0000003" };
    };
    await store.download("p3", 1, "override", "override");
    expect(h.downloads).toHaveLength(1);               // already on its way: it goes
    expect(h.brainOpened).toBe(0);
    expect(store.brainClosed).toMatch(/closed for the download/);
  });

  it("with no card, the brain terminal is still not opened again after the download (VX-UI-4a)", async () => {
    vi.useFakeTimers();
    const store = new VexStore();
    h.brain = "c0000001";
    await store.refreshStatus();
    expect(store.brainTerminal).toBe("c0000001");           // a link the daemon holds is shown, never opened
    h.download = async () => ({ terminal_id: "b0000004" });
    const work = store.download("p3", 1, "override", "override");
    await vi.advanceTimersByTimeAsync(10_000);
    await work;
    await vi.advanceTimersByTimeAsync(10_000);
    expect(h.brainOpened).toBe(0);
    expect(store.brainTerminal).toBeNull();
    expect(store.brainClosed).toMatch(/never reopens by itself/);
    expect(store.brainNote).toBe("");                        // not an error: nothing failed
  });

  it("a card is a card", async () => {
    const store = new VexStore();
    h.download = async () => {
      store.setBlocked(true);
      store.setBlocked(false);
      throw buildFirst();
    };
    await store.download("p3", 1, "override", "override");
    expect(h.builds.map((b) => b.action)).toEqual(["clean", "build"]);
    expect(store.run("p3").note).toMatch(/approval card came up before it rebuilt/);
  });
});

describe("Stop stops the running VEX action (owner decision 6, 2026-10-15)", () => {
  const running = (id: string, action = "build") => ({ id, project_id: "p3", action, state: "running" as const,
    terminal_id: `a${id.slice(1)}`, exit_code: null, errors: [], output: "", bin: null, started: "", ended: null });

  it("during the chain's clean: its program closed, nothing downloaded, the line says Stopped", async () => {
    const store = new VexStore();
    h.download = async () => ({ terminal_id: "b0000001" });
    let stopped = false;
    h.buildState = (id) => stopped ? { ...running(id, "clean"), state: "failed", exit_code: 129 } : running(id, "clean");
    const work = store.download("p3", 2, "override", "override");
    await vi.waitFor(() => expect(store.run("p3").build?.state).toBe("running"));
    // Another project's view never stops this chain (review MED-2).
    expect(store.stoppable("p5")).toBe(false);
    store.stop("p5");
    expect(h.closed).toEqual([]);
    expect(store.chain?.cancelled).toBe(false);
    expect(store.stoppable("p3")).toBe(true);
    store.stop("p3");
    stopped = true;
    await work;
    expect(h.closed).toEqual(["a0000001"]);
    expect(h.downloads).toEqual([]);
    expect(store.run("p3").note).toBe("Stopped: nothing was downloaded.");
    // Its clean is said as stopped, not failed (review LOW-2).
    expect(store.run("p3").stoppedBuild).toBe("00000001");
    expect(store.chain).toBeNull();
  });

  it("another project's view stops neither the chain's download nor anything else (review MED-2)", async () => {
    vi.useFakeTimers();
    const store = new VexStore();
    h.download = async () => ({ terminal_id: "b0000001" });
    vi.spyOn(api, "vexDownloadState").mockResolvedValue({ id: "b0000001", project_id: "p3", slot: 3, state: "running",
      terminal_id: "b0000001", exit_code: null, output: "", started: "", ended: null });
    void store.download("p3", 3, "auton", "override");
    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => expect(store.chain?.phase).toBe("download"));
    expect(store.stoppable("p5")).toBe(false);
    store.stop("p5");
    store.stop(null);
    expect(h.closed).toEqual([]);
    expect(store.chain?.cancelled).toBe(false);
  });

  it("a stopped download stays stopped when a later Check status reads its end (review NIT-2)", async () => {
    vi.useFakeTimers();
    const store = new VexStore();
    h.download = async () => ({ terminal_id: "b0000001" });
    const rec = { id: "b0000001", project_id: "p3", slot: 3, state: "running" as const, terminal_id: "b0000001",
      exit_code: null as number | null, output: "", started: "", ended: null as string | null };
    const state = vi.spyOn(api, "vexDownloadState").mockImplementation(async () => ({ ...rec }));
    const work = store.download("p3", 3, "auton", "override");
    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => expect(store.chain?.phase).toBe("download"));
    store.stop("p3");
    // The record stays running past the follow's deadline: the outcome is not read.
    await vi.advanceTimersByTimeAsync(4 * 60_000 + 1000);
    await work;
    expect(store.run("p3").downloaded).toMatchObject({ ok: false, stopped: true });
    state.mockImplementation(async () => ({ ...rec, state: "failed", exit_code: 129, ended: "x" }));
    await store.refreshDownload("p3");
    expect(store.run("p3").downloaded).toMatchObject({ ok: false, stopped: true, code: 129 });
    expect(store.run("p3").note).toMatch(/^Download stopped: slot 3/);
  });

  it("whether the brain terminal holds the port is asked after the build, not at the chain's start (review LOW-1)", async () => {
    const store = new VexStore();
    h.download = async () => ({ terminal_id: "b0000004" });
    h.buildState = (id) => {
      h.brain = "c0000009";                              // opened while the project built
      return { id, project_id: "p3", action: "build", state: "succeeded", terminal_id: null, exit_code: 0,
        errors: [], bin: null, started: "", ended: "" };
    };
    await store.download("p3", 3, "override", "override");
    expect(h.order).toEqual(["clean", "build", "close c0000009", "download"]);
  });

  it("during the download: vexcom's terminal closed, and the slot is said to be unknown — never success", async () => {
    vi.useFakeTimers();
    const store = new VexStore();
    h.brain = "c0000001";
    h.download = async () => ({ terminal_id: "b0000001" });
    const rec = { id: "b0000001", project_id: "p3", slot: 4, state: "running" as const, terminal_id: "b0000001",
      exit_code: null as number | null, output: "download :\n0....10....20", started: "", ended: null as string | null };
    const state = vi.spyOn(api, "vexDownloadState").mockImplementation(async () => ({ ...rec }));
    const work = store.download("p3", 4, "auton", "override");
    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => expect(store.run("p3").download?.state).toBe("running"));
    expect(store.chain?.phase).toBe("download");
    expect(store.stoppable("p3")).toBe(true);
    store.stop("p3");
    expect(h.closed).toContain("b0000001");
    // The daemon's exit hook settles the record as vexcom killed by SIGHUP.
    Object.assign(rec, { state: "failed", exit_code: 129, ended: "x" });
    state.mockImplementation(async () => ({ ...rec }));
    await vi.advanceTimersByTimeAsync(700);
    await work;
    const r = store.run("p3");
    expect(r.downloaded).toEqual({ slot: 4, ok: false, code: 129, stopped: true });
    expect(r.note).toMatch(/^Download stopped: slot 4 may hold an incomplete program/);
    expect(r.noteTone).toBe("bad");
    // A stopped chain never opens the brain terminal again by itself (L6).
    await vi.advanceTimersByTimeAsync(5000);
    expect(h.brainOpened).toBe(0);
    expect(store.brainClosed).toMatch(/not opened again, since the download was stopped/);
    expect(h.downloads).toHaveLength(1);                 // and nothing is retried
  });

  it("a download that finished before vexcom closed is what its record says: in the slot", async () => {
    vi.useFakeTimers();
    const store = new VexStore();
    h.download = async () => ({ terminal_id: "b0000001" });
    const rec = { id: "b0000001", project_id: "p3", slot: 3, state: "running" as const, terminal_id: "b0000001",
      exit_code: null as number | null, output: "", started: "", ended: null as string | null };
    const state = vi.spyOn(api, "vexDownloadState").mockImplementation(async () => ({ ...rec }));
    const work = store.download("p3", 3, "auton", "override");
    await vi.advanceTimersByTimeAsync(0);
    await vi.waitFor(() => expect(store.chain?.phase).toBe("download"));
    store.stop("p3");
    state.mockImplementation(async () => ({ ...rec, state: "succeeded", exit_code: 0, ended: "x" }));
    await vi.advanceTimersByTimeAsync(700);
    await work;
    expect(store.run("p3").downloaded?.ok).toBe(true);
    expect(store.run("p3").note).toMatch(/^Downloaded to slot 3/);
  });

  it("pressed while the download request is on its way: vexcom is closed as soon as it is known", async () => {
    vi.useFakeTimers();
    const store = new VexStore();
    let release!: (v: { terminal_id: string }) => void;
    h.download = () => new Promise((res) => { release = res; });
    vi.spyOn(api, "vexDownloadState").mockResolvedValue({ id: "b0000007", project_id: "p3", slot: 3, state: "failed",
      terminal_id: "b0000007", exit_code: 129, output: "", started: "", ended: "x" });
    const work = store.download("p3", 3, "auton", "override");
    await vi.waitFor(() => expect(h.downloads).toHaveLength(1));
    store.stop("p3");
    expect(h.closed).toEqual([]);
    release({ terminal_id: "b0000007" });
    await vi.advanceTimersByTimeAsync(700);
    await work;
    expect(h.closed).toEqual(["b0000007"]);
    expect(store.run("p3").downloaded?.stopped).toBe(true);
  });

  it("a project's own clean or build: its terminal closed, and it is said as stopped, not failed", async () => {
    const store = new VexStore();
    let stopped = false;
    h.buildState = (id) => stopped ? { ...running(id), state: "failed", exit_code: 129 } : running(id);
    const work = store.build("p3", "build");
    await vi.waitFor(() => expect(store.run("p3").build?.terminal_id).toBe("a0000001"));
    expect(store.stoppable("p3")).toBe(true);
    expect(store.stoppable("p5")).toBe(false);           // another project's view has nothing to stop
    store.stop("p3");
    stopped = true;
    await work;
    expect(h.closed).toEqual(["a0000001"]);
    expect(store.run("p3").stoppedBuild).toBe("00000001");
  });

  it("refused under a card or after a crash, and when nothing runs", async () => {
    const store = new VexStore();
    h.buildState = (id) => running(id);
    void store.build("p3", "build");
    await vi.waitFor(() => expect(store.run("p3").build?.terminal_id).toBe("a0000001"));
    store.setBlocked(true);
    store.stop("p3");
    expect(h.closed).toEqual([]);
    store.setBlocked(false);
    store.crash();
    store.stop("p3");
    expect(h.closed).toEqual([]);
    const idle = new VexStore();
    idle.stop("p3");
    expect(idle.stoppable("p3")).toBe(false);
    expect(h.closed).toEqual([]);
  });
});

describe("edited since (VX-UI-2, advisory)", () => {
  it("only a source file's save marks the project", () => {
    const store = new VexStore();
    store.edited("p3", "notes.txt");
    expect(store.run("p3").editedAt).toBeNull();
    store.edited("p3", "include/pid.hpp");
    expect(store.run("p3").editedAt).toBeGreaterThan(0);
  });
});

describe("Download feedback uses actual action receipts", () => {
  it("retains live output despite an absent terminal listing, then settles on EOF receipt", async () => {
    vi.useFakeTimers();
    const store = new VexStore();
    h.download = async () => ({terminal_id: "b0000001"});
    vi.spyOn(terminals, "row").mockReturnValue(null as any);
    const d = {id: "b0000001", project_id: "p3", slot: 3, state: "running" as const,
      terminal_id: "b0000001", exit_code: null, output: "Downloading <b>program</b>", started: "", ended: null};
    const state = vi.spyOn(api, "vexDownloadState").mockResolvedValue(d);
    const work = store.download("p3", 3, "dummy");
    await vi.advanceTimersByTimeAsync(0);
    expect(store.run("p3").busy).toBe("download");
    expect(store.run("p3").download?.output).toContain("<b>program</b>");
    expect(store.run("p3").downloaded).toBeNull();
    state.mockResolvedValue({...d, state: "succeeded", exit_code: 0, output: "Finished"});
    await vi.advanceTimersByTimeAsync(600);
    await work;
    expect(store.run("p3").downloaded?.ok).toBe(true);
    expect(store.run("p3").download?.output).toBe("Finished");
    expect(h.downloads).toHaveLength(1);
  });

  it("failed status reads retry visibly; forgotten receipts never retry a Download", async () => {
    vi.useFakeTimers();
    const store = new VexStore();
    h.download = async () => ({terminal_id: "b0000001"});
    vi.spyOn(api, "vexDownloadState").mockRejectedValueOnce(new Error("offline"))
      .mockRejectedValueOnce(Object.assign(new Error("forgotten"), {status: 404}));
    const work = store.download("p3", 3, "dummy");
    await vi.advanceTimersByTimeAsync(0);
    expect(store.run("p3").downloadNote).toContain("Retrying");
    await vi.advanceTimersByTimeAsync(600);
    await work;
    expect(store.run("p3").downloadNote).toContain("no longer available");
    expect(store.run("p3").downloaded?.ok).toBeNull();
    expect(h.downloads).toHaveLength(1);
  });

  it("manual recheck changes an unknown result to the actual late success", async () => {
    vi.useFakeTimers();
    const store = new VexStore();
    h.download = async () => ({terminal_id: "b0000001"});
    const state = vi.spyOn(api, "vexDownloadState").mockRejectedValue(Object.assign(new Error("forgotten"), {status: 404}));
    await store.download("p3", 3, "dummy");
    expect(store.run("p3").downloaded?.ok).toBeNull();
    state.mockResolvedValue({id: "b0000001", project_id: "p3", slot: 3, state: "succeeded", terminal_id: "b0000001",
      exit_code: 0, output: "late Finished", started: "", ended: ""});
    await store.refreshDownload("p3");
    expect(store.run("p3").downloaded?.ok).toBe(true);
    expect(store.run("p3").note).toContain("Downloaded to slot 3");
    expect(store.run("p3").download?.output).toBe("late Finished");
    expect(h.downloads).toHaveLength(1);
  });

  it("a late manual status response cannot replace a newer Download", async () => {
    vi.useFakeTimers();
    const store = new VexStore();
    h.download = async () => ({terminal_id: "b0000001"});
    await store.download("p3", 3, "dummy");
    let release!: (v: any) => void;
    const state = vi.spyOn(api, "vexDownloadState").mockImplementationOnce(() => new Promise(r => {release = r;}));
    const old = store.refreshDownload("p3");
    h.download = async () => ({terminal_id: "b0000002"});
    state.mockResolvedValue({id: "b0000002", project_id: "p3", slot: 3, state: "running", terminal_id: "b0000002",
      exit_code: null, output: "new running", started: "", ended: null});
    const next = store.download("p3", 3, "dummy");
    await vi.advanceTimersByTimeAsync(0);
    release({id: "b0000001", project_id: "p3", slot: 3, state: "succeeded", terminal_id: "b0000001", exit_code: 0, output: "old", started: "", ended: ""});
    await old;
    expect(store.run("p3").download?.id).toBe("b0000002");
    expect(store.run("p3").downloaded).toBeNull();
    state.mockResolvedValue({id: "b0000002", project_id: "p3", slot: 3, state: "succeeded", terminal_id: "b0000002",
      exit_code: 0, output: "new end", started: "", ended: ""});
    await vi.advanceTimersByTimeAsync(600);
    await next;
  });

  it("late same-ID running reads cannot regress an already settled Download", async () => {
    vi.useFakeTimers();
    const store = new VexStore();
    h.download = async () => ({terminal_id: "b0000001"});
    const state = vi.spyOn(api, "vexDownloadState").mockRejectedValueOnce(Object.assign(new Error("forgotten"), {status: 404}));
    await store.download("p3", 3, "dummy");
    const d = {id: "b0000001", project_id: "p3", slot: 3, state: "running" as const, terminal_id: "b0000001",
      exit_code: null, output: "old progress", started: "", ended: null};
    let release!: (v: any) => void;
    state.mockImplementationOnce(() => new Promise(r => {release = r;}));
    const old = store.refreshDownload("p3");
    state.mockResolvedValueOnce({...d, state: "succeeded", exit_code: 0, output: "actual Finished", ended: "done"});
    await store.refreshDownload("p3");
    release(d);
    await old;
    expect(store.run("p3").download?.state).toBe("succeeded");
    expect(store.run("p3").download?.output).toBe("actual Finished");
    expect(store.run("p3").downloaded?.ok).toBe(true);
    expect(store.run("p3").downloadNote).toBe("");
  });

  it("USB demand coalesces, keeps last confirmed values on error and refuses cards/chains", async () => {
    const store = new VexStore();
    await store.refreshStatus();
    const probe = vi.spyOn(api, "vexConnection");
    await Promise.all([store.refreshConnection(), store.refreshConnection()]);
    expect(probe).toHaveBeenCalledTimes(1);
    expect(store.connection?.brain).toBe(true);
    store.setBlocked(true);
    await store.refreshConnection();
    expect(probe).toHaveBeenCalledTimes(1);
  });
});

describe("the Brain tab's Connect and Disconnect (VX-UI-4a)", () => {
  it("the pressing tab takes the place before the store names the terminal (review LOW-1)", async () => {
    const store = new VexStore();
    const seen: (string | null)[] = [];
    // The daemon's vex_device record makes a status read while Connect's answer is on its way.
    vi.spyOn(api, "vexBrain").mockImplementationOnce(async () => {
      h.brain = "c0000002";
      await store.refreshStatus();
      return { terminal_id: "c0000002", started: true };
    });
    await store.connectBrain((id) => seen.push(id, store.brainTerminal));
    expect(seen).toEqual(["c0000002", null]);
    expect(store.brainTerminal).toBe("c0000002");
  });

  it("Connect from an ended link clears the old one away, never a live one (review LOW-2)", async () => {
    const store = new VexStore();
    const gone = vi.spyOn(terminals, "gone");
    const row = vi.spyOn(terminals, "row");
    store.brainTerminal = "c0000001";
    row.mockReturnValue({ exited: true, exit_code: 3 } as any);
    await store.connectBrain();
    await vi.waitFor(() => expect(gone).toHaveBeenCalledWith("c0000001"));
    expect(h.closed).toContain("c0000001");
    expect(store.brainTerminal).toBe("c0000002");
    // The same live link coming back (started: false) is never closed.
    gone.mockClear();
    h.closed.length = 0;
    row.mockReturnValue({ exited: false } as any);
    vi.spyOn(api, "vexBrain").mockResolvedValueOnce({ terminal_id: "c0000002", started: false });
    await store.connectBrain();
    expect(gone).not.toHaveBeenCalled();
    expect(h.closed).toEqual([]);
  });

  it("Connect asks the daemon once, holds the terminal as this window's, and never opens the panel", async () => {
    const store = new VexStore();
    const adopt = vi.spyOn(terminals, "adoptProgram");
    const show = vi.spyOn(terminals, "showProgram");
    await store.connectBrain();
    expect(h.brainOpened).toBe(1);
    expect(adopt).toHaveBeenCalledWith("c0000002");
    expect(show).not.toHaveBeenCalled();
    expect(store.brainTerminal).toBe("c0000002");
    expect(store.brainBusy).toBe(false);
  });

  it("refused under a card, after a crash, during a chain and while a device read is on its way", async () => {
    const store = new VexStore();
    store.setBlocked(true);
    await store.connectBrain();
    store.setBlocked(false);
    store.infoBusy = "system";
    await store.connectBrain();
    store.infoBusy = null;
    store.chain = { seq: 1, projectId: "p3", label: "", slot: 1, phase: "build", cancelled: false,
      buildTerminal: null, downloadTerminal: null, done: [], brainPaused: false };
    await store.connectBrain();
    store.chain = null;
    store.crash();
    await store.connectBrain();
    expect(h.brainOpened).toBe(0);
  });

  it("a refusal is said in the daemon's words and leaves nothing to show", async () => {
    const store = new VexStore();
    vi.spyOn(api, "vexBrain").mockRejectedValueOnce(
      Object.assign(new Error("A download is running. One device action at a time."), { status: 409 }));
    await store.connectBrain();
    expect(store.brainNote).toBe("A download is running. One device action at a time.");
    expect(store.brainTerminal).toBeNull();
  });

  it("Disconnect closes the terminal (frees the port); a late status read never brings it back", async () => {
    const store = new VexStore();
    await store.connectBrain();
    const gone = vi.spyOn(terminals, "gone");
    const late = vi.spyOn(api, "vexStatus").mockResolvedValueOnce({ configured: true, hint: null, vexcom_version: null,
      extension_version: null, sdks: null, templates: false, project_folder: null, device: "brain", brain: "c0000002" });
    await store.disconnectBrain();
    expect(h.closed).toEqual(["c0000002"]);
    expect(gone).toHaveBeenCalledWith("c0000002");
    expect(store.brainTerminal).toBeNull();
    await vi.waitFor(() => expect(late).toHaveBeenCalled());
    expect(store.brainTerminal).toBeNull();                    // the daemon had not let go yet: still gone here
    store.setBlocked(true);
    store.brainTerminal = "c0000003";
    await store.disconnectBrain();
    expect(h.closed).toEqual(["c0000002"]);                    // nothing under a card
  });
});

describe("Connect is offered once a download ends, without a reload (the owner, live, 2026-10-17)", () => {
  // The bug: the bus's `vex_device` record at a download's start made the
  // window read `/vex/status` while the daemon held the device ("download"),
  // and nothing read it again when the download ended — so the Brain tab's
  // Connect stayed off: "A download holds the robot's port; Connect once it ends."
  const off = (store: VexStore) => brainView({
    configured: true, blocked: false, chain: store.chain ? { paused: store.chain.brainPaused } : null,
    terminal: null, asking: store.brainBusy, session: null, exitCode: null, shownIn: null,
    error: store.brainNote, closed: store.brainClosed, device: store.status?.device ?? null, reading: !!store.infoBusy,
  }).connect.off;

  /**
   * One chain against the fake daemon: the device is the download's from its
   * request until its record ends (one step on the daemon), and the bus's
   * record arrives while the request is on its way. `settleAt`: the record
   * read that sees the end (1: an instant exit). `stop`: the owner's Stop ends it.
   */
  async function chain(end: { state: DownloadView["state"]; exit_code: number }, o: { settleAt?: number; stop?: boolean; brain?: boolean } = {}) {
    vi.useFakeTimers();
    const store = new VexStore();
    if (o.brain) {
      h.brain = "c0000001";
      await store.refreshStatus();
    }
    const rec: DownloadView = { id: "b0000001", project_id: "p3", slot: 1, state: "running", terminal_id: "b0000001",
      exit_code: null, output: "", started: "", ended: null };
    const settle = () => {
      Object.assign(rec, end, { ended: "x" });
      h.device = null;                                   // with the record's end (vex.py `ended`)
    };
    h.download = async () => {
      h.device = "download";
      store.heardDevice({ action: "download", at: "2026-10-17T20:22:11" });
      return { terminal_id: "b0000001" };
    };
    let reads = 0;
    vi.spyOn(api, "vexDownloadState").mockImplementation(async () => {
      reads += 1;
      if (o.stop ? h.closed.includes("b0000001") : reads >= (o.settleAt ?? 3)) settle();
      return { ...rec };
    });
    const said = vi.spyOn(api, "vexStatus");
    const work = store.download("p3", 1, "auton", "override");
    if ((o.settleAt ?? 3) > 1) {
      await vi.waitFor(() => expect(store.run("p3").download?.state).toBe("running"));
      // While it really runs: the daemon says "download" and Connect is off.
      expect(store.status?.device).toBe("download");
      expect(off(store)).toMatch(/download holds the robot's port/);
    }
    if (o.stop) store.stop("p3");
    await vi.advanceTimersByTimeAsync(5000);
    await work;
    // The bug's premise, whatever the exit: a status read said "download".
    const answers = await Promise.all(said.mock.results.map((r) => r.value));
    expect(answers.some((a: any) => a?.device === "download")).toBe(true);
    return store;
  }

  const offered = (store: VexStore) => {
    expect(store.chain).toBeNull();
    expect(store.status?.device).toBeNull();
    expect(off(store)).toBe("");
    expect(h.brainOpened).toBe(0);                          // offered, never opened (VX-UI-4a)
    expect(h.downloads).toHaveLength(1);                    // and nothing resent
  };

  it("after a download that succeeded", async () => {
    const store = await chain({ state: "succeeded", exit_code: 0 });
    expect(store.run("p3").note).toBe("Downloaded to slot 1. Start it on the brain or the controller.");
    offered(store);
  });

  it("after a download that failed (exit 4)", async () => {
    const store = await chain({ state: "failed", exit_code: 4 });
    expect(store.run("p3").note).toMatch(/^Download failed \(exit 4\)/);
    offered(store);
  });

  it("after an instant exit 6 (the port held elsewhere), ended at its first read", async () => {
    const store = await chain({ state: "failed", exit_code: 6 }, { settleAt: 1 });
    expect(store.run("p3").note).toMatch(/^Download failed \(exit 6\)/);
    offered(store);
  });

  it("after the owner's Stop", async () => {
    const store = await chain({ state: "failed", exit_code: 129 }, { stop: true });
    expect(store.run("p3").downloaded).toMatchObject({ ok: false, stopped: true });
    offered(store);
  });

  it("after a download that closed the brain terminal: Connect is offered, the terminal never reopened", async () => {
    const store = await chain({ state: "succeeded", exit_code: 0 }, { brain: true });
    expect(store.brainClosed).toMatch(/closed for the download\. Click Connect/);
    offered(store);
  });

  it("when the record is gone (a restart): the chain's end still reads the status again", async () => {
    vi.useFakeTimers();
    const store = new VexStore();
    h.download = async () => {
      h.device = "download";
      store.heardDevice({ action: "download", at: "x" });
      return { terminal_id: "b0000001" };
    };
    vi.spyOn(api, "vexDownloadState").mockImplementation(async () => {
      h.device = null;
      throw Object.assign(new Error("download record not found"), { status: 404 });
    });
    const work = store.download("p3", 1, "auton", "override");
    await vi.advanceTimersByTimeAsync(5000);
    await work;
    expect(store.run("p3").note).toMatch(/outcome unknown/);
    offered(store);
  });

  it("a later Check status that reads the end reads the status too", async () => {
    vi.useFakeTimers();
    const store = new VexStore();
    const rec: DownloadView = { id: "b0000001", project_id: "p3", slot: 1, state: "running", terminal_id: "b0000001",
      exit_code: null, output: "", started: "", ended: null };
    h.download = async () => ({ terminal_id: "b0000001" });
    const state = vi.spyOn(api, "vexDownloadState").mockImplementation(async () => ({ ...rec }));
    const work = store.download("p3", 1, "auton", "override");
    h.device = "download";
    await vi.advanceTimersByTimeAsync(4 * 60_000 + 1000);   // the follow gives up while it still runs
    await work;
    expect(store.status?.device).toBe("download");
    h.device = null;
    state.mockImplementation(async () => ({ ...rec, state: "succeeded", exit_code: 0, ended: "x" }));
    await store.refreshDownload("p3");
    await vi.waitFor(() => expect(store.status?.device).toBeNull());
    expect(off(store)).toBe("");
  });

  it("a status read asked before the end, answering after one asked after it, never brings back \"download\"", async () => {
    const store = new VexStore();
    const answers: ((s: any) => void)[] = [];
    vi.spyOn(api, "vexStatus").mockImplementation(() => new Promise((r) => answers.push(r)));
    const said = (device: string | null) => ({ configured: true, hint: null, vexcom_version: null, extension_version: null,
      sdks: null, templates: false, project_folder: null, device, brain: null });
    const early = store.refreshStatus();                   // the bus's read, at the download's start
    const late = store.refreshStatus();                    // the read once it had ended
    answers[1](said(null));
    await late;
    answers[0](said("download"));
    await early;
    expect(store.status?.device).toBeNull();
    expect(off(store)).toBe("");
  });

  it("a superseded status read settles only after the newest, so its awaiter sees the newest answer", async () => {
    // #241 review LOW: the chain awaits refreshStatus() and then reads this.status.
    const store = new VexStore();
    const answers: ((s: any) => void)[] = [];
    vi.spyOn(api, "vexStatus").mockImplementation(() => new Promise((r) => answers.push(r)));
    const said = (device: string | null) => ({ configured: true, hint: null, vexcom_version: null, extension_version: null,
      sdks: null, templates: false, project_folder: null, device, brain: null });
    const early = store.refreshStatus();
    const late = store.refreshStatus();
    let seen: string | null | undefined = undefined;
    const awaiter = early.then(() => { seen = store.status?.device ?? null; });
    answers[0](said("download"));                          // the older answer comes first
    await new Promise((r) => setTimeout(r, 0));
    expect(seen).toBeUndefined();                          // still waiting for the newest
    answers[1](said(null));
    await awaiter;
    await late;
    expect(seen).toBeNull();
    expect(store.status?.device).toBeNull();
  });

  it("an older failing status read after a newer success never overwrites the error state", async () => {
    // #241 review LOW (its mutant M5: no guard in the error handler).
    const store = new VexStore();
    const answers: { ok: (s: any) => void; fail: (e: any) => void }[] = [];
    vi.spyOn(api, "vexStatus").mockImplementation(() => new Promise((ok, fail) => answers.push({ ok, fail })));
    const early = store.refreshStatus();
    const late = store.refreshStatus();
    answers[1].ok({ configured: true, hint: null, vexcom_version: null, extension_version: null,
      sdks: null, templates: false, project_folder: null, device: null, brain: null });
    await late;
    answers[0].fail(new Error("connection refused"));
    await early;
    expect(store.statusError).toBe("");
    expect(store.status?.device).toBeNull();
  });

  it("a device read's end reads the status again (the same stale \"info\")", async () => {
    const store = new VexStore();
    h.device = "info";
    await store.refreshStatus();
    expect(off(store)).toMatch(/device read holds the robot's port/);
    await store.readInfo("system");
    await vi.waitFor(() => expect(store.status?.device).toBeNull());
    expect(off(store)).toBe("");
  });
});
