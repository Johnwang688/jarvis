// The window. One SSE subscription writes lifecycle state; every panel reads
// the store. The orb, the capture pipeline and the wake word live here because
// they are properties of the *window*, not of whichever tab is showing.

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, subscribe } from "./api";
import { useStore, currentProject, currentTask, type Tab } from "./state/store";
import { Sidebar } from "./components/Sidebar";
import { ChatTab } from "./components/ChatTab";
import { InputBar } from "./components/InputBar";
import { TaskView } from "./components/TaskView";
import { FileTab } from "./components/FileTab";
import { DiffTab } from "./components/DiffTab";
import { PreviewTab } from "./components/PreviewTab";
import { ApprovalQueue, ApprovalVeil } from "./components/Approvals";
import { RoutePanel, SchedulesPanel, UsagePanel } from "./components/Panels";
import { AvatarPicker, ModelPicker, NewProject, NewTask, VoicePicker } from "./components/Pickers";
import { Orb } from "./components/Orb";
import { Capture } from "./lib/capture";
import { HINTS, isMuted, loadMode, outcomeFor, saveMode, type DictationMode } from "./lib/dictation";
import { WakeGate, compileWake, matchesWake, WAKE_PATTERNS } from "./lib/wake";
import type { Attachment, AvatarDesc, ModelRow, VoiceEntry } from "./types";

const TABS: Tab[] = ["chat", "task", "file", "diff", "preview"];
const PROPOSAL_WINDOW_MS = 60_000;

