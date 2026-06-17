"""
ClearanceJobs.com scraper — JD ENRICHMENT ONLY.

Extracts skill and qualification language from public ClearanceJobs job postings
to enrich the SkillPicture. This module is never used for candidate sourcing.

Usage:
    from scripts.scrapers.clearancejobs_scraper import enrich_from_postings
    result = await enrich_from_postings("Cyber Analyst", "SIGINT NSA")
"""

import asyncio
import logging
import random
import re
from typing import Optional
from urllib.parse import urlencode, urljoin

from playwright.async_api import async_playwright, Page, TimeoutError as PlaywrightTimeout

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BASE_URL = "https://www.clearancejobs.com"
SEARCH_PATH = "/jobs"

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]

# Selectors — ClearanceJobs renders with React; these target stable data attributes
# where possible.  Update if the site restructures.
SELECTORS = {
    "job_links": "a[href*='/jobs/']",
    "job_title": "h1[data-cy='job-title'], h1.job-title, h1",
    "job_description": "[data-cy='job-description'], .job-description, .description, article",
    "skills_section": "[data-cy='skills'], .skills, .requirements",
    "clearance_badge": "[data-cy='clearance-level'], .clearance-level, [class*='clearance']",
    "company_name": "[data-cy='company-name'], .company-name, [class*='company']",
}

# Known clearance level tokens (used for extraction even without structured markup)
CLEARANCE_KEYWORDS = [
    "TS/SCI", "TS/SCI with Full Scope Polygraph", "TS/SCI with Polygraph",
    "TS/SCI FSP", "Top Secret/SCI", "Top Secret", "TS",
    "Secret", "Secret clearance", "DoD Secret",
    "Confidential", "Public Trust", "Tier 1", "Tier 2", "Tier 3", "Tier 5",
    "SCI eligible", "CI Polygraph", "Full Scope Poly", "Lifestyle Poly",
]

# Common certification tokens
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


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _random_ua() -> str:
    return random.choice(USER_AGENTS)


async def _random_delay() -> None:
    delay = random.uniform(1.5, 4.0)
    logger.debug("Sleeping %.2f seconds between requests", delay)
    await asyncio.sleep(delay)


def _build_search_url(role_title: str, domain_context: str) -> str:
    params = {
        "q": f"{role_title} {domain_context}".strip(),
        "radius": "50",
    }
    return urljoin(BASE_URL, SEARCH_PATH) + "?" + urlencode(params)


def _extract_clearance_levels(text: str) -> list[str]:
    found = []
    for kw in CLEARANCE_KEYWORDS:
        if re.search(re.escape(kw), text, re.IGNORECASE):
            found.append(kw)
    return list(dict.fromkeys(found))  # preserve order, deduplicate


def _extract_certs(text: str) -> list[str]:
    return list(dict.fromkeys(m.group(0) for m in CERT_PATTERNS.finditer(text)))


def _extract_skill_phrases(text: str) -> list[str]:
    """
    Pull bullet-point lines and sentences that look like skill/requirement
    statements.  Keeps raw language so callers can do their own NLP.
    """
    skills: list[str] = []
    lines = text.splitlines()
    for line in lines:
        line = line.strip()
        # Skip very short or very long lines (headers vs. paragraphs)
        if len(line) < 10 or len(line) > 300:
            continue
        lower = line.lower()
        skill_signals = (
            "experience", "knowledge", "proficiency", "familiar", "skill",
            "ability", "certif", "clearance", "degree", "bachelor", "master",
            "required", "preferred", "must have", "nice to have", "desired",
            "years", "yr ", "+yrs", "hands-on",
        )
        if any(sig in lower for sig in skill_signals):
            skills.append(line)
    return skills


async def _safe_navigate(page: Page, url: str) -> bool:
    """
    Navigate to url; return False on 429/403 or timeout without crashing.
    """
    try:
        response = await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        if response is None:
            logger.warning("No response object for %s", url)
            return False
        status = response.status
        if status in (403, 429):
            logger.warning("HTTP %d for %s — skipping", status, url)
            return False
        if status >= 400:
            logger.warning("HTTP %d for %s — skipping", status, url)
            return False
        return True
    except PlaywrightTimeout:
        logger.warning("Timeout navigating to %s — skipping", url)
        return False
    except Exception as exc:
        logger.warning("Navigation error for %s: %s — skipping", url, exc)
        return False


async def _collect_posting_urls(page: Page, search_url: str, max_links: int = 20) -> list[str]:
    """
    Load search results page and return up to max_links individual job posting URLs.
    """
    ok = await _safe_navigate(page, search_url)
    if not ok:
        return []

    # Wait briefly for JS to hydrate
    try:
        await page.wait_for_load_state("networkidle", timeout=15_000)
    except PlaywrightTimeout:
        pass  # proceed with whatever rendered

    links = await page.eval_on_selector_all(
        SELECTORS["job_links"],
        "els => els.map(e => e.href)",
    )

    # Filter to actual job detail pages (contain numeric ID or slug after /jobs/)
    posting_urls: list[str] = []
    seen: set[str] = set()
    for href in links:
        if not href:
            continue
        # Exclude the search page itself and login-required pages
        if "/jobs/" not in href:
            continue
        if any(x in href for x in ("login", "register", "signin", "create-account")):
            continue
        clean = href.split("?")[0].rstrip("/")
        if clean not in seen and clean != urljoin(BASE_URL, SEARCH_PATH):
            seen.add(clean)
            posting_urls.append(clean)
        if len(posting_urls) >= max_links:
            break

    logger.info("Found %d posting URLs on search page", len(posting_urls))
    return posting_urls


