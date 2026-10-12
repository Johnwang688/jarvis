// New VEX project (VX-3; editor plan §2.9, decisions V-4): a project from the
// VS Code extension's own templates, made through the `project_folder` path.
//
// Three things it will not do:
//   - **make up a path.** The templates are the daemon's list, and the exact
//     folder shown is the daemon's answer to the very body Create sends
//     (`POST /vex/projects/check`): the path the approval card will name.
//     Create waits until that answer is for exactly the inputs on screen.
//   - **say yes for the owner.** Create raises the `project_folder` card
//     (Approve/Deny, never Always); the dialog only reads how it ended. Deny
//     makes nothing, and the dialog says so and stays open.
//   - **act behind a card.** Under any approval card every control is
//     disabled (and inert, as everything outside the veil is), and Escape is
//     left alone: it belongs to the card, which denies — the dialog neither
//     closes on it nor stops it.
//
// Rules in `lib/vexNew.ts`; the daemon's side in `jarvis/v2/vex.py`.

import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { SETUP_COMMAND, setupNote, type Language, type VexStatus } from "../lib/vex";
import {
  bodyKey, createReady, newBody, pickTemplate, projectNameRefusal, requestLine, templatesOf,
  type NewCheck, type NewRequest, type Template,
} from "../lib/vexNew";

const POLL_MS = 400;
const CHECK_DELAY_MS = 200;

