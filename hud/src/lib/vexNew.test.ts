import { describe, expect, it } from "vitest";
import {
  bodyKey, createReady, newBody, parseNewCheck, parseNewRequest, parseTemplates, pickTemplate,
  projectNameRefusal, requestLine, templatesOf, type NewForm,
} from "./vexNew";
import { parseStatus } from "./vex";

const ROBOTICS = "/mnt/c/Users/johnw/Documents/Robotics";
const TEMPLATES = [
  { id: "cpp_v5_vsc_competition_template", language: "cpp", label: "Competition Template" },
  { id: "cpp_v5_vsc_empty", language: "cpp", label: "Empty Project" },
  { id: "py_v5_vsc_empty", language: "python", label: "Empty Project" },
];

describe("the template list (the daemon's, never the window's)", () => {
  it("keeps a row only when its id is a VS Code template of the language it says", () => {
    const got = parseTemplates([
      ...TEMPLATES,
      { id: "py_v5_vsc_x", language: "cpp", label: "wrong language" },
      { id: "../../etc/passwd", language: "cpp", label: "a path" },
      { id: "Clawbot_Competition", language: "cpp", label: "an example zip" },
      { id: "cpp_v5_vsc_empty", language: "cpp", label: "twice" },
      { id: "cpp_v5_vsc_long", language: "cpp", label: "x".repeat(500) },
      null, "cpp_v5_vsc_empty", { id: 3 },
    ]);
    expect(got.map((t) => t.id)).toEqual(
      ["cpp_v5_vsc_competition_template", "cpp_v5_vsc_empty", "py_v5_vsc_empty", "cpp_v5_vsc_long"]);
    expect(got.find((t) => t.id === "cpp_v5_vsc_long")!.label).toHaveLength(80);
    expect(parseTemplates({ error: "x" })).toEqual([]);
  });

  it("filters by language and keeps the choice while it is offered", () => {
    const list = parseTemplates(TEMPLATES);
    expect(templatesOf(list, "python").map((t) => t.id)).toEqual(["py_v5_vsc_empty"]);
    expect(pickTemplate(list, "cpp", "cpp_v5_vsc_empty")).toBe("cpp_v5_vsc_empty");
    expect(pickTemplate(list, "python", "cpp_v5_vsc_empty")).toBe("py_v5_vsc_empty");
    expect(pickTemplate(list, "cpp", "")).toBe("cpp_v5_vsc_competition_template");
    expect(pickTemplate([], "cpp", "x")).toBe("");
  });
});

describe("the name, as the daemon's PROJECT_NAME", () => {
  it("passes what make takes and says why for the rest", () => {
    for (const ok of ["bot", "Over_ride-2", "_x", "a".repeat(64), "9lives"]) expect(projectNameRefusal(ok)).toBeNull();
    expect(projectNameRefusal("")).toMatch(/name/);
    expect(projectNameRefusal("a".repeat(65))).toMatch(/64/);
    expect(projectNameRefusal("-x")).toMatch(/start with -/);
    for (const bad of ["a b", "a;b", "naïve", "a/b", "..", "a.b", "a\\b", "x\u200by"]) {
      expect(projectNameRefusal(bad)).toMatch(/letters, digits/);
    }
  });
});

describe("the body Create sends, and the check asked about it", () => {
  const form = (over: Partial<NewForm> = {}): NewForm =>
    ({ name: "bot", language: "cpp", template: "cpp_v5_vsc_empty", location: ROBOTICS, ...over });

  it("holds exactly the daemon's keys; the default folder is left to the daemon", () => {
    expect(newBody(form(), ROBOTICS)).toEqual({ name: "bot", language: "cpp", template: "cpp_v5_vsc_empty" });
    expect(newBody(form({ location: "  " }), ROBOTICS)).not.toHaveProperty("location");
    expect(newBody(form({ location: ` ${ROBOTICS} ` }), ROBOTICS)).not.toHaveProperty("location");
    expect(newBody(form({ location: "/mnt/c/Users/johnw/Desktop" }), ROBOTICS).location).toBe("/mnt/c/Users/johnw/Desktop");
    expect(Object.keys(newBody(form({ location: "/x" }), null)).sort()).toEqual(["language", "location", "name", "template"]);
  });

  it("Create waits for the daemon's answer to exactly these inputs, and never under a card", () => {
    const check = (f: NewForm) => ({ key: bodyKey(newBody(f, ROBOTICS)), value: parseNewCheck({
      path: `${ROBOTICS}/${f.name}`, windows_path: null, state: "missing", asks: true, describe: "a new, empty folder" })! });
    const base = { configured: true, form: form(), defaultFolder: ROBOTICS, check: check(form()), pending: false, blocked: false };
    expect(createReady(base)).toBe(true);
    expect(createReady({ ...base, blocked: true })).toBe(false);
    expect(createReady({ ...base, pending: true })).toBe(false);
    expect(createReady({ ...base, configured: false })).toBe(false);
    expect(createReady({ ...base, check: null })).toBe(false);
    // An answer for other inputs — the name typed on, another template, another folder — is no answer.
    expect(createReady({ ...base, form: form({ name: "bot2" }) })).toBe(false);
    expect(createReady({ ...base, form: form({ template: "cpp_v5_vsc_competition_template" }) })).toBe(false);
    expect(createReady({ ...base, form: form({ location: "/mnt/c/elsewhere" }) })).toBe(false);
    expect(createReady({ ...base, form: form({ name: "a b" }), check: check(form({ name: "a b" })) })).toBe(false);
    expect(createReady({ ...base, form: form({ template: "" }), check: check(form({ template: "" })) })).toBe(false);
  });
});

describe("the daemon's answers", () => {
  it("a check is an absolute path in one of two states", () => {
    expect(parseNewCheck({ path: "/mnt/c/x/bot", windows_path: "C:\\x\\bot", state: "empty", asks: false,
      describe: "an existing empty folder" })).toEqual({ path: "/mnt/c/x/bot", windows_path: "C:\\x\\bot",
      state: "empty", asks: false, describe: "an existing empty folder" });
    expect(parseNewCheck({ path: "bot", state: "missing" })).toBeNull();
    expect(parseNewCheck({ path: "/x", state: "nonempty" })).toBeNull();
    expect(parseNewCheck({ path: "/x", state: "missing" })!.asks).toBe(true);
    expect(parseNewCheck(null)).toBeNull();
  });

  it("a request has an eight-hex id and a known state", () => {
    const r = parseNewRequest({ id: "deadbeef", name: "bot", path: "/x/bot", state: "asking", project_id: null, message: "" })!;
    expect(r.state).toBe("asking");
    expect(requestLine(r)).toBe("Approve or Deny on the card to create /x/bot.");
    expect(parseNewRequest({ id: "nope", state: "asking" })).toBeNull();
    expect(parseNewRequest({ id: "deadbeef", state: "done" })).toBeNull();
    const declined = parseNewRequest({ id: "deadbeef", name: "bot", path: "/x/bot", state: "declined", message: "" })!;
    expect(requestLine(declined)).toMatch(/nothing was made/);
    const said = parseNewRequest({ id: "deadbeef", state: "failed", message: "The project was not made: x" })!;
    expect(requestLine(said)).toBe("The project was not made: x");
  });

  it("the status carries the new-project folder (V-4)", () => {
    expect(parseStatus({ configured: true, project_folder: ROBOTICS })!.project_folder).toBe(ROBOTICS);
    expect(parseStatus({ configured: false })!.project_folder).toBeNull();
  });
});
