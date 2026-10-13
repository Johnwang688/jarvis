# Compose recovery follow-ups

Overlapping file reads are capped against current reducer state, and the owner sees how many files were left out. A failed first send can park its words and files while its compose pane is hidden; the same row takes them back only when drawn, once, with identity validation. Closing a slot moves already parked compose data to the next owner-started thread, even if that slot now shows a different thread. Explicitly discarded visible compose input stays discarded.

Draft-location notices replace only their own obsolete location; unrelated errors and thread-draft notices remain. A pane with its own pending draft explains why global parked words still wait. Project channel failures append to the recovery notice.

Plan independently reviewed by title_patch. Regression tests cover live/parked attachment overflow, hidden delayed first-send refusal, next drawn recovery, close/reuse, duplicate/stale restoration, other-thread isolation and notice preservation. Stop during send acknowledgment is a separate package because it needs exact-turn daemon synchronization. Reducer clock cleanup and InputBar measurement remain separate follow-ups.
