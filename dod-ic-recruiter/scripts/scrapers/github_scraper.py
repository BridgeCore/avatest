"""
github_scraper.py — GitHub public-API scraper for dod-ic-recruiter.

Finds US-based engineers/analysts whose GitHub profiles carry signals
relevant to the DoD/IC/defense community (clearance mentions, agency
references, defense-adjacent technology stacks) and maps their public
profile data onto CandidateRaw records for downstream scoring.

Authentication
--------------
Set the environment variable GITHUB_TOKEN to a personal access token (PAT)
to raise the GitHub REST API rate limit from 10 req/min (unauthenticated)
to 60 req/min (authenticated).  The scraper runs without a token but will
exhaust the unauthenticated quota quickly on large query sets.

Rate-limit handling
-------------------
- Authenticated:   60 requests/minute  (REST search endpoint: 30/min)
- Unauthenticated: 10 requests/minute  (REST search endpoint: 10/min)

On HTTP 429 or 403 the request is logged and skipped; the scraper never
raises an exception due to a rate-limit response.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from scripts.deduplicator import CandidateRaw
from scripts.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GITHUB_API_BASE = "https://api.github.com"

# Bio / profile keywords that indicate IC/DoD/cleared talent.
_CLEARANCE_KEYWORDS: list[str] = [
    "clearance",
    "secret clearance",
    "top secret",
    "ts/sci",
    "dod",
    "department of defense",
    "ic",
    "intelligence community",
    "defense",
    "government",
    "intelligence",
    "nsa",
    "cia",
    "dia",
    "nro",
    "nga",
    "disa",
    "darpa",
    "afrl",
    "cleared",
]

# GitHub topic labels that suggest defense/gov-aligned repositories.
_IC_TOPICS: list[str] = [
    "defense",
    "government",
    "military",
    "intelligence",
    "cybersecurity",
    "osint",
    "geoint",
    "sigint",
    "national-security",
    "federal",
]

# Maximum GitHub search results pages to walk per query (100 results/page).
_MAX_PAGES = 3

# Seconds to wait after a 429 before retrying (in addition to random delay).
_RATE_LIMIT_BACKOFF = 60.0


# ---------------------------------------------------------------------------
# GitHubScraper
# ---------------------------------------------------------------------------

class GitHubScraper(BaseScraper):
    """
    Scrapes GitHub user-search for DoD/IC-relevant talent profiles.

    Usage
    -----
    ::

        scraper = GitHubScraper()
        candidates = scraper.search(["python sigint", "rust dod embedded"])
    """

    def __init__(self) -> None:
        token = os.environ.get("GITHUB_TOKEN", "").strip()
        self._headers: dict[str, str] = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if token:
            self._headers["Authorization"] = f"Bearer {token}"
            logger.info("GitHubScraper: using authenticated requests (60 req/min)")
        else:
            logger.info(
                "GitHubScraper: no GITHUB_TOKEN set — unauthenticated (10 req/min)"
            )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(self, queries: list[str]) -> list[CandidateRaw]:
        """
        Search GitHub for DoD/IC-relevant users matching *queries*.

        For each query the scraper builds a GitHub user-search string that
        restricts results to US-based accounts and appends clearance/IC
        keyword filters.  Profiles are then individually fetched to gather
        bio, employer, languages, and pinned repository metadata.

        Parameters
        ----------
        queries:
            Free-text search strings (e.g. role keywords, technology names).

        Returns
        -------
        list[CandidateRaw]
            Deduplicated (by profile URL) list of candidate records.
        """
        seen_urls: set[str] = set()
        results: list[CandidateRaw] = []

        with httpx.Client(
            headers=self._headers,
            timeout=20.0,
            follow_redirects=True,
        ) as client:
            for query in queries:
                logger.info("GitHubScraper: processing query %r", query)
                user_logins = self._search_users(client, query)

                for login in user_logins:
                    profile_url = f"https://github.com/{login}"
                    if profile_url in seen_urls:
                        continue
                    seen_urls.add(profile_url)

                    candidate = self._build_candidate(client, login)
                    if candidate is not None:
                        results.append(candidate)
                    self._random_delay()

        logger.info(
            "GitHubScraper: collected %d candidate(s) from %d query(ies)",
            len(results),
            len(queries),
        )
        return results

    # ------------------------------------------------------------------
    # Internal helpers — search
    # ------------------------------------------------------------------

    def _search_users(self, client: httpx.Client, query: str) -> list[str]:
        """
        Run the GitHub user-search API and return a list of login names.

        The query is augmented to restrict to US location and append
        bio-keyword signals.  Up to _MAX_PAGES pages of 100 results each
        are fetched.
        """
        # Build a composite search query:
        #   <caller query> location:US <clearance OR IC keywords in bio>
        # The GitHub user search supports: in:login, in:email, in:fullname.
        # Bio keyword matching requires the ``in:bio`` qualifier per term.
        keyword_clause = " ".join(
            f'"{kw}" in:bio' for kw in random.sample(_CLEARANCE_KEYWORDS, k=5)
        )
        full_query = f"{query} location:US {keyword_clause}"

        logins: list[str] = []
        for page in range(1, _MAX_PAGES + 1):
            params: dict[str, Any] = {
                "q": full_query,
                "per_page": 100,
                "page": page,
            }
            response = self._get(
                client,
                f"{_GITHUB_API_BASE}/search/users",
                params=params,
            )
            if response is None:
                break

            data = self._safe_json(response)
            if not data:
                break

            items: list[dict] = data.get("items", [])
            if not items:
                break

            logins.extend(item["login"] for item in items if "login" in item)

            # GitHub caps user-search results at 1 000 total regardless of
            # pagination; stop early if we have everything.
            total = data.get("total_count", 0)
            if len(logins) >= min(total, _MAX_PAGES * 100):
                break

            self._random_delay()

        return logins

    # ------------------------------------------------------------------
    # Internal helpers — profile enrichment
    # ------------------------------------------------------------------

    def _build_candidate(
        self, client: httpx.Client, login: str
    ) -> CandidateRaw | None:
        """
        Fetch full profile, top languages, and repository topics for *login*
        then map the data onto a CandidateRaw record.

        Returns None if the profile fetch fails (rate-limited, deleted, etc.).
        """
        # --- Fetch user profile -------------------------------------------
        profile_resp = self._get(client, f"{_GITHUB_API_BASE}/users/{login}")
        if profile_resp is None:
            return None
        profile: dict = self._safe_json(profile_resp) or {}

        if not profile:
            return None

        # --- Fetch public repositories (up to 30, sorted by stars) --------
        repos_resp = self._get(
            client,
            f"{_GITHUB_API_BASE}/users/{login}/repos",
            params={"per_page": 30, "sort": "stars", "type": "public"},
        )
        repos: list[dict] = []
        if repos_resp is not None:
            repos = self._safe_json(repos_resp) or []

        # --- Extract profile fields ----------------------------------------
        username: str = profile.get("login", login)
        display_name: str = profile.get("name") or username
        bio: str = profile.get("bio") or ""
        company: str = profile.get("company") or ""
        location: str = profile.get("location") or ""
        email: str = profile.get("email") or ""
        profile_url: str = profile.get("html_url") or f"https://github.com/{login}"

        # --- Derive tech skills from language breakdown --------------------
        languages = self._collect_languages(client, repos)

        # --- Collect repository topics and names for raw_text --------------
        repo_topics: list[str] = []
        repo_names: list[str] = []
        for repo in repos:
            repo_names.append(repo.get("name", ""))
            repo_topics.extend(repo.get("topics") or [])

        # Deduplicate topics
        repo_topics = list(dict.fromkeys(repo_topics))

        # --- Contribution activity signal (public event count proxy) -------
        contrib_signal = self._contribution_signal(profile)

        # --- Build raw_text ------------------------------------------------
        raw_parts: list[str] = [
            f"username: {username}",
            f"bio: {bio}",
            f"company: {company}",
            f"location: {location}",
            f"languages: {', '.join(languages)}",
            f"top_repos: {', '.join(repo_names[:10])}",
            f"topics: {', '.join(repo_topics[:20])}",
            f"public_repos: {profile.get('public_repos', 0)}",
            f"followers: {profile.get('followers', 0)}",
            f"contribution_activity: {contrib_signal}",
        ]
        raw_text = " | ".join(raw_parts)

        # --- Infer work history from company field -------------------------
        work_history_note = (
            f"Current employer (self-reported on GitHub): {company}"
            if company
            else ""
        )

        # Prepend work_history_note to raw_text if present
        if work_history_note:
            raw_text = work_history_note + " | " + raw_text

        return CandidateRaw(
            name=display_name,
            source_url=profile_url,
            source_platform="github",
            raw_text=raw_text,
            scraped_at=datetime.now(tz=timezone.utc),
            current_employer=company.lstrip("@").strip(),
            current_title="",          # GitHub has no structured title field
            location=location,
            skills=languages,
            email=email,
        )

    def _collect_languages(
        self, client: httpx.Client, repos: list[dict]
    ) -> list[str]:
        """
        Aggregate programming languages across a user's public repos.

        Fetches the languages endpoint for the top 5 starred repos and
        returns a deduplicated, frequency-sorted list of language names.
        """
        lang_counts: dict[str, int] = {}
        for repo in repos[:5]:
            langs_url = repo.get("languages_url", "")
            if not langs_url:
                continue
            resp = self._get(client, langs_url)
            if resp is None:
                continue
            lang_data: dict = self._safe_json(resp) or {}
            for lang, byte_count in lang_data.items():
                lang_counts[lang] = lang_counts.get(lang, 0) + byte_count
            self._random_delay()

        # Sort by byte-count descending, return names only
        return [
            lang
            for lang, _ in sorted(
                lang_counts.items(), key=lambda kv: kv[1], reverse=True
            )
        ]

    @staticmethod
    def _contribution_signal(profile: dict) -> str:
        """
        Build a lightweight contribution-activity descriptor from profile
        summary fields (no separate API call required).
        """
        public_repos = profile.get("public_repos", 0)
        followers = profile.get("followers", 0)
        public_gists = profile.get("public_gists", 0)

        if public_repos >= 50 or followers >= 100:
            return "high"
        if public_repos >= 10 or followers >= 20:
            return "medium"
        if public_repos >= 1 or public_gists >= 1:
            return "low"
        return "none"

    # ------------------------------------------------------------------
    # Internal helpers — HTTP
    # ------------------------------------------------------------------

    def _get(
        self,
        client: httpx.Client,
        url: str,
        params: dict[str, Any] | None = None,
    ) -> httpx.Response | None:
        """
        Perform a GET request with a rotated User-Agent.

        Returns the response on success, None on 429/403 or network error.
        Never raises an exception — all errors are logged and suppressed so
        the caller can continue to the next item.
        """
        headers = {"User-Agent": self._rotate_user_agent()}
        try:
            response = client.get(url, params=params, headers=headers)
        except httpx.RequestError as exc:
            logger.warning(
                "GitHubScraper: network error fetching %s — %s", url, exc
            )
            return None

        if self._handle_rate_limit(response):
            # Back off longer than the standard random delay.
            logger.info(
                "GitHubScraper: backing off %.0fs after rate-limit on %s",
                _RATE_LIMIT_BACKOFF,
                url,
            )
            time.sleep(_RATE_LIMIT_BACKOFF)
            return None

        if not response.is_success:
            logger.warning(
                "GitHubScraper: unexpected HTTP %d for %s",
                response.status_code,
                url,
            )
            return None

        return response

    @staticmethod
    def _safe_json(response: httpx.Response) -> Any:
        """Parse JSON from *response*, returning None on decode failure."""
        try:
            return response.json()
        except Exception as exc:
            logger.warning("GitHubScraper: JSON decode error — %s", exc)
            return None


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------

def search(queries: list[str]) -> list[CandidateRaw]:
    """
    Module-level entry point — instantiates GitHubScraper and calls search().

    Parameters
    ----------
    queries:
        Free-text search strings passed directly to :meth:`GitHubScraper.search`.

    Returns
    -------
    list[CandidateRaw]
    """
    return GitHubScraper().search(queries)
