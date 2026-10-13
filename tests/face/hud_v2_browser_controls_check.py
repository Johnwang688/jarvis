"""Compact owner Browser controls under the HUD CSP and production registry.
Synthetic Remote transport only; no page loads, live daemon or model calls.
"""
from __future__ import annotations
import json, os, sys
from pathlib import Path
REPO=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(REPO))
from tests.face.hud_v2_layout_check import _boot,_pane,_set_ws,_set_storage,_approval
from tests.face.hud_v2_remote_check import Remote
from jarvis.v2.browser.panes import PaneRefused

def controls_checks(browser,mock,base,check,until,guard,fake):
    ctx=browser.new_context(viewport={"width":1600,"height":800},has_touch=True)
    guard(ctx);ctx.add_init_script(fake);remote=Remote(ctx,mock);page=ctx.new_page()
    page.set_default_timeout(5000);errors=[];page.on("pageerror",lambda e:errors.append(str(e)))
    try:
        _boot(page,mock,base,until)
        raw={"preset":"cols2","focused":2,"panes":[{"view":"chat"},{"view":"browser","browserProfile":"p-11111111",
             "browser":{"active":"pageaa","tabs":[{"id":"pageaa","url":"https://example.test/","mode":"remote"}]}},
             {"view":"file"},{"view":"task"}]}
        _set_storage(page,zoom="100",layout="{}");_set_ws(page,json.dumps(raw));_boot(page,mock,None,until,reload=True)
        pane=page.locator(_pane(2));button=pane.locator('[data-testid="browser-options-toggle"]')
        options=pane.locator('[data-testid="browser-options"]')
        until(lambda:pane.locator("canvas").get_attribute("data-frame")=="ready",timeout=4)
        pane.locator('[data-testid="browser-url"]').click()
        until(lambda:page.evaluate("window.__hud.workspace().ws.focused")==2,timeout=3)
        page.set_viewport_size({"width":375,"height":800})
        until(lambda:pane.locator(".browser-navigation").is_visible(),timeout=3)
        front=lambda:next(b for _,p,b in reversed(mock.calls) if p=="/browser/front")
        original=next(p for p in front()["panes"] if p["pane_id"]==2)
        with mock.browser_lock: snapshot=mock.browser_panes.resolve("2")
        check("Browser starts with profile/capture options collapsed",options.is_hidden()
              and pane.get_by_role("button",name="Manage",exact=True).count()==0)
        check("Browser offers automatic addressing without mode choices",pane.locator('[aria-label="Browser mode"]').count()==0)
        geometry=pane.locator(".browser-navigation").evaluate("""e=>({
            height:e.getBoundingClientRect().height,width:e.clientWidth,scroll:e.scrollWidth,
            top:[...e.children].map(c=>Math.round(c.getBoundingClientRect().top))})""")
        check("Browser navigation fits one row at 375px",geometry["scroll"]<=geometry["width"]+1
              and max(geometry["top"])-min(geometry["top"])<4,str(geometry))
        screenshot=os.environ.get("BROWSER_CONTROLS_SCREENSHOT")
        if screenshot:page.screenshot(path=screenshot)
        button.click()
        until(lambda:not next(p for p in front()["panes"] if p["pane_id"]==2)["drawn"],timeout=3)
        check("Browser options are labelled and take keyboard focus",options.is_visible()
              and options.get_attribute("role")=="dialog" and button.get_attribute("aria-expanded")=="true"
              and options.locator('select[aria-label="Browser profile"]').evaluate("e=>e===document.activeElement"))
        current=next(p for p in front()["panes"] if p["pane_id"]==2)
        check("Browser options preserve the owner's see/drive preferences",current["see"]==original["see"]
              and current["drive"]==original["drive"])
        with mock.browser_lock:
            invalidated=not mock.browser_panes.owner_current(snapshot)
            try:mock.browser_panes.resolve("2");refused=False
            except PaneRefused:refused=True
        check("Browser options revoke host eligibility and previous refs",invalidated and refused)
        before=len(remote.inputs())
        pane.locator('[data-testid="remote-input"]').evaluate("e=>e.dispatchEvent(new KeyboardEvent('keydown',{key:'x',code:'KeyX',bubbles:true}))")
        pane.locator('[data-testid="remote-surface"]').evaluate("e=>e.dispatchEvent(new MouseEvent('mousedown',{bubbles:true,button:0,clientX:1,clientY:1}))")
        check("Browser options cannot send keyboard or pointer input to Remote",len(remote.inputs())==before)
        bounds=options.bounding_box();owner=pane.bounding_box()
        check("Browser options remain inside their pane at 375px",bounds["x"]>=owner["x"] and bounds["x"]+bounds["width"]<=owner["x"]+owner["width"]+1)
        if screenshot:page.screenshot(path=screenshot.replace(".png","-options.png"))
        page.keyboard.press("Escape")
        until(lambda:next(p for p in front()["panes"] if p["pane_id"]==2)["drawn"],timeout=3)
        check("Browser Escape restores its options button and host visibility",options.is_hidden()
              and button.evaluate("e=>e===document.activeElement"))
        surface=pane.locator('[data-testid="remote-surface"]')
        def page_press_events(after):
            return [m for m in remote.inputs()[after:] if m.get("t")=="mouse" and m.get("e",{}).get("type") in ("mousePressed","mouseReleased")]
        for gesture in ("mouse","touch"):
            button.click();before=len(remote.inputs())
            point={"x":20,"y":surface.bounding_box()["height"]-25}
            if gesture=="mouse":surface.click(position=point)
            else:surface.tap(position=point)
            page.wait_for_timeout(100)
            check("Browser "+gesture+" dismissal consumes its compatibility page press",options.is_hidden() and not page_press_events(before),str(remote.inputs()[before:]))
            before=len(remote.inputs());surface.click(position=point)
            until(lambda:bool(page_press_events(before)),timeout=3)
            check("Browser next deliberate click works after "+gesture+" dismissal",bool(page_press_events(before)))
        address=pane.locator('[data-testid="browser-url"]')
        address.fill(f"http://127.0.0.1:{mock.dev_port}/");address.press("Enter")
        until(lambda:pane.locator('[data-testid="browser-frame"]').count()==1,timeout=4)
        frame=pane.frame_locator('[data-testid="browser-frame"]')
        frame.locator("body").evaluate("e=>{window.__dismissInputs=[];for(const k of ['pointerdown','mousedown','mouseup','click'])document.addEventListener(k,()=>window.__dismissInputs.push(k),true)}")
        button.click();bounds=pane.locator('[data-testid="browser-frame"]').bounding_box()
        page.mouse.click(bounds["x"]+20,bounds["y"]+bounds["height"]-25)
        page.wait_for_timeout(100)
        check("Browser Direct iframe cannot receive the dismissal gesture",options.is_hidden() and frame.locator("body").evaluate("()=>window.__dismissInputs.length")==0)
        address.fill("https://example.test/");address.press("Enter")
        until(lambda:pane.locator("canvas").get_attribute("data-frame")=="ready",timeout=4)
        button.click();_approval(mock,"options-card")
        until(lambda:page.locator('[data-testid="approval-card"]').count()==1,timeout=3)
        check("Browser cards close and disable options before owner actions",options.is_hidden() and button.is_disabled())
        page.keyboard.press("Escape")
        until(lambda:page.locator('[data-testid="approval-card"]').count()==0,timeout=3)
        check("Browser page status remains outside collapsed options",pane.locator(".remotestatus").is_visible()
              and options.locator(".remotestatus").count()==0)
        check("Browser controls raise no page errors or control listener hits",not errors and not mock.api_hits,str(errors))
    except Exception:
        print("Controls failure state:",page.locator("body").inner_text()[:2400],flush=True)
        print("Controls calls:",mock.calls[-12:],flush=True)
        print("Controls workspace:",page.evaluate("window.__hud.workspace()"),flush=True)
        raise
    finally:ctx.close()

def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    from tests.face.hud_v2_mock import MockDaemon
    mock=MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True)
            try:controls_checks(browser,mock,f"http://127.0.0.1:{mock.port}",v2.check,v2.until,v2.guard,v2.FAKE_RECOGNIZER)
            finally:browser.close()
    finally:mock.stop()
    print(f"{v2.CHECKS-len(v2.FAILURES)}/{v2.CHECKS} Browser controls checks passed")
    return bool(v2.FAILURES)
if __name__=="__main__":sys.exit(main())
