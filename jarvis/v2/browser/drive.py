"""Caller-bound Browser action gate (BR9b).

Production reads only the owner's BrowserSettings switch. Desk, pane and
Private consent plus all existing action guards remain mandatory.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import hashlib
import json
import math
import secrets as random
import threading
from urllib.parse import urlsplit

from jarvis import runtime, untrusted
from jarvis.tools import secrets
from jarvis.v2 import terminal_guard
from jarvis.v2.approvals import ApprovalRequest
from jarvis.v2.model import utcnow
from jarvis.v2.provider import Decision
from . import act_rules as rules, act_review as review, policy
from .drive_state import DriveState, DriveStateRefused, current_turn
from .look import LookRefused, CHANGED, SOURCE_OFF, SOURCE_UNKNOWN
from .panes import PaneRefused

OFF = "Agents may drive is off; the owner must use the Browser directly."
STOPPED = "this Browser action was stopped; look again before trying another action"
CHANGED_ACTION = "the Browser target or page changed after review; look again"


def _shipping_enabled(daemon) -> bool:
    """The owner's one production switch; no alternate env or CLI route."""
    return daemon.browser_settings.value("agents_may_drive") is True


@dataclass(frozen=True)
class Action:
    action: str
    pane: str = ""
    target: str = ""
    text: str = field(default="", repr=False)
    file: str = field(default="", repr=False)

    def validate(self):
        if any(not isinstance(v,str) for v in (self.action,self.pane,self.target,self.text,self.file)):
            raise LookRefused("Browser action arguments must be strings")
        if self.action not in rules.ACTIONS:raise LookRefused(rules.R_ACTION)
        if self.pane and self.pane not in tuple(str(i) for i in range(1,9)):
            raise LookRefused("pane must be a visible slot from 1 to 8 or empty")
        targeted=self.action in ("click","type","select","upload")
        if targeted and (not self.target.startswith("e") or not self.target[1:].isdigit() or len(self.target)>4):
            raise LookRefused("choose an interactive ref from a fresh look at this pane")
        if not targeted and self.action!="scroll" and self.target:raise LookRefused("that Browser action does not take a target ref")
        limit=rules.MAX_TYPE_CHARS if self.action=="type" else rules.MAX_NAV_URL
        if len(self.text)>limit or len(self.file)>4096:raise LookRefused("the Browser action argument is too long")
        if self.action=="press" and rules._key_name(self.text) is None:raise LookRefused(rules.R_KEY)
        if self.action=="scroll" and self.text.strip().lower() not in rules.SCROLLS:raise LookRefused(rules.R_SCROLL)
        if self.action not in ("navigate","type","press","scroll","select") and self.text:
            raise LookRefused("that Browser action does not take text")
        if self.action!="upload" and self.file:raise LookRefused("only upload takes a file")


@dataclass(frozen=True)
class ActResult:
    outcome: str
    text: str
    ref_snapshot: object = field(default=None,repr=False,compare=False)
    ref_scan: object = field(default=None,repr=False,compare=False)


@dataclass
class Guard:
    service: object
    turn: object
    snapshot: object
    lease: object
    capability: object
    input_serial: int
    attachment_generation: int
    hold_count: int
    stop: object
    desk: object
    download: object = None

    def __call__(self, *, own_card=None, navigation=False):
        return self.service._current(self,own_card=own_card,navigation=navigation)


