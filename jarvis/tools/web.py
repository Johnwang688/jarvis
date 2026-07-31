"""Web search and page fetching.

Search backend is pluggable: Brave Search API when BRAVE_API_KEY is set
(2k queries/month free, much more reliable), DuckDuckGo's HTML endpoint
otherwise (no key, but rate-limits under heavy use).
"""

from __future__ import annotations

import os
import re
from typing import Annotated
from urllib.parse import parse_qs, unquote, urlparse

import httpx
from bs4 import BeautifulSoup

from . import tool

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def _brave(query: str, count: int) -> list[dict]:
    response = httpx.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": query, "count": count},
        headers={"X-Subscription-Token": os.environ["BRAVE_API_KEY"], "Accept": "application/json"},
        timeout=20,
    )
    response.raise_for_status()
    return [
        {"title": r.get("title", ""), "url": r.get("url", ""), "snippet": r.get("description", "")}
        for r in response.json().get("web", {}).get("results", [])[:count]
    ]


def _unwrap(href: str) -> str:
    # DDG wraps targets in a redirect: //duckduckgo.com/l/?uddg=<real url>
    if "uddg=" in href:
        return unquote(parse_qs(urlparse(href).query).get("uddg", [href])[0])
    return href


def _ddg_html(query: str, count: int) -> list[dict] | None:
    """Primary DDG endpoint. Returns None when hit by the bot challenge."""
    response = httpx.post(
        "https://html.duckduckgo.com/html/",
        data={"q": query},
        headers={"User-Agent": UA},
        timeout=20,
        follow_redirects=True,
    )
    # DDG serves its anomaly/captcha challenge as a 202 with no results in it.
    if response.status_code == 202 or "anomaly" in response.text:
        return None
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    results = []
    for anchor in soup.select("a.result__a")[:count]:
        container = anchor.find_parent(class_="result")
        snippet = container.select_one(".result__snippet") if container else None
        results.append(
            {
                "title": anchor.get_text(strip=True),
                "url": _unwrap(anchor.get("href", "")),
                "snippet": snippet.get_text(strip=True) if snippet else "",
            }
        )
    return results


def _ddg_lite(query: str, count: int) -> list[dict] | None:
    """Fallback endpoint — often unaffected when /html/ is challenging."""
    response = httpx.get(
        "https://lite.duckduckgo.com/lite/",
        params={"q": query},
        headers={"User-Agent": UA},
        timeout=20,
        follow_redirects=True,
    )
    if response.status_code == 202 or "anomaly" in response.text:
        return None
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    anchors = soup.select("a.result-link")[:count]
    snippets = soup.select("td.result-snippet")
    results = []
    for i, anchor in enumerate(anchors):
        results.append(
            {
                "title": anchor.get_text(strip=True),
                "url": _unwrap(anchor.get("href", "")),
                "snippet": snippets[i].get_text(strip=True) if i < len(snippets) else "",
            }
        )
    return results


def _ddg(query: str, count: int) -> list[dict]:
    for backend in (_ddg_html, _ddg_lite):
        results = backend(query, count)
        if results:  # None = challenged, [] = genuinely no hits; try the other
            return results
    raise httpx.HTTPError("DuckDuckGo is rate-limiting (bot challenge on both endpoints)")


@tool
def web_search(
    query: Annotated[str, "What to search for"],
    max_results: Annotated[int, "How many results, 1-10"] = 5,
) -> str:
    """Search the web. Returns titles, URLs, and snippets.

    Snippets are often enough to answer; use fetch_page on a promising URL
    when they are not.
    """
    max_results = max(1, min(int(max_results), 10))
    try:
        if os.environ.get("BRAVE_API_KEY"):
            results = _brave(query, max_results)
        else:
            results = _ddg(query, max_results)
    except httpx.HTTPError as exc:
        return f"Error: search failed ({exc}). Try again or rephrase the query."

    if not results:
        return f"No results for {query!r}. The search backend may be rate-limiting; try again shortly."

    return "\n\n".join(
        f"{i}. {r['title']}\n   {r['url']}\n   {r['snippet']}" for i, r in enumerate(results, 1)
    )


@tool
def fetch_page(
    url: Annotated[str, "The full URL to fetch, e.g. https://example.com/article"],
    max_chars: Annotated[int, "Cap on returned text length"] = 12_000,
) -> str:
    """Fetch a web page and return its readable text content.

    Works on articles and documentation. Pages that require JavaScript or a
    login will come back mostly empty — say so rather than guessing.
    """
    if not url.startswith(("http://", "https://")):
        return "Error: url must start with http:// or https://"

    try:
        response = httpx.get(
            url, headers={"User-Agent": UA}, timeout=30, follow_redirects=True
        )
    except httpx.HTTPError as exc:
        return f"Error: could not fetch {url} ({exc})"

    if response.status_code >= 400:
        return f"Error: {url} returned HTTP {response.status_code}."

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type and "text" not in content_type:
        return f"Error: {url} is {content_type or 'unknown type'}, not a readable page."

    soup = BeautifulSoup(response.text, "html.parser")
    for junk in soup(["script", "style", "noscript", "svg", "iframe", "header", "footer", "nav"]):
        junk.decompose()

    main = soup.find("main") or soup.find("article") or soup.body or soup
    text = re.sub(r"\n{3,}", "\n\n", main.get_text("\n", strip=True))

    title = soup.title.get_text(strip=True) if soup.title else url
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n\n[truncated at {max_chars} chars — page continues]"
    if len(text) < 200:
        text += "\n\n[very little text extracted — this page likely needs JavaScript or a login]"
    return f"# {title}\n\n{text}"
