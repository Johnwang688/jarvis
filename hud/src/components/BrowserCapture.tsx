// Explicit owner gestures only. A capture never turns see on or changes source.
import { useLayoutEffect, useRef, useState } from "react";
import { api } from "../api";
import type { Attachment } from "../types";
import { type Op } from "../lib/annotate";
import {
  CAPTURE_CHANGED, CAPTURE_NO_CHAT, DIRECT_CAPTURE, captureAttachments, captureReply,
  type CaptureIdentity, type CaptureStill,
} from "../lib/pageCapture";
import { Annotate } from "./Annotate";

export interface CaptureDestination {
  /** Read at the Send gesture, before either capture or export starts. */
  target: () => string | null;
  /** Atomically commit both files to that draft; return whether both landed. */
  stage: (key: string, pair: Attachment[]) => boolean;
}
export function BrowserCapture(props: {
  remoteTabId: string | null; active: boolean; drawn: boolean; blocked: boolean; zoom: number;
  identity: () => CaptureIdentity | null; destination: CaptureDestination;
  onRemote: () => void; onAnnotating?: (active: boolean) => void;
}) {
  const [whole, setWhole] = useState(false);
  const [still, setStill] = useState<CaptureStill | null>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [fallback, setFallback] = useState(false);
  const now = useRef(props); now.current = props;
  const epoch = useRef(0), working = useRef(false), mounted = useRef(true);
  const eligible = () => mounted.current && now.current.active && now.current.drawn && !now.current.blocked;
  useLayoutEffect(() => {
    // Clearing coverage later cannot revive an older capture/export response.
    if (!props.active || !props.drawn || props.blocked) { epoch.current++; working.current = false; setBusy(false); }
  }, [props.active, props.drawn, props.blocked]);
  useLayoutEffect(() => { mounted.current = true; return () => { mounted.current = false; epoch.current++; now.current.onAnnotating?.(false); }; }, []);
  useLayoutEffect(() => { now.current.onAnnotating?.(!!still); }, [!!still]);
  useLayoutEffect(() => { epoch.current++; working.current = false; setBusy(false); setStill(null); setMessage(""); setFallback(false); }, [props.remoteTabId]);
  const valid = (token: number) => token === epoch.current && eligible();
  const target = () => {
    const key = now.current.destination.target();
    if (!key) setMessage(CAPTURE_NO_CHAT);
    return key;
  };
  const stage = async (capture: CaptureStill, ops: readonly Op[], key: string, token: number) => {
    const pair = await captureAttachments(capture, ops);
    if (!valid(token)) return false;
    const accepted = now.current.destination.stage(key, pair);
    if (!accepted) setMessage("This capture needs two attachment slots: remove a file and try again.");
    return accepted;
  };
  const begin = async (send: boolean) => {
    if (!eligible() || working.current) return;
    setMessage("");
    const key = send ? target() : null;
    if (send && !key) return;
    if (!props.remoteTabId) { setFallback(true); setMessage(DIRECT_CAPTURE); return; }
    const identity = props.identity();
    if (!identity) { setMessage(CAPTURE_CHANGED); return; }
    const stamp = JSON.stringify(identity), token = ++epoch.current;
    working.current = true; setBusy(true); setFallback(false);
    try {
      const capture = captureReply(await api.captureBrowser(props.remoteTabId, identity, whole));
      if (!valid(token) || JSON.stringify(now.current.identity()) !== stamp) {
        if (valid(token)) setMessage(CAPTURE_CHANGED);
        return;
      }
      setStill(capture);
      if (send && key && await stage(capture, [], key, token)) setStill(null);
    } catch (e) {
      if (valid(token)) setMessage(e instanceof Error ? e.message : "This page could not be captured.");
    } finally {
      if (mounted.current && token === epoch.current) { working.current = false; setBusy(false); }
    }
  };
  const sendStill = async (ops: readonly Op[]) => {
    if (!still || !eligible() || working.current) return false;
    const key = target(); if (!key) return false;
    const token = ++epoch.current;
    working.current = true;
    try { return await stage(still, ops, key, token); }
    finally { if (token === epoch.current) working.current = false; }
  };
  const disabled = !props.active || !props.drawn || props.blocked || busy;
  return <>
    <div className="browser-capture-actions" data-testid="browser-capture-actions">
      <button type="button" disabled={disabled} onClick={() => void begin(false)}>Annotate</button>
      <button type="button" disabled={disabled} onClick={() => void begin(true)}>Send to chat</button>
      <label><input type="checkbox" checked={whole} disabled={disabled || !!still}
        onChange={e => { if (eligible()) setWhole(e.target.checked); }}
        onKeyDown={e => e.stopPropagation()} onKeyUp={e => e.stopPropagation()} /> Whole-page text</label>
      {busy && <span role="status">Capturing…</span>}
    </div>
    {message && <div className="annotation-note" role="status" data-testid="capture-notice">{message}</div>}
    {fallback && <button type="button" className="capture-remote" disabled={disabled}
      onClick={() => { if (eligible()) { setFallback(false); props.onRemote(); } }}>Open in Remote</button>}
    {still && <Annotate key={still.capture_id} still={still} zoom={props.zoom}
      blocked={props.blocked || busy} active={props.active && props.drawn} notice={message}
      onClose={() => { if (eligible()) setStill(null); }} onStage={sendStill} />}
  </>;
}
