"""USPTO scraper — queries PatentsView API for patents matching a technology keyword.

Source: https://search.patentsview.org (free, no auth required)
Adapted from WATCHTOWER_Horizon_Scanner USPTO adapter logic.

STATUS: Commented out in orchestrator — search.patentsview.org is blocked by
BCore corporate network DNS. To enable: ask IT to whitelist search.patentsview.org,
then uncomment this scraper in run_horizon_scan.py.
"""
import httpx

_API_URL = "https://search.patentsview.org/api/v1/patent/"


def search_uspto(query: str, max_results: int = 25) -> list[dict]:
    payload = {
        "q": {"_text_any": {"patent_title": query, "patent_abstract": query}},
        "f": [
            "patent_title",
            "patent_date",
            "patent_abstract",
            "assignees.assignee_organization",
        ],
        "o": {"per_page": max_results, "matched_subentities_only": True},
    }

    try:
        resp = httpx.post(_API_URL, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [uspto] Error for '{query}': {e}")
        return []

    patents = data.get("patents") or []
    results = []
    for patent in patents:
        assignees = patent.get("assignees") or []
        orgs = [
            a.get("assignee_organization", "")
            for a in assignees
            if a.get("assignee_organization")
        ]
        results.append({
            "title": patent.get("patent_title", ""),
            "assignee": ", ".join(orgs) if orgs else "Individual/Unassigned",
            "date": patent.get("patent_date", ""),
            "abstract": (patent.get("patent_abstract", "") or "")[:300],
            "source": "uspto",
        })

    return [r for r in results if r["title"]]
