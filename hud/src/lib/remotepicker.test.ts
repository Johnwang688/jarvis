import {
  parsePicker, parsePickerClosed, validPickerValue, initialHighlight, moveHighlight, edgeHighlight, typeahead,
  pickerKey, pickerPlacement, PickerGesture, PICKER_LABEL_MAX, PICKER_OPTIONS_MAX, PICKER_GESTURE_MS,
  type SelectPicker,
} from "./remotepicker";

const rect = { x: 10, y: 20, width: 220, height: 30 };
const select = (over: Record<string, unknown> = {}) => ({
  t: "picker", id: "0123abcd", kind: "select", rect,
  items: [{ i: 0, label: "Alpha", disabled: false, g: -1 }, { i: 1, label: "Bravo", disabled: false, g: 0 },
          { i: 2, label: "Charlie", disabled: true, g: 0 }, { i: 4, label: "Delta", disabled: false, g: -1 }],
  groups: ["Group one"], selected: 1, total: 5, shown: 4, ...over,
});
const date = (over: Record<string, unknown> = {}) => ({
  t: "picker", id: "0123abcd", kind: "date", rect, value: "2026-10-16", min: "2026-01-01", max: "", step: "",
  required: false, ...over,
});

describe("picker messages", () => {
  it("parses each kind exactly", () => {
    const s = parsePicker(select()) as SelectPicker;
    expect(s.kind).toBe("select");
    expect(s.items.map(i => i.i)).toEqual([0, 1, 2, 4]);
    expect(parsePicker(date())).toMatchObject({ kind: "date", value: "2026-10-16", min: "2026-01-01" });
    for (const kind of ["time", "datetime-local", "month", "week"]) {
      const value = { time: "09:30", "datetime-local": "2026-10-16T09:30", month: "2026-10", week: "2026-W42" }[kind];
      expect(parsePicker(date({ kind, value, min: "", max: "" }))).not.toBeNull();
    }
    expect(parsePicker({ t: "picker", id: "0123abcd", kind: "color", rect, value: "#336699" })).toMatchObject({ value: "#336699" });
  });
  it("refuses wrong shapes, ids, rectangles and bounds", () => {
    const bad = [
      { ...select(), extra: 1 }, select({ id: "0123ABCD" }), select({ id: "0123abc" }), select({ kind: "datalist" }),
      select({ rect: { ...rect, width: 0 } }), select({ rect: { ...rect, x: Infinity } }), select({ rect: { ...rect, z: 1 } }),
      select({ selected: 3 }), select({ shown: 3 }), select({ total: 3 }),
      select({ items: [{ i: 0, label: "a", disabled: false, g: 1 }], shown: 1 }),
      select({ items: [{ i: 0, label: "a", disabled: false, g: -1, html: "<b>" }], shown: 1, selected: 0 }),
      select({ items: [{ i: 0, label: 5, disabled: false, g: -1 }], shown: 1, selected: 0 }),
      select({ items: [{ i: 0, label: "a", disabled: false, g: -1 }, { i: 0, label: "b", disabled: false, g: -1 }], shown: 2, selected: 0 }),
      select({ items: Array.from({ length: PICKER_OPTIONS_MAX + 1 }, (_, i) => ({ i, label: "x", disabled: false, g: -1 })),
               shown: PICKER_OPTIONS_MAX + 1, total: PICKER_OPTIONS_MAX + 1, selected: -1 }),
      date({ value: "16/10/2026" }), date({ min: "yesterday" }), date({ step: "1e9" }), date({ required: "no" }),
      { t: "picker", id: "0123abcd", kind: "color", rect, value: "red" },
      { t: "picker", id: "0123abcd", kind: "color", rect, value: "#336699", min: "" },
      null, [], "picker",
    ];
    for (const message of bad) expect(parsePicker(message), JSON.stringify(message)?.slice(0, 80)).toBeNull();
  });
  it("cleans and caps page text again", () => {
    const s = parsePicker(select({ items: [{ i: 0, label: "Fox‮trot​ " + "x".repeat(300), disabled: false, g: -1 }],
      shown: 1, total: 1, selected: 0, groups: [] })) as SelectPicker;
    const label = s.items[0].label;
    expect(label).not.toMatch(/[‮​]/);
    expect([...label].length).toBeLessThanOrEqual(PICKER_LABEL_MAX);
  });
  it("parses only known close reasons", () => {
    expect(parsePickerClosed({ t: "picker_closed", id: "0123abcd", reason: "applied" })).toEqual({ id: "0123abcd", reason: "applied" });
    expect(parsePickerClosed({ t: "picker_closed", id: "0123abcd", reason: "whatever" })).toBeNull();
    expect(parsePickerClosed({ t: "picker_closed", id: "0123abcd" })).toBeNull();
  });
  it("shares the daemon's value grammar", () => {
    expect(validPickerValue("date", "2026-10-16")).toBe(true);
    expect(validPickerValue("date", "")).toBe(true);
    expect(validPickerValue("color", "")).toBe(false);
    expect(validPickerValue("color", "#00FF00")).toBe(false);
    expect(validPickerValue("week", "2026-W42")).toBe(true);
    expect(validPickerValue("time", "9:30")).toBe(false);
    expect(validPickerValue("select", "a")).toBe(false);
  });
});

