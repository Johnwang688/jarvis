"""Exact-file, immutable byte handoff for Browser uploads; no disk staging."""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass,field
import os
import hashlib
import stat
from pathlib import Path

from jarvis import config
from . import files
from .act_rules import MAX_UPLOAD_BYTES,upload_refusal
from .look import LookRefused


def _identity(info):
    return (info.st_dev,info.st_ino,info.st_mode,info.st_nlink,info.st_size,info.st_mtime_ns,info.st_ctime_ns)


@dataclass(frozen=True)
class Upload:
    path: str = field(repr=False)
    info: os.stat_result = field(repr=False)
    digest: bytes = field(repr=False)


def judge_upload(path,project_root):
    judged=[]
    why,resolved=upload_refusal(path,project_root,judged=judged)
    if why or len(judged)!=1:raise LookRefused(why or "the upload could not be checked safely")
    try:data=files._read_judged(resolved,MAX_UPLOAD_BYTES,judged[0],stable=True)
    except files.FileRefused as exc:raise LookRefused(str(exc)) from None
    return Upload(resolved,judged[0],hashlib.sha256(data).digest())


@dataclass(frozen=True)
class UploadBytes:
    name: str = field(repr=False)
    data: bytes = field(repr=False)


def load_upload(approved:Upload,path,project_root,current):
    """After yes, bind exactly the stable bytes checked against the approval.

    No path is handed to Chrome and no private disk copy is created. The File
    handed to the page is byte-backed, so later submission cannot lazy-read a
    changed or deleted source. The caller holds this buffer only through input.
    """
    current()
    judged=[]
    why,resolved=upload_refusal(path,project_root,judged=judged)
    if why or len(judged)!=1:raise LookRefused(why or "the upload could not be checked safely")
    if resolved!=approved.path or _identity(judged[0])!=_identity(approved.info):
        raise LookRefused("the approved upload changed; choose the file again")
    try:data=files._read_judged(resolved,MAX_UPLOAD_BYTES,approved.info,stable=True)
    except files.FileRefused as exc:raise LookRefused(str(exc)) from None
    if hashlib.sha256(data).digest()!=approved.digest:
        raise LookRefused("the approved upload contents changed; choose the file again")
    current()
    return UploadBytes(files.clean_name(Path(resolved).name),data)


@contextmanager
def _staging_root():
    # Walk every ancestor with O_NOFOLLOW, pin it, and revalidate its name
    # before deletion. Checking only the final directory leaves ancestor swaps
    # able to redirect traversal. No chmod/create side effects during recovery.
    path=Path(config.BROWSER_DIR).absolute()/"drive-staging"
    fds=[];links=[]
    try:
        parent=os.open(path.anchor,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW);fds.append(parent)
        for name in path.parts[1:]:
            before=os.stat(name,dir_fd=parent,follow_symlinks=False)
            try:child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=parent)
            except FileNotFoundError:raise OSError("staging ancestor changed") from None
            fds.append(child)
            info=os.fstat(child)
            if (before.st_dev,before.st_ino)!=(info.st_dev,info.st_ino):raise OSError("staging ancestor changed")
            links.append((parent,name,child,info));parent=child
        for _parent,_name,_fd,info in links[-2:]:
            if info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o700:raise OSError("staging directory is not private")
        def current():
            for parent,name,child,info in links:
                try:now=os.stat(name,dir_fd=parent,follow_symlinks=False)
                except FileNotFoundError:raise OSError("staging ancestor changed") from None
                if not stat.S_ISDIR(now.st_mode) or (now.st_dev,now.st_ino)!=(info.st_dev,info.st_ino):
                    raise OSError("staging ancestor changed")
        current();yield parent,current
    finally:
        for fd in reversed(fds):os.close(fd)


def sweep_staging():
    """Remove only old bounded private one-file remnants; new uploads use RAM."""
    try:
        with _staging_root() as (rootfd,current):
            return _sweep_staging(rootfd,current)
    except FileNotFoundError:return True
    except OSError:return False


def _sweep_staging(rootfd,current):
    complete=True
    with os.scandir(rootfd) as entries:
        names=[]
        for entry in entries:
            if len(names)>=512:return False
            names.append(entry.name)
    for name in names:
        if len(name)!=32 or any(c not in "0123456789abcdef" for c in name):complete=False;continue
        child=None
        try:
            current()
            child=os.open(name,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=rootfd)
            folder=os.fstat(child)
            if folder.st_uid!=os.getuid() or stat.S_IMODE(folder.st_mode)!=0o700:
                complete=False;continue
            with os.scandir(child) as entries:
                items=[]
                for entry in entries:
                    items.append(entry.name)
                    if len(items)>1:break
            if len(items)>1:complete=False;continue
            current()
            named=os.stat(name,dir_fd=rootfd,follow_symlinks=False)
            if (named.st_dev,named.st_ino)!=(folder.st_dev,folder.st_ino):complete=False;continue
            if items:
                item=items[0];info=os.stat(item,dir_fd=child,follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.getuid()
                        or stat.S_IMODE(info.st_mode)!=0o600):complete=False;continue
                os.unlink(item,dir_fd=child)
            current()
            named=os.stat(name,dir_fd=rootfd,follow_symlinks=False)
            if (named.st_dev,named.st_ino)!=(folder.st_dev,folder.st_ino):complete=False;continue
            os.rmdir(name,dir_fd=rootfd)
        except OSError:complete=False
        finally:
            if child is not None:os.close(child)
    return complete
