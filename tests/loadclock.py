"""CPU-time bounds that hold while other suites run (QA-LOAD, 2026-10-14).

`time.thread_time()` stops charging a check for the time other processes
hold the CPU, but not for the CPU running slower while they do: a busy SMT
sibling, a shared cache and a shared memory bus slow every instruction this
thread runs. Measured on the owner's 24-thread machine with 24 busy loops
beside the check, `rules_check`'s slowest cost line went from 52-69 to
80-104 CPU ms against its 100 ms bound, and the terminal's colour flood from
58 to 35 MB/CPU-s against its 28; full sweeps failed at 114 and 145 ms.

So a bound that a run misses is judged again at the machine's present
speed: a fixed reference workload (`reference`, plain Python string, regex
and dict work, and none of Jarvis's code, so no mutation of it can slow the
yardstick) is timed right after the run, and the run's CPU time is divided
by how much slower the reference ran than it does on this machine idle
(`REFERENCE_IDLE_S`).

What that buys, and what it costs (be exact about it):

- **Idle, the bound is exact.** A run under its bound is never rescaled, and
  on an idle machine the slowdown is 1, so the check is the bound as written.
- **Under load the bound is looser, by up to ~30%.** The reference does not
  slow exactly as the work does. Measured by the QA-LOAD review under 24 and
  48 busy loops, the reference slowed 1.29-1.44x while the real work slowed
  1.06-1.27x, so dividing by the reference excused ~10-30% more than the load
  cost (the sniffer flood ~29%). The best of three runs then keeps the most
  favourable ratio. So under load a regression up to ~30% past its bound can
  pass; it still fails alone.
- **Never more than 3x.** The slowdown is clamped to [1, `MAX_SLOWDOWN`]: a
  fast moment never tightens a bound past what it says, and no load excuses
  more than 3x, so work costing over three times its bound idle fails however
  busy the machine is. The pinned mutants and their idle cost against their
  bounds: `rules_check`, `charge()` a no-op, 531 ms against 100 ms (5.3x);
  `terminal_check` and `devurl_check`, colour off the bulk path (`_PLAIN`
  without its SGR branch), 7.3 against 28 MB/CPU-s (3.8x): both past the cap,
  so caught under any load. `vex_check`'s synthetic mutant (`decide()`
  judging a line eight times), 2.04 s against 1 s (2.0x), is inside the cap:
  it read 1.71-2.30 s at idle speed under 48 burner scopes and failed every
  run there, but a load that slowed the reference twice as much as the work
  could excuse it.
- The reference is timed three times and the fastest taken, the same
  estimate the callers make of their work: a pause that hits a reference run
  can only tighten the bound, and a pause that hits the work is what the
  callers' retries are for.

Only a run that misses the raw bound pays for the reference, so a check that
passes alone costs nothing extra. These constants set the strength of every
bound that uses them, and each caller pins them (`pinned`).

Recalibrate `REFERENCE_IDLE_S` on a new machine or Python with the machine
idle: `python tests/loadclock.py` prints the reference's idle cost. Round it
down (a smaller value makes every scaled bound stricter, never looser).
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import time

REFERENCE_IDLE_S = 0.010    # `reference()` thread CPU on the owner's machine, idle (2026-10-14)
MAX_SLOWDOWN = 3.0          # the most a busy machine may excuse

_TEXT = " ".join(f"--opt{i % 97}=v{i} run{i % 13} 'q {i}'" for i in range(3000))
_WORD = re.compile(r"[^\s']+|'[^']*'")


def reference() -> int:
    """A fixed piece of CPU work: tokenise, split, count and join."""
    seen: dict[str, int] = {}
    for _ in range(8):
        for word in _WORD.findall(_TEXT):
            key = word.lstrip("-").split("=", 1)[0]
            seen[key] = seen.get(key, 0) + len(word)
        "".join(sorted(seen))
    return len(seen)


def cpu(fn, *args, **kwargs):
    """(calling-thread CPU seconds, value) for `fn(*args, **kwargs)`."""
    started = time.thread_time()
    value = fn(*args, **kwargs)
    return time.thread_time() - started, value


def slowdown() -> float:
    """How much slower than idle this machine runs `reference` now, in
    [1, MAX_SLOWDOWN]: the fastest of three timings."""
    took = min(cpu(reference)[0] for _ in range(3))
    return min(max(took / REFERENCE_IDLE_S, 1.0), MAX_SLOWDOWN)


@dataclass(frozen=True)
class Held:
    """A run's CPU time, and the machine's slowdown when it missed its bound."""
    cpu: float
    slow: float = 1.0

    @property
    def idle(self) -> float:
        """The run's CPU seconds at this machine's idle speed."""
        return self.cpu / self.slow

    def __str__(self) -> str:
        if self.slow == 1.0:
            return f"{self.cpu * 1000:.0f} CPU ms"
        return (f"{self.idle * 1000:.0f} CPU ms at idle speed ({self.cpu * 1000:.0f} measured, "
                f"machine {self.slow:.2f}x slow)")


_reference_warm = False


def judge(cpu_s: float, limit_s: float) -> Held:
    """`cpu_s` as a `Held`: as measured when it is under `limit_s`; otherwise
    scaled by the slowdown measured now, right after the run."""
    global _reference_warm
    if cpu_s < limit_s:
        return Held(cpu_s)
    if not _reference_warm:         # compile and allocate once, outside the timing
        reference()
        _reference_warm = True
    return Held(cpu_s, slowdown())


def pinned() -> None:
    """The constants that set the strength of every bound judged here
    (CLAUDE.md: pin guard-strength constants)."""
    assert (REFERENCE_IDLE_S, MAX_SLOWDOWN) == (0.010, 3.0), (REFERENCE_IDLE_S, MAX_SLOWDOWN)


if __name__ == "__main__":
    reference()
    runs = sorted(cpu(reference)[0] for _ in range(40))
    print(f"reference(): fastest {runs[0] * 1000:.2f} ms, median {runs[20] * 1000:.2f} ms, "
          f"slowest {runs[-1] * 1000:.2f} ms of 40 (REFERENCE_IDLE_S = {REFERENCE_IDLE_S}); "
          f"slowdown now {slowdown():.2f}x")
