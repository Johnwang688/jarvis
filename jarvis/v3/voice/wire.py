"""Voice link v3 codecs, shared verbatim with the Windows helper."""

import hashlib
import hmac
import json
import re
import secrets
import struct


UP, DOWN = b"U", b"D"
HELLO, AUDIO, KEY, PLAYED, FLUSHED, CLICK, PING, LEVEL = range(1, 9)
EVENT, AUDIO_OUT, PONG = range(0x81, 0x84)
FIRST, LAST = 1, 2
MAX_LINE = MAX_CONTROL = 4096
MAX_AUDIO = 16384
MAX_TEXT = 600
MIC_SAMPLES = 320
MIC_BYTES = MIC_SAMPLES * 2
CHUNK_MS = 200
HANDSHAKE_TIMEOUT_S = 5.0
MAX_UNAUTHENTICATED = 4
LIVENESS_S = 8.0
HEARTBEAT_S = 2.0
LEVEL_PER_SECOND = 10
MAX_QUEUED_AUDIO_S = 2
MAX_QUEUED_EVENTS = 256
_HEADER = struct.Struct(">BII")
_SPEECH = struct.Struct(">IBI")
MAC_BYTES = 8
UINT32_MAX = 2**32 - 1
_HEX64 = re.compile(r"[0-9a-fA-F]{64}")
_TYPES = {UP: frozenset(range(HELLO, LEVEL + 1)), DOWN: {EVENT, AUDIO_OUT, PONG}}
_STATES = {"resting", "listening", "working", "speaking", "asking", "unavailable"}
_EVENT_TEXT = {
    "state": ("state",), "partial": ("text",), "heard": ("text",), "say": ("text",),
    "asking": ("digest", "tool", "sentence"), "earcon": ("name",), "flush": (),
    "error": ("reason",), "notice": ("title", "text"),
}


class ProtocolError(ValueError):
    """Invalid wire data. Diagnostics must never include its contents."""


def _require(condition):
    if not condition:
        raise ProtocolError("Invalid voice link message.")


def parse_key(data):
    _require(re.fullmatch(rb"[0-9a-fA-F]{64}\n?", data) is not None)
    return bytes.fromhex(data.decode("ascii"))


def _mac(key, data):
    return hmac.digest(key, data, hashlib.sha256)


def derive_k3(key):
    return _mac(key, b"jarvis-voice-link-v3/1")


def _hex(value):
    return isinstance(value, str) and _HEX64.fullmatch(value) is not None


def _nonces(nonce_a, nonce_d):
    _require(_hex(nonce_a) and _hex(nonce_d) and nonce_a.lower() != nonce_d.lower())


def _proof(k3, label, first, second):
    # Use the ASCII hex spelling on the wire, as in the original voice link.
    return _mac(k3, b"|".join((label, first.encode("ascii"), second.encode("ascii"))))


def derive_ks(k3, nonce_a, nonce_d):
    _nonces(nonce_a, nonce_d)
    return _proof(k3, b"session", nonce_a, nonce_d)


def new_nonce():
    return secrets.token_hex(32)


def helper_hello(nonce_a):
    _require(_hex(nonce_a))
    return {"hello": "jarvis-voice3-helper", "v": 3, "nonce_a": nonce_a}


def check_helper_hello(value):
    _require(value.keys() == {"hello", "v", "nonce_a"} and type(value["v"]) is int
             and value == helper_hello(value.get("nonce_a")))
    return value["nonce_a"]


def daemon_hello(k3, nonce_a, nonce_d):
    _nonces(nonce_a, nonce_d)
    return {"hello": "jarvis-voice3-link", "v": 3, "nonce_d": nonce_d,
            "proof": _proof(k3, b"daemon", nonce_a, nonce_d).hex()}


def _check_proof(value, expected):
    _require(_hex(value) and hmac.compare_digest(bytes.fromhex(value), expected))


