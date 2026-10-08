// The pickers, all modal, all built from text.
//
// The model picker **chooses the fast path's model only** (§12.1). Provider
// and model per role live in Settings, read-only: the picker can never touch
// Claude's or Codex's settings, and saying so in the window is part of the
// design, not a decoration.
//
// A model name comes off the network, and this window draws authorization
// cards — so every row is React text, never markup.

import { useEffect, useState } from "react";
import type { AvatarDesc, ModelRow, RouteView, VoiceEntry } from "../types";
import type { Project, ProjectImpact } from "../types";
import { DirPicker } from "./DirPicker";
import { DiscordLink } from "./DiscordLink";
import { api } from "../api";
import { editEffects, formFrom, nameKey, projectEditBody, uniqueName, type ProjectForm } from "../lib/projects";

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

export function SettingsDialog(props: { route: RouteView | null; onClose: () => void }) {
  const chains = props.route?.table?.chains || {};
  return (
    <Shell title="Settings" onClose={props.onClose}>
      <div className="pad" data-testid="routing-readonly">
        <div className="muted small" style={{ marginBottom: 6 }}>Routing</div>
        <table className="plain" data-testid="route-table">
          <tbody>
            {Object.entries(chains).map(([role, chain]) => (
              <tr key={role}>
                <th>{role}</th>
                <td>{Array.isArray(chain) ? chain.join(" → ") : String(chain)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        {Object.keys(chains).length === 0 ? <div className="muted small">no routing table</div> : null}
      </div>
    </Shell>
  );
}

export function ModelPicker(props: {
  models: ModelRow[];
  selected: string | null;
  onPick: (id: string) => void;
  /** `""` is AUTO. Setting effort must not also switch him onto that model. */
  onEffort: (id: string, effort: string) => void;
  onClose: () => void;
}) {
  return (
    <Shell title="Fast-path model" onClose={props.onClose}>
      <div className="pad small muted" data-testid="model-scope-note">
        This picks the <b>fast path's</b> model only. Who runs each role is in Settings and is
        never changed here — the picker cannot touch Claude or Codex.
      </div>
      {props.models.map((m) => (
        <div
          key={m.id}
          className={"prow" + (m.id === props.selected ? " sel" : "")}
          data-testid={`model-${m.id}`}
          onClick={() => props.onPick(m.id)}
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
                // Setting effort must not also switch him onto that model —
                // AUTO included: it used to arrive as a row click.
                props.onEffort(m.id, e.target.value);
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

/**
 * New project, and editing one (decisions B). The owner's first-use complaint
 * was that creating one was not obvious; this is the other half of that fix
 * (the first is the button, in `Sidebar`). Three things it will not do: guess
 * a root, accept a path the backend refuses, or close on an error the owner
 * never saw.
 *
 * The root and each access folder are chosen with `DirPicker`, so a typo is
 * not a project pointed at a folder that does not exist — and a `/mnt/<drive>/`
 * root is badged **before** it is created, because the 9p caution is advice
 * about a decision, not a label on one already made.
 *
 * In **edit** mode it sends only what changed (`projectEditBody`), and says
 * what an edit leaves where it was: a new root applies to new threads and
 * tasks, while existing threads and unfinished tasks stay on the old folder
 * (B5), read back from `/impact` rather than guessed. The Inbox's name and
 * root are fixed, and the dialog says so instead of letting a PATCH 409.
 * A colliding name is numbered by the backend, and the dialog says what it
 * will be saved as.
 */
export function ProjectDialog(props: {
  mode: "new" | "edit";
  initial?: Project | null;
  /** Other projects' names, for the "saves as" preview; the backend decides. */
  taken?: string[];
  /** `channel`: also create its Discord channel (B1, the New Project box). */
  onCreate?: (body: { name: string; root: string; extra_dirs: string[]; profile: string },
              options: { channel: boolean }) => Promise<unknown>;
  onSave?: (body: Record<string, unknown>) => Promise<unknown>;
  /** Is the Discord server set up? Decides the New Project box (B1). */
  discordConfigured?: boolean;
  /** A channel was created, linked or unlinked from the dialog. */
  onDiscordChanged?: () => void;
  onClose: () => void;
}) {
  const edit = props.mode === "edit" && !!props.initial;
  const initial = props.initial || null;
  const start: ProjectForm = initial
    ? formFrom(initial)
    : { name: "", root: "", profile: "auto", extra_dirs: [], always_ask: [] };
  const [name, setName] = useState(start.name);
  const [root, setRoot] = useState(start.root);
  const [extra, setExtra] = useState<string[]>(start.extra_dirs);
  const [profile, setProfile] = useState(start.profile);
  const [rules, setRules] = useState(start.always_ask.join("\n"));
  const [browsing, setBrowsing] = useState<null | "root" | "extra">(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [impact, setImpact] = useState<ProjectImpact | null>(null);
  // Every project gets a channel (decisions O1): on by default once the
  // server is set up, and off — with the reason — before.
  const [channel, setChannel] = useState(!!props.discordConfigured);
  useEffect(() => setChannel(!!props.discordConfigured), [props.discordConfigured]);

  useEffect(() => {
    if (!edit || !initial) return;
    api.projectImpact(initial.id).then(setImpact).catch(() => setImpact(null));
  }, [edit, initial?.id]);

  // Escape closes the dialog wherever focus is (the Inbox's name box, the
  // usual home for it, is disabled) — unless the folder picker is on top.
  useEffect(() => {
    if (browsing) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") props.onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [browsing, props.onClose]);

  const inbox = !!initial?.inbox;
  const windows = root.startsWith("/mnt/");
  const form: ProjectForm = {
    name, root, profile, extra_dirs: extra, always_ask: rules.split("\n"),
  };
  const body = edit && initial ? projectEditBody(initial, form) : null;
  const ready = !!name.trim() && !!root.trim() && !busy && (!edit || Object.keys(body || {}).length > 0);
  const savesAs = props.taken && name.trim() && (!edit || nameKey(name) !== nameKey(initial?.name))
    ? uniqueName(name, props.taken)
    : "";
  const effects = edit && initial && body ? editEffects(initial, body, impact) : [];

  const submit = () => {
    if (!name.trim()) return setError("Give the project a name.");
    if (!root.trim()) return setError("Choose a root folder.");
    if (busy) return;
    if (edit && body && !Object.keys(body).length) return props.onClose();
    setBusy(true);
    setError("");
    const go = edit
      ? props.onSave?.(body || {})
      : props.onCreate?.({ name: name.trim(), root: root.trim(), extra_dirs: extra, profile },
                         { channel: channel && !!props.discordConfigured });
    Promise.resolve(go)
      // The dialog stays open on a refusal with the backend's own words: a
      // dialog that closes on an error is a project the owner believes exists.
      .catch((e) => setError(e?.message || (edit ? "could not save the project" : "could not create the project")))
      .finally(() => setBusy(false));
  };

  const keys = (e: React.KeyboardEvent) => {
    e.stopPropagation();
    if (e.key === "Enter") submit();
    if (e.key === "Escape") props.onClose();
  };

  return (
    <>
      <Shell
        title={edit ? `Edit project · ${initial?.name}` : "New project"}
        onClose={props.onClose}
        foot={
          edit ? (
            <button type="button" data-testid="save-project" disabled={!ready} onClick={submit}>
              Save
            </button>
          ) : (
            <button type="button" data-testid="create-project" disabled={!ready} onClick={submit}>
              Create
            </button>
          )
        }
      >
        <div className="pad col" data-testid={edit ? "project-dialog-edit" : "project-dialog-new"}
             onKeyDown={(e) => { if (e.key === "Escape") props.onClose(); }}>
          <label className="muted small">Name</label>
          <input data-testid="project-name" placeholder="what to call it" value={name}
                 autoFocus={!inbox} disabled={inbox} onKeyDown={keys} onChange={(e) => setName(e.target.value)} />
          {savesAs && savesAs !== name.trim() ? (
            <div className="muted small" data-testid="project-saves-as">
              That name is taken; it will be saved as <b>{savesAs}</b>.
            </div>
          ) : null}
          {inbox ? (
            <div className="muted small" data-testid="project-inbox-note">
              The Inbox is where unplaced chat lands: its name and folder are fixed.
            </div>
          ) : null}

          <label className="muted small">Root folder</label>
          <div className="row">
            <input data-testid="project-root" placeholder="choose or type an absolute path"
                   value={root} disabled={inbox} onKeyDown={keys} onChange={(e) => setRoot(e.target.value)} />
            <button type="button" data-testid="browse-root" disabled={inbox} onClick={() => setBrowsing("root")}>
              Browse
            </button>
          </div>
          {windows ? (
            <div className="row" data-testid="project-win-preview">
              <span className="badge win">WIN</span>
              <span className="muted small">
                A Windows path worked from WSL over 9p: git is slow and line endings are mangled.
              </span>
            </div>
          ) : null}

          <label className="muted small">Access folders (beyond the root)</label>
          {extra.map((d) => (
            <div className="row" key={d} data-testid={`extra-${d}`}>
              <span className="small" style={{ flex: "1 1 auto", overflowWrap: "anywhere" }}>{d}</span>
              <button type="button" onClick={() => setExtra(extra.filter((x) => x !== d))}>
                Remove
              </button>
            </div>
          ))}
          <button type="button" data-testid="browse-extra" onClick={() => setBrowsing("extra")}>
            Add access folder
          </button>

          <label className="muted small">Profile</label>
          <select data-testid="project-profile" value={profile} autoFocus={inbox}
                  onChange={(e) => setProfile(e.target.value)}>
            <option value="auto">auto (default)</option>
            <option value="ask">ask — every dangerous call reaches you</option>
            <option value="strict">strict — no network, deny-all</option>
          </select>

          {!edit ? (
            <label className="row small" data-testid="project-discord-row">
              <input type="checkbox" data-testid="project-discord-create"
                     checked={channel && !!props.discordConfigured}
                     disabled={!props.discordConfigured}
                     onChange={(e) => setChannel(e.target.checked)} />
              <span>Create a Discord channel</span>
              {!props.discordConfigured ? (
                <span className="muted" data-testid="project-discord-setup">
                  · set up the server first: <code>jarvis auth discord-guild</code>
                </span>
              ) : null}
            </label>
          ) : null}

          {edit ? (
            <>
              <label className="muted small">Always ask (one per line: a tool, or a command with globs)</label>
              <textarea rows={3} data-testid="project-always-ask" value={rules}
                        placeholder="vercel --prod"
                        onKeyDown={(e) => {
                          e.stopPropagation();
                          if (e.key === "Escape") props.onClose();
                        }}
                        onChange={(e) => setRules(e.target.value)} />
              <label className="muted small">Discord channel</label>
              {initial ? <DiscordLink project={initial} onChanged={props.onDiscordChanged} /> : null}
              {effects.length ? (
                <div className="effects" data-testid="project-effects">
                  {effects.map((line) => <div key={line}>{line}</div>)}
                  {typeof body?.root === "string" && impact?.on_root.tasks.length ? (
                    <ul className="small">
                      {impact.on_root.tasks.map((t) => (
                        <li key={t.id}>{t.id} · {t.state} · {t.brief.slice(0, 60)}</li>
                      ))}
                    </ul>
                  ) : null}
                </div>
              ) : null}
            </>
          ) : null}

          {error ? <div className="err" data-testid="project-error">{error}</div> : null}
        </div>
      </Shell>
      {browsing ? (
        <DirPicker
          title={browsing === "root" ? "Project root" : "Access folder"}
          confirm="Use this folder"
          start={browsing === "root" ? root : root || ""}
          onChoose={(p) => {
            if (browsing === "root") setRoot(p);
            else if (!extra.includes(p)) setExtra([...extra, p]);
            setBrowsing(null);
          }}
          onClose={() => setBrowsing(null)}
        />
      ) : null}
    </>
  );
}

/** The create half of `ProjectDialog`, under its old name. */
export function NewProject(props: {
  onCreate: (body: { name: string; root: string; extra_dirs: string[]; profile: string },
             options: { channel: boolean }) => Promise<unknown>;
  taken?: string[];
  discordConfigured?: boolean;
  onClose: () => void;
}) {
  return <ProjectDialog mode="new" onCreate={props.onCreate} taken={props.taken}
                        discordConfigured={props.discordConfigured} onClose={props.onClose} />;
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
