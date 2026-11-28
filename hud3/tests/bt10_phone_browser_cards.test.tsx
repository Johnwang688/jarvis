import { act } from "react";
import { expect, it } from "vitest";
import type { ApprovalRow } from "../src/components/approvals/approvalApi";
import { approval, chat, json, phoneFixture } from "./phoneFixture";

const TYPED = "  first line\n" + "keep this complete. ".repeat(90);
const site = { label: "Site", value: "https://shop.example.invalid" };
const linkTitle = "Follow a link on the browser page";
const linkRows: ApprovalRow[] = [
  { label: "Link number", value: "1" },
  { label: "Address", value: "https://login.example.invalid/path?q=" + "fixture".repeat(240) + "#end", source: "page" },
  { label: "From", value: site.value, source: "page" },
];
const cases: [string, string, ApprovalRow[]][] = [
  ["browser_fill", "Fill a browser field", [
    site, { label: "Field", value: "Notes", source: "page" },
    { label: "Text", value: TYPED },
    { label: "Then", value: "the page receives this text; the form is not submitted" },
  ]],
  ["browser_press", "Submit a browser form", [
    site, { label: "Button", value: "Send", source: "page" },
    { label: "Kind", value: "button" }, { label: "Sends to", value: "https://other.example.invalid" },
  ]],
  ["browser_sign_in", "Sign in to a saved site", [
    { label: "Saved login", value: "fixture" }, site, { label: "Browser", value: "chrome" },
  ]],
  ["browser_open", "Open a browser address", [
    { label: "URL", value: site.value + "/" + "long-path-".repeat(25) },
    { label: "Reason", value: "This chat has read outside text. Jarvis wants to open an address you did not supply in this turn." },
  ]],
  ["browser_follow", linkTitle, [
    ...linkRows, { label: "Destination", value: "login.example.invalid" },
    { label: "Reason", value: "Jarvis has a saved login on the destination site." },
  ]],
  ["browser_press", linkTitle, linkRows],
  ["browser_read", "Read the browser page", []],
];
const phone = phoneFixture();

it.each(cases)("shows every %s row in the phone chat and permits an unlocked approval", async (tool, title, rows) => {
  const request = { ...approval, tool, why: tool === "browser_sign_in" ? "phone" : "auto-off", card: { title, rows } };
  phone.unlocked(3600);
  phone.reply((path) => path === "/m/api/approvals"
    ? json({ approvals: [request], session_id: chat.session_id }) : undefined);
  await phone.mount();
  await phone.selectTab("Chat");
  await phone.advance(600);
  const card = phone.box.querySelector(".approval-card")!;
  expect(card.querySelector("h3")?.textContent).toBe(title);
  const fields = Array.from(card.querySelectorAll(".approval-target .approval-field, .approval-summary .approval-field"));
  for (const row of rows) {
    const label = row.label + (row.source === "page" ? " (text from the page)" : "");
    const field = fields.find((item) => item.querySelector("dt")?.textContent === label);
    expect(field?.querySelector("dd")?.textContent, label).toBe(row.value);
  }
  const button = phone.button("Approve");
  expect(button.disabled).toBe(false);
  await act(async () => {
    button.focus();
    button.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", bubbles: true }));
    button.dispatchEvent(new KeyboardEvent("keyup", { key: "Enter", bubbles: true }));
  });
  const answers = phone.requests("/m/api/approvals/answer");
  expect(answers).toHaveLength(1);
  expect(JSON.parse(String(answers[0].body))).toEqual({
    id: request.id, digest: request.digest, decision: "approve",
  });
});

it.each(["browser_follow", "browser_press"])("keeps %s unapprovable without an Address row", async (tool) => {
  const rows = linkRows.filter((row) => row.label !== "Address");
  const request = { ...approval, tool, card: { title: linkTitle, rows } };
  phone.unlocked(3600);
  phone.reply((path) => path === "/m/api/approvals"
    ? json({ approvals: [request], session_id: chat.session_id }) : undefined);
  await phone.mount();
  await phone.selectTab("Chat");
  await phone.advance(600);
  expect(phone.button("Deny").disabled).toBe(false);
  expect(phone.box.querySelector(".approval-card [role=status]")?.textContent).toBe("This request cannot be shown safely");
  expect(phone.box.querySelector(".approval-approve")).toBeNull();
});