describe("the listbox's keys", () => {
  const s = parsePicker(select()) as SelectPicker;
  it("starts at the selected option, else the first enabled one", () => {
    expect(initialHighlight(s)).toBe(1);
    expect(initialHighlight({ ...s, selected: -1 })).toBe(0);
    expect(initialHighlight({ ...s, selected: 2 })).toBe(0);
  });
  it("moves over enabled options only, clamped", () => {
    expect(moveHighlight(s.items, 1, 1)).toBe(3);
    expect(moveHighlight(s.items, 3, 1)).toBe(3);
    expect(moveHighlight(s.items, 3, -1)).toBe(1);
    expect(moveHighlight(s.items, 0, -5)).toBe(0);
    expect(moveHighlight(s.items, -1, 1)).toBe(0);
    expect(edgeHighlight(s.items, true)).toBe(3);
    expect(edgeHighlight(s.items, false)).toBe(0);
    expect(moveHighlight([{ i: 0, label: "x", disabled: true, g: -1 }], 0, 1)).toBe(-1);
  });
  it("types ahead, cycling one letter and narrowing on more", () => {
    const items = ["Apple", "Banana", "Avocado", "Apricot"].map((label, i) => ({ i, label, disabled: false, g: -1 }));
    expect(typeahead(items, 0, "a")).toBe(2);
    expect(typeahead(items, 2, "a")).toBe(3);
    expect(typeahead(items, 3, "a")).toBe(0);
    expect(typeahead(items, 0, "ap")).toBe(0);
    expect(typeahead(items, 0, "apr")).toBe(3);
    expect(typeahead(items, 0, "z")).toBe(-1);
    expect(typeahead(s.items, 0, "c")).toBe(-1);      // Charlie is disabled
  });
  it("maps keys", () => {
    expect(pickerKey({ key: "ArrowDown" })).toBe("down");
    expect(pickerKey({ key: "Escape" })).toBe("escape");
    expect(pickerKey({ key: "Enter" })).toBe("enter");
    expect(pickerKey({ key: "b" })).toBe("char");
    expect(pickerKey({ key: "b", ctrlKey: true })).toBe("other");
    expect(pickerKey({ key: "F5" })).toBe("other");
    expect(pickerKey({ key: "\u0000" })).toBe("other");
  });
});

describe("placement", () => {
  const bounds = { width: 400, height: 300 };
  it("opens below the control, at least as wide", () => {
    expect(pickerPlacement({ x: 10, y: 20, width: 220, height: 30 }, { width: 150, height: 100 }, bounds))
      .toMatchObject({ x: 10, y: 50, width: 220, above: false });
  });
  it("flips above when there is more room there and clamps both axes", () => {
    const at = pickerPlacement({ x: 380, y: 250, width: 40, height: 30 }, { width: 200, height: 200 }, bounds);
    expect(at.above).toBe(true);
    expect(at.y).toBe(50);
    expect(at.x + at.width).toBeLessThanOrEqual(bounds.width);
    const tall = pickerPlacement({ x: 0, y: 0, width: 40, height: 30 }, { width: 200, height: 900 }, bounds);
    expect(tall.maxHeight).toBeLessThanOrEqual(270);
    expect(tall.y + Math.min(900, tall.maxHeight)).toBeLessThanOrEqual(bounds.height);
  });
  it("stays inside a narrow surface", () => {
    const at = pickerPlacement({ x: 300, y: 10, width: 500, height: 30 }, { width: 600, height: 100 }, { width: 375, height: 600 });
    expect(at.x).toBe(0);
    expect(at.width).toBe(375);
  });
});

describe("the owner's gesture", () => {
  it("accepts one picker within the window, for the same generation only", () => {
    const g = new PickerGesture();
    expect(g.accept(1, 0)).toBe(false);
    g.press(1, 100);
    expect(g.accept(2, 200)).toBe(false);
    g.press(1, 100);
    expect(g.accept(1, 100 + PICKER_GESTURE_MS + 1)).toBe(false);
    g.press(1, 100);
    expect(g.accept(1, 300)).toBe(true);
    expect(g.accept(1, 301)).toBe(false);
  });
});
