"""BR10 owner sound controls against a mock daemon; no Pulse or host Chrome."""
from __future__ import annotations
import json, sys
from pathlib import Path
REPO = Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path: sys.path.insert(0, str(REPO))
from tests.face.hud_v2_layout_check import _boot, _pane, _set_ws, _set_storage, _ws, _approval


def sound_checks(browser, mock, base, check, until, guard, init_script):
    print("\nBrowser sound: explicit owner speaker gestures and session-only profile state")
    from tests.face.hud_v2_remote_check import Remote
    from tests.face.hud_v2_mock_browser import state as host_state
    ctx=browser.new_context(viewport={"width":1600,"height":1000},permissions=["microphone"])
    guard(ctx);ctx.add_init_script(init_script);remote=Remote(ctx,mock)
    master={"desired":True,"generation":1,"available":True,"reason":None}
    rows=[{"profile":"p-11111111","sound":{"requested":False,"applied":False,"generation":1,"phase":"ready","reason":None}}]
    patches=[];toggles=[];malformed=[False]
    def status(route):
        response=route.fetch();value=response.json()
        value["browser"]={"sound":dict(master),"instances":rows}
        if malformed[0]:value["browser"]["sound"]["generation"]="untrusted status detail"
        route.fulfill(response=response,json=value)
    def settings(route):
        if route.request.method!="PATCH":return route.fallback()
        body=route.request.post_data_json;patches.append(body);master["desired"]=body["sound"];master["generation"]+=1
        if not body["sound"]:
            for row in rows:row["sound"].update(requested=False,applied=False)
        route.fulfill(json={"sound":body["sound"]})
    def toggle(route):
        tid=route.request.url.split("/")[-2];body=route.request.post_data_json
        toggles.append((tid,body));profile=host_state(mock).rows[tid]["profile"]
        row=next(row for row in rows if row["profile"]==profile)
        row["sound"].update(requested=body["on"],applied=body["on"]);master["generation"]+=1
        route.fulfill(json={"on":body["on"],"profile":profile,"generation":master["generation"]})
    ctx.route(base+"/status",status);ctx.route(base+"/browser/settings",settings)
    ctx.route(base+"/browser/tabs/*/sound",toggle)
    page=ctx.new_page();page.set_default_timeout(5000)
    errors=[];page.on("pageerror",lambda e:errors.append(str(e)))
    try:
        _boot(page,mock,base,until)
        raw={"preset":"cols2","focused":2,"panes":[{"view":"chat"},{"view":"browser","browserProfile":"p-11111111","see":False,
            "browser":{"active":"pageaa","tabs":[{"id":"pageaa","url":"https://example.test/sound","mode":"remote","source":{"kind":"unknown"}},
                {"id":"pagebb","url":"https://example.test/second","mode":"remote"}]}},{"view":"file"},{"view":"task"}]}
        _set_storage(page,zoom="100",layout="{}");_set_ws(page,json.dumps(raw));_boot(page,mock,None,until,reload=True)
        active=_pane(2,'[data-browser-page][aria-hidden="false"]')
        speaker=page.locator(active+' [data-testid="browser-sound-toggle"]')
        until(lambda:speaker.is_enabled() and page.locator(active+" canvas").get_attribute("data-frame")=="ready",timeout=5)
        check("BR10: allowed master never unmutes on discovery",not patches and not toggles and speaker.get_attribute("aria-pressed")=="false")
        check("BR10: per-profile limitation and hidden playback are disclosed","New tabs inherit it" in speaker.get_attribute("title") and "changing sites does not mute" in speaker.get_attribute("title") and "Hidden panes keep playing" in speaker.get_attribute("title"))
        chat=page.locator(_pane(1,'[data-testid="input"]'));chat.fill("sound fixture draft")
        before=len(remote.inputs());speaker.evaluate("e=>e.click()")
        check("BR10: script-generated speaker activation cannot unmute",not toggles)
        speaker.focus();speaker.press("Enter")
        until(lambda:speaker.get_attribute("aria-pressed")=="true",timeout=4)
        check("BR10: keyboard speaker grants sound directly without Settings",len(toggles)==1 and toggles[0][1]=={"on":True} and not patches and page.locator('[data-testid="settings-page"]').count()==0)
        check("BR10: speaker sends no page input or owner focus",len(remote.inputs())==before,str(remote.inputs()[before:]))
        check("BR10: on indicator is readable in pane and every profile chip","Sound on for Main" in speaker.inner_text() and page.locator(_pane(2,'[data-browser-tab="pagebb"]')).inner_text().endswith("sound on"))
        _approval(mock,"sound-card");until(lambda:page.locator('[data-testid="approval-card"]').count()==1,timeout=4)
        check("BR10: approval card disables speaker",speaker.is_disabled())
        speaker.evaluate("e=>e.click()");check("BR10: covered approval cannot toggle sound",len(toggles)==1)
        page.keyboard.press("Escape");until(lambda:page.locator('[data-testid="approval-card"]').count()==0,timeout=4)
        check("BR10: approval does not mute already granted playback",rows[0]["sound"]["requested"])
        speaker.click();until(lambda:speaker.get_attribute("aria-pressed")=="false",timeout=4)
        check("BR10: one pointer mute sends explicit false",len(toggles)==2 and toggles[-1][1]=={"on":False})
        page.keyboard.press("Control+,");until(lambda:page.locator('[data-testid="settings-page"]').count()==1,timeout=4)
        page.locator('[data-testid="settings-browser"]').click()
        switch=page.locator('[data-testid="browser-sound-setting"]')
        until(lambda:switch.is_enabled(),timeout=4)
        check("BR10: Settings says allow and is authoritative",switch.is_checked() and "Allow Browser sound" in page.locator('[data-testid="settings-content"]').inner_text())
        check("BR10: hidden covered speaker is inert",speaker.is_disabled())
        switch.click();until(lambda:not switch.is_checked(),timeout=4)
        page.locator('[data-testid="settings-close"]').click();until(lambda:page.locator('[data-testid="settings-page"]').count()==0,timeout=4)
        check("BR10: master OFF hides Remote speaker",speaker.count()==0 and patches==[{"sound":False}])
        page.keyboard.press("Control+,");until(lambda:page.locator('[data-testid="settings-page"]').count()==1,timeout=4)
        switch.click();until(lambda:switch.is_checked(),timeout=4)
        page.locator('[data-testid="settings-close"]').click();until(lambda:speaker.is_enabled(),timeout=4)
        check("BR10: master ON does not restore previous sound",speaker.get_attribute("aria-pressed")=="false" and len(toggles)==2)
        malformed[0]=True;mock.emit("browser_sound_changed",{"generation":master["generation"]})
        until(lambda:speaker.is_disabled(),timeout=4)
        check("BR10: unknown status cannot grant sound",speaker.is_disabled())
        malformed[0]=False;mock.emit("browser_sound_changed",{"generation":master["generation"]})
        until(lambda:speaker.is_enabled(),timeout=4)
        check("BR10: source and see survive sound gestures",_ws(page)["ws"]["panes"][1]["browser"]["tabs"][0]["source"]=={"kind":"unknown"} and not _ws(page)["ws"]["panes"][1]["see"] and chat.input_value()=="sound fixture draft")
        profile=page.locator(_pane(2,'select[aria-label="Browser profile"]'))
        rows.append({"profile":"private","sound":{"requested":False,"applied":False,"generation":2,"phase":"ready","reason":None}})
        profile.select_option("private")
        until(lambda:speaker.count()==1 and "Close Private panes" in speaker.inner_text(),timeout=4)
        check("BR10: Private cannot lose its session through relaunch",speaker.is_disabled())
        check("BR10: Direct frames have no autoplay permission",not any("autoplay" in (f.get_attribute("allow") or "") for f in page.locator("iframe").all()))
        check("BR10: no page errors or control listener requests",not errors and not mock.api_hits,str(errors[:3]))
    finally:ctx.close()

def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    from tests.face.hud_v2_mock import MockDaemon
    mock = MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True, args=['--use-fake-ui-for-media-stream', '--use-fake-device-for-media-stream'])
            try: sound_checks(browser, mock, f'http://127.0.0.1:{mock.port}', v2.check, v2.until, v2.guard, v2.FAKE_RECOGNIZER)
            except Exception as exc: v2.check('Sound scenario completes', False, str(exc))
            finally: browser.close()
    finally: mock.stop()
    print(f'{v2.CHECKS} sound checks, {len(v2.FAILURES)} failures')
    return bool(v2.FAILURES)
if __name__ == '__main__': sys.exit(main())