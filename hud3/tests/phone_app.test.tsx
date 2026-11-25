import { expect, it } from "vitest";
import { takePairingCode } from "../src/phone/PhoneApp";
import { approval, brief, challenge, chat, fill, json, phoneFixture } from "./phoneFixture";

const phone = phoneFixture();

it("PH3.8 offers sign-in on 401 and uses the passkey before loading the dashboard", async () => {
  let signedIn = false;
  phone.reply((path) => {
    if (path === "/m/api/me" && !signedIn) return json({ error: "unauthorized" }, 401);
    if (path === "/m/api/login/options") return json({ challenge });
    if (path === "/m/api/login/verify") {
      signedIn = true;
      return json({ signed_in: true });
    }
  });
  await phone.mount();
  expect(phone.fetch.mock.calls.map(([path]) => path)).toEqual(["/m/api/me"]);
  await phone.click("Sign in with passkey");
  expect(phone.get).toHaveBeenCalledWith(expect.objectContaining({
    publicKey: expect.objectContaining({ challenge: new Uint8Array([1, 2, 3]) }),
  }));
  expect(phone.fetch.mock.calls.slice(0, 4).map(([path]) => path))
    .toEqual(["/m/api/me", "/m/api/login/options", "/m/api/login/verify", "/m/api/me"]);
  expect(JSON.parse(String(phone.requests("/m/api/login/verify")[0].body)).credential)
    .toMatchObject({ id: challenge, response: { signature: "Bg" } });
  expect(phone.box.querySelector(".phone-dashboard")).not.toBeNull();
  await phone.click("Sign out");
  expect(phone.requests("/m/api/logout")).toHaveLength(1);
  expect(phone.button("Sign in with passkey")).toBeDefined();
});

it("PH3.9 erases the pairing fragment before rendering and sends it only in the pairing POST bodies", async () => {
  const code = "fake-pairing-code-fixture";
  history.replaceState(null, "", `/m/pair#${code}`);
  const consumed = takePairingCode(location, history);
  expect(consumed).toBe(code);
  expect(location.pathname).toBe("/m/pair");
  expect(location.hash).toBe("");
  phone.reply((path) => {
    if (path === "/m/api/pair/options") return json({ challenge, user: { id: challenge } });
    if (path === "/m/api/pair/verify") return json({ paired: true });
  });
  await phone.mount(consumed);
  expect(phone.box.outerHTML).not.toContain(code);
  expect(phone.fetch).not.toHaveBeenCalled();
  await phone.click("Create passkey");
  expect(phone.create).toHaveBeenCalledOnce();
  expect(phone.get).not.toHaveBeenCalled();
  expect(phone.fetch.mock.calls.map(([path]) => path)).toEqual(["/m/api/pair/options", "/m/api/pair/verify"]);
  for (const [, init] of phone.fetch.mock.calls) {
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body)).code).toBe(code);
  }
  expect(phone.box.outerHTML).not.toContain(code);
  expect(location.pathname).toBe("/m/");
  expect(phone.button("Sign in with passkey")).toBeDefined();
});

it("PH3.12 shows the pending approvals count with Chat hidden and updates it from chat", async () => {
  let count = 3;
  phone.reply((path) => {
    if (path === "/m/api/chat") return json({ ...chat, approvals: count });
    if (path === "/m/api/chat/events") return json({ ...chat, approvals: count, gap: false, events: [] });
  });
  await phone.mount();
  const tab = phone.box.querySelector<HTMLButtonElement>("#tab-chat")!;
  expect(tab.querySelector(".phone-badge")?.textContent).toBe("3");
  expect(phone.box.querySelector("[role=tabpanel]:not([hidden])")?.id).toBe("phone-feed");
  count = 2;
  await phone.advance(700);
  expect(tab.querySelector(".phone-badge")?.textContent).toBe("2");
  await phone.click("Work");
  expect([...phone.box.querySelectorAll("[role=tabpanel]:not([hidden])")].map(({ id }) => id)).toEqual(["phone-work"]);
  expect(phone.box.querySelector("#phone-work")?.textContent).toContain("Scheduled jobs");
});

it("labels a queued chat event and sends the text with the phone still locked", async () => {
  let queued = false;
  const message = "Queue the fixture message";
  phone.reply((path) => {
    if (path === "/m/api/chat/send") {
      queued = true;
      return json({ state: "running", steered: false });
    }
    if (path === "/m/api/chat/events" && queued) {
      return json({ ...chat, seq: 2, gap: false, events: [{ kind: "steer", text: message, queued: true, seq: 2 }] });
    }
  });
  await phone.mount();
  await phone.selectTab("Chat");
  const composer = phone.box.querySelector<HTMLTextAreaElement>(".chat-composer textarea")!;
  expect(composer.placeholder).toBe("Queued for the next turn");
  await fill(composer, message);
  await phone.click("Send");
  await phone.advance(700);
  expect(phone.requests("/m/api/chat/send")).toHaveLength(1);
  expect(JSON.parse(String(phone.requests("/m/api/chat/send")[0].body))).toEqual({ text: message });
  expect(phone.get).not.toHaveBeenCalled();
  expect(phone.box.querySelector(".chat-log")?.textContent).toContain("Queued for the next turn");
  expect(phone.box.querySelector(".chat-log")?.textContent).toContain(message);
  expect(phone.box.querySelector(".chat-log")?.textContent).not.toContain("Added to this turn");
});

it("renders server markup and external references as text", async () => {
  const markup = '<button onclick="alert(1)">Fixture control</button><img src=x><iframe src=x></iframe>';
  phone.reply((path) => {
    if (path === "/m/api/feed") {
      return json({ briefs: [{ ...brief, ask: markup, context: [{ label: markup, ref: "https://phone.example.invalid/a" }] }], recent: [] });
    }
    if (path === "/m/api/chat") return json({ ...chat, history: [{ role: "assistant", text: markup }] });
    if (path === "/m/api/approvals") {
      return json({ approvals: [{ ...approval, card: { ...approval.card, rows: [{ label: "Title", value: markup }] } }] });
    }
  });
  await phone.mount();
  expect(phone.box.querySelector(".feed-toggle")?.textContent).toBe(markup);
  expect(phone.box.querySelector(".chat-text")?.textContent).toBe(markup);
  expect(phone.box.querySelector(".approval-target dd")?.textContent).toBe(markup);
  expect(phone.box.querySelector(".feed-context")?.textContent).toContain("https://phone.example.invalid/a");
  expect(phone.box.querySelector("img, iframe, [onclick], a")).toBeNull();
  expect([...phone.box.querySelectorAll("button")].some(({ textContent }) => textContent === "Fixture control")).toBe(false);
});
