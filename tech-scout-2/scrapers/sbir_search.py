"""SBIR/STTR scraper — queries SBIR.gov awards API by keyword.

Source: https://api.www.sbir.gov (free, requires API token)
Adapted from WATCHTOWER_Horizon_Scanner SBIR adapter logic.

STATUS: Commented out in orchestrator — SBIR API requires a free auth token.
To enable: register at https://www.sbir.gov/api, then set the token in the
Authorization header below and uncomment this scraper in run_horizon_scan.py.
"""
import httpx
from urllib.parse import urlencode

_BASE = "https://api.www.sbir.gov/"
_PAGE_SIZE = 50


def search_sbir(keyword: str, max_results: int = 50) -> list[dict]:
    # SBIR API rejects long multi-term keywords; truncate to first 4 words
    short_keyword = " ".join(keyword.split()[:4])
    params = urlencode([
        ("keyword", short_keyword),
        ("rows", str(min(max_results, _PAGE_SIZE))),
        ("start", "0"),
    ])
    url = f"{_BASE}awards?{params}"

    try:
        resp = httpx.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"  [sbir] Error for '{keyword}': {e}")
        return []

    awards = data if isinstance(data, list) else data.get("results", data.get("awards", []))

    results = []
    for award in awards:
        if not isinstance(award, dict):
            continue
        firm = award.get("firm", "").strip()
        if not firm:
            continue
        solicitation = award.get("solicitation") or {}
        results.append({
            "firm": firm,
            "title": award.get("title", ""),
            "abstract": (award.get("abstract", "") or "")[:500],
            "year": award.get("award_year", ""),
            "agency": award.get("agency", ""),
            "topic_code": solicitation.get("topic_code", "") if isinstance(solicitation, dict) else "",
            "source": "sbir",
        })

    return results
