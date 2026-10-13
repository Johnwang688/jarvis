# Task action refusals and question contract

Scope: checkpoint hud-open-followups PR46 only. Current App TaskView handlers swallow steer/cancel/resume/start refusals; TaskView discards steering text before success. Transcript Cancel does the same. The TypeScript question rule is not pinned to Python.

Change both App TaskView mounts to callbacks accepting captured task ID and returning rejecting promises; successful state mutations refresh tasks, no optimistic state claims. TaskView catches these promises, keeps per-task error, steering words and pending state, disables duplicate action requests synchronously, and shows daemon refusals below controls. Clear only the submitted steering value on success, preserving newly typed words. Switching tasks cannot redirect a request or place late errors/text into the next task. Transcript Cancel keeps its stable callback but shows contextual refusal in the existing window error area.

Keep answer behavior and all permissions unchanged. Test: real HTTP409/network failure for every action, per-task pending/retry/success, steer text preservation and delayed result across task switch, transcript Cancel visibility. Pin actual question box IDs by computing commands.open_questions on deserialized mock Task, including assumable, answered-empty and non-clarifying states. Build/full units, focused task browser and full HUD before merge; independent code review and mutations.

Independent plan review: accepted with a shared App guard across all mounts,
steering draft versions (including edit-away/back), and separate mutation/read
outcomes. TaskView keeps per-task pending state, words and error messages.
