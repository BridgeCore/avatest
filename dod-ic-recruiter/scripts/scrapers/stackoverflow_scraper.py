"""
stackoverflow_scraper.py — Stack Overflow public API scraper.

Uses the Stack Exchange REST API v2.3 to search for public user profiles
matching skill/tag combinations derived from each query string.

Public interface
----------------
    from scripts.scrapers.stackoverflow_scraper import StackOverflowScraper

    scraper = StackOverflowScraper()
    candidates = scraper.search(["python intelligence analyst", "geospatial GIS"])

Environment variables
---------------------
STACKOVERFLOW_KEY (optional)
    Stack Exchange API application key.  When present, the per-day request
    quota is raised from ~300 (anonymous) to ~10 000 (keyed).  Obtain one at
    https://stackapps.com/apps/oauth/register.

Notes
-----
- All data is fetched from the public Stack Exchange API — no login required.
- The API returns gzip-compressed JSON; httpx decompresses automatically.
- Users without a listed location or employer are still included; those fields
  will be empty strings, and the scorer will treat them as unknown.
- ``inname`` filter searches display names; ``tagged`` filters by top tags.
  We combine both signals across queries for broad coverage.
"""

from __future__ import annotations

import os
from typing import List

from dotenv import load_dotenv

from scripts.scrapers.base import (
    BaseScraper,
    CandidateRaw,
    build_headers,
    get_with_retry,
    random_delay,
)

load_dotenv()

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_API_BASE = "https://api.stackexchange.com/2.3"
_USERS_ENDPOINT = f"{_API_BASE}/users"
_PAGE_SIZE = 30        # max allowed by the API per page
_MAX_PAGES = 3         # pages per (query, strategy) combination

# Stack Exchange filter that requests only the fields we need, keeping
# response size small.  "default" returns all fields; a custom filter
# can be created at https://api.stackexchange.com/docs/filters — but the
# default is safe and requires no registration.
_API_FILTER = "default"


