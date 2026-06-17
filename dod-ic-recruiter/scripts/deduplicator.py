"""
deduplicator.py — Deduplication engine for dod-ic-recruiter skill.

Merges CandidateRaw records from multiple sources (LinkedIn scrapers,
job-board scrapers, iCIMS import) into a single deduplicated list.

Matching priority:
  1. Email (exact, case-insensitive) — tertiary/highest confidence
  2. Normalized name + normalized employer — primary
  3. Normalized name + approximate location (city or state) — secondary

Fuzzy name matching handles:
  - Common nickname expansions (Bob/Robert, Bill/William, etc.)
  - Hyphenated surnames
  - Generational suffixes (Jr, Sr, II, III, IV)
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from difflib import SequenceMatcher
from typing import List, Optional, Tuple


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class CandidateRaw:
    """A single candidate record as returned by any scraper or import."""

    name: str
    source_url: str
    source_platform: str          # e.g. "linkedin", "clearancejobs", "icims"
    raw_text: str
    scraped_at: datetime
    current_employer: str = ""
    current_title: str = ""
    location: str = ""
    skills: List[str] = field(default_factory=list)
    email: str = ""

    # Populated by the deduplicator — not set by scrapers.
    sources_found: List[str] = field(default_factory=list)
    recruiter_flag: str = ""


# ---------------------------------------------------------------------------
# Nickname / common-name expansion table
# ---------------------------------------------------------------------------

# Keys are canonical/formal names; values are known nicknames / variants.
# Both directions are resolved at match time.
_NICKNAME_MAP: dict[str, list[str]] = {
    "robert":    ["bob", "rob", "bobby", "robby"],
    "william":   ["bill", "will", "billy", "willy", "liam"],
    "james":     ["jim", "jimmy", "jamie"],
    "john":      ["jack", "johnny", "jon"],
    "richard":   ["rick", "rich", "dick", "ricky"],
    "charles":   ["chuck", "charlie", "chas"],
    "thomas":    ["tom", "tommy"],
    "joseph":    ["joe", "joey"],
    "michael":   ["mike", "mikey", "mick"],
    "stephen":   ["steve", "stevie"],
    "steven":    ["steve", "stevie"],
    "edward":    ["ed", "eddie", "ned", "ted", "teddy"],
    "henry":     ["hank", "harry"],
    "harold":    ["harry", "hal"],
    "george":    ["georgie"],
    "david":     ["dave", "davey"],
    "daniel":    ["dan", "danny"],
    "andrew":    ["andy", "drew"],
    "anthony":   ["tony"],
    "christopher": ["chris", "topher"],
    "matthew":   ["matt", "matty"],
    "nicholas":  ["nick", "nicky", "nic"],
    "timothy":   ["tim", "timmy"],
    "patrick":   ["pat", "patty", "rick"],
    "lawrence":  ["larry", "lars"],
    "raymond":   ["ray"],
    "gerald":    ["jerry", "gerry"],
    "jerome":    ["jerry"],
    "donald":    ["don", "donnie"],
    "ronald":    ["ron", "ronnie"],
    "kenneth":   ["ken", "kenny"],
    "gregory":   ["greg", "gregg"],
    "samuel":    ["sam", "sammy"],
    "benjamin":  ["ben", "benny"],
    "nathan":    ["nate"],
    "nathaniel": ["nate", "nat"],
    "jonathan":  ["jon", "jonny"],
    "alexander": ["alex", "alec", "al"],
    "katherine": ["kate", "kathy", "katy", "kat", "kay"],
    "catherine": ["cathy", "cat", "kate", "kay"],
    "margaret":  ["maggie", "meg", "peggy", "peg", "marge"],
    "elizabeth":  ["liz", "lizzie", "beth", "eliza", "betty", "bette", "ellie"],
    "patricia":  ["pat", "patty", "trish", "tricia"],
    "barbara":   ["barb", "barbie"],
    "susan":     ["sue", "suzy", "susie"],
    "dorothy":   ["dot", "dottie"],
    "helen":     ["nell", "nellie"],
    "virginia":  ["ginny"],
    "jennifer":  ["jen", "jenny"],
    "deborah":   ["deb", "debbie"],
    "donna":     ["donnie"],
    "carol":     ["carrie"],
    "ruth":      ["ruthie"],
    "sharon":    ["shari"],
    "laura":     ["laurie"],
    "linda":     ["lin", "lynda"],
    "amanda":    ["mandy"],
    "melissa":   ["mel", "missy"],
    "stephanie":  ["steph", "stevie"],
    "jacqueline": ["jackie", "jacqui"],
    "victoria":  ["vicki", "vickie", "tori"],
    "jessica":   ["jess", "jessie"],
    "ashley":    ["ash"],
    "sarah":     ["sara", "sadie", "sallie"],
    "rachel":    ["rach"],
    "rebecca":   ["becca", "becky", "bex"],
}

# Build a reverse lookup: nickname -> set of canonical names it may expand to
_NICKNAME_REVERSE: dict[str, set[str]] = {}
for _canonical, _nicknames in _NICKNAME_MAP.items():
    for _nn in _nicknames:
        _NICKNAME_REVERSE.setdefault(_nn, set()).add(_canonical)
    # canonical also maps to itself
    _NICKNAME_REVERSE.setdefault(_canonical, set()).add(_canonical)


def _canonical_names(first: str) -> set[str]:
    """Return the set of canonical first-name forms for *first*."""
    result: set[str] = {first}
    if first in _NICKNAME_MAP:
        result.update(_NICKNAME_MAP[first])
    if first in _NICKNAME_REVERSE:
        result.update(_NICKNAME_REVERSE[first])
    return result


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

_SUFFIX_RE = re.compile(
    r"\b(jr\.?|sr\.?|ii|iii|iv|v|esq\.?|phd\.?|md\.?|dds\.?|cpa\.?)\b",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_text(text: str) -> str:
    """Lowercase, strip accents, collapse whitespace."""
    nfkd = unicodedata.normalize("NFKD", text)
    ascii_str = "".join(c for c in nfkd if not unicodedata.combining(c))
    return _WHITESPACE_RE.sub(" ", ascii_str.lower()).strip()


def _normalize_name(name: str) -> Tuple[str, str]:
    """
    Return (first_normalized, last_normalized) after:
    - Unicode normalization
    - Removing generational suffixes
    - Collapsing hyphens in surnames to a single token
    - Lowercasing
    """
    name = _normalize_text(name)
    name = _SUFFIX_RE.sub("", name).strip()
    parts = name.split()
    if not parts:
        return ("", "")
    first = parts[0]
    last = " ".join(parts[1:]) if len(parts) > 1 else ""
    # Collapse hyphenated last names: smith-jones -> smithjones
    last_nohyphen = last.replace("-", "")
    return (first, last_nohyphen)


def _normalize_employer(employer: str) -> str:
    """Strip legal suffixes, punctuation, and lowercase for comparison."""
    text = _normalize_text(employer)
    # Remove common legal entity suffixes
    text = re.sub(
        r"\b(inc\.?|llc\.?|ltd\.?|corp\.?|co\.?|llp\.?|lp\.?|plc\.?|gmbh)\b",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"[^\w\s]", " ", text)
    return _WHITESPACE_RE.sub(" ", text).strip()


def _normalize_location(location: str) -> Tuple[str, str]:
    """
    Parse a location string into (city_normalized, state_normalized).

    Handles formats: "City, ST", "City, State", "City", "ST".
    """
    loc = _normalize_text(location)
    # Remove country tokens
    loc = re.sub(r"\b(usa?|united states|us)\b", "", loc).strip()
    parts = [p.strip() for p in loc.split(",") if p.strip()]
    if len(parts) >= 2:
        return (parts[0], parts[1])
    if len(parts) == 1:
        token = parts[0]
        # Heuristic: two-letter token is likely a state abbreviation
        if len(token) <= 2:
            return ("", token)
        return (token, "")
    return ("", "")


def _normalize_email(email: str) -> str:
    return email.strip().lower()


# ---------------------------------------------------------------------------
# Fuzzy matching helpers
# ---------------------------------------------------------------------------

_FUZZY_THRESHOLD = 0.82  # SequenceMatcher ratio threshold for "same name"


def _ratio(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _names_match(name_a: str, name_b: str) -> bool:
    """
    Return True when two full name strings refer to the same person.

    Steps:
    1. If normalized (first, last) are identical — match.
    2. Expand first names via nickname table; if any pair matches — match.
    3. Fuzzy last-name comparison (handles minor typos / middle-name drift).
    """
    first_a, last_a = _normalize_name(name_a)
    first_b, last_b = _normalize_name(name_b)

    if not last_a or not last_b:
        return False

    # Last name must be close enough
    last_ratio = _ratio(last_a, last_b)
    if last_ratio < _FUZZY_THRESHOLD:
        return False

    # First name: exact or nickname expansion
    canon_a = _canonical_names(first_a)
    canon_b = _canonical_names(first_b)
    if canon_a & canon_b:  # non-empty intersection
        return True

    # Fuzzy first name (catches minor typos / middle-initial inclusion)
    if _ratio(first_a, first_b) >= _FUZZY_THRESHOLD:
        return True

    return False


def _employers_match(emp_a: str, emp_b: str) -> bool:
    """Return True when two employer strings refer to the same organisation."""
    if not emp_a or not emp_b:
        return False
    a = _normalize_employer(emp_a)
    b = _normalize_employer(emp_b)
    if a == b:
        return True
    return _ratio(a, b) >= _FUZZY_THRESHOLD


def _locations_match(loc_a: str, loc_b: str) -> bool:
    """Return True when two location strings share city OR state."""
    city_a, state_a = _normalize_location(loc_a)
    city_b, state_b = _normalize_location(loc_b)
    if city_a and city_b and city_a == city_b:
        return True
    if state_a and state_b and state_a == state_b:
        return True
    return False


# ---------------------------------------------------------------------------
# Merge logic
# ---------------------------------------------------------------------------

_ICIMS_PLATFORM = "icims"


def _is_icims(candidate: CandidateRaw) -> bool:
    return candidate.source_platform.strip().lower() == _ICIMS_PLATFORM


def _merge_two(base: CandidateRaw, incoming: CandidateRaw) -> CandidateRaw:
    """
    Merge *incoming* into *base*, returning an updated record.

    Field-resolution rules:
    - iCIMS data beats scraped data for structured fields.
    - Among scraped records, prefer the one with the newer scraped_at.
    - sources_found accumulates all distinct platform names.
    - recruiter_flag is recalculated after merge (caller's responsibility).
    """
    # Determine authority record for structured fields
    if _is_icims(incoming) and not _is_icims(base):
        authority, secondary = incoming, base
    elif _is_icims(base) and not _is_icims(incoming):
        authority, secondary = base, incoming
    else:
        # Both scraped or both iCIMS — prefer newer
        if incoming.scraped_at > base.scraped_at:
            authority, secondary = incoming, base
        else:
            authority, secondary = base, incoming

    def pick(auth_val: str, sec_val: str) -> str:
        return auth_val.strip() if auth_val.strip() else sec_val.strip()

    merged_skills = list(
        dict.fromkeys(
            [s.strip() for s in authority.skills if s.strip()]
            + [s.strip() for s in secondary.skills if s.strip()]
        )
    )

    merged_sources = list(
        dict.fromkeys(
            (base.sources_found or [base.source_platform])
            + (incoming.sources_found or [incoming.source_platform])
        )
    )

    result = CandidateRaw(
        name=authority.name or secondary.name,
        source_url=authority.source_url or secondary.source_url,
        source_platform=authority.source_platform,
        raw_text=authority.raw_text or secondary.raw_text,
        scraped_at=authority.scraped_at,
        current_employer=pick(authority.current_employer, secondary.current_employer),
        current_title=pick(authority.current_title, secondary.current_title),
        location=pick(authority.location, secondary.location),
        skills=merged_skills,
        email=pick(authority.email, secondary.email),
        sources_found=merged_sources,
    )
    return result


def _build_recruiter_flag(candidate: CandidateRaw) -> str:
    """Generate the appropriate recruiter_flag for a (possibly merged) record."""
    sources = candidate.sources_found or [candidate.source_platform]
    n = len(sources)
    platform_set = {s.strip().lower() for s in sources}
    has_icims = _ICIMS_PLATFORM in platform_set
    has_external = any(s != _ICIMS_PLATFORM for s in platform_set)

    if has_icims and has_external:
        return (
            "Active candidate — found in iCIMS pipeline and externally visible"
        )
    if n > 1:
        return f"Found on {n} sources — strong activity signal"
    return ""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def deduplicate(
    candidates: List[CandidateRaw],
) -> Tuple[List[CandidateRaw], str]:
    """
    Deduplicate a list of CandidateRaw objects.

    Matching is applied in three passes (highest confidence first):

    Pass 1 — Email match (exact, case-insensitive).
    Pass 2 — Normalized name + normalized employer (fuzzy).
    Pass 3 — Normalized name + approximate location (city or state).

    Parameters
    ----------
    candidates:
        Raw list of CandidateRaw objects from all scrapers / imports.

    Returns
    -------
    (merged_list, summary_string)
        merged_list: deduplicated CandidateRaw objects with populated
                     sources_found and recruiter_flag.
        summary_string: human-readable summary e.g.
            "47 raw candidates across all sources, 31 unique after deduplication"
    """
    raw_count = len(candidates)

    # --- initialise sources_found for every record -------------------------
    for c in candidates:
        if not c.sources_found:
            c.sources_found = [c.source_platform]

    # Work on a copy so callers are not surprised by mutation
    pool: List[CandidateRaw] = list(candidates)
    merged: List[CandidateRaw] = []

    # Union-find would be O(n alpha(n)) but n is expected to be small
    # (<10k candidates per run), so a simple O(n^2) pass is fine.

    # Track which pool indices have been consumed
    consumed = [False] * len(pool)

    for i in range(len(pool)):
        if consumed[i]:
            continue
        current = pool[i]
        for j in range(i + 1, len(pool)):
            if consumed[j]:
                continue
            other = pool[j]
            if _is_duplicate(current, other):
                current = _merge_two(current, other)
                consumed[j] = True
        merged.append(current)

    # --- populate recruiter_flag -------------------------------------------
    for c in merged:
        c.recruiter_flag = _build_recruiter_flag(c)

    unique_count = len(merged)
    summary = (
        f"{raw_count} raw candidate{'s' if raw_count != 1 else ''} across all sources, "
        f"{unique_count} unique after deduplication"
    )
    return merged, summary


def _is_duplicate(a: CandidateRaw, b: CandidateRaw) -> bool:
    """
    Return True if records *a* and *b* refer to the same person.

    Three independent signals; any one match is sufficient:
    1. Email (both non-empty, exact match after normalisation).
    2. Name match + employer match.
    3. Name match + location match.
    """
    # --- Pass 1: Email -------------------------------------------------------
    email_a = _normalize_email(a.email)
    email_b = _normalize_email(b.email)
    if email_a and email_b and email_a == email_b:
        return True

    # --- Name match prerequisite for passes 2 & 3 ---------------------------
    if not _names_match(a.name, b.name):
        return False

    # --- Pass 2: Name + Employer ---------------------------------------------
    if a.current_employer and b.current_employer:
        if _employers_match(a.current_employer, b.current_employer):
            return True

    # --- Pass 3: Name + Location ---------------------------------------------
    if a.location and b.location:
        if _locations_match(a.location, b.location):
            return True

    return False