def check_daemon_hello(k3, nonce_a, value):
    _require(value.keys() == {"hello", "v", "nonce_d", "proof"}
             and value["hello"] == "jarvis-voice3-link" and type(value["v"]) is int and value["v"] == 3)
    nonce_d = value["nonce_d"]
    _nonces(nonce_a, nonce_d)
    _check_proof(value["proof"], _proof(k3, b"daemon", nonce_a, nonce_d))
    return nonce_d


def helper_proof(k3, nonce_a, nonce_d):
    _nonces(nonce_a, nonce_d)
    return {"proof": _proof(k3, b"helper", nonce_d, nonce_a).hex()}


def check_helper_proof(k3, nonce_a, nonce_d, value):
    _nonces(nonce_a, nonce_d)
    _require(value.keys() == {"proof"})
    _check_proof(value["proof"], _proof(k3, b"helper", nonce_d, nonce_a))


def _unique_object(pairs):
    value = dict(pairs)
    _require(len(pairs) == len(value))
    return value


def _invalid_constant(_):
    raise ProtocolError("Invalid voice link JSON.")


def _json_object(data):
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                           parse_constant=_invalid_constant)
    except (ValueError, RecursionError):
        raise ProtocolError("Invalid voice link JSON.") from None
    _require(isinstance(value, dict))
    return value


def _json_bytes(value):
    try:
        return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        raise ProtocolError("Invalid voice link JSON.") from None


def json_line(value):
    line = _json_bytes(value) + b"\n"
    _require(len(line) <= MAX_LINE)
    return line


def read_json(stream):
    line = stream.readline(MAX_LINE + 1)
    _require(len(line) <= MAX_LINE and line.endswith(b"\n"))
    return _json_object(line)


def _payload_limit(direction, kind):
    _require(kind in _TYPES.get(direction, ()))
    return MAX_AUDIO if kind in (AUDIO, AUDIO_OUT) else MAX_CONTROL


class FrameWriter:
    def __init__(self, ks, direction):
        self._key, self._direction, self._seq = ks, direction, 0

    def encode(self, kind, payload):
        _require(len(payload) <= _payload_limit(self._direction, kind) and self._seq <= UINT32_MAX)
        data = _HEADER.pack(kind, len(payload), self._seq) + payload
        tag = _mac(self._key, self._direction + data)[:MAC_BYTES]
        self._seq += 1
        return data + tag


class FrameReader:
    def __init__(self, ks, direction):
        self._key, self._direction, self._seq = ks, direction, 0

    def read(self, stream):
        header = stream.read(_HEADER.size)
        _require(len(header) == _HEADER.size)
        kind, length, seq = _HEADER.unpack(header)
        _require(length <= _payload_limit(self._direction, kind) and seq == self._seq)
        body = stream.read(length + MAC_BYTES)
        _require(len(body) == length + MAC_BYTES)
        payload, tag = body[:length], body[length:]
        expected = _mac(self._key, self._direction + header + payload)[:MAC_BYTES]
        _require(hmac.compare_digest(tag, expected))
        self._seq += 1
        return kind, decode_payload(kind, payload)


def _id(value):
    return isinstance(value, str) or type(value) is int


def _event(value):
    kind = value.get("kind")
    _require(isinstance(kind, str) and kind in _EVENT_TEXT)
    _require(all(isinstance(value.get(field), str) for field in _EVENT_TEXT[kind]))
    _require(all(not isinstance(field, str) or len(field) <= MAX_TEXT for field in value.values()))
    if kind == "state":
        _require(value["state"] in _STATES and isinstance(value.get("reason", ""), str))
    if kind in ("say", "asking"):
        _require(_id(value.get("id")))
    if kind == "say":
        _require(type(value.get("n")) is int and 0 <= value["n"] <= UINT32_MAX)
    if kind == "asking":
        _require(type(value.get("expires_in")) in (int, float))
    return value


