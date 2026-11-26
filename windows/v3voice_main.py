"""Run the paired Windows voice helper or its device-only self-test."""

import argparse
from collections import deque
from contextlib import contextmanager
from dataclasses import replace
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from queue import Empty, SimpleQueue
import sys
import threading
import time
import warnings

if __package__:
    from . import v3voice_audio as audio, v3voice_client as client, v3voice_key as key
else:
    import v3voice_audio as audio
    import v3voice_client as client
    import v3voice_key as key


BIND_ADDRESS = "127.0.0.1"


@contextmanager
def single_instance(port):
    import ctypes as c
    from ctypes import wintypes as w

    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = (w.LPVOID, w.BOOL, w.LPCWSTR)
    kernel.CreateMutexW.restype = w.HANDLE
    kernel.CloseHandle.argtypes = (w.HANDLE,)
    # Share the port mutex with the previous helper as they use the same port.
    c.set_last_error(0)
    handle = kernel.CreateMutexW(None, False, f"Local\\JarvisVoiceAnywhere-{port}")
    exists = c.get_last_error() == 183
    if not handle:
        raise OSError("Could not acquire the voice helper mutex.")
    try:
        yield not exists
    finally:
        kernel.CloseHandle(handle)


@contextmanager
def log_file(folder):
    handler = RotatingFileHandler(folder / "jarvis-voice.log", maxBytes=128 * 1024,
                                  backupCount=2, encoding="utf-8")
    logger = logging.Logger("Windows voice helper")
    logger.propagate = False
    logger.addHandler(handler)
    try:
        yield logger
    finally:
        handler.close()


