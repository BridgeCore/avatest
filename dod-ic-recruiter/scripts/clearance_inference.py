"""
clearance_inference.py
----------------------
Clearance inference engine for the dod-ic-recruiter skill.

Loads signal configuration from config/clearance_signals.yaml and computes a
clearance inference level for each candidate based on signals found in their
profile text, employer history, and role titles.

Clearance NEVER automatically disqualifies a candidate — it only affects scoring.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# Text normalisation helpers
# ---------------------------------------------------------------------------

def _normalize(text: str) -> str:
    """
    Lowercase, strip accents, collapse whitespace, and strip punctuation that
    would prevent simple substring matching (e.g. en-dashes, smart quotes).
    Keeps hyphens intact so patterns like "ts/sci" still match.
    """
    if not isinstance(text, str):
        return ""
    # NFKD normalisation strips combining accents
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    # Replace common punctuation variants but preserve "/" and "-"
    text = re.sub(r"[–—–—]", "-", text)   # en/em-dash → hyphen
    text = re.sub(r"[''`]", "'", text)               # smart single quotes
    text = re.sub(r"[\"""'']", '"', text)             # smart double quotes
    text = re.sub(r"\s+", " ", text).strip()
    return text


def _normalize_list(items: list) -> list[str]:
    """Normalise every string in a list, dropping non-strings."""
    return [_normalize(s) for s in items if isinstance(s, str)]


# ---------------------------------------------------------------------------
# YAML config loader (cached per config_path)
# ---------------------------------------------------------------------------

_config_cache: dict[str, dict] = {}


def _load_config(config_path: str) -> dict:
    """Load and cache the clearance signals YAML configuration."""
    abs_path = str(Path(config_path).resolve())
    if abs_path in _config_cache:
        return _config_cache[abs_path]
    with open(abs_path, "r", encoding="utf-8") as fh:
        config = yaml.safe_load(fh)
    _config_cache[abs_path] = config
    return config


# ---------------------------------------------------------------------------
# Signal detection helpers
# ---------------------------------------------------------------------------

def _pattern_in_text(pattern: str, text: str) -> bool:
    """
    Case-insensitive substring match on already-normalised text.
    Uses word-boundary anchors only for short single-word patterns to avoid
    false positives (e.g. "dia" inside "media").
    """
    # Patterns that are <= 4 chars and all alpha get word-boundary protection
    clean = pattern.strip()
    if re.fullmatch(r"[a-z]{1,4}", clean):
        return bool(re.search(r"\b" + re.escape(clean) + r"\b", text))
    return clean in text


def _detect_confirmed_signals(
    norm_full_text: str,
    cfg_confirmed: dict,
) -> list[dict]:
    signals: list[dict] = []
    weight = float(cfg_confirmed.get("weight", 1.0))
    for entry in cfg_confirmed.get("patterns", []):
        pattern = _normalize(entry["pattern"])
        if _pattern_in_text(pattern, norm_full_text):
            signals.append({
                "signal_type": "confirmed",
                "description": entry["description"],
                "weight": weight,
                "matched_pattern": entry["pattern"],
            })
    return signals


def _detect_strong_signals(
    norm_full_text: str,
    norm_employers: list[str],
    norm_titles: list[str],
    cfg_strong: dict,
) -> list[dict]:
    signals: list[dict] = []
    weight = float(cfg_strong.get("weight", 0.7))

    # IC agencies — check employer history first, fall back to full text
    for agency in cfg_strong.get("ic_agencies", []):
        matched = False
        for alias in agency.get("aliases", []):
            norm_alias = _normalize(alias)
            # employer history match (more reliable)
            for emp in norm_employers:
                if _pattern_in_text(norm_alias, emp):
                    matched = True
                    break
            if not matched:
                # full-text fallback with word-boundary protection
                if _pattern_in_text(norm_alias, norm_full_text):
                    matched = True
            if matched:
                break
        if matched:
            signals.append({
                "signal_type": "strong",
                "description": agency["description"],
                "weight": weight,
                "matched_pattern": agency["name"],
            })

    # Cleared contractors — same strategy
    for contractor in cfg_strong.get("cleared_contractors", []):
        matched = False
        for alias in contractor.get("aliases", []):
            norm_alias = _normalize(alias)
            for emp in norm_employers:
                if _pattern_in_text(norm_alias, emp):
                    matched = True
                    break
            if not matched and _pattern_in_text(norm_alias, norm_full_text):
                matched = True
            if matched:
                break
        if matched:
            signals.append({
                "signal_type": "strong",
                "description": contractor["description"],
                "weight": weight,
                "matched_pattern": contractor["name"],
            })

    # Clearance-implying role titles — check role_titles first, then full text
    for title_entry in cfg_strong.get("clearance_implying_titles", []):
        pattern = _normalize(title_entry["pattern"])
        matched = any(_pattern_in_text(pattern, t) for t in norm_titles)
        if not matched:
            matched = _pattern_in_text(pattern, norm_full_text)
        if matched:
            signals.append({
                "signal_type": "strong",
                "description": title_entry["description"],
                "weight": weight,
                "matched_pattern": title_entry["pattern"],
            })

    return signals


def _detect_gov_contracting_tenure(
    employer_history: list,
    min_years: int,
    description: str,
    weight: float,
) -> list[dict]:
    """
    Heuristic: look for 'years' annotations or date ranges in employer records.
    Accepts dicts with optional 'years', 'duration_years', or 'start'/'end' keys,
    or plain strings that mention government/federal/contractor employment alongside
    year counts.
    """
    signals: list[dict] = []
    total_gov_years: float = 0.0

    gov_keywords = re.compile(
        r"\b(government|federal|dod|department of defense|army|navy|air force|"
        r"marine|navy|coast guard|contractor|pentagon|intelligence community)\b",
        re.IGNORECASE,
    )
    year_pattern = re.compile(r"\b(\d{1,2})\s*(?:\+\s*)?years?\b", re.IGNORECASE)
    date_range_pattern = re.compile(
        r"\b((?:19|20)\d{2})\s*[-–—to]+\s*((?:19|20)\d{2}|present|current|now)\b",
        re.IGNORECASE,
    )

    for entry in employer_history:
        entry_years: float = 0.0

        if isinstance(entry, dict):
            # Prefer explicit duration keys
            for key in ("years", "duration_years", "duration"):
                val = entry.get(key)
                if val is not None:
                    try:
                        entry_years = float(str(val).replace("+", "").strip())
                    except ValueError:
                        pass
                    break

            # Fall back to start/end dates
            if entry_years == 0.0:
                start = entry.get("start") or entry.get("start_year")
                end = entry.get("end") or entry.get("end_year")
                if start and end:
                    try:
                        end_yr = 2025 if str(end).lower() in ("present", "current", "now") else int(str(end)[:4])
                        entry_years = max(0.0, float(end_yr) - float(str(start)[:4]))
                    except (ValueError, TypeError):
                        pass

            employer_text = " ".join(str(v) for v in entry.values())
        else:
            employer_text = str(entry)

        # If no explicit years, try parsing from string
        if entry_years == 0.0:
            year_match = year_pattern.search(employer_text)
            if year_match:
                entry_years = float(year_match.group(1))
            else:
                dr_match = date_range_pattern.search(employer_text)
                if dr_match:
                    try:
                        end_yr = 2025 if dr_match.group(2).lower() in ("present", "current", "now") else int(dr_match.group(2))
                        entry_years = max(0.0, float(end_yr) - float(dr_match.group(1)))
                    except (ValueError, TypeError):
                        pass

        # Only count toward gov tenure if employer looks government-related
        if entry_years > 0 and gov_keywords.search(employer_text):
            total_gov_years += entry_years

    if total_gov_years >= min_years:
        signals.append({
            "signal_type": "medium",
            "description": f"{description} (inferred {total_gov_years:.1f} years)",
            "weight": weight,
            "matched_pattern": f">={min_years}_years_gov_contracting",
        })

    return signals


def _detect_medium_signals(
    norm_full_text: str,
    employer_history: list,
    cfg_medium: dict,
) -> list[dict]:
    signals: list[dict] = []
    weight = float(cfg_medium.get("weight", 0.4))

    # Program/contract keywords
    for entry in cfg_medium.get("program_keywords", []):
        pattern = _normalize(entry["pattern"])
        if _pattern_in_text(pattern, norm_full_text):
            signals.append({
                "signal_type": "medium",
                "description": entry["description"],
                "weight": weight,
                "matched_pattern": entry["pattern"],
            })

    # Government contracting tenure heuristic
    tenure_cfg = cfg_medium.get("gov_contracting_tenure", {})
    min_years = int(tenure_cfg.get("min_years", 5))
    desc = tenure_cfg.get("description", f"{min_years}+ years continuous government contracting")
    signals.extend(
        _detect_gov_contracting_tenure(employer_history, min_years, desc, weight)
    )

    # IC pipeline education
    for entry in cfg_medium.get("ic_pipeline_education", []):
        pattern = _normalize(entry["pattern"])
        if _pattern_in_text(pattern, norm_full_text):
            signals.append({
                "signal_type": "medium",
                "description": entry["description"],
                "weight": weight,
                "matched_pattern": entry["pattern"],
            })

    return signals


def _detect_weak_signals(
    norm_full_text: str,
    norm_employers: list[str],
    cfg_weak: dict,
) -> list[dict]:
    """
    Weak signals are only fired when both a security-adjacent cert AND government
    employment context are present together, or when a contractor self-identification
    pattern is found without any stronger signal already firing (de-duplication is
    handled in infer_clearance).
    """
    signals: list[dict] = []
    weight = float(cfg_weak.get("weight", 0.2))

    gov_context_keywords = (
        "government", "federal", "dod", "department of defense",
        "army", "navy", "air force", "marine", "pentagon",
        "national security", "intelligence", "contractor",
    )
    has_gov_context = any(kw in norm_full_text for kw in gov_context_keywords)
    has_employer_gov_context = any(
        any(kw in emp for kw in gov_context_keywords)
        for emp in norm_employers
    )

    cert_patterns = {"security+", "security plus", "cissp", "cism"}

    for entry in cfg_weak.get("patterns", []):
        pattern = _normalize(entry["pattern"])
        if not _pattern_in_text(pattern, norm_full_text):
            continue

        # Cert signals only count when there is government employment context
        if pattern in cert_patterns:
            if has_gov_context or has_employer_gov_context:
                signals.append({
                    "signal_type": "weak",
                    "description": entry["description"],
                    "weight": weight,
                    "matched_pattern": entry["pattern"],
                })
        else:
            signals.append({
                "signal_type": "weak",
                "description": entry["description"],
                "weight": weight,
                "matched_pattern": entry["pattern"],
            })

    return signals


# ---------------------------------------------------------------------------
# De-duplication: keep the highest-weight signal per matched concept
# ---------------------------------------------------------------------------

def _deduplicate_signals(signals: list[dict]) -> list[dict]:
    """
    Remove redundant signals for the same underlying concept.
    Strategy: if a strong/confirmed signal names the same employer or program
    that also triggered a weak/medium match, drop the weaker duplicate.
    This prevents double-counting, e.g. "NSA" triggering both a strong
    ic_agencies hit and a weak 'government contractor' pattern hit.
    """
    # Build a set of matched_patterns from higher tiers
    tier_order = {"confirmed": 0, "strong": 1, "medium": 2, "weak": 3}
    seen: dict[str, dict] = {}

    for sig in signals:
        key = sig["matched_pattern"].lower()
        if key not in seen:
            seen[key] = sig
        else:
            existing = seen[key]
            if tier_order.get(sig["signal_type"], 99) < tier_order.get(existing["signal_type"], 99):
                seen[key] = sig

    # Secondary dedup: if a weak 'government contractor' fired but we already have
    # strong employer signals, suppress the generic weak ones to avoid noise.
    strong_confirmed_count = sum(
        1 for s in seen.values() if s["signal_type"] in ("confirmed", "strong")
    )
    generic_weak_patterns = {
        "government contractor", "federal contractor", "dod contractor",
    }
    if strong_confirmed_count > 0:
        seen = {
            k: v for k, v in seen.items()
            if not (
                v["signal_type"] == "weak"
                and v["matched_pattern"].lower() in generic_weak_patterns
            )
        }

    return list(seen.values())


# ---------------------------------------------------------------------------
# Main public API
# ---------------------------------------------------------------------------

def infer_clearance(
    candidate_text: str,
    employer_history: list,
    role_titles: list,
    config_path: str,
) -> dict:
    """
    Infer a candidate's likely security clearance level from profile signals.

    Parameters
    ----------
    candidate_text : str
        Full free-text profile (bio, summary, skills section, etc.).
    employer_history : list
        List of employer records. Each element may be a dict (with keys such as
        'name', 'title', 'start', 'end', 'years', 'description') or a plain str.
    role_titles : list
        List of role/position title strings drawn from the candidate's history.
    config_path : str
        Absolute or relative path to clearance_signals.yaml.

    Returns
    -------
    dict with keys:
        clearance_inference_level : str
            One of: "confirmed", "probable", "possible", "unconfirmed"
        clearance_signals_found : list of dict
            Each dict: {signal_type, description, weight, matched_pattern}
        total_signal_weight : float
    """
    config = _load_config(config_path)
    thresholds: dict[str, float] = config.get("thresholds", {})
    tiers: dict[str, Any] = config.get("signal_tiers", {})

    # Build combined normalised search surface
    norm_profile = _normalize(candidate_text)

    # Flatten employer history to normalised strings for text search
    def _emp_to_str(emp: Any) -> str:
        if isinstance(emp, dict):
            return " ".join(str(v) for v in emp.values())
        return str(emp)

    norm_employers = _normalize_list([_emp_to_str(e) for e in employer_history])
    norm_titles = _normalize_list(role_titles)

    # Full combined text: profile + all employer strings + all role titles
    norm_full_text = " ".join(
        [norm_profile] + norm_employers + norm_titles
    )

    # Collect raw signals from each tier
    all_signals: list[dict] = []

    cfg_confirmed = tiers.get("confirmed", {})
    if cfg_confirmed:
        all_signals.extend(_detect_confirmed_signals(norm_full_text, cfg_confirmed))

    cfg_strong = tiers.get("strong", {})
    if cfg_strong:
        all_signals.extend(
            _detect_strong_signals(
                norm_full_text, norm_employers, norm_titles, cfg_strong
            )
        )

    cfg_medium = tiers.get("medium", {})
    if cfg_medium:
        all_signals.extend(
            _detect_medium_signals(norm_full_text, employer_history, cfg_medium)
        )

    cfg_weak = tiers.get("weak", {})
    if cfg_weak:
        all_signals.extend(
            _detect_weak_signals(norm_full_text, norm_employers, cfg_weak)
        )

    # De-duplicate and compute total weight
    deduped = _deduplicate_signals(all_signals)
    total_weight: float = round(sum(s["weight"] for s in deduped), 4)

    # Determine confirmed-tier contribution separately
    confirmed_tier_weight: float = sum(
        s["weight"] for s in deduped if s["signal_type"] == "confirmed"
    )

    # Apply thresholds
    thresh_confirmed = float(thresholds.get("confirmed", 1.0))
    thresh_probable = float(thresholds.get("probable", 1.4))
    thresh_possible = float(thresholds.get("possible", 0.7))

    if confirmed_tier_weight >= thresh_confirmed:
        level = "confirmed"
    elif total_weight >= thresh_probable:
        level = "probable"
    elif total_weight >= thresh_possible:
        level = "possible"
    else:
        level = "unconfirmed"

    # Return only the public-facing fields (drop internal matched_pattern if
    # callers don't need it — kept here for debuggability)
    return {
        "clearance_inference_level": level,
        "clearance_signals_found": [
            {
                "signal_type": s["signal_type"],
                "description": s["description"],
                "weight": s["weight"],
            }
            for s in deduped
        ],
        "total_signal_weight": total_weight,
    }


# ---------------------------------------------------------------------------
# CLI smoke-test (python clearance_inference.py)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import json
    import os

    _this_dir = Path(__file__).parent
    _config_path = _this_dir.parent / "config" / "clearance_signals.yaml"

    _test_cases = [
        {
            "label": "Confirmed TS/SCI with poly",
            "text": "Holds active TS/SCI with full scope polygraph. Experienced SIGINT analyst.",
            "employers": [{"name": "Booz Allen Hamilton", "years": 6}],
            "titles": ["Senior SIGINT Analyst"],
        },
        {
            "label": "Probable (strong IC employer + titles, no explicit clearance)",
            "text": "Worked on classified programs supporting national security objectives.",
            "employers": [
                {"name": "NSA", "years": 4},
                {"name": "SAIC", "years": 3},
            ],
            "titles": ["All Source Analyst", "Intelligence Operations Specialist"],
        },
        {
            "label": "Possible (single cleared contractor, SCIF mention)",
            "text": "Worked in a SCIF environment supporting DoD customers on SIPRNet systems.",
            "employers": [{"name": "Leidos", "years": 2}],
            "titles": ["Systems Engineer"],
        },
        {
            "label": "Unconfirmed (no clearance signals)",
            "text": "Software engineer with 5 years of experience in Python and cloud platforms.",
            "employers": [{"name": "Accenture", "years": 5}],
            "titles": ["Software Engineer"],
        },
    ]

    for case in _test_cases:
        result = infer_clearance(
            candidate_text=case["text"],
            employer_history=case["employers"],
            role_titles=case["titles"],
            config_path=str(_config_path),
        )
        print(f"\n--- {case['label']} ---")
        print(f"  Level  : {result['clearance_inference_level']}")
        print(f"  Weight : {result['total_signal_weight']}")
        for sig in result["clearance_signals_found"]:
            print(f"    [{sig['signal_type']:9s}] {sig['description']} (+{sig['weight']})")