export default function App() {
  const { state, dispatch } = useStore();
  const [avatars, setAvatars] = useState<AvatarDesc[]>([]);
  const [models, setModels] = useState<ModelRow[]>([]);
  const [selectedModel, setSelectedModel] = useState<string | null>(null);
  const [voices, setVoices] = useState<VoiceEntry[]>([]);
  const [voiceOverride, setVoiceOverride] = useState("");
  const [avatarCacheBust, setAvatarCacheBust] = useState(0);

  // `live` gives the capture callbacks — which run on the audio thread's
  // cadence, not React's — the current state without stale closures.
  const live = useRef(state);
  live.current = state;

  const patch = useCallback((p: Parameters<typeof dispatch>[0] extends any ? any : never) => {
    dispatch({ type: "patch", patch: p });
  }, [dispatch]);

  // ---- capture ------------------------------------------------------------

  const capture = useMemo(
    () =>
      new Capture({
        // He does not answer himself, and never talks over a pending
        // authorization or an open picker.
        suppressed: () =>
          live.current.orb === "speaking" ||
          live.current.busy ||
          live.current.approvals.length > 0 ||
          live.current.picker !== null,
        muted: () => isMuted(live.current.dictation),
        onLevel: (level) => dispatch({ type: "patch", patch: { level } }),
        onListening: () =>
          dispatch({ type: "patch", patch: { orb: "listening", status: "LISTENING · SPEAK NOW" } }),
        onIdle: (note) => dispatch({ type: "patch", patch: { orb: "idle", status: note || "" } }),
        onUtterance: (wav) => void onUtterance(wav),
      }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dispatch],
  );

  const onUtterance = useCallback(
    async (wav: Blob) => {
      const mode = live.current.dictation;
      const what = outcomeFor(mode);
      // OFF never reaches here (Capture refuses to claim while muted), but the
      // mode is checked again rather than assumed: the fail-closed direction
      // costs a discarded utterance and the other costs an upload.
      if (what === "discard") return;
      dispatch({ type: "patch", patch: { orb: "transcribing", status: "TRANSCRIBING" } });
      let text = "";
      try {
        text = await api.stt(wav);
      } catch (e: any) {
        dispatch({ type: "patch", patch: { orb: "error", status: "STT FAILED", error: e.message } });
        return;
      }
      if (!text.trim()) {
        dispatch({ type: "patch", patch: { orb: "idle", status: "DIDN'T CATCH THAT" } });
        return;
      }
      if (what === "review") {
        // REVIEW never sends on its own: the transcript lands in the box.
        dispatch({ type: "patch", patch: { orb: "idle", status: "", pendingTranscript: text } });
        return;
      }
      await send(text, []);
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [dispatch],
  );

  // ---- sending ------------------------------------------------------------

  const send = useCallback(
    async (text: string, attachments: Attachment[]) => {
      const threadId = live.current.threadId;
      if (!threadId) {
        dispatch({ type: "patch", patch: { error: "No thread selected." } });
        return;
      }
      dispatch({ type: "message", message: { role: "user", text } });
      dispatch({ type: "patch", patch: { busy: true, orb: "thinking", status: "THINKING", draft: "" } });
      try {
        await api.send(threadId, { text, attachments: attachments.length ? attachments : undefined });
      } catch (e: any) {
        dispatch({ type: "patch", patch: { busy: false, orb: "error", error: e.message } });
      }
    },
    [dispatch],
  );

  // ---- events -------------------------------------------------------------

  useEffect(() => {
    const stop = subscribe((e: any) => {
      const kind = e.kind;
      const data = e.data || e;
      const mine = !e.thread_id || e.thread_id === live.current.threadId;
      switch (kind) {
        case "turn_started":
          if (mine) dispatch({ type: "patch", patch: { busy: true, orb: "thinking", draft: "" } });
          break;
        case "text_delta":
          if (mine) dispatch({ type: "delta", text: data.text || "" });
          break;
        case "text":
          if (mine) dispatch({ type: "settle", text: data.text || "" });
          break;
        case "tool_started":
          if (mine)
            dispatch({
              type: "op_start",
              op: { call_id: data.call_id || String(Date.now()), name: data.name || "tool", started: Date.now() },
            });
          break;
        case "tool_finished":
          if (mine)
            dispatch({
              type: "op_done",
              call_id: data.call_id || "",
              ok: data.ok !== false,
              summary: data.summary || "",
            });
          break;
        case "turn_finished":
          if (mine)
            dispatch({ type: "patch", patch: { busy: false, orb: "idle", status: "" } });
          capture.openFollowUp();
          break;
        case "error":
          if (mine)
            dispatch({ type: "patch", patch: { busy: false, orb: "error", error: data.message || "error" } });
          break;
        case "approval_requested":
          dispatch({ type: "approval_add", request: { ...data, req_id: data.req_id } });
          break;
        case "approval_resolved":
          dispatch({ type: "approval_drop", req_id: data.req_id });
          break;
        case "proposal_reply":
          // A proposal is a system line with a 60-second cancel: the fast path
          // has already answered and a task is about to run.
          dispatch({
            type: "message",
            message: {
              role: "system",
              text: data.reply || "",
              proposal: data.task_id
                ? { task_id: data.task_id, until: Date.now() + PROPOSAL_WINDOW_MS }
                : undefined,
            },
          });
          void refreshTasks();
          break;
        case "task_created":
        case "task_status_changed":
        case "task_question":
          void refreshTasks();
          break;
        case "thread_opened":
        case "thread_closed":
          void refreshThreads();
          break;
        case "usage_updated":
          api.usage().then((usage) => dispatch({ type: "patch", patch: { usage } })).catch(() => {});
          break;
        case "schedule_fired":
          api.schedules().then((schedules) => dispatch({ type: "patch", patch: { schedules } })).catch(() => {});
          break;
        case "avatar":
          void loadAvatar();
          break;
        case "model":
          void loadModels();
          break;
        case "voice":
          void loadVoices();
          break;
        case "mute":
          break;
        default:
          break;
      }
    });
    return stop;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dispatch]);

  // ---- loading ------------------------------------------------------------

  const refreshTasks = useCallback(async () => {
    try {
      const tasks = await api.tasks();
      dispatch({ type: "patch", patch: { tasks } });
      const id = live.current.taskId;
      if (id) {
        const threads = await api.taskThreads(id).catch(() => []);
        dispatch({ type: "patch", patch: { taskThreads: { ...live.current.taskThreads, [id]: threads } } });
      }
    } catch {
      /* the window keeps what it had rather than blanking */
    }
  }, [dispatch]);

  const refreshThreads = useCallback(async () => {
    try {
      dispatch({ type: "patch", patch: { threads: await api.threads() } });
    } catch {
      /* keep what we had */
    }
  }, [dispatch]);

  const loadAvatar = useCallback(async () => {
    try {
      const r = await api.avatars();
      setAvatars(r.avatars || []);
      const active = (r.avatars || []).find((a) => a.slug === r.active) || null;
      setAvatarCacheBust(Date.now());
      dispatch({
        type: "patch",
        patch: {
          avatar: active,
          // Every degradation path leaves him summonable: with no avatar, or
          // an avatar with no stated phrase, the built-in still fires.
          wakePatterns: compileWake(active?.wake_source || active?.wake) || WAKE_PATTERNS,
        },
      });
    } catch {
      dispatch({ type: "patch", patch: { wakePatterns: WAKE_PATTERNS.slice() } });
    }
  }, [dispatch]);

  const loadModels = useCallback(async () => {
    try {
      const r = await api.models();
      setModels(r.models || []);
      setSelectedModel(r.selected ?? null);
    } catch {
      /* the picker just has nothing to show */
    }
  }, []);

  const loadVoices = useCallback(async () => {
    try {
      const r = await api.voices();
      setVoices(r.voices || []);
      setVoiceOverride(r.override || "");
    } catch {
      /* ditto */
    }
  }, []);

  useEffect(() => {
    dispatch({ type: "patch", patch: { dictation: loadMode() } });
    (async () => {
      const [projects, threads, tasks] = await Promise.all([
        api.projects().catch(() => []),
        api.threads().catch(() => []),
        api.tasks().catch(() => []),
      ]);
      const platforms: Record<string, any> = {};
      await Promise.all(
        projects.map(async (p) => {
          platforms[p.id] = await api.platform(p.id).catch(() => ({ platform: "wsl", note: null }));
        }),
      );
      dispatch({
        type: "patch",
        patch: {
          projects, threads, tasks, platforms,
          projectId: projects[0]?.id ?? null,
          threadId: threads.find((t) => !t.task_id)?.id ?? null,
        },
      });
      const [usage, schedules, route, approvals] = await Promise.all([
        api.usage().catch(() => null),
        api.schedules().catch(() => []),
        api.route().catch(() => null),
        api.approvals().catch(() => []),
      ]);
      dispatch({ type: "patch", patch: { usage, schedules, route, approvals } });
      await Promise.all([loadAvatar(), loadModels(), loadVoices()]);
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // A thread's transcript is loaded when it becomes the live one, so switching
  // redraws from the saved conversation rather than from whatever is on screen.
  useEffect(() => {
    if (!state.threadId) return;
    api
      .transcript(state.threadId)
      .then((r) => dispatch({ type: "patch", patch: { messages: r.messages || [], draft: "", ops: [] } }))
      .catch(() => dispatch({ type: "patch", patch: { messages: [], draft: "", ops: [] } }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.threadId]);

  useEffect(() => {
    if (!state.taskId) return;
    api
      .taskThreads(state.taskId)
      .then((threads) =>
        dispatch({ type: "patch", patch: { taskThreads: { ...live.current.taskThreads, [state.taskId!]: threads } } }),
      )
      .catch(() => {});
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.taskId]);

  // ---- wake word ----------------------------------------------------------

  useEffect(() => {
    const SR: any = (window as any).SpeechRecognition || (window as any).webkitSpeechRecognition;
    if (!SR) return;
    if (isMuted(state.dictation)) return; // a muted mic stops the recognizer outright
    const gate = new WakeGate();
    let recog: any = new SR();
    let dead = false;
    recog.continuous = true;
    recog.interimResults = true;
    recog.lang = "en-US";
    recog.onstart = () => gate.restart();
    recog.onresult = (e: any) => {
      let text = "";
      // Concatenated from resultIndex: a two-word phrase can straddle a
      // segment boundary, and matching the pieces separately would miss it.
      for (let i = e.resultIndex; i < e.results.length; i++) text += e.results[i][0].transcript;
      const patterns = live.current.wakePatterns.length ? live.current.wakePatterns : WAKE_PATTERNS;
      if (!matchesWake(text, patterns)) return;
      if (!gate.shouldFire(e.results.length - 1, performance.now())) return;
      onWakeHit();
    };
    recog.onend = () => {
      if (dead) return;
      setTimeout(() => {
        gate.restart();
        try {
          recog.start();
        } catch {
          /* already running */
        }
      }, 300);
    };
    try {
      recog.start();
    } catch {
      /* already running */
    }
    (window as any).__hudRecog = recog;
    return () => {
      dead = true;
      try {
        recog.onend = null;
        recog.stop();
      } catch {
        /* nothing to stop */
      }
      recog = null;
      // The handle has to go with it: a stopped recognizer that still answers
      // "yes, I am here" is exactly the kind of state a surface confabulates
      // around — and here it would say the mic is still streaming to Google's
      // speech service when it is not.
      delete (window as any).__hudRecog;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [state.dictation]);

  const onWakeHit = useCallback(() => {
    if (live.current.approvals.length) return; // never talk over a pending authorization
    if (live.current.picker) return;
    capture.onWake();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [capture]);

  // ---- push to talk -------------------------------------------------------

  const press = useCallback(() => {
    if (live.current.approvals.length) return; // answer the authorization first
    if (live.current.picker) return;
    // The interrupt half comes first: muting yourself must not take away the
    // orb as the way to shut him up.
    if (live.current.busy && live.current.threadId) api.interrupt(live.current.threadId).catch(() => {});
    if (isMuted(live.current.dictation)) {
      dispatch({ type: "patch", patch: { status: "MIC MUTED" } });
      return;
    }
    capture.press();
  }, [capture, dispatch]);

  const release = useCallback(() => capture.release(), [capture]);

  useEffect(() => {
    const down = (e: KeyboardEvent) => {
      if (e.code === "Space" && !e.repeat) {
        e.preventDefault();
        press();
      }
    };
    const up = (e: KeyboardEvent) => {
      if (e.code === "Space") {
        e.preventDefault();
        release();
      }
    };
    document.addEventListener("keydown", down);
    document.addEventListener("keyup", up);
    return () => {
      document.removeEventListener("keydown", down);
      document.removeEventListener("keyup", up);
    };
  }, [press, release]);

  // ---- microphone ---------------------------------------------------------

  useEffect(() => {
    let ctx: AudioContext | null = null;
    let stream: MediaStream | null = null;
    (async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        ctx = new AudioContext();
        const ratio = ctx.sampleRate / 16000;
        const src = ctx.createMediaStreamSource(stream);
        const node = ctx.createScriptProcessor(2048, 1, 1);
        let acc = 0, n = 0, pos = 0;
        node.onaudioprocess = (e) => {
          const frame = e.inputBuffer.getChannelData(0);
          const out = new Float32Array(Math.ceil(frame.length / ratio) + 1);
          let k = 0;
          for (let i = 0; i < frame.length; i++) {
            acc += frame[i];
            n++;
            pos += 1;
            if (pos >= ratio) {
              out[k++] = acc / n;
              pos -= ratio;
              acc = 0;
              n = 0;
            }
          }
          capture.onFrame(out.subarray(0, k));
        };
        src.connect(node);
        const mute = ctx.createGain();
        mute.gain.value = 0;
        node.connect(mute).connect(ctx.destination);
      } catch {
        // A window without a mic grant still types; every other affordance
        // stays exactly as it was.
      }
    })();
    return () => {
      stream?.getTracks().forEach((t) => t.stop());
      ctx?.close().catch(() => {});
    };
  }, [capture]);

  // ---- test hooks ---------------------------------------------------------

  useEffect(() => {
    (window as any).__hud = {
      capture,
      mic: {
        feedMs: (ms: number, amp: number, frame?: number) => capture.feedMs(ms, amp, frame),
        wake: () => onWakeHit(),
        state: () => ({
          open: capture.vad.open,
          thresh: capture.vad.thresh,
          claimed: capture.segClaimed,
          written: capture.ring.written,
        }),
      },
      state: () => live.current,
      dispatch,
    };
  }, [capture, dispatch, onWakeHit]);

  // ---- actions ------------------------------------------------------------

  const setMode = (mode: DictationMode) => {
    const wasMuted = isMuted(live.current.dictation);
    saveMode(mode);
    dispatch({ type: "patch", patch: { dictation: mode, status: "" } });
    if (wasMuted && !isMuted(mode)) capture.markUnmute();
    if (isMuted(mode)) capture.closeFollowUp();
    api.mute(isMuted(mode)).catch(() => {});
  };

  const decide = async (reqId: string, allow: boolean, always = false) => {
    try {
      await api.resolveApproval(reqId, { decision: allow ? "allow" : "deny", always });
    } catch {
      /* the broker may have timed out already; the card goes either way */
    }
    dispatch({ type: "approval_drop", req_id: reqId });
  };

  const project = currentProject(state);
  const task = currentTask(state);
  const hint =
    state.status ||
    (state.approvals.length ? "ANSWER THE AUTHORIZATION" : HINTS[state.dictation]);

  return (
    <>
      <div id="shell">
        <Sidebar
          projects={state.projects}
          platforms={state.platforms}
          threads={state.threads}
          tasks={state.tasks}
          taskThreads={state.taskThreads}
          projectId={state.projectId}
          threadId={state.threadId}
          taskId={state.taskId}
          onPickProject={(id) => patch({ projectId: id })}
          onPickThread={(id) => patch({ threadId: id, tab: "chat" })}
          onPickTask={(id) => patch({ taskId: id, tab: "task" })}
          onNewProject={() => patch({ picker: "newProject" })}
          onNewThread={async () => {
            if (!state.projectId) return;
            const t = await api.openThread({ project_id: state.projectId, role: "chat" }).catch(() => null);
            if (t) {
              await refreshThreads();
              patch({ threadId: t.id, tab: "chat" });
            }
          }}
          onNewTask={() => patch({ picker: "newTask" })}
          onOpen={(what) => patch({ tab: what === "schedules" ? state.tab : state.tab, picker: what === "route" ? "route" : state.picker })}
        />

        <div className="pane" id="main">
          <div id="tabs">
            {TABS.map((t) => (
              <button
                type="button"
                key={t}
                data-testid={`tab-${t}`}
                className={state.tab === t ? "on" : ""}
                onClick={() => patch({ tab: t })}
              >
                {t}
              </button>
            ))}
            <div style={{ marginLeft: "auto", display: "flex", gap: 6, alignItems: "center", paddingRight: 8 }}>
              {project ? (
                <select
                  data-testid="profile"
                  style={{ width: 110 }}
                  value={project.profile}
                  onChange={(e) =>
                    api
                      .patchProject(project.id, { profile: e.target.value })
                      .then(() => api.projects())
                      .then((projects) => patch({ projects }))
                      .catch(() => {})
                  }
                >
                  <option value="auto">auto</option>
                  <option value="ask">ask</option>
                  <option value="strict">strict</option>
                </select>
              ) : null}
              <button type="button" data-testid="open-model" onClick={() => patch({ picker: "model" })}>
                Model
              </button>
              <button type="button" data-testid="open-voice" onClick={() => patch({ picker: "voice" })}>
                Voice
              </button>
              <button type="button" data-testid="open-avatar" onClick={() => patch({ picker: "avatar" })}>
                Avatar
              </button>
            </div>
          </div>

          {state.tab === "chat" ? (
            <>
              <ChatTab
                messages={state.messages}
                draft={state.draft}
                ops={state.ops}
                onCancelTask={(id) => api.cancelTask(id).then(refreshTasks).catch(() => {})}
              />
              <InputBar
                mode={state.dictation}
                level={state.level}
                hint={hint}
                pendingTranscript={state.pendingTranscript}
                disabled={state.approvals.length > 0}
                onModeChange={setMode}
                onSend={(text, files) => void send(text, files)}
                onTranscriptTaken={() => patch({ pendingTranscript: "" })}
              />
            </>
          ) : null}
          {state.tab === "task" ? (
            <div className="scroll">
              <TaskView
                task={task}
                threads={state.taskId ? state.taskThreads[state.taskId] || [] : []}
                onAnswer={(question, answer) =>
                  task && api.answerTask(task.id, { question, answer }).then(refreshTasks).catch(() => {})
                }
                onSteer={(text) => task && api.steerTask(task.id, text).catch(() => {})}
                onCancel={() => task && api.cancelTask(task.id).then(refreshTasks).catch(() => {})}
                onResume={() => task && api.resumeTask(task.id).then(refreshTasks).catch(() => {})}
                onStart={() => task && api.startTask(task.id).then(refreshTasks).catch(() => {})}
              />
            </div>
          ) : null}
          {state.tab === "file" ? <FileTab projectId={state.projectId} /> : null}
          {state.tab === "diff" ? <DiffTab taskId={state.taskId} /> : null}
          {state.tab === "preview" ? <PreviewTab projectId={state.projectId} /> : null}
        </div>

        <div className="pane" id="right">
          <h2 className="bar">{task ? "Task" : "Status"}</h2>
          <div className="scroll">
            <ApprovalQueue requests={state.approvals} />
            <TaskView
              task={task}
              threads={state.taskId ? state.taskThreads[state.taskId] || [] : []}
              onAnswer={(question, answer) =>
                task && api.answerTask(task.id, { question, answer }).then(refreshTasks).catch(() => {})
              }
              onSteer={(text) => task && api.steerTask(task.id, text).catch(() => {})}
              onCancel={() => task && api.cancelTask(task.id).then(refreshTasks).catch(() => {})}
              onResume={() => task && api.resumeTask(task.id).then(refreshTasks).catch(() => {})}
              onStart={() => task && api.startTask(task.id).then(refreshTasks).catch(() => {})}
            />
            <UsagePanel usage={state.usage} />
            <SchedulesPanel
              schedules={state.schedules}
              projects={state.projects}
              onCreate={(b) => api.createSchedule(b).then(() => api.schedules()).then((s) => patch({ schedules: s })).catch(() => {})}
              onToggle={(id, enabled) =>
                api.patchSchedule(id, { enabled }).then(() => api.schedules()).then((s) => patch({ schedules: s })).catch(() => {})
              }
              onRunNow={(id) => api.runSchedule(id).catch(() => {})}
              onDelete={(id) =>
                api.deleteSchedule(id).then(() => api.schedules()).then((s) => patch({ schedules: s })).catch(() => {})
              }
            />
            <RoutePanel route={state.route} />
            {state.error ? <div className="block err" data-testid="error">{state.error}</div> : null}
          </div>
        </div>
      </div>

      <Orb
        state={state.orb}
        level={state.level}
        rings={state.avatar?.rings}
        accent={state.avatar?.accent}
        avatarUrl={state.avatar ? `/avatar.svg?v=${avatarCacheBust}` : null}
        status={state.orb === "idle" ? "" : state.orb}
        onPress={press}
        onRelease={release}
      />

      <ApprovalVeil requests={state.approvals} onDecide={decide} />

      {state.picker === "model" ? (
        <ModelPicker
          models={models}
          selected={selectedModel}
          route={state.route}
          onPick={(id, effort) => {
            api.setModel(id, effort).then(loadModels).catch(() => {});
          }}
          onClose={() => patch({ picker: null })}
        />
      ) : null}
      {state.picker === "voice" ? (
        <VoicePicker
          voices={voices}
          override={voiceOverride}
          onPick={(name) => api.setVoice(name).then(loadVoices).catch(() => {})}
          onClose={() => patch({ picker: null })}
        />
      ) : null}
      {state.picker === "avatar" ? (
        <AvatarPicker
          avatars={avatars}
          active={state.avatar?.slug || ""}
          onPick={(slug) => api.setAvatar(slug).then(loadAvatar).catch(() => {})}
          onClose={() => patch({ picker: null })}
        />
      ) : null}
      {state.picker === "route" ? (
        <div className="pickerveil" data-testid="picker" onClick={() => patch({ picker: null })}>
          <div className="picker" onClick={(e) => e.stopPropagation()}>
            <h3>Routing</h3>
            <div className="rows">
              <RoutePanel route={state.route} />
            </div>
            <div className="foot">
              <button type="button" data-testid="picker-close" onClick={() => patch({ picker: null })}>Close</button>
            </div>
          </div>
        </div>
      ) : null}
      {state.picker === "newProject" ? (
        <NewProject
          onCreate={(b) =>
            api
              .createProject(b)
              .then(() => api.projects())
              .then((projects) => patch({ projects, picker: null }))
              .catch((e) => patch({ error: e.message }))
          }
          onClose={() => patch({ picker: null })}
        />
      ) : null}
      {state.picker === "newTask" ? (
        <NewTask
          onCreate={(brief) => {
            if (!state.projectId) return;
            api
              .createTask({ project_id: state.projectId, brief })
              .then((t) => {
                patch({ picker: null, taskId: t.id, tab: "task" });
                return refreshTasks();
              })
              .catch((e) => patch({ error: e.message }));
          }}
          onClose={() => patch({ picker: null })}
        />
      ) : null}
    </>
  );
}
