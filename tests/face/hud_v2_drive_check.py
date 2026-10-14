"""BR9c owner presentation with ticketed synthetic drive events; owner switch behavior is exercised with fake Chrome."""
from __future__ import annotations
import json,os,re,sys,time
from datetime import datetime,timezone
from pathlib import Path
REPO=Path(__file__).resolve().parents[2]
if str(REPO) not in sys.path:sys.path.insert(0,str(REPO))
from tests.face.hud_v2_layout_check import _boot,_pane,_set_storage,_set_ws,_ws
from tests.face.hud_v2_remote_check import Remote
from tests.face.hud_v2_autoopen_mock import DevFixture
from tests.face.hud_v2_mock_browser import state,connected

class DriveFixture(DevFixture):
    def stream(self,handler,window):
        # The combined fixture needs both the dev-server event stream and the
        # Browser registry's normal owner-window presence hook.
        connected(self.mock,window)
        super().stream(handler,window)

class DriveRemote(Remote):
    def __init__(self,ctx,mock):
        self.drivers={};self.confirm_resume=True
        super().__init__(ctx,mock)
    def attach(self,ws):
        before=len(self.sockets);super().attach(ws)
        if len(self.sockets)>before:
            sock=self.sockets[-1]
            self.replay(sock)
    def replay(self,sock):
        row=self.drivers.get(sock['tid'])
        if row:self.send(sock,{'t':'agent','kind':'driver','thread_id':row['thread'],'provider':'fast'})
        self.send(sock,{'t':'agent','kind':'paused','paused':bool(row and row['paused']),'by':'owner' if row and row['paused'] else None})
        self.send(sock,{'t':'agent','kind':'list','actions':row['actions'] if row else []})
    def drive(self,sock,thread):
        self.drivers[sock['tid']]={'thread':thread,'paused':False,'actions':[]};self.replay(sock)
    def receive(self,sock,text):
        super().receive(sock,text);msg=json.loads(text);row=self.drivers.get(sock['tid'])
        if row and msg.get('t')=='stop':
            row['paused']=True;self.send(sock,{'t':'agent','kind':'paused','paused':True,'by':'stop'})
        if row and msg.get('t')=='resume' and self.confirm_resume:
            row['paused']=False;self.send(sock,{'t':'agent','kind':'paused','paused':False})

def private_card(req_id,tid,text='PRIVATE_CARD_TEXT',category='ordinary'):
    public={'req_id':req_id,'code':'ABCD','tool':'hud_browser_act','layer':'browser-gate','thread_id':'t1',
            'asked_at':datetime.now(timezone.utc).isoformat(),'timeout_s':120,'allowlistable':False,'hud_only':True}
    args={'action':'type','pane':'1','target':'e1','text':text,'file':''}
    detail={**public,'args':args,'command':json.dumps(args,ensure_ascii=False),'reason':'fixed','headline':'fixed',
            'browser':{'tab_id':tid,'action_id':'a'*32,'pane':1,'page_identity':1,'document_generation':2,'rect':None,
                       'action':'type','category':category,'host':'example.test','path':'/owner','target':'PRIVATE_CARD_LABEL','text':text}}
    return public,detail

