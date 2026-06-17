"""
USAJOBS public API scraper — JD ENRICHMENT ONLY.

Queries the free USAJOBS REST API to extract skill and qualification language
from federal job postings.  Results are used to enrich the SkillPicture;
this module is never used for candidate sourcing.

Requires USAJOBS_EMAIL env var (or .env file) — the USAJOBS API requires a
registered email address in the User-Agent header.  Register at:
https://developer.usajobs.gov/

Usage:
    from scripts.scrapers.usajobs_scraper import enrich_from_postings
    result = await enrich_from_postings("Cyber Analyst", "SIGINT NSA")
    # or synchronously:
    import asyncio
    result = asyncio.run(enrich_from_postings("Cyber Analyst", "SIGINT NSA"))
"""

import asyncio
import json
import logging
import os
import random
import re
import time
from collections import Counter
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

API_BASE = "https://data.usajobs.gov/api/search"
DEFAULT_RESULTS_PER_PAGE = 25
MAX_PAGES = 2                    # cap at 50 postings total (25 × 2)
HIGH_CONFIDENCE_THRESHOLD = 3   # skill appearing in N+ postings → high confidence

# Retry / rate-limit handling
MAX_RETRIES = 3
RETRY_BASE_DELAY = 2.0           # seconds; doubles each retry

CERT_PATTERNS = re.compile(
    r"\b("
    r"CISSP|CISM|CISA|CEH|Security\+|Sec\+|Network\+|CySA\+|CASP\+|GCIA|GCIH|GPEN|GWAPT|OSCP|"
    r"PMP|CAPM|CSM|SAFe|"
    r"AWS Certified|Azure [A-Za-z]+|GCP [A-Za-z]+|"
    r"CompTIA [A-Za-z+]+|"
    r"CCNA|CCNP|CCIE|"
    r"A\+|Linux\+|Cloud\+|"
    r"ITIL|"
    r"DoD 8570|DoD 8140|IAT Level [I]+|IAM Level [I]+"
    r")\b",
    re.IGNORECASE,
)

SKILL_SIGNALS = (
    "experience", "knowledge", "proficiency", "familiar", "skill",
    "ability", "certif", "clearance", "degree", "bachelor", "master",
    "required", "preferred", "must have", "nice to have", "desired",
    "years", "yr ", "+yrs", "hands-on", "demonstrated", "working knowledge",
    "expertise", "understanding of",
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_user_agent() -> str:
    """
    USAJOBS requires 'Application Name/Version email@domain.com' format.
    Falls back to a generic value if USAJOBS_EMAIL is not set.
    """
    email = os.environ.get("USAJOBS_EMAIL", "user@example.com")
    return f"dod-ic-recruiter/1.0 {email}"


def _build_params(role_title: str, domain_context: str, page: int = 1) -> dict:
    keyword = f"{role_title} {domain_context}".strip()
    return {
        "Keyword": keyword,
        "ResultsPerPage": DEFAULT_RESULTS_PER_PAGE,
        "Page": page,
        "Fields": "Min",   # minimal field set reduces payload size
    }


def _extract_skill_phrases(text: str) -> list[str]:
    """
    Pull lines/sentences that look like skill/qualification statements.
    HTML tags are stripped before processing.
    """
    # Strip simple HTML tags
    clean = re.sub(r"<[^>]+>", " ", text)
    clean = re.sub(r"&[a-z]+;", " ", clean)
    skills: list[str] = []
    for line in clean.splitlines():
        line = line.strip()
        if len(line) < 12 or len(line) > 350:
            continue
        lower = line.lower()
        if any(sig in lower for sig in SKILL_SIGNALS):
            skills.append(line)
    return skills


def _extract_certs(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(0) for m in CERT_PATTERNS.finditer(text)))


