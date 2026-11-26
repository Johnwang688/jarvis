"""Status categories shared by the work ledger and its watcher."""

from . import t3_client


FINISHED = frozenset({"completed", "idle", "exported"})
FINAL = t3_client.SETTLED - {"waiting"} | {"gone"}


def category(status, waiting):
    if status in FINISHED:
        return "finished"
    if status in FINAL:
        return "stopped"
    return "waiting" if waiting else "running"
