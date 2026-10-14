"""Actual Chromium auth-form focus/keyboard/hold lifecycle over isolated mock HUD.

The real backend Basic/Digest fixtures are separate. This fixture supplies only
synthetic socket metadata/credentials and never starts the owner daemon.
"""
import json
import sys
from urllib.parse import urlsplit
from pathlib import Path
REPO=Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:sys.path.insert(0,str(REPO))
from tests.face.hud_v2_layout_check import _boot,_pane,_set_ws,_set_storage,_ws,_approval
from tests.face.hud_v2_remote_check import Remote
from tests.face.hud_v2_mock_browser import state

# Synthetic fixture-only fields, never owner or service credentials.
FAKE_USER = 'fixture'
FAKE_SECRET = 'synthetic-ui-secret-1'

class AuthRemote(Remote):
    def receive(self,sock,text):
        super().receive(sock,text)
        msg=json.loads(text)
        if msg.get("t")=="auth":
            self.send(sock,{"t":"auth_closed","id":msg["id"],"reason":"cancelled" if msg.get("cancel") else "answered"})

def auth_checks(browser,mock,base,check,until,guard,init_script):
    ctx=browser.new_context(viewport={"width":1600,"height":1000},permissions=["microphone"])
    guard(ctx);ctx.add_init_script(init_script)
    # Browser-owned state observes actual closure even for mocked routes. The
    # installed Playwright route.on_close path errors on code-less ws.close().
    ctx.add_init_script("""window.__authFixtureSockets=[];
        const NativeSocket=window.WebSocket;
        window.WebSocket=class extends NativeSocket {
          constructor(...args){super(...args);window.__authFixtureSockets.push(this);}
        };""")
    remote=AuthRemote(ctx,mock)
    def pending_restore(route):
        response=route.fetch();row=response.json();tid=row["tab_id"]
        state(mock).rows[tid]["url"]="about:blank"
        row.update(url="about:blank",loaded=False,auth_pending=True)
        route.fulfill(response=response,json=row)
    ctx.route(base+"/browser/tabs/*/restore",pending_restore)
    page=ctx.new_page();page.set_default_timeout(5000);errors=[];browser_responses=[]
    page.on("response",lambda response:browser_responses.append({"path":urlsplit(response.url).path,"status":response.status}) if "/browser/" in response.url else None)
    page.on("pageerror",lambda error:errors.append(str(error)))
    try:
        _boot(page,mock,base,until)
        raw={"preset":"cols2","focused":2,"panes":[{"view":"chat"},{"view":"browser","browserProfile":"p-11111111","see":False,
            "browser":{"active":"pageaa","tabs":[{"id":"pageaa","url":"https://example.test/auth","mode":"remote","source":{"kind":"unknown"}}]}},
            {"view":"file"},{"view":"task"}]}
        _set_storage(page,zoom="100",layout="{}");_set_ws(page,json.dumps(raw));_boot(page,mock,None,until,reload=True)
        active=_pane(2,'[data-browser-page][aria-hidden="false"]')
        assert until(lambda:page.locator(active+" canvas").get_attribute("data-frame")=="ready",timeout=5)
        sock=remote.sockets[-1]
        form=page.locator(active+' [data-testid="browser-auth"]')
        user=page.locator(active+' [data-testid="browser-auth-user"]')
        secret=page.locator(active+' [data-testid="browser-auth-secret"]')
        def replies():return [m for item in remote.sockets for m in item["messages"] if m.get("t")=="auth"]
        def prompt(number):
            remote.send(sock,{"t":"auth","id":format(number,"032x"),"origin":"https://example.test","realm":"<img src=x onerror=alert(1)>","scheme":"basic","expires_in":60})
            assert until(lambda:form.count()==1 and user.is_enabled(),timeout=4)
        check("Auth pending stored restore attaches its real tab without generic restore failure",
              bool(remote.sockets) and "This page was not restored" not in page.locator(_pane(2)).inner_text())
        chat=page.locator(_pane(1,'[data-testid="input"]'));chat.fill("owner draft");chat.focus();prompt(1)
        check("Auth prompt never takes focus and realm is plain text",chat.evaluate("e=>e===document.activeElement") and form.locator("img").count()==0 and "<img" in form.inner_text())
        check("Auth password is masked and autocomplete is disabled",secret.get_attribute("type")=="password" and secret.get_attribute("autocomplete")=="off")
        before=len(remote.inputs())
        page.locator(active+' [data-testid="remote-input"]').evaluate("e=>e.dispatchEvent(new KeyboardEvent('keydown',{key:'x',code:'KeyX',bubbles:true}))")
        check("Auth attention blocks page input",len(remote.inputs())==before)
        user.fill(FAKE_USER);secret.fill(FAKE_SECRET)
        signin=form.get_by_role("button",name="Sign in",exact=True);signin.focus();signin.press("Enter")
        assert until(lambda:form.count()==0,timeout=4)
        check("Keyboard Enter activates Sign in exactly once",len(replies())==1 and replies()[0]=={"t":"auth","id":format(1,"032x"),"cancel":False,"username":FAKE_USER,"password":FAKE_SECRET})
        check("Auth credentials are absent from browser storage and ordinary HTTP requests",
              "synthetic-ui-secret" not in page.evaluate("JSON.stringify({...localStorage})") and "synthetic-ui-secret" not in json.dumps(mock.calls))
        prompt(2);cancel=form.get_by_role("button",name="Cancel",exact=True);cancel.focus();cancel.press("Enter")
        assert until(lambda:form.count()==0,timeout=4)
        check("Keyboard Enter activates Cancel exactly once",len(replies())==2 and replies()[-1]=={"t":"auth","id":format(2,"032x"),"cancel":True})
        prompt(3);user.focus();user.press("Escape");assert until(lambda:form.count()==0,timeout=4)
        check("Escape in the owner form cancels once",len(replies())==3 and replies()[-1]["cancel"] is True)
        prompt(4);secret.fill("synthetic-ui-held-secret");_approval(mock,"auth-card")
        assert until(lambda:page.locator('[data-testid="approval-card"]').count()==1 and secret.is_disabled(),timeout=4)
        check("Higher approval clears typed auth fields",user.input_value()=="" and secret.input_value()=="")
        count=len(replies());page.keyboard.press("Escape")
        assert until(lambda:page.locator('[data-testid="approval-card"]').count()==0,timeout=4)
        check("Escape answers the higher card and never replays the old auth nonce",len(replies())==count and secret.is_disabled())
        prompt(5);check("A fresh post-approval nonce starts with empty fields",user.input_value()=="" and secret.input_value()=="")
        secret.fill("synthetic-ui-disconnected-secret");count=len(remote.sockets);calls_before=len(mock.calls)
        page.keyboard.press("Control+,");assert until(lambda:page.locator('[data-testid="settings-page"]').count()==1,timeout=4)
        assert until(lambda:any(c[1]=="/browser/front" and c[2].get("covered") for c in mock.calls[calls_before:]),timeout=4), "Settings never published covered ownership"
        assert until(lambda:page.evaluate("url=>window.__authFixtureSockets.some(s=>s.url===url&&s.readyState===WebSocket.CLOSED)",sock["ws"].url),timeout=4), "Settings never detached the old Remote socket"
        page.locator('[data-testid="settings-close"]').click()
        def reattached():
            # Sync Playwright dispatches routed socket callbacks during API
            # calls; Python-only sleep/polling would starve that event pump.
            page.evaluate("0")
            return len(remote.sockets)>count
        if not until(reattached,timeout=4):
            details={"sockets_before":count,"sockets_after":len(remote.sockets),"bad_tickets":remote.bad_tickets,
                "settings_visible":page.locator('[data-testid="settings-page"]').count(),
                "workspace_covered":page.locator('.workspace-surface').get_attribute("data-covered"),
                "workspace_inert":page.locator('.workspace-surface').evaluate("e=>e.inert"),
                "latest_front":next((call[2] for call in reversed(mock.calls) if call[1]=="/browser/front"),None),
                "retained_host_tabs":list(state(mock).rows.values()),
                "remote_views":page.locator('[data-testid="remote-view"]').count(),
                "connection_errors":page.locator('[data-testid="remote-error"]').all_text_contents(),
                "browser_responses":browser_responses,
                "page_errors":errors}
            raise AssertionError("Settings return did not reattach: "+json.dumps(details,sort_keys=True))
        sock=remote.sockets[-1];prompt(6)
        check("Reattachment discards previous typed auth and keeps the host handle",user.input_value()=="" and secret.input_value()=="" and len(state(mock).rows)==1)
        page.set_viewport_size({"width":375,"height":812})
        # A previously visible form does not prove the resize observer and
        # workspace fit have committed the new narrow-pane geometry yet.
        def narrow_geometry():
            return page.evaluate("""() => {
              const form=document.querySelector('[data-testid="browser-auth"]');
              const shape=e=>e?{rect:e.getBoundingClientRect().toJSON(),scrollWidth:e.scrollWidth,clientWidth:e.clientWidth,visibility:getComputedStyle(e).visibility}:null;
              return {viewport:{width:innerWidth,height:innerHeight},form:shape(form),pane:shape(form?.closest('[data-testid="pane-2"]')),visible:!!form?.getClientRects().length,inert:!!form?.closest('[inert]')};
            }""")
        settled=until(lambda:_ws(page)["fit"]["panes"]==[2] and form.is_visible()
                      and form.evaluate("e=>e.getBoundingClientRect().width<=innerWidth"),timeout=4)
        assert settled, "narrow Browser layout never settled: "+json.dumps(narrow_geometry(),sort_keys=True)
        fits=form.evaluate("e=>e.scrollWidth<=e.clientWidth+1")
        check("Auth form remains within a narrow pane",fits,"" if fits else json.dumps(narrow_geometry(),sort_keys=True))
        check("Auth UI has no page errors, stale tickets or control-listener requests",not errors and remote.bad_tickets==0 and not mock.api_hits,str(errors[:3]))
    finally:ctx.close()

def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    from tests.face.hud_v2_mock import MockDaemon
    mock=MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,args=["--use-fake-ui-for-media-stream","--use-fake-device-for-media-stream"])
            try:auth_checks(browser,mock,f"http://127.0.0.1:{mock.port}",v2.check,v2.until,v2.guard,v2.FAKE_RECOGNIZER)
            except Exception as exc:
                import traceback
                traceback.print_exc()
                v2.check("Auth UI scenario completes",False,str(exc))
            finally:browser.close()
    finally:mock.stop()
    print(f"{v2.CHECKS} Auth UI checks, {len(v2.FAILURES)} failures")
    return bool(v2.FAILURES)
if __name__=="__main__":sys.exit(main())
