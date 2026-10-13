# Per-turn runtime cleanup

An agent turn restored only its toolset, leaving its plan, approver, cancellation callback and loaded-group bindings on the calling context. A scripted fresh-context reproduction confirmed those four values remained after completion; nested depth could remain too.

`runtime.turn_scope()` restores the six values `_run_turn` binds with ContextVar tokens on success, nested calls and exceptions. Mutable plan and loaded-group objects remain attached to their agent, so later turns retain their state. Provider-owned origin, caller, proposal and desk bindings are untouched. Compact-only turns remain unchanged. Session recording runs after restoration, as before.

Free dispatch checks cover originally unset and prebound contexts, success, provider errors, nested real turns, default-denying approval after completion, provider slots, and loaded tools across successive turns. Existing toolset and unconfined policies are unchanged. Independent review and a lead sweep precede merge.
