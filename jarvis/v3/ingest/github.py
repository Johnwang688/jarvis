"""Index open authored and review-requested pull requests using one query."""

from datetime import datetime
import hashlib
import json
import re

from . import Doc, RateLimited
from .. import signals


_FIRST = 100
_MAX_PAGES = 10  # GitHub search exposes the first 1,000 matches.
_SEARCHES = {"authored": "authored", "review": "review_requested"}
_QUERY = f"""query($authoredAfter: String, $reviewAfter: String,
                   $fetchAuthored: Boolean! = true, $fetchReview: Boolean! = true) {{
  authored: search(query: "is:pr is:open archived:false author:@me", type: ISSUE,
                   first: {_FIRST}, after: $authoredAfter) @include(if: $fetchAuthored) {{
    issueCount
    pageInfo {{ hasNextPage endCursor }}
    nodes {{ ...PullRequestFields }}
  }}
  review: search(query: "is:pr is:open archived:false review-requested:@me", type: ISSUE,
                 first: {_FIRST}, after: $reviewAfter) @include(if: $fetchReview) {{
    issueCount
    pageInfo {{ hasNextPage endCursor }}
    nodes {{ ...PullRequestFields }}
  }}
}}
fragment PullRequestFields on PullRequest {{
  id number title url isDraft state updatedAt
  repository {{ nameWithOwner }}
  author {{ login }}
  reviewDecision
  commits(last: 1) {{ nodes {{ commit {{ oid statusCheckRollup {{ state }} }} }} }}
}}"""
GH_ARGV = (("gh", "api", "graphql", "-f", f"query={_QUERY}"),)
_REPO = re.compile(r"[a-zA-Z0-9-]+/[a-zA-Z0-9_.-]+")
_CURSOR = re.compile(r"[A-Za-z0-9_+/=-]{1,4096}")
_CHECKS = {"success", "failure", "pending", "error", "expected"}


class GitHubError(Exception):
    pass


def _object(value):
    return value if isinstance(value, dict) else {}


def _repo_number(row):
    repo = _object(row.get("repository")).get("nameWithOwner")
    number = row.get("number")
    if isinstance(repo, str) and _REPO.fullmatch(repo) and type(number) is int and number > 0:
        return repo, number


def _string(value):
    return value if isinstance(value, str) else None


def _timestamp(updated):
    if updated is None:
        return None
    try:
        instant = datetime.fromisoformat(updated)
        if instant.tzinfo is not None:
            return instant.timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def _checks(commit):
    if "statusCheckRollup" not in commit:
        return None
    rollup = commit["statusCheckRollup"]
    if rollup is None:
        return "none"
    state = _string(_object(rollup).get("state"))
    if state is not None and state.lower() in _CHECKS:
        return state.lower()


def _node(row, repo, number):
    draft = row.get("isDraft")
    updated = _string(row.get("updatedAt"))
    ts = _timestamp(updated)
    decision = _string(row.get("reviewDecision"))
    commits = _object(row.get("commits")).get("nodes")
    commit = _object(_object(commits[-1]).get("commit")) if isinstance(commits, list) and commits else {}
    fields = signals.scrub({"repo": repo, "number": number, "title": _string(row.get("title")),
                            "draft": int(draft) if type(draft) is bool else None,
                            "checks": _checks(commit), "review_decision": decision.lower() if decision else None,
                            "url": _string(row.get("url")), "updated": updated if ts is not None else None,
                            "author": _string(_object(row.get("author")).get("login"))})
    return f"{fields['repo']}#{number}", fields, ts, signals.scrub(_string(commit.get("oid")))


