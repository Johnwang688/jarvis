// The task view's answer boxes: the index each one sends, and what a refusal
// leaves on screen. The browser suite (`tests/face/hud_v2_task_check.py`)
// drives the same against the mock daemon, whose body check is the daemon's.

import { act, createElement } from "react";
import { createRoot, type Root } from "react-dom/client";
import { TaskView } from "./TaskView";
import type { OpenQuestion, Task } from "../types";

(globalThis as any).IS_REACT_ACT_ENVIRONMENT = true;

const q = (text: string, blocking: boolean, answer: string | null = null): OpenQuestion =>
  ({ text, blocking, options: [], answer, assumed: blocking ? null : "3.14" });

const task = (state: Task["state"]): Task => ({
  id: "k1", project_id: "p1", brief: "b", state, created: "", updated: "",
  spec: {
    goal: "g", deliverable: "d", acceptance: [], constraints: [],
    questions: [q("python version?", false), q("main or a branch?", true), q("which runner?", true)],
  },
  plan: [],
  status: { phase: state, step: 0, steps: 0, elapsed_s: 0, cost_usd: 0, routing: [] } as unknown as Task["status"],
  report: null, thread_ids: [], worktree: null, branch: null, profile: null, ceilings: {},
});

describe("TaskView answers", () => {
  let host: HTMLDivElement;
  let root: Root;
  let sent: [string, number, string][];
  let answer: (taskId: string, index: number, text: string) => Promise<unknown>;

  const render = (t: Task) =>
    act(() => {
      root.render(createElement(TaskView, {
        task: t, threads: [],
        onAnswer: (id: string, index: number, text: string) => answer(id, index, text),
        onSteer: () => {}, onCancel: () => {}, onResume: () => {}, onStart: () => {},
      }));
    });
  const $ = (id: string) => host.querySelector<HTMLElement>(`[data-testid="${id}"]`);
  const type = (id: string, text: string) =>
    act(() => {
      const input = $(id) as HTMLInputElement;
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!;
      setter.call(input, text);
      input.dispatchEvent(new Event("input", { bubbles: true }));
    });
  const click = async (id: string) => {
    await act(async () => {
      $(id)!.click();
    });
  };

  beforeEach(() => {
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    sent = [];
  });
  afterEach(() => {
    act(() => root.unmount());
    host.remove();
  });

  it("each box sends its question's index in spec.questions, and its words", async () => {
    answer = async (id, index, text) => { sent.push([id, index, text]); };
    render(task("clarifying"));
    expect($("answer-0")).toBeNull();                 // assumable: not asked
    expect($("answer-1")).not.toBeNull();
    expect($("answer-2")).not.toBeNull();
    type("answer-2", "  unittest  ");
    await click("answer-send-2");
    expect(sent).toEqual([["k1", 2, "unittest"]]);
    expect(($("answer-2") as HTMLInputElement).value).toBe("");   // taken: the box is clear
    expect($("answer-error-2")).toBeNull();
  });

  it("a refusal shows the daemon's sentence under the box and keeps the words", async () => {
    answer = async () => { throw new Error("Task k1 is cancelled; it is not waiting on a question."); };
    render(task("clarifying"));
    type("answer-1", "a branch");
    await click("answer-send-1");
    expect($("answer-error-1")?.textContent).toBe(
      "Not answered: Task k1 is cancelled; it is not waiting on a question.");
    expect(($("answer-1") as HTMLInputElement).value).toBe("a branch");
    expect(($("answer-send-1") as HTMLButtonElement).disabled).toBe(false);   // can try again
  });

  it("a second try that is taken clears the sentence", async () => {
    let refuse = true;
    answer = async () => { if (refuse) throw new Error("no runner"); };
    render(task("clarifying"));
    type("answer-1", "a branch");
    await click("answer-send-1");
    expect($("answer-error-1")).not.toBeNull();
    refuse = false;
    await click("answer-send-1");
    expect($("answer-error-1")).toBeNull();
  });

  it("Enter answers too; an empty box sends nothing", async () => {
    answer = async (id, index, text) => { sent.push([id, index, text]); };
    render(task("clarifying"));
    await click("answer-send-1");
    expect(sent).toEqual([]);
    type("answer-1", "main");
    await act(async () => {
      $("answer-1")!.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    });
    expect(sent).toEqual([["k1", 1, "main"]]);
  });

  it("no box for a task that is not clarifying: the daemon would refuse it", () => {
    answer = async () => {};
    render(task("running"));
    expect($("blocking-questions")).toBeNull();
  });
});