def _json_payload(kind, value):
    if kind == HELLO:
        _require(isinstance(value.get("features"), list)
                 and all(isinstance(feature, str) for feature in value["features"])
                 and type(value.get("canceller")) is bool and isinstance(value.get("version"), str))
    elif kind == PLAYED:
        _require(_id(value.get("id")))
    elif kind == CLICK:
        _require(_id(value.get("id")) and isinstance(value.get("digest"), str)
                 and value.get("decision") in ("approve", "deny"))
    else:
        return _event(value)
    return value


def encode_event(kind, **fields):
    value = {key: field[:MAX_TEXT] if isinstance(field, str) else field for key, field in fields.items()}
    value["kind"] = kind
    data = _json_bytes(_event(value))
    if len(data) <= MAX_CONTROL:
        return data
    # A 600-character field can take more than 600 bytes. Fit display strings
    # to the frame without cutting an identifier, digest, or UTF-8 code point.
    display = set(value) - {"kind", "id", "n", "digest", "state"}
    low, high = 0, MAX_TEXT
    while low < high:
        middle = (low + high + 1) // 2
        trial = {key: field[:middle] if key in display and isinstance(field, str) else field
                 for key, field in value.items()}
        if len(_json_bytes(trial)) <= MAX_CONTROL:
            low = middle
        else:
            high = middle - 1
    return encode_payload(EVENT, {key: field[:low] if key in display and isinstance(field, str) else field
                                  for key, field in value.items()})


def audio_chunk_samples(rate):
    _require(type(rate) is int and 5 <= rate <= UINT32_MAX)
    return min(rate * CHUNK_MS // 1000, (MAX_AUDIO - _SPEECH.size) // 2)


def _check_audio_out(flags, rate, pcm):
    _require(type(flags) is int and 0 <= flags <= (FIRST | LAST) and isinstance(pcm, bytes)
             and len(pcm) % 2 == 0 and 0 < len(pcm) <= 2 * audio_chunk_samples(rate))


def encode_payload(kind, value):
    if kind in (HELLO, PLAYED, CLICK, EVENT):
        payload = _json_bytes(_json_payload(kind, value))
    elif kind == AUDIO:
        raw, cancelled = value
        _require(len(raw) == MIC_BYTES and (cancelled is None or len(cancelled) == MIC_BYTES))
        payload = raw + (cancelled or b"")
    elif kind == AUDIO_OUT:
        index, flags, rate, pcm = value
        _require(type(index) is int and 0 <= index <= UINT32_MAX)
        _check_audio_out(flags, rate, pcm)
        payload = _SPEECH.pack(index, flags, rate) + pcm
    elif kind in (KEY, LEVEL):
        _require(type(value) is bool if kind == KEY else type(value) is int and 0 <= value <= 255)
        payload = bytes((value,))
    elif kind == FLUSHED:
        _require(value is None)
        payload = b""
    else:
        payload = value
        _require(kind in (PING, PONG) and isinstance(payload, bytes) and len(payload) == 8)
    _require(len(payload) <= _payload_limit(UP if kind < EVENT else DOWN, kind))
    return payload


def decode_payload(kind, payload):
    _require(len(payload) <= _payload_limit(UP if kind < EVENT else DOWN, kind))
    if kind in (HELLO, PLAYED, CLICK, EVENT):
        return _json_payload(kind, _json_object(payload))
    if kind == AUDIO:
        _require(len(payload) in (MIC_BYTES, 2 * MIC_BYTES))
        return payload[:MIC_BYTES], payload[MIC_BYTES:] or None
    if kind == AUDIO_OUT:
        _require(len(payload) > _SPEECH.size)
        index, flags, rate = _SPEECH.unpack_from(payload)
        pcm = payload[_SPEECH.size:]
        _check_audio_out(flags, rate, pcm)
        return index, flags, rate, pcm
    if kind == KEY:
        _require(payload in (b"\x00", b"\x01"))
        return payload == b"\x01"
    if kind == LEVEL:
        _require(len(payload) == 1)
        return payload[0]
    if kind == FLUSHED:
        _require(not payload)
        return None
    _require(kind in (PING, PONG) and len(payload) == 8)
    return payload
