"""v1-registry tools that only the v2 layer uses.

A module here registers into `jarvis.tools.REGISTRY` through the ordinary
`@tool` decorator (invariant 6: schema from type hints), so a v2 provider can
hand the name to a v1 `Agent` with nothing else changed. Importing the module
*is* the registration, so a provider imports what it needs and a v1-only
process never sees these at all.
"""
