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
  brainOpened: 0,
}));

vi.mock("../api", () => ({
  api: {
    vexStatus: async () => ({ configured: true, hint: null, vexcom_version: null, extension_version: null,
                              sdks: null, templates: false, device: null, brain: h.brain }),
    vexDownload: (pid: string, body: { slot: number; name: string }) => {
      h.downloads.push({ pid, ...body });
      return h.download!(pid).then(r => ({...r, download_id: r.terminal_id}));
    },
    vexDownloadState: async (id: string) => ({id, project_id: h.downloads.at(-1)?.pid ?? "p3", slot: 3,
      state: "succeeded", terminal_id: id, exit_code: 0, output: "Finished", started: "", ended: ""}),
    vexConnection: async () => ({brain: true, controller: false, state: "ready", checked_at: "", message: "", events: []}),
    vexBuild: async (pid: string, action: string) => {
      h.builds.push({ pid, action });
      return { build_id: `0000000${h.builds.length}`, terminal_id: `a000000${h.builds.length}` };
    },
    vexBuildState: async (id: string) => ({
      id, project_id: "p3", action: "build", state: "succeeded", terminal_id: null, exit_code: 0, errors: [],
      bin: null, started: "", ended: "",
    }),
    closeTerminal: async (id: string) => {
      if (id === h.brain) h.brain = null;
      return { ok: true };
    },
    vexBrain: async () => {
      h.brainOpened += 1;
      h.brain = "c0000002";
      return { terminal_id: "c0000002", started: true };
    },
    vexProject: async () => ({}),
  },
}));
vi.mock("../components/Terminal", () => ({
  terminals: {
    showProgram: async () => {},
    gone: () => {},
    refresh: async () => {},
    row: () => ({ exited: true, exit_code: 0 }),
    sessions: new Map(),
  },
}));

import { VexStore } from "./vex";
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
  h.brainOpened = 0;
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

describe("Build first (delta review MED-1): a build, then one rebuild, never a third", () => {
  it("a build that leaves it stale is followed by one rebuild, then the download", async () => {
    const store = new VexStore();
    const answers = [buildFirst(), buildFirst(), null];
    h.download = async () => {
      const a = answers.shift();
      if (a) throw a;
      return { terminal_id: "b0000009" };
    };
    await store.download("p3", 1, "override", "override");
    expect(h.builds.map((b) => b.action)).toEqual(["build", "rebuild"]);
    expect(h.downloads).toHaveLength(3);
    expect(store.run("p3").downloaded?.ok).toBe(true);
  });

  it("still stale after the rebuild: stops, says the daemon's sentence, sends nothing more", async () => {
    const store = new VexStore();
    h.download = async () => {
      throw buildFirst();
    };
    await store.download("p3", 1, "override", "override");
    expect(h.builds.map((b) => b.action)).toEqual(["build", "rebuild"]);
    expect(h.downloads).toHaveLength(3);
    expect(store.run("p3").note).toMatch(/after a build and a rebuild/);
    expect(store.run("p3").note).toContain("Build first, then download");
    expect(store.chain).toBeNull();
  });

  it("a refusal without the code builds nothing", async () => {
    const store = new VexStore();
    h.download = async () => {
      throw Object.assign(new Error("A download is running. One device action at a time."), {
        status: 409, body: { error: "A download is running. One device action at a time." },
      });
    };
    await store.download("p3", 1, "override", "override");
    expect(h.builds).toEqual([]);
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
    expect(h.builds).toEqual([]);
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
    expect(store.brainNote).toMatch(/closed for the download/);
  });

  it("with no card, the brain terminal is opened again after the download", async () => {
    const store = new VexStore();
    h.brain = "c0000001";
    h.download = async () => ({ terminal_id: "b0000004" });
    await store.download("p3", 1, "override", "override");
    await vi.waitFor(() => expect(h.brainOpened).toBe(1));
  });

  it("a card is a card", async () => {
    const store = new VexStore();
    h.download = async () => {
      store.setBlocked(true);
      store.setBlocked(false);
      throw buildFirst();
    };
    await store.download("p3", 1, "override", "override");
    expect(h.builds).toEqual([]);
    expect(store.run("p3").note).toMatch(/approval card came up before it built/);
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