export function NewVexProject(props: {
  /** An approval card is up: every control off, Escape the card's. */
  blocked: boolean;
  /** The project exists (the request ended `created`). */
  onCreated: (projectId: string) => void;
  onClose: () => void;
}) {
  const [status, setStatus] = useState<VexStatus | null>(null);
  const [loadError, setLoadError] = useState("");
  const [templates, setTemplates] = useState<Template[] | null>(null);
  const [name, setName] = useState("");
  const [language, setLanguage] = useState<Language>("cpp");
  const [template, setTemplate] = useState("");
  const [location, setLocation] = useState("");
  const [check, setCheck] = useState<{ key: string; value: NewCheck } | null>(null);
  const [refusal, setRefusal] = useState<{ key: string; text: string } | null>(null);
  const [request, setRequest] = useState<NewRequest | null>(null);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");

  const blocked = props.blocked;
  const blockedRef = useRef(blocked);
  blockedRef.current = blocked;
  const closeRef = useRef(props.onClose);
  closeRef.current = props.onClose;
  const createdRef = useRef(props.onCreated);
  createdRef.current = props.onCreated;

  // What VEX is set up with, then its templates (never asked for unset up).
  useEffect(() => {
    let live = true;
    api.vexStatus()
      .then((s) => {
        if (!live) return;
        setStatus(s);
        if (!s) return setLoadError("The daemon's VEX status was not understood.");
        setLocation(s.project_folder || "");
        if (!s.configured) return;
        return api.vexTemplates().then((list) => {
          if (!live) return;
          setTemplates(list);
          if (!list.length) setLoadError("The templates folder holds no VS Code project templates.");
        });
      })
      .catch((e) => live && setLoadError(e?.message || "VEX's status could not be read."));
    return () => {
      live = false;
    };
  }, []);

  // The template follows the language: the one chosen while it is offered, else the first.
  useEffect(() => {
    if (templates) setTemplate((t) => pickTemplate(templates, language, t));
  }, [templates, language]);

  const configured = !!status?.configured;
  const defaultFolder = status?.project_folder ?? null;
  const form = { name, language, template, location };
  const body = newBody(form, defaultFolder);
  const key = bodyKey(body);
  const nameWhy = name ? projectNameRefusal(name) : null;
  const pending = sending || request?.state === "asking";

  // The daemon's own answer for exactly these inputs; an older answer never lands.
  const seq = useRef(0);
  useEffect(() => {
    const n = ++seq.current;
    if (!configured || !template || projectNameRefusal(name)) return;
    const timer = window.setTimeout(() => {
      api.vexCheckNew(body)
        .then((value) => {
          if (n !== seq.current) return;
          setCheck({ key, value });
          setRefusal(null);
        })
        .catch((e) => {
          if (n !== seq.current) return;
          setCheck(null);
          setRefusal({ key, text: e?.message || "The daemon refused that folder." });
        });
    }, CHECK_DELAY_MS);
    return () => window.clearTimeout(timer);
    // `key` is the body; the body object itself is new every render.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, configured]);

  // A request on its way: read how it ended (the card is the owner's).
  useEffect(() => {
    if (!request) return;
    if (request.state === "created" && request.project_id) {
      createdRef.current(request.project_id);
      return;
    }
    if (request.state !== "asking") return;
    let live = true;
    const timer = window.setInterval(() => {
      api.vexNewState(request.id)
        .then((r) => live && r.state !== "asking" && setRequest(r))
        .catch((e) => {
          if (!live) return;
          setRequest({ ...request, state: "failed", message: e?.message || "The request could not be read." });
        });
    }, POLL_MS);
    return () => {
      live = false;
      window.clearInterval(timer);
    };
  }, [request]);

  // Escape closes the dialog — never under a card: that Escape is the card's (it denies).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (blockedRef.current || e.key !== "Escape") return;
      closeRef.current();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const shown = check && check.key === key ? check.value : null;
  const said = refusal && refusal.key === key ? refusal.text : "";
  const ready = createReady({ configured, form, defaultFolder, check, pending, blocked });

  const create = () => {
    if (!ready || blockedRef.current) return;
    setSending(true);
    setError("");
    setRequest(null);
    api.vexNewProject(body)
      .then((r) => setRequest(r))
      // A refusal stays on screen in the daemon's words: the dialog never
      // closes on an error the owner did not see.
      .catch((e) => setError(e?.message || "The project could not be made."))
      .finally(() => setSending(false));
  };

  const keys = (e: React.KeyboardEvent) => {
    if (blockedRef.current) return;      // a card's keys are the card's
    e.stopPropagation();
    if (e.key === "Enter") create();
    if (e.key === "Escape") props.onClose();
  };

  const setup = setupNote(status);
  const offered = templates ? templatesOf(templates, language) : [];
  const off = blocked || pending;

  return (
    <div className="pickerveil" data-testid="vex-new" onClick={(e) => {
      if (e.target === e.currentTarget && !blockedRef.current) props.onClose();
    }}>
      <div className="picker" role="dialog" aria-label="New VEX project">
        <h3>New VEX project</h3>
        <div className="rows">
          <div className="pad col" data-testid="vex-new-form">
            {setup ? (
              <div className="vexsetup" data-testid="vex-new-setup" role="status">
                <div>{setup.headline}</div>
                <code className="vexcmd">{SETUP_COMMAND}</code>
                {setup.detail ? <div className="muted small">{setup.detail}</div> : null}
              </div>
            ) : null}
            {loadError ? <div className="err" data-testid="vex-new-load-error">{loadError}</div> : null}

            <label className="muted small" htmlFor="vex-new-name">Name</label>
            <input id="vex-new-name" data-testid="vex-new-name" placeholder="letters, digits, _ and -"
                   value={name} autoFocus disabled={off} maxLength={80} spellCheck={false}
                   onKeyDown={keys} onChange={(e) => setName(e.target.value)} />
            {nameWhy ? <div className="err" data-testid="vex-new-name-refusal">{nameWhy}</div> : null}

            <label className="muted small" htmlFor="vex-new-language">Language</label>
            <select id="vex-new-language" data-testid="vex-new-language" value={language} disabled={off}
                    onKeyDown={keys} onChange={(e) => setLanguage(e.target.value === "python" ? "python" : "cpp")}>
              <option value="cpp">C++</option>
              <option value="python">Python</option>
            </select>

            <label className="muted small" htmlFor="vex-new-template">Template</label>
            <select id="vex-new-template" data-testid="vex-new-template" value={template}
                    disabled={off || !offered.length} onKeyDown={keys}
                    onChange={(e) => setTemplate(e.target.value)}>
              {offered.map((t) => (
                <option key={t.id} value={t.id} title={t.id}>{t.label}</option>
              ))}
            </select>
            {templates && !offered.length ? (
              <div className="muted small" data-testid="vex-new-no-templates">
                No {language === "cpp" ? "C++" : "Python"} templates in the extension's folder.
              </div>
            ) : null}

            <label className="muted small" htmlFor="vex-new-location">In folder</label>
            <input id="vex-new-location" data-testid="vex-new-location" value={location} disabled={off}
                   spellCheck={false} onKeyDown={keys} onChange={(e) => setLocation(e.target.value)} />
            {defaultFolder ? (
              <div className="muted small" data-testid="vex-new-default">
                Default (from <code>jarvis vex setup</code>): {defaultFolder}
              </div>
            ) : null}

            <div className="effects" data-testid="vex-new-target">
              {shown ? (
                <>
                  <div className="muted small">Creates exactly this folder:</div>
                  <div className="small" data-testid="vex-new-path" style={{ overflowWrap: "anywhere" }}>
                    {shown.path}
                  </div>
                  {shown.windows_path ? (
                    <div className="muted small" data-testid="vex-new-windows"
                         style={{ overflowWrap: "anywhere" }}>
                      {shown.windows_path}
                    </div>
                  ) : null}
                  <div className="muted small" data-testid="vex-new-asks">
                    {shown.asks
                      ? "You approve the folder on a card; Deny makes nothing."
                      : `${shown.describe[0]?.toUpperCase() ?? ""}${shown.describe.slice(1)}: used without asking.`}
                  </div>
                </>
              ) : said ? (
                <div className="err" data-testid="vex-new-refusal">{said}</div>
              ) : (
                <div className="muted small" data-testid="vex-new-path-pending">
                  {configured && name && !nameWhy && template ? "Checking the folder…" : "Name the project to see its folder."}
                </div>
              )}
            </div>

            {request ? (
              <div className={request.state === "failed" ? "err" : "small"} data-testid="vex-new-state"
                   data-state={request.state} role="status">
                {requestLine(request)}
              </div>
            ) : null}
            {error ? <div className="err" data-testid="vex-new-error">{error}</div> : null}
          </div>
        </div>
        <div className="foot">
          <button type="button" data-testid="vex-new-create" disabled={!ready} onClick={create}>
            {request?.state === "asking" ? "Waiting for the card…" : sending ? "Sending…" : "Create"}
          </button>
          <button type="button" data-testid="vex-new-close" disabled={blocked} onClick={() => {
            if (!blockedRef.current) props.onClose();
          }}>
            Close
          </button>
        </div>
      </div>
    </div>
  );
}
