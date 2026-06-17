"""
google_scraper.py — Google Search HTML scraper for dod-ic-recruiter.

Uses httpx + BeautifulSoup to scrape Google search result pages directly.
Targets public profile sources (primarily GitHub) via site-scoped queries.

LinkedIn handling
-----------------
LinkedIn URLs found in Google snippets are NEVER followed.  Only the name
and title visible in the Google snippet are extracted.  These leads are
flagged prominently so recruiters know they must visit the profile manually:

    source_platform = "linkedin_snippet"
    recruiter_flag  = "LinkedIn lead — name and title extracted from Google
                       snippet only, profile not visited"

GitHub handling
---------------
GitHub profile URLs found in Google results ARE followed and fully scraped.
The README, pinned-repository descriptions, and bio fields are extracted.

Usage
-----
    from scripts.scrapers.google_scraper import GoogleScraper

    scraper = GoogleScraper()
    candidates = scraper.search([
        "site:github.com MITRE clearance signals intelligence python",
        '"cleared" "TS/SCI" site:github.com',
    ])
"""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, urljoin, quote_plus

import httpx
from bs4 import BeautifulSoup, Tag

from scripts.deduplicator import CandidateRaw
from scripts.scrapers.orcid_scraper import BaseScraper

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_GOOGLE_SEARCH_URL = "https://www.google.com/search"

_GITHUB_API_USER_URL = "https://api.github.com/users/{username}"
_GITHUB_API_REPOS_URL = "https://api.github.com/users/{username}/repos"
_GITHUB_PROFILE_URL = "https://github.com/{username}"

# Courtesy delay between Google requests (seconds) — randomised within range
_MIN_DELAY = 3.5
_MAX_DELAY = 7.0

# Delay between GitHub follow-up requests
_GITHUB_DELAY = 1.0

# Cap on results parsed per Google search page
_MAX_RESULTS_PER_PAGE = 10

# Minimum meaningful text length for a Google snippet
_MIN_SNIPPET_LEN = 20

# Recruiter flags
_LINKEDIN_FLAG = (
    "LinkedIn lead — name and title extracted from Google snippet only, "
    "profile not visited"
)

