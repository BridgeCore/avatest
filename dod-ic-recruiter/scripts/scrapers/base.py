"""
base.py — Abstract base class for all dod-ic-recruiter scrapers.

Every scraper module must:
  1. Subclass BaseScraper.
  2. Set class attribute source_platform (str).
  3. Implement search(queries) -> list[CandidateRaw].

Common helpers (user-agent rotation, randomised delay, 429/403 handling) are
provided here so concrete scrapers stay lean and consistent.
"""

from __future__ import annotations

import random
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import List

import httpx

from scripts.deduplicator import CandidateRaw  # noqa: F401 — re-exported for scrapers


# ---------------------------------------------------------------------------
# Rotating user-agent pool
# ---------------------------------------------------------------------------

_USER_AGENTS: List[str] = [
    # Chrome on Windows
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    # Chrome on macOS
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    # Firefox on Windows
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) "
        "Gecko/20100101 Firefox/125.0"
    ),
    # Firefox on Linux
    (
        "Mozilla/5.0 (X11; Linux x86_64; rv:124.0) "
        "Gecko/20100101 Firefox/124.0"
    ),
    # Safari on macOS
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.4.1 Safari/605.1.15"
    ),
    # Edge on Windows
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
    ),
]


def random_user_agent() -> str:
    """Return a randomly selected User-Agent string."""
    return random.choice(_USER_AGENTS)


def random_delay(min_s: float = 1.5, max_s: float = 4.5) -> None:
    """Sleep for a randomised duration to reduce rate-limit exposure."""
    time.sleep(random.uniform(min_s, max_s))


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

_DEFAULT_TIMEOUT = 20        # seconds per request
_MAX_RETRIES = 3
_RETRY_BACKOFF_BASE = 2.0    # seconds; multiplied by attempt index


def build_headers(extra: dict | None = None) -> dict:
    """
    Return a browser-like headers dict with a freshly sampled User-Agent.

    Pass ``extra`` to merge or override individual headers.
    """
    headers = {
        "User-Agent": random_user_agent(),
        "Accept": (
            "text/html,application/xhtml+xml,application/xml;q=0.9,"
            "image/avif,image/webp,*/*;q=0.8"
        ),
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }
    if extra:
        headers.update(extra)
    return headers


def get_with_retry(
    url: str,
    *,
    params: dict | None = None,
    headers: dict | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
    max_retries: int = _MAX_RETRIES,
) -> httpx.Response | None:
    """
    Perform an HTTP GET with automatic retry on 429 / 5xx responses.

    - 200: returned immediately.
    - 403: returns None (hard block, no point retrying without proxy rotation).
    - 429: honours ``Retry-After`` header or exponential back-off; rotates UA.
    - 5xx: exponential back-off, rotate UA.
    - Network error: back-off, rotate UA, retry.

    Returns the ``httpx.Response`` on success or ``None`` on failure.
    """
    hdrs = headers or build_headers()
    for attempt in range(max_retries):
        try:
            resp = httpx.get(
                url,
                params=params,
                headers=hdrs,
                timeout=timeout,
                follow_redirects=True,
            )
        except httpx.RequestError:
            time.sleep(_RETRY_BACKOFF_BASE * (attempt + 1))
            hdrs = build_headers()
            continue

        if resp.status_code == 200:
            return resp

        if resp.status_code == 403:
            # Hard block — cease immediately
            return None

        if resp.status_code == 429:
            retry_after = resp.headers.get("Retry-After")
            wait = float(retry_after) if retry_after else _RETRY_BACKOFF_BASE ** (attempt + 1)
            time.sleep(wait)
            hdrs = build_headers()
            continue

        if resp.status_code >= 500:
            time.sleep(_RETRY_BACKOFF_BASE * (attempt + 1))
            hdrs = build_headers()
            continue

        # Non-retryable status (e.g. 404, 301 already followed)
        return resp

    return None


# ---------------------------------------------------------------------------
# Abstract base scraper
# ---------------------------------------------------------------------------

class BaseScraper(ABC):
    """
    Abstract interface that every platform scraper must implement.

    Subclasses:
    - Set ``source_platform`` at the class level.
    - Implement ``search(queries) -> list[CandidateRaw]``.
    - Use module-level helpers (``get_with_retry``, ``random_delay``,
      ``build_headers``) rather than raw httpx calls.
    """

    #: Lowercase platform identifier written into every CandidateRaw record.
    source_platform: str = "unknown"

    def now(self) -> datetime:
        """Return the current UTC time as a timezone-aware datetime."""
        return datetime.now(tz=timezone.utc)

    @abstractmethod
    def search(self, queries: List[str]) -> List[CandidateRaw]:
        """
        Search the platform for candidates matching each query string.

        Parameters
        ----------
        queries:
            Plain-text search strings (skill labels, job titles, etc.).
            Each query is searched independently; results are concatenated.

        Returns
        -------
        list[CandidateRaw]
            Flat list of raw candidate records.  Duplicates across queries
            are expected — the caller deduplicates downstream via
            ``scripts.deduplicator.deduplicate()``.
        """
        ...
