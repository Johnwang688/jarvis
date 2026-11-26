// @vitest-environment node
import { readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import ts from "typescript";
import { beforeEach, expect, it, vi } from "vitest";
import type { ApiName } from "../src/api";

const routes = {
  status: ["/v3/status", "/m/api/status"],
  feed: ["/v3/owner/feed", "/m/api/feed"],
  feedAnswer: ["/v3/owner/feed/answer", "/m/api/feed/answer"],
  feedFeedback: ["/v3/owner/feed/feedback", "/m/api/feed/feedback"],
  proposal: ["/v3/owner/proposal", "/m/api/proposal"],
  chat: ["/v3/owner/chat", "/m/api/chat"],
  chatEvents: ["/v3/owner/chat/events", "/m/api/chat/events"],
  chatSend: ["/v3/owner/chat/send", "/m/api/chat/send"],
  chatStop: ["/v3/owner/chat/stop", "/m/api/chat/stop"],
  chatNew: ["/v3/owner/chat/new", "/m/api/chat/new"],
  approvals: ["/v3/owner/approvals", "/m/api/approvals"],
  approvalsAnswer: ["/v3/owner/approvals/answer", "/m/api/approvals/answer"],
  work: ["/v3/owner/work", "/m/api/work"],
  voice: ["/v3/owner/voice", null],
  voiceSettings: ["/v3/owner/voice/settings", null],
  voiceHelper: ["/v3/owner/voice/helper", null],
  me: [null, "/m/api/me"],
  "login/options": [null, "/m/api/login/options"],
  "login/verify": [null, "/m/api/login/verify"],
  "pair/options": [null, "/m/api/pair/options"],
  "pair/verify": [null, "/m/api/pair/verify"],
  logout: [null, "/m/api/logout"],
  lock: [null, "/m/api/lock"],
  "unlock/options": [null, "/m/api/unlock/options"],
  unlock: [null, "/m/api/unlock"],
} satisfies Record<ApiName, (string | null)[]>;

beforeEach(() => vi.resetModules());

it("PH3.1 preserves every window path and reserves the phone auth paths", async () => {
  const { apiPath, surface } = await import("../src/api");
  expect(surface()).toBe("window");
  for (const name of Object.keys(routes) as ApiName[]) {
    const expected = routes[name][0];
    if (expected === null) expect(() => apiPath(name), name).toThrow();
    else expect(apiPath(name), name).toBe(expected);
  }
});

it("PH3.2 seals the surface on the first apiPath read", async () => {
  const { apiPath, setSurface, surface } = await import("../src/api");
  apiPath("status");
  expect(() => setSurface("phone")).toThrow();
  expect(surface()).toBe("window");
});

it("PH3.3 maps every phone name to its fixed route", async () => {
  const { apiPath, setSurface, surface } = await import("../src/api");
  setSurface("phone");
  expect(surface()).toBe("phone");
  for (const name of Object.keys(routes) as ApiName[]) {
    const expected = routes[name][1];
    if (expected === null) expect(() => apiPath(name), name).toThrow();
    else expect(apiPath(name), name).toBe(expected);
  }
});

it("PH3.5 centralizes fetch and API paths in api.ts", () => {
  const src = new URL("../src/", import.meta.url);
  const hits: string[] = [];
  for (const file of readdirSync(src, { recursive: true }).map(String)) {
    if (!/\.tsx?$/.test(file) || file === "api.ts") continue;
    const source = readFileSync(new URL(file, src), "utf8");
    const tree = ts.createSourceFile(join("src", file), source, ts.ScriptTarget.Latest, true);
    function visit(node: ts.Node) {
      if ((ts.isCallExpression(node) && /(?:^|\.)fetch$/.test(node.expression.getText(tree)))
        || (ts.isStringLiteralLike(node) && /^\/(?:v3\/|m\/api\/)/.test(node.text))) {
        hits.push(`${file}: ${node.getText(tree)}`);
      }
      ts.forEachChild(node, visit);
    }
    visit(tree);
  }
  expect(hits).toEqual([]);
});
