"""arXiv scraper — searches arXiv REST API by keyword, returns papers with author affiliations.

Source: https://arxiv.org (free, no auth required)
Adapted from WATCHTOWER_Horizon_Scanner arXiv adapter logic.
"""
import arxiv


def search_arxiv(query: str, max_results: int = 20) -> list[dict]:
    client = arxiv.Client(page_size=50, delay_seconds=1, num_retries=2)
    search = arxiv.Search(
        query=query,
        max_results=max_results,
        sort_by=arxiv.SortCriterion.Relevance,
    )

    results = []
    try:
        for paper in client.results(search):
            authors = [a.name for a in paper.authors[:5]]
            results.append({
                "title": paper.title,
                "authors": authors,
                "abstract": (paper.summary or "")[:500],
                "published": paper.published.isoformat() if paper.published else None,
                "url": paper.entry_id,
                "source": "arxiv",
            })
    except Exception as e:
        print(f"  [arxiv] Error for '{query}': {e}")

    return results
