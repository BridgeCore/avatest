"""
iCIMS CSV import handler for the dod-ic-recruiter skill.

Scans the imports/ folder for CSV files, parses them using the column mapping
defined in config/icims_columns.yaml, feeds candidates into the shared
inference/scoring pipeline, and moves processed files to imports/processed/.

If an iCIMS candidate matches an existing data-store record by name+employer,
the records are merged and iCIMS data is preferred as more structured.
"""

import csv
import shutil
from datetime import datetime
from pathlib import Path

import yaml

from scripts.deduplicator import CandidateRaw


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def scan_imports(imports_dir: str) -> list[tuple[str, str]]:
    """Return a list of (filename, filepath) tuples for every .csv found in
    *imports_dir*.  Sub-directories (e.g. processed/) are intentionally
    skipped so already-handled files are not re-surfaced.

    Args:
        imports_dir: Absolute or relative path to the imports folder.

    Returns:
        Sorted list of (filename, filepath) tuples — one entry per CSV file
        found directly inside *imports_dir* (non-recursive).
    """
    base = Path(imports_dir)
    if not base.exists():
        return []

    results: list[tuple[str, str]] = []
    for entry in base.iterdir():
        if entry.is_file() and entry.suffix.lower() == ".csv":
            results.append((entry.name, str(entry.resolve())))

    return sorted(results, key=lambda t: t[0])


def parse_icims_csv(filepath: str, column_config: dict) -> list[CandidateRaw]:
    """Parse a single iCIMS-exported CSV file into a list of CandidateRaw
    objects ready for the shared inference/scoring pipeline.

    The *column_config* dict is expected to match the structure produced by
    loading config/icims_columns.yaml::

        {
            "column_mappings": {
                "name": "Candidate Name",
                "current_title": "Current Title",
                "current_employer": "Current Employer",
                "skills": "Skills",
                "skills_delimiter": ";",
                "years_experience": "Years of Experience",
                "location": "Location",
                "application_date": "Application Date",
                "requisition_id": "Req ID",
            }
        }

    Fields that are absent from a row are silently set to ``None``/``[]``.

    Args:
        filepath:      Path to the CSV file to parse.
        column_config: Parsed content of icims_columns.yaml.

    Returns:
        List of CandidateRaw instances, one per non-header row.
    """
    mappings: dict = column_config.get("column_mappings", {})
    skills_delimiter: str = mappings.get("skills_delimiter", ";")

    # Convenience helper — look up the CSV header name for a logical field.
    def col(key: str) -> str | None:
        return mappings.get(key)

    candidates: list[CandidateRaw] = []

    with open(filepath, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            # -- core identity fields -------------------------------------------
            name: str = _get(row, col("name"), "")
            current_title: str = _get(row, col("current_title"), "")
            current_employer: str = _get(row, col("current_employer"), "")
            location: str = _get(row, col("location"), "")

            # -- skills (delimited string -> list) --------------------------------
            raw_skills: str = _get(row, col("skills"), "")
            skills: list[str] = (
                [s.strip() for s in raw_skills.split(skills_delimiter) if s.strip()]
                if raw_skills
                else []
            )

            # -- numeric fields ---------------------------------------------------
            years_exp_raw: str = _get(row, col("years_experience"), "")
            years_experience: float | None = _parse_float(years_exp_raw)

            # -- metadata ---------------------------------------------------------
            application_date: str = _get(row, col("application_date"), "")
            requisition_id: str = _get(row, col("requisition_id"), "")

            candidate = CandidateRaw(
                name=name,
                current_title=current_title,
                current_employer=current_employer,
                skills=skills,
                years_experience=years_experience,
                location=location,
                application_date=application_date,
                requisition_id=requisition_id,
                source_platform="icims_import",
                raw_row=dict(row),  # preserve original for audit/merge
            )
            candidates.append(candidate)

    return candidates


def move_to_processed(filepath: str, processed_dir: str) -> str:
    """Move a processed CSV to *processed_dir* with a UTC timestamp appended
    to the stem so files never collide.

    Example::

        imports/candidates.csv  ->  imports/processed/candidates_20260617T143022Z.csv

    Args:
        filepath:      Path of the CSV file to move.
        processed_dir: Destination directory (created if absent).

    Returns:
        The absolute path of the file at its new location.
    """
    src = Path(filepath)
    dest_dir = Path(processed_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    new_name = f"{src.stem}_{timestamp}{src.suffix}"
    dest = dest_dir / new_name

    shutil.move(str(src), str(dest))
    return str(dest.resolve())


# ---------------------------------------------------------------------------
# Column-config loader (convenience — callers may use this directly)
# ---------------------------------------------------------------------------

def load_column_config(config_path: str) -> dict:
    """Load and return the parsed contents of icims_columns.yaml.

    Args:
        config_path: Path to config/icims_columns.yaml (or equivalent).

    Returns:
        Parsed YAML as a plain dict.
    """
    with open(config_path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


# ---------------------------------------------------------------------------
# Merge helper (name + employer deduplication against existing store)
# ---------------------------------------------------------------------------

def merge_with_existing(
    incoming: list[CandidateRaw],
    existing: list[CandidateRaw],
) -> list[CandidateRaw]:
    """Merge *incoming* iCIMS candidates with *existing* data-store records.

    Matching key: normalised (name, current_employer) pair (case-insensitive,
    whitespace-collapsed).  When a match is found the existing record is
    updated in-place with all non-empty fields from the iCIMS record, which
    is treated as the more structured authoritative source.

    Records in *incoming* that have no match in *existing* are appended as
    new entries.

    Args:
        incoming: Candidates parsed from the iCIMS CSV.
        existing: Current data-store candidate list.

    Returns:
        Updated list containing all existing records (merged where relevant)
        plus any genuinely new iCIMS candidates.
    """
    def key(c: CandidateRaw) -> tuple[str, str]:
        return (
            _normalise(c.name),
            _normalise(c.current_employer),
        )

    existing_index: dict[tuple[str, str], CandidateRaw] = {
        key(c): c for c in existing
    }

    new_candidates: list[CandidateRaw] = []

    for candidate in incoming:
        k = key(candidate)
        if k in existing_index:
            _merge_into(existing_index[k], candidate)
        else:
            new_candidates.append(candidate)

    return existing + new_candidates


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------

def _get(row: dict, header: str | None, default: str) -> str:
    """Return the value for *header* in *row*, or *default* if the header is
    None or absent."""
    if header is None:
        return default
    return (row.get(header) or "").strip() or default


def _parse_float(value: str) -> float | None:
    """Convert a string to float; return None if conversion fails."""
    if not value:
        return None
    try:
        return float(value.strip())
    except ValueError:
        return None


def _normalise(text: str) -> str:
    """Lower-case and collapse whitespace for loose matching."""
    return " ".join(text.lower().split())


def _merge_into(target: CandidateRaw, source: CandidateRaw) -> None:
    """Overwrite fields on *target* with non-empty values from *source* (iCIMS
    data preferred).  The ``raw_row`` and ``source_platform`` fields on the
    merged record are updated to reflect the iCIMS origin."""
    if source.current_title:
        target.current_title = source.current_title
    if source.current_employer:
        target.current_employer = source.current_employer
    if source.skills:
        target.skills = source.skills
    if source.years_experience is not None:
        target.years_experience = source.years_experience
    if source.location:
        target.location = source.location
    if source.application_date:
        target.application_date = source.application_date
    if source.requisition_id:
        target.requisition_id = source.requisition_id

    # Mark the record as having been touched by an iCIMS import.
    target.source_platform = "icims_import"
    if source.raw_row:
        target.raw_row = source.raw_row
