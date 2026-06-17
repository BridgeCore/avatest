"""
results_writer.py — Final step of every dod-ic-recruiter run.

Usage:
    python scripts/results_writer.py '<json_string>'

where json_string is passed as sys.argv[1].
"""

import json
import sys
import shutil
from pathlib import Path
from datetime import datetime, timezone


def main() -> int:
    if len(sys.argv) < 2:
        print("Error: missing required argument <json_string>", file=sys.stderr)
        return 1

    # --- THING 1: Write session/last_results.json ---
    try:
        data = json.loads(sys.argv[1])
    except json.JSONDecodeError as exc:
        print(f"Error: failed to parse JSON argument — {exc}", file=sys.stderr)
        return 1

    if "run_at" not in data:
        print("Error: JSON is missing required field 'run_at'", file=sys.stderr)
        return 1
    if "candidates" not in data or not isinstance(data["candidates"], list):
        print("Error: JSON is missing required field 'candidates' (must be a list)", file=sys.stderr)
        return 1

    try:
        session_dir = Path("session")
        session_dir.mkdir(parents=True, exist_ok=True)

        last_results_path = session_dir / "last_results.json"
        last_results_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print("Results written to session/last_results.json — dashboard will update automatically.")
    except Exception as exc:
        print(f"Error writing session/last_results.json — {exc}", file=sys.stderr)
        return 1

    # --- THING 2: Archive session/current_search.json ---
    try:
        current_search_path = session_dir / "current_search.json"
        if current_search_path.exists():
            processed_dir = session_dir / "processed"
            processed_dir.mkdir(parents=True, exist_ok=True)

            timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            archive_path = processed_dir / f"current_search_{timestamp}.json"

            shutil.copy2(current_search_path, archive_path)
            current_search_path.unlink()
            print("Search input archived to session/processed/")
    except Exception as exc:
        print(f"Error archiving session/current_search.json — {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
