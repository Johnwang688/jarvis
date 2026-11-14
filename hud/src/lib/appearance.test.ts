import { describe, expect, it, vi } from "vitest";
import {
  ACCENT_CHROMA_FLOOR, ACCENT_MIN_CONTRAST, AMBER, APPEARANCE_EVENT, APPEARANCE_KEY, BASE_MAX_LUMINANCE, DEFAULT_ACCENT, DEFAULT_CODE_ACCENT,
  DEFAULT_APPEARANCE, DEFAULT_BASE, DEFAULT_COLOURS, DEFAULT_WASHES, MEANING_DELTA_E, RED, WARM_BAND, WASH_MAX_LUMINANCE,
  accentContrast, checkAccent, checkBase, checkWash, colourProps, deriveAccent, deriveCodeAccent, impersonatesMeaning, loadAppearance, normHex,
  clampDrift, parseAppearance, resolveColours, saveAppearance,
} from "./appearance";
import { deltaE, hexLch, lchHex, luminance, rgbHex } from "./oklch";
import { contrast } from "./editorTheme";
import { applyAppearance, MANAGED_PROPS } from "./appearanceApply";
import { DEFAULT_COOL, palette, setCoolHues } from "./orbpalette";
import css from "../theme.css?raw";

const store = (init: Record<string, string> = {}) => {
  const m = { ...init };
  return { getItem: (k: string) => (k in m ? m[k] : null), setItem: (k: string, v: string) => { m[k] = v; }, m };
};

describe("appearance preferences", () => {
  it("default to Fluid with both stirs on", () => {
    expect(loadAppearance(store())).toEqual({ motion: "fluid", stir: true, activity: true, drift: 1, rgb: false, rgbSpeed: 30 });
    expect(DEFAULT_APPEARANCE.motion).toBe("fluid");
  });

  it("fall back field by field on a corrupt or wrong-typed record", () => {
    for (const raw of ["{", "null", "[]", '"x"', "12", ""]) expect(parseAppearance(raw), raw).toEqual(DEFAULT_APPEARANCE);
    expect(parseAppearance('{"motion":"swirl","stir":"yes","activity":false}')).toEqual({ motion: "fluid", stir: true, activity: false, drift: 1, rgb: false, rgbSpeed: 30 });
    expect(parseAppearance('{"motion":"still","stir":false}')).toEqual({ motion: "still", stir: false, activity: true, drift: 1, rgb: false, rgbSpeed: 30 });
  });

  it("survive a storage that throws", () => {
    const bad = { getItem: () => { throw new Error("blocked"); }, setItem: () => { throw new Error("blocked"); } };
    expect(loadAppearance(bad)).toEqual(DEFAULT_APPEARANCE);
    expect(() => saveAppearance({ motion: "flow" }, bad)).not.toThrow();
  });

  it("save over what is there, keep AG-3's fields through a round trip, and tell the window", () => {
    const s = store({ [APPEARANCE_KEY]: JSON.stringify({ motion: "flow", accent: "#6fc3df", washes: ["#2a8fa0", "#3468c4", "#6450c4"] }) });
    const heard = vi.fn();
    window.addEventListener(APPEARANCE_EVENT, heard);
    const next = saveAppearance({ stir: false }, s);
    window.removeEventListener(APPEARANCE_EVENT, heard);
    expect(heard).toHaveBeenCalledTimes(1);
    expect(next).toMatchObject({ motion: "flow", stir: false, activity: true, accent: "#6fc3df" });
    expect(JSON.parse(s.m[APPEARANCE_KEY]!).washes).toEqual(["#2a8fa0", "#3468c4", "#6450c4"]);
  });

  it("ignore accent and washes of the wrong type", () => {
    expect(parseAppearance('{"accent":5,"washes":[1,2]}')).toEqual(DEFAULT_APPEARANCE);
  });

  it("saving keeps fields this HUD does not know (a later HUD's, a malformed accent) without applying them", () => {
    const s = store({ [APPEARANCE_KEY]: JSON.stringify({ motion: "flow", base: "url(x)", future: { a: 1 }, accent: 7 }) });
    const next = saveAppearance({ stir: false }, s);
    expect(next).toEqual({ motion: "flow", stir: false, activity: true, drift: 1, rgb: false, rgbSpeed: 30 });
    expect(JSON.parse(s.m[APPEARANCE_KEY]!)).toEqual({ motion: "flow", stir: false, activity: true, drift: 1, rgb: false, rgbSpeed: 30, base: "url(x)", future: { a: 1 }, accent: 7 });
  });
});

