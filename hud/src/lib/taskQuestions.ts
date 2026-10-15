// A task's open questions, by the daemon's own rule (`commands.open_questions`,
// which Discord's `/answer` reads, and `runner.answer_question`): only a
// CLARIFYING task is waiting on one; a question is open while it is blocking
// and unanswered; and its `index` is its place in `spec.questions` — every
// question, answered and assumable ones included, never its place among the
// open ones. `POST /tasks/{id}/answer` takes exactly `{index, text}`.
//
// The HUD used to send `{question, answer}` and swallow the 400 (2026-10-11),
// so answering from the window never worked; and it numbered questions by
// their place in its own filtered list, which is only right while every
// question before an open one is open too.

import type { OpenQuestion, Task } from "../types";

export interface Asked {
  /** The question's place in `spec.questions`: what `/answer` takes. */
  index: number;
  question: OpenQuestion;
}

export function openQuestions(task: Task | null | undefined): Asked[] {
  if (!task || task.state !== "clarifying") return [];
  const out: Asked[] = [];
  (task.spec?.questions || []).forEach((question, index) => {
    if (question && question.blocking && question.answer == null) out.push({ index, question });
  });
  return out;
}


/**
 * The task list once the daemon has accepted an answer: that question holds
 * the text, so `openQuestions` stops asking it without waiting for the next
 * `refreshTasks()`. Call it only after `/answer` resolved — a refusal leaves the
 * list, the row and the typed words as they were. Nothing here is the truth
 * (the refresh that follows is); a task, index or question it does not find is
 * returned untouched, and the other tasks keep their identity.
 */
export function markAnswered(tasks: Task[], taskId: string, index: number, text: string): Task[] {
  return tasks.map((t) => {
    const questions = t.id === taskId ? t.spec?.questions : undefined;
    const asked = questions?.[index];
    if (!questions || !asked || asked.answer != null) return t;
    return { ...t, spec: { ...t.spec, questions: questions.map((x, i) => (i === index ? { ...x, answer: text } : x)) } };
  });
}
