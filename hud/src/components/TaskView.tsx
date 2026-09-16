// The task view — the spec with an answer box per blocking question, the plan
// with the current step marked, the status record, the routing line, steer,
// cancel/resume, and the §10.4 report when done.
//
// Everything here is read from the **status record**, which the runner
// maintains and the model never writes (§10.3). The one v1 lesson this
// encodes: a surface that shows the owner something the record does not carry
// is a surface inventing it.

import { useState } from "react";
import type { Task, TaskThread } from "../types";
import { routingLine } from "../state/store";

function elapsed(s: number) {
  const n = Math.max(0, Math.round(s));
  const m = Math.floor(n / 60);
  return m ? `${m}m ${n % 60}s` : `${n}s`;
}

export function TaskView(props: {
  task: Task | null;
  threads: TaskThread[];
  onAnswer: (question: string, answer: string) => void;
  onSteer: (text: string) => void;
  onCancel: () => void;
  onResume: () => void;
  onStart: () => void;
}) {
  const [answers, setAnswers] = useState<Record<string, string>>({});
  const [steer, setSteer] = useState("");
  const t = props.task;

  if (!t) {
    return (
      <div className="block muted" data-testid="task-empty">
        No task selected.
      </div>
    );
  }

  const st = t.status || ({} as Task["status"]);
  const blocking = (t.spec?.questions || []).filter((q) => q.blocking && !q.answer);
  const line = routingLine(t);

  return (
    <div data-testid="task-view">
      <div className="block">
        <h3>Status</h3>
        <div className="kv"><span className="k">phase</span><span className={`v phase ${st.phase}`} data-testid="task-phase">{st.phase}</span></div>
        <div className="kv"><span className="k">step</span><span className="v" data-testid="task-step">{st.steps ? `${st.step}/${st.steps}` : "—"}</span></div>
        <div className="kv"><span className="k">elapsed</span><span className="v" data-testid="task-elapsed">{elapsed(st.elapsed_s || 0)}</span></div>
        <div className="kv"><span className="k">cost</span><span className="v" data-testid="task-cost">${(st.cost_usd || 0).toFixed(4)}</span></div>
        <div className="kv"><span className="k">last tool</span><span className="v">{st.last_tool || "—"}</span></div>
        <div className="kv"><span className="k">last file</span><span className="v">{st.last_file || "—"}</span></div>
        {/* §8.5: a routing decision the owner cannot see is one they will
            assume was wrong. */}
        <div className="routing-line" data-testid="routing-line">{line || "routing: not decided yet"}</div>
      </div>

      <div className="block">
        <h3>Controls</h3>
        <div className="row" style={{ flexWrap: "wrap" }}>
          {t.state === "intake" ? (
            <button type="button" data-testid="task-start" onClick={props.onStart}>Start</button>
          ) : null}
          <button type="button" data-testid="task-cancel" onClick={props.onCancel}>Cancel</button>
          {/* resume applies to BLOCKED only; FAILED is terminal (WP11). */}
          <button
            type="button"
            data-testid="task-resume"
            disabled={t.state !== "blocked"}
            title={t.state !== "blocked" ? "resume applies to a blocked task" : ""}
            onClick={props.onResume}
          >
            Resume
          </button>
        </div>
        <div className="col" style={{ marginTop: 6 }}>
          <textarea
            data-testid="steer-box"
            rows={2}
            placeholder="steer…"
            value={steer}
            onChange={(e) => setSteer(e.target.value)}
            onKeyDown={(e) => e.stopPropagation()}
          />
          <button
            type="button"
            data-testid="steer-send"
            onClick={() => {
              if (!steer.trim()) return;
              props.onSteer(steer.trim());
              setSteer("");
            }}
          >
            Steer
          </button>
        </div>
      </div>

      <div className="block">
        <h3>Spec</h3>
        <div className="kv"><span className="k">goal</span><span className="v">{t.spec?.goal || "—"}</span></div>
        <div className="kv"><span className="k">deliverable</span><span className="v">{t.spec?.deliverable || "—"}</span></div>
        {(t.spec?.acceptance || []).map((a, i) => (
          <div className="step" key={i} data-testid="acceptance">· {a}</div>
        ))}
        {blocking.length ? (
          <div style={{ marginTop: 8 }} data-testid="blocking-questions">
            {blocking.map((q, i) => (
              <div className="col" key={i} style={{ marginBottom: 8 }}>
                <div className="small" style={{ color: "var(--amber)" }}>{q.text}</div>
                {q.options?.length ? <div className="muted small">{q.options.join(" / ")}</div> : null}
                <input
                  data-testid={`answer-${i}`}
                  value={answers[q.text] || ""}
                  onChange={(e) => setAnswers({ ...answers, [q.text]: e.target.value })}
                  onKeyDown={(e) => e.stopPropagation()}
                />
                <button
                  type="button"
                  data-testid={`answer-send-${i}`}
                  onClick={() => {
                    const a = (answers[q.text] || "").trim();
                    if (a) props.onAnswer(q.text, a);
                  }}
                >
                  Answer
                </button>
              </div>
            ))}
          </div>
        ) : null}
      </div>

      <div className="block">
        <h3>Plan</h3>
        {(t.plan || []).length === 0 ? (
          <div className="muted small">no plan yet</div>
        ) : (
          (t.plan || []).map((step, i) => {
            const n = i + 1;
            const cls = n < st.step ? "done" : n === st.step ? "cur" : "";
            return (
              <div className={`step ${cls}`} key={i} data-testid={`plan-step-${n}`}>
                <span>{n === st.step ? "▶" : n < st.step ? "✓" : "·"}</span>
                <span>{step}</span>
              </div>
            );
          })
        )}
      </div>

      <div className="block">
        <h3>Threads</h3>
        {props.threads.length === 0 ? (
          <div className="muted small">none yet</div>
        ) : (
          props.threads.map((k) => (
            <div className="kv" key={k.thread_id}>
              <span className="k">{k.role}</span>
              <span className="v">
                {k.provider}
                {k.model ? ` · ${k.model}` : ""} · {k.state} · {k.turns} turns
              </span>
            </div>
          ))
        )}
      </div>

      {t.report ? (
        <div className="block" data-testid="report">
          <h3>Report</h3>
          {/* the §10.4 layout, in that order */}
          <div className="kv"><span className="k">DONE</span><span className="v">{t.report.done.join("; ")}</span></div>
          <div className="kv"><span className="k">CHANGED</span><span className="v">{t.report.changed.join("; ")}</span></div>
          <div className="kv"><span className="k">VERIFIED</span><span className="v">{t.report.verified}</span></div>
          <div className="kv"><span className="k">OPEN</span><span className="v">{t.report.open.join("; ") || "—"}</span></div>
          <div className="kv"><span className="k">NEXT</span><span className="v">{t.report.next || "—"}</span></div>
          <div className="kv">
            <span className="k">COST</span>
            <span className="v">
              {Object.entries(t.report.cost || {})
                .map(([k, v]) => `${k}: ${v}`)
                .join(" · ") || "—"}
            </span>
          </div>
        </div>
      ) : null}
    </div>
  );
}
