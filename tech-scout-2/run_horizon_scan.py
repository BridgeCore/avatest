"""Horizon scan orchestrator.

Reads a JSON config written by the Claude skill, runs all active scrapers
in parallel across every subtopic, deduplicates results, and writes a
JSON results file for Claude to read and format into the final report.

Usage:
    python run_horizon_scan.py <path_to_scan_config.json>
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from scrapers.arxiv_search import search_arxiv
# from scrapers.sbir_search import search_sbir   # requires SBIR API token — register at https://www.sbir.gov/api
# from scrapers.uspto_search import search_uspto  # requires IT to whitelist search.patentsview.org
from scrapers.openweb_search import search_openweb

_SCRAPERS = {
    "arxiv": search_arxiv,
    # "sbir": search_sbir,
    # "uspto": search_uspto,
    "web": search_openweb,
}


def _deduplicate(items: list[dict], key: str) -> list[dict]:
    seen: set[str] = set()
    out = []
    for item in items:
        val = (item.get(key) or "").strip().lower()
        if val and val not in seen:
            seen.add(val)
            out.append(item)
        elif not val:
            out.append(item)
    return out


def run(config: dict) -> dict:
    technology = config["technology"]
    subtopics: list[str] = config.get("subtopics", [technology])

    buckets: dict[str, list[dict]] = {name: [] for name in _SCRAPERS}

    tasks = [
        (source_name, fn, subtopic)
        for subtopic in subtopics
        for source_name, fn in _SCRAPERS.items()
    ]

    print(f"\nScanning {len(subtopics)} subtopics across {len(_SCRAPERS)} sources "
          f"({len(tasks)} total queries)...\n")

    with ThreadPoolExecutor(max_workers=8) as pool:
        future_map = {
            pool.submit(fn, subtopic): (source_name, subtopic)
            for source_name, fn, subtopic in tasks
        }
        for future in as_completed(future_map):
            source_name, subtopic = future_map[future]
            try:
                results = future.result()
                buckets[source_name].extend(results)
                print(f"  [{source_name}] '{subtopic}' -> {len(results)} results")
            except Exception as e:
                print(f"  [{source_name}] '{subtopic}' -> ERROR: {e}")

    dedup_keys = {
        "arxiv": "title",
        # "sbir": "firm",
        # "uspto": "title",
        "web": "url",
    }
    for source_name, key in dedup_keys.items():
        before = len(buckets[source_name])
        buckets[source_name] = _deduplicate(buckets[source_name], key)
        after = len(buckets[source_name])
        if before != after:
            print(f"  [{source_name}] deduplicated {before} -> {after}")

    return {
        "technology": technology,
        "context": config.get("context", ""),
        "focus": config.get("focus", "both"),
        "geography": config.get("geography", "global"),
        "subtopics_searched": subtopics,
        "results": buckets,
        "counts": {name: len(items) for name, items in buckets.items()},
    }


def main():
    if len(sys.argv) < 2:
        print("Usage: python run_horizon_scan.py <scan_config.json>")
        sys.exit(1)

    config_path = Path(sys.argv[1])
    if not config_path.exists():
        print(f"Config file not found: {config_path}")
        sys.exit(1)

    config = json.loads(config_path.read_text(encoding="utf-8"))
    output = run(config)

    results_path = config_path.parent / "scan_results.json"
    results_path.write_text(json.dumps(output, indent=2, default=str), encoding="utf-8")

    counts = output["counts"]
    print(f"\nDone. Results written to: {results_path}")
    print(f"  arXiv papers : {counts.get('arxiv', 0)}")
    # print(f"  SBIR awards  : {counts.get('sbir', 0)}")   # disabled - needs API token
    # print(f"  USPTO patents: {counts.get('uspto', 0)}")  # disabled - needs network access
    print(f"  Web results  : {counts.get('web', 0)}")


if __name__ == "__main__":
    main()