// ---- AG-3: colours ---------------------------------------------------------------------------------------

const root = (() => {
  const block = /:root\s*\{([\s\S]*?)\n\}/.exec(css.replace(/\/\*[\s\S]*?\*\//g, ""))!;
  return new Map([...block[1].matchAll(/(--[\w-]+)\s*:\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]));
})();
/** A token's value with whitespace and trailing zeros normalised, so "0.30" and "0.3" compare equal. */
const norm = (v: string) => v.replace(/\s+/g, " ").replace(/(\d)\.?0+\b(?!\d)/g, "$1").replace(/0\.([1-9])0\b/g, "0.$1").toLowerCase();

describe("input is #rrggbb only", () => {
  it("accepts six hex digits in either case and lower-cases them", () => {
    expect(normHex("#6FC3DF")).toBe("#6fc3df");
    expect(normHex("#6fc3df")).toBe("#6fc3df");
  });
  it("refuses everything else before it can reach a style property", () => {
    for (const bad of ["url(https://x/y)", "var(--accent)", "#fff", "#ffff", "red", "rgb(1,2,3)", "#12345g", " #aabbcc", "#aabbcc ", "#aabbcc\n",
      "#aabbccdd", "aabbcc", "#aabbcc;background:url(x)", "#aabbcc}", "", 5, null, undefined, {}, ["#aabbcc"]]) {
      expect(normHex(bad), String(bad)).toBeNull();
      for (const check of [checkAccent, checkWash, checkBase]) {
        const r = check(bad as never);
        expect(r.ok, `${String(bad)} via ${check.name}`).toBe(false);
        if (!r.ok) expect(r.reason).toMatch(/#rrggbb/);
      }
    }
  });
});

describe("the accent hue guard", () => {
  it("is built from --amber and --red: the band holds both with a margin", () => {
    expect(root.get("--amber")).toBe(AMBER);
    expect(root.get("--red")).toBe(RED);
    const red = hexLch(RED).h, amber = hexLch(AMBER).h;
    expect(red).toBeGreaterThan(WARM_BAND.from + 10);
    expect(amber).toBeLessThan(WARM_BAND.to - 10);
    expect(deltaE(AMBER, RED)).toBeGreaterThan(MEANING_DELTA_E);
  });

  it("refuses amber, red, orange, gold and coral accents with a reason, and nothing is shifted", () => {
    for (const bad of [AMBER, RED, "#ff8800", "#ffd700", "#ff7f50", "#fb923c", "#facc15", "#ef4444", "#ff9e3d", "#f5c542"]) {
      const r = checkAccent(bad);
      expect(r.ok, bad).toBe(false);
      if (!r.ok) expect(r.reason).toMatch(/amber or red/);
    }
  });

  it("refuses an accent within the tested distance of --red or --amber, even outside the hue band", () => {
    // Sweep the hue at red's own lightness and chroma: some hues outside the band are still too close.
    const red = hexLch(RED);
    let byDistanceOnly = 0;
    for (let h = 330; h <= 362; h++) {
      const hex = lchHex({ ...red, h: h % 360 });
      const lch = hexLch(hex);
      const inBand = lch.h >= WARM_BAND.from && lch.h <= WARM_BAND.to;
      const near = deltaE(hex, RED) < MEANING_DELTA_E;
      if (near && !inBand) {
        byDistanceOnly++;
        expect(checkAccent(hex).ok, hex).toBe(false);
      }
    }
    expect(byDistanceOnly).toBeGreaterThan(3);
    // And a colour just past the distance on the cool side is let through.
    const far = lchHex({ L: 0.8, C: red.C, h: 335 });
    expect(deltaE(far, RED)).toBeGreaterThanOrEqual(MEANING_DELTA_E);
    expect(checkAccent(far).ok).toBe(true);
  });

  it("every hue angle is refused if the pick or any relative impersonates a meaning colour", () => {
    for (let h = 0; h < 360; h += 3) {
      const hex = lchHex({ L: 0.78, C: 0.13, h });
      const lch = hexLch(hex);
      const inBand = lch.h >= WARM_BAND.from && lch.h <= WARM_BAND.to;
      const r = checkAccent(hex);
      if (inBand) expect(r.ok, `h=${h} ${hex}`).toBe(false);
      else {
        const d = deriveAccent(hex);
        const pigments = [hex, d.props["--accent-hi"], deriveCodeAccent(hex)["--syn-fn"], ...Object.values(d.orb).map((v) => rgbHex(v.split(",").map(Number) as [number, number, number]))];
        expect(r.ok, `h=${h} ${hex}`).toBe(!pigments.some((v) => impersonatesMeaning(v, ACCENT_CHROMA_FLOOR)));
      }
    }
  });

  it("lets low-chroma near-greys through whatever their hue angle", () => {
    for (let h = 0; h < 360; h += 30) {
      const hex = lchHex({ L: 0.8, C: ACCENT_CHROMA_FLOOR - 0.012, h });
      expect(hexLch(hex).C, hex).toBeLessThan(ACCENT_CHROMA_FLOOR);
      expect(checkAccent(hex).ok, `h=${h} ${hex}`).toBe(true);
    }
    expect(checkAccent("#b8b0a8").ok).toBe(true); // a warm grey
    expect(checkAccent("#cccccc").ok).toBe(true);
    // ...but the same hue at real chroma is refused.
    expect(checkAccent(lchHex({ L: 0.8, C: 0.12, h: 70 })).ok).toBe(false);
  });

  it("lets the default cyan, lavender, mint and rose through", () => {
    for (const ok of [DEFAULT_ACCENT, "#c0aef5", "#6dd0b0", "#a0c4ff", "#f0a0d0", "#9be3b4"]) {
      const r = checkAccent(ok);
      expect(r.ok, ok).toBe(true);
      if (r.ok) expect(r.value).toBe(ok);
    }
  });

  it("refuses safe-looking picks whose derived hue or distance crosses the boundary", () => {
    // hi: hue 104.8; fn: hue 103; composing: hue 75. hi of the second pick is only 0.117 from amber.
    for (const hex of ["#b0ae7e", "#b1ae76"]) {
      expect(impersonatesMeaning(hex, ACCENT_CHROMA_FLOOR)).toBe(false);
      expect(accentContrast(hex)).toBeGreaterThanOrEqual(3);
      expect(checkAccent(hex)).toMatchObject({ ok: false, reason: expect.stringContaining("amber or red") });
      expect(resolveColours({ accent: hex }).accent).toBe(DEFAULT_ACCENT);
    }
  });

  it("sweeps the band edges and near-greys: every accepted token and orb pigment stays safe", () => {
    let accepted = 0, nearGrey = 0;
    for (let h = 0; h < 360; h += 2) {
      if (h > 120 && h < 330) continue;
      for (const L of [0.6, 0.7, 0.74, 0.8, 0.86, 0.92, 0.98]) {
        for (const C of [0.01, 0.028, 0.039, 0.04, 0.041, 0.06, 0.08, 0.12, 0.2, 0.3]) {
          const hex = lchHex({ L, C, h });
          if (!checkAccent(hex).ok) continue;
          accepted++;
          if (hexLch(hex).C < ACCENT_CHROMA_FLOOR) nearGrey++;
          const d = deriveAccent(hex);
          // Alpha tokens share the accent pigment; the focus ring, badge and xterm cyan/cursor do too.
          const pigments = [hex, d.props["--accent-hi"], deriveCodeAccent(hex)["--syn-fn"], ...Object.values(d.orb).map((v) => rgbHex(v.split(",").map(Number) as [number, number, number]))];
          for (const value of pigments) expect(impersonatesMeaning(value, ACCENT_CHROMA_FLOOR), `${hex} -> ${value}`).toBe(false);
        }
      }
    }
    expect(accepted).toBeGreaterThan(500);
    expect(nearGrey).toBeGreaterThan(500);
  });
});

describe("the accent contrast guard", () => {
  it("refuses an accent under 3:1 on the glass pane over the brightest wash, with the ratio", () => {
    for (const dark of ["#1a3a4a", "#2f4f6a", "#334455", "#444444", "#5a4fa0"]) {
      expect(accentContrast(dark), dark).toBeLessThan(ACCENT_MIN_CONTRAST);
      const r = checkAccent(dark);
      expect(r.ok, dark).toBe(false);
      if (!r.ok) expect(r.reason).toMatch(/Too dark.*:1.*needs 3:1/);
    }
  });
  it("matches the glass tests' pane maths: the default accent measures what glass.test measures", () => {
    // Pane = tint .50 then the sheen's peak over the wash; the constants are pinned against theme.css here.
    expect(root.get("--glass-pane")).toBe("rgba(11, 19, 25, 0.50)");
    expect(root.get("--glass-sheen")).toMatch(/rgba\(255, 255, 255, 0\.09\)/);
    expect(accentContrast(DEFAULT_ACCENT)).toBeGreaterThanOrEqual(ACCENT_MIN_CONTRAST);
    expect(accentContrast(DEFAULT_ACCENT)).toBeLessThan(10);
  });
  it("is measured against the brightest wash in use, and the default brightest one", () => {
    expect(accentContrast("#808080", ["#000000", "#000000", "#000000"])).toBeCloseTo(accentContrast("#808080", [...DEFAULT_WASHES]), 6);
  });
  it("rechecks the current washes even when they satisfy the wash luminance cap", () => {
    const wash = checkWash("#ffffff");
    expect(wash.ok).toBe(true);
    if (!wash.ok) return;
    expect(checkAccent("#ababab").ok).toBe(true);
    expect(checkAccent("#ababab", [wash.value, wash.value, wash.value]).ok).toBe(false);
    expect(resolveColours({ accent: "#ababab", washes: [wash.value, wash.value, wash.value] }).accent).toBe(DEFAULT_ACCENT);
  });
});

describe("the wash clamp", () => {
  it("rechecks the rounded clamped wash against the chroma floor and hue band", () => {
    for (const hex of ["#dbb1b9", "#dbb1b8", "#cca2a3"]) {
      expect(impersonatesMeaning(hex, 0.05)).toBe(false);
      expect(checkWash(hex)).toMatchObject({ ok: false, reason: expect.stringContaining("amber or red") });
      expect(resolveColours({ washes: [hex, hex, hex] }).washes).toEqual(DEFAULT_WASHES);
    }
  });
  it("darkens a too-bright wash (not refuses it) to at most the default brightest wash's luminance", () => {
    expect(WASH_MAX_LUMINANCE).toBeCloseTo(0.227, 2);
    for (const bright of ["#ffffff", "#80ffff", "#a0e0ff", "#c0aef5", "#60ff90", "#ffffcc00".slice(0, 7), "#9999ff", "#e0b0ff"]) {
      if (impersonatesMeaning(bright, 0.05)) continue;
      const r = checkWash(bright);
      expect(r.ok, bright).toBe(true);
      if (!r.ok) continue;
      expect(luminance(r.value), bright).toBeLessThanOrEqual(WASH_MAX_LUMINANCE);
      expect(r.clamped, bright).toBe(true);
      expect(r.note).toMatch(/Darkened to #[0-9a-f]{6}/);
      // Hue is kept (for a colour with real chroma) and it is only ever darker.
      const a = hexLch(bright), b = hexLch(r.value);
      expect(b.L).toBeLessThan(a.L);
      if (a.C > 0.05 && b.C > 0.03) expect(Math.abs(((a.h - b.h + 540) % 360) - 180)).toBeLessThan(8);
    }
  });
  it("leaves a wash that is already dark enough exactly as picked", () => {
    for (const w of DEFAULT_WASHES) {
      const r = checkWash(w);
      expect(r).toEqual({ ok: true, value: w, clamped: false });
    }
  });
  it("caps every hue, at any chroma, so --text, --text-muted and the accent keep their D-8 floors on the pane", () => {
    const tint = [11, 19, 25, 0.5], sheen = 0.09;
    const pane = (hex: string) => {
      const w = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16));
      return "#" + [0, 1, 2].map((i) => Math.round(255 * sheen + (tint[i] * tint[3] + w[i] * (1 - tint[3])) * (1 - sheen)).toString(16).padStart(2, "0")).join("");
    };
    for (let h = 0; h < 360; h += 10) {
      for (const C of [0.02, 0.08, 0.16, 0.3]) {
        const hex = lchHex({ L: 0.97, C, h });
        const r = checkWash(hex);
        if (!r.ok) continue; // warm hues are refused
        expect(luminance(r.value), hex).toBeLessThanOrEqual(WASH_MAX_LUMINANCE);
        const p = pane(r.value);
        expect(contrast(root.get("--text")!, p), `--text over ${hex}->${r.value}`).toBeGreaterThanOrEqual(4.5);
        expect(contrast(root.get("--text-muted")!, p), `--text-muted over ${hex}->${r.value}`).toBeGreaterThanOrEqual(3);
        expect(contrast(DEFAULT_ACCENT, p), `accent over ${hex}->${r.value}`).toBeGreaterThanOrEqual(3);
      }
    }
  });
  it("refuses an amber or red wash at real chroma, with the reason, and lets a dull dark one through", () => {
    for (const bad of ["#ff8800", "#c07000", "#d04040", AMBER, RED, "#b8860b"]) {
      const r = checkWash(bad);
      expect(r.ok, bad).toBe(false);
      if (!r.ok) expect(r.reason).toMatch(/amber or red/);
    }
    expect(checkWash("#3a3430").ok).toBe(true); // a dull warm dark: just brown-grey
  });
});

describe("the base clamp", () => {
  it("clamps a bright base to 1.5x the default base's luminance, and leaves darker ones alone", () => {
    expect(BASE_MAX_LUMINANCE).toBeCloseTo(luminance(DEFAULT_BASE) * 1.5, 8);
    for (const bright of ["#808080", "#ffffff", "#2040a0", "#40a0ff"]) {
      const r = checkBase(bright);
      expect(r.ok, bright).toBe(true);
      if (r.ok) {
        expect(luminance(r.value), bright).toBeLessThanOrEqual(BASE_MAX_LUMINANCE);
        expect(r.clamped).toBe(true);
      }
    }
    for (const dark of ["#000000", "#05080a", DEFAULT_BASE, "#0c1418"]) {
      expect(checkBase(dark)).toEqual({ ok: true, value: dark, clamped: false });
    }
    expect(checkBase("#5a1010").ok).toBe(false); // a dark red base would read as an error
  });
});

describe("the accent family is derived in OKLCH from one pick", () => {
  it("reproduces today's default tokens from the default accent", () => {
    const d = deriveAccent(DEFAULT_ACCENT);
    const props = { ...d.props, ...deriveCodeAccent(DEFAULT_CODE_ACCENT) };
    for (const k of ["--accent", "--accent-hi", "--accent-soft", "--accent-line", "--accent-sel", "--accent-hover", "--accent-badge", "--focus-ring", "--badge-fast", "--code-accent", "--code-accent-hi", "--syn-fn"]) {
      expect(norm(props[k]), k).toBe(norm(root.get(k)!));
    }
    expect(colourProps(DEFAULT_COLOURS)["--wash-1"]).toBe(root.get("--wash-1"));
    expect(colourProps(DEFAULT_COLOURS)["--wash-2"]).toBe(root.get("--wash-2"));
    expect(colourProps(DEFAULT_COLOURS)["--wash-3"]).toBe(root.get("--wash-3"));
    expect(colourProps(DEFAULT_COLOURS)["--base"]).toBe(root.get("--base"));
    expect(root.get("--bg-0")).toBe(root.get("--base"));
    expect(d.orb).toEqual(DEFAULT_COOL);
  });
  it("keeps the same relationships for another pick: lighter hi, quieter fn, same hue family, never warm", () => {
    for (const accent of ["#c0aef5", "#6dd0b0", "#a0c4ff", "#f0a0d0"]) {
      const d = deriveAccent(accent);
      const code = deriveCodeAccent(accent);
      const a = hexLch(accent), hi = hexLch(d.props["--accent-hi"]), fn = hexLch(code["--syn-fn"]);
      expect(hi.L, accent).toBeGreaterThan(a.L);
      expect(fn.C, accent).toBeLessThan(a.C);
      for (const f of [hi, fn]) expect(Math.abs(((f.h - a.h + 540) % 360) - 180), accent).toBeLessThan(40);
      for (const hex of [d.props["--accent-hi"], code["--syn-fn"]]) expect(impersonatesMeaning(hex, ACCENT_CHROMA_FLOOR), hex).toBe(false);
      expect(d.props["--focus-ring"]).toBe(`0 0 0 2px ${accent}`);
      expect(d.props["--badge-fast"]).toBe(accent);
      expect(d.props["--accent-soft"]).toMatch(/^rgba\(\d+, \d+, \d+, 0\.14\)$/);
    }
  });
  it("only ever emits #rrggbb, rgba(n, n, n, a), a ring built from #rrggbb, or an r,g,b triple", () => {
    const re = /^(#[0-9a-f]{6}|rgba\(\d{1,3}, \d{1,3}, \d{1,3}, 0\.\d+\)|0 0 0 2px #[0-9a-f]{6})$/;
    for (const accent of ["#c0aef5", "#ffffff", "#bbbbbb", DEFAULT_ACCENT]) {
      const d = deriveAccent(accent);
      for (const [k, v] of Object.entries(d.props)) expect(v, k).toMatch(re);
      for (const t of Object.values(d.orb)) expect(t).toMatch(/^\d{1,3},\d{1,3},\d{1,3}$/);
    }
  });
});

describe("resolving a stored record", () => {
  it("falls back, field by field, when the stored colours are unsafe", () => {
    const c = resolveColours({ accent: AMBER, washes: ["url(x)", "#ffffff", RED], base: "#ffffff" });
    expect(c.accent).toBe(DEFAULT_ACCENT);
    expect(c.washes[0]).toBe(DEFAULT_WASHES[0]);
    expect(c.washes[2]).toBe(DEFAULT_WASHES[2]);
    expect(luminance(c.washes[1])).toBeLessThanOrEqual(WASH_MAX_LUMINANCE);
    expect(luminance(c.base)).toBeLessThanOrEqual(BASE_MAX_LUMINANCE);
    // The editor's colour is read by name, not enumerated (resolveColours): the five-colour palette is the rest.
    expect(resolveColours({})).toEqual({ accent: DEFAULT_COLOURS.accent, washes: DEFAULT_COLOURS.washes, base: DEFAULT_COLOURS.base });
    expect(resolveColours({}).codeAccent).toBe(DEFAULT_COLOURS.codeAccent);
  });
  it("parse keeps only well-formed #rrggbb, and a corrupt record falls back to the defaults", () => {
    expect(parseAppearance('{"accent":"#C0AEF5","base":"#0c1418","washes":["#112233","#445566","#778899"]}')).toEqual({
      ...DEFAULT_APPEARANCE, accent: "#c0aef5", base: "#0c1418", washes: ["#112233", "#445566", "#778899"],
    });
    for (const raw of ['{"accent":"red","base":"var(--x)","washes":["url(x)","#fff",3]}', '{"washes":"#112233"}', '{"washes":["#112233"]}', "{", "[]"]) {
      expect(parseAppearance(raw), raw).toEqual(DEFAULT_APPEARANCE);
    }
    expect(parseAppearance('{"washes":["#112233","bad","#778899"]}').washes).toEqual(["#112233", DEFAULT_WASHES[1], "#778899"]);
  });
  it("round-trips through storage, and a reset clears every colour (a stored copy too)", () => {
    const s = store();
    saveAppearance({ accent: "#c0aef5", washes: ["#112233", "#445566", "#778899"], base: "#0c1418" }, s);
    expect(loadAppearance(s)).toMatchObject({ accent: "#c0aef5", base: "#0c1418", washes: ["#112233", "#445566", "#778899"] });
    saveAppearance({ accent: undefined, washes: undefined, base: undefined }, s);
    const saved = JSON.parse(s.m[APPEARANCE_KEY]!);
    expect(saved).toEqual({ motion: "fluid", stir: true, activity: true, drift: 1, rgb: false, rgbSpeed: 30 });
    expect(loadAppearance(s)).toEqual(DEFAULT_APPEARANCE);
  });
});

describe("applying to the window", () => {
  const el = () => document.createElement("div");
  it("sets the derived tokens and the orb's hues, and a default record sets nothing", () => {
    const r = el();
    applyAppearance({ ...DEFAULT_APPEARANCE, accent: "#c0aef5", washes: ["#112233", "#445566", "#335577"], base: "#0c1418" }, r);
    expect(r.style.getPropertyValue("--accent")).toBe("#c0aef5");
    expect(r.style.getPropertyValue("--focus-ring")).toBe("0 0 0 2px #c0aef5");
    expect(r.style.getPropertyValue("--wash-2")).toBe("#445566");
    expect(r.style.getPropertyValue("--base")).toBe("#0c1418");
    expect(palette("thinking").hue).toBe("192,174,245");
    applyAppearance(DEFAULT_APPEARANCE, r);
    expect(r.getAttribute("style") ?? "").toBe("");
    expect(palette("thinking").hue).toBe(DEFAULT_COOL.base);
    setCoolHues(DEFAULT_COOL);
  });
  it("never writes a hostile stored value: only #rrggbb-derived text reaches the style", () => {
    const r = el();
    applyAppearance({ ...DEFAULT_APPEARANCE, accent: "red;background:url(x)", washes: ["var(--x)", "url(//evil)", "expression(1)"], base: "#fff" } as never, r);
    expect(r.getAttribute("style") ?? "").toBe("");
    // An amber accent that got into storage by hand is not applied either.
    applyAppearance({ ...DEFAULT_APPEARANCE, accent: AMBER }, r);
    expect(r.getAttribute("style") ?? "").toBe("");
    for (const k of MANAGED_PROPS) expect(r.style.getPropertyValue(k)).toBe("");
  });
  it("the orb keeps amber and red whatever the accent", () => {
    setCoolHues(deriveAccent("#c0aef5").orb);
    expect(palette("tool").hue).toBe("251,191,36");
    expect(palette("approval").hue).toBe("251,191,36");
    expect(palette("error").hue).toBe("248,113,113");
    setCoolHues(DEFAULT_COOL);
    setCoolHues({ base: "javascript:1", hi: "1,2,3", composing: "1,2,3" });
    expect(palette("idle").hue).toBe(DEFAULT_COOL.base);
  });
});

describe("the hue guard and the wash clamp are the boundary", () => {
  it("the accent guard refuses the meaning colours themselves", () => {
    expect(impersonatesMeaning(AMBER, ACCENT_CHROMA_FLOOR)).toBe(true);
    expect(impersonatesMeaning(RED, ACCENT_CHROMA_FLOOR)).toBe(true);
    expect(checkAccent(AMBER).ok).toBe(false);
    expect(checkAccent(RED).ok).toBe(false);
  });
  it("a bright wash comes out dark", () => {
    const r = checkWash("#ffffff");
    expect(r.ok && luminance(r.value)).toBeLessThanOrEqual(WASH_MAX_LUMINANCE);
    expect(r.ok && r.value).not.toBe("#ffffff");
  });
});

describe("natural drift storage", () => {
  const drift = (raw: string | null) => parseAppearance(raw).drift;
  it("a missing or garbage value is 1; out-of-range values are clamped to 0..2", () => {
    expect(drift(null)).toBe(1);
    expect(drift('{"motion":"flow"}')).toBe(1);
    for (const bad of ['"fast"', "null", "[]", "{}", "true"]) expect(drift(`{"drift":${bad}}`), bad).toBe(1);
    expect(clampDrift(NaN)).toBe(1);
    expect(clampDrift(Infinity)).toBe(1);
    expect(drift('{"drift":5}')).toBe(2);
    expect(drift('{"drift":-1}')).toBe(0);
    expect(drift('{"drift":0}')).toBe(0);
    expect(drift('{"drift":0.5}')).toBe(0.5);
  });
  it("saving a drift keeps unknown fields and clamps what it stores", () => {
    const m: Record<string, string> = { [APPEARANCE_KEY]: '{"future":{"a":1},"drift":0.3}' };
    const s = { getItem: (k: string) => m[k] ?? null, setItem: (k: string, v: string) => void (m[k] = v) };
    expect(saveAppearance({ drift: 9 }, s).drift).toBe(2);
    expect(JSON.parse(m[APPEARANCE_KEY]!)).toMatchObject({ future: { a: 1 }, drift: 2 });
    saveAppearance({ drift: NaN }, s);
    expect(JSON.parse(m[APPEARANCE_KEY]!).drift).toBe(1);
  });
});

describe("colour presets", async () => {
  const A = await import("./appearance");
  it("every preset passes every guard as it stands (no clamp, no refusal) and survives resolveColours", () => {
    for (const p of A.COLOUR_PRESETS) {
      expect(A.checkPreset(p), p.id).toBeNull();
      for (const c of [p.accent, p.base, ...p.washes]) expect(A.normHex(c), `${p.id} ${c}`).toBe(c);
      const r = A.resolveColours({ accent: p.accent, washes: [...p.washes], base: p.base });
      expect(A.matchingPreset(r)?.id, p.id).toBe(p.id);
    }
  });
  it("ids and names are unique, and the first preset is the default", () => {
    const ids = A.COLOUR_PRESETS.map((p) => p.id), names = A.COLOUR_PRESETS.map((p) => p.name);
    expect(new Set(ids).size).toBe(ids.length);
    expect(new Set(names).size).toBe(names.length);
    expect(A.matchingPreset(A.DEFAULT_COLOURS)?.id).toBe(A.COLOUR_PRESETS[0].id);
    expect(A.matchingPreset(A.resolveColours({}))?.id).toBe("aurora");
  });
  it("checkPreset refuses a preset a hand pick would refuse or darken", () => {
    const base = A.COLOUR_PRESETS[1];
    expect(A.checkPreset({ ...base, accent: A.AMBER })).not.toBeNull();
    expect(A.checkPreset({ ...base, washes: ["#ff7a00", base.washes[1], base.washes[2]] })).not.toBeNull();
    expect(A.checkPreset({ ...base, washes: ["#9fe8ff", base.washes[1], base.washes[2]] })).not.toBeNull(); // would clamp
    expect(A.checkPreset({ ...base, base: "#405060" })).not.toBeNull(); // would clamp
    expect(A.checkPreset({ ...base, accent: "#101418" })).not.toBeNull(); // too dark
  });
});