class BrowserDrive:
    def __init__(self, daemon, *, enabled=None, state=None):
        self.daemon=daemon
        self.enabled=enabled if enabled is not None else lambda: _shipping_enabled(daemon)
        self.state=state if state is not None else DriveState()
        self.closed=False
        from .drive_downloads import Downloads
        self.downloads=Downloads(self)

    def _turn(self):
        if self.closed or self.enabled() is not True:raise LookRefused(OFF)
        turn=current_turn()
        if turn is None or not runtime.at_desk() or runtime.depth()!=0:raise LookRefused(rules.R_DESK)
        if turn.strict:raise LookRefused(rules.R_STRICT)
        if runtime.should_stop()():raise LookRefused(STOPPED)
        self._live(turn)
        return turn

    def _live(self, turn):
        sessions=getattr(self.daemon,"_sessions",None)
        if sessions is not None:
            with self.daemon._lock:
                session=sessions.get(turn.thread_id)
                if (session is None or session.retired or session.turn_id!=turn.turn_id
                        or session.cancelled.is_set()):raise LookRefused(STOPPED)

    def _url(self,url):
        parsed=policy.judge_url(url,refused=self.daemon.browser.refused_ports())
        parts=urlsplit(parsed)
        # Agent navigation never enters the special owner-only sign-in path.
        if parts.scheme not in ("http","https"):raise LookRefused("that Browser address is owner-only")
        kind,address=policy.host_kind(parts.hostname or "")
        local=kind=="loopback_name" or (kind=="literal" and (address.is_loopback or address.is_unspecified))
        if local and not self.daemon.browser.loopback_open(parts.port or (443 if parts.scheme=="https" else 80)):
            raise LookRefused("that address has no current owner-granted network access")
        return parsed

    def _binding(self,snapshot, *, navigation=False):
        if snapshot.mode!="remote":raise LookRefused("driving needs an owner's visible Remote Browser pane")
        if not snapshot.see or not snapshot.drive:raise LookRefused("the owner has not allowed this Browser pane to be driven")
        if not snapshot.drawn or snapshot.covered:raise LookRefused(CHANGED)
        registry=self.daemon.browser_panes
        fresh=registry.after_navigation(snapshot) if navigation else snapshot
        if fresh is None or (not navigation and not registry.still_current(snapshot)):raise LookRefused(CHANGED)
        source=fresh.source
        if source.kind not in ("owner","terminal","thread"):raise LookRefused(SOURCE_UNKNOWN)
        if source.kind=="terminal" and not self.daemon.terminals.readable_source(source.terminal_id):
            raise LookRefused(SOURCE_OFF)
        self._url(fresh.url)
        return fresh

    def _owner(self,snapshot, *, own_card=None):
        row=self.daemon.browser.owner_state(snapshot.tab_id)
        if (row.get("paused") is not False or row.get("shown") is not True or row.get("visible") is not True
                or row.get("depressed") is not False or row.get("input_busy") is not False):
            raise LookRefused("the owner has paused or is no longer showing this Browser pane")
        quiet=row.get("owner_quiet_s")
        if quiet is not None and (type(quiet) not in (int,float) or not math.isfinite(quiet) or quiet<rules.OWNER_QUIET_S):
            raise LookRefused("the owner is using this Browser pane; wait for five quiet seconds")
        if any(type(row.get(k)) is not int or row[k]<0 for k in ("input_serial","attachment_generation","hold_count")):
            raise LookRefused(STOPPED)
        pending=self.daemon.approvals.pending()
        if any(own_card is None or request is not own_card for request in pending):
            raise LookRefused("an approval card is waiting; finish it before driving")
        if row.get("held") is not False and not (own_card is not None and pending==[own_card]):
            raise LookRefused("an approval card is waiting; finish it before driving")
        return row

    def _current(self,guard, *, own_card=None,navigation=False):
        if (self.closed or self.enabled() is not True or guard.turn.closed or guard.turn.strict
                or guard.stop() or not guard.desk() or not self.state.current(guard.lease)):
            raise LookRefused(STOPPED)
        self._live(guard.turn)
        fresh=self._binding(guard.snapshot,navigation=navigation)
        if guard.capability is not None and not self.state.refs_current(guard.capability):raise LookRefused(CHANGED_ACTION)
        row=self._owner(fresh,own_card=own_card)
        if (row["input_serial"]!=guard.input_serial or row["attachment_generation"]!=guard.attachment_generation):
            raise LookRefused("the owner took over this Browser pane; look again")
        allowed_holds=(guard.hold_count,guard.hold_count+1) if own_card is not None else (guard.hold_count,)
        if row["hold_count"] not in allowed_holds:raise LookRefused("another approval interrupted this Browser action")
        return fresh,row

    def publish_look(self,snapshot,scan):
        """Called only after the Remote tool read has passed its final guard."""
        turn=current_turn()
        if turn is None or turn.strict or self.closed or snapshot.mode!="remote":return
        if self.enabled() is not True:return
        refs=scan.get("refs")
        if not isinstance(refs,list):return
        self.state.publish(turn,snapshot,scan.get("serial"),[row.get("ref") for row in refs if isinstance(row,dict)])

    def _facts(self,action,prepared,guard,row):
        judged,hourly=self.state.counts(guard.lease)
        return rules.Facts(action.action,key=action.text if action.action=="press" else "",
            text=action.text,nav_url=action.text if action.action=="navigate" else "",
            url=prepared.url,title=prepared.title,target=prepared.target,form=prepared.form,
            context=prepared.context,context_flagged=prepared.context_flagged,upload_path=action.file,
            project_dir=guard.turn.project_root,profile=guard.snapshot.profile,
            card_pending=False,paused=False,owner_has_input=row["owner_quiet_s"] is not None,
            owner_input_age_s=row["owner_quiet_s"],agents_may_drive=True,pane_can_drive=True,pane_can_see=True,
            private=guard.snapshot.profile=="private",tab_drawn=True,direct_mode=False,url_refused=False,
            other_driver=False,judged_this_turn=judged,actions_this_hour=hourly,strict_profile=False,
            at_desk=True,depth=0,sub_agent=False)

    def _audit(self,action,prepared,turn,decision,category,source):
        from jarvis.v2.permissions import ApprovalRecord,log_decision
        category=category if category in (*rules.CATEGORIES,rules.UPLOAD,rules.NO_TARGET,review.UNJUDGED) else "ordinary"
        source=source if source in ("rules","model","cache","guard","failure","owner") else "failure"
        blob=json.dumps([action.action,action.target,action.text,action.file,prepared.fingerprint],ensure_ascii=False)
        digest=hashlib.sha256(blob.encode("utf-8","surrogatepass")).hexdigest()
        log_decision(ApprovalRecord("hud_browser_act",digest,"browser-gate",decision,
            reason=category+":"+source,thread_id=turn.thread_id,provider="fast"))

    def _card(self,action,prepared,guard,action_id,category):
        hidden=prepared.context_flagged
        parts=urlsplit(prepared.url)
        target="Page details withheld" if hidden else untrusted.one_line(prepared.target.name if prepared.target else "",120)
        rect=None
        if not hidden and prepared.rect:
            left,top,right,bottom=prepared.rect;rect={"x":left,"y":top,"width":right-left,"height":bottom-top}
        browser={"tab_id":guard.snapshot.tab_id,"action_id":action_id,"pane":guard.snapshot.slot,
            "page_identity":guard.snapshot.page_identity,"document_generation":guard.snapshot.document_generation,
            "rect":rect,"action":action.action,"category":category,
            "host":"" if hidden else untrusted.one_line(parts.hostname or "",200),
            "path":"" if hidden else untrusted.one_line(parts.path or "/",400),"target":target,
            "text":action.text if action.action in ("type","select") else ""}
        args={"action":action.action,"pane":str(guard.snapshot.slot),"target":action.target,"text":action.text,"file":action.file}
        return ApprovalRequest("hud_browser_act",args=args,command=json.dumps(args,ensure_ascii=False),
            reason="Approve this one Browser action? Page labels are untrusted page text.",
            layer="browser-gate",thread_id=guard.turn.thread_id,provider="fast",allowlistable=False,
            headline="Browser action: "+action.action,hud_only=True,browser=browser,max_wait_s=120)

    def _cancelled(self,guard,request):
        try:guard(own_card=request);return False
        except Exception:return True

    def act(self,action:Action)->ActResult:
        snapshot=None;lease=None;outcome="refused";action_id=random.token_hex(16)
        try:
            turn=self._turn();turn.attempted=True
            turn.pane=action.pane if action.pane in tuple(str(i) for i in range(1,9)) else ""
            action.validate()
            snapshot=self.daemon.browser_panes.resolve(action.pane)
            snapshot=self._binding(snapshot)
            turn.pane=str(snapshot.slot)
            row=self._owner(snapshot)
            capability=self.state.capability(turn,snapshot,action.target) if action.target else None
            lease=self.state.reserve(turn,snapshot.tab_id)
            stop=runtime.should_stop()
            guard=Guard(self,turn,snapshot,lease,capability,row["input_serial"],row["attachment_generation"],
                        row["hold_count"],stop,lambda:runtime.at_desk() and runtime.depth()==0)
            if action.action=="navigate":self._url(action.text)
            forms=terminal_guard.value_forms(secrets.secret_values())
            prepared=self.daemon.browser.prepare_action(snapshot,action,capability,action_id,forms,guard)
            _,row=guard()
            facts=self._facts(action,prepared,guard,row)
            pre=rules.pre_judge(facts)
            if pre.kind==rules.REFUSE:raise LookRefused(pre.reason)
            upload=None
            if action.action=="upload":
                from .drive_files import judge_upload
                upload=judge_upload(action.file,turn.project_root)
            verdict=None
            if pre.kind in (rules.ASK,rules.CLASSIFY):
                self.state.charge_judgement(lease)
                verdict=review.judge(facts,turn.owner_words)
                guard()
            decision=review.decide(pre,verdict)
            category=pre.category or (verdict.category if verdict else rules.ORDINARY)
            self._audit(action,prepared,turn,decision,category,verdict.source if verdict else "rules")
            if decision in ("deny","refuse"):raise LookRefused("the Browser action was refused by its safety review")
            if decision=="ask":
                if getattr(self.daemon.approvals,"human_backed",False) is not True:
                    raise LookRefused("this Browser action needs the owner at the HUD")
                request=self._card(action,prepared,guard,action_id,category)
                self.daemon.browser.drive_notice(snapshot.tab_id,turn.thread_id,action_id,action.action,"asking",prepared,action.text)
                answer=self.daemon.approvals.ask(request,cancelled=lambda:self._cancelled(guard,request))
                self._audit(action,prepared,turn,"allow" if answer is Decision.ALLOW else "deny",category,"owner")
                # A resolved card no longer appears in pending(), but its one
                # hold remains. Any second card or owner input invalidates yes.
                _,row=guard(own_card=request)
                guard.hold_count=row["hold_count"]
                if answer is not Decision.ALLOW:
                    outcome="asked"
                    return ActResult(outcome,"The Browser action was not approved.")
            guard()
            refreshed=self.daemon.browser.prepare_action(snapshot,action,capability,action_id,forms,guard,
                                                          prepared=prepared)
            if refreshed.fingerprint!=prepared.fingerprint:raise LookRefused(CHANGED_ACTION)
            def execute(upload_bytes=None):
                guard()
                self.state.charge_action(lease)
                self.daemon.browser.drive_notice(snapshot.tab_id,turn.thread_id,action_id,action.action,"running",refreshed,action.text)
                token=self.downloads.begin(snapshot.tab_id,action_id,guard,refreshed)
                guard.download=token
                try:
                    result=self.daemon.browser.execute_action(snapshot,action,refreshed,action_id,forms,guard,upload_bytes=upload_bytes)
                    self.daemon.browser.wait_action_downloads(token)
                    notices=self.downloads.review(token)
                    if notices:result=replace(result,text=result.text+"\n"+"\n".join(notices))
                    if result.outcome=="acted" and result.ref_snapshot is not None:
                        fresh,_=guard(navigation=True)
                        if fresh!=result.ref_snapshot:raise LookRefused(CHANGED_ACTION)
                        scan=result.ref_scan
                        self.state.publish(turn,fresh,scan["serial"],[row["ref"] for row in scan["refs"]])
                    return result
                finally:self.downloads.end(token)
            if upload is not None:
                from .drive_files import load_upload
                result=execute(load_upload(upload,action.file,turn.project_root,guard))
            else:result=execute()
            outcome=result.outcome
            return result
        except (LookRefused,DriveStateRefused,PaneRefused) as exc:
            return ActResult("refused","Refused: "+str(exc))
        except Exception:
            return ActResult("refused","Refused: the Browser action could not be checked safely")
        finally:
            if lease is not None:self.state.finish(lease)
            if snapshot is not None:
                try:
                    self.daemon.bus.publish({"kind":"browser_acted","data":{
                        "tab_id":snapshot.view_id,"action_id":action_id,"at":utcnow(),"outcome":outcome}})
                    self.daemon.browser.drive_finished(snapshot.tab_id,action_id,outcome)
                except Exception:pass

    def install(self):
        global _installed
        from .drive_files import sweep_staging
        try:clean=sweep_staging()
        except Exception:clean=False
        if not clean:self.closed=True
        with _installed_lock:_installed=self

    def stop(self):
        global _installed
        self.closed=True;self.state.stop();self.downloads.stop()
        with _installed_lock:
            if _installed is self:_installed=None


_installed_lock=threading.Lock()
_installed=None


def act_for_tool(action,pane="",target="",text="",file=""):
    with _installed_lock:service=_installed
    if service is None:return ActResult("refused",OFF)
    return service.act(Action(action,pane,target,text,file))


def retire_turn(owner):
    with _installed_lock:service=_installed
    if service is not None:service.state.retire(owner)
