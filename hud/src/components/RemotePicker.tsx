import { useLayoutEffect, useRef, useState, type ReactElement } from "react";
import { pickerPlacement, validPickerValue, type Picker, type PickerRect, type SelectPicker } from "../lib/remotepicker";
import "./RemotePicker.css";

/**
 * B-PICKERS: the owner's overlay for a native picker the Remote screencast
 * never draws. **It never takes the keyboard by itself**: nothing here calls
 * `focus()`. Keys the owner types stay in the Remote proxy, which routes them
 * to this overlay while it is open (RemoteView). The listbox's options keep
 * focus where it is (`mousedown` default prevented); the HUD's own date and
 * colour inputs take focus only from the owner's own click on them. Page text
 * is React text only. Mounted only while the stream is live and unblocked.
 */
export interface RemotePickerProps {
  picker: Picker; anchor: PickerRect; bounds: { width: number; height: number };
  highlight: number; value: string; blocked: boolean;
  onHighlight: (position: number) => void; onValue: (value: string) => void;
  onPick: (choice: { index: number } | { value: string }) => void; onCancel: () => void;
}
const TITLES: Record<string, string> = {
  select: "Choose an option", date: "Choose a date", time: "Choose a time",
  "datetime-local": "Choose a date and time", month: "Choose a month", week: "Choose a week", color: "Choose a colour",
};
export function RemotePicker(props: RemotePickerProps) {
  const box = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 220, height: 240 });
  useLayoutEffect(() => {
    const node = box.current;
    if (!node) return;
    const next = { width: node.offsetWidth, height: node.scrollHeight };
    if (next.width !== size.width || next.height !== size.height) setSize(next);
  });
  const at = pickerPlacement(props.anchor, size, props.bounds);
  const { picker } = props;
  const stop = (event: React.SyntheticEvent) => event.stopPropagation();
  return <div ref={box} className={"remotepicker" + (at.above ? " above" : "")} data-testid="remote-picker"
    data-kind={picker.kind} role={picker.kind === "select" ? "presentation" : "dialog"}
    aria-label={TITLES[picker.kind]}
    style={{ left: at.x, top: at.y, minWidth: Math.min(at.width, props.bounds.width), maxHeight: at.maxHeight }}
    onMouseDown={stop} onMouseUp={stop} onMouseMove={stop} onPointerDown={stop} onWheel={stop}
    onContextMenu={event => { event.preventDefault(); event.stopPropagation(); }}>
    {picker.kind === "select"
      ? <SelectList {...props} picker={picker} />
      : <ValueForm {...props} />}
  </div>;
}

function SelectList(props: RemotePickerProps & { picker: SelectPicker }) {
  const { picker, highlight, blocked } = props;
  const list = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    // Keep the highlighted option in view inside the overlay only.
    const node = list.current?.querySelector<HTMLElement>(`[data-position="${highlight}"]`);
    const parent = list.current;
    if (!node || !parent) return;
    if (node.offsetTop < parent.scrollTop) parent.scrollTop = node.offsetTop;
    else if (node.offsetTop + node.offsetHeight > parent.scrollTop + parent.clientHeight)
      parent.scrollTop = node.offsetTop + node.offsetHeight - parent.clientHeight;
  }, [highlight]);
  const rows: ReactElement[] = [];
  let group = -1;
  picker.items.forEach((item, position) => {
    if (item.g !== group) {
      group = item.g;
      if (group >= 0) rows.push(<div key={`g${position}`} className="remotepicker-group" role="presentation">{picker.groups[group]}</div>);
    }
    const chosen = item.i === picker.selected;
    rows.push(<div key={item.i} id={`remote-picker-${picker.id}-${item.i}`} role="option" data-position={position}
      data-index={item.i} aria-selected={position === highlight} aria-disabled={item.disabled || blocked}
      className={"remotepicker-option" + (position === highlight ? " active" : "") + (chosen ? " chosen" : "")
        + (item.g >= 0 ? " grouped" : "")}
      onMouseDown={event => event.preventDefault()}
      onMouseEnter={() => { if (!item.disabled) props.onHighlight(position); }}
      onClick={event => { event.stopPropagation(); if (!item.disabled && !blocked) props.onPick({ index: item.i }); }}>
      <span className="remotepicker-check" aria-hidden="true">{chosen ? "✓" : ""}</span>
      <span className="remotepicker-label">{item.label || " "}</span>
    </div>);
  });
  const hidden = picker.total - picker.shown;
  return <>
    <div ref={list} className="remotepicker-list" role="listbox" aria-label={TITLES.select}
      aria-activedescendant={picker.items[highlight] ? `remote-picker-${picker.id}-${picker.items[highlight].i}` : undefined}>
      {rows}
    </div>
    {hidden > 0 ? <div className="remotepicker-note small muted" role="note">{`${hidden} more option${hidden === 1 ? "" : "s"} not shown`}</div> : null}
  </>;
}

function ValueForm(props: RemotePickerProps) {
  const { picker, value, blocked } = props;
  if (picker.kind === "select") return null;
  const valid = validPickerValue(picker.kind, value);
  const clearable = picker.kind !== "color" && !picker.required;
  const set = () => { if (!blocked && valid) props.onPick({ value }); };
  return <div className="remotepicker-form">
    <input type={picker.kind} aria-label={TITLES[picker.kind]} data-testid="remote-picker-input" value={value}
      disabled={blocked} autoFocus={false}
      {...(picker.kind !== "color" ? { min: picker.min || undefined, max: picker.max || undefined, step: picker.step || undefined } : {})}
      onChange={event => props.onValue(event.target.value)}
      onKeyDown={event => {
        if (event.key === "Escape" && document.getElementById("authveil")) return;
        event.stopPropagation();
        if (event.key === "Enter") { event.preventDefault(); set(); }
        if (event.key === "Escape") { event.preventDefault(); props.onCancel(); }
      }} />
    {picker.kind === "color" ? <span className="remotepicker-hex mono">{valid ? value : ""}</span> : null}
    <div className="remotepicker-actions">
      <button type="button" data-testid="remote-picker-set" disabled={blocked || !valid}
        onMouseDown={event => event.preventDefault()} onClick={set}>Set</button>
      {clearable ? <button type="button" data-testid="remote-picker-clear" disabled={blocked}
        onMouseDown={event => event.preventDefault()}
        onClick={() => { if (!blocked) props.onPick({ value: "" }); }}>Clear</button> : null}
      <button type="button" data-testid="remote-picker-cancel" disabled={blocked}
        onMouseDown={event => event.preventDefault()} onClick={() => props.onCancel()}>Cancel</button>
    </div>
  </div>;
}
