// The icon left of each file name in a File pane's tree, and the folder
// chevron (the owner's VS Code screenshot: `{}` for JSON, a C / C++ badge, a
// "lines of text" document for text and anything unknown, folders a chevron
// and no icon). Which icon is lib/fileIcons.ts's rule.
//
// Drawn here, as inline SVG, by hand: no icon font, no `<img>`, no request —
// the HUD's Content-Security-Policy would refuse one, and every headless
// suite fails on a CSP violation. Paths only, never SVG `<text>`, so a row's
// text stays its name. Each glyph is drawn in `currentColor`; the colour is a
// theme token (`ICONS[id].color`, read as a CSS variable), never a hex and
// never amber or red. Icons are decorative: `aria-hidden`, never focusable.

import { memo, type ReactNode } from "react";
import { ICONS, iconFor, type IconId } from "../lib/fileIcons";

/** An eight-toothed gear around the centre, as one path (the build icon). */
function gearPath(): string {
  const pt = (r: number, deg: number) => {
    const a = ((deg - 90) * Math.PI) / 180;
    return `${(8 + r * Math.cos(a)).toFixed(2)} ${(8 + r * Math.sin(a)).toFixed(2)}`;
  };
  const out: string[] = [];
  for (let k = 0; k < 8; k++) {
    const a = k * 45;
    out.push(pt(4.9, a - 13), pt(6.6, a - 8), pt(6.6, a + 8), pt(4.9, a + 13));
  }
  return `M${out.join("L")}Z`;
}

const dot = (cx: number, cy: number, r = 0.95) => <circle cx={cx} cy={cy} r={r} fill="currentColor" stroke="none" />;
const LINES = <path d="M3 4h10M3 7h10M3 10h10M3 13h6" />;
const S = "M13.3 5.1c-.4-1-1.2-1.6-2.2-1.6-1.2 0-2 .8-2 1.9 0 2.5 4.3 1.9 4.3 4.8 0 1.4-1 2.3-2.3 2.3-1.1 0-2-.6-2.3-1.6";

