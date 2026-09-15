"""Provider implementations. One module per provider; the interface is ../provider.py.

Nothing here is imported on package import: a provider drags in a CLI SDK or a
subprocess transport, and a surface that only needs the fast path must not pay
for the others. Import the module you want by name.
"""
