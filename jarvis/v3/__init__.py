"""Jarvis v3 is the next Jarvis control plane, built alongside frozen v2.
It imports v2 modules only through the explicit allowlist, and v2 never
imports v3. When v3 needs different behavior from a v2 module, copy the
module into v3 and change it there.

`jarvis agentd` serves the v3 status API on loopback port 8410 by default.
"""
