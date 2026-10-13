"""Exact-file upload preparation and private staging for Browser actions."""
from __future__ import annotations
from contextlib import contextmanager
from dataclasses import dataclass,field
import os
import hashlib
import secrets
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


@contextmanager
def stage_upload(approved:Upload,path,project_root,current):
    """Recheck exact identity after yes, copy once, delete only our own copy."""
    from .host import secure_dir
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
    root=secure_dir(secure_dir(config.BROWSER_DIR)/"drive-staging")
    folder=root/secrets.token_hex(16)
    os.mkdir(folder,0o700)
    folder_info=os.lstat(folder);copy=folder/files.clean_name(Path(resolved).name);copy_info=None
    try:
        fd=os.open(copy,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
        try:
            copy_info=os.fstat(fd)
            view=memoryview(data)
            while view:
                current()
                written=os.write(fd,view[:1024*1024])
                if written<=0:raise LookRefused("the private upload copy could not be written")
                view=view[written:]
            os.fsync(fd);copy_info=os.fstat(fd)
        finally:os.close(fd)
        current()
        if copy_info.st_nlink!=1 or copy_info.st_size!=len(data):raise LookRefused("the private upload copy changed")
        yield str(copy)
    finally:
        # No recursive delete, and never follow a replaced staging path.
        try:
            now=os.lstat(folder)
            if (now.st_dev,now.st_ino)!=(folder_info.st_dev,folder_info.st_ino) or not stat.S_ISDIR(now.st_mode):
                raise LookRefused("private upload cleanup is unfinished")
            if os.path.lexists(copy):
                info=os.lstat(copy)
                if (copy_info is not None and (info.st_dev,info.st_ino)!=(copy_info.st_dev,copy_info.st_ino)) or not stat.S_ISREG(info.st_mode):
                    raise LookRefused("private upload cleanup is unfinished")
                os.unlink(copy)
            os.rmdir(folder)
        except OSError:raise LookRefused("private upload cleanup is unfinished") from None


def sweep_staging():
    """After daemon ownership, remove only bounded, private one-file remnants.

    Unknown entries are retained and report unfinished cleanup. Directory FDs
    keep a renamed/replaced parent from redirecting deletion to another tree.
    """
    from .host import secure_dir
    root=Path(config.BROWSER_DIR)/"drive-staging"
    if not os.path.lexists(root):return True
    secure_dir(config.BROWSER_DIR,create=False);secure_dir(root,create=False)
    rootfd=os.open(root,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
    complete=True
    try:
        with os.scandir(rootfd) as entries:
            names=[]
            for entry in entries:
                if len(names)>=512:return False
                names.append(entry.name)
        for name in names:
            if len(name)!=32 or any(c not in "0123456789abcdef" for c in name):complete=False;continue
            child=None
            try:
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
                if items:
                    item=items[0];info=os.stat(item,dir_fd=child,follow_symlinks=False)
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink!=1 or info.st_uid!=os.getuid()
                            or stat.S_IMODE(info.st_mode)!=0o600):complete=False;continue
                    os.unlink(item,dir_fd=child)
                named=os.stat(name,dir_fd=rootfd,follow_symlinks=False)
                if (named.st_dev,named.st_ino)!=(folder.st_dev,folder.st_ino):complete=False;continue
                os.rmdir(name,dir_fd=rootfd)
            except OSError:complete=False
            finally:
                if child is not None:os.close(child)
    finally:os.close(rootfd)
    return complete
