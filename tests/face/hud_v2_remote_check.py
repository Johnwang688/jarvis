"""BR3b Remote UI over the production pane registry and a hermetic stream.
The mock host never loads an external page. Tickets are consumed once, frames
are real JPEGs, and actual Chromium input/layout are observed end to end.
"""
from __future__ import annotations
import io,json,re,struct,sys,time
from pathlib import Path
from urllib.parse import urlparse,parse_qs
REPO=Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:sys.path.insert(0,str(REPO))
from tests.face.hud_v2_layout_check import _boot,_pane,_set_ws,_set_storage,_ws,_approval
from tests.face.hud_v2_mock_browser import state

class Remote:
    def __init__(self,ctx,mock):
        self.mock=mock;self.sockets=[];self.bad_tickets=0
        ctx.route_web_socket(re.compile(rf"^ws://127\.0\.0\.1:{mock.port}/browser/tabs/"),self.attach)
    def attach(self,ws):
        parsed=urlparse(ws.url);tid=parsed.path.split('/')[3]
        ticket=parse_qs(parsed.query).get('ticket',[''])[0]
        host=state(self.mock)
        if host.tickets.pop(ticket,None)!=tid or tid not in host.rows:
            self.bad_tickets+=1;return
        sock={'ws':ws,'tid':tid,'messages':[],'frames':0};self.sockets.append(sock)
        ws.on_message(lambda text:self.receive(sock,text))
        self.send(sock,{'t':'location','url':host.rows[tid]['url'],'title':'Remote fixture','security':'secure'})
    def send(self,sock,msg):sock['ws'].send(json.dumps(msg))
    def receive(self,sock,text):
        msg=json.loads(text);sock['messages'].append(msg)
        if msg.get('t')=='resize':
            from PIL import Image
            width=msg['width'];height=msg['height'];out=io.BytesIO()
            Image.new('RGB',(width,height),(24,30,35)).save(out,'JPEG')
            sock['frames']+=1;sock['ws'].send(struct.pack('!IHH',sock['frames'],width,height)+out.getvalue())
        if msg.get('t')=='dialog':self.send(sock,{'t':'dialog_closed','id':msg['id']})
    def inputs(self):return [m for s in self.sockets for m in s['messages'] if m.get('t') in ('key','mouse','text','dialog','selection','focus','blur')]


