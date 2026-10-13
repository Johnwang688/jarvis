"""Bounded action-attributed executable downloads and exact owner-only release.

Only the active action's response URL digests and MIME classes are retained.
Missing/ambiguous evidence stays held. A release is an in-memory grant for one
exact saved byte sequence, never permission to run it or a persistent allowlist.
"""
from __future__ import annotations
from collections import OrderedDict
from dataclasses import dataclass,field
import hashlib
import json
import os
import stat
import threading
from pathlib import Path
from . import files,act_rules
from .drive_files import _identity
from .look import LookRefused
from jarvis.v2.approvals import ApprovalRequest
from jarvis.v2.provider import Decision

RELEASE_CAP=50*1024*1024
MAX_RELEASES=128
MAX_RESPONSES=8
MAX_DOWNLOADS=4

@dataclass
class Initiation:
    tab_id:str
    action_id:str
    guard:object=field(repr=False)
    prepared:object=field(repr=False)
    active:bool=True
    ambiguous:bool=False
    responses:dict=field(default_factory=dict,repr=False)
    downloads:list=field(default_factory=list,repr=False)

@dataclass(frozen=True)
class Saved:
    download_id:str
    path:str=field(repr=False)
    info:object=field(repr=False)
    digest:bytes=field(repr=False)

@dataclass
class Download:
    token:Initiation|None=field(repr=False)
    url_hash:str=field(repr=False)
    row:dict|None=None
    done:object=None


def _url_hash(value):
    if not isinstance(value,str) or not value or len(value)>8192:return None
    return hashlib.sha256(value.encode('utf-8','surrogatepass')).hexdigest()


