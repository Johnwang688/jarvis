r"""Owner-started Windows voice agent: Right Ctrl typing and wake questions.

Audio, transcripts and answers stay in memory. Start with:
venv\Scripts\python.exe voice_agent.py 127.0.0.1 8409 <key path>
"""
from __future__ import annotations

import io
from pathlib import Path
import queue
import sys
import threading
import time
import wave

if __package__:
    from .voicewire import Capture, Client, HotkeyMachine, MicrophoneRelay, new_nonce, KeyFileError, LinkError, NotJarvis, load_key, wav_bytes
    from .voicetype import Typer
    from .voiceactions import Actions
    from .voiceflow import Flow
    from . import voiceui, voicewake
else:
    from voicewire import Capture, Client, HotkeyMachine, MicrophoneRelay, new_nonce, KeyFileError, LinkError, NotJarvis, load_key, wav_bytes
    try:
        from voicetype import Typer
        from voiceactions import Actions
        from voiceflow import Flow
        import voiceui
        import voicewake
    except ModuleNotFoundError:
        # Also support importlib loading by the Linux VA-1 verifier.
        from windows.voicetype import Typer
        from windows.voiceactions import Actions
        from windows.voiceflow import Flow
        from windows import voiceui, voicewake

VK_RCONTROL = 0xA3
RATE = 16000
MAX_FRAMES = 120 * RATE


def first_signal_frame(data, width, channels):
    """First frame exceeding -50 dBFS, ignoring leading PCM silence."""
    threshold = (1 << (width * 8 - 1)) * 0.00316
    for pos in range(0, len(data), width):
        value = int.from_bytes(data[pos:pos + width], "little", signed=width != 1)
        if abs(value - (128 if width == 1 else 0)) >= threshold:
            return pos // (width * channels)
    return None