class Helper:
    def __init__(self, host, port, key_path, *, logger, devices=None,
                 make_canceller=audio.Canceller.available, make_client=client.Client,
                 make_key=key.TalkKey, make_popup=None):
        if __package__:
            from . import v3voice_render as render
        else:
            import v3voice_render as render
        if make_popup is None:
            if __package__:
                from .v3voice_popup import Popup
            else:
                from v3voice_popup import Popup
            make_popup = Popup

        self._logger, self._render = logger, render
        self._lock = threading.RLock()
        self._click_lock = threading.Lock()
        self._notices, self._clicks = SimpleQueue(), deque()
        self._display = render.PopupState()
        self._target = None
        self._sentences = {}
        self._interrupted = set()
        self._connected = self._key_down = False
        self._pump = self._pump_stop = None
        self._rejected = False
        devices = audio.SoundcardDevices() if devices is None else devices
        self._capture = audio.Capture(devices, make_canceller(), on_error=self._device_error)
        self._playback = audio.Playback(devices, played=self._played, on_error=self._device_error)
        self._client = make_client(
            host, port, key_path, canceller=self._capture.canceller,
            on_connect=self._connect, on_disconnect=self._disconnect, on_event=self._event,
            on_audio=self._audio, on_state=self._state)
        self._popup = make_popup(on_click=self._click)
        self._key = make_key(self._on_key, on_error=self._device_error)

    def run(self):
        self._logger.info("Voice helper started.")
        try:
            self._key.start()
            self._client.run()
        finally:
            self._client.close()
            self._disconnect()
            self._key.close()
            self._popup.close()
            self._logger.info("Voice helper stopped.")
        return 1 if self._rejected else 0

    def _publish(self, state, *, approval=None, expires=None, click_expires=None):
        # Keep already queued clicks when the playback receipt refreshes the same card.
        if approval is not self._target:
            self._set_card(None)
        if click_expires is None:
            self._popup.set(state, expires_in=expires)
        else:
            self._popup.set(state, expires_in=expires, click_expires_in=click_expires)
        self._display = state
        self._set_card(approval)

    def _set_card(self, card):
        with self._click_lock:
            if self._target is not card:
                self._target = card
                self._clicks.clear()
                self._client.set_card(card)

    def _connect(self):
        self._pump_stop = threading.Event()
        self._pump = threading.Thread(target=self._capture_loop, args=(self._pump_stop,), daemon=True)
        with self._lock:
            self._notices = SimpleQueue()
            self._connected = True
        self._logger.info("Voice link connected.")
        self._playback.start()
        self._capture.start()
        self._pump.start()

    def _disconnect(self, dropped=0):
        with self._lock:
            if not self._connected:
                return
            self._connected = self._key_down = False
            self._sentences.clear()
            self._interrupted.clear()
            self._publish(self._render.PopupState())
        self._pump_stop.set()
        self._capture.stop()
        if self._pump.ident is not None:
            self._pump.join()
        self._playback.close()
        drops, repeats = self._capture.corrections
        self._logger.info("Voice link disconnected. Dropped frames: %d. Reference drops: %d. Repeats: %d.",
                          dropped + self._capture.dropped_frames, drops, repeats)

    def _capture_loop(self, stop):
        capture_active = True
        while not stop.is_set():
            while True:
                try:
                    reason = self._notices.get_nowait()
                except Empty:
                    break
                with self._lock:
                    if self._connected:
                        event = {"kind": "error", "reason": reason}
                        self._publish(self._render.state_from_event(self._display, event))
                if reason != "Talk key stopped after a hook error." and capture_active:
                    self._capture.stop()
                    capture_active = False
            self._send_clicks()
            if not capture_active:
                stop.wait(0.05)
                continue
            generation = self._capture.generation
            frame = self._capture.get(0.05)
            if frame is None:
                continue
            if not self._capture.forward(generation, frame, self._client.send_audio,
                                         lambda level: self._client.send(client.wire.LEVEL, level)):
                continue
            level = frame[2]
            if level is not None:
                with self._lock:
                    if self._connected and self._display.kind == "listening":
                        self._publish(replace(self._display, level=level / 255))

    def _device_error(self, reason):
        # All producers supply fixed local messages. The log uses a fixed
        # sentence independently of the message displayed in the pop-up.
        self._logger.info("A voice device or hook stopped.")
        with self._lock:
            if self._connected:
                self._notices.put(reason)

    def _state(self, state):
        if state == client.WRONG_PEER:
            self._rejected = True
            self._logger.warning("That wasn't Jarvis")
            print(client.WRONG_PEER)
        else:
            self._logger.info("Voice link rejected a frame.")

    def _event(self, event):
        kind = event["kind"]
        expires = click_expires = None
        if kind == "asking":
            received_at = time.monotonic()
            expires = None if event.get("voice_waiting") is True else event.get("expires_in")
            click_expires = event.get("click_expires_in")
        with self._lock:
            if kind == "state" and event.get("state") not in self._render.APPROVALS:
                self._set_card(None)
            if kind == "quit":
                self._set_card(None)
                self._client.close()
            elif kind == "earcon":
                self._playback.earcon(event["name"])
            elif kind == "notice":
                notice = self._render.state_from_event(self._display, event)
                if notice is not self._display:
                    self._popup.notice(notice)
            elif kind == "flush":
                self._sentences.clear()
                self._interrupted.clear()
                number = event["n"]
                self._playback.flush(on_silent=lambda: self._client.send(client.wire.FLUSHED, number))
            else:
                if kind == "say":
                    self._sentences[event["n"]] = event["id"]
                state = self._render.state_from_event(self._display, event)
                if state is not self._display:
                    approval = None
                    if kind == "asking" and state.buttons:
                        if (click_expires is not None and event.get("voice_waiting") is False
                                and self._target is not None and self._target.accepts(state.id, state.digest)):
                            approval = self._target
                        else:
                            lifetime = event.get("expires_in") if click_expires is None else click_expires
                            approval = client.Card(state.id, state.digest, received_at + lifetime)
                    self._publish(state, approval=approval, expires=expires, click_expires=click_expires)
                elif kind == "asking":
                    self._set_card(None)

    def _audio(self, index, flags, rate, data):
        with self._lock:
            if index in self._interrupted:
                return
            accepted = self._playback.put(index, flags, rate, data)
            if not accepted and flags & client.wire.LAST:
                self._sentences.pop(index, None)

    def _on_key(self, down):
        with self._lock:
            if self._connected and self._key_down != down:
                self._key_down = down
                self._client.send(client.wire.KEY, down)
                if down and (self._sentences or self._playback.playing):
                    self._interrupted.update(self._sentences)
                    self._sentences.clear()
                    self._playback.flush()

    def _played(self, index):
        with self._lock:
            if index in self._sentences:
                self._client.send(client.wire.PLAYED, {"id": self._sentences.pop(index)})

    def _click(self, decision, ident=None, digest=None):
        # The checked-in popup sends these same three fields as a dictionary.
        if isinstance(decision, dict):
            ident, digest, decision = decision.get("id"), decision.get("digest"), decision.get("decision")
        with self._click_lock:
            if (self._connected and decision in ("approve", "deny")
                    and self._target is not None and self._target.accepts(ident, digest)):
                self._clicks.append({"id": ident, "digest": digest, "decision": decision})

    def _send_clicks(self):
        with self._click_lock:
            while self._clicks:
                click = self._clicks.popleft()
                if (self._connected and self._target is not None
                        and self._target.accepts(click["id"], click["digest"])):
                    self._client.send(client.wire.CLICK, click)


