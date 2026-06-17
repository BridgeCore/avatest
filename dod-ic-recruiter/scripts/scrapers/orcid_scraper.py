"""
orcid_scraper.py — ORCID public API scraper for dod-ic-recruiter.

Uses the ORCID public API (no authentication required) to surface R&D,
scientific, and academic-adjacent candidates relevant to IC roles at
organisations such as MITRE, APL, Sandia, RAND, and the national labs.

Public API base: https://pub.orcid.org/v3.0/search/
Docs: https://info.orcid.org/documentation/api-tutorials/api-tutorial-searching-the-orcid-registry/

Usage
-----
    from scripts.scrapers.orcid_scraper import OrcidScraper

    scraper = OrcidScraper()
    candidates = scraper.search([
        "signals intelligence national lab",
        "cyber security MITRE clearance",
    ])
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

import httpx

from scripts.deduplicator import CandidateRaw

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# BaseScraper interface — shared by all scrapers in this package
# ---------------------------------------------------------------------------

class BaseScraper(ABC):
    """Abstract base class that all dod-ic-recruiter scrapers must implement."""

    @abstractmethod
    def search(self, queries: list[str]) -> list[CandidateRaw]:
        """
        Execute one or more search queries and return deduplicated CandidateRaw
        records.

        Parameters
        ----------
        queries:
            List of free-text query strings.  Semantics are scraper-specific
            (keyword search, boolean operators, site: scope, etc.).

        Returns
        -------
        List of CandidateRaw instances.  Callers are responsible for further
        deduplication across scrapers via scripts.deduplicator.deduplicate().
        """


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ORCID_SEARCH_URL = "https://pub.orcid.org/v3.0/search/"
_ORCID_RECORD_URL = "https://pub.orcid.org/v3.0/{orcid_id}"

# ORCID returns max 200 results per page; we cap at 50 to be polite
_PAGE_SIZE = 50

# Seconds to sleep between consecutive HTTP calls (rate-limit courtesy)
_REQUEST_DELAY = 0.5

# Academic/national-lab employer keywords used when building affiliation context
_RELEVANT_EMPLOYERS = {
    "mitre", "jhu apl", "johns hopkins", "applied physics laboratory",
    "sandia", "rand", "los alamos", "oak ridge", "argonne", "llnl",
    "lawrence livermore", "pacific northwest national", "pnnl",
    "institute for defense analyses", "ida", "cna", "leidos",
    "saic", "bah", "booz allen", "miter", "bae systems",
    "lincoln laboratory", "mit lincoln", "naval research laboratory", "nrl",
    "army research laboratory", "arl", "afrl", "disa", "nsa",
    "aerospace corporation",
}

# Headers required by ORCID public API
_HEADERS = {
    "Accept": "application/json",
    "User-Agent": "dod-ic-recruiter/1.0 (recruiting tool; contact: recruiting@bcore.com)",
}


# ---------------------------------------------------------------------------
# OrcidScraper
# ---------------------------------------------------------------------------

class OrcidScraper(BaseScraper):
    """
    Scrapes the ORCID public registry for candidates matching keyword queries.

    No authentication is required.  The ORCID public API allows anonymous
    access to public researcher profiles.

    Extracted fields (mapped to CandidateRaw):
      - name                  -> CandidateRaw.name
      - current employer      -> CandidateRaw.current_employer
      - current title         -> CandidateRaw.current_title
      - employment history    -> incorporated into CandidateRaw.raw_text
      - education             -> incorporated into CandidateRaw.raw_text
      - publication topics    -> CandidateRaw.skills (keyword tags)
      - location              -> CandidateRaw.location
      - ORCID profile URL     -> CandidateRaw.source_url
    """

    def __init__(
        self,
        page_size: int = _PAGE_SIZE,
        request_delay: float = _REQUEST_DELAY,
        timeout: float = 20.0,
    ) -> None:
        self._page_size = page_size
        self._request_delay = request_delay
        self._client = httpx.Client(
            headers=_HEADERS,
            timeout=timeout,
            follow_redirects=True,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def search(self, queries: list[str]) -> list[CandidateRaw]:
        """
        Search ORCID for each query string and return CandidateRaw records.

        Duplicate ORCID IDs encountered across queries are collapsed so each
        researcher appears only once in the result list.

        Parameters
        ----------
        queries:
            List of keyword / affiliation strings.

        Returns
        -------
        List of CandidateRaw records with source_platform="orcid".
        """
        seen_orcid_ids: set[str] = set()
        results: list[CandidateRaw] = []

        for query in queries:
            logger.info("OrcidScraper: searching for %r", query)
            orcid_ids = self._run_query(query)
            new_ids = [oid for oid in orcid_ids if oid not in seen_orcid_ids]
            seen_orcid_ids.update(new_ids)

            for orcid_id in new_ids:
                candidate = self._fetch_record(orcid_id)
                if candidate is not None:
                    results.append(candidate)
                time.sleep(self._request_delay)

        logger.info("OrcidScraper: returning %d candidates.", len(results))
        return results

    # ------------------------------------------------------------------
    # Search query
    # ------------------------------------------------------------------

    def _run_query(self, query: str) -> list[str]:
        """
        Execute a single search query against the ORCID public API.

        Returns a list of ORCID IDs (strings of the form XXXX-XXXX-XXXX-XXXX).
        """
        params = {
            "q": query,
            "rows": self._page_size,
            "start": 0,
        }
        try:
            resp = self._client.get(_ORCID_SEARCH_URL, params=params)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning("OrcidScraper: search request failed for %r: %s", query, exc)
            return []

        data = resp.json()
        results_block = data.get("result") or []
        orcid_ids: list[str] = []
        for item in results_block:
            orcid_id = (
                item.get("orcid-identifier", {}).get("path", "").strip()
            )
            if orcid_id:
                orcid_ids.append(orcid_id)

        logger.debug(
            "OrcidScraper: query %r returned %d ORCID IDs.", query, len(orcid_ids)
        )
        return orcid_ids

    # ------------------------------------------------------------------
    # Record fetch
    # ------------------------------------------------------------------

    def _fetch_record(self, orcid_id: str) -> CandidateRaw | None:
        """
        Fetch a full ORCID record and return a CandidateRaw or None on error.
        """
        url = _ORCID_RECORD_URL.format(orcid_id=orcid_id)
        try:
            resp = self._client.get(url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning(
                "OrcidScraper: failed to fetch record %s: %s", orcid_id, exc
            )
            return None

        record = resp.json()
        return self._parse_record(orcid_id, url, record)

    # ------------------------------------------------------------------
    # Record parsing
    # ------------------------------------------------------------------

    def _parse_record(
        self,
        orcid_id: str,
        profile_url: str,
        record: dict[str, Any],
    ) -> CandidateRaw | None:
        """
        Parse a raw ORCID record dict into a CandidateRaw.

        Returns None if no usable name can be extracted.
        """
        # --- Name -----------------------------------------------------------
        person_block = record.get("person") or {}
        name_block = person_block.get("name") or {}

        given = (name_block.get("given-names") or {}).get("value", "").strip()
        family = (name_block.get("family-name") or {}).get("value", "").strip()
        full_name = f"{given} {family}".strip()
        if not full_name:
            logger.debug("OrcidScraper: skipping %s — no name in record.", orcid_id)
            return None

        # --- Employment history ---------------------------------------------
        activities = record.get("activities-summary") or {}
        employments_block = activities.get("employments") or {}
        employment_items = _collect_summary_items(employments_block)

        current_employer = ""
        current_title = ""
        location = ""
        employment_lines: list[str] = []

        for item in employment_items:
            org = (item.get("organization") or {})
            org_name = org.get("name", "").strip()
            dept = (item.get("department-name") or "").strip()
            role = (item.get("role-title") or "").strip()
            start_year = _extract_year(item.get("start-date"))
            end_year = _extract_year(item.get("end-date"))
            end_label = end_year if end_year else "present"

            org_address = org.get("address") or {}
            city = org_address.get("city", "").strip()
            state = org_address.get("region", "").strip()
            country = (org_address.get("country") or {}).get("value", "").strip()

            if city or state:
                loc_parts = [p for p in [city, state, country] if p]
                location = location or ", ".join(loc_parts)

            line = f"{role or 'Employee'} at {org_name}"
            if dept:
                line += f" ({dept})"
            if start_year:
                line += f" [{start_year}–{end_label}]"
            employment_lines.append(line)

            # Use the most recent / current position as primary
            if not end_year and not current_employer:
                current_employer = org_name
                current_title = role

        if not current_employer and employment_lines:
            # Fallback: use the first listed employer
            first_item = employment_items[0]
            current_employer = (first_item.get("organization") or {}).get("name", "")
            current_title = (first_item.get("role-title") or "").strip()

        # --- Education ------------------------------------------------------
        educations_block = activities.get("educations") or {}
        education_items = _collect_summary_items(educations_block)
        education_lines: list[str] = []
        for item in education_items:
            org_name = (item.get("organization") or {}).get("name", "").strip()
            role = (item.get("role-title") or "").strip()  # degree type
            dept = (item.get("department-name") or "").strip()
            start_year = _extract_year(item.get("start-date"))
            end_year = _extract_year(item.get("end-date"))
            line = f"{role or 'Degree'} from {org_name}"
            if dept:
                line += f", {dept}"
            if start_year or end_year:
                line += f" [{start_year or '?'}–{end_year or 'present'}]"
            education_lines.append(line)

        # --- Publication keywords -> skills ---------------------------------
        works_block = activities.get("works") or {}
        work_items = works_block.get("group") or []
        pub_keywords: list[str] = []
        pub_titles: list[str] = []
        for group in work_items:
            for work_summary in group.get("work-summary") or []:
                title_val = (
                    (work_summary.get("title") or {})
                    .get("title", {})
                    .get("value", "")
                    .strip()
                )
                if title_val:
                    pub_titles.append(title_val)
                for kw_block in (work_summary.get("keywords") or {}).get("keyword") or []:
                    kw = (kw_block.get("content") or "").strip()
                    if kw and kw not in pub_keywords:
                        pub_keywords.append(kw)

        # --- Assemble raw_text ----------------------------------------------
        raw_sections: list[str] = []
        if employment_lines:
            raw_sections.append("EMPLOYMENT:\n" + "\n".join(f"  - {l}" for l in employment_lines))
        if education_lines:
            raw_sections.append("EDUCATION:\n" + "\n".join(f"  - {l}" for l in education_lines))
        if pub_titles:
            raw_sections.append(
                "SELECTED PUBLICATIONS:\n"
                + "\n".join(f"  - {t}" for t in pub_titles[:10])
            )
        if pub_keywords:
            raw_sections.append("PUBLICATION KEYWORDS: " + ", ".join(pub_keywords))

        raw_text = f"ORCID: {orcid_id}\nName: {full_name}\n\n" + "\n\n".join(raw_sections)

        return CandidateRaw(
            name=full_name,
            source_url=profile_url,
            source_platform="orcid",
            raw_text=raw_text,
            scraped_at=datetime.now(timezone.utc),
            current_employer=current_employer,
            current_title=current_title,
            location=location,
            skills=pub_keywords,
            email="",
        )

    # ------------------------------------------------------------------
    # Context manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> "OrcidScraper":
        return self

    def __exit__(self, *_: Any) -> None:
        self._client.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _collect_summary_items(block: dict[str, Any]) -> list[dict[str, Any]]:
    """
    ORCID employment/education blocks nest items inside
    'affiliation-group' -> 'summaries' -> [{'employment-summary': ...}] or
    'affiliation-group' -> 'summaries' -> [{'education-summary': ...}].

    This helper flattens all of those into a plain list of summary dicts.
    """
    items: list[dict[str, Any]] = []
    for group in block.get("affiliation-group") or []:
        for summary_wrapper in group.get("summaries") or []:
            for key in ("employment-summary", "education-summary", "distinction-summary",
                        "membership-summary", "service-summary", "invited-position-summary",
                        "qualification-summary"):
                item = summary_wrapper.get(key)
                if item:
                    items.append(item)
    return items


def _extract_year(date_block: dict[str, Any] | None) -> str:
    """Return the year string from an ORCID date block, or empty string."""
    if not date_block:
        return ""
    year_block = date_block.get("year") or {}
    return (year_block.get("value") or "").strip()