class StackOverflowScraper(BaseScraper):
    """
    Searches Stack Overflow user profiles via the Stack Exchange API.

    Two search strategies are applied per query:
    1. ``inname`` — search display names for tokens from the query.
    2. ``tagged``  — search users whose top tags match skills in the query.

    Results from both strategies are merged and deduplicated by user_id
    before being returned.
    """

    source_platform = "stackoverflow"

    def __init__(self) -> None:
        self._api_key: str | None = os.environ.get("STACKOVERFLOW_KEY") or None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(self, queries: List[str]) -> List[CandidateRaw]:
        """
        Search Stack Overflow user profiles for each query.

        Parameters
        ----------
        queries:
            Plain-text search strings (skill labels, job titles, etc.).

        Returns
        -------
        list[CandidateRaw]
            Aggregated results across all queries and search strategies.
        """
        results: List[CandidateRaw] = []
        seen_user_ids: set[int] = set()

        for query in queries:
            tags = _extract_tags(query)
            name_token = _primary_name_token(query)

            # Strategy 1: inname search
            if name_token:
                for user in self._fetch_by_inname(name_token):
                    if user["user_id"] not in seen_user_ids:
                        seen_user_ids.add(user["user_id"])
                        results.append(self._build_candidate(user))
                random_delay(1.0, 2.5)

            # Strategy 2: tagged search
            if tags:
                for user in self._fetch_by_tags(tags):
                    if user["user_id"] not in seen_user_ids:
                        seen_user_ids.add(user["user_id"])
                        results.append(self._build_candidate(user))
                random_delay(1.0, 2.5)

        return results

    # ------------------------------------------------------------------
    # API fetch helpers
    # ------------------------------------------------------------------

    def _base_params(self) -> dict:
        """Common query parameters sent with every API call."""
        params: dict = {
            "site": "stackoverflow",
            "pagesize": _PAGE_SIZE,
            "filter": _API_FILTER,
            "order": "desc",
            "sort": "reputation",
        }
        if self._api_key:
            params["key"] = self._api_key
        return params

    def _fetch_by_inname(self, name_token: str) -> List[dict]:
        """Fetch users whose display name contains ``name_token``."""
        all_users: List[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            params = self._base_params()
            params["inname"] = name_token
            params["page"] = page

            data = self._get_json(_USERS_ENDPOINT, params=params)
            if data is None:
                break

            items: List[dict] = data.get("items", [])
            all_users.extend(items)

            if not data.get("has_more", False):
                break

        return all_users

    def _fetch_by_tags(self, tags: List[str]) -> List[dict]:
        """
        Fetch users via the top-tags endpoint for each tag, then merge.

        The Stack Exchange API does not expose a direct "users by tag"
        endpoint, but ``/tags/{tag}/top-answerers`` and
        ``/tags/{tag}/top-askers`` return user lists.  We use both to
        maximise coverage.
        """
        all_users: List[dict] = []
        seen_in_batch: set[int] = set()

        for tag in tags[:5]:   # cap at 5 tags per query to stay within quota
            for role in ("top-answerers", "top-askers"):
                url = f"{_API_BASE}/tags/{tag}/{role}/all_time"
                params = self._base_params()
                # top-answerers/askers return {"items": [{"user": {...}, "score": n}]}
                data = self._get_json(url, params=params)
                if data is None:
                    continue

                for item in data.get("items", []):
                    user = item.get("user") or item
                    uid = user.get("user_id")
                    if uid and uid not in seen_in_batch:
                        seen_in_batch.add(uid)
                        all_users.append(user)

                random_delay(0.8, 1.8)

        return all_users

    def _get_json(self, url: str, params: dict) -> dict | None:
        """
        Perform a GET against the Stack Exchange API and return parsed JSON.

        Returns None on HTTP failure or if the response is not valid JSON.
        """
        headers = build_headers(
            extra={"Accept": "application/json", "Accept-Encoding": "gzip"}
        )
        resp = get_with_retry(url, params=params, headers=headers)
        if resp is None:
            return None

        try:
            return resp.json()
        except Exception:
            return None

    # ------------------------------------------------------------------
    # CandidateRaw construction
    # ------------------------------------------------------------------

    def _build_candidate(self, user: dict) -> CandidateRaw:
        """
        Map a Stack Exchange API user object to a CandidateRaw record.

        API user fields used:
          display_name, location, link (profile URL), website_url,
          about_me (free-text bio).
        """
        name: str = user.get("display_name", "")
        source_url: str = user.get("link", "https://stackoverflow.com/users")
        location: str = user.get("location") or ""

        # Stack Overflow does not expose employer directly — extract from bio
        about_me: str = _strip_html(user.get("about_me") or "")
        current_employer: str = _extract_employer_from_bio(about_me)
        current_title: str = _extract_title_from_bio(about_me)

        # Top tags function as a proxy for skills
        # The /users endpoint returns badge_counts but not top tags directly.
        # When available via a richer filter, "top_tags" is a list of dicts
        # with "tag_name".  Fall back to empty list gracefully.
        top_tags: List[dict] = user.get("top_tags") or []
        skills: List[str] = [t["tag_name"] for t in top_tags if t.get("tag_name")]

        # Compose raw_text from all available free-text fields
        website: str = user.get("website_url") or ""
        raw_parts = [name, location, current_employer, current_title, about_me, website]
        raw_text = " ".join(p for p in raw_parts if p)

        return CandidateRaw(
            name=name,
            source_url=source_url,
            source_platform=self.source_platform,
            raw_text=raw_text,
            scraped_at=self.now(),
            current_title=current_title,
            current_employer=current_employer,
            location=location,
            skills=skills,
        )


# ---------------------------------------------------------------------------
# Query parsing helpers
# ---------------------------------------------------------------------------

# Common English stop words to strip when extracting tag tokens
_STOP_WORDS = frozenset({
    "a", "an", "the", "and", "or", "of", "in", "for", "to", "with",
    "on", "at", "by", "from", "is", "are", "was", "be", "this", "that",
    "it", "its", "as", "not", "no", "clearance", "cleared", "analyst",
    "engineer", "developer", "manager", "senior", "junior", "lead",
    "staff", "principal", "role", "position",
})


def _extract_tags(query: str) -> List[str]:
    """
    Derive Stack Overflow tag candidates from a plain-text query string.

    Tokens are lowercased, stop-words stripped, and short tokens (<3 chars)
    removed.  Single-word technology names make the best SO tags.
    """
    tokens = query.lower().replace("-", " ").replace("/", " ").split()
    tags = [
        t.strip(".,;:!?")
        for t in tokens
        if t.strip(".,;:!?") not in _STOP_WORDS
        and len(t.strip(".,;:!?")) >= 3
    ]
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: List[str] = []
    for tag in tags:
        if tag not in seen:
            seen.add(tag)
            unique.append(tag)
    return unique


def _primary_name_token(query: str) -> str:
    """
    Return the most meaningful single token from the query for an inname
    search.  We use the longest non-stop token as the primary signal.
    """
    tokens = [
        t.strip(".,;:!?")
        for t in query.lower().split()
        if t.strip(".,;:!?") not in _STOP_WORDS
        and len(t.strip(".,;:!?")) >= 3
    ]
    if not tokens:
        return ""
    return max(tokens, key=len)


# ---------------------------------------------------------------------------
# HTML / text helpers
# ---------------------------------------------------------------------------

import re as _re  # local import to keep top-level namespace clean


def _strip_html(text: str) -> str:
    """Remove HTML tags from a string (bio fields often contain markup)."""
    return _re.sub(r"<[^>]+>", " ", text).strip()


def _extract_employer_from_bio(bio: str) -> str:
    """
    Attempt to extract a current employer from an about_me bio string.

    Looks for patterns like:
    - "at Acme Corp"
    - "@ Lockheed"
    - "works at / working at ..."
    - "employed by ..."
    """
    patterns = [
        r"(?:currently\s+)?(?:works?|working)\s+at\s+([A-Z][^\n,.]{2,40})",
        r"(?:employed|employed by)\s+([A-Z][^\n,.]{2,40})",
        r"@\s*([A-Z][^\n,.]{2,40})",
        r"(?:at|for)\s+([A-Z][^\n,.]{2,40})",
    ]
    for pat in patterns:
        m = _re.search(pat, bio, _re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


def _extract_title_from_bio(bio: str) -> str:
    """
    Attempt to extract a job title from an about_me bio string.

    Looks for patterns like:
    - "Software Engineer at"
    - "I am a / I'm a Senior Analyst"
    - "Principal Engineer"
    """
    patterns = [
        r"(?:i\s+am\s+a|i'm\s+a)\s+([A-Z][^\n,.@]{3,60}?)(?:\s+at\b|[,.\n]|$)",
        r"^([A-Z][a-z]+(?:\s+[A-Z]?[a-z]+){0,4})\s+at\s+",
        r"(?:title|role|position)\s*[:\-]\s*([^\n,.]{3,60})",
    ]
    for pat in patterns:
        m = _re.search(pat, bio.strip(), _re.IGNORECASE | _re.MULTILINE)
        if m:
            return m.group(1).strip()
    return ""
