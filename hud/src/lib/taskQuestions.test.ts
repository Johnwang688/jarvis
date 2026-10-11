// The daemon's rule for which questions a task is waiting on, and the index
// `/answer` takes (`commands.open_questions`, `control.answer_refusal`).

import { openQuestions } from "./taskQuestions";
import type { OpenQuestion, Task } from "../types";

const q = (text: string, blocking: boolean, answer: string | null = null): OpenQuestion =>
  ({ text, blocking, options: [], answer, assumed: blocking ? null : "assumed" });

const task = (state: Task["state"], questions: OpenQuestion[]): Task =>
  ({ id: "k1", state, spec: { goal: "", deliverable: "", acceptance: [], constraints: [], questions } } as unknown as Task);

describe("openQuestions", () => {
  it("numbers a question by its place in spec.questions, never among the open ones", () => {
    const t = task("clarifying", [
      q("python version?", false),
      q("main or a branch?", true),
      q("already answered?", true, "yes"),
      q("which runner?", true),
    ]);
    expect(openQuestions(t).map((a) => [a.index, a.question.text])).toEqual([
      [1, "main or a branch?"],
      [3, "which runner?"],
    ]);
  });

  it("only a CLARIFYING task is waiting on a question", () => {
    const qs = [q("main or a branch?", true)];
    for (const state of ["intake", "planned", "running", "verifying", "done", "blocked", "failed", "cancelled"] as const) {
      expect(openQuestions(task(state, qs))).toEqual([]);
    }
    expect(openQuestions(task("clarifying", qs))).toHaveLength(1);
  });

  it("an empty-string answer is still an answer, as the daemon's `answer is None` reads it", () => {
    expect(openQuestions(task("clarifying", [q("x?", true, "")]))).toEqual([]);
  });

  it("no task, no spec or no questions is nothing to answer", () => {
    expect(openQuestions(null)).toEqual([]);
    expect(openQuestions({ id: "k1", state: "clarifying" } as unknown as Task)).toEqual([]);
    expect(openQuestions(task("clarifying", []))).toEqual([]);
  });
});
