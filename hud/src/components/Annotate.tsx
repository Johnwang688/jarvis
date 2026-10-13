// BR-4: drawing on a frozen owner capture never forwards input to the live page.
import { useLayoutEffect, useRef, useState } from "react";
import {
  INK, LINE_CSS, MAX_OPS, TEXT_CSS, TEXT_MAX, TOOLS, addPoint, clear, clientToImage, commit,
  fitStill, inkWidth, render, shapeOp, startStroke, textOp, undo, type Fit, type Op, type Pt, type Tool,
} from "../lib/annotate";
import { loadCaptureImage, type CaptureStill } from "../lib/pageCapture";

export function Annotate(props: {
  still: CaptureStill; zoom: number; blocked: boolean; active: boolean; notice?: string;
  onClose: () => void; onStage: (ops: readonly Op[]) => Promise<boolean>;
}) {
  const view = useRef<HTMLDivElement>(null);
  const canvas = useRef<HTMLCanvasElement>(null);
  const image = useRef<HTMLImageElement | null>(null);
  const fit = useRef<Fit>({ x: 0, y: 0, scale: 1 });
  const drag = useRef<{ id: number; from: Pt; tool: Exclude<Tool, "text">; op: Op; colour: string; width: number } | null>(null);
  const textInput = useRef<HTMLInputElement>(null);
  const textBox = useRef<HTMLDivElement>(null);
  const textAt = useRef<Pt | null>(null);
  const [tool, setTool] = useState<Tool>("pen");
  const [colour, setColour] = useState<string>(INK[0].value);
  const [ops, setOps] = useState<Op[]>([]);
  const marks = useRef(ops); marks.current = ops;
  const [text, setText] = useState<{ at: Pt; value: string; size: number; colour: string } | null>(null);
  textAt.current = text?.at ?? null;
  const [error, setError] = useState("");
  const [ready, setReady] = useState(false);
  const [busy, setBusy] = useState(false);
  const sending = useRef(false);
  const now = useRef(props); now.current = props;
  const unavailable = () => now.current.blocked || !now.current.active || sending.current;
  const redraw = () => {
    const v = view.current, c = canvas.current, img = image.current;
    if (!v || !c || !img) return;
    fit.current = fitStill({ width: v.clientWidth, height: v.clientHeight }, props.still.image);
    const f = fit.current;
    if (textBox.current && textAt.current) {
      textBox.current.style.left = Math.max(0, Math.min(v.clientWidth - textBox.current.offsetWidth, f.x + textAt.current[0] * f.scale)) + "px";
      textBox.current.style.top = Math.max(0, Math.min(v.clientHeight - textBox.current.offsetHeight, f.y + textAt.current[1] * f.scale)) + "px";
    }
    c.style.left = f.x + "px"; c.style.top = f.y + "px";
    c.style.width = props.still.image.width * f.scale + "px";
    c.style.height = props.still.image.height * f.scale + "px";
    const ctx = c.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, c.width, c.height);
    ctx.drawImage(img, 0, 0);
    render(ctx, drag.current ? [...marks.current, drag.current.op] : marks.current, 1);
  };
  const paint = useRef(redraw); paint.current = redraw;
  const cancelDrag = () => {
    const d = drag.current; drag.current = null;
    if (d && view.current?.hasPointerCapture(d.id)) view.current.releasePointerCapture(d.id);
    paint.current();
  };
  useLayoutEffect(() => {
    let alive = true;
    void loadCaptureImage(props.still).then(img => {
      if (!alive) { img.removeAttribute("src"); return; }
      image.current = img; setReady(true); paint.current();
    }).catch(() => { if (alive) setError("This capture image could not be read. Capture it again."); });
    const node = view.current;
    const observer = new ResizeObserver(() => paint.current());
    if (node) observer.observe(node);
    return () => {
      alive = false; observer.disconnect(); cancelDrag();
      image.current?.removeAttribute("src"); image.current = null;
    };
  }, [props.still]);
  useLayoutEffect(() => { paint.current(); }, [ops, props.zoom, props.active, !!text]);
  useLayoutEffect(() => {
    if (props.blocked || !props.active) cancelDrag();
  }, [props.blocked, props.active]);
  useLayoutEffect(() => {
    if (text && !props.blocked && props.active) textInput.current?.focus({ preventScroll: true });
  }, [!!text]);
  const point = (e: { clientX: number; clientY: number }): Pt =>
    clientToImage({ x: e.clientX, y: e.clientY }, view.current!.getBoundingClientRect(), props.zoom, fit.current, props.still.image);
  const finishText = () => {
    if (!text || unavailable()) return;
    setOps(old => commit(old, textOp(text.at, text.value, text.colour, text.size)));
    setText(null);
  };
  const stage = async () => {
    if (unavailable() || !ready || text || drag.current) return;
    sending.current = true; setBusy(true); setError("");
    try { if (await props.onStage(marks.current.slice())) props.onClose(); }
    catch (e) { setError(e instanceof Error ? e.message : "This capture could not be attached."); }
    finally { sending.current = false; setBusy(false); }
  };
  const disabled = props.blocked || !props.active || busy;
  return <section className="browser-annotate" data-testid="browser-annotate" aria-label="Annotate captured page"
    {...(disabled ? { inert: "" } as Record<string, string> : {})}
    onKeyDown={e => {
      e.stopPropagation();
      if (unavailable()) return;
      if (e.key === "Escape") { e.preventDefault(); if (text) setText(null); else props.onClose(); }
    }} onKeyUp={e => e.stopPropagation()}>
    <div className="annotation-toolbar">
      <span className="annotation-caption">Captured view</span>
      {TOOLS.map(t => <button type="button" key={t} aria-pressed={tool === t} disabled={disabled || !ready}
        onClick={() => { if (!unavailable()) { finishText(); setTool(t); } }}>{t[0].toUpperCase() + t.slice(1)}</button>)}
      <span className="annotation-inks" role="group" aria-label="Ink colour">
        {INK.map(c => <button type="button" key={c.name} aria-label={c.name + " ink"} aria-pressed={colour === c.value}
          disabled={disabled} style={{ "--ink": c.value } as React.CSSProperties}
          onClick={() => { if (!unavailable()) setColour(c.value); }}><span /></button>)}
      </span>
      <button type="button" disabled={disabled || !ops.length} onClick={() => { if (!unavailable()) setOps(undo); }}>Undo</button>
      <button type="button" disabled={disabled || !ops.length} onClick={() => { if (!unavailable()) setOps(clear()); }}>Clear</button>
      <button type="button" data-testid="capture-send" disabled={disabled || !ready || !!text}
        onClick={() => void stage()}>{busy ? "Attaching…" : "Send to chat"}</button>
      <button type="button" disabled={disabled} onClick={() => { if (!unavailable()) props.onClose(); }}>Done</button>
    </div>
    <div className="annotation-meta" title={props.still.url}>
      {props.still.title || props.still.url} · {props.still.whole ? "Whole-page text" : "Visible text"} · Stages two attachments
    </div>
    <div className="annotation-view" ref={view} data-testid="annotation-canvas" data-marks={ops.length}
      onPointerDown={e => {
        e.stopPropagation();
        if (unavailable() || !ready || e.button !== 0 || drag.current || text || ops.length >= MAX_OPS) return;
        e.preventDefault(); const at = point(e);
        if (tool === "text") { setText({ at, value: "", colour, size: inkWidth(TEXT_CSS, fit.current) }); return; }
        const width = inkWidth(LINE_CSS, fit.current);
        const op = tool === "pen" ? startStroke(at, colour, width) : shapeOp(tool, at, at, { colour, width });
        drag.current = { id: e.pointerId, from: at, tool, op, colour, width };
        e.currentTarget.setPointerCapture(e.pointerId); paint.current();
      }}
      onPointerMove={e => {
        e.stopPropagation(); const d = drag.current;
        if (unavailable() || !d || d.id !== e.pointerId) return;
        const at = point(e);
        d.op = d.tool === "pen" ? addPoint(d.op, at) : shapeOp(d.tool, d.from, at, { colour: d.colour, width: d.width, shift: e.shiftKey });
        paint.current();
      }}
      onPointerUp={e => {
        e.stopPropagation(); const d = drag.current;
        if (!d || d.id !== e.pointerId) return;
        if (!unavailable()) {
          const at = point(e);
          const op = d.tool === "pen" ? addPoint(d.op, at) : shapeOp(d.tool, d.from, at, { colour: d.colour, width: d.width, shift: e.shiftKey });
          setOps(old => commit(old, op));
        }
        cancelDrag();
      }}
      onPointerCancel={cancelDrag} onLostPointerCapture={cancelDrag}>
      <canvas ref={canvas} width={props.still.image.width} height={props.still.image.height} />
      {text && <div className="annotation-text" ref={textBox}
        onPointerDown={e => e.stopPropagation()}>
        <input ref={textInput} aria-label="Annotation text" maxLength={TEXT_MAX} value={text.value} disabled={disabled}
          onChange={e => setText({ ...text, value: e.target.value })}
          onKeyDown={e => { e.stopPropagation(); if (e.key === "Enter") { e.preventDefault(); finishText(); }
            if (e.key === "Escape" && !unavailable()) { e.preventDefault(); setText(null); } }} />
        <button type="button" disabled={disabled} onClick={finishText}>Add</button>
      </div>}
    </div>
    {props.notice && <div className="annotation-note" role="status">{props.notice}</div>}
    {error && <div className="annotation-note err" role="status">{error}</div>}
    {props.still.notes.map((note, i) => <div className="annotation-note muted" key={i}>{note}</div>)}
  </section>;
}
