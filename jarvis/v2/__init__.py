"""Jarvis v2 — the control plane over Claude Code and Codex.

Design: docs/jarvis-v2-design.md. Nothing under this package imports the v1
loop except through jarvis.v2.providers.fastpath. Interfaces in model.py and
provider.py are owned by the lead (Claude); implementations of work packages
must not change them without a design-doc edit.
"""
