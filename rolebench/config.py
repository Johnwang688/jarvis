"""Where things live, and the constants every module shares.

All of it is overridable by environment variable so a test never touches the
real cache, the real cases or the real checkout.
"""

from __future__ import annotations

import os
import pwd
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent

# The Python that has jarvis' dependencies. Reviewers are told to use it for
# the suites, and the Jarvis-loop worker runs on it.
DEFAULT_PYTHON = "/home/johnw/projects/Jarvis/.venv/bin/python"

HARNESSES = ("claude", "codex", "jarvis")

SEVERITIES = ("HIGH", "MED", "LOW", "NIT")
# Recall weights (plan 3.4). NIT is recorded in ground truth but never scored.
WEIGHTS = {"HIGH": 5, "MED": 3, "LOW": 1, "NIT": 0}

# Decision O-2: OpenRouter cells only. Claude and Codex cells use plan quota.
TOTAL_CAP_USD = 10.0
PER_RUN_CAP_USD = 0.50

# At or above this percent of any quota window the Claude or Codex cell is
# skipped (the Diff quick review's threshold, `jarvis.v2.diff_review.THRESHOLD`).
QUOTA_THRESHOLD = 75.0
# The hard ceiling: at or above it a Claude or Codex cell, or a judge call, is
# refused even under the owner's override. ROLEBENCH_QUOTA_CEILING can only
# lower it, never raise it, and never lift the rule.
QUOTA_CEILING = 97.0

MAX_CONCURRENT = 3
MEMORY_MAX = "3G"

REVIEW_TIMEOUT_S = 1800
IMPLEMENT_TIMEOUT_S = 3600
GRADE_TIMEOUT_S = 900
REVIEW_MAX_TURNS = 80
IMPLEMENT_MAX_TURNS = 120

# Decision O-1 as amended 2026-10-21 (owner): the models each harness runs.
MATRIX = {
    "claude": ("haiku", "sonnet", "opus"),                   # Claude Code, the 5.5 family
    "codex": ("gpt-6.1-sol", "gpt-6-luna"),
    "jarvis": ("anthropic/claude-haiku-5.5", "deepseek/deepseek-v4.1-flash", "z-ai/glm-5.3-flash"),
}



class OffMatrix(ValueError):
    pass


def check_matrix(harness: str, model: str, off_matrix: bool = False) -> None:
    """OffMatrix unless `model` is in MATRIX[harness] (exact id) or the run
    was started with --off-matrix (recorded in its run row)."""
    if off_matrix or model in MATRIX.get(harness, ()):
        return
    raise OffMatrix(f"{model!r} is not in the O-1 matrix for {harness} "
                    f"({', '.join(MATRIX.get(harness, ())) or 'none'}); pass --off-matrix to run it anyway")


# The estimate's completion allowance when the call sets no max_tokens and no
# endpoint lists its maximum completion.
DEFAULT_COMPLETION_ALLOWANCE = 16_000
# Long-context price tiers the endpoints list does not carry: at or above
# `threshold` prompt tokens the whole request is priced at `multiplier` times
# (Haiku 5.5: 5x from 100k, CLAUDE.md's model default note).
PRICE_TIERS = {"anthropic/claude-haiku-5.5": (100_000, 5.0)}

# Kimi K3 was dropped from the matrix by the owner (O-1 amendment, 2026-10-21),
# and with it the Moonshot pin and its price floor. The guard stays: a
# `moonshotai/` id is refused unless it is listed here, and none is, so no
# Kimi id runs unpinned at an unknown price.
MOONSHOT_PREFIX = "moonshotai/"
KIMI_ALLOWED: tuple[str, ...] = ()


class UnknownModel(ValueError):
    pass


def check_model(model: str) -> None:
    """UnknownModel for a `moonshotai/` id that is not allowed (all of them, now)."""
    key = (model or "").strip().lower()
    if key.startswith(MOONSHOT_PREFIX) and key not in KIMI_ALLOWED:
        raise UnknownModel(f"{model}: moonshotai/ ids are refused (Kimi left the matrix in the "
                           f"O-1 amendment; allowed: {', '.join(KIMI_ALLOWED) or 'none'})")

# Never in a bench run's environment (use the CLI logins, never a key). The
# first three are the ones the plan names; the rest would silently switch a CLI
# from its login to an API key, or hand a run a way to push.
SCRUBBED_ENV = (
    "GH_TOKEN", "GITHUB_TOKEN", "OPENROUTER_API_KEY",
    "GH_ENTERPRISE_TOKEN", "GITHUB_PAT", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN",
    "OPENAI_API_KEY", "CODEX_API_KEY",
)


def real_home() -> str:
    """The account's home directory, whatever HOME says (tests repoint HOME)."""
    return pwd.getpwuid(os.getuid()).pw_dir


def login_home() -> str:
    """Where the CLI logins are read from (a test points this at fakes)."""
    return os.environ.get("ROLEBENCH_LOGIN_HOME") or real_home()


def venv_dir() -> Path:
    """The venv a cell mounts read-only (python_bin's, without resolving links)."""
    return Path(os.environ.get("ROLEBENCH_VENV") or Path(python_bin()).parent.parent)


def node_modules_dir() -> Path:
    """The bench's own copy of hud/node_modules, made once (never the owner's tree).
    The directory must be named node_modules: Node resolves a package's own
    dependencies by walking up from its real path to a `node_modules` dir."""
    return home() / "hud-deps" / "node_modules"


def home() -> Path:
    """Everything the bench writes: snapshots, runs, the log, the ledger."""
    return Path(os.environ.get("ROLEBENCH_HOME")
                or Path.home() / ".cache" / "jarvis-lead" / "rolebench")


def cases_dir() -> Path:
    return Path(os.environ.get("ROLEBENCH_CASES") or PACKAGE_DIR / "cases")


def source_repo() -> Path:
    """The clone that holds the objects snapshots are built from."""
    return Path(os.environ.get("ROLEBENCH_REPO") or REPO_ROOT)


def python_bin() -> str:
    return os.environ.get("ROLEBENCH_PYTHON") or DEFAULT_PYTHON


def snap_root() -> Path:
    return home() / "snap"


def runs_root() -> Path:
    return home() / "runs"


def log_path() -> Path:
    return home() / "log.jsonl"


def ledger_path() -> Path:
    return home() / "spend.json"


def prices_dir() -> Path:
    return home() / "prices"
