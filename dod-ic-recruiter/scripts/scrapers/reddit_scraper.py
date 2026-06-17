"""
reddit_scraper.py — Reddit API scraper for dod-ic-recruiter.

Targets subreddits where cleared professionals self-identify relevant
experience, employer history, or security-clearance status.  Reddit data is
treated as a cross-reference signal only — NOT a primary source.

All Reddit-sourced candidates are flagged:
    recruiter_flag: "Reddit lead — partial data only"

Required environment variables
--------------------------------
    REDDIT_CLIENT_ID   — App client ID from https://www.reddit.com/prefs/apps
    REDDIT_SECRET      — App client secret

Free registration: create a "script" type application at the URL above.
No user credentials are needed for read-only public data.

Targeted subreddits
-------------------
    r/SecurityClearance   — Clearance holders discussing jobs / poly / TS/SCI
    r/netsec              — Security researchers and practitioners
    r/govtech             — Gov IT / digital services professionals
    r/cscareerquestions   — CS professionals who may mention cleared roles
    r/cybersecurity       — Broad security community

Usage
-----
    from scripts.scrapers.reddit_scraper import RedditScraper

    scraper = RedditScraper()
    candidates = scraper.search([
        "cleared developer python MITRE",
        "signals intelligence NGA",
    ])
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any

import httpx

from scripts.deduplicator import CandidateRaw
from scripts.scrapers.orcid_scraper import BaseScraper

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
_SEARCH_URL = "https://oauth.reddit.com/r/{subreddit}/search"
_COMMENTS_URL = "https://oauth.reddit.com/r/{subreddit}/search"

_TARGET_SUBREDDITS = [
    "SecurityClearance",
    "netsec",
    "govtech",
    "cscareerquestions",
    "cybersecurity",
]

# Keywords that suggest a post is a self-identification of employment/experience
_SELF_ID_PATTERNS = [
    re.compile(r"\bi\s+(work|worked|am|was)\s+(at|for|with|in)\b", re.IGNORECASE),
    re.compile(r"\bmy\s+(current\s+)?(employer|company|job|role|position|team)\b", re.IGNORECASE),
    re.compile(r"\b(cleared|clearance|ts[/ ]sci|top.?secret|poly(graph)?)\b", re.IGNORECASE),
    re.compile(r"\b(mitre|apl|sandia|rand|saic|booz allen|leidos|bah|caci|cia|nsa|dia|nga)\b", re.IGNORECASE),
    re.compile(r"\b(years? (of )?experience|background in|expertise in)\b", re.IGNORECASE),
]

_EMPLOYER_EXTRACT_RE = re.compile(
    r"\b(?:work(?:ed|ing)?|am|was|employed)\s+(?:at|for|by|with)\s+([\w &,./()-]{3,50})",
    re.IGNORECASE,
)

_TITLE_EXTRACT_RE = re.compile(
    r"\b(?:i(?:'m| am| was) (?:a |an )?)([\w\s/-]{3,60}?)(?:\s+at\b|\s+for\b|\s*[.,;]|\s*$)",
    re.IGNORECASE,
)

# Rate limiting: Reddit allows ~60 requests/minute for OAuth apps
_REQUEST_DELAY = 1.2

_REDDIT_FLAG = "Reddit lead — partial data only"


# ---------------------------------------------------------------------------
# RedditScraper
# ---------------------------------------------------------------------------

class RedditScraper(BaseScraper):
    """
    Scrapes public Reddit posts and comments for self-identifying IC/cleared
    professionals.

    Authentication
    --------------
    Reads REDDIT_CLIENT_ID and REDDIT_SECRET from the environment.  A bearer
    token is obtained once via client-credentials flow (no user login required
    for read-only access to public subreddits).

    Data quality note
    -----------------
    Reddit usernames are pseudonyms.  Treat all Reddit-sourced records as weak
    signals requiring manual follow-up.  The recruiter_flag field is always set
    to _REDDIT_FLAG to make this explicit in the data store.
    """

    def __init__(
        self,
        subreddits: list[str] | None = None,
        request_delay: float = _REQUEST_DELAY,
        timeout: float = 20.0,
        results_per_query: int = 25,
    ) -> None:
        self._subreddits = subreddits or _TARGET_SUBREDDITS
        self._request_delay = request_delay
        self._timeout = timeout
        self._results_per_query = results_per_query
        self._token: str | None = None

        client_id = os.environ.get("REDDIT_CLIENT_ID", "").strip()
        secret = os.environ.get("REDDIT_SECRET", "").strip()

        if not client_id or not secret:
            raise EnvironmentError(
                "RedditScraper requires REDDIT_CLIENT_ID and REDDIT_SECRET "
                "environment variables.  Register a free 'script' app at "
                "https://www.reddit.com/prefs/apps to obtain these credentials."
            )

        self._client_id = client_id
        self._secret = secret

        self._http = httpx.Client(
            timeout=timeout,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "dod-ic-recruiter/1.0 by u/bcore_recruiting_tool "
                    "(read-only research tool)"
                )
            },
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(self, queries: list[str]) -> list[CandidateRaw]:
        """
        Search target subreddits for each query and return CandidateRaw records.

        Deduplication within this call: same Reddit username is merged so a
        user appearing across multiple queries or subreddits yields one record.

        Parameters
        ----------
        queries:
            Free-text keyword strings.

        Returns
        -------
        List of CandidateRaw with source_platform="reddit" and recruiter_flag
        set to "Reddit lead — partial data only".
        """
        self._ensure_token()

        seen_usernames: dict[str, CandidateRaw] = {}

        for query in queries:
            for subreddit in self._subreddits:
                logger.info(
                    "RedditScraper: searching r/%s for %r", subreddit, query
                )
                posts = self._search_subreddit(subreddit, query)
                for post in posts:
                    candidate = self._post_to_candidate(post, subreddit)
                    if candidate is None:
                        continue
                    username = candidate.source_url  # username embedded in URL
                    if username in seen_usernames:
                        # Merge raw_text and skills into existing record
                        _merge_reddit_records(seen_usernames[username], candidate)
                    else:
                        seen_usernames[username] = candidate
                time.sleep(self._request_delay)

        results = list(seen_usernames.values())
        logger.info("RedditScraper: returning %d candidates.", len(results))
        return results

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def _ensure_token(self) -> None:
        """Obtain a bearer token if we don't have one yet."""
        if self._token:
            return
        try:
            resp = self._http.post(
                _TOKEN_URL,
                data={"grant_type": "client_credentials"},
                auth=(self._client_id, self._secret),
            )
            resp.raise_for_status()
            self._token = resp.json()["access_token"]
            self._http.headers["Authorization"] = f"bearer {self._token}"
            logger.debug("RedditScraper: obtained bearer token.")
        except (httpx.HTTPError, KeyError) as exc:
            raise RuntimeError(
                f"RedditScraper: failed to obtain Reddit access token: {exc}"
            ) from exc

    # ------------------------------------------------------------------
    # Subreddit search
    # ------------------------------------------------------------------

    def _search_subreddit(
        self, subreddit: str, query: str
    ) -> list[dict[str, Any]]:
        """
        Search a single subreddit and return a list of post data dicts.
        """
        url = _SEARCH_URL.format(subreddit=subreddit)
        params = {
            "q": query,
            "limit": self._results_per_query,
            "sort": "relevance",
            "t": "all",
            "restrict_sr": True,
            "type": "link",
        }
        try:
            resp = self._http.get(url, params=params)
            resp.raise_for_status()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                # Token may have expired; reset and retry once
                self._token = None
                self._ensure_token()
                try:
                    resp = self._http.get(url, params=params)
                    resp.raise_for_status()
                except httpx.HTTPError as inner_exc:
                    logger.warning(
                        "RedditScraper: retry also failed for r/%s query %r: %s",
                        subreddit, query, inner_exc,
                    )
                    return []
            else:
                logger.warning(
                    "RedditScraper: request failed for r/%s query %r: %s",
                    subreddit, query, exc,
                )
                return []
        except httpx.HTTPError as exc:
            logger.warning(
                "RedditScraper: request failed for r/%s query %r: %s",
                subreddit, query, exc,
            )
            return []

        data = resp.json()
        posts = []
        for child in (data.get("data") or {}).get("children") or []:
            post_data = child.get("data") or {}
            if post_data:
                posts.append(post_data)
        return posts

    # ------------------------------------------------------------------
    # Post -> CandidateRaw
    # ------------------------------------------------------------------

    def _post_to_candidate(
        self, post: dict[str, Any], subreddit: str
    ) -> CandidateRaw | None:
        """
        Convert a Reddit post dict into a CandidateRaw.

        Returns None if the post does not contain self-identifying signals.
        """
        author = (post.get("author") or "").strip()
        if not author or author in ("[deleted]", "AutoModerator", ""):
            return None

        title = (post.get("title") or "").strip()
        selftext = (post.get("selftext") or "").strip()
        combined_text = f"{title}\n{selftext}".strip()

        # Filter: only keep posts that show self-identification signals
        if not _has_self_id_signal(combined_text):
            return None

        permalink = post.get("permalink", "")
        post_url = f"https://www.reddit.com{permalink}" if permalink else ""

        # Attempt to extract employer and title from text
        current_employer = _extract_employer(combined_text)
        current_title = _extract_title(combined_text)

        # Skills: extract clearance levels and known tech/domain keywords
        skills = _extract_skills(combined_text)

        # raw_text: include enough context for the scorer
        raw_text = (
            f"Reddit username: u/{author}\n"
            f"Subreddit: r/{subreddit}\n"
            f"Post title: {title}\n\n"
            f"{selftext[:2000]}"  # cap at 2k chars to keep records manageable
        )

        candidate = CandidateRaw(
            name=f"u/{author}",
            source_url=post_url or f"https://www.reddit.com/user/{author}",
            source_platform="reddit",
            raw_text=raw_text,
            scraped_at=datetime.now(timezone.utc),
            current_employer=current_employer,
            current_title=current_title,
            location="",
            skills=skills,
            email="",
        )
        candidate.recruiter_flag = _REDDIT_FLAG
        return candidate

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "RedditScraper":
        return self

    def __exit__(self, *_: Any) -> None:
        self._http.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _has_self_id_signal(text: str) -> bool:
    """Return True if *text* contains at least one self-identification pattern."""
    return any(pat.search(text) for pat in _SELF_ID_PATTERNS)