class GitHubIngester:
    name = "github"

    def __init__(self, run, *, signals_dir=None, require_pagination=False):
        self._run = run
        self._signals_dir = signals_dir
        self._require_pagination = require_pagination

    def poll(self, cursor):
        try:
            return self._poll(cursor or {})
        except (RateLimited, GitHubError):
            raise
        except Exception:
            raise GitHubError("GitHub poll failed.") from None

    def _search(self, pending):
        argv = GH_ARGV[0]
        for alias in _SEARCHES:
            # Both switches are non-null in the query: GitHub rejects a request
            # that leaves either one out, so each is always sent.
            argv += ("-F", f"fetch{alias.capitalize()}={'true' if alias in pending else 'false'}")
            if pending.get(alias) is not None:
                argv += ("-f", f"{alias}After={pending[alias]}")
        try:
            status, text = self._run(argv)
        except Exception:
            raise GitHubError("GitHub command failed.") from None
        payload = _object(json.loads(text))
        errors = payload.get("errors")
        if isinstance(errors, list) and any(_object(error).get("type") == "RATE_LIMITED" for error in errors):
            raise RateLimited()
        if status != 0 or errors:
            raise GitHubError("GitHub command failed.")
        return _object(payload.get("data"))

    def _pull_requests(self):
        pending = dict.fromkeys(_SEARCHES)
        seen_cursors = {alias: set() for alias in _SEARCHES}
        seen_nodes = {alias: set() for alias in _SEARCHES}
        totals = {}
        counts = dict.fromkeys(_SEARCHES, 0)
        incomplete = set()
        prs = {}
        for _ in range(_MAX_PAGES):
            data = self._search(pending)
            following = {}
            for alias in pending:
                search = _object(data.get(alias))
                rows = search.get("nodes")
                if not isinstance(rows, list):
                    raise GitHubError("Invalid GitHub response.")
                counts[alias] += len(rows)
                page = _object(search.get("pageInfo"))
                more = page.get("hasNextPage", False)
                if type(more) is not bool or ("pageInfo" in search and "hasNextPage" not in page):
                    raise GitHubError("Incomplete GitHub search.")
                has_total, has_page = "issueCount" in search, "pageInfo" in search
                if has_total:
                    count = search["issueCount"]
                    if (type(count) is not int or not 0 <= count <= _FIRST * _MAX_PAGES
                            or count != totals.setdefault(alias, count)
                            or (counts[alias] >= count if more else counts[alias] != count)):
                        raise GitHubError("Incomplete GitHub search.")
                    if count == _FIRST * _MAX_PAGES:
                        incomplete.add(alias)
                elif more or pending[alias] is not None:
                    raise GitHubError("Missing GitHub search total.")
                # The input contract also accepts a single, uncapped nodes-only
                # listing. The daemon's GraphQL query must return both markers.
                single_page = not self._require_pagination and not (has_total or has_page) and len(rows) < _FIRST
                if len(rows) > _FIRST or not (single_page or (has_total and has_page)):
                    incomplete.add(alias)
                if more:
                    token = page.get("endCursor")
                    if (not rows or not isinstance(token, str) or not _CURSOR.fullmatch(token)
                            or signals.scrub(token) != token or token in seen_cursors[alias]):
                        raise GitHubError("Invalid or repeated GitHub search cursor.")
                    seen_cursors[alias].add(token)
                    following[alias] = token
                for row in rows[:_FIRST]:
                    row = _object(row)
                    identity = _repo_number(row)
                    identifier = row.get("id")
                    identities = set()
                    if isinstance(identifier, str) and identifier:
                        identities.add(identifier)
                    if identity is not None:
                        identities.add((identity[0].lower(), identity[1]))
                    if identities & seen_nodes[alias]:
                        raise GitHubError("GitHub search changed while paging.")
                    seen_nodes[alias].update(identities)
                    if identity is None:
                        incomplete.add(alias)
                        continue
                    ref, fields, ts, oid = _node(row, *identity)
                    # The searches may see different versions of the same PR.
                    entry, previous_ts, previous_oid = prs.get(
                        ref, ({"authored": 0, "review_requested": 0}, None, None))
                    if ref not in prs or ts is not None and (previous_ts is None or ts > previous_ts):
                        entry.update(fields)
                        previous_ts, previous_oid = ts, oid
                    entry[_SEARCHES[alias]] = 1
                    prs[ref] = entry, previous_ts, previous_oid
            if not following:
                return prs, incomplete
            pending = following
        raise GitHubError("GitHub search page limit reached.")

    def _poll(self, cursor):
        prs, incomplete = self._pull_requests()
        known = _object(cursor.get("digests"))
        items = {}
        digests = {}
        if incomplete:
            snapshot = signals.read(self.name, directory=self._signals_dir)
            items = {item["id"]: item for item in snapshot["items"]} if snapshot else {}
            if known.keys() - items.keys() - prs.keys():
                raise GitHubError("Incomplete GitHub search and missing previous items.")
            digests.update(known)
        changes = []
        for ref, (entry, ts, oid) in prs.items():
            for alias in incomplete:
                role = _SEARCHES[alias]
                if items.get(ref, {}).get(role) == 1:
                    entry[role] = 1
            item = {key: entry[key] for key in ("repo", "number", "title", "draft", "checks",
                                                "authored", "review_requested", "review_decision")}
            items[ref] = {"id": ref, **item}
            title = entry.pop("title")
            digest = hashlib.sha256(json.dumps([title, entry, oid], sort_keys=True).encode()).hexdigest()
            digests[ref] = digest
            if known.get(ref) != digest:
                meta = {"untrusted": 1, "state": "open", **entry}
                body = [f"{ref} {title}" if title else ref]
                if entry["checks"] is not None:
                    body.append(f"Checks: {entry['checks']}")
                if entry["url"]:
                    body.append(entry["url"])
                changes.append(Doc(ref, title or "", ts if ts is not None else 0,
                                   [("pull_request", signals.scrub("\n".join(body)), ts)], meta))
        for ref in known.keys() - digests.keys():
            changes.append(Doc(ref, "", 0, (), {"untrusted": 1}, deleted=True))
        signals.write(self.name, items.values(), directory=self._signals_dir)
        return changes, {"digests": digests}
