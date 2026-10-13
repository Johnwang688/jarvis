# Notice timestamps

PR52 follow-up: the store now captures issuedAt once at dispatch, before React can defer or replay an action. The wrote/noticed helpers use that captured time for the error line and one-second notice joining. Explicit errorAt and protocol at values retain their meanings; legacy direct reducer calls without a stamp use zero. This change does not remove unrelated existing reducer clocks.

Independent plan review passed. Four pre-fix behavior regressions reproduced delayed/replayed timestamp changes, including through the production StoreProvider. Source review, focused/full UI gates and two named mutations are recorded with the lead execution evidence before merge.
