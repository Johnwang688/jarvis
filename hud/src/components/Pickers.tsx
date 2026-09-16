// The pickers, all modal, all built from text.
//
// The model picker **chooses the fast path's model only** (§12.1). Provider
// and model per role come from the routing table, shown read-only beside it:
// the picker can never touch Claude's or Codex's settings, and saying so in
// the window is part of the design, not a decoration.
//
// A model name comes off the network, and this window draws authorization
// cards — so every row is React text, never markup.

import { useState } from "react";
import type { AvatarDesc, ModelRow, RouteView, VoiceEntry } from "../types";

function Shell(props: { title: string; onClose: () => void; children: React.ReactNode; foot?: React.ReactNode }) {
  return (
    <div className="pickerveil" data-testid="picker" onClick={(e) => {
      if (e.target === e.currentTarget) props.onClose();
    }}>
      <div className="picker">
        <h3>{props.title}</h3>
        <div className="rows">{props.children}</div>
        <div className="foot">
          {props.foot}
          <button type="button" data-testid="picker-close" onClick={props.onClose}>Close</button>
        </div>
      </div>
    </div>
  );
}

export function ModelPicker(props: {
  models: ModelRow[];
  selected: string | null;
  route: RouteView | null;
  onPick: (id: string, effort?: string | null) => void;
  onClose: () => void;
}) {
  return (
    <Shell title="Fast-path model" onClose={props.onClose}>
      <div className="pad small muted" data-testid="model-scope-note">
        This picks the <b>fast path's</b> model only. Provider and model per role come from the
        routing table below and are never changed here — the picker cannot touch Claude or Codex.
      </div>
      {props.models.map((m) => (
        <div
          key={m.id}
          className={"prow" + (m.id === props.selected ? " sel" : "")}
          data-testid={`model-${m.id}`}
          onClick={() => props.onPick(m.id, null)}
        >
          <span>{m.name || m.id}</span>
          <span className="sub">{m.id}</span>
          {m.efforts?.length ? (
            <select
              data-testid={`effort-${m.id}`}
              value={m.effort || ""}
              onClick={(e) => e.stopPropagation()}
              onChange={(e) => {
                e.stopPropagation();
                // Setting effort must not also switch him onto that model.
                props.onPick(m.id, e.target.value || null);
              }}
              style={{ width: 90 }}
            >
              <option value="">AUTO</option>
              {m.efforts.map((x) => (
                <option key={x} value={x}>{x}</option>
              ))}
            </select>
          ) : null}
        </div>
      ))}
      {props.route ? (
        <div className="pad" data-testid="routing-readonly">
          <table className="plain">
            <tbody>
              {Object.entries(props.route.table?.chains || {}).map(([role, chain]) => (
                <tr key={role}>
                  <th>{role}</th>
                  <td>{Array.isArray(chain) ? chain.join(" → ") : String(chain)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </Shell>
  );
}

export function VoicePicker(props: {
  voices: VoiceEntry[];
  override: string;
  onPick: (name: string) => void;
  onClose: () => void;
}) {
  return (
    <Shell title="Voice" onClose={props.onClose}>
      {/* "" clears the override, which is the way back to the avatar's own
          voice — a picker that outranks every avatar forever with no way out
          was the v1 bug this row fixes. */}
      <div
        className={"prow" + (props.override ? "" : " sel")}
        data-testid="voice-default"
        onClick={() => props.onPick("")}
      >
        <span>AVATAR DEFAULT</span>
      </div>
      {props.voices.map((v) => (
        <div
          key={v.name}
          className={"prow" + (v.name === props.override ? " sel" : "")}
          data-testid={`voice-${v.name}`}
          onClick={() => props.onPick(v.name)}
        >
          <span>{v.name}</span>
          <span className="sub">{v.backend} · {v.kind}</span>
        </div>
      ))}
    </Shell>
  );
}

export function AvatarPicker(props: {
  avatars: AvatarDesc[];
  active: string;
  onPick: (slug: string) => void;
  onClose: () => void;
}) {
  return (
    <Shell title="Avatar" onClose={props.onClose}>
      {props.avatars.map((a) => (
        <div
          key={a.slug}
          className={"prow" + (a.slug === props.active ? " sel" : "")}
          data-testid={`avatar-${a.slug}`}
          onClick={() => props.onPick(a.slug)}
        >
          <span>{a.name}</span>
          <span className="sub">{(a.wake || []).join(" / ")}</span>
        </div>
      ))}
    </Shell>
  );
}

export function NewProject(props: {
  onCreate: (body: { name: string; root: string; extra_dirs: string[]; profile: string }) => void;
  onClose: () => void;
}) {
  const [name, setName] = useState("");
  const [root, setRoot] = useState("");
  const [extra, setExtra] = useState("");
  const [profile, setProfile] = useState("auto");
  return (
    <Shell
      title="New project"
      onClose={props.onClose}
      foot={
        <button
          type="button"
          data-testid="create-project"
          onClick={() =>
            name.trim() &&
            root.trim() &&
            props.onCreate({
              name: name.trim(),
              root: root.trim(),
              extra_dirs: extra.split(",").map((s) => s.trim()).filter(Boolean),
              profile,
            })
          }
        >
          Create
        </button>
      }
    >
      <div className="pad col">
        <input data-testid="project-name" placeholder="name" value={name}
               onKeyDown={(e) => e.stopPropagation()} onChange={(e) => setName(e.target.value)} />
        <input data-testid="project-root" placeholder="absolute root path" value={root}
               onKeyDown={(e) => e.stopPropagation()} onChange={(e) => setRoot(e.target.value)} />
        <input data-testid="project-extra" placeholder="access folders, comma separated" value={extra}
               onKeyDown={(e) => e.stopPropagation()} onChange={(e) => setExtra(e.target.value)} />
        <select data-testid="project-profile" value={profile} onChange={(e) => setProfile(e.target.value)}>
          <option value="auto">auto (default)</option>
          <option value="ask">ask — every dangerous call reaches you</option>
          <option value="strict">strict — no network, deny-all</option>
        </select>
      </div>
    </Shell>
  );
}

export function NewTask(props: { onCreate: (brief: string) => void; onClose: () => void }) {
  const [brief, setBrief] = useState("");
  return (
    <Shell
      title="New task"
      onClose={props.onClose}
      foot={
        <button type="button" data-testid="create-task" onClick={() => brief.trim() && props.onCreate(brief.trim())}>
          Create
        </button>
      }
    >
      <div className="pad col">
        <textarea rows={5} data-testid="task-brief" placeholder="brief — say what done means"
                  value={brief} onKeyDown={(e) => e.stopPropagation()}
                  onChange={(e) => setBrief(e.target.value)} />
        <span className="muted small">
          Acceptance criteria belong in the brief: a task with nothing pinning "done" produces a
          complete-looking skeleton.
        </span>
      </div>
    </Shell>
  );
}
