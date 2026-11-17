"""Local operational logging; no audio or conversation content belongs here."""
from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import threading

LOG_NAME = "voice-agent.log"
_lock = threading.Lock()
_hooks_installed = False


class _PrivateErrors(logging.Filter):
    def filter(self, record):
        # Action/provider errors can echo text or clipboard content. Keep the
        # stack and class, but remove their message before ANY handler sees it.
        if getattr(record, "voice_private", False) and record.exc_info:
            kind, exc, tb = record.exc_info
            safe_kind = type(kind.__name__, (Exception,), {})
            record.exc_info = (safe_kind, safe_kind("details withheld for voice privacy"), tb)
            record.exc_text = None
        return True


def setup(log_dir: Path) -> logging.Logger:
    global _hooks_installed
    with _lock:
        log_dir = Path(log_dir)
        log_dir.mkdir(parents=True, exist_ok=True)
        logger = logging.getLogger("jarvis.voice")
        logger.setLevel(logging.INFO)
        target = str((log_dir / LOG_NAME).resolve())
        rotating = [h for h in logger.handlers if isinstance(h, RotatingFileHandler)]
        kept = False
        for handler in rotating:
            if handler.baseFilename != target or kept:
                logger.removeHandler(handler)
                handler.close()
            else:
                kept = True
        if not kept:
            handler = RotatingFileHandler(log_dir / LOG_NAME, maxBytes=1_000_000,
                                          backupCount=3, encoding="utf-8")
            handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(threadName)s %(message)s"))
            logger.addHandler(handler)
        if not any(isinstance(f, _PrivateErrors) for f in logger.filters):
            logger.addFilter(_PrivateErrors())
        if not _hooks_installed:
            previous_sys, previous_thread = sys.excepthook, threading.excepthook

            def uncaught(kind, value, tb):
                logger.error("Uncaught voice error", exc_info=(kind, value, tb))
                previous_sys(kind, value, tb)

            def thread_uncaught(args):
                logger.error("Uncaught thread error", exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
                             extra={"voice_private": args.thread.name == "voice-network"})
                previous_thread(args)

            sys.excepthook, threading.excepthook = uncaught, thread_uncaught
            _hooks_installed = True
        return logger
