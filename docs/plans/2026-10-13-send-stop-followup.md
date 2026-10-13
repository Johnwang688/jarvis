# Stop during send acknowledgement

A Stop can arrive after a first thread opens, or during an existing thread's
send request, before the daemon acknowledges a turn. Track that send through
the acknowledgement and use its exact turn ID. Never stop a successor because
an HTTP response or SSE is late. Failed or dropped sends clear the pending stop.

The daemon checks the optional expected turn identity under its lifecycle lock.
Provider interrupt delivery runs outside that lock with a thread-keyed
reservation, preventing a new or replacement session from starting a successor
until delivery completes. Temporary reservations preserve queued words; all
exit paths release and reconsider the queue. Existing unqualified Stop remains
compatible. Invalid expected identities are rejected at the route boundary.

Validation: targeted units, delayed HTTP browser fixtures, lifecycle tests with
barrier-controlled provider delivery, independent review, and complete backend
and HUD sweeps before merge. This is split from the compose-draft repairs.