async def _scrape_single_posting(page: Page, url: str) -> Optional[dict]:
    """
    Scrape one job posting.  Returns a dict with raw extracted fields, or None
    if the page could not be scraped.
    """
    ok = await _safe_navigate(page, url)
    if not ok:
        return None

    try:
        await page.wait_for_load_state("networkidle", timeout=12_000)
    except PlaywrightTimeout:
        pass

    try:
        full_text = await page.inner_text("body")
    except Exception as exc:
        logger.warning("Could not read body text from %s: %s", url, exc)
        return None

    # Attempt structured selectors first, fall back to full page text
    description_text = full_text
    try:
        desc_el = await page.query_selector(SELECTORS["job_description"])
        if desc_el:
            description_text = await desc_el.inner_text()
    except Exception:
        pass

    company_name: str = ""
    try:
        company_el = await page.query_selector(SELECTORS["company_name"])
        if company_el:
            company_name = (await company_el.inner_text()).strip()
    except Exception:
        pass

    return {
        "url": url,
        "company_name": company_name,
        "description_text": description_text,
        "skill_phrases": _extract_skill_phrases(description_text),
        "clearance_levels": _extract_clearance_levels(full_text),
        "certs": _extract_certs(full_text),
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def enrich_from_postings(role_title: str, domain_context: str) -> dict:
    """
    Scrape ClearanceJobs.com for job postings matching role_title and
    domain_context.  Extracts skill language, clearance levels, certification
    requirements, and contractor/agency names for SkillPicture enrichment.

    This function is for JD ENRICHMENT ONLY and must never be used to source
    or identify individual candidates.

    Args:
        role_title:      e.g. "All-Source Analyst"
        domain_context:  e.g. "SIGINT NSA IC"

    Returns:
        {
            "skills_extracted":     list[str],   # deduplicated skill phrases
            "clearance_levels_seen": list[str],  # deduplicated clearance tokens
            "certs_seen":           list[str],   # deduplicated cert tokens
            "postings_reviewed":    int,
        }
    """
    all_skill_phrases: list[str] = []
    all_clearances: list[str] = []
    all_certs: list[str] = []
    postings_reviewed = 0

    search_url = _build_search_url(role_title, domain_context)
    logger.info(
        "ClearanceJobs enrichment search | role='%s' domain='%s' url=%s",
        role_title, domain_context, search_url,
    )

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=_random_ua(),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
            java_script_enabled=True,
        )
        # Block images/fonts to speed up loading and reduce fingerprint surface
        await context.route(
            "**/*.{png,jpg,jpeg,gif,webp,svg,ico,woff,woff2,ttf,otf}",
            lambda route, _req: route.abort(),
        )
        page = await context.new_page()

        # Step 1: collect posting URLs from the search results page
        posting_urls = await _collect_posting_urls(page, search_url, max_links=20)

        if not posting_urls:
            logger.warning(
                "No postings found for role='%s' domain='%s'", role_title, domain_context
            )
            await browser.close()
            return {
                "skills_extracted": [],
                "clearance_levels_seen": [],
                "certs_seen": [],
                "postings_reviewed": 0,
            }

        # Step 2: scrape each individual posting
        for url in posting_urls:
            await _random_delay()

            # Rotate user-agent per request via a fresh context header
            try:
                await page.set_extra_http_headers({"User-Agent": _random_ua()})
            except Exception:
                pass

            result = await _scrape_single_posting(page, url)
            if result is None:
                continue

            postings_reviewed += 1
            all_skill_phrases.extend(result["skill_phrases"])
            all_clearances.extend(result["clearance_levels"])
            all_certs.extend(result["certs"])

            logger.debug(
                "Scraped posting %d/%d: %s (company=%r, skills=%d, clearances=%d, certs=%d)",
                postings_reviewed, len(posting_urls), url,
                result["company_name"],
                len(result["skill_phrases"]),
                len(result["clearance_levels"]),
                len(result["certs"]),
            )

        await browser.close()

    # Deduplicate while preserving insertion order
    def _dedup(items: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for item in items:
            key = item.strip().lower()
            if key and key not in seen:
                seen.add(key)
                out.append(item.strip())
        return out

    output = {
        "skills_extracted": _dedup(all_skill_phrases),
        "clearance_levels_seen": _dedup(all_clearances),
        "certs_seen": _dedup(all_certs),
        "postings_reviewed": postings_reviewed,
    }

    logger.info(
        "ClearanceJobs enrichment complete | postings=%d skills=%d clearances=%d certs=%d",
        output["postings_reviewed"],
        len(output["skills_extracted"]),
        len(output["clearance_levels_seen"]),
        len(output["certs_seen"]),
    )
    return output


# ---------------------------------------------------------------------------
# CLI entry point for ad-hoc testing
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    import json

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    _role = sys.argv[1] if len(sys.argv) > 1 else "All-Source Analyst"
    _domain = sys.argv[2] if len(sys.argv) > 2 else "SIGINT IC DoD"

    result = asyncio.run(enrich_from_postings(_role, _domain))
    print(json.dumps(result, indent=2))