def drive_checks(browser,mock,base,check,until,guard,init_script):
    print('\nBR9c: Browser controls and exact private consent')
    ctx=browser.new_context(viewport={'width':1800,'height':1000},permissions=['microphone'])
    guard(ctx);ctx.add_init_script(init_script);remote=DriveRemote(ctx,mock);fixture=DriveFixture(mock)
    details={};reads=[];held=[]
    def private(route):
        identity=route.request.url.rsplit('/',1)[-1];reads.append(identity)
        row=details.get(identity)
        if row=='hold':held.append((identity,route));return
        if row is None:route.fulfill(status=404,json={'error':'no pending Browser approval'})
        else:route.fulfill(status=200,json=row)
    ctx.route(re.compile(r'/browser/approvals/[^/?]+$'),private)
    page=ctx.new_page();page.set_default_timeout(5000);errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
    try:
        _boot(page,mock,base,until)
        panes=[{'view':'chat','projectId':'p1'} for _ in range(8)]
        panes[0]={'view':'browser','browserProfile':'p-11111111','browser':{'active':'pageaa','tabs':[{'id':'pageaa','url':'https://example.test/one','mode':'remote'}]}}
        raw={'preset':'grid4','order':[1,5,2,3],'kept':[4,6,7,8],'focused':5,'panes':panes}
        _set_storage(page,zoom='100',layout='{}');_set_ws(page,json.dumps(raw));_boot(page,mock,None,until,reload=True)
        page.evaluate("window.__hud.dispatch({type:'chat',pane:5,patch:{threadId:'t1'}})")
        active=_pane(1,'[data-browser-page][aria-hidden="false"]');canvas=page.locator(active+' canvas')
        until(lambda:canvas.get_attribute('data-frame')=='ready',timeout=5)
        sock=remote.sockets[-1];tid=sock['tid'];remote.drive(sock,'t1')
        status=page.locator(active+' [data-testid="drive-status"]');until(lambda:status.count()==1)
        check('BR9c banner names the displayed chat slot, not its internal ID','chat in pane 2 (fast path)' in status.inner_text())
        check('BR9c Browser front has a live owner receipt','a live owner window is required' not in page.locator(_pane(1)).inner_text())
        options=page.locator(_pane(1,'[data-testid="browser-options-toggle"]'))
        options.click()
        drive=page.locator(_pane(1,'[data-testid="browser-drive"]'))
        until(lambda:drive.is_enabled())
        check('DR named pane driving starts on',drive.is_checked())
        before=sum(method=='PATCH' and path=='/browser/panes/1' for method,path,_ in mock.calls)
        drive.evaluate('e=>e.click()')
        check('DR scripted pane click does not change permission',drive.is_checked() and sum(method=='PATCH' and path=='/browser/panes/1' for method,path,_ in mock.calls)==before)
        drive.click();until(lambda:not drive.is_checked())
        check('DR pane opt-out reaches owner permission route',any(method=='PATCH' and path=='/browser/panes/1' and body.get('drive') is False for method,path,body in mock.calls))
        drive.click();until(lambda:drive.is_checked())
        options.click();until(lambda:page.locator(_pane(1,'[data-testid="browser-options"]')).is_hidden())
        check('BR9c neutral banner contains no authorization or pixel preview',not re.search('Authorize|Approve|Deny',status.inner_text(),re.I) and status.locator('canvas,img,svg').count()==0)
        for i in range(25):remote.send(sock,{'t':'agent','kind':'action','id':f'action{i}','at':time.time()*1000,'action':'click','outcome':'done','target':'<img src=x>PRIVATE_ACTION_LABEL'+str(i),'text':'x'*100})
        until(lambda:'Actions (20)' in status.inner_text());status.get_by_role('button',name='Actions (20)',exact=True).click()
        if os.environ.get('BR9C_SHOTS'):page.screenshot(path=str(Path(os.environ['BR9C_SHOTS'])/'br9c-drive.png'))
        check('BR9c action list is capped at20 and page text stays inert',status.locator('li').count()==20 and status.locator('li img').count()==0 and 'PRIVATE_ACTION_LABEL' in status.inner_text())
        status.get_by_role('button',name='Actions (20)',exact=True).click()
        until(lambda:canvas.bounding_box()['height']>80)
        canvas.hover();page.keyboard.press('Control+d');check('BR9c ordinary pointer motion and HUD PTT do not take over',status.get_by_test_id('drive-stop').count()==1)
        canvas.click(position={'x':40,'y':40});until(lambda:status.get_by_test_id('drive-resume').count()==1)
        check('BR9c successful owner pointer input pauses immediately','you took over' in status.inner_text() and any(m.get('e',{}).get('type')=='mousePressed' for m in sock['messages']))
        remote.confirm_resume=False;status.get_by_test_id('drive-resume').click();until(lambda:any(m.get('t')=='resume' for m in sock['messages']))
        check('BR9c Resume waits for its daemon confirmation',status.get_by_test_id('drive-resume').is_disabled() and 'Paused' in status.inner_text())
        remote.send(sock,{'t':'agent','kind':'paused','paused':False});until(lambda:status.get_by_test_id('drive-stop').count()==1)
        remote.confirm_resume=True;status.get_by_test_id('drive-stop').click();until(lambda:'you stopped' in status.inner_text() and any(m.get('t')=='stop' for m in sock['messages']))
        check('BR9c Stop uses only the ticketed socket',any(m.get('t')=='stop' for m in sock['messages']) and not any(bool(re.search(r'/browser/(?:act|tabs/[^/]+/act)(?:/|$)',path)) for _,path,_ in mock.calls),str([path for _,path,_ in mock.calls if bool(re.search(r'/browser/(?:act|tabs/[^/]+/act)(?:/|$)',path))]))
        status.get_by_test_id('drive-resume').click();until(lambda:status.get_by_test_id('drive-stop').count()==1)
        page.keyboard.press('Control+,');until(lambda:page.locator('[data-testid="settings-page"]').count()==1)
        page.get_by_role('button',name='Browser',exact=True).click()
        setting=page.locator('[data-testid="browser-agents-drive"]')
        until(lambda:setting.is_enabled())
        check('DR Settings global driving preference is ON',setting.is_checked())
        before=sum(method=='PATCH' and path=='/browser/settings' and 'agents_may_drive' in body for method,path,body in mock.calls)
        setting.evaluate('e=>e.click()')
        check('DR scripted global click does not change permission',setting.is_checked() and sum(method=='PATCH' and path=='/browser/settings' and 'agents_may_drive' in body for method,path,body in mock.calls)==before)
        setting.focus();page.keyboard.press('Space');until(lambda:not setting.is_checked())
        check('DR global off uses only explicit owner settings PATCH',mock.browser_settings.value('agents_may_drive') is False)
        setting.click();until(lambda:setting.is_checked())
        check('BR9c Settings covers existing work inertly',page.locator('.workspace-surface').evaluate('e=>e.inert'))
        old_count=len(remote.sockets);page.locator('[data-testid="settings-close"]').click();until(lambda:len(remote.sockets)>old_count)
        sock=remote.sockets[-1];until(lambda:status.get_by_test_id('drive-stop').count()==1)
        check('BR9c reattachment uses replay on the same host tab',sock['tid']==tid and remote.bad_tickets==0)
        for zoom in ('70','160'):
            _set_storage(page,zoom=zoom,layout='{}');_boot(page,mock,None,until,reload=True)
            req='drivecard'+zoom.zfill(3)
            public,detail=private_card(req,tid,'<img src=x onerror=bad> PRIVATE_CARD_TEXT'+(' whole action'*700))
            details[req]=detail;mock.world['approvals']=[public];mock.emit('approval_requested',public)
            card=page.locator('[data-testid="approval-card"]');until(lambda:card.locator('[data-testid="browser-approval-details"]').count()==1)
            if os.environ.get('BR9C_SHOTS'):page.screenshot(path=str(Path(os.environ['BR9C_SHOTS'])/('br9c-card-'+zoom+'.png')))
            check('BR9c consent controls remain visible with a long command at '+zoom,card.locator('.btns').evaluate('(e)=>{const r=e.getBoundingClientRect();return r.top>=0&&r.bottom<=innerHeight;}'))
            check('BR9c exact private command is visible at '+zoom,card.locator('[data-testid="approval-command"]').inner_text()==detail['command'])
            check('BR9c private preview is explicitly unavailable at '+zoom,card.locator('[data-testid="browser-approval-preview"]').inner_text()=='Preview unavailable' and card.locator('canvas,img').count()==0)
            check('BR9c private card never offers Always at '+zoom,card.locator('[data-testid="approval-always"]').count()==0)
            page.keyboard.press('Tab');check('BR9c only Deny enters card Tab order at '+zoom,page.locator('[data-testid="approval-deny"]').evaluate('e=>e===document.activeElement'))
            page.keyboard.press('Shift+Tab');page.keyboard.press('Enter')
            until(lambda:mock.sent('POST','/approvals/'+req),timeout=3)
            check('BR9c keyboard default remains cheap denial at '+zoom,mock.sent('POST','/approvals/'+req)[-1].get('decision')=='deny')
        _set_storage(page,zoom='100',layout='{}');_boot(page,mock,None,until,reload=True);sock=remote.sockets[-1];tid=sock['tid'];remote.drive(sock,'t1')
        public,detail=private_card('drive_card03',tid);details[public['req_id']]=detail
        queued,queued_detail=private_card('drive_card04',tid,'PRIVATE_QUEUED_TEXT');details[queued['req_id']]=queued_detail
        mock.world['approvals']=[public,queued];mock.emit('approval_requested',public);mock.emit('approval_requested',queued)
        card=page.locator('[data-testid="approval-card"]');until(lambda:card.locator('[data-testid="browser-approval-details"]').count()==1)
        check('BR9c queued private details are not fetched or shown','drive_card04' not in reads and 'PRIVATE_QUEUED_TEXT' not in page.locator('body').inner_text())
        check('BR9c private detail never enters public store or browser storage',all(marker not in page.evaluate("JSON.stringify(window.__hud.state())+JSON.stringify(localStorage)+JSON.stringify(sessionStorage)") for marker in ['PRIVATE_CARD_TEXT','PRIVATE_CARD_LABEL','PRIVATE_ACTION_LABEL']))
        allow=card.locator('[data-testid="approval-allow"]');allow.scroll_into_view_if_needed();allow.evaluate('e=>e.focus()')
        page.keyboard.press('Enter');page.keyboard.press('Space');check('BR9c forced focus still cannot authorize by keyboard',not mock.sent('POST','/approvals/drive_card03'))
        allow.click();until(lambda:mock.sent('POST','/approvals/drive_card03'))
        check('BR9c deliberate private approval sends allow with noAlways',mock.sent('POST','/approvals/drive_card03')==[{'decision':'allow','always':False}])
        until(lambda:card.get_attribute('data-req')=='drive_card04');page.keyboard.press('Escape');until(lambda:card.count()==0)
        check('BR9c queued card loads only when it becomes current',reads.count('drive_card04')==1)
        a,ad=private_card('drive_card05',tid);b,bd=private_card('drive_card06',tid,'CURRENT_ONLY');details[a['req_id']]='hold';details[b['req_id']]=bd
        mock.world['approvals']=[a];mock.emit('approval_requested',a);until(lambda:len(held)==1)
        check('BR9c loading card can deny but cannot authorize',page.locator('[data-testid="approval-allow"]').is_disabled() and not page.locator('[data-testid="approval-deny"]').is_disabled())
        mock.world['approvals']=[b];mock.emit('approval_resolved',{'req_id':a['req_id'],'decision':'deny'});mock.emit('approval_requested',b)
        until(lambda:'CURRENT_ONLY' in card.inner_text())
        try:held[0][1].fulfill(status=200,json=ad)
        except Exception:pass  # aborted owner detail request has no receiving component
        page.wait_for_timeout(100)
        check('BR9c late old detail cannot replace the current request','CURRENT_ONLY' in card.inner_text() and 'PRIVATE_CARD_TEXT' not in card.inner_text())
        page.keyboard.press('Escape');until(lambda:card.count()==0)
        delegated,_=private_card('drive_card07',tid);delegated['layer']='always-ask'
        details[delegated['req_id']]={**delegated,'allowlistable':False,'args':{},'command':None,
            'browser':{'kind':'delegation','tool':'hud_browser_act'},'headline':'PRIVATE UNUSED','reason':'PRIVATE UNUSED'}
        mock.world['approvals']=[delegated];mock.emit('approval_requested',delegated)
        until(lambda:card.locator('[data-testid="browser-delegation-details"]').count()==1)
        check('BR9c named-rule card grants no action or invented page facts','This grants no Browser action' in card.inner_text()
            and not re.search('PRIVATE|Preview|Pane|example.test',card.inner_text()) and card.locator('[data-testid="approval-command"],canvas,img').count()==0)
        card.locator('[data-testid="approval-allow"]').click();until(lambda:card.count()==0)
        check('BR9c named-rule consent is one ordinary noAlways answer',mock.sent('POST','/approvals/drive_card07')==[{'decision':'allow','always':False}])
        release,rd=private_card('drive_card08',tid)
        download={'id':'abcde123','name':'sample.bin','size':1024};args={'action':'release_download','download_id':download['id'],'name':download['name'],'size':download['size']}
        rd.update(args=args,command=json.dumps(args));rd['browser'].update(action='release_download',category='executable_download',text='',rect=None,download=download)
        details[release['req_id']]=rd;mock.world['approvals']=[release];mock.emit('approval_requested',release)
        until(lambda:'Release this executable download?' in card.inner_text())
        check('BR9c release card shows exact file facts without an execute claim','sample.bin' in card.inner_text()
            and '1,024 bytes' in card.inner_text() and 'It does not run it.' in card.inner_text() and card.locator('[data-testid="approval-command"]').inner_text()==rd['command'])
        page.keyboard.press('Escape');until(lambda:card.count()==0)
        sock=remote.sockets[-1];remote.drive(sock,'t1')
        chat=page.locator(_pane(5,'[data-testid="input"]'));chat.click();chat.fill('keep this draft')
        until(lambda:page.evaluate('window.__hud.mic.state().open'))
        focus="({focused:window.__hud.workspace().ws.focused,selected:window.__hud.state().selectedChat,micOpen:window.__hud.mic.state().open,micClaimed:window.__hud.mic.state().claimed})"
        before=page.evaluate(focus);original=_ws(page)['ws']['panes'][0]['browser']['active'];old=len(state(mock).rows)
        automatic_start=len(mock.calls)
        entry=fixture.add(mock.dev_port+11)
        until(lambda:len(_ws(page)['ws']['panes'][0]['browser']['tabs'])==2,timeout=5)
        current=_ws(page)['ws']['panes'][0]['browser']
        check('BR9c active driven pane gets a background Direct tab',current['active']==original and current['tabs'][-1].get('mode','direct')=='direct' and len(state(mock).rows)==old)
        check('BR9c auto-open retains exact focus/chat/mic ownership',page.evaluate(focus)==before and chat.evaluate('e=>e===document.activeElement'))
        check('BR9c automatic placement has an exact daemon receipt',bool(mock.posted('/dev-servers/'+entry.id+'/placed')))
        check('BR9c no screenshot/capture/drive enable request was manufactured',not any('/capture' in path or bool(re.search(r'/browser/(?:act|tabs/[^/]+/act)(?:/|$)',path)) or body.get('drive') is True for _,path,body in mock.calls[automatic_start:]),str([path for _,path,body in mock.calls[automatic_start:] if '/capture' in path or bool(re.search(r'/browser/(?:act|tabs/[^/]+/act)(?:/|$)',path)) or body.get('drive') is True]))
        check('BR9c no page errors or control-listener request',not errors and not mock.api_hits,str(errors[:3]))
    except Exception:
        print('BR9c fixture state',page.evaluate('window.__hud?.workspace()'))
        print('BR9c fixture errors',errors)
        print('BR9c pane1',page.locator(_pane(1)).inner_text()[:1600] if page.locator(_pane(1)).count() else 'not mounted')
        print('BR9c sockets',len(remote.sockets),'rows',list(state(mock).rows))
        raise
    finally:ctx.close();fixture.close()

def main():
    from playwright.sync_api import sync_playwright
    from tests.face import hud_v2_check as v2
    from tests.face.hud_v2_mock import MockDaemon
    mock=MockDaemon(0).start()
    try:
        with sync_playwright() as pw:
            browser=pw.chromium.launch(headless=True,args=['--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream'])
            try:drive_checks(browser,mock,f'http://127.0.0.1:{mock.port}',v2.check,v2.until,v2.guard,v2.FAKE_RECOGNIZER)
            except Exception as exc:v2.check('BR9c scenario completes',False,str(exc))
            finally:browser.close()
    finally:mock.stop()
    print(f'{v2.CHECKS} Browser drive UI checks, {len(v2.FAILURES)} failures');return bool(v2.FAILURES)
if __name__=='__main__':sys.exit(main())
