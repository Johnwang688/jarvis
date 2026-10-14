"""Actual Chromium owner find/menu/cursor gestures over isolated mocked sockets."""
from pathlib import Path
import json,sys
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from tests.face.hud_v2_remote_check import Remote
from tests.face.hud_v2_layout_check import _boot,_pane,_set_ws,_approval


class OwnerRemote(Remote):
    def receive(self,sock,text):
        super().receive(sock,text);message=json.loads(text)
        if message.get("t")=="find":self.send(sock,{"t":"find","seq":message["seq"],"available":True,"matched":message["query"]=="FIND_QUERY_PRIVATE"})
        elif message.get("t")=="cursor":self.send(sock,{"t":"cursor","seq":message["seq"],"cursor":"pointer"})
        elif message.get("t")=="selection":self.send(sock,{"t":"selection","text":"synthetic owner selection"})


def owner_tools_checks(browser,mock,base,check,until,guard,init_script):
    def required(predicate,timeout=5):
        assert until(predicate,timeout=timeout), "required owner-tools prerequisite did not settle"
        return True
    ctx=browser.new_context(viewport={"width":1400,"height":900},permissions=["microphone"])
    guard(ctx);ctx.add_init_script(init_script)
    ctx.add_init_script("window.__ownerClipboard=[];Object.defineProperty(navigator,'clipboard',{value:{writeText:async text=>__ownerClipboard.push(text)},configurable:true});")
    remote=OwnerRemote(ctx,mock);page=ctx.new_page();page.set_default_timeout(5000);errors=[]
    page.on("pageerror",lambda error:errors.append(str(error)))
    try:
        _boot(page,mock,base,required)
        raw={"preset":"cols2","focused":1,"panes":[{"view":"chat"},{"view":"browser","browserProfile":"p-11111111","browser":{"active":"ownerpage","tabs":[{"id":"ownerpage","mode":"remote","url":"https://example.test/owner"}]}}]}
        _set_ws(page,json.dumps(raw));_boot(page,mock,None,required,reload=True)
        active=_pane(2,'[data-browser-page][aria-hidden="false"]')
        canvas=page.locator(active+" canvas");proxy=page.locator(active+' [data-testid="remote-input"]')
        query=page.locator(active+' [data-testid="remote-find-query"]');menu=page.locator(active+' [data-testid="remote-context-menu"]')
        required(lambda:canvas.get_attribute("data-frame")=="ready",timeout=5);sock=remote.sockets[-1]
        chat=page.locator(_pane(1,'[data-testid="input"]'));chat.click();chat.fill("retained draft")
        page.keyboard.press("Control+f")
        check("owner Ctrl+F outside Remote never opens its find",query.count()==0)
        remote.send(sock,{"t":"cursor","seq":999,"cursor":"pointer"})
        remote.send(sock,{"t":"find","seq":999,"available":True,"matched":True})
        check("unsolicited owner metadata never focuses or opens Find",query.count()==0 and chat.evaluate("e=>e===document.activeElement"))
        canvas.click(position={"x":100,"y":100});page.keyboard.press("Control+f");required(lambda:query.count()==1)
        check("Remote proxy Ctrl+F opens and focuses only local find",query.evaluate("e=>e===document.activeElement")
              and not any(m.get("t")=="key" and m.get("e",{}).get("key")=="f" for m in sock["messages"]))
        before=len([m for m in sock["messages"] if m.get("t")=="find"])
        query.fill("FIND_QUERY_PRIVATE")
        check("typing a find query makes no search",len([m for m in sock["messages"] if m.get("t")=="find"])==before)
        query.press("Enter");required(lambda:any(m.get("t")=="find" for m in sock["messages"]))
        required(lambda:"Match selected" in page.locator(active+' [data-testid="remote-find"]').inner_text())
        query.press("Shift+Enter");required(lambda:any(m.get("t")=="find" and m.get("backward") for m in sock["messages"]))
        check("find Next and Previous are explicit owner requests",len([m for m in sock["messages"] if m.get("t")=="find"])==before+2)
        check("find query is absent from browser storage","FIND_QUERY_PRIVATE" not in page.evaluate("JSON.stringify(localStorage)"))
        query.press("Escape");required(lambda:query.count()==0)
        check("closing Find restores the Remote proxy on the owner gesture",proxy.evaluate("e=>e===document.activeElement"))
        bounds=canvas.bounding_box();page.mouse.click(bounds["x"]+bounds["width"]-3,bounds["y"]+bounds["height"]-3,button="right")
        required(lambda:menu.count()==1)
        check("owner context menu contains only five fixed actions",menu.locator("button").all_text_contents()==["Back","Forward","Reload","Find","Copy selection"])
        rect=menu.bounding_box()
        check("edge menu stays inside its visible Remote viewport",rect["x"]>=bounds["x"]-1 and rect["y"]>=bounds["y"]-1
              and rect["x"]+rect["width"]<=bounds["x"]+bounds["width"]+1 and rect["y"]+rect["height"]<=bounds["y"]+bounds["height"]+1)
        check("right-button down/up never reach the Remote page",not any(m.get("t")=="mouse" and m.get("e",{}).get("button")=="right" for m in sock["messages"]))
        menu.get_by_role("menuitem",name="Copy selection").click()
        required(lambda:page.evaluate("__ownerClipboard.length")==1)
        check("menu copy uses only explicit existing selection bridge",page.evaluate("__ownerClipboard")==["synthetic owner selection"])
        canvas.click(position={"x":80,"y":80},button="right");required(lambda:menu.count()==1)
        before=len([m for m in sock["messages"] if m.get("t")=="mouse"])
        canvas.click(position={"x":200,"y":180});required(lambda:menu.count()==0)
        check("outside menu dismissal consumes complete first Remote gesture",len([m for m in sock["messages"] if m.get("t")=="mouse"])==before)
        canvas.click(position={"x":200,"y":180})
        required(lambda:len([m for m in sock["messages"] if m.get("t")=="mouse"])>before)
        check("next deliberate left click still works",any(m.get("t")=="mouse" and m.get("e",{}).get("type")=="mouseReleased" for m in sock["messages"][before:]))
        page.keyboard.press("Control+f");required(lambda:query.count()==1);query.fill("retained query")
        remote.send(sock,{"t":"owner_reset","query":"forged"})
        check("malformed owner reset cannot close Find",query.count()==1)
        remote.send(sock,{"t":"owner_reset"});required(lambda:query.count()==0)
        check("same-document lifecycle reset closes Find without focus theft",chat.input_value()=="retained draft")
        canvas.click(position={"x":80,"y":80},button="right");required(lambda:menu.count()==1)
        _approval(mock,"owner-tools-card");required(lambda:page.locator('[data-testid="approval-card"]').count()==1)
        required(lambda:menu.count()==0)
        page.keyboard.press("Escape")
        required(lambda:page.locator('[data-testid="approval-card"]').count()==0)
        check("approval Escape has priority over owner menu/find",menu.count()==0 and query.count()==0)
        # Synthetic auth metadata is attention only; it must never expose the
        # find/menu actions or move keyboard focus without an owner gesture.
        chat.click()
        auth_id="b"*32
        remote.send(sock,{"t":"auth","id":auth_id,"origin":"https://example.test","realm":"synthetic realm","scheme":"basic","expires_in":60})
        required(lambda:page.locator(active+' [data-testid="browser-auth"]').count()==1)
        before=len(sock["messages"])
        proxy.evaluate("e=>e.dispatchEvent(new KeyboardEvent('keydown',{key:'f',code:'KeyF',ctrlKey:true,bubbles:true,cancelable:true}))")
        canvas.click(position={"x":80,"y":80},button="right")
        check("pending authentication blocks local Find and context actions",query.count()==0 and menu.count()==0 and not any(m.get("t") in ("find","cursor") for m in sock["messages"][before:]))
        check("authentication metadata retains the active chat draft",chat.input_value()=="retained draft" and chat.evaluate("e=>e===document.activeElement"))
        remote.send(sock,{"t":"auth_closed","id":auth_id,"reason":"cancelled"})
        required(lambda:page.locator(active+' [data-testid="browser-auth"]').count()==0)
        narrow={"preset":"single","focused":1,"panes":[raw["panes"][1]]}
        _set_ws(page,json.dumps(narrow));page.set_viewport_size({"width":375,"height":812})
        _boot(page,mock,None,required,reload=True)
        active=_pane(1,'[data-browser-page][aria-hidden="false"]')
        canvas=page.locator(active+" canvas");query=page.locator(active+' [data-testid="remote-find-query"]')
        menu=page.locator(active+' [data-testid="remote-context-menu"]')
        required(lambda:canvas.get_attribute("data-frame")=="ready")
        canvas.click(position={"x":20,"y":20});page.keyboard.press("Control+f");required(lambda:query.count()==1)
        check("375px Find wraps inside its visible Browser pane",page.locator(active+' [data-testid="remote-find"]').evaluate("e=>e.scrollWidth<=e.clientWidth+1"))
        query.press("Escape");required(lambda:query.count()==0)
        bounds=canvas.bounding_box();page.mouse.click(bounds["x"]+bounds["width"]-2,bounds["y"]+bounds["height"]-2,button="right")
        required(lambda:menu.count()==1);rect=menu.bounding_box()
        check("375px menu stays inside its visible Remote surface",rect["x"]>=bounds["x"]-1 and rect["y"]>=bounds["y"]-1 and rect["x"]+rect["width"]<=min(375,bounds["x"]+bounds["width"])+1 and rect["y"]+rect["height"]<=bounds["y"]+bounds["height"]+1)
        check("owner tools use no control listener",not mock.api_hits)
        check("owner tools produce no page errors",not errors,str(errors[:3]))
    finally:ctx.close()


def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    from tests.face.hud_v2_mock import MockDaemon
    mock=MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,args=["--use-fake-ui-for-media-stream","--use-fake-device-for-media-stream"])
            try:owner_tools_checks(browser,mock,f"http://127.0.0.1:{mock.port}",v2.check,v2.until,v2.guard,v2.FAKE_RECOGNIZER)
            except Exception as error:v2.check("owner tools scenario completes",False,str(error))
            finally:browser.close()
    finally:mock.stop()
    print(f"{v2.CHECKS} owner tools checks, {len(v2.FAILURES)} failures")
    return bool(v2.FAILURES)
if __name__=="__main__":sys.exit(main())