def remote_checks(browser,mock,base,check,until,guard,init_script):
    print('\nRemote Browser: owner profiles, streaming, input and lifecycle')
    ctx=browser.new_context(viewport={'width':1600,'height':1000},permissions=['microphone'])
    guard(ctx);ctx.add_init_script(init_script);remote=Remote(ctx,mock);page=ctx.new_page();page.set_default_timeout(5000)
    errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        _boot(page,mock,base,until)
        raw={'preset':'cols2','focused':2,'panes':[{'view':'chat'}, {'view':'browser','browserProfile':'p-11111111','see':False,'browser':{'active':'pageaa','tabs':[{'id':'pageaa','url':'https://example.test/one','mode':'remote','source':{'kind':'unknown'}},{'id':'pagebb','url':'https://example.test/two','mode':'remote'}]}}, {'view':'file'},{'view':'task'}]}
        _set_storage(page,zoom='100',layout='{}');_set_ws(page,json.dumps(raw));_boot(page,mock,None,until,reload=True)
        active=_pane(2,'[data-browser-page][aria-hidden="false"]')
        canvas=page.locator(active+' canvas');box=page.locator(active+' [data-testid="browser-url"]')
        until(lambda:canvas.get_attribute('data-frame')=='ready',timeout=5)
        host=state(mock);first=remote.sockets[-1];tid=first['tid']
        check('BR3b: only the active stored Remote tab starts Chrome',len(host.rows)==1,str(host.rows))
        check('BR3b: Remote has a JPEG canvas and no Direct frame',canvas.count()==1 and page.locator('[data-testid="browser-frame"]').count()==0)
        calls=mock.calls
        check('BR3b: stored page uses restore, never owner navigate',any(p==f'/browser/tabs/{tid}/restore' for _,p,_ in calls) and not any(p==f'/browser/tabs/{tid}/navigate' for _,p,_ in calls))
        check('BR3b: saved see-off is shown and drive cannot turn on',not page.locator(_pane(2,'[data-testid="browser-see"]')).is_checked() and page.locator(_pane(2,'[data-testid="browser-drive"]')).is_disabled())
        check('BR3b: restored metadata retains unknown source',_ws(page)['ws']['panes'][1]['browser']['tabs'][0]['source']=={'kind':'unknown'})
        until(lambda:any(m.get('t')=='ack' for m in first['messages']),timeout=3)
        check('BR3b: decoded frames are acknowledged',any(m.get('t')=='ack' for m in first['messages']))
        canvas.click(position={'x':50,'y':50});page.keyboard.type('abc');page.keyboard.press('Control+p')
        until(lambda:any(m.get('e',{}).get('key')=='c' for m in first['messages']),timeout=3)
        check('BR3b: ordinary keys and Ctrl+P reach Remote',all(any(m.get('t')=='key' and m.get('e',{}).get('key')==k for m in first['messages']) for k in ['a','b','c','p']))
        before=len(remote.inputs());page.keyboard.press('Control+l')
        check('BR3b: Ctrl+L focuses this pane address without page shortcut',box.evaluate('e=>e===document.activeElement') and not any(m.get('e',{}).get('key')=='l' for m in remote.inputs()[before:]))
        chat=page.locator(_pane(1,'[data-testid="input"]'));chat.click();chat.fill('owner typing')
        remote.send(first,{'t':'dialog','id':'1234abcd','kind':'prompt','message':'<img src=x onerror=alert(1)>','default':'answer'})
        dialog=page.locator(_pane(2,'[data-testid="browser-dialog"]'));until(lambda:dialog.count()==1,timeout=3)
        check('BR3b: page dialog stays inline as plain text and never takes focus',dialog.locator('img').count()==0 and '<img' in dialog.inner_text() and chat.evaluate('e=>e===document.activeElement'))
        _approval(mock,'remote-card');until(lambda:page.locator('[data-testid="approval-card"]').count()==1,timeout=3)
        before=len(remote.inputs());page.keyboard.type('card only');page.locator(active+' [data-testid="remote-input"]').evaluate("e=>e.dispatchEvent(new KeyboardEvent('keydown',{key:'z',code:'KeyZ',bubbles:true}))")
        check('BR3b: approval disables page answers and all page input',dialog.get_by_role('button',name='OK',exact=True).is_disabled() and len(remote.inputs())==before)
        mock.emit('approval_resolved',{'req_id':'remote-card','decision':'deny'});until(lambda:page.locator('[data-testid="approval-card"]').count()==0,timeout=3)
        dialog.get_by_role('button',name='OK',exact=True).click();until(lambda:dialog.count()==0,timeout=3)
        check('BR3b: owner answer reaches its exact dialog',any(m.get('t')=='dialog' and m.get('id')=='1234abcd' and m.get('text')=='answer' for m in first['messages']))
        before=len(remote.sockets);page.keyboard.press('Control+,');until(lambda:page.locator('[data-testid="settings-page"]').count()==1,timeout=3)
        until(lambda:any(c[1]=='/browser/front' and c[2].get('covered') for c in mock.calls),timeout=3)
        check('BR3b: Settings hides Remote while retaining its host handle',tid in host.rows and page.locator('.workspace-surface').evaluate('e=>e.inert'))
        page.locator('[data-testid="settings-close"]').click();until(lambda:len(remote.sockets)>before,timeout=3)
        check('BR3b: Settings return reattaches with a fresh ticket and same tab',remote.sockets[-1]['tid']==tid and len(host.rows)==1 and remote.bad_tickets==0)
        first=remote.sockets[-1]
        page.locator(_pane(2,'[data-browser-tab="pagebb"]')).click();until(lambda:len(host.rows)==2,timeout=3)
        check('BR3b: selecting a stored background tab starts it lazily',len(host.rows)==2)
        page.locator(_pane(2,'[data-browser-tab="pageaa"]')).click();until(lambda:remote.sockets[-1]['tid']==tid,timeout=3)
        see=page.locator(_pane(2,'[data-testid="browser-see"]'));see.click();until(lambda:see.is_checked(),timeout=3)
        check('BR3b: see change is host acknowledged',any(m=='PATCH' and p=='/browser/panes/2' and b.get('see') is True for m,p,b in mock.calls))
        box.fill('https://example.test/owner');box.press('Enter');until(lambda:_ws(page)['ws']['panes'][1]['browser']['tabs'][0]['url']=='https://example.test/owner',timeout=3)
        check('BR3b: actual owner address clears provenance only on success','source' not in _ws(page)['ws']['panes'][1]['browser']['tabs'][0])
        page.wait_for_timeout(200)  # let the successful address front receipt settle
        mock.emit('browser_viewed',{'tab_id':tid,'at':'2026-10-13T12:30:00Z','refused':False});until(lambda:page.locator(active+' [data-testid="browser-viewed"]').count()==1,timeout=3)
        check('BR3b: looked indicator consumes the bound ISO timestamp','Jarvis looked' in page.locator(active+' [data-testid="browser-viewed"]').inner_text())
        profile=page.locator(_pane(2,'select[aria-label="Browser profile"]'));profile.select_option('private')
        until(lambda:any(row['profile']=='private' for row in host.rows.values()),timeout=4)
        until(lambda:page.locator(active+' canvas').get_attribute('data-frame')=='ready',timeout=4)
        check('BR3b: Private conversion uses only Remote and a distinct context',_ws(page)['ws']['panes'][1].get('browserCtx') is not None and page.locator(active+' select[aria-label="Browser mode"]').is_disabled() and page.locator('[data-testid="browser-frame"]').count()==0)
        stored=json.loads(page.evaluate("localStorage.getItem('jarvis.hud.workspace')"));private=stored['panes'][1]
        check('BR3b: Private storage contains no URL or title',all(not t.get('url') and not t.get('title') for t in private['browser']['tabs']) and not private.get('browserUrl'))
        private_tid=remote.sockets[-1]['tid'];private_ctx=_ws(page)['ws']['panes'][1]['browserCtx']
        check('BR3b: explicit Private conversion navigates as its owner gesture',any(p==f'/browser/tabs/{private_tid}/navigate' for _,p,_ in mock.calls))
        _set_storage(page,zoom='130',layout='{}');_boot(page,mock,None,until,reload=True)
        until(lambda:remote.sockets[-1]['tid']==private_tid and canvas.get_attribute('data-frame')=='ready',timeout=4)
        check('BR3b: a live Private handle reattaches after reload without a new context',_ws(page)['ws']['panes'][1].get('browserCtx')==private_ctx and not _ws(page)['ws']['panes'][1].get('browserPrivateEnded'))
        bounds=canvas.bounding_box();page.mouse.click(bounds['x']+bounds['width']/2,bounds['y']+bounds['height']/2)
        current=remote.sockets[-1];resize=next(m for m in reversed(current['messages']) if m['t']=='resize')
        pointer=next(m['e'] for m in reversed(current['messages']) if m['t']=='mouse' and m['e']['type']=='mousePressed')
        check('BR3b: 130 percent zoom maps centre input into the streamed viewport',abs(pointer['x']-resize['width']/2)<=2 and abs(pointer['y']-resize['height']/2)<=2,str((pointer,resize)))
        check('BR3b: zoomed controls remain inside the Browser pane',page.locator(_pane(2,'[data-testid="browser-profile"]')).evaluate('e=>e.scrollWidth<=e.clientWidth+1'))
        with mock.browser_lock:host.rows={tid:row for tid,row in host.rows.items() if row['ctx']!=private_ctx}
        calls=len(mock.calls);_boot(page,mock,None,until,reload=True)
        until(lambda:page.get_by_role('button',name='Start a new Private session',exact=True).count()==1,timeout=4)
        check('BR3b: expired Private does not replay its old address or allocate',not any(m=='POST' and (p=='/browser/tabs' or p.endswith('/restore')) for m,p,_ in mock.calls[calls:]))
        page.get_by_role('button',name='Start a new Private session',exact=True).click();until(lambda:any(row['profile']=='private' for row in host.rows.values()),timeout=4)
        check('BR3b: explicitly restarted Private is blank with a fresh context',_ws(page)['ws']['panes'][1].get('browserCtx')!=private_ctx and all(row['url']=='about:blank' for row in host.rows.values() if row['profile']=='private'))
        profile.select_option('p-11111111');until(lambda:profile.input_value()=='p-11111111',timeout=4)
        page.locator(_pane(2,'[data-testid="browser-profile"]')).get_by_role('button',name='Manage',exact=True).click()
        manage=page.locator(_pane(2,'[data-testid="browser-profile-manage"]'));name=manage.get_by_label('Profile name')
        name.fill('Work');manage.get_by_role('button',name='New profile',exact=True).click();until(lambda:any(p['label']=='Work' for p in host.profiles),timeout=4)
        new_profile=next(p['id'] for p in host.profiles if p['label']=='Work');until(lambda:profile.input_value()==new_profile,timeout=4)
        check('BR3b: creating a profile selects the returned host identity',profile.input_value()==new_profile)
        name.fill('Renamed');manage.get_by_role('button',name='Rename',exact=True).click();until(lambda:any(p['id']==new_profile and p['label']=='Renamed' for p in host.profiles),timeout=4)
        check('BR3b: rename retains profile identity',profile.input_value()==new_profile)
        manage.get_by_role('button',name='Sign in to Chrome',exact=True).click();until(lambda:any(row['url']=='chrome://settings/syncSetup' for row in host.rows.values()),timeout=4)
        check('BR3b: sign-in uses the dedicated owner route',any(p==f'/browser/profiles/{new_profile}/signin' for _,p,_ in mock.calls))
        manage.get_by_role('button',name='Sign out of everything',exact=True).click();check('BR3b: profile clearing waits for explicit confirmation',not any(p==f'/browser/profiles/{new_profile}/forget' for _,p,_ in mock.calls))
        manage.get_by_role('button',name='Confirm',exact=True).click();until(lambda:any(p==f'/browser/profiles/{new_profile}/forget' for _,p,_ in mock.calls),timeout=4)
        check('BR3b: forget retains its label',any(p['id']==new_profile and p['label']=='Renamed' for p in host.profiles))
        manage.get_by_role('button',name='Delete profile',exact=True).click();manage.get_by_role('button',name='Confirm',exact=True).click();until(lambda:all(p['id']!=new_profile for p in host.profiles),timeout=4)
        check('BR3b: deletion never silently picks another profile',profile.input_value()==new_profile)
        check('BR3b: no stale ticket was redeemed',remote.bad_tickets==0)
        check('BR3b: no Remote page errors',not errors,str(errors[:3]))
        check('BR3b: no control listener request',not mock.api_hits,str(mock.api_hits[:3]))
    except Exception:
        print('Remote failure state:', page.locator(_pane(2)).inner_text()[:1600])
        raise
    finally:ctx.close()


def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    from tests.face.hud_v2_mock import MockDaemon
    mock=MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,args=['--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream'])
            try:remote_checks(browser,mock,f'http://127.0.0.1:{mock.port}',v2.check,v2.until,v2.guard,v2.FAKE_RECOGNIZER)
            except Exception as exc:v2.check('Remote scenario completes',False,str(exc))
            finally:browser.close()
    finally:mock.stop()
    print(f'{v2.CHECKS} Remote checks, {len(v2.FAILURES)} failures')
    return bool(v2.FAILURES)
if __name__=='__main__':sys.exit(main())