class Downloads:
    def __init__(self,service):
        self.service=service;self.lock=threading.RLock();self.active={};self.tainted=set();self.releases=OrderedDict();self.closed=False

    def begin(self,tab_id,action_id,guard,prepared):
        token=Initiation(tab_id,action_id,guard,prepared)
        with self.lock:
            if self.closed or tab_id in self.active or len(self.active)>=64 or (tab_id not in self.tainted and len(self.tainted)>=64):raise LookRefused('download attribution is unavailable')
            self.active[tab_id]=token;self.tainted.add(tab_id)
        return token

    def response(self,token,params):
        row=(params or {}).get('response') or {};key=_url_hash(row.get('url'));mime=row.get('mimeType')
        with self.lock:
            if not token.active:return
            if key is None or not isinstance(mime,str) or not mime.strip() or len(mime)>256:
                token.ambiguous=True;return
            if key in token.responses or len(token.responses)>=MAX_RESPONSES:
                token.ambiguous=True;return
            token.responses[key]=mime

    def started(self,tab_id,url):
        with self.lock:
            token=self.active.get(tab_id)
            if token is None:return Download(None,'') if tab_id in self.tainted else None
            if len(token.downloads)>=MAX_DOWNLOADS:token.ambiguous=True;return Download(token,'')
            key=_url_hash(url)
            if key is None:token.ambiguous=True
            item=Download(token,key or '');token.downloads.append(item);return item

    def closed_tab(self,tab_id):
        with self.lock:self.tainted.discard(tab_id)

    def held(self,item,name):
        # All attributed files are held while completion/classification is
        # pending. The owner route cannot race the post-save approval.
        return True

    def completed(self,item,row):
        with self.lock:item.row=dict(row)

    def end(self,token):
        with self.lock:
            token.active=False
            if self.active.get(token.tab_id) is token:self.active.pop(token.tab_id,None)

    def _saved(self,download_id):
        found=files._find(download_id);path=files._entry(found[1]) if found else None
        if path is None:raise LookRefused('the saved download is no longer available')
        info=os.lstat(path)
        if info.st_size>RELEASE_CAP:raise LookRefused('this executable download is too large to verify for release; it stays held')
        try:data=files._read_judged(str(path),RELEASE_CAP,info,stable=True)
        except (OSError,files.FileRefused):raise LookRefused('the saved download changed; it stays held') from None
        return Saved(download_id,str(path),info,hashlib.sha256(data).digest()),data

    def _same(self,saved):
        current,data=self._saved(saved.download_id)
        if current.path!=saved.path or _identity(current.info)!=_identity(saved.info) or current.digest!=saved.digest:
            raise LookRefused('the approved download changed; it stays held')
        return data

    def _delete_exact(self,saved):
        # A denial never deletes a replacement or unrelated owner download.
        self._same(saved)
        path=Path(saved.path);folder=path.parent
        fd=os.open(folder,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        try:
            info=os.stat(path.name,dir_fd=fd,follow_symlinks=False)
            if _identity(info)!=_identity(saved.info):raise LookRefused('download cleanup is unfinished; the file stays held')
            os.unlink(path.name,dir_fd=fd)
            # Only the fixed held marker belongs to this file's folder.
            if os.path.lexists(folder/files.HELD_MARKER):
                marker=os.stat(files.HELD_MARKER,dir_fd=fd,follow_symlinks=False)
                if stat.S_ISREG(marker.st_mode) and marker.st_nlink==1:os.unlink(files.HELD_MARKER,dir_fd=fd)
        finally:os.close(fd)
        try:os.rmdir(folder)
        except OSError:raise LookRefused('download cleanup is unfinished') from None

    def review(self,token):
        """Called with the action's trusted context after all input is finished."""
        guard=token.guard;notices=[]
        for item in tuple(token.downloads):
            if item.row is None:continue
            saved=None
            try:
                saved,data=self._saved(item.row['id'])
                mime=token.responses.get(item.url_hash)
                if token.ambiguous or mime is None:
                    notices.append("Download stays held because its initiating response could not be verified.");continue
                guard(navigation=True)
                executable=files.executable(Path(saved.path).name,data[:8]) or act_rules.executable_download('',mime)
                if executable:
                    if self.service.daemon.approvals.human_backed is not True:
                        notices.append("Executable download stays held until owner approval is available.");continue
                    request=self._card(token,saved)
                    def cancelled():
                        try:guard(own_card=request,navigation=True);return False
                        except Exception:return True
                    answer=self.service.daemon.approvals.ask(request,cancelled=cancelled)
                    from jarvis.v2.permissions import ApprovalRecord,log_decision
                    digest=hashlib.sha256(("release_download:"+saved.download_id+":"+token.action_id).encode()).hexdigest()
                    log_decision(ApprovalRecord("hud_browser_act",digest,"browser-gate",
                        "allow" if answer is Decision.ALLOW else "deny",reason="executable_download:owner",
                        thread_id=guard.turn.thread_id,provider="fast"))
                    if answer is not Decision.ALLOW:
                        self._delete_exact(saved);notices.append("Executable download was declined and deleted.");continue
                    _snapshot,row=guard(own_card=request,navigation=True);guard.hold_count=row['hold_count']
                guard(navigation=True);self._same(saved)
                with self.lock:
                    if self.closed:raise LookRefused('the download release was stopped')
                    self.releases[saved.download_id]=saved
                    while len(self.releases)>MAX_RELEASES:self.releases.popitem(last=False)
                self.service.daemon.browser.download_released(token.tab_id,item.row)
                notices.append("Download is available for the owner to save; it was not run.")
            except Exception:
                if saved is not None:
                    try:self._delete_exact(saved)
                    except Exception:notices.append("Download remains held; cleanup is unfinished.")
                    else:notices.append("Download was stopped and deleted.")
                else:notices.append("Download remains held because it could not be verified.")
                continue
        return notices

    def listed_release(self,download_id):
        with self.lock:saved=None if self.closed else self.releases.get(download_id)
        if saved is None:return False
        try:return _identity(os.lstat(saved.path))==_identity(saved.info)
        except OSError:return False

    def _card(self,token,saved):
        snapshot=token.guard.snapshot
        args={'action':'release_download','download_id':saved.download_id,'name':Path(saved.path).name,'size':saved.info.st_size}
        browser={'tab_id':snapshot.tab_id,'action_id':token.action_id,'pane':snapshot.slot,
                 'page_identity':snapshot.page_identity,'document_generation':snapshot.document_generation,
                 'rect':None,'action':'release_download','category':act_rules.EXECUTABLE_DOWNLOAD,
                 'host':'','path':'','target':'Downloaded file','text':'',
                 'download':{'id':saved.download_id,'name':Path(saved.path).name,'size':saved.info.st_size}}
        return ApprovalRequest('hud_browser_act',args=args,command=json.dumps(args,ensure_ascii=False),
            reason='Release this one executable download to the owner? This does not run it.',
            layer='browser-gate',thread_id=token.guard.turn.thread_id,provider='fast',allowlistable=False,
            headline='Browser action: release download',hud_only=True,browser=browser,max_wait_s=120)

    def authorized(self,download_id,path,info):
        with self.lock:saved=None if self.closed else self.releases.get(download_id)
        if saved is None:return None
        if saved.path!=str(path) or _identity(info)!=_identity(saved.info):
            with self.lock:
                if self.releases.get(download_id) is saved:self.releases.pop(download_id,None)
            return None
        try:return self._same(saved)
        except Exception:
            with self.lock:self.releases.pop(download_id,None)
            return None

    def stop(self):
        with self.lock:
            self.closed=True;self.releases.clear()
            for token in self.active.values():token.active=False
            self.active.clear();self.tainted.clear()
