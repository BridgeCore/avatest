"""
data_store.py — Candidate data persistence layer for dod-ic-recruiter.

Responsibilities:
  - Load all existing candidate JSON files from data/candidates/ at startup.
  - Provide a stable, deterministic candidate ID (SHA-256 of normalized key).
  - Merge incoming candidate data with stored data using a defined precedence:
      * Structured data beats scraped data.
      * Newer scraped_at timestamp wins otherwise.
      * The `notes` field is SACRED — never overwritten under any circumstances.
  - Flush all new/updated candidates back to data/candidates/<id>.json on save.
  - Maintain job_skill_memory.json in the skill root (append-only).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

# Skill root is one level above this file (dod-ic-recruiter/)
_SKILL_ROOT: Path = Path(__file__).resolve().parent.parent
_CANDIDATES_DIR: Path = _SKILL_ROOT / "data" / "candidates"
_JOB_SKILL_MEMORY_PATH: Path = _SKILL_ROOT / "job_skill_memory.json"

# ---------------------------------------------------------------------------
# Schema helpers
# ---------------------------------------------------------------------------

CANDIDATE_SCHEMA: dict[str, Any] = {
    "id": "",
    "name": "",
    "sources_found": [],
    "primary_source_url": "",
    "scraped_at": "",
    "raw_text": "",
    "explicit_skills": [],
    "inferred_skills": [],        # list of {"skill","source","confidence","justification"}
    "skill_gaps": [],
    "clearance_inference_level": "",
    "clearance_signals_found": [],
    "last_match_score": None,
    "last_match_dimensions": {},
    "last_matched_jd_hash": "",
    "last_skill_picture_version": "",
    "recruiter_flags": [],
    "icims_metadata": {"application_date": "", "requisition_id": ""},
    "notes": "",
}


def _blank_candidate() -> dict[str, Any]:
    """Return a deep copy of the blank schema so callers get independent dicts."""
    import copy
    return copy.deepcopy(CANDIDATE_SCHEMA)


# ---------------------------------------------------------------------------
# ID generation
# ---------------------------------------------------------------------------

def _normalize_name(name: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation for stable hashing."""
    name = name.lower().strip()
    name = re.sub(r"[^\w\s]", "", name)          # drop punctuation
    name = re.sub(r"\s+", " ", name)              # collapse whitespace
    return name


def make_candidate_id(name: str, source_url: str = "", current_employer: str = "") -> str:
    """
    Compute a stable SHA-256 candidate ID.

    For candidates with a source URL:
        SHA256(normalized_name + "|" + source_url.strip())

    For iCIMS-only candidates (no URL):
        SHA256(normalized_name + "|employer:" + normalized_employer)
    """
    norm_name = _normalize_name(name)
    if source_url and source_url.strip():
        key = f"{norm_name}|{source_url.strip()}"
    else:
        norm_employer = _normalize_name(current_employer) if current_employer else ""
        key = f"{norm_name}|employer:{norm_employer}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Timestamp utilities
# ---------------------------------------------------------------------------

