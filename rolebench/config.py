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

MAX_CONCURRENT = 3
MEMORY_MAX = "3G"

REVIEW_TIMEOUT_S = 1800
IMPLEMENT_TIMEOUT_S = 3600
GRADE_TIMEOUT_S = 900
REVIEW_MAX_TURNS = 80
IMPLEMENT_MAX_TURNS = 120

# Decision O-3: Kimi K3 runs on Moonshot direct (the BYOK credit) and is
# budgeted at that provider's price, never the catalog's. Any id under the
# `moonshotai/` prefix must be listed here: an unknown Kimi id is refused
# rather than run unpinned at an unknown price.
MOONSHOT_PREFIX = "moonshotai/"
MOONSHOT_PROVIDER = "moonshotai"
MOONSHOT_PRICES = {"moonshotai/kimi-k3": (3.0, 15.0)}   # $ per M input, output


class UnknownModel(ValueError):
    pass


def _moonshot(model: str) -> bool:
    return (model or "").strip().lower().startswith(MOONSHOT_PREFIX)


def price_override(model: str) -> tuple[float, float] | None:
    """The pinned provider's price for a `moonshotai/` id; None for any other
    model; UnknownModel for a Kimi id that is not in the table."""
    if not _moonshot(model):
        return None
    key = model.strip().lower()
    if key not in MOONSHOT_PRICES:
        raise UnknownModel(f"{model}: unknown moonshotai/ model (known: {', '.join(sorted(MOONSHOT_PRICES))})")
    return MOONSHOT_PRICES[key]


def provider_pin(model: str) -> str:
    """The provider a call to `model` is pinned to ('' = OpenRouter picks)."""
    return MOONSHOT_PROVIDER if price_override(model) else ""

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
    """The bench's own copy of hud/node_modules, made once (never the owner's tree)."""
    return home() / "hud-node_modules"


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