def _parse_posting(item: dict) -> Optional[dict]:
    """
    Extract enrichment data from a single USAJOBS search result item.
    Returns None if the item is malformed.
    """
    try:
        matched = item.get("MatchedObjectDescriptor", {})
        position_title = matched.get("PositionTitle", "")
        org_name = matched.get("OrganizationName", "")
        dept_name = matched.get("DepartmentName", "")
        qualifications = matched.get("QualificationSummary", "") or ""
        user_area = matched.get("UserArea", {})
        details = user_area.get("Details", {}) if isinstance(user_area, dict) else {}
        requirements = details.get("Requirements", "") or ""
        low_grade = matched.get("PositionRemuneration", [{}])[0].get("MinimumRange", "")

        # Combine all text for phrase extraction
        combined_text = " ".join([
            position_title,
            qualifications,
            requirements,
        ])

        return {
            "title": position_title,
            "agency": dept_name or org_name,
            "qualifications": qualifications,
            "requirements": requirements,
            "skill_phrases": _extract_skill_phrases(combined_text),
            "certs": _extract_certs(combined_text),
        }
    except Exception as exc:
        logger.debug("Could not parse USAJOBS item: %s", exc)
        return None


async def _fetch_page(
    client: httpx.AsyncClient,
    role_title: str,
    domain_context: str,
    page: int,
) -> Optional[dict]:
    """
    Fetch one page of USAJOBS API results.  Handles rate-limit (429) and
    server errors with exponential back-off retries.  Returns the parsed JSON
    body or None on unrecoverable failure.
    """
    params = _build_params(role_title, domain_context, page)
    delay = RETRY_BASE_DELAY

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.debug(
                "USAJOBS API request | page=%d attempt=%d params=%s",
                page, attempt, params,
            )
            response = await client.get(API_BASE, params=params)

            if response.status_code == 200:
                return response.json()

            if response.status_code == 429:
                retry_after = float(response.headers.get("Retry-After", delay))
                logger.warning(
                    "USAJOBS rate-limited (429) — waiting %.1f seconds (attempt %d/%d)",
                    retry_after, attempt, MAX_RETRIES,
                )
                await asyncio.sleep(retry_after)
                delay *= 2
                continue

            if response.status_code in (403, 401):
                logger.error(
                    "USAJOBS API auth error %d — check USAJOBS_EMAIL env var",
                    response.status_code,
                )
                return None

            logger.warning(
                "USAJOBS API returned HTTP %d on page %d (attempt %d/%d)",
                response.status_code, page, attempt, MAX_RETRIES,
            )
            await asyncio.sleep(delay)
            delay *= 2

        except httpx.TimeoutException as exc:
            logger.warning(
                "USAJOBS request timed out (attempt %d/%d): %s", attempt, MAX_RETRIES, exc
            )
            await asyncio.sleep(delay)
            delay *= 2

        except httpx.RequestError as exc:
            logger.warning(
                "USAJOBS network error (attempt %d/%d): %s", attempt, MAX_RETRIES, exc
            )
            await asyncio.sleep(delay)
            delay *= 2

    logger.error("USAJOBS: all %d attempts failed for page %d", MAX_RETRIES, page)
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def enrich_from_postings(role_title: str, domain_context: str) -> dict:
    """
    Query the USAJOBS public API for postings matching role_title and
    domain_context.  Extracts skill language, agencies actively hiring,
    and formal qualification language for SkillPicture enrichment.

    Skills appearing in 3 or more postings are elevated to high confidence.

    This function is for JD ENRICHMENT ONLY and must never be used to source
    or identify individual candidates.

    Args:
        role_title:      e.g. "All-Source Analyst"
        domain_context:  e.g. "SIGINT NSA IC"

    Returns:
        {
            "skills_extracted":  list[dict],  # [{text: str, confidence: "high"|"standard"}]
            "agencies_found":    list[str],   # deduplicated agency names
            "postings_reviewed": int,
        }
    """
    email = os.environ.get("USAJOBS_EMAIL")
    if not email:
        logger.warning(
            "USAJOBS_EMAIL env var is not set. "
            "API requests will use a placeholder and may be rejected. "
            "Register at https://developer.usajobs.gov/"
        )

    headers = {
        "User-Agent": _get_user_agent(),
        "Host": "data.usajobs.gov",
        "Authorization": "",   # public endpoint; key not required but header expected
    }

    all_phrases: list[str] = []
    all_agencies: list[str] = []
    all_certs: list[str] = []
    postings_reviewed = 0

    async with httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(30.0, connect=10.0),
        follow_redirects=True,
    ) as client:
        for page_num in range(1, MAX_PAGES + 1):
            if page_num > 1:
                # Polite delay between pages
                await asyncio.sleep(random.uniform(1.5, 3.5))

            body = await _fetch_page(client, role_title, domain_context, page_num)
            if body is None:
                logger.warning("Stopping pagination at page %d due to API error", page_num)
                break

            search_result = body.get("SearchResult", {})
            items = search_result.get("SearchResultItems", [])

            if not items:
                logger.info("No more results at page %d — stopping pagination", page_num)
                break

            total_count = search_result.get("SearchResultCount", 0)
            logger.info(
                "USAJOBS page %d: %d items returned (total available: %s)",
                page_num, len(items), total_count,
            )

            for item in items:
                parsed = _parse_posting(item)
                if parsed is None:
                    continue

                postings_reviewed += 1
                all_phrases.extend(parsed["skill_phrases"])
                all_certs.extend(parsed["certs"])

                if parsed["agency"]:
                    all_agencies.append(parsed["agency"])

                logger.debug(
                    "Parsed posting %d: '%s' | agency=%r | skills=%d | certs=%d",
                    postings_reviewed,
                    parsed["title"],
                    parsed["agency"],
                    len(parsed["skill_phrases"]),
                    len(parsed["certs"]),
                )

    # ---------------------------------------------------------------------------
    # Confidence scoring: count occurrences of each phrase across all postings
    # ---------------------------------------------------------------------------
    phrase_counter: Counter = Counter()
    # Normalise for counting (lowercase, strip)
    for phrase in all_phrases:
        phrase_counter[phrase.strip().lower()] += 1

    # Map normalised → canonical (first-seen capitalisation)
    canonical_map: dict[str, str] = {}
    for phrase in all_phrases:
        key = phrase.strip().lower()
        if key not in canonical_map:
            canonical_map[key] = phrase.strip()

    skills_extracted = []
    seen_keys: set[str] = set()
    for phrase in all_phrases:
        key = phrase.strip().lower()
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        confidence = "high" if phrase_counter[key] >= HIGH_CONFIDENCE_THRESHOLD else "standard"
        skills_extracted.append({
            "text": canonical_map[key],
            "confidence": confidence,
        })

    # Sort: high confidence first, then alphabetical within each tier
    skills_extracted.sort(key=lambda s: (0 if s["confidence"] == "high" else 1, s["text"].lower()))

    # Deduplicate agencies (case-insensitive)
    agencies_seen: set[str] = set()
    agencies_found: list[str] = []
    for agency in all_agencies:
        key = agency.strip().lower()
        if key and key not in agencies_seen:
            agencies_seen.add(key)
            agencies_found.append(agency.strip())

    output = {
        "skills_extracted": skills_extracted,
        "agencies_found": agencies_found,
        "postings_reviewed": postings_reviewed,
    }

    high_count = sum(1 for s in skills_extracted if s["confidence"] == "high")
    logger.info(
        "USAJOBS enrichment complete | postings=%d skills=%d (high_confidence=%d) agencies=%d",
        postings_reviewed,
        len(skills_extracted),
        high_count,
        len(agencies_found),
    )
    return output


# ---------------------------------------------------------------------------
# Synchronous convenience wrapper
# ---------------------------------------------------------------------------

def enrich_from_postings_sync(role_title: str, domain_context: str) -> dict:
    """
    Synchronous wrapper around enrich_from_postings for callers that are not
    running inside an async event loop.
    """
    return asyncio.run(enrich_from_postings(role_title, domain_context))


# ---------------------------------------------------------------------------
# CLI entry point for ad-hoc testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    _role = sys.argv[1] if len(sys.argv) > 1 else "All-Source Analyst"
    _domain = sys.argv[2] if len(sys.argv) > 2 else "SIGINT IC DoD"

    result = asyncio.run(enrich_from_postings(_role, _domain))
    print(json.dumps(result, indent=2))
