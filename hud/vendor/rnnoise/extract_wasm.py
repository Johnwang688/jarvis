#!/usr/bin/env python3
"""Re-derive hud/vendor/rnnoise/rnnoise_0.2.wasm from Jitsi's rnnoise-sync.js.

    python3 extract_wasm.py <rnnoise-sync.js | rnnoise-wasm-0.2.1.tgz> [out.wasm]

Stdlib only. Both inputs are checked against the digests in manifest.json before
anything is decoded, and the result against the pinned wasm digest: the binary is
the base64 data URI inside the sync build, decoded unchanged. Untrusted input: it
is only hashed, matched with one regex and base64-decoded, never executed.
"""
import base64, hashlib, io, json, re, sys, tarfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
M = json.loads((HERE / "manifest.json").read_text())["model"]


def sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def main(argv):
    src = Path(argv[1]).read_bytes()
    if src[:2] == b"\x1f\x8b":
        assert sha(src) == M["derived_from"]["tarball_sha256"], "tarball digest differs from the pin"
        with tarfile.open(fileobj=io.BytesIO(src), mode="r:gz") as t:
            src = t.extractfile(M["derived_from"]["file_in_tarball"]).read()
    assert sha(src) == M["derived_from"]["file_sha256"], "rnnoise-sync.js digest differs from the pin"
    m = re.search(rb"data:application/octet-stream;base64,([A-Za-z0-9+/=]+)", src)
    wasm = base64.b64decode(m.group(1), validate=True)
    assert len(wasm) == M["bytes"] and sha(wasm) == M["sha256"], "decoded wasm differs from the pin"
    out = Path(argv[2]) if len(argv) > 2 else HERE / M["file"]
    out.write_bytes(wasm)
    print(f"{out}: {len(wasm)} bytes, sha256 {sha(wasm)}")


if __name__ == "__main__":
    main(sys.argv)
