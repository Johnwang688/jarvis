"""BR4 owner gestures over real Chromium and a synthetic Remote stream.
The capture reply is a deterministic still, not evidence for Direct M6. Real
Chrome screenshot/text matching is separately tested in browser_capture_chrome.
"""
from __future__ import annotations
import base64, io, json, re, sys, os
from pathlib import Path
from urllib.parse import urlparse, parse_qs
REPO=Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path: sys.path.insert(0,str(REPO))
from tests.face.hud_v2_layout_check import _boot,_pane,_set_ws,_set_storage,_ws,_approval
from tests.face.hud_v2_mock_browser import state
from tests.face.hud_v2_remote_check import Remote

def capture_checks(browser,mock,base,check,until,guard,init_script):
    from PIL import Image
    print("\nBR4 owner Browser captures: immutable still, ink and atomic draft staging")
    ctx=browser.new_context(viewport={"width":2200,"height":1400},permissions=["microphone"])
    guard(ctx);ctx.add_init_script(init_script);remote=Remote(ctx,mock)
    image=io.BytesIO();Image.new("RGB",(1000,600),(238,244,248)).save(image,"PNG")
    image64=base64.b64encode(image.getvalue()).decode()
    requests=[];pending=[];mode={"hold":False}
    def reply(route):
        query={k:v[0] for k,v in parse_qs(urlparse(route.request.url).query).items()}
        requests.append(query)
        try:
            with mock.browser_lock:
                snap=mock.browser_panes.resolve_owner(query["window"],int(query["pane"]),query["local_tab"],query["view"])
            assert snap.mode=="remote"
        except Exception as exc:
            route.fulfill(status=409,json={"error":"Capture fixture binding refused"});return
        data={"capture_id":f"{len(requests):032x}","image":{"mime":"image/png","data_b64":image64,"width":1000,"height":600},
              "url":"https://example.test/captured","title":"Immutable fixture","text_chars":12,"whole":query["whole"]=="1",
              "notes":["Synthetic still; Direct Region Capture remains unmeasured."]}
        if mode["hold"]:pending.append((route,data))
        else:route.fulfill(json=data)
    ctx.route(re.compile(r"/browser/tabs/[0-9a-f]{8}/capture\?"),reply)
    page=ctx.new_page();page.set_default_timeout(5000);errors=[];page.on("pageerror",lambda e:errors.append(str(e)))
    raw={"preset":"cols2","focused":2,"panes":[{"view":"chat"},{"view":"browser","browserProfile":"p-11111111","see":False,
      "browser":{"active":"pageaa","tabs":[{"id":"pageaa","mode":"remote","url":"https://example.test/one","source":{"kind":"unknown"}},
      {"id":"pagebb","mode":"remote","url":"https://example.test/two"}]}},{"view":"file"},{"view":"task"}]}
    active=_pane(2,'[data-browser-page][aria-hidden="false"]')
    annotation=page.locator(active+' [data-testid="browser-annotate"]')
    canvas=page.locator(active+' [data-testid="annotation-canvas"]')
    actions=page.locator(_pane(2,'[data-testid="browser-capture-actions"]'))
    def options():
        button=page.locator(_pane(2,'[data-testid="browser-options-toggle"]'))
        if button.get_attribute("aria-expanded")!="true":button.click()
    def capture(label):
        options()
        actions.get_by_role("button",name=label,exact=True).click()
    def boot(zoom="100", chat=True):
        _set_storage(page,zoom=zoom,layout="{}");_set_ws(page,json.dumps(raw));_boot(page,mock,None,until,reload=True)
        until(lambda:page.locator(active+' .remotesurface canvas').get_attribute("data-frame")=="ready",timeout=5)
        until(lambda:any(p=="/browser/front" for _,p,_ in mock.calls),timeout=3)
        if chat:page.locator(_pane(1,'[data-testid="input"]')).click()
        options()
    def draw(tool,index):
        annotation.get_by_role("button",name=tool,exact=True).click()
        b=canvas.bounding_box();x=b["x"]+b["width"]*.3;y=b["y"]+b["height"]*.4
        if tool=="Text":
            page.mouse.click(x,y);annotation.get_by_label("Annotation text").fill("Owner mark")
            annotation.get_by_label("Annotation text").press("Enter")
        else:
            page.mouse.move(x,y);page.mouse.down();page.mouse.move(x+60,y+40,steps=4);page.mouse.up()
        check(f"BR4 {tool} commits image-space mark {index}",canvas.get_attribute("data-marks")==str(index))
    try:
        _boot(page,mock,base,until)
        for zoom in ("70","160"):
            boot(zoom);capture("Annotate")
            until(lambda:annotation.count()==1 and not annotation.get_by_role("button",name="Pen",exact=True).is_disabled(),timeout=4)
            check(f"BR4 {zoom}% captures the registered see-off source without changing permission",annotation.count()==1 and _ws(page)["ws"]["panes"][1]["see"] is False
                  and _ws(page)["ws"]["panes"][1]["browser"]["tabs"][0]["source"]=={"kind":"unknown"})
            before=len(remote.inputs())
            for i,tool in enumerate(("Pen","Arrow","Box","Text"),1):draw(tool,i)
            annotation.get_by_role("button",name="Undo",exact=True).click()
            check("BR4 Undo removes only the last mark",canvas.get_attribute("data-marks")=="3")
            annotation.get_by_role("button",name="Clear",exact=True).click();draw("Pen",1)
            page.locator(active+' [data-testid="remote-input"]').evaluate("e=>e.dispatchEvent(new KeyboardEvent('keydown',{key:'z',code:'KeyZ',bubbles:true}))")
            check("BR4 annotation keys and gestures never reach Remote",len(remote.inputs())==before,str(remote.inputs()[before:]))
            check("BR4 captured view blocks live navigation",page.locator(active+' button[aria-label="Reload"]').is_disabled())
            _approval(mock,"capture-card");until(lambda:page.locator('[data-testid="approval-card"]').count()==1,timeout=3)
            check("BR4 approval makes drawing and staging inert",annotation.evaluate("e=>e.inert") and annotation.locator('[data-testid="capture-send"]').is_disabled())
            page.keyboard.press("Escape");until(lambda:page.locator('[data-testid="approval-card"]').count()==0,timeout=3)
            check("BR4 Escape answers the card before annotation cancellation",annotation.count()==1 and canvas.get_attribute("data-marks")=="1")
            page.keyboard.press("Control+,");until(lambda:page.locator('[data-testid="settings-page"]').count()==1,timeout=3)
            check("BR4 Settings covers the still without discarding ink",annotation.count()==1 and page.locator(".workspace-surface").evaluate("e=>e.inert"))
            page.locator('[data-testid="settings-close"]').click();until(lambda:annotation.is_visible(),timeout=3)
            check("BR4 Settings return retains exactly the same marks",canvas.get_attribute("data-marks")=="1")
            page.evaluate("""() => window.__hud.dispatch({type:'chat',pane:1,patch:{files:Array.from({length:7},(_,i)=>({name:'old'+i,mime:'text/plain',data_b64:''}))}})""")
            annotation.locator('[data-testid="capture-send"]').click()
            until(lambda:annotation.count()==0 or "two attachment slots" in annotation.inner_text(),timeout=3)
            check("BR4 seven files refuse the whole pair and retain ink",annotation.count()==1 and len(page.evaluate("window.__hud.state().chats[1].files"))==7 and canvas.get_attribute("data-marks")=="1")
            page.evaluate("window.__hud.dispatch({type:'chat',pane:1,patch:{files:[]}})")
            annotation.locator('[data-testid="capture-send"]').click();until(lambda:annotation.count()==0,timeout=3)
            files=page.evaluate("window.__hud.state().chats[1].files")
            check("BR4 Send to chat stages exactly the canonical pair",len(files)==2 and [f["name"] for f in files]==["page.png","page text"])
            check("BR4 staging never sends a chat turn",not any(p.endswith("/send") for _,p,_ in mock.calls))
            check("BR4 still and ink export as an actual PNG",base64.b64decode(files[0]["data_b64"]).startswith(b"\x89PNG"))
        boot();actions.get_by_label("Whole-page text").check();capture("Annotate")
        until(lambda:annotation.count()==1,timeout=3)
        check("BR4 whole text is fixed before capture",requests[-1]["whole"]=="1" and "Whole-page text" in annotation.inner_text())
        annotation.get_by_role("button",name="Done",exact=True).click()
        mode["hold"]=True;capture("Annotate");until(lambda:bool(pending),timeout=3)
        page.locator(_pane(2,'[data-browser-tab="pagebb"]')).click()
        r,data=pending.pop();r.fulfill(json=data);mode["hold"]=False
        check("BR4 a late capture never appears on a different selected tab",page.locator('[data-testid="browser-annotate"]:visible').count()==0)
        until(lambda:page.locator(active+' .remotesurface canvas').get_attribute("data-frame")=="ready",timeout=4)
        # Use the real explicit conversion path; fallback must never silently capture Direct.
        box=page.locator(active+' [data-testid="browser-url"]');box.fill(f"http://127.0.0.1:{mock.dev_port}/");box.press("Enter")
        until(lambda:_ws(page)["ws"]["panes"][1]["browser"]["tabs"][1]["url"]==f"http://127.0.0.1:{mock.dev_port}/",timeout=4)
        check("BR4 fallback fixture is a real Direct frame",bool(until(lambda:page.locator(active+' [data-testid="browser-frame"]').count()==1,timeout=4)))
        n=len(requests);capture("Annotate")
        until(lambda:"Open this in Remote to annotate." in page.locator(active).inner_text(),timeout=4)
        check("BR4 Direct offers only the explicit documented fallback",len(requests)==n and "Open this in Remote to annotate." in page.locator(active).inner_text())
        page.locator(active).get_by_role("button",name="Open in Remote",exact=True).click()
        until(lambda:page.locator(active+' .remotesurface canvas').get_attribute("data-frame")=="ready",timeout=4)
        check("BR4 explicit fallback conversion opens Remote, without an implicit capture",len(requests)==n and page.locator(active+' .remotesurface canvas').count()==1)
        capture("Annotate");until(lambda:annotation.count()==1,timeout=3);draw("Box",1)
        if os.environ.get("BR4_SCREENSHOT"):page.screenshot(path=os.environ["BR4_SCREENSHOT"])
        annotation.get_by_role("button",name="Done",exact=True).click()
        # Two drawn chats; select the second before Send, then choose the first
        # while the capture is delayed. The accepted pair follows the gesture.
        raw["preset"]="cols3";raw["panes"][2]={"view":"chat"}
        boot();page.locator(_pane(3,'[data-testid="input"]')).click()
        check("BR4 split fixture selects chat three",page.evaluate("window.__hud.state().selectedChat")==3)
        mode["hold"]=True;capture("Send to chat")
        until(lambda:bool(pending),timeout=3)
        page.locator(_pane(1,'[data-testid="input"]')).click()
        r,data=pending.pop();r.fulfill(json=data);mode["hold"]=False
        until(lambda:len(page.evaluate("window.__hud.state().chats[3].files"))==2,timeout=3)
        check("BR4 delayed Send stays with selected split chat",len(page.evaluate("window.__hud.state().chats[3].files"))==2
              and page.evaluate("window.__hud.state().chats[1].files")==[])
        # A selected compose closes during the capture; another visible chat
        # receives nothing, and the whole pair is recoverable with its project.
        boot();page.locator(_pane(3,'[data-testid="input"]')).click()
        mode["hold"]=True;capture("Send to chat")
        until(lambda:bool(pending),timeout=3)
        page.locator('[data-testid="pane-3-close"]').click()
        until(lambda:page.locator(_pane(3)).count()==0,timeout=3)
        # Wait for the registry to acknowledge the remaining visible pane set.
        until(lambda:any(p=="/browser/front" and all(x["pane_id"]!=3 for x in b.get("panes",[])) for _,p,b in mock.calls[-4:]),timeout=3)
        r,data=pending.pop();r.fulfill(json=data);mode["hold"]=False
        until(lambda:len(page.evaluate("window.__hud.state().drafts['compose:*']?.files??[]"))==2,timeout=3)
        check("BR4 closure parks both files with an explicit recovery notice",len(page.evaluate("window.__hud.state().drafts['compose:*']?.files??[]"))==2
              and "next new thread" in page.evaluate("window.__hud.state().error??''")
              and page.evaluate("window.__hud.state().chats[1].files")==[])
        # Capture export failure leaves the immutable still and its ink.
        boot();capture("Annotate")
        until(lambda:annotation.count()==1,timeout=3);draw("Arrow",1)
        page.evaluate("() => {window.__realBlob=HTMLCanvasElement.prototype.toBlob;HTMLCanvasElement.prototype.toBlob=function(cb){cb(null)};}")
        annotation.locator('[data-testid="capture-send"]').click()
        until(lambda:"could not be exported" in annotation.inner_text(),timeout=3)
        check("BR4 failed export keeps the still and ink without staging",canvas.get_attribute("data-marks")=="1"
              and page.evaluate("window.__hud.state().chats[1].files")==[])
        page.evaluate("() => {HTMLCanvasElement.prototype.toBlob=window.__realBlob;}")
        annotation.get_by_role("button",name="Done",exact=True).click()
        raw["preset"]="cols2";raw["panes"][0]={"view":"chat"};raw["panes"][2]={"view":"file"}
        boot();page.locator(_pane(1,'[data-testid="tab-file"]')).click()
        until(lambda:page.evaluate("window.__hud.state().selectedChat") is None,timeout=3)
        n=len(requests);capture("Send to chat")
        check("BR4 no drawn chat refuses before capture",len(requests)==n and
              "No chat is open to attach files to" in page.locator(active).inner_text())
        check("BR4 all capture paths still send zero turns",not any(p.endswith("/send") for _,p,_ in mock.calls))
        check("BR4 no page errors",not errors,str(errors[:3]))
        check("BR4 no live control listener request",not mock.api_hits,str(mock.api_hits[:3]))
    except Exception:
        print("Capture failure state:",page.locator(_pane(2)).inner_text()[:1600]);raise
    finally:ctx.close()

def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    from tests.face.hud_v2_mock import MockDaemon
    mock=MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,args=["--use-fake-ui-for-media-stream","--use-fake-device-for-media-stream"])
            try:capture_checks(browser,mock,f"http://127.0.0.1:{mock.port}",v2.check,v2.until,v2.guard,v2.FAKE_RECOGNIZER)
            except Exception as exc:v2.check("BR4 scenario completes",False,str(exc))
            finally:browser.close()
    finally:mock.stop()
    print(f"{v2.CHECKS} capture checks, {len(v2.FAILURES)} failures")
    return bool(v2.FAILURES)
if __name__=="__main__":sys.exit(main())