/** Every glyph on a 16×16 grid, stroked in currentColor unless a part says otherwise. */
const GLYPHS: Record<IconId, ReactNode> = {
  c: <path d="M11.25 4.75A4.6 4.6 0 1 0 11.25 11.25" strokeWidth={1.8} />,
  cpp: (
    <>
      <path d="M7.47 5.53A3.5 3.5 0 1 0 7.47 10.47" strokeWidth={1.7} />
      <path d="M10.4 6.7v2.6M9.1 8h2.6M13.8 6.7v2.6M12.5 8h2.6" />
    </>
  ),
  h: <path d="M4.5 3.5v9M11.5 3.5v9M4.5 8h7" strokeWidth={1.6} />,
  python: (
    <>
      <rect x="2.2" y="2.2" width="7.2" height="7.2" rx="2" />
      <rect x="6.6" y="6.6" width="7.2" height="7.2" rx="2" />
      {dot(4.7, 4.7)}
      {dot(11.3, 11.3)}
    </>
  ),
  js: <path d={`M6.4 3.5v6.7c0 1.5-.8 2.3-2.1 2.3-.9 0-1.6-.4-1.9-1.2 ${S}`} />,
  ts: <path d={`M1.8 3.5h5.6M4.6 3.5v9 ${S}`} />,
  react: (
    <>
      <ellipse cx="8" cy="8" rx="6.6" ry="2.5" />
      <ellipse cx="8" cy="8" rx="6.6" ry="2.5" transform="rotate(60 8 8)" />
      <ellipse cx="8" cy="8" rx="6.6" ry="2.5" transform="rotate(120 8 8)" />
      {dot(8, 8, 1.2)}
    </>
  ),
  json: (
    <path d="M6 2.5c-1.2 0-1.8.6-1.8 1.8v1.9c0 .9-.5 1.5-1.4 1.8.9.3 1.4.9 1.4 1.8v1.9c0 1.2.6 1.8 1.8 1.8M10 2.5c1.2 0 1.8.6 1.8 1.8v1.9c0 .9.5 1.5 1.4 1.8-.9.3-1.4.9-1.4 1.8v1.9c0 1.2-.6 1.8-1.8 1.8" />
  ),
  markdown: <path d="M1.6 11.8V4.4l3 3.9 3-3.9v7.4M11.9 4.4v7.2M9.6 9.3l2.3 2.4 2.3-2.4" />,
  html: <path d="M5.2 4.3 1.6 8l3.6 3.7M10.8 4.3 14.4 8l-3.6 3.7M9.1 3.3 6.9 12.7" />,
  css: <path d="M6.4 2.8 5.2 13.2M10.8 2.8 9.6 13.2M3 6h10.6M2.4 10h10.6" />,
  config: (
    <>
      <path d="M2.5 4.5h11M2.5 8h11M2.5 11.5h11" />
      {dot(5.5, 4.5, 1.7)}
      {dot(10.5, 8, 1.7)}
      {dot(7, 11.5, 1.7)}
    </>
  ),
  shell: <path d="M2.5 4.5 6 8l-3.5 3.5M8 12h5.5" strokeWidth={1.5} />,
  sql: (
    <>
      <ellipse cx="8" cy="4" rx="5" ry="1.9" />
      <path d="M3 4v8c0 1.05 2.24 1.9 5 1.9s5-.85 5-1.9V4M3 8c0 1.05 2.24 1.9 5 1.9s5-.85 5-1.9" />
    </>
  ),
  text: LINES,
  image: (
    <>
      <rect x="1.8" y="2.8" width="12.4" height="10.4" rx="1.6" />
      <path d="M2.2 12.2l3.9-4 3 3 1.8-1.8 3.1 2.9" />
      <circle cx="10.6" cy="5.9" r="1.2" />
    </>
  ),
  lock: (
    <>
      <path d="M2.5 3.5h11M2.5 6.5h11M2.5 9.5h4.5M10 10.4V9.3a1.6 1.6 0 0 1 3.2 0v1.1" />
      <rect x="8.8" y="10.4" width="5.6" height="4.1" rx=".8" fill="currentColor" stroke="none" />
    </>
  ),
  env: (
    <>
      <path d="M5.2 7.2V5.3a2.8 2.8 0 0 1 5.6 0v1.9M8 10.4v1.7" />
      <rect x="3.6" y="7.2" width="8.8" height="6.8" rx="1.3" />
      {dot(8, 9.9, 0.9)}
    </>
  ),
  git: (
    <>
      <path d="M8 1.4 14.6 8 8 14.6 1.4 8Z" />
      <path d="M6 5 8.2 7.2V11M8.2 7.2l2.2 2.2" />
      {dot(6, 5)}
      {dot(8.2, 11.2)}
      {dot(10.4, 9.4)}
    </>
  ),
  make: (
    <>
      <path d={gearPath()} />
      <circle cx="8" cy="8" r="2" />
    </>
  ),
  docker: (
    <>
      <path d="M1.4 10.4h13c-.6 2.6-3 4.2-6.8 4.2-3.3 0-5.6-1.6-6.2-4.2Z" />
      <path
        d="M2.6 6.9h2.4v2.4H2.6ZM5.5 6.9h2.4v2.4H5.5ZM8.4 6.9h2.4v2.4H8.4ZM5.5 4h2.4v2.4H5.5ZM8.4 4h2.4v2.4H8.4Z"
        fill="currentColor"
        stroke="none"
      />
    </>
  ),
  package: <path d="M8 1.8 14 5v6.1l-6 3.1-6-3.1V5ZM2 5l6 3.1L14 5M8 8.1v6.1" />,
  file: LINES,
};

/** The icon for a file named `name`; `policy` is the tree entry's (`env` wears the lock). */
export const FileIcon = memo(function FileIcon(props: { name: string; policy?: string | null }) {
  const id = iconFor(props.name, props.policy);
  return (
    <span className="ficon" data-icon={id} aria-hidden="true" style={{ color: `var(${ICONS[id].color})` }}>
      <svg
        viewBox="0 0 16 16"
        width="16"
        height="16"
        focusable="false"
        fill="none"
        stroke="currentColor"
        strokeWidth={1.35}
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        {GLYPHS[id]}
      </svg>
    </span>
  );
});

/** A folder's twistie: `>` collapsed, `v` expanded (VS Code's chevron, not a triangle). */
export function Chevron(props: { open: boolean }) {
  return (
    <svg className="chev" viewBox="0 0 10 10" width="10" height="10" aria-hidden="true" focusable="false">
      <path
        d={props.open ? "M2 3.5 5 6.5 8 3.5" : "M3.5 2 6.5 5 3.5 8"}
        fill="none"
        stroke="currentColor"
        strokeWidth={1.3}
        strokeLinecap="round"
        strokeLinejoin="round"
      />
    </svg>
  );
}
