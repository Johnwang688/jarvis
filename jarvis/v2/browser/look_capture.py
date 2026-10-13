"""Read-only CDP capture on BrowserHost's thread, including Direct isolation."""
from __future__ import annotations

import asyncio
import json
import secrets
import time
from urllib.parse import urlsplit

from . import policy
from .look import LookRefused, CHANGED, guarded, masked_image
from .look_scan import SCAN_JS, CLEAR_JS, NODE_CAP, RAW_CAP, REF_CAP, SCAN_TIMEOUT_S

LOOK_TIMEOUT_S=30.0


def direct_url(host,url: str) -> str:
    target=policy.judge_url(url,refused=host.refused_ports())
    parts=urlsplit(target)
    kind,address=policy.host_kind(parts.hostname or "")
    local=kind=="loopback_name" or (kind=="literal" and (address.is_loopback or address.is_unspecified))
    if not local:raise LookRefused("the Direct pane address is not a local page")
    port=parts.port or (443 if parts.scheme=="https" else 80)
    if not host.loopback_open(port):
        raise LookRefused("this Direct page has no owner-granted Remote network access; ask the owner to use Open in Remote or Send to chat")
    return target


async def _settle(awaitable):
    """Repeated cancellation cannot abandon an in-flight context retirement."""
    task=asyncio.create_task(awaitable)
    cancelled=False
    while not task.done():
        try:await asyncio.shield(task)
        except asyncio.CancelledError:cancelled=True
    result=task.result()
    if cancelled:raise asyncio.CancelledError()
    return result


async def _retire_hidden(host,ctx):
    from .host import PRIVATE
    async with host._launching.setdefault(PRIVATE,asyncio.Lock()):
        instance=host._instances.get(PRIVATE) or host._closing_instances.get(PRIVATE)
        if instance is not None and ctx in instance.private_contexts:
            await host._retire_context_locked(ctx)
        host._look_reserved.discard(ctx)


async def _hidden(host,url):
    from .host import PRIVATE, BrowserRefused
    # Retry retained failed cleanups before another hidden context can exist.
    for old in tuple(host._look_reserved):await _retire_hidden(host,old)
    async with host._launching.setdefault(PRIVATE,asyncio.Lock()):
        if host._closing_contexts:
            raise LookRefused("Private cleanup is unfinished; retry closing it first")
        if len(host._tabs)+len(host._look_reserved)>=max(1,host._tab_cap()):
            raise LookRefused("the Browser tab limit is full; close a tab first")
        direct_url(host,url)
        ctx="look-"+secrets.token_hex(12)
        host._look_reserved.add(ctx)
        try:
            instance=await host._instance(PRIVATE)
            context=await instance.browser.new_context(
                proxy={"server":f"socks5://127.0.0.1:{host._gate.port}","bypass":"<-loopback>"},
                accept_downloads=False,viewport={"width":1280,"height":800})
            instance.private_contexts[ctx]=context
            page=await context.new_page()
            context.on("page",lambda popup:asyncio.create_task(popup.close()))
            page.on("dialog",lambda dialog:asyncio.create_task(dialog.dismiss()))
            # No owner lease is admitted; every connection still meets the gate.
            await page.goto(url,wait_until="domcontentloaded",timeout=20_000)
            return ctx,page
        except BaseException:
            instance=host._instances.get(PRIVATE) or host._closing_instances.get(PRIVATE)
            if instance is None or ctx not in instance.private_contexts:
                host._look_reserved.discard(ctx)
            else:
                host._closing_contexts.add(ctx)
            raise


async def _scan(cdp,context_id,url,serial,whole,replace):
    args={"url":url,"serial":serial,"whole":whole,"replace":replace,
          "nodeCap":NODE_CAP,"rawCap":RAW_CAP,"refCap":REF_CAP}
    result=await asyncio.wait_for(cdp.send("Runtime.evaluate",{
        "expression":"("+SCAN_JS+")("+json.dumps(args)+")",
        "contextId":context_id,"returnByValue":True,"silent":True,
        "timeout":SCAN_TIMEOUT_S*1000}),SCAN_TIMEOUT_S+0.5)
    if result.get("exceptionDetails"):raise LookRefused("the page scan failed safely")
    scan=(result.get("result") or {}).get("value")
    if not isinstance(scan,dict):raise LookRefused("the page scan failed safely")
    if scan.get("changed"):raise LookRefused(CHANGED)
    return scan


async def _frame(cdp):
    data=await cdp.send("Page.getFrameTree")
    row=data["frameTree"]["frame"]
    return row["id"],row.get("loaderId"),row["url"]


async def capture(host,snapshot,whole,images,forms,current,deadline):
    from .host import PRIVATE
    if host._look_lock is None:host._look_lock=asyncio.Lock()
    task=asyncio.current_task();host._look_tasks.add(task)
    ctx=None;cdp=None;context_id=None;success=False
    try:
        async with host._look_lock:
            try:
                async with asyncio.timeout(max(0,deadline-time.monotonic())):
                    current()
                    if snapshot.mode=="direct":
                        # _hidden may fail after reserving; the finally retires all
                        # retained hidden identities, never a user's Private ctx.
                        ctx,page=await _hidden(host,snapshot.url)
                        cdp=await page.context.new_cdp_session(page)
                    else:
                        tab=host._tabs.get(snapshot.tab_id)
                        if tab is None or id(tab.page)!=snapshot.page_identity:raise LookRefused(CHANGED)
                        page=tab.page;cdp=await host._ensure_cdp(tab)
                    await cdp.send("Page.enable")
                    before=await _frame(cdp)
                    url=policy.judge_url(before[2],refused=host.refused_ports())
                    # Remote reads exactly the bound document. Direct may redirect
                    # through the same gate; its final URL is scanned and fenced.
                    if snapshot.mode=="remote" and url!=snapshot.url:raise LookRefused(CHANGED)
                    if snapshot.mode=="direct":direct_url(host,snapshot.url)
                    current()
                    world=await cdp.send("Page.createIsolatedWorld",{
                        "frameId":before[0],"worldName":"jarvis-look","grantUniveralAccess":False})
                    context_id=world["executionContextId"];serial=secrets.token_hex(16)
                    scan=await _scan(cdp,context_id,url,serial,whole,True)
                    withheld=guarded(scan,forms)
                    current()
                    image=None
                    if images and not withheld:
                        captured=await cdp.send("Page.captureScreenshot",{
                            "format":"png","fromSurface":True,"captureBeyondViewport":False})
                        after_scan=await _scan(cdp,context_id,url,serial,whole,False)
                        withheld=guarded(after_scan,forms) or withheld
                        if scan==after_scan and not withheld:image=masked_image(captured["data"],scan)
                    after=await _frame(cdp)
                    if before!=after:raise LookRefused(CHANGED)
                    current()
                    if snapshot.mode=="direct":direct_url(host,snapshot.url)
                    success=True
                    return scan,image,withheld
            finally:
                if cdp is not None and context_id is not None:
                    expression=("globalThis.__jarvisLook?.observer?.disconnect();" if success else CLEAR_JS)
                    try:await asyncio.wait_for(cdp.send("Runtime.evaluate",{
                        "expression":expression,"contextId":context_id,"silent":True}),1)
                    except Exception:pass
                if snapshot.mode=="direct":
                    for reserved in tuple(host._look_reserved):
                        await _settle(_retire_hidden(host,reserved))
    finally:
        host._look_tasks.discard(task)