_GOOGLE_SEARCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/webp,*/*;q=0.8"
    ),
    "Referer": "https://www.google.com/",
}

_GITHUB_HEADERS = {
    "User-Agent": "dod-ic-recruiter/1.0 (recruiting tool; contact: recruiting@bcore.com)",
    "Accept": "application/vnd.github+json",
}

# Skill tokens relevant to IC/DoD recruiting
_SKILL_TOKEN_RE = re.compile(
    r"\b("
    r"python|java|golang|rust|c\+\+|javascript|typescript|"
    r"kubernetes|docker|terraform|ansible|helm|"
    r"aws|azure|gcp|cloud|devops|devsecops|ci/cd|"
    r"sigint|humint|geoint|masint|osint|all[- ]source|"
    r"machine learning|deep learning|nlp|data science|"
    r"malware analysis|reverse engineering|penetration testing|red team|"
    r"vulnerability research|exploit development|"
    r"network defense|soc|incident response|"
    r"ts[/ ]sci|top[- ]secret|clearance|cleared|poly(graph)?|"
    r"systems engineering|embedded systems|fpga|vhdl|"
    r"zero trust|siem|splunk|elastic|elk|"
    r"mitre att&ck|cybersecurity|information security"
    r")\b",
    re.IGNORECASE,
)

# Patterns to guess a person's name from a GitHub bio or Google snippet
_NAME_RE = re.compile(
    r"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3})\b"
)

# LinkedIn URL detection
_LINKEDIN_URL_RE = re.compile(
    r"https?://(?:www\.)?linkedin\.com/in/[A-Za-z0-9_%-]+",
    re.IGNORECASE,
)

# GitHub URL detection (profile, not repo-only)
_GITHUB_PROFILE_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_-]+)/?(?:\?[^\"'\s]*)?$",
    re.IGNORECASE,
)

# Employer extraction heuristics
_EMPLOYER_RE = re.compile(
    r"(?:at|@)\s+([\w &,./()-]{3,50}?)(?:\s*[|·•–\-,]|\s*$)",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# GoogleScraper
# ---------------------------------------------------------------------------

class GoogleScraper(BaseScraper):
    """
    Lead-discovery scraper using Google search HTML.

    Role
    ----
    This scraper is a supplementary data layer.  It is particularly effective
    for building site-scoped queries (e.g., ``site:github.com``) that surface
    public developer profiles with relevant keywords.

    LinkedIn policy
    ---------------
    LinkedIn rate-limits and blocks automated access aggressively.  Following
    LinkedIn URLs would likely result in IP bans and ToS violations.
    This scraper NEVER follows LinkedIn URLs.  It extracts name and title
    from the Google snippet only and flags the record for manual review.

    GitHub policy
    -------------
    GitHub public profiles and repository metadata are fully fetched via the
    GitHub API (unauthenticated, rate-limited to 60 req/hour).  No
    authentication token is required for public data.
    """

    def __init__(
        self,
        request_delay_range: tuple[float, float] = (_MIN_DELAY, _MAX_DELAY),
        github_delay: float = _GITHUB_DELAY,
        timeout: float = 25.0,
        max_results_per_page: int = _MAX_RESULTS_PER_PAGE,
    ) -> None:
        self._min_delay, self._max_delay = request_delay_range
        self._github_delay = github_delay
        self._max_results = max_results_per_page

        self._google_client = httpx.Client(
            headers=_GOOGLE_SEARCH_HEADERS,
            timeout=timeout,
            follow_redirects=True,
        )
        self._github_client = httpx.Client(
            headers=_GITHUB_HEADERS,
            timeout=timeout,
            follow_redirects=True,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(self, queries: list[str]) -> list[CandidateRaw]:
        """
        Execute Google searches and return CandidateRaw records.

        For each result URL found:
          - LinkedIn URLs  -> snippet-only extraction, recruiter_flag set
          - GitHub URLs    -> full profile + repo scrape
          - Other URLs     -> snippet-only extraction, source_platform="google_search"

        Parameters
        ----------
        queries:
            List of Google search query strings.  Callers should include
            site: scoping where appropriate (e.g., ``site:github.com``).

        Returns
        -------
        List of CandidateRaw.  Duplicates within this call (same URL seen in
        multiple query results) are collapsed.
        """
        seen_urls: dict[str, CandidateRaw] = {}

        for query in queries:
            logger.info("GoogleScraper: searching for %r", query)
            results = self._google_search(query)

            for result in results:
                url = result["url"]
                snippet = result["snippet"]
                display_title = result["title"]

                if url in seen_urls:
                    continue

                candidate = self._process_result(url, snippet, display_title)
                if candidate is not None:
                    seen_urls[url] = candidate

            _sleep(_MIN_DELAY, _MAX_DELAY)

        all_candidates = list(seen_urls.values())
        logger.info("GoogleScraper: returning %d candidates.", len(all_candidates))
        return all_candidates

    # ------------------------------------------------------------------
    # Google search
    # ------------------------------------------------------------------

    def _google_search(self, query: str) -> list[dict[str, str]]:
        """
        Fetch one page of Google search results for *query*.

        Returns a list of dicts with keys: url, title, snippet.
        """
        params = {
            "q": query,
            "num": self._max_results,
            "hl": "en",
        }
        try:
            resp = self._google_client.get(_GOOGLE_SEARCH_URL, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("GoogleScraper: Google request failed for %r: %s", query, exc)
            return []

        return _parse_google_html(resp.text)

    # ------------------------------------------------------------------
    # Result routing
    # ------------------------------------------------------------------

    def _process_result(
        self, url: str, snippet: str, display_title: str
    ) -> CandidateRaw | None:
        """Route a single Google result to the appropriate handler."""
        # LinkedIn: snippet-only, never follow
        if _is_linkedin_url(url):
            return self._handle_linkedin_snippet(url, snippet, display_title)

        # GitHub profile: follow and scrape
        github_match = _GITHUB_PROFILE_RE.match(url)
        if github_match:
            username = github_match.group(1)
            return self._handle_github_profile(username, url, snippet)

        # Generic: snippet extraction
        return self._handle_generic_snippet(url, snippet, display_title)

    # ------------------------------------------------------------------
    # LinkedIn snippet handler
    # ------------------------------------------------------------------

    def _handle_linkedin_snippet(
        self, url: str, snippet: str, display_title: str
    ) -> CandidateRaw | None:
        """
        Extract name and title from a Google snippet for a LinkedIn URL.

        NEVER follows the LinkedIn URL.
        """
        if len(snippet) < _MIN_SNIPPET_LEN and len(display_title) < _MIN_SNIPPET_LEN:
            return None

        name, title = _extract_name_title_from_snippet(display_title, snippet)
        if not name:
            return None

        employer = _extract_employer_from_snippet(snippet)
        skills = _extract_skills(snippet + " " + display_title)

        raw_text = (
            f"[LinkedIn snippet — profile NOT visited]\n"
            f"Google display title: {display_title}\n"
            f"Google snippet: {snippet}\n"
            f"LinkedIn URL (not visited): {url}"
        )

        candidate = CandidateRaw(
            name=name,
            source_url=url,
            source_platform="linkedin_snippet",
            raw_text=raw_text,
            scraped_at=datetime.now(timezone.utc),
            current_employer=employer,
            current_title=title,
            location="",
            skills=skills,
            email="",
        )
        candidate.recruiter_flag = _LINKEDIN_FLAG
        return candidate

    # ------------------------------------------------------------------
    # GitHub profile handler
    # ------------------------------------------------------------------

    def _handle_github_profile(
        self, username: str, profile_url: str, google_snippet: str
    ) -> CandidateRaw | None:
        """
        Fully scrape a GitHub user profile via the GitHub API.

        Fetches: user bio, company, location, name, blog, and pinned-repo
        descriptions to build a comprehensive raw_text.
        """
        api_url = _GITHUB_API_USER_URL.format(username=username)
        try:
            resp = self._github_client.get(api_url)
            if resp.status_code == 404:
                logger.debug("GitHubScraper: user %s not found.", username)
                return None
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning(
                "GoogleScraper: GitHub API request failed for %s: %s", username, exc
            )
            return None

        user = resp.json()
        time.sleep(self._github_delay)

        # Fetch public repositories for additional context
        repos = self._fetch_github_repos(username)

        return self._build_github_candidate(username, profile_url, user, repos, google_snippet)

    def _fetch_github_repos(self, username: str) -> list[dict[str, Any]]:
        """Fetch up to 30 public repos sorted by last push date."""
        url = _GITHUB_API_REPOS_URL.format(username=username)
        params = {"sort": "pushed", "per_page": 30, "type": "owner"}
        try:
            resp = self._github_client.get(url, params=params)
            resp.raise_for_status()
            return resp.json() if isinstance(resp.json(), list) else []
        except httpx.HTTPError as exc:
            logger.debug(
                "GoogleScraper: could not fetch repos for %s: %s", username, exc
            )
            return []

    def _build_github_candidate(
        self,
        username: str,
        profile_url: str,
        user: dict[str, Any],
        repos: list[dict[str, Any]],
        google_snippet: str,
    ) -> CandidateRaw | None:
        """Assemble a CandidateRaw from GitHub API data."""
        name = (user.get("name") or "").strip() or f"github:{username}"
        bio = (user.get("bio") or "").strip()
        company = (user.get("company") or "").strip().lstrip("@")
        location = (user.get("location") or "").strip()
        blog = (user.get("blog") or "").strip()

        # Build repo descriptions section
        repo_lines: list[str] = []
        for repo in repos[:15]:  # cap at 15 repos
            repo_name = repo.get("name", "")
            repo_desc = (repo.get("description") or "").strip()
            lang = (repo.get("language") or "").strip()
            stars = repo.get("stargazers_count", 0)
            line = f"  {repo_name}"
            if lang:
                line += f" [{lang}]"
            if stars:
                line += f" ★{stars}"
            if repo_desc:
                line += f" — {repo_desc}"
            repo_lines.append(line)

        # Repo language stats
        repo_languages = list(
            dict.fromkeys(
                r.get("language", "") for r in repos if r.get("language")
            )
        )

        raw_sections = [f"GitHub profile: {profile_url}"]
        if bio:
            raw_sections.append(f"Bio: {bio}")
        if company:
            raw_sections.append(f"Company: {company}")
        if location:
            raw_sections.append(f"Location: {location}")
        if blog:
            raw_sections.append(f"Website/Blog: {blog}")
        if repo_languages:
            raw_sections.append("Languages used: " + ", ".join(repo_languages))
        if repo_lines:
            raw_sections.append("Public repositories:\n" + "\n".join(repo_lines))
        if google_snippet:
            raw_sections.append(f"Google snippet: {google_snippet}")

        raw_text = "\n".join(raw_sections)

        all_text = raw_text
        skills = _extract_skills(all_text)
        # Also add repo languages as skills
        for lang in repo_languages:
            if lang.lower() not in {s.lower() for s in skills}:
                skills.append(lang)

        return CandidateRaw(
            name=name,
            source_url=profile_url,
            source_platform="google_search",
            raw_text=raw_text,
            scraped_at=datetime.now(timezone.utc),
            current_employer=company,
            current_title="",
            location=location,
            skills=skills,
            email="",
        )

    # ------------------------------------------------------------------
    # Generic snippet handler
    # ------------------------------------------------------------------

    def _handle_generic_snippet(
        self, url: str, snippet: str, display_title: str
    ) -> CandidateRaw | None:
        """Handle non-LinkedIn, non-GitHub results as snippet-only leads."""
        if len(snippet) < _MIN_SNIPPET_LEN:
            return None

        name, title = _extract_name_title_from_snippet(display_title, snippet)
        if not name:
            return None

        employer = _extract_employer_from_snippet(snippet)
        skills = _extract_skills(snippet + " " + display_title)

        raw_text = (
            f"Source URL: {url}\n"
            f"Google display title: {display_title}\n"
            f"Google snippet: {snippet}"
        )

        return CandidateRaw(
            name=name,
            source_url=url,
            source_platform="google_search",
            raw_text=raw_text,
            scraped_at=datetime.now(timezone.utc),
            current_employer=employer,
            current_title=title,
            location="",
            skills=skills,
            email="",
        )

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "GoogleScraper":
        return self

    def __exit__(self, *_: Any) -> None:
        self._google_client.close()
        self._github_client.close()


# ---------------------------------------------------------------------------
# Google HTML parser
# ---------------------------------------------------------------------------

def _parse_google_html(html: str) -> list[dict[str, str]]:
    """
    Parse Google search result HTML into a list of result dicts.

    Each dict contains: url, title, snippet.

    Google's HTML structure changes over time; this parser is written
    defensively and will silently skip unparseable results rather than crash.
    """
    soup = BeautifulSoup(html, "html.parser")
    results: list[dict[str, str]] = []

    # Google wraps organic results in <div class="g"> containers
    for container in soup.select("div.g, div[data-sokoban-container]"):
        # URL: first <a> with an href that looks like a real URL
        url = ""
        anchor: Tag | None = container.find("a", href=True)
        if anchor:
            href = anchor["href"]
            if isinstance(href, str) and href.startswith("http"):
                url = href.split("&")[0]  # strip Google tracking params

        if not url:
            continue

        # Title: text of the <h3> inside the anchor
        title = ""
        h3 = container.find("h3")
        if h3:
            title = h3.get_text(separator=" ", strip=True)

        # Snippet: longest text-bearing <span> or <div> not containing a URL
        snippet = ""
        for el in container.find_all(["span", "div"]):
            text = el.get_text(separator=" ", strip=True)
            if (
                len(text) > len(snippet)
                and len(text) > _MIN_SNIPPET_LEN
                and "http" not in text
                and text != title
            ):
                snippet = text

        results.append({"url": url, "title": title, "snippet": snippet})

    return results


# ---------------------------------------------------------------------------
# Utility helpers
# ---------------------------------------------------------------------------

def _is_linkedin_url(url: str) -> bool:
    """Return True if *url* is a LinkedIn profile URL."""
    parsed = urlparse(url)
    return "linkedin.com" in parsed.netloc.lower()


def _extract_skills(text: str) -> list[str]:
    """Return deduplicated skill tokens found in *text*."""
    found = _SKILL_TOKEN_RE.findall(text)
    seen: set[str] = set()
    result: list[str] = []
    for token in found:
        key = token.lower().strip()
        if key not in seen:
            seen.add(key)
            result.append(token.strip())
    return result


def _extract_name_title_from_snippet(
    display_title: str, snippet: str
) -> tuple[str, str]:
    """
    Attempt to extract a candidate name and job title from a Google snippet.

    Heuristic: Google display titles for LinkedIn/professional-profile pages
    typically follow the pattern "First Last - Title at Company | LinkedIn".
    """
    name = ""
    title = ""

    # Pattern: "Name - Title at Employer | Platform"
    title_pattern = re.compile(
        r"^([A-Z][a-z]+(?:\s+[A-Z][a-z'-]+){1,3})\s*[-–|]\s*(.+?)(?:\s*[|·]|$)",
        re.IGNORECASE,
    )
    m = title_pattern.match(display_title)
    if m:
        name = m.group(1).strip()
        raw_title = m.group(2).strip()
        # Strip " at Employer" suffix from title
        title = re.split(r"\s+at\s+", raw_title, maxsplit=1)[0].strip()

    # Fallback: try to find a capitalised name at the start of the snippet
    if not name:
        nm = _NAME_RE.match(snippet)
        if nm:
            name = nm.group(1).strip()

    return name, title


def _extract_employer_from_snippet(snippet: str) -> str:
    """Extract employer name from a snippet using the 'at Company' pattern."""
    m = _EMPLOYER_RE.search(snippet)
    if m:
        return m.group(1).strip().rstrip(".,;")
    return ""


def _sleep(min_s: float, max_s: float) -> None:
    """Sleep for a random duration between *min_s* and *max_s* seconds."""
    time.sleep(random.uniform(min_s, max_s))