class Player:
    """Blocking playback on the worker; stop is callable from the hotkey thread.

    A stop also suppresses a pending say reply until the next flow begins.
    Raw streams keep playback independent of sounddevice's convenience globals.
    """
    def __init__(self):
        self.playing = threading.Event()
        self.cancelled = threading.Event()
        self.lock = threading.Lock()
        self.stream = None
        self.on_output = None

    def begin(self):
        self.cancelled.clear()

    def play(self, wav):
        if self.cancelled.is_set():
            return
        import sounddevice as sd
        self.playing.set()
        try:
            with wave.open(io.BytesIO(wav), "rb") as audio:
                dtype = {1: "uint8", 2: "int16", 3: "int24", 4: "int32"}.get(audio.getsampwidth())
                if dtype is None or audio.getcomptype() != "NONE":
                    raise RuntimeError("Unsupported speech audio")
                with sd.RawOutputStream(samplerate=audio.getframerate(), channels=audio.getnchannels(),
                                        dtype=dtype) as stream:
                    with self.lock:
                        self.stream = stream
                    try:
                        while not self.cancelled.is_set():
                            data = audio.readframes(max(1, audio.getframerate() // 20))
                            if not data:
                                break
                            start = time.monotonic()
                            stream.write(data)
                            if self.on_output and not self.cancelled.is_set():
                                frame = first_signal_frame(data, audio.getsampwidth(), audio.getnchannels())
                                if frame is not None:
                                    callback, self.on_output = self.on_output, None
                                    callback(start + stream.latency + frame / audio.getframerate())
                    except sd.PortAudioError:
                        if not self.cancelled.is_set():
                            raise
                    finally:
                        with self.lock:
                            self.stream = None
        except (wave.Error, EOFError, sd.PortAudioError):
            if not self.cancelled.is_set():
                raise RuntimeError("Speech playback unavailable") from None
        finally:
            self.playing.clear()

    def stop(self):
        self.cancelled.set()
        with self.lock:
            if self.stream is not None:
                try:
                    self.stream.abort()
                except Exception:
                    pass


class _FlowUI:
    def __init__(self, agent, generation):
        self.agent, self.generation = agent, generation

    def set_state(self, state, text=None):
        if self.generation == self.agent.generation:
            self.agent.overlay.set_state(state, text)


class Agent:
    def __init__(self, host, port, key_path):
        import ctypes
        import tkinter as tk

        self.host, self.port, self.key_path = host, port, key_path
        self.done = threading.Event()
        self.instance = new_nonce()
        self.stop_requested = threading.Event()
        self.mic_released = threading.Event()
        self.relay = None
        self.online = threading.Event()
        self.recording = threading.Event()
        self.flow_running = threading.Event()
        self.clips = queue.Queue(maxsize=4)
        self.generation = 0
        self.generation_lock = threading.Lock()
        self.capture_lock = threading.Lock()
        self.capture_online = False
        self.packet_ready = False
        self.upload = None
        self.client = None
        self.capture = None
        self.tapped = False
        self.chunks = []
        self.frames = 0
        self.audio_bad = False
        self.user32 = ctypes.windll.user32
        self.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user32.GetAsyncKeyState.restype = ctypes.c_short
        self.root = tk.Tk()
        self.root.withdraw()
        self.overlay = voiceui.Overlay(self.root)
        self.player = Player()
        self.wake = None

    def share_frame(self, pcm):
        relay = self.relay
        if relay is not None:
            relay.offer(pcm)

    def next_generation(self):
        with self.generation_lock:
            self.generation += 1
            return self.generation

    def show(self, generation, text, transient=False):
        # Keep the VA-1 headless recorder seam usable without creating Win32/Tk.
        if not hasattr(self, "overlay"):
            self.events.put((generation, text, transient))
        elif generation == self.generation:
            self.overlay.set_state("hidden" if text is None else "error" if transient else "transcribing", text)

    def busy(self):
        return self.recording.is_set() or self.player.playing.is_set() or self.flow_running.is_set()

    def wake_status(self):
        wake = getattr(self, "wake", None)
        if wake is None:
            return "starting"
        return wake.status()

    def enqueue(self, wav, source, generation, *, speech_end=None):
        if hasattr(self, "online") and not self.online.is_set():
            self.show(generation, "Jarvis isn't running", True)
            return
        try:
            self.clips.put_nowait((generation, wav, source, speech_end) if speech_end is not None else (generation, wav, source))
        except queue.Full:
            self.show(generation, "Jarvis is busy — try again", True)

    def on_wake(self, wav):
        if self.done.is_set() or self.busy():
            return
        generation = self.next_generation()
        self.enqueue(wav, "wake", generation, speech_end=getattr(getattr(self, "wake", None), "speech_end", None))

    def wake_state(self, state, text=None):
        if not self.busy():
            self.overlay.set_state(state, text)

    def audio(self, indata, frames, timing, status):
        with self.capture_lock:
            if status:
                self.audio_bad = True
            count = min(frames, MAX_FRAMES - self.frames)
            if count > 0:
                pcm = indata[:count].tobytes()
                upload = getattr(self, "upload", None)
                if upload is None:
                    self.chunks.append(pcm)
                else:
                    upload.append(pcm)
                self.frames += count
            else:
                pcm = b""
        if pcm and hasattr(self, "overlay"):
            import numpy as np
            samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
            level = float(np.sqrt(np.mean(samples * samples)))
            if upload is not None and level > 0.01:
                upload.speech_end = time.monotonic()
            self.overlay.push_level(level)

    def start_capture(self):
        import sounddevice as sd
        self.recording.set()
        with self.capture_lock:
            self.chunks, self.frames, self.audio_bad = [], 0, False
            self.capture_online = not hasattr(self, "online") or self.online.is_set()
            if getattr(self, "packet_ready", False) and self.capture_online:
                self.upload = Capture()
                try:
                    self.clips.put_nowait((self.generation, self.upload, "hotkey"))
                except queue.Full:
                    self.upload = None
        # A second stream on the mic fails to start while the wake listener's
        # runs, so borrow that one when it is live.
        wake = getattr(self, "wake", None)
        if wake is not None and wake.tap(self.audio):
            self.tapped = True
            self.overlay.set_state("listening")
            return
        wasapi = next((api for api in sd.query_hostapis() if "WASAPI" in api["name"]), None)
        if wasapi is None or wasapi["default_input_device"] < 0:
            raise RuntimeError("Microphone unavailable")
        self.capture = sd.InputStream(
            device=wasapi["default_input_device"], samplerate=RATE, channels=1,
            dtype="int16", extra_settings=sd.WasapiSettings(auto_convert=True), callback=self.audio)
        self.capture.start()
        self.overlay.set_state("listening")

    def finish_capture(self, cancel=False):
        release_at = time.monotonic()
        if getattr(self, "tapped", False):
            self.tapped = False
            self.wake.tap(None)
        capture, self.capture = self.capture, None
        try:
            if capture:
                try:
                    capture.stop()
                finally:
                    capture.close()
        finally:
            if hasattr(self, "recording"):
                self.recording.clear()
        with self.capture_lock:
            chunks, self.chunks = self.chunks, []
            bad = self.audio_bad
        wav = wav_bytes(b"".join(chunks)) if chunks and not cancel and not bad else None
        with self.capture_lock:
            # discard_clips invalidates a capture even if the link recovers
            # before release. Keep the check and enqueue atomic with discard.
            online = not hasattr(self, "online") or self.online.is_set()
            valid = (not hasattr(self, "online")
                     or (getattr(self, "capture_online", False) and online))
            self.capture_online = False
            upload = getattr(self, "upload", None)
            if upload is not None:
                self.upload = None
                commit = valid and not cancel and not bad and self.frames > 0
                if commit:
                    self.show(self.generation, "Transcribing…")
                upload.finish(cancel=not commit, release_at=release_at)
                if commit:
                    return
            if cancel:
                self.show(self.generation, None)
            elif not valid:
                self.show(self.generation, "Jarvis isn't running", True)
            elif bad:
                self.show(self.generation, "Microphone capture failed", True)
            elif wav:
                self.show(self.generation, "Transcribing…")
                if hasattr(self, "overlay"):
                    self.enqueue(wav, "hotkey", self.generation)
                else:
                    try:
                        self.clips.put_nowait((self.generation, wav))
                    except queue.Full:
                        self.show(self.generation, "Jarvis is busy — try again", True)
            else:
                self.show(self.generation, "No speech heard", True)

    def poll(self):
        machine = HotkeyMachine()
        others = [k for k in range(1, 255) if k not in (VK_RCONTROL, 0x11)]
        previous = False
        try:
            while not self.done.is_set():
                held = bool(self.user32.GetAsyncKeyState(VK_RCONTROL) & 0x8000)
                if held and not previous:
                    self.player.stop()  # barge-in even on a Ctrl chord
                    client = getattr(self, "client", None)
                    if client is not None:
                        client.interrupt_speech()
                # Only the Right Ctrl press, hold or release can be a chord.
                # While idle scan one key instead of polling the whole keyboard.
                other = (held or previous) and any(self.user32.GetAsyncKeyState(k) & 0x8000 for k in others)
                previous = held
                for action in machine.feed(time.monotonic_ns() // 1_000_000, held, other):
                    try:
                        if action == "start":
                            self.next_generation()
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

    def discard_clips(self, message):
        with self.capture_lock:
            self.capture_online = False
            self.packet_ready = False
            if getattr(self, "upload", None) is not None:
                self.upload.finish(cancel=True)
            try:
                while True:
                    item = self.clips.get_nowait()
                    self.show(item[0], message, True)
            except queue.Empty:
                pass

    def network(self):
        client = None
        delay, last_ping, reported_wake = 1.0, 0.0, None
        # These objects and every Client call belong to this one worker.
        typer = Typer() if hasattr(self, "overlay") else None
        actions = Actions(warm_in_background=True) if typer is not None else None
        try:
            while not self.done.is_set():
                if client is None:
                    if getattr(self, "relay", None) is not None:
                        self.relay.close()
                        self.relay = None
                    try:
                        client = Client(self.host, self.port, load_key(self.key_path), timeout=90.0)
                        client.connect()
                        delay, last_ping, reported_wake = 1.0, 0.0, None
                        if hasattr(self, "online"):
                            # Drop anything captured during the failed/offline interval.
                            self.discard_clips("Jarvis isn't running")
                            self.online.set()
                    except (KeyFileError, LinkError) as exc:
                        if client:
                            client.close()
                        client = None
                        message = ("Not paired: run jarvis voice-link setup" if isinstance(exc, KeyFileError)
                                   else "That wasn't Jarvis — refused" if isinstance(exc, NotJarvis)
                                   else "Jarvis isn't running")
                        self.discard_clips(message)
                        self.done.wait(delay)
                        delay = min(30.0, delay * 2)
                        continue
                wake = self.wake_status() if typer is not None else None
                if wake != reported_wake or time.monotonic() - last_ping >= 3:
                    try:
                        if wake is None:
                            client.ping()
                        else:
                            if hasattr(self, "instance"):
                                client.ping(wake=wake, instance=self.instance)
                                if client.shared_capable and self.relay is None:
                                    self.relay = MicrophoneRelay(client, self.wake_status,
                                                                 self.stop_requested, self.mic_released)
                                    self.relay.start()
                            else:
                                client.ping(wake=wake)
                        self.client = client
                        self.packet_ready = client.streaming is True
                        last_ping, reported_wake = time.monotonic(), wake
                    except LinkError:
                        if hasattr(self, "online"):
                            self.online.clear()
                        client.close()
                        client = None
                        self.discard_clips("Jarvis isn't running")
                        continue
                try:
                    item = self.clips.get(timeout=0.2)
                except queue.Empty:
                    continue
                generation, clip = item[:2]
                source = item[2] if len(item) > 2 else "hotkey"
                try:
                    if typer is None:  # VA-1 headless recorder compatibility
                        self.show(generation, client.dictate(clip)["text"] or "No speech heard", True)
                    else:
                        if source == "wake" and self.recording.is_set():
                            continue
                        self.flow_running.set()
                        transcript = None
                        capture = clip if isinstance(clip, Capture) else None
                        if capture is not None:
                            got = client.dictate_capture(capture)
                            if got is None:
                                continue
                            transcript = got["text"]
                        # Toggle-stop can enqueue while Right Ctrl is still down.
                        # Let the physical modifier up before any synthetic typing.
                        while self.user32.GetAsyncKeyState(VK_RCONTROL) & 0x8000:
                            if self.done.wait(0.015):
                                break
                        if self.done.is_set():
                            break
                        if generation == self.generation:
                            self.player.begin()
                        flow = Flow(client, typer, actions, _FlowUI(self, generation), self.player)
                        speech_end = capture.speech_end if capture else item[3] if len(item) > 3 else None
                        def metric(label, start, end):
                            if start is not None and generation == self.generation:
                                self.overlay.timing(label, max(0, round(1000 * (end - start))))
                        self.player.on_output = lambda end: metric("Voice end to estimated audio", speech_end, end)
                        typer.on_insert = lambda end: metric("Capture stop to text input", capture.release_at if capture else None, end)
                        if transcript is None:
                            flow.on_clip(clip, source)
                        else:
                            flow.on_clip(None, source, transcript=transcript)
                        if flow.link_error is not None:
                            raise flow.link_error
                    last_ping = time.monotonic()
                except LinkError as exc:
                    self.show(generation, str(exc), True)
                    if hasattr(self, "online"):
                        self.online.clear()
                    client.close()
                    client = None
                    self.discard_clips("Jarvis isn't running")
                finally:
                    if hasattr(self, "flow_running"):
                        self.flow_running.clear()
                    clip = item = None
        finally:
            if hasattr(self, "online"):
                self.online.clear()
            if client:
                client.close()
            if actions:
                actions.close()

    def run(self):
        hotkey = threading.Thread(target=self.poll, name="voice-hotkey", daemon=True)
        hotkey.start()
        threading.Thread(target=self.network, name="voice-network", daemon=True).start()
        try:
            self.wake = voicewake.WakeListener(self.on_wake, self.overlay.push_level,
                                              self.busy, on_state=self.wake_state)
            self.wake.on_pcm = self.share_frame
            if not self.wake.start():
                self.overlay.set_state("error", self.wake.reason)
        except Exception:
            self.overlay.set_state("error", "Wake word unavailable; Right Ctrl still works.")
        def check_stop():
            if self.stop_requested.is_set():
                self.root.quit()
            else:
                self.root.after(100, check_stop)
        if hasattr(self, "stop_requested"):
            self.root.after(100, check_stop)
        try:
            self.root.mainloop()
        finally:
            self.done.set()
            self.player.stop()
            if self.wake:
                self.wake.stop()
            hotkey.join()
            if hasattr(self, "mic_released"):
                self.mic_released.set()
                relay = self.relay
                if relay:
                    relay.close()
                    relay.thread.join(2)


def main():
    host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
    port = int(sys.argv[2]) if len(sys.argv) > 2 else 8409
    key = Path(sys.argv[3]) if len(sys.argv) > 3 else Path(sys.executable).resolve().parents[2] / "voice-link.key"
    # One helper per port in this logged-in Windows session, including launches
    # from a shortcut during a WSL reconnect. Windows releases this on exit.
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.CreateMutexW(None, False, f"Local\\JarvisVoiceAnywhere-{port}")
    if not handle:
        raise OSError("Could not open the Windows voice session.")
    try:
        if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
            return
        agent = Agent(host, port, key)
        if len(sys.argv) > 4 and len(sys.argv[4]) == 64:
            agent.instance = sys.argv[4]
        agent.run()
    finally:
        kernel.CloseHandle(handle)


if __name__ == "__main__":
    main()
