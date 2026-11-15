// One outline family. Only code-owned geometry enters these decorative SVGs.
const NS = "http://www.w3.org/2000/svg";

/** Every SVG root this app builds in code. The hostile-text tests treat any other <svg> as injected
 *  markup: membership cannot be forged by server text, which only ever reaches the DOM as text. */
export const CODE_SVG = new WeakSet<Element>();
function icon(paths: string[]): SVGSVGElement {
  const svg = document.createElementNS(NS, "svg");
  CODE_SVG.add(svg);
  for (const [key, value] of Object.entries({ viewBox: "0 0 24 24", fill: "none", stroke: "currentColor",
    "stroke-width": "1.75", "stroke-linecap": "round", "stroke-linejoin": "round",
    "aria-hidden": "true", focusable: "false", class: "m-icon" })) svg.setAttribute(key, value);
  for (const d of paths) {
    const path = document.createElementNS(NS, "path");
    path.setAttribute("d", d);
    svg.append(path);
  }
  return svg;
}
export const inboxIcon = () => icon(["M4 4h16l2 10v6H2v-6L4 4Z", "M2 14h6l2 3h4l2-3h6"]);
export const chatsIcon = () => icon(["M3 3h13v10H7l-4 4V3Z", "M10 17h7l4 4V9h-2"]);
export const settingsIcon = () => icon(["M10 2h4l1 3 3 1 3 2-2 3v2l2 3-3 2-3 1-1 3h-4l-1-3-3-1-3-2 2-3v-2L3 8l3-2 3-1 1-3Z", "M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z"]);
export const lockIcon = () => icon(["M7 10V7a5 5 0 0 1 10 0v3", "M5 10h14v11H5V10Z"]);
export const unlockIcon = () => icon(["M7 10V7a5 5 0 0 1 9.6-2", "M5 10h14v11H5V10Z"]);
export const chevronIcon = () => icon(["m9 5 7 7-7 7"]);
export const backIcon = () => icon(["m15 5-7 7 7 7"]);
export const moreIcon = () => icon(["M5 12h.01M12 12h.01M19 12h.01"]);
export const micIcon = () => icon(["M9 5a3 3 0 0 1 6 0v7a3 3 0 0 1-6 0V5Z", "M5 10v2a7 7 0 0 0 14 0v-2M12 19v3M8 22h8"]);
export const sendIcon = () => icon(["M12 21V3m-7 7 7-7 7 7"]);
export const copyIcon = () => icon(["M9 8h12v14H9V8Z", "M6 17H3V2h12v3"]);
export const terminalIcon = () => icon(["m4 5 7 7-7 7M13 20h7"]);
export const sunIcon = () => icon(["M16 12a4 4 0 1 1-8 0 4 4 0 0 1 8 0Z", "M12 1v2m0 18v2M1 12h2m18 0h2M4 4l2 2m12 12 2 2M4 20l2-2M18 6l2-2"]);
