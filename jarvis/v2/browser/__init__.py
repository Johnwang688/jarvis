"""The HUD's built-in Browser (BR-1a): a daemon-owned Google Chrome, its SOCKS
gate and its navigation policy.

- `policy.py`: the URL judge and the connection rule, pure.
- `gate.py`: the loopback-only SOCKS5 proxy every Chrome connection passes
  through, which resolves names itself and connects to the address it judged.
- `host.py`: Chrome itself, one instance per profile plus the Private one,
  driven by Playwright's async API on its own thread; the landing guard.
- `routes.py`: `/browser…`, owner-only on the HUD listener.
- `settings.py`: `browser.json`, the owner's switches (`lan`, `local_servers`,
  BR-8a's `auto_open`): the one reader and the one writer of that file.

Design: docs/plans/2026-10-10-hud-browser-plan.md, its amendment 1 and its
decisions file (which wins). Fully separate from v1's `jarvis.browser`.
Imports nothing: `daemon.py` imports `settings.py` without starting the engine.
"""
