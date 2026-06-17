"""
indeed_scraper.py — Indeed public resume/candidate listing scraper.

Scrapes Indeed's publicly accessible resume search pages without requiring
a login.  Uses httpx for HTTP and BeautifulSoup for HTML parsing.

Public interface
----------------
    from scripts.scrapers.indeed_scraper import IndeedScraper

    scraper = IndeedScraper()
    candidates = scraper.search(["intelligence analyst TS/SCI", "SIGINT python"])

Notes
-----
- No API key or session cookie is required.
- Indeed's public resume search renders server-side HTML; we parse it
  directly.  Structure may change; CSS selectors are isolated to
  ``_parse_resume_card()`` for easy maintenance.
- Randomised delays and rotating user-agents reduce rate-limit exposure.
- 403 responses (hard block) are logged and skipped gracefully.
"""

from __future__ import annotations

import re
from typing import List
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from scripts.scrapers.base import (
    BaseScraper,
    CandidateRaw,
    build_headers,
    get_with_retry,
    random_delay,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_BASE_URL = "https://www.indeed.com/resumes"
_MAX_PAGES = 3          # pages per query (10 results/page typical)
_RESULTS_PER_PAGE = 10  # used to calculate the ``start`` offset param


class IndeedScraper(BaseScraper):
    """Scrapes Indeed public resume search for candidate profiles."""

    source_platform = "indeed"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(self, queries: List[str]) -> List[CandidateRaw]:
        """
        Search Indeed resume listings for each query.

        Parameters
        ----------
        queries:
            List of plain-text search strings.

        Returns
        -------
        list[CandidateRaw]
            Aggregated results across all queries and pages.
        """
        results: List[CandidateRaw] = []
        seen_urls: set[str] = set()

        for query in queries:
            for page in range(_MAX_PAGES):
                start = page * _RESULTS_PER_PAGE
                candidates = self._fetch_page(query, start)
                if not candidates:
                    break   # empty page — stop paginating this query

                for c in candidates:
                    if c.source_url not in seen_urls:
                        seen_urls.add(c.source_url)
                        results.append(c)

                random_delay(2.0, 5.0)

        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_page(self, query: str, start: int) -> List[CandidateRaw]:
        """Fetch one page of Indeed resume results and return parsed candidates."""
        params = {
            "q": query,
            "start": start,
        }
        url = _BASE_URL
        resp = get_with_retry(url, params=params, headers=build_headers())
        if resp is None:
            return []

        return self._parse_page(resp.text, base_url=_BASE_URL)

    def _parse_page(self, html: str, base_url: str) -> List[CandidateRaw]:
        """Parse an Indeed resume search results page."""
        soup = BeautifulSoup(html, "html.parser")
        candidates: List[CandidateRaw] = []

        # Indeed wraps each resume card in a div with class containing "rezemp-ResumeCard"
        # or similar.  We try multiple selector strategies for resilience.
        cards = (
            soup.select("div[data-testid='resume-card']")
            or soup.select("div.rezemp-ResumeCard")
            or soup.select("div.icl-Card")
            or soup.select("div[class*='ResumeCard']")
        )

        for card in cards:
            candidate = self._parse_resume_card(card)
            if candidate is not None:
                candidates.append(candidate)

        return candidates

    def _parse_resume_card(self, card) -> CandidateRaw | None:
        """
        Extract candidate fields from a single Indeed resume card element.

        Returns None if the card lacks a usable name or URL.
        """
        # --- Name ---
        name_el = (
            card.select_one("a[data-testid='resume-name']")
            or card.select_one("a.icl-TextColor--primary")
            or card.select_one("a[href*='/resume/']")
            or card.select_one("h2 a")
        )
        if name_el is None:
            return None

        name = name_el.get_text(strip=True)
        if not name:
            return None

        # --- Profile URL ---
        href = name_el.get("href", "")
        if href.startswith("/"):
            source_url = "https://www.indeed.com" + href
        elif href.startswith("http"):
            source_url = href
        else:
            source_url = "https://www.indeed.com/resumes"

        # --- Headline / current title ---
        headline_el = (
            card.select_one("div[data-testid='resume-headline']")
            or card.select_one("span.rezemp-ResumeCard-title")
            or card.select_one("div.icl-u-xs-mt--xs span")
        )
        headline = headline_el.get_text(strip=True) if headline_el else ""

        # --- Most recent role (title + employer) ---
        role_el = (
            card.select_one("div[data-testid='most-recent-experience']")
            or card.select_one("div.rezemp-WorkExperienceDisplay")
            or card.select_one("div[class*='WorkExperience']")
        )
        current_title = ""
        current_employer = ""
        if role_el:
            title_el = role_el.select_one("span[class*='title'], b, strong")
            employer_el = role_el.select_one("span[class*='company'], span[class*='employer']")
            current_title = title_el.get_text(strip=True) if title_el else ""
            current_employer = employer_el.get_text(strip=True) if employer_el else ""

        # Fall back: headline often contains "Title at Employer"
        if not current_title and headline:
            current_title = headline.split(" at ")[0].strip() if " at " in headline else headline
        if not current_employer and " at " in headline:
            current_employer = headline.split(" at ", 1)[1].strip()

        # --- Location ---
        loc_el = (
            card.select_one("span[data-testid='candidate-location']")
            or card.select_one("span.rezemp-ResumeCard-location")
            or card.select_one("span[class*='location']")
        )
        location = loc_el.get_text(strip=True) if loc_el else ""

        # --- Skills ---
        skills: List[str] = []
        skills_container = (
            card.select_one("div[data-testid='resume-skills']")
            or card.select_one("ul.rezemp-ResumeCard-skills")
            or card.select_one("ul[class*='skills']")
        )
        if skills_container:
            skill_els = skills_container.select("li, span[class*='skill']")
            skills = [s.get_text(strip=True) for s in skill_els if s.get_text(strip=True)]

        # --- Raw text (full card text for downstream NLP) ---
        raw_text = card.get_text(separator=" ", strip=True)

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
