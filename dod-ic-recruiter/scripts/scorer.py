"""
scorer.py — Candidate scoring engine for dod-ic-recruiter.

Scores every enriched candidate dict against a confirmed SkillPicture and
returns a list of MatchResult dicts sorted by overall_score descending.

Usage
-----
    from scripts.scorer import score_candidates

    results = score_candidates(candidates, skill_picture, weights_path)
    # or score a single candidate:
    result  = score_candidate(candidate, skill_picture, weights_path)
"""

from __future__ import annotations

import os
import re
import textwrap
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Default weights path — resolved relative to this file so the module is
# importable regardless of the caller's cwd.
# ---------------------------------------------------------------------------
_DEFAULT_WEIGHTS_PATH = str(
    Path(__file__).parent.parent / "config" / "scoring_weights.yaml"
)

# ---------------------------------------------------------------------------
# IC/DoD domain vocabulary used for domain_alignment scoring
# ---------------------------------------------------------------------------
_IC_AGENCIES = {
    "cia", "nsa", "dia", "nro", "nga", "nctc", "odni", "dhs", "ic",
    "intelligence community", "national security agency",
    "central intelligence agency", "defense intelligence agency",
    "national reconnaissance office", "national geospatial",
    "office of the director of national intelligence",
}

_DOD_KEYWORDS = {
    "dod", "department of defense", "pentagon", "darpa", "jsoc", "socom",
    "joint chiefs", "combatant command", "cocom", "j2", "j3", "j6",
    "army", "navy", "air force", "marines", "space force", "coast guard",
    "military intelligence", "sipr", "nipr", "jwics", "scif",
    "special operations", "psyop", "sigint", "humint", "geoint", "masint",
    "osint", "all-source", "all source", "fusion analyst",
}

_OPS_ENV_KEYWORDS = {
    "classified", "top secret", "ts/sci", "sci", "poly", "polygraph",
    "full scope", "lifestyle poly", "codeword", "sensitive compartmented",
    "sci access", "sar", "special access", "black", "covert", "overt",
    "forward deployed", "deployed", "theater", "overseas", "contingency",
    "combat support", "warfighter", "mission partner",
}

