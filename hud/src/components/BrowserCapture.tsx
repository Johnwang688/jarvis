// Explicit owner gestures only. A capture never turns see on or changes source.
// A Direct tab captures only when the owner's `direct_capture` switch is on
// (B-DIRECT-CAPTURE, lib/directCapture.ts), on a trusted press; otherwise it
// offers the explicit "Open in Remote" conversion, as before.
import { useLayoutEffect, useRef, useState } from "react";
import { api } from "../api";
import { createPortal } from "react-dom";
import type { CaptureTarget } from "../lib/captureDraft";
import type { Attachment } from "../types";
import { type Op } from "../lib/annotate";
import {
  CAPTURE_CHANGED, CAPTURE_NO_CHAT, DIRECT_CAPTURE, captureAttachments, captureReply, directStill,
  type CaptureIdentity, type CaptureStill,
} from "../lib/pageCapture";
import { DirectRefused, browserEnv, captureDirect, type CaptureEnv } from "../lib/directCapture";
import { Annotate } from "./Annotate";

export interface CaptureDestination {
  /** Read at the Send gesture, before either capture or export starts. */
  target: () => CaptureTarget | null;
  /** Atomically commit both files to that draft; return whether both landed. */
  stage: (target: CaptureTarget, pair: Attachment[]) => boolean;
  release?: (target: CaptureTarget) => void;
}
export interface DirectCapture {
  /** The owner's switch, read at the gesture: true only on the daemon's receipt. */
  enabled: () => boolean;
  /** The tab's own live iframe, or null. */
  frame: () => HTMLIFrameElement | null;
  env?: CaptureEnv;
}
export function BrowserCapture(props: {
  remoteTabId: string | null; active: boolean; drawn: boolean; blocked: boolean; zoom: number;
  identity: () => CaptureIdentity | null; destination: CaptureDestination;
  onRemote: () => void; onAnnotating?: (active: boolean) => void;
  controlsTarget?: HTMLElement | null; beforeCapture?: () => Promise<void>;
  direct?: DirectCapture;
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
  const stage = async (capture: CaptureStill, ops: readonly Op[], key: CaptureTarget, token: number) => {
    const pair = await captureAttachments(capture, ops);
    if (!valid(token)) return false;
    const accepted = now.current.destination.stage(key, pair);
    if (!accepted) setMessage("This capture needs two attachment slots: remove a file and try again.");
    return accepted;
  };
  const begin = async (send: boolean, press: { isTrusted: boolean }) => {
    if (!eligible() || working.current) return;
    const direct = !now.current.remoteTabId && now.current.direct?.enabled() ? now.current.direct : null;
    const env = direct ? direct.env ?? browserEnv() : null;
    if (direct && !env!.trusted(press)) { setMessage(new DirectRefused("gesture").message); return; }
    setMessage("");
    const key = send ? target() : null;
    if (send && !key) return;
    const token = ++epoch.current;
    working.current = true; setBusy(true); setFallback(false);
    try {
      // Pin the destination/token before waiting for an options panel's fresh
      // uncovered receipt. A card/tab/profile change cannot revive this gesture.
      await now.current.beforeCapture?.();
      if (!valid(token)) return;
      if (!now.current.remoteTabId && !direct) { setFallback(true); setMessage(DIRECT_CAPTURE); return; }
      const identity = now.current.identity();
      if (!identity) { setMessage(CAPTURE_CHANGED); return; }
      const stamp = JSON.stringify(identity);
      let capture: CaptureStill;
      if (direct) {
        const frame = direct.frame();
        if (!frame) { setFallback(true); setMessage(DIRECT_CAPTURE); return; }
        // Let a just-closed options panel leave the screen before it is checked.
        await new Promise<void>(r => requestAnimationFrame(() => requestAnimationFrame(() => r())));
        const live = () => valid(token) && direct.frame() === frame && JSON.stringify(now.current.identity()) === stamp;
        if (!live()) return;
        try {
          const image = await captureDirect(frame, env!, live);
          // The reference is asked for only after the pixels passed: a
          // cancelled prompt or refused frame leaves nothing on the daemon.
          capture = directStill(await api.directCapture(identity), image);
        } catch (e) {
          if (e instanceof DirectRefused) {
            if (e.reason !== "stale" && valid(token)) { setMessage(e.message); if (e.reason !== "gesture") setFallback(true); }
            return;
          }
          throw e;
        }
      } else capture = captureReply(await api.captureBrowser(now.current.remoteTabId!, identity, whole));
      if (!valid(token) || JSON.stringify(now.current.identity()) !== stamp) {
        if (valid(token)) setMessage(CAPTURE_CHANGED);
        return;
      }
      setStill(capture);
      if (send && key && await stage(capture, [], key, token)) setStill(null);
    } catch (e) {
      if (valid(token)) setMessage(e instanceof Error ? e.message : "This page could not be captured.");
    } finally {
      if (key) now.current.destination.release?.(key);
      if (mounted.current && token === epoch.current) { working.current = false; setBusy(false); }
    }
  };
  const sendStill = async (ops: readonly Op[]) => {
    if (!still || !eligible() || working.current) return false;
    const key = target(); if (!key) return false;
    const token = ++epoch.current;
    working.current = true;
    try { return await stage(still, ops, key, token); }
    finally { now.current.destination.release?.(key); if (token === epoch.current) working.current = false; }
  };
  const disabled = !props.active || !props.drawn || props.blocked || busy;
  const actions = <div className="browser-capture-actions" data-browser-capture="" data-testid="browser-capture-actions">
      <button type="button" disabled={disabled} onClick={e => void begin(false, e.nativeEvent)}>Annotate</button>
      <button type="button" disabled={disabled} onClick={e => void begin(true, e.nativeEvent)}>Send to chat</button>
      <label><input type="checkbox" checked={whole} disabled={disabled || !!still}
        onChange={e => { if (eligible()) setWhole(e.target.checked); }}
        onKeyDown={e => e.stopPropagation()} onKeyUp={e => e.stopPropagation()} /> Whole-page text</label>
      {busy && <span role="status">Capturing…</span>}
    </div>;
  return <>
    {props.active && props.drawn ? "controlsTarget" in props ? props.controlsTarget ? createPortal(actions, props.controlsTarget) : null : actions : null}
    {message && <div className="annotation-note" data-browser-capture="" role="status" data-testid="capture-notice">{message}</div>}
    {fallback && <button type="button" className="capture-remote" data-browser-capture="" disabled={disabled}
      onClick={() => { if (eligible()) { setFallback(false); props.onRemote(); } }}>Open in Remote</button>}
    {still && <Annotate key={still.capture_id} still={still} zoom={props.zoom}
      blocked={props.blocked || busy} active={props.active && props.drawn} notice={message}
      onClose={() => { if (eligible()) setStill(null); }} onStage={sendStill} />}
  </>;
}
