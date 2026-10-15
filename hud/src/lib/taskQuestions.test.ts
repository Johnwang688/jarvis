// The daemon's rule for which questions a task is waiting on, and the index
// `/answer` takes (`commands.open_questions`, `control.answer_refusal`).

import { markAnswered, openQuestions } from "./taskQuestions";
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

describe("markAnswered", () => {
  const qs = () => [q("python version?", false), q("main or a branch?", true), q("which runner?", true)];

  it("takes the answered question out of the open ones and leaves the rest", () => {
    const a = task("clarifying", qs());
    const other = { ...task("clarifying", qs()), id: "k2" } as Task;
    const next = markAnswered([a, other], "k1", 1, "a branch");
    expect(openQuestions(next[0]).map((x) => x.index)).toEqual([2]);
    expect(next[0].spec.questions[1].answer).toBe("a branch");
    expect(next[1]).toBe(other);                                    // another task is not touched
    expect(openQuestions(a).map((x) => x.index)).toEqual([1, 2]);   // the input is not mutated
  });

  it("an answer that is not in the list changes nothing", () => {
    const a = task("clarifying", qs());
    const list = [a];
    for (const [id, index] of [["gone", 1], ["k1", 9], ["k1", -1]] as const) {
      expect(markAnswered(list, id, index, "x")[0]).toBe(a);
    }
    const answered = task("clarifying", [q("done?", true, "yes")]);
    expect(markAnswered([answered], "k1", 0, "other")[0]).toBe(answered);   // never overwrites an answer
    const noSpec = { id: "k1", state: "clarifying" } as unknown as Task;
    expect(markAnswered([noSpec], "k1", 0, "x")[0]).toBe(noSpec);
  });
});
