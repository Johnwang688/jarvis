import { act } from "react";
import { expect, it } from "vitest";
import { fill, json, phoneFixture } from "./phoneFixture";

const phone = phoneFixture();
const feedAnswer = "/m/api/feed/answer";
const approvalAnswer = "/m/api/approvals/answer";
const locked = () => phone.box.querySelector(".phone-lock")?.textContent === "Locked";

async function tapApproval(label: string) {
  const button = phone.button(label);
  expect(button.disabled).toBe(false);
  await act(async () => {
    for (const type of ["pointerdown", "pointerup", "click"]) {
      const event = new MouseEvent(type, { bubbles: true, cancelable: true, detail: type === "click" ? 1 : 0 });
      Object.assign(event, { pointerId: 1, pointerType: "touch", isPrimary: true });
      button.dispatchEvent(event);
    }
  });
}

it("PH3.10 unlocks a brief approval on the first tap and answers exactly once on a second tap", async () => {
  await phone.mount();
  expect(locked()).toBe(true);
  await phone.click("Unlock to approve");
  expect(phone.get).toHaveBeenCalledOnce();
  expect(phone.requests("/m/api/unlock/options")).toHaveLength(1);
  expect(phone.requests("/m/api/unlock")).toHaveLength(1);
  expect(phone.requests(feedAnswer)).toHaveLength(0);
  expect(locked()).toBe(false);
  await phone.click("Approve");
  expect(phone.requests(feedAnswer)).toHaveLength(1);
  expect(JSON.parse(String(phone.requests(feedAnswer)[0].body)).action).toBe("approve");
});

it("PH3.10 unlocks an approval card on the first tap and answers exactly once on a second tap", async () => {
  await phone.mount();
  await phone.selectTab("Chat");
  await phone.advance(600);
  await tapApproval("Unlock to approve");
  expect(phone.get).toHaveBeenCalledOnce();
  expect(phone.requests(approvalAnswer)).toHaveLength(0);
  await tapApproval("Approve");
  expect(phone.requests(approvalAnswer)).toHaveLength(1);
  expect(JSON.parse(String(phone.requests(approvalAnswer)[0].body)).decision).toBe("approve");
});

it.each(["cancelled", "refused"])("stays locked when unlock is %s", async (result) => {
  if (result === "cancelled") phone.get.mockRejectedValue(new DOMException("Fixture cancellation", "NotAllowedError"));
  else phone.reply((path) => path === "/m/api/unlock" ? json({ error: "refused" }, 403) : undefined);
  await phone.mount();
  await phone.click("Unlock to approve");
  expect(locked()).toBe(true);
  expect(phone.button("Unlock to approve")).toBeDefined();
  expect(phone.requests(feedAnswer)).toHaveLength(0);
});

it("denies a card while locked without asking for a passkey", async () => {
  await phone.mount();
  await phone.selectTab("Chat");
  await phone.click("Deny");
  expect(locked()).toBe(true);
  expect(phone.requests(approvalAnswer)).toHaveLength(1);
  expect(JSON.parse(String(phone.requests(approvalAnswer)[0].body)).decision).toBe("deny");
  expect(phone.get).not.toHaveBeenCalled();
});

it("PH3.11 a 423 locks the phone and never resends the write", async () => {
  phone.unlocked(30);
  phone.reply((path) => path === feedAnswer ? json({ error: "locked", unlock: true }, 423) : undefined);
  await phone.mount();
  expect(locked()).toBe(false);
  await phone.click("Approve");
  expect(locked()).toBe(true);
  expect(phone.box.querySelector(".phone-notice")?.textContent).toContain("Phone locked");
  expect(phone.get).not.toHaveBeenCalled();
  await phone.click("Unlock to approve");
  await phone.advance(1500);
  expect(phone.requests(feedAnswer)).toHaveLength(1);
  expect(phone.get).toHaveBeenCalledOnce();
  expect(phone.button("Approve")).toBeDefined();
});

it("unlocks Edit and a separately relocked Submit without opening or submitting on the unlock tap", async () => {
  await phone.mount();
  await phone.click("Unlock to edit");
  expect(phone.box.querySelector(".feed-editor")).toBeNull();
  await phone.click("Edit");
  await fill(phone.box.querySelector(".feed-editor textarea")!, "Fixture edit");
  phone.unlocked(0);
  await phone.advance(15_000);
  expect(locked()).toBe(true);
  const submit = phone.box.querySelector<HTMLButtonElement>(".feed-editor button[type=submit]")!;
  expect(submit.textContent?.trim()).toBe("Unlock to edit");
  await act(async () => submit.click());
  expect(phone.requests(feedAnswer)).toHaveLength(0);
  await phone.click("Submit");
  expect(phone.requests(feedAnswer)).toHaveLength(1);
  expect(JSON.parse(String(phone.requests(feedAnswer)[0].body))).toMatchObject({ action: "edit", text: "Fixture edit" });
});
