"""B-IME: IME composition from a real HUD Remote pane to a real page (Chrome 155).

Both ends are real headless Google Chrome (`channel="chrome"`, the daemon's
own): the built HUD with its real RemoteView, and an isolated page holding an
`<input>` and a `contenteditable`. Composition is driven on the **HUD's** page
through CDP (`Input.imeSetComposition`, `Input.insertText`,
`Input.dispatchKeyEvent`), exactly as an IME drives Chrome. Every `key` and
`text` message the pane sends is parsed by the daemon's own validator
(`jarvis.v2.browser.stream.parse`) and applied to the page with the CDP method
it names, so what is checked is what the page receives. The mock daemon,
ephemeral ports and a temp HOME only; nothing loads a public page, and no
composition text is printed.
"""
from __future__ import annotations
import json,sys
from pathlib import Path
REPO=Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:sys.path.insert(0,str(REPO))
from tests.face.hud_v2_remote_check import Remote
from tests.face.hud_v2_layout_check import _boot,_pane,_set_ws,_approval
from jarvis.v2.browser import stream

TARGET="""<!doctype html><meta charset=utf-8><style>body{margin:0;font:16px sans-serif}input,div{display:block;width:300px;height:24px;margin:8px;border:1px solid #888}</style>
<input id=field><div id=rich contenteditable></div>
<script>
window.__log=[];
for(const id of ['field','rich'])for(const type of ['keydown','compositionstart','compositionend','beforeinput'])
  document.getElementById(id).addEventListener(type,e=>__log.push({id,type,key:e.key??null,inputType:e.inputType??null}));
window.__value=()=>({field:document.getElementById('field').value,rich:document.getElementById('rich').textContent});
</script>"""


class PageBridge(Remote):
    """The mock stream, plus the daemon's parse → CDP onto a real page."""
    def __init__(self,ctx,mock):
        super().__init__(ctx,mock);self.queue=[];self.refused=[]
    def receive(self,sock,text):
        super().receive(sock,text)
        if json.loads(text).get('t') in ('key','text'):self.queue.append(text)
    def deliver(self,cdp):
        while self.queue:
            raw=self.queue.pop(0)
            try:msg=stream.parse(raw.encode())
            except stream.Bad as bad:self.refused.append(str(bad));continue
            if msg.method:cdp.send(msg.method,msg.params)