def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO-8601 timestamp string; returns None on failure."""
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# DataStore
# ---------------------------------------------------------------------------

class DataStore:
    """
    In-memory candidate store backed by per-candidate JSON files.

    Usage pattern::

        store = DataStore()
        store.load()
        # ... manipulate candidates via upsert() ...
        store.save()
    """

    def __init__(
        self,
        candidates_dir: Path = _CANDIDATES_DIR,
        skill_memory_path: Path = _JOB_SKILL_MEMORY_PATH,
    ) -> None:
        self._candidates_dir = candidates_dir
        self._skill_memory_path = skill_memory_path
        # id -> candidate dict
        self._store: dict[str, dict[str, Any]] = {}
        # ids that were created or modified since load()
        self._dirty: set[str] = set()

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def load(self) -> int:
        """
        Read all *.json files from the candidates directory into memory.

        Returns the number of candidates loaded.
        """
        self._candidates_dir.mkdir(parents=True, exist_ok=True)
        loaded = 0
        for json_file in sorted(self._candidates_dir.glob("*.json")):
            try:
                with json_file.open("r", encoding="utf-8") as fh:
                    candidate = json.load(fh)
                cid = candidate.get("id")
                if not cid:
                    logger.warning("Candidate file %s has no 'id' field; skipping.", json_file)
                    continue
                self._store[cid] = candidate
                loaded += 1
            except (json.JSONDecodeError, OSError) as exc:
                logger.error("Failed to load candidate file %s: %s", json_file, exc)
        logger.info("DataStore.load: loaded %d candidate(s) from %s", loaded, self._candidates_dir)
        return loaded

    # ------------------------------------------------------------------
    # Upsert / Merge
    # ------------------------------------------------------------------

    def upsert(self, incoming: dict[str, Any]) -> dict[str, Any]:
        """
        Insert or merge an incoming candidate dict into the store.

        Merge rules:
          1. If the candidate does not exist yet, insert a fully-hydrated copy.
          2. If it exists:
             a. Structured-data fields beat any scraped value (see _is_structured).
             b. For scraped fields, prefer the newer scraped_at timestamp.
             c. sources_found is union-merged (no duplicates).
             d. notes is NEVER overwritten.
          3. Mark the candidate dirty so it will be flushed on save().

        Returns the post-merge stored candidate.
        """
        cid = incoming.get("id")
        if not cid:
            raise ValueError("Incoming candidate dict must have a non-empty 'id' field.")

        if cid not in self._store:
            # New candidate — hydrate with schema defaults then overlay incoming
            record = _blank_candidate()
            record.update({k: v for k, v in incoming.items() if v not in (None, "", [], {})})
            record["id"] = cid
            self._store[cid] = record
            self._dirty.add(cid)
            logger.debug("DataStore.upsert: inserted new candidate %s (%s)", cid, record.get("name"))
            return record

        # Existing candidate — merge
        stored = self._store[cid]
        stored_scraped_at = _parse_iso(stored.get("scraped_at", ""))
        incoming_scraped_at = _parse_iso(incoming.get("scraped_at", ""))

        incoming_is_newer_scrape = (
            incoming_scraped_at is not None
            and (stored_scraped_at is None or incoming_scraped_at > stored_scraped_at)
        )
        incoming_is_structured = incoming.get("_data_source") == "structured"
        stored_is_structured = stored.get("_data_source") == "structured"

        changed = False

        for field, incoming_val in incoming.items():
            if field == "notes":
                # SACRED — never overwrite
                continue

            if field == "id":
                # ID is immutable
                continue

            if field == "sources_found":
                # Union merge
                merged_sources = list(stored.get("sources_found") or [])
                for src in incoming_val or []:
                    if src not in merged_sources:
                        merged_sources.append(src)
                if merged_sources != stored.get("sources_found"):
                    stored["sources_found"] = merged_sources
                    changed = True
                continue

            if field == "icims_metadata":
                # Merge sub-dict; prefer non-empty values
                stored_meta = stored.get("icims_metadata") or {}
                incoming_meta = incoming_val or {}
                for k, v in incoming_meta.items():
                    if v and not stored_meta.get(k):
                        stored_meta[k] = v
                        changed = True
                stored["icims_metadata"] = stored_meta
                continue

            if field == "last_match_dimensions":
                # Structured field — overwrite with incoming if structured or newer scrape
                if incoming_val and (incoming_is_structured or incoming_is_newer_scrape):
                    if stored.get(field) != incoming_val:
                        stored[field] = incoming_val
                        changed = True
                continue

            # General field merge logic
            current_val = stored.get(field)

            # Prefer structured data over scraped
            if incoming_is_structured and not stored_is_structured:
                if incoming_val not in (None, "", [], {}):
                    if current_val != incoming_val:
                        stored[field] = incoming_val
                        changed = True
                continue

            if stored_is_structured and not incoming_is_structured:
                # Stored is structured; only accept incoming if it provides a value we don't have
                if current_val in (None, "", [], {}) and incoming_val not in (None, "", [], {}):
                    stored[field] = incoming_val
                    changed = True
                continue

            # Both same type — prefer newer scrape timestamp
            if incoming_is_newer_scrape:
                if incoming_val not in (None, "", [], {}) and current_val != incoming_val:
                    stored[field] = incoming_val
                    changed = True
            else:
                # Incoming is older/same; only fill gaps
                if current_val in (None, "", [], {}) and incoming_val not in (None, "", [], {}):
                    stored[field] = incoming_val
                    changed = True

        if changed:
            self._dirty.add(cid)
            logger.debug(
                "DataStore.upsert: updated candidate %s (%s)",
                cid,
                stored.get("name"),
            )

        return stored

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    def get(self, candidate_id: str) -> dict[str, Any] | None:
        """Return the stored candidate dict or None if not found."""
        return self._store.get(candidate_id)

    def all_candidates(self) -> list[dict[str, Any]]:
        """Return all stored candidates as a list."""
        return list(self._store.values())

    def count(self) -> int:
        """Return the total number of candidates in the store."""
        return len(self._store)

    def set_note(self, candidate_id: str, note_text: str) -> bool:
        """
        The ONLY permitted way to update the notes field.
        Returns True if the candidate was found and the note set.
        """
        if candidate_id not in self._store:
            return False
        self._store[candidate_id]["notes"] = note_text
        self._dirty.add(candidate_id)
        return True

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save(self) -> int:
        """
        Flush all dirty candidates to <candidates_dir>/<id>.json.

        Returns the number of files written.
        """
        self._candidates_dir.mkdir(parents=True, exist_ok=True)
        written = 0
        for cid in self._dirty:
            candidate = self._store.get(cid)
            if candidate is None:
                continue
            out_path = self._candidates_dir / f"{cid}.json"
            try:
                with out_path.open("w", encoding="utf-8") as fh:
                    json.dump(candidate, fh, indent=2, ensure_ascii=False)
                written += 1
            except OSError as exc:
                logger.error("DataStore.save: failed to write %s: %s", out_path, exc)
        self._dirty.clear()
        logger.info("DataStore.save: wrote %d candidate file(s).", written)
        return written

    # ------------------------------------------------------------------
    # Job skill memory
    # ------------------------------------------------------------------

    def update_job_skill_memory(
        self,
        kept_skills: list[str] | None = None,
        added_skills: list[str] | None = None,
        removed_skills: list[str] | None = None,
        enrichment_confirmed_skills: list[dict[str, str]] | None = None,
    ) -> None:
        """
        Update job_skill_memory.json in the skill root.

        Parameters
        ----------
        kept_skills:
            Skill names the recruiter reviewed and kept.  occurrence_count is
            incremented, last_seen updated, source set to "recruiter_confirmed".

        added_skills:
            Brand-new skill names the recruiter typed in.  Added as new entries
            with source "recruiter_added".

        removed_skills:
            Skill names the recruiter removed from the suggestion.  The entry is
            NOT deleted — removed_count is incremented instead.

        enrichment_confirmed_skills:
            List of dicts, each with keys "skill" and "source" (e.g.
            "clearancejobs_enrichment" or "usajobs_enrichment") for skills
            surfaced by an external enrichment pass that the recruiter confirmed.
        """
        memory = self._load_skill_memory()
        now_iso = _utcnow_iso()

        # Index existing entries by skill name for fast lookup
        index: dict[str, dict[str, Any]] = {
            entry["skill"]: entry for entry in memory.get("skills", [])
        }

        # kept
        for skill_name in (kept_skills or []):
            entry = index.get(skill_name)
            if entry is None:
                entry = _new_skill_entry(skill_name, "recruiter_confirmed", now_iso)
                index[skill_name] = entry
            else:
                entry["occurrence_count"] = int(entry.get("occurrence_count", 0)) + 1
                entry["last_seen"] = now_iso
                entry["source"] = "recruiter_confirmed"

        # added
        for skill_name in (added_skills or []):
            entry = index.get(skill_name)
            if entry is None:
                entry = _new_skill_entry(skill_name, "recruiter_added", now_iso)
                index[skill_name] = entry
            else:
                # Already exists — treat as confirmed and bump count
                entry["occurrence_count"] = int(entry.get("occurrence_count", 0)) + 1
                entry["last_seen"] = now_iso
                entry["source"] = "recruiter_added"

        # removed — never delete, just bump removed_count
        for skill_name in (removed_skills or []):
            entry = index.get(skill_name)
            if entry is None:
                entry = _new_skill_entry(skill_name, "recruiter_removed", now_iso)
                entry["removed_count"] = 1
                index[skill_name] = entry
            else:
                entry["removed_count"] = int(entry.get("removed_count", 0)) + 1
                entry["last_seen"] = now_iso

        # enrichment confirmed
        for item in (enrichment_confirmed_skills or []):
            skill_name = item.get("skill", "").strip()
            source_tag = item.get("source", "enrichment_confirmed").strip()
            if not skill_name:
                continue
            entry = index.get(skill_name)
            if entry is None:
                entry = _new_skill_entry(skill_name, source_tag, now_iso)
                index[skill_name] = entry
            else:
                entry["occurrence_count"] = int(entry.get("occurrence_count", 0)) + 1
                entry["last_seen"] = now_iso
                # Only upgrade the source tag if it wasn't already recruiter-touched
                if entry.get("source") not in ("recruiter_confirmed", "recruiter_added"):
                    entry["source"] = source_tag

        # Rebuild the skills list (preserving insertion order via dict ordering)
        memory["skills"] = list(index.values())
        memory["last_updated"] = now_iso

        try:
            with self._skill_memory_path.open("w", encoding="utf-8") as fh:
                json.dump(memory, fh, indent=2, ensure_ascii=False)
        except OSError as exc:
            logger.error(
                "DataStore.update_job_skill_memory: failed to write %s: %s",
                self._skill_memory_path,
                exc,
            )
            return

        logger.info(
            "DataStore.update_job_skill_memory: wrote %d skill entries to %s",
            len(index),
            self._skill_memory_path,
        )

    def _load_skill_memory(self) -> dict[str, Any]:
        """Load job_skill_memory.json, returning a safe default if missing or corrupt."""
        if not self._skill_memory_path.exists():
            return {"skills": [], "role_family_domain_pairs": []}
        try:
            with self._skill_memory_path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if not isinstance(data, dict):
                logger.warning(
                    "job_skill_memory.json has unexpected type %s; resetting.",
                    type(data).__name__,
                )
                return {"skills": [], "role_family_domain_pairs": []}
            # Ensure the skills key exists
            if "skills" not in data:
                data["skills"] = []
            return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.error("Failed to load job_skill_memory.json: %s; starting fresh.", exc)
            return {"skills": [], "role_family_domain_pairs": []}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _new_skill_entry(skill_name: str, source: str, now_iso: str) -> dict[str, Any]:
    return {
        "skill": skill_name,
        "occurrence_count": 1,
        "removed_count": 0,
        "source": source,
        "first_seen": now_iso,
        "last_seen": now_iso,
    }


# ---------------------------------------------------------------------------
# Module-level convenience functions
# ---------------------------------------------------------------------------

def load_candidates(candidates_dir: Path = _CANDIDATES_DIR) -> DataStore:
    """
    Convenience factory: create a DataStore, load it, and return it.

    Example::

        store = load_candidates()
        # work with store ...
        store.save()
    """
    store = DataStore(candidates_dir=candidates_dir)
    store.load()
    return store