_CLEARANCE_KEYWORDS = {
    "confirmed": [
        r"\bts/sci\b", r"\btop secret/sci\b", r"\bts\s*/\s*sci\b",
        r"\bsci eligible\b", r"\bactive\s+ts\b", r"\bactive\s+clearance\b",
        r"\bsci access\b", r"\bcurrent\s+clearance\b",
        r"\bpolygraph\b", r"\bpoly\b",
        r"\bfull scope\b", r"\blifestyle poly\b",
        r"\bsecret\s+clearance\b", r"\bts clearance\b",
        r"\bdod clearance\b", r"\bactive\s+secret\b",
    ],
    "probable": [
        r"\btop secret\b", r"\b(?:holds?|hold|held)\s+(?:a\s+)?clearance\b",
        r"\bscope\s+poly\b", r"\bci\s+poly\b", r"\bclearance\s+holder\b",
        r"\bcleared\s+professional\b", r"\bsecurity\s+cleared\b",
        r"\bcleared\b",  # standalone "cleared" is probable, not confirmed
    ],
    "possible": [
        r"\bsecret\b",
        r"\bcleared\s+facility\b",
        r"\bcleared\s+environment\b",
        r"\bscif\b",
        r"\bjwics\b",
        r"\bsipr\b",
        r"\bnipr\b",
        r"\bclassified\s+work\b",
        r"\bsensitive\s+compartmented\b",
    ],
    "unconfirmed": [
        r"\bclearable\b",
        r"\bwilling\s+to\s+obtain\s+clearance\b",
        r"\bable\s+to\s+obtain\b",
        r"\bus\s+citizen\b",
        r"\bamerican\s+citizen\b",
        r"\bcitizen.*clearance\b",
    ],
}


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_weights(weights_path: str) -> dict:
    """Load scoring weights from YAML; fill defaults on any missing keys."""
    path = Path(weights_path)
    config: dict = {}
    if path.exists():
        with open(path, "r", encoding="utf-8") as fh:
            config = yaml.safe_load(fh) or {}

    weights = config.get("weights", {})
    defaults = {
        "skill_match": 0.40,
        "clearance_signal": 0.25,
        "experience_match": 0.20,
        "domain_alignment": 0.15,
    }
    for k, v in defaults.items():
        weights.setdefault(k, v)

    # Normalise so weights always sum to 1.0 (guards against hand-edits)
    total = sum(weights.values())
    if total > 0:
        weights = {k: v / total for k, v in weights.items()}

    internals = config.get("skill_match_internals", {})
    internals.setdefault("required_weight", 0.70)
    internals.setdefault("preferred_weight", 0.30)

    clearance_map = config.get("clearance_score_map", {})
    clearance_defaults = {
        "confirmed": 1.00,
        "probable": 0.75,
        "possible": 0.50,
        "unconfirmed": 0.25,
        "none": 0.00,
    }
    for k, v in clearance_defaults.items():
        clearance_map.setdefault(k, v)

    return {
        "weights": weights,
        "skill_match_internals": internals,
        "clearance_score_map": clearance_map,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalise(text: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation for fuzzy matching."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _skill_tokens(skill: str) -> set[str]:
    """Return the set of meaningful tokens in a skill label."""
    return set(_normalise(skill).split())


def _skill_present(skill: str, candidate_skills: list[str]) -> bool:
    """
    True if `skill` appears in `candidate_skills` — exact or token-subset
    match.  "Python" matches "Python 3", "machine learning" matches
    "Applied Machine Learning".
    """
    needle_tokens = _skill_tokens(skill)
    if not needle_tokens:
        return False
    for cs in candidate_skills:
        haystack_tokens = _skill_tokens(cs)
        # exact normalised match
        if _normalise(skill) == _normalise(cs):
            return True
        # needle tokens are a subset of haystack tokens (e.g. "Python" in "Python 3")
        if needle_tokens.issubset(haystack_tokens):
            return True
        # haystack tokens are a subset of needle (e.g. "ML" in "applied ML")
        if haystack_tokens.issubset(needle_tokens) and len(haystack_tokens) > 0:
            return True
    return False


def _collect_candidate_skills(candidate: dict) -> list[str]:
    """
    Gather all skill strings from known enriched-candidate fields:
    explicit_skills, inferred_skills, skills (flat list), raw_text tokens.
    """
    skills: list[str] = []
    for field in ("explicit_skills", "inferred_skills", "skills"):
        val = candidate.get(field)
        if isinstance(val, list):
            skills.extend(str(s) for s in val)
        elif isinstance(val, str):
            skills.append(val)
    return skills


def _collect_free_text(candidate: dict) -> str:
    """
    Concatenate every string-valued field in the candidate into a single
    lower-cased blob for keyword searches.
    """
    parts: list[str] = []
    for v in candidate.values():
        if isinstance(v, str):
            parts.append(v.lower())
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    parts.append(item.lower())
    return " ".join(parts)


def _candidate_display_name(candidate: dict) -> str:
    name = candidate.get("name") or candidate.get("full_name") or candidate.get("candidate_id", "")
    return name.strip()


# ---------------------------------------------------------------------------
# Dimension 1 — skill_match
# ---------------------------------------------------------------------------

def _score_skill_match(
    candidate: dict,
    skill_picture: dict,
    internals: dict,
) -> tuple[float, list[str]]:
    """
    Returns (score 0-1, list of matched skill labels).

    skill_picture expected keys (all optional):
      required_skills, preferred_skills, high_confidence_implicit_skills
    """
    req_weight = internals["required_weight"]
    pref_weight = internals["preferred_weight"]

    candidate_skills = _collect_candidate_skills(candidate)

    required: list[str] = skill_picture.get("required_skills", []) or []
    preferred: list[str] = skill_picture.get("preferred_skills", []) or []
    implicit: list[str] = skill_picture.get("high_confidence_implicit_skills", []) or []

    # Treat implicit skills as preferred
    combined_preferred = list(preferred) + list(implicit)

    matched: list[str] = []

    req_score = 0.0
    if required:
        hits = [s for s in required if _skill_present(s, candidate_skills)]
        matched.extend(hits)
        req_score = len(hits) / len(required)

    pref_score = 0.0
    if combined_preferred:
        hits = [s for s in combined_preferred if _skill_present(s, candidate_skills)]
        matched.extend(h for h in hits if h not in matched)
        pref_score = len(hits) / len(combined_preferred)

    if required and combined_preferred:
        score = req_weight * req_score + pref_weight * pref_score
    elif required:
        score = req_score
    elif combined_preferred:
        score = pref_score
    else:
        score = 0.0

    return round(min(score, 1.0), 4), matched


# ---------------------------------------------------------------------------
# Dimension 2 — experience_match
# ---------------------------------------------------------------------------

_SENIORITY_ORDER = ["junior", "mid", "senior", "staff", "principal"]

_ROLE_FAMILY_SYNONYMS: dict[str, list[str]] = {
    "analyst": ["analyst", "analysis", "intelligence analyst", "all-source", "fusion"],
    "engineer": ["engineer", "engineering", "developer", "developer", "swe", "software"],
    "data_scientist": ["data scientist", "data science", "ml", "machine learning", "ai"],
    "program_manager": ["program manager", "pm", "project manager", "pmo"],
    "operations": ["operations", "operator", "ops", "mission support"],
    "cybersecurity": ["cyber", "cybersecurity", "information security", "infosec", "soc"],
    "geospatial": ["geospatial", "geoint", "gis", "imagery", "remote sensing"],
    "sigint": ["sigint", "signals", "signals intelligence"],
    "humint": ["humint", "human intelligence", "case officer", "debriefer"],
}


def _parse_yoe(candidate: dict) -> float | None:
    """Extract years of experience as a float from various candidate fields."""
    for field in ("years_of_experience", "yoe", "experience_years", "years_experience"):
        val = candidate.get(field)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                pass

    # Try to infer from free text: "10 years of experience", "8+ years"
    text = _collect_free_text(candidate)
    m = re.search(r"(\d{1,2})\s*\+?\s*years?\s+(?:of\s+)?(?:experience|exp)", text)
    if m:
        return float(m.group(1))
    return None


def _parse_seniority(candidate: dict) -> str | None:
    """Return normalised seniority band label or None."""
    for field in ("seniority", "seniority_level", "level", "grade"):
        val = candidate.get(field)
        if isinstance(val, str):
            v = val.lower().strip()
            for band in _SENIORITY_ORDER:
                if band in v:
                    return band
            # common aliases
            if v in ("entry", "entry-level", "entry level"):
                return "junior"
            if v in ("lead", "sr", "sr.", "sr "):
                return "senior"
            if v in ("distinguished", "fellow"):
                return "principal"

    # Infer from titles
    titles_raw = candidate.get("titles", candidate.get("current_title", ""))
    if isinstance(titles_raw, list):
        titles_raw = " ".join(titles_raw)
    if isinstance(titles_raw, str):
        t = titles_raw.lower()
        if any(w in t for w in ("principal", "distinguished", "fellow")):
            return "principal"
        if any(w in t for w in ("staff", "architect")):
            return "staff"
        if any(w in t for w in ("senior", "sr.", "lead")):
            return "senior"
        if any(w in t for w in ("junior", "jr.", "associate", "entry")):
            return "junior"
    return None


def _seniority_distance(a: str | None, b: str | None) -> int:
    """Ordinal distance between two seniority bands; 0 = same."""
    if a is None or b is None:
        return 1  # moderate penalty for unknown
    try:
        return abs(_SENIORITY_ORDER.index(a) - _SENIORITY_ORDER.index(b))
    except ValueError:
        return 1


def _role_family_match(candidate: dict, skill_picture: dict) -> float:
    """0.0-1.0 score for role family alignment."""
    sp_role = (skill_picture.get("role_family") or "").lower().strip()
    if not sp_role:
        return 0.5  # no requirement specified — neutral

    candidate_text = _collect_free_text(candidate)

    # Find canonical family for skill_picture role
    sp_family = None
    for family, synonyms in _ROLE_FAMILY_SYNONYMS.items():
        if any(s in sp_role for s in synonyms) or sp_role == family:
            sp_family = family
            break
    if sp_family is None:
        # Try direct substring
        for family in _ROLE_FAMILY_SYNONYMS:
            if family in sp_role:
                sp_family = family
                break

    if sp_family is None:
        return 0.5  # unknown role family — neutral

    synonyms = _ROLE_FAMILY_SYNONYMS[sp_family]
    if any(s in candidate_text for s in synonyms):
        return 1.0
    # partial: at least one token matches
    sp_tokens = set(sp_role.split())
    if sp_tokens & set(candidate_text.split()):
        return 0.5
    return 0.0


def _score_experience_match(candidate: dict, skill_picture: dict) -> float:
    """Score experience alignment on YOE, seniority band, and role family."""
    # --- YOE ---
    candidate_yoe = _parse_yoe(candidate)
    target_yoe_min: float | None = skill_picture.get("min_years_experience")
    target_yoe_max: float | None = skill_picture.get("max_years_experience")
    target_yoe: float | None = skill_picture.get("years_experience") or skill_picture.get("target_yoe")

    # Normalise to min/max range
    if target_yoe is not None and target_yoe_min is None and target_yoe_max is None:
        target_yoe_min = max(0, target_yoe - 2)
        target_yoe_max = target_yoe + 3

    yoe_score = 0.5  # default neutral when no requirement
    if candidate_yoe is not None and (target_yoe_min is not None or target_yoe_max is not None):
        lo = float(target_yoe_min or 0)
        hi = float(target_yoe_max or 99)
        if lo <= candidate_yoe <= hi:
            yoe_score = 1.0
        else:
            gap = min(abs(candidate_yoe - lo), abs(candidate_yoe - hi))
            yoe_score = max(0.0, 1.0 - gap * 0.08)  # lose 8% per year off

    # --- Seniority ---
    candidate_seniority = _parse_seniority(candidate)
    target_seniority = skill_picture.get("seniority_level") or skill_picture.get("seniority")
    if isinstance(target_seniority, str):
        target_seniority = target_seniority.lower().strip()

    distance = _seniority_distance(candidate_seniority, target_seniority)
    seniority_score = max(0.0, 1.0 - distance * 0.33)

    # --- Role family ---
    role_score = _role_family_match(candidate, skill_picture)

    # Weighted composite: yoe 40%, seniority 30%, role 30%
    score = 0.40 * yoe_score + 0.30 * seniority_score + 0.30 * role_score
    return round(min(score, 1.0), 4)


# ---------------------------------------------------------------------------
# Dimension 3 — domain_alignment
# ---------------------------------------------------------------------------

def _score_domain_alignment(candidate: dict, skill_picture: dict) -> float:
    """
    IC/DoD context fit:
      - IC agency hint alignment
      - Operational environment keywords
      - DoD/military background
    """
    text = _collect_free_text(candidate)

    # Agency hint alignment
    sp_agencies: list[str] = skill_picture.get("agency_hints", []) or []
    agency_score = 0.0
    if sp_agencies:
        matched_agencies = [a for a in sp_agencies if a.lower() in text]
        agency_score = len(matched_agencies) / len(sp_agencies)
    else:
        # Generic IC presence counts
        ic_hits = sum(1 for kw in _IC_AGENCIES if kw in text)
        agency_score = min(1.0, ic_hits * 0.25)

    # DoD keywords
    dod_hits = sum(1 for kw in _DOD_KEYWORDS if kw in text)
    dod_score = min(1.0, dod_hits * 0.15)

    # Operational environment
    ops_hits = sum(1 for kw in _OPS_ENV_KEYWORDS if kw in text)
    ops_score = min(1.0, ops_hits * 0.20)

    # Weighted composite: agency 50%, dod 30%, ops 20%
    score = 0.50 * agency_score + 0.30 * dod_score + 0.20 * ops_score
    return round(min(score, 1.0), 4)


# ---------------------------------------------------------------------------
# Dimension 4 — clearance_signal
# ---------------------------------------------------------------------------

def _score_clearance(
    candidate: dict,
    clearance_score_map: dict,
) -> tuple[float, str, list[str]]:
    """
    Returns (score, inference_level, signals_found).

    Checks candidate's explicit clearance fields first, then scans free text.
    Inference levels: confirmed, probable, possible, unconfirmed, none.
    """
    # Explicit clearance fields take priority
    explicit_level = (
        candidate.get("clearance_level")
        or candidate.get("clearance")
        or candidate.get("security_clearance")
        or ""
    )
    if isinstance(explicit_level, str) and explicit_level.strip():
        level_lower = explicit_level.lower().strip()
        # Map common explicit values to inference levels
        if any(x in level_lower for x in ("ts/sci", "top secret/sci", "ts sci", "sci")):
            return clearance_score_map["confirmed"], "confirmed", [explicit_level]
        if any(x in level_lower for x in ("top secret", "ts")):
            return clearance_score_map["confirmed"], "confirmed", [explicit_level]
        if "secret" in level_lower:
            return clearance_score_map["confirmed"], "confirmed", [explicit_level]
        if "public trust" in level_lower:
            return clearance_score_map["possible"], "possible", [explicit_level]

    # Scan free text for clearance signals
    text = _collect_free_text(candidate)
    signals: list[str] = []

    for level in ("confirmed", "probable", "possible", "unconfirmed"):
        patterns = _CLEARANCE_KEYWORDS[level]
        level_signals = []
        for pattern in patterns:
            for m in re.finditer(pattern, text, re.IGNORECASE):
                level_signals.append(m.group(0))
        if level_signals:
            signals = list(dict.fromkeys(level_signals))  # deduplicate preserving order
            return clearance_score_map[level], level, signals

    return clearance_score_map["none"], "none", []


# ---------------------------------------------------------------------------
# Recruiter flags
# ---------------------------------------------------------------------------

def _build_recruiter_flags(
    candidate: dict,
    skill_picture: dict,
    skill_score: float,
    exp_score: float,
    domain_score: float,
    clearance_level: str,
    matched_skills: list[str],
) -> list[str]:
    """Return a list of short recruiter-facing flag strings."""
    flags: list[str] = []

    required: list[str] = skill_picture.get("required_skills", []) or []
    candidate_skills = _collect_candidate_skills(candidate)
    missing_required = [s for s in required if not _skill_present(s, candidate_skills)]
    if missing_required:
        flags.append(f"Missing required skills: {', '.join(missing_required[:5])}")

    if clearance_level in ("none", "unconfirmed"):
        flags.append("No clearance signal detected — verify eligibility before outreach")
    elif clearance_level == "possible":
        flags.append("Clearance possible but not confirmed — verify before outreach")

    target_yoe_min = skill_picture.get("min_years_experience")
    candidate_yoe = _parse_yoe(candidate)
    if target_yoe_min and candidate_yoe is not None and candidate_yoe < float(target_yoe_min):
        flags.append(
            f"Under-experienced: {candidate_yoe:.0f} yrs vs {target_yoe_min:.0f} yrs minimum"
        )

    target_seniority = (skill_picture.get("seniority_level") or "").lower()
    candidate_seniority = _parse_seniority(candidate) or ""
    if target_seniority and candidate_seniority:
        dist = _seniority_distance(candidate_seniority, target_seniority)
        if dist >= 2:
            flags.append(
                f"Seniority gap: candidate is {candidate_seniority}, role needs {target_seniority}"
            )

    if skill_score >= 0.85:
        flags.append("Strong technical skills match")
    if domain_score >= 0.75:
        flags.append("Strong IC/DoD domain background")
    if clearance_level == "confirmed":
        flags.append("Confirmed clearance signal")

    return flags


# ---------------------------------------------------------------------------
# Reasoning narrative
# ---------------------------------------------------------------------------

def _build_reasoning(
    candidate: dict,
    skill_picture: dict,
    overall_score: float,
    matched_skills: list[str],
    clearance_level: str,
    clearance_signals: list[str],
    flags: list[str],
) -> str:
    """
    2-4 plain-English sentences for a recruiter.
    References the candidate's actual employers and titles.
    """
    name = _candidate_display_name(candidate) or "This candidate"

    # Most recent employer and title
    employer = (
        candidate.get("current_employer")
        or candidate.get("employer")
        or candidate.get("company")
        or candidate.get("current_company")
        or ""
    )
    title = (
        candidate.get("current_title")
        or candidate.get("title")
        or candidate.get("job_title")
        or ""
    )
    if isinstance(title, list):
        title = title[0] if title else ""

    role_label = skill_picture.get("role_title") or skill_picture.get("title") or "the target role"

    # Opening sentence: who they are and where they work
    if employer and title:
        opening = f"{name} is currently a {title} at {employer}."
    elif title:
        opening = f"{name} currently holds the title of {title}."
    elif employer:
        opening = f"{name} works at {employer}."
    else:
        opening = f"{name}'s background was evaluated against the requirements for {role_label}."

    # Skill fit sentence
    required: list[str] = skill_picture.get("required_skills", []) or []
    candidate_skills = _collect_candidate_skills(candidate)
    hit_required = [s for s in required if _skill_present(s, candidate_skills)]
    miss_required = [s for s in required if not _skill_present(s, candidate_skills)]

    if hit_required and not miss_required:
        skill_sentence = (
            f"They meet all required skills for {role_label}, "
            f"including {', '.join(hit_required[:4])}."
        )
    elif hit_required:
        skill_sentence = (
            f"They match {len(hit_required)} of {len(required)} required skills "
            f"({', '.join(hit_required[:3])}), but are missing {', '.join(miss_required[:3])}."
        )
    elif required:
        skill_sentence = (
            f"They do not appear to match the required skills for {role_label} "
            f"({', '.join(required[:3])})."
        )
    else:
        skill_sentence = ""

    # Clearance sentence
    if clearance_level == "confirmed":
        signal_text = f" ({clearance_signals[0]})" if clearance_signals else ""
        clearance_sentence = f"Their profile contains a confirmed clearance signal{signal_text}."
    elif clearance_level == "probable":
        clearance_sentence = "Their profile suggests they likely hold an active clearance, but it should be verified."
    elif clearance_level == "possible":
        clearance_sentence = "Their background hints at possible clearance eligibility, but no explicit confirmation was found."
    elif clearance_level == "unconfirmed":
        clearance_sentence = (
            "No clearance was identified in their profile — "
            "verify eligibility before IC/DoD-specific outreach."
        )
    else:
        clearance_sentence = (
            "No clearance indicators were found; confirm citizenship and eligibility before outreach."
        )

    # Overall recommendation sentence
    if overall_score >= 0.80:
        rec = f"{name} is a strong match and should be prioritised for outreach."
    elif overall_score >= 0.60:
        rec = f"{name} is a moderate match and worth a screening call to assess gaps."
    elif overall_score >= 0.40:
        rec = f"{name} is a partial match; address the identified gaps before advancing."
    else:
        rec = f"{name} does not closely match the current requirement and may not be worth pursuing at this time."

    sentences = [s for s in [opening, skill_sentence, clearance_sentence, rec] if s]
    return " ".join(sentences)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def score_candidate(
    candidate: dict,
    skill_picture: dict,
    weights_path: str = _DEFAULT_WEIGHTS_PATH,
) -> dict:
    """
    Score a single enriched candidate dict against a confirmed SkillPicture.

    Parameters
    ----------
    candidate : dict
        Enriched candidate record.  Expected keys (all optional but useful):
          name, current_title, current_employer, years_of_experience,
          seniority, explicit_skills, inferred_skills, skills,
          clearance_level, candidate_id, …
    skill_picture : dict
        Confirmed SkillPicture.  Expected keys (all optional):
          role_title, required_skills, preferred_skills,
          high_confidence_implicit_skills, min_years_experience,
          max_years_experience, seniority_level, role_family,
          agency_hints, …
    weights_path : str
        Path to config/scoring_weights.yaml.

    Returns
    -------
    dict
        MatchResult with keys: overall_score, skill_match, experience_match,
        domain_alignment, clearance_signal, clearance_inference_level,
        clearance_signals_found, sources_found, reasoning,
        recruiter_flags, candidate_id.
    """
    config = _load_weights(weights_path)
    weights = config["weights"]
    internals = config["skill_match_internals"]
    clearance_map = config["clearance_score_map"]

    skill_score, matched_skills = _score_skill_match(candidate, skill_picture, internals)
    exp_score = _score_experience_match(candidate, skill_picture)
    domain_score = _score_domain_alignment(candidate, skill_picture)
    clearance_score, clearance_level, clearance_signals = _score_clearance(candidate, clearance_map)

    overall = (
        weights["skill_match"] * skill_score
        + weights["experience_match"] * exp_score
        + weights["domain_alignment"] * domain_score
        + weights["clearance_signal"] * clearance_score
    )
    overall = round(min(overall, 1.0), 4)

    flags = _build_recruiter_flags(
        candidate, skill_picture, skill_score, exp_score,
        domain_score, clearance_level, matched_skills,
    )

    reasoning = _build_reasoning(
        candidate, skill_picture, overall, matched_skills,
        clearance_level, clearance_signals, flags,
    )

    # Sources found: any URL-like strings in candidate profile
    text = _collect_free_text(candidate)
    sources_found = list(dict.fromkeys(
        re.findall(r"https?://[^\s\"'>]+", text)
    ))

    candidate_id = str(
        candidate.get("candidate_id")
        or candidate.get("id")
        or candidate.get("profile_url")
        or candidate.get("name", "unknown")
    )

    return {
        "candidate_id": candidate_id,
        "overall_score": overall,
        "skill_match": skill_score,
        "experience_match": exp_score,
        "domain_alignment": domain_score,
        "clearance_signal": clearance_score,
        "clearance_inference_level": clearance_level,
        "clearance_signals_found": clearance_signals,
        "sources_found": sources_found,
        "reasoning": reasoning,
        "recruiter_flags": flags,
    }


def score_candidates(
    candidates: list[dict],
    skill_picture: dict,
    weights_path: str = _DEFAULT_WEIGHTS_PATH,
) -> list[dict]:
    """
    Score a list of enriched candidate dicts and return MatchResult dicts
    sorted by overall_score descending.

    Parameters
    ----------
    candidates : list[dict]
        List of enriched candidate records.
    skill_picture : dict
        Confirmed SkillPicture.
    weights_path : str
        Path to config/scoring_weights.yaml.

    Returns
    -------
    list[dict]
        MatchResult dicts sorted by overall_score descending.
    """
    results = [score_candidate(c, skill_picture, weights_path) for c in candidates]
    results.sort(key=lambda r: r["overall_score"], reverse=True)
    return results
