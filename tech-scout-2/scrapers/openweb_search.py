"""Open web scraper — DuckDuckGo search via ddgs package.

Source: DuckDuckGo (free, no auth required)
Adapted from WATCHTOWER_Horizon_Scanner open web adapter logic.
"""
try:
    from ddgs import DDGS
except ImportError:
    from duckduckgo_search import DDGS


def search_openweb(query: str, max_results: int = 10) -> list[dict]:
    results = []
    try:
        with DDGS() as ddgs:
            for r in ddgs.text(query, max_results=max_results):
                results.append({
                    "title": r.get("title", ""),
                    "url": r.get("href", ""),
                    "snippet": r.get("body", "")[:400],
                    "source": "web",
                })
    except Exception as e:
        print(f"  [web] Error for '{query}': {e}")

    return results