def ime_checks(browser,mock,base,check,until,guard,init_script):
    print('\nB-IME: composition from a real Remote pane to a real page')
    target_ctx=browser.new_context(viewport={'width':400,'height':200});guard(target_ctx)
    target=target_ctx.new_page();target.set_content(TARGET);target_cdp=target_ctx.new_cdp_session(target)
    ctx=browser.new_context(viewport={'width':1400,'height':900},permissions=['microphone'])
    guard(ctx);ctx.add_init_script(init_script);remote=PageBridge(ctx,mock);page=ctx.new_page();page.set_default_timeout(5000)
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    def required(predicate,timeout=5):
        def poll():
            page.evaluate('0')  # routed socket callbacks need a Playwright dispatch
            return predicate()
        assert until(poll,timeout=timeout),'prerequisite did not settle'
    try:
        _boot(page,mock,base,required)
        raw={'preset':'cols2','focused':1,'panes':[{'view':'chat'},{'view':'browser','browserProfile':'p-11111111','browser':{'active':'pageaa','tabs':[{'id':'pageaa','mode':'remote','url':'https://example.test/ime'}]}}]}
        _set_ws(page,json.dumps(raw));_boot(page,mock,None,required,reload=True)
        active=_pane(2,'[data-browser-page][aria-hidden="false"]')
        canvas=page.locator(active+' canvas');proxy=page.locator(active+' [data-testid="remote-input"]')
        chat=page.locator(_pane(1,'[data-testid="input"]'))
        required(lambda:canvas.get_attribute('data-frame')=='ready');cdp=ctx.new_cdp_session(page)

        def settle():
            page.wait_for_timeout(120)  # past COMMIT_HOLD_MS, and lets socket callbacks run
            page.evaluate('0');remote.deliver(target_cdp)
        def value(which):return target.evaluate('__value()')[which]
        def aim(which):target.evaluate(f"document.getElementById('{which}').focus()");target.evaluate('__log.length=0')
        def into_pane(x=60,y=70):canvas.click(position={'x':x,'y':y});required(lambda:proxy.evaluate('e=>e===document.activeElement'))
        def compose(text):cdp.send('Input.imeSetComposition',{'text':text,'selectionStart':len(text),'selectionEnd':len(text)})
        def commit(text):cdp.send('Input.insertText',{'text':text})
        def page_saw(*types):return [x for x in target.evaluate('__log') if x['type'] in types]

        check('the proxy is a 1px box before any composition',proxy.evaluate('e=>e.getBoundingClientRect().width')<=2)
        # 1. Pinyin: Process keys, updates, a commit → the page gets the text once, no key, no composition.
        aim('field');into_pane(60,70)
        for ch in ['n','ni','nih','niha','nihao']:
            cdp.send('Input.dispatchKeyEvent',{'type':'rawKeyDown','key':'Process','code':'Key'+ch[-1].upper(),'windowsVirtualKeyCode':229})
            compose(ch)
            cdp.send('Input.dispatchKeyEvent',{'type':'keyUp','key':ch[-1],'code':'Key'+ch[-1].upper(),'windowsVirtualKeyCode':ord(ch[-1].upper())})
        box=proxy.bounding_box();surface=page.locator(active+' [data-testid="remote-surface"]').bounding_box();cv=canvas.bounding_box()
        check('composing shows the box where the owner pressed, so the candidate window opens there',
              proxy.evaluate('e=>e.hasAttribute("data-composing")&&getComputedStyle(e).opacity==="1"') and box['width']>50
              and abs(box['x']-(cv['x']+60))<=2 and abs(box['y']-(cv['y']+70))<=2 and box['x']+box['width']<=surface['x']+surface['width']+1,
              json.dumps({'box':box,'canvas':cv}))
        settle()
        check('nothing reaches the page while composing',value('field')=='' and not page_saw('keydown','beforeinput'))
        commit('你好');settle()
        check('a pinyin commit reaches the <input> once, as text',value('field')=='你好')
        check('no composed key and no composition event reached the page',not page_saw('keydown','compositionstart','compositionend')
              and [x['inputType'] for x in page_saw('beforeinput')]==['insertText'])
        check('the box is gone after the commit',not proxy.evaluate('e=>e.hasAttribute("data-composing")') and proxy.input_value()=='')

        # 2. Japanese in the contenteditable, converted then committed with a key 229 between.
        # The first key reports its letter with keyCode 229 before compositionstart, as some IMEs do.
        aim('rich');into_pane(60,110)
        cdp.send('Input.dispatchKeyEvent',{'type':'rawKeyDown','key':'n','code':'KeyN','windowsVirtualKeyCode':229})
        for t in ['に','にほ','にほん']:compose(t)
        cdp.send('Input.dispatchKeyEvent',{'type':'rawKeyDown','key':'n','code':'KeyN','windowsVirtualKeyCode':229})
        compose('日本');commit('日本');settle()
        check('a Japanese commit reaches the contenteditable once',value('rich')=='日本' and not page_saw('keydown'))

        # 3. Escape cancels: nothing reaches the page, and typing after it works.
        aim('field');into_pane()
        compose('nih');cdp.send('Input.dispatchKeyEvent',{'type':'rawKeyDown','key':'Escape','code':'Escape','windowsVirtualKeyCode':27})
        compose('');settle()
        check('Escape cancels with nothing sent',value('field')=='你好' and not page_saw('keydown','beforeinput'))
        page.keyboard.type('a');settle()
        check('ordinary typing after a cancel reaches the page once',value('field')=='你好a')

        # 4. Blur mid-composition (a click into the chat pane): Chrome commits the raw text before the blur.
        aim('field');into_pane();compose('wo');chat.click();settle()
        check('a pane switch mid-composition leaves no stray text',value('field')=='你好a' and not page_saw('beforeinput'))
        into_pane();commit('我');settle()
        check("the IME's late commit of the cancelled composition is dropped",value('field')=='你好a' and not page_saw('beforeinput'))
        # 4b. Focus leaving with no press at all: Chrome's own commit of the raw text precedes the blur.
        aim('field');into_pane();compose('ni');chat.evaluate('e=>e.focus()');settle()
        check('focus leaving mid-composition drops the raw text Chrome commits for it',value('field')=='你好a' and not page_saw('beforeinput'))

        # 5. A press in the page mid-composition cancels it.
        aim('field');into_pane();compose('ta');canvas.click(position={'x':120,'y':90});settle()
        check('a press in the page mid-composition sends nothing and closes the box',value('field')=='你好a'
              and not proxy.evaluate('e=>e.hasAttribute("data-composing")') and proxy.input_value()=='')

        # 6. An approval card mid-composition: nothing then, nothing after.
        aim('field');into_pane();compose('ka')
        _approval(mock,'ime-card');required(lambda:page.locator('[data-testid="approval-card"]').count()==1)
        commit('卡');settle()
        mock.emit('approval_resolved',{'req_id':'ime-card','decision':'deny'});required(lambda:page.locator('[data-testid="approval-card"]').count()==0)
        settle()
        check('a card mid-composition commits nothing, during or after',value('field')=='你好a' and not page_saw('beforeinput'))

        # 7. A navigation (owner_reset) mid-composition.
        aim('field');into_pane();compose('dao');remote.send(remote.sockets[-1],{'t':'owner_reset'})
        required(lambda:proxy.input_value()=='');commit('到');settle()
        check('a navigation mid-composition commits nothing',value('field')=='你好a' and not page_saw('beforeinput'))

        # 8. A socket lost mid-composition, then Retry.
        aim('field');into_pane();compose('lian');before=len(remote.sockets)
        remote.sockets[-1]['ws'].close()
        required(lambda:page.locator(active+' [data-testid="remote-error"]').count()==1)
        commit('连');settle()
        page.locator(active+' [data-testid="remote-error"] button').click();required(lambda:len(remote.sockets)>before)
        required(lambda:canvas.get_attribute('data-frame')=='ready');settle()
        check('a reconnect mid-composition commits nothing on either socket',value('field')=='你好a' and not page_saw('beforeinput'))
        into_pane();compose('hao');commit('好');settle()
        check('after the reconnect a commit works once',value('field')=='你好a好')

        # 9. The Windows emoji panel inserts with no composition at all.
        aim('field');into_pane();commit('😀');settle()
        check('emoji-panel text with no composition reaches the page once',value('field')=='你好a好😀'
              and [x['inputType'] for x in page_saw('beforeinput')]==['insertText'] and proxy.input_value()=='')

        # 10. Dead keys: Windows (a Dead keydown, then the composed key) and the composition kind.
        aim('field');into_pane()
        cdp.send('Input.dispatchKeyEvent',{'type':'rawKeyDown','key':'Dead','code':'Quote','windowsVirtualKeyCode':222})
        cdp.send('Input.dispatchKeyEvent',{'type':'keyUp','key':'Dead','code':'Quote','windowsVirtualKeyCode':222})
        cdp.send('Input.dispatchKeyEvent',{'type':'keyDown','key':'é','code':'KeyE','windowsVirtualKeyCode':69,'text':'é','unmodifiedText':'é'})
        cdp.send('Input.dispatchKeyEvent',{'type':'keyUp','key':'é','code':'KeyE','windowsVirtualKeyCode':69})
        compose('´');commit('é');settle()
        check('both kinds of dead key type their letter once',value('field')=='你好a好😀éé'
              and [x['key'] for x in page_saw('keydown')]==['é'])

        # 11. A commit followed at once by a key keeps their order.
        aim('field');into_pane();compose('x');commit('西');page.keyboard.press('b');settle()
        check('a commit and the next key keep their order',value('field')=='你好a好😀éé西b')
        check('the daemon validator refused nothing the pane sent',not remote.refused,str(remote.refused[:3]))
        check('no page errors',not errors,str(errors[:3]))
    finally:
        ctx.close();target_ctx.close()


def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    from tests.face.hud_v2_mock import MockDaemon
    mock=MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(channel='chrome',headless=True,args=['--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream'])
            print('Chrome',browser.version)
            try:ime_checks(browser,mock,f'http://127.0.0.1:{mock.port}',v2.check,v2.until,v2.guard,v2.FAKE_RECOGNIZER)
            except Exception as exc:
                import traceback;traceback.print_exc()
                v2.check('B-IME scenario completes',False,type(exc).__name__)
            finally:browser.close()
    finally:mock.stop()
    print(f'{v2.CHECKS} B-IME checks, {len(v2.FAILURES)} failures')
    return bool(v2.FAILURES)
if __name__=='__main__':sys.exit(main())
