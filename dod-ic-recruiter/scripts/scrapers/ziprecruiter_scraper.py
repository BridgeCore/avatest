"""
ziprecruiter_scraper.py — ZipRecruiter public candidate listing scraper.

Scrapes ZipRecruiter's publicly accessible candidate/resume search pages
without requiring a login.  Uses httpx for HTTP and BeautifulSoup for
HTML parsing.

Public interface
----------------
    from scripts.scrapers.ziprecruiter_scraper import ZipRecruiterScraper

    scraper = ZipRecruiterScraper()
    candidates = scraper.search(["all-source analyst cleared", "GEOINT python"])

Notes
-----
- No API key or session cookie is required.
- ZipRecruiter's public candidate search renders server-side HTML.
  CSS selectors are isolated to ``_parse_candidate_card()`` for easy
  maintenance should the page structure change.
- Randomised delays and rotating user-agents reduce rate-limit exposure.
- 403 responses (hard block) are logged and skipped gracefully.
"""

from __future__ import annotations

from typing import List

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

_BASE_URL = "https://www.ziprecruiter.com/candidate/search"
_MAX_PAGES = 3
_RESULTS_PER_PAGE = 20  # ZipRecruiter typically shows 20 cards per page


class ZipRecruiterScraper(BaseScraper):
    """Scrapes ZipRecruiter public candidate search for candidate profiles."""

    source_platform = "ziprecruiter"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(self, queries: List[str]) -> List[CandidateRaw]:
        """
        Search ZipRecruiter candidate listings for each query.

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
            for page in range(1, _MAX_PAGES + 1):
                candidates = self._fetch_page(query, page)
                if not candidates:
                    break   # empty page — stop paginating this query

                for c in candidates:
                    if c.source_url not in seen_urls:
                        seen_urls.add(c.source_url)
                        results.append(c)

                random_delay(2.0, 5.5)

        return results

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _fetch_page(self, query: str, page: int) -> List[CandidateRaw]:
        """Fetch one page of ZipRecruiter candidate results."""
        params = {
            "search": query,
            "page": page,
        }
        resp = get_with_retry(_BASE_URL, params=params, headers=build_headers())
        if resp is None:
            return []

        return self._parse_page(resp.text)

    def _parse_page(self, html: str) -> List[CandidateRaw]:
        """Parse a ZipRecruiter candidate search results page."""
        soup = BeautifulSoup(html, "html.parser")
        candidates: List[CandidateRaw] = []

        # ZipRecruiter wraps each candidate in a article or div card element.
        # Multiple selector strategies for resilience against markup changes.
        cards = (
            soup.select("article[data-testid='candidate-card']")
            or soup.select("div[class*='CandidateCard']")
            or soup.select("div[class*='candidate_card']")
            or soup.select("li[class*='candidate']")
        )

        for card in cards:
            candidate = self._parse_candidate_card(card)
            if candidate is not None:
                candidates.append(candidate)

        return candidates

    def _parse_candidate_card(self, card) -> CandidateRaw | None:
        """
        Extract candidate fields from a single ZipRecruiter candidate card.

        Returns None if the card lacks a usable name.
        """
        # --- Name ---
        name_el = (
            card.select_one("a[data-testid='candidate-name']")
            or card.select_one("h2 a")
            or card.select_one("h3 a")
            or card.select_one("a[class*='name']")
            or card.select_one("span[class*='name']")
        )
        if name_el is None:
            return None

        name = name_el.get_text(strip=True)
        if not name:
            return None

        # --- Profile URL ---
        href = name_el.get("href", "") if name_el.name == "a" else ""
        if href.startswith("/"):
            source_url = "https://www.ziprecruiter.com" + href
        elif href.startswith("http"):
            source_url = href
        else:
            source_url = _BASE_URL

        # --- Headline / most recent title ---
        headline_el = (
            card.select_one("p[data-testid='candidate-headline']")
            or card.select_one("div[class*='headline']")
            or card.select_one("span[class*='title']")
            or card.select_one("p[class*='headline']")
        )
        headline = headline_el.get_text(strip=True) if headline_el else ""

        # --- Current employer ---
        employer_el = (
            card.select_one("span[data-testid='candidate-employer']")
            or card.select_one("span[class*='employer']")
            or card.select_one("span[class*='company']")
            or card.select_one("div[class*='employer']")
        )
        current_employer = employer_el.get_text(strip=True) if employer_el else ""

        # Attempt to split "Title at Employer" from headline if employer not found
        current_title = headline
        if not current_employer and " at " in headline:
            parts = headline.split(" at ", 1)
            current_title = parts[0].strip()
            current_employer = parts[1].strip()

        # --- Location ---
        loc_el = (
            card.select_one("span[data-testid='candidate-location']")
            or card.select_one("span[class*='location']")
            or card.select_one("div[class*='location']")
        )
        location = loc_el.get_text(strip=True) if loc_el else ""

        # --- Skills ---
        skills: List[str] = []
        skills_container = (
            card.select_one("ul[data-testid='candidate-skills']")
            or card.select_one("ul[class*='skills']")
            or card.select_one("div[class*='skills']")
        )
        if skills_container:
            skill_els = skills_container.select("li, span[class*='skill'], span[class*='tag']")
            skills = [s.get_text(strip=True) for s in skill_els if s.get_text(strip=True)]

        # --- Raw text ---
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