def selftest(devices=None, make_canceller=audio.Canceller.available):
    import numpy as np

    devices = audio.SoundcardDevices() if devices is None else devices
    canceller = make_canceller()
    pairs = []
    capture = audio.Capture(devices, canceller, on_pair=lambda *frames: pairs.append(frames))
    playback = audio.Playback(devices, played=lambda _: None, flushed=lambda: None)
    try:
        playback.start()
        capture.start()
        time.sleep(0.5)
        playback.put(0, client.wire.FIRST | client.wire.LAST, audio.DEVICE_RATE,
                     audio.pcm16(audio.tone(1, 523, 719, 0.08)).tobytes())
        time.sleep(2.5)
    finally:
        capture.stop()
        playback.close()
    opened = int(all(capture.opened) and playback.opened and capture.reason is None)
    delay_ms = before = after = 0.0
    if pairs:
        near, far, clean = zip(*pairs)
        reference = np.concatenate(far)
        mic = np.concatenate(near)
        delay_ms = audio.Canceller.estimate(mic, reference) or 0
        # Use the time where the reference tone is present, shifted by the
        # measured delay, and measure only the two generated frequencies.
        audible = np.flatnonzero(np.abs(reference) > 128)
        if len(audible):
            start, end = np.minimum(audible[[0, -1]] + round(delay_ms * 16), len(mic))

            def tone_db(samples):
                signal = samples[start:end] / 32768
                if not len(signal):
                    return -120.0
                t = np.arange(len(signal)) / audio.VOICE_RATE
                rms = sum(abs(np.mean(signal * np.exp(2j * np.pi * hz * t)))**2
                          for hz in (523, 719))**0.5 * 2**0.5
                return 20 * np.log10(max(1e-6, rms))

            before = tone_db(mic)
            after = tone_db(np.concatenate(clean)) if canceller is not None else before
    drops, repeats = capture.corrections
    print(f"{opened} {capture.received[0]} {capture.received[1]} "
          f"{delay_ms:.2f} {before:.2f} {after:.2f} {drops} {repeats}")
    return 0 if opened else 1


def main(argv=None, *, mutex=single_instance, make_helper=Helper):
    parser = argparse.ArgumentParser(prog="v3voice_main.py")
    parser.add_argument("--selftest", action="store_true")
    parser.add_argument("host", nargs="?")
    parser.add_argument("port", nargs="?", type=int)
    parser.add_argument("key_file", nargs="?", type=Path)
    args = parser.parse_args(argv)
    warnings.filterwarnings("ignore", module=r"(soundcard|livekit)(\.|$)")
    if args.selftest:
        try:
            return selftest()
        except Exception:
            print("0 0 0 0 0 0 0 0")
            return 1
    if args.host != BIND_ADDRESS or args.port is None or not 0 < args.port <= 65535 or args.key_file is None:
        parser.error("Provide the loopback address, a port and a pairing file.")
    try:
        with mutex(args.port) as acquired:
            if not acquired:
                return 0
            with log_file(args.key_file.parent) as logger:
                return make_helper(args.host, args.port, args.key_file, logger=logger).run()
    except KeyboardInterrupt:
        return 0
    except Exception:
        # Never marshal exceptions from a library, path or frame to a console.
        print("The voice helper could not run.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