def _extract_employer(text: str) -> str:
    """Attempt to extract a current employer name from free text."""
    match = _EMPLOYER_EXTRACT_RE.search(text)
    if match:
        return match.group(1).strip().rstrip(".,;")
    return ""


def _extract_title(text: str) -> str:
    """Attempt to extract a job title from free text."""
    match = _TITLE_EXTRACT_RE.search(text)
    if match:
        return match.group(1).strip().rstrip(".,;")
    return ""


# Known clearance levels and IC/DoD domain tokens to surface as skills
_SKILL_TOKENS = re.compile(
    r"\b("
    r"ts[/ ]sci|top secret|secret clearance|public trust|"
    r"polygraph|full scope poly|lifestyle poly|counter[- ]intelligence poly|"
    r"python|java|c\+\+|golang|rust|kubernetes|docker|aws|azure|gcp|"
    r"sigint|humint|geoint|masint|osint|all[- ]source|"
    r"malware analysis|reverse engineering|penetration testing|red team|"
    r"vulnerability research|exploit development|"
    r"machine learning|ml|ai|nlp|data science|"
    r"network defense|soc analyst|incident response|"
    r"systems engineering|software engineering|cloud architect"
    r")\b",
    re.IGNORECASE,
)


def _extract_skills(text: str) -> list[str]:
    """Return deduplicated skill/clearance tokens found in *text*."""
    found = _SKILL_TOKENS.findall(text)
    # Normalise and deduplicate
    seen: set[str] = set()
    result: list[str] = []
    for token in found:
        normalised = token.lower().strip()
        if normalised not in seen:
            seen.add(normalised)
            result.append(token.strip())
    return result


def _merge_reddit_records(base: CandidateRaw, incoming: CandidateRaw) -> None:
    """
    Merge *incoming* Reddit record into *base* in-place.

    - raw_text: append new context (capped)
    - skills: union
    - current_employer / current_title: fill gaps only
    """
    if incoming.current_employer and not base.current_employer:
        base.current_employer = incoming.current_employer
    if incoming.current_title and not base.current_title:
        base.current_title = incoming.current_title

    # Union skills
    existing_skills = set(s.lower() for s in base.skills)
    for skill in incoming.skills:
        if skill.lower() not in existing_skills:
            base.skills.append(skill)
            existing_skills.add(skill.lower())

    # Append snippet of new raw_text (avoid unbounded growth)
    new_snippet = incoming.raw_text[incoming.raw_text.find("\n\n") + 2:]
    if new_snippet and new_snippet not in base.raw_text:
        base.raw_text = base.raw_text[:4000] + "\n\n---\n\n" + new_snippet[:1000]
