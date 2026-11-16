r"""Owner-started Windows Right Ctrl recorder. VA-1 displays text only.

Audio is bounded PCM in memory; no transcript printing, files or app typing.
Usage: venv\Scripts\python.exe voice_agent.py 127.0.0.1 8409 <key path>
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import queue
import sys
import threading
import time
import tkinter as tk

import sounddevice as sd
from voicewire import Client, HotkeyMachine, KeyFileError, LinkError, NotJarvis, load_key, wav_bytes

VK_RCONTROL = 0xA3
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
RATE = 16000
MAX_FRAMES = 120 * RATE


class Agent:
    def __init__(self, host, port, key_path):
        self.host, self.port, self.key_path = host, port, key_path
        self.done = threading.Event()
        self.events = queue.Queue()
        # Bound the backlog of clips while a recognizer is slow/offline.
        self.clips = queue.Queue(maxsize=4)
        self.generation = 0
        self.capture_lock = threading.Lock()
        self.capture = None
        self.chunks = []
        self.frames = 0
        self.audio_bad = False
        self.user32 = ctypes.windll.user32
        self.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user32.GetAsyncKeyState.restype = ctypes.c_short
        self.user32.GetParent.argtypes = [wintypes.HWND]
        self.user32.GetParent.restype = wintypes.HWND
        self.user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.GetWindowLongW.restype = ctypes.c_long
        self.user32.SetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_long]
        self.user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        self.user32.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                           ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.label = tk.Label(self.root, text="", font=("Segoe UI", 13), wraplength=660,
                              bg="#17232e", fg="#f0f7fc", padx=22, pady=14, takefocus=False)
        self.label.pack()
        self.root.update_idletasks()
        self.hwnd = self.user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
        style = self.user32.GetWindowLongW(self.hwnd, -20)
        self.user32.SetWindowLongW(self.hwnd, -20, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        self.display_generation = -1
        self.hide_at = None

    def show(self, generation, text, transient=False):
        self.events.put((generation, text, transient))

    def ui_tick(self):
        # All Tk operations, including after(), run on the Tk thread.
        try:
            while True:
                generation, text, transient = self.events.get_nowait()
                if generation < self.display_generation:
                    continue
                self.display_generation = generation
                self.hide_at = time.monotonic() + 5 if transient else None
                if text is None:
                    self.user32.ShowWindow(self.hwnd, 0)
                    continue
                self.label.configure(text=text)
                self.root.update_idletasks()
                width, height = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
                x = (self.root.winfo_screenwidth() - width) // 2
                y = self.root.winfo_screenheight() - height - 80
                # SWP_NOACTIVATE | SWP_SHOWWINDOW; never deiconify/focus a window.
                self.user32.SetWindowPos(self.hwnd, wintypes.HWND(-1), x, y, width, height, 0x50)
                self.user32.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
        except queue.Empty:
            pass
        if self.hide_at and time.monotonic() >= self.hide_at:
            self.user32.ShowWindow(self.hwnd, 0)
            self.hide_at = None
        self.root.after(30, self.ui_tick)

    def audio(self, indata, frames, timing, status):
        with self.capture_lock:
            if status:
                self.audio_bad = True
            count = min(frames, MAX_FRAMES - self.frames)
            if count > 0:
                self.chunks.append(indata[:count].tobytes())
                self.frames += count

    def start_capture(self):
        with self.capture_lock:
            self.chunks, self.frames, self.audio_bad = [], 0, False
        wasapi = next((api for api in sd.query_hostapis() if "WASAPI" in api["name"]), None)
        if wasapi is None or wasapi["default_input_device"] < 0:
            raise RuntimeError("Microphone unavailable")
        # Shared WASAPI: keep other apps' audio working and permit the system
        # converter when the default microphone's mix format is not 16 kHz.
        self.capture = sd.InputStream(
            device=wasapi["default_input_device"], samplerate=RATE, channels=1,
            dtype="int16", extra_settings=sd.WasapiSettings(auto_convert=True),
            callback=self.audio)
        self.capture.start()
        self.show(self.generation, "Listening…")

    def finish_capture(self, cancel=False):
        capture, self.capture = self.capture, None
        if capture:
            try:
                capture.stop()
            finally:
                capture.close()
        with self.capture_lock:
            chunks, self.chunks = self.chunks, []
            bad = self.audio_bad
        if cancel:
            self.show(self.generation, None)
        elif bad:
            self.show(self.generation, "Microphone capture failed", True)
        elif chunks:
            self.show(self.generation, "Transcribing…")
            try:
                self.clips.put_nowait((self.generation, wav_bytes(b"".join(chunks))))
            except queue.Full:
                self.show(self.generation, "Jarvis is busy — try again", True)
        else:
            self.show(self.generation, "No speech heard", True)

    def poll(self):
        machine = HotkeyMachine()
        # VK_CONTROL aliases both Ctrl keys: do not mistake Right Ctrl for a
        # chord. Left Ctrl has its own code and still counts as another key.
        others = [k for k in range(1, 255) if k not in (VK_RCONTROL, 0x11)]
        try:
            while not self.done.is_set():
                held = bool(self.user32.GetAsyncKeyState(VK_RCONTROL) & 0x8000)
                other = any(self.user32.GetAsyncKeyState(k) & 0x8000 for k in others)
                for action in machine.feed(time.monotonic_ns() // 1_000_000, held, other):
                    try:
                        if action == "start":
                            self.generation += 1
                            self.start_capture()
                        else:
                            self.finish_capture(cancel=action == "cancel")
                    except Exception:
                        machine.state = "wait" if held else "idle"
                        try:
                            self.finish_capture(cancel=True)
                        except Exception:
                            pass
                        self.show(self.generation, "Microphone unavailable", True)
                self.done.wait(0.015)
        finally:
            self.finish_capture(cancel=True)

    def network(self):
        client = None
        delay = 1.0
        last_ping = 0.0
        while not self.done.is_set():
            if client is None:
                try:
                    # Reread on reconnect so re-pairing needs no transcript or key logs.
                    client = Client(self.host, self.port, load_key(self.key_path), timeout=90.0)
                    client.connect()
                    delay = 1.0
                    last_ping = time.monotonic()
                except (KeyFileError, LinkError) as exc:
                    if client:
                        client.close()
                    client = None
                    message = ("Not paired: run jarvis voice-link setup" if isinstance(exc, KeyFileError)
                               else "That wasn't Jarvis — refused" if isinstance(exc, NotJarvis)
                               else "Jarvis isn't running")
                    # Discard offline clips: never deliver stale audio on reconnect.
                    try:
                        while True:
                            generation, _ = self.clips.get_nowait()
                            self.show(generation, message, True)
                    except queue.Empty:
                        pass
                    self.done.wait(delay)
                    delay = min(30.0, delay * 2)
                    continue
            try:
                generation, clip = self.clips.get(timeout=0.2)
            except queue.Empty:
                if time.monotonic() - last_ping >= 3:
                    try:
                        client.ping()
                        last_ping = time.monotonic()
                    except LinkError:
                        client.close()
                        client = None
                continue
            try:
                self.show(generation, client.dictate(clip)["text"] or "No speech heard", True)
            except LinkError as exc:
                self.show(generation, str(exc), True)
                client.close()
                client = None
            finally:
                clip = None
        if client:
            client.close()

    def run(self):
        threading.Thread(target=self.poll, name="voice-hotkey", daemon=True).start()
        threading.Thread(target=self.network, name="voice-network", daemon=True).start()
        self.root.after(30, self.ui_tick)
        try:
            self.root.mainloop()
        finally:
            self.done.set()


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8409
    key = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(sys.executable).resolve().parents[2] / "voice-link.key"
    Agent(host, port, key).run()


if __name__ == "__main__":
    main()
