"""
seekout_mcp.py — SeekOut MCP integration stub for dod-ic-recruiter.

Overview
--------
SeekOut is a talent intelligence platform purpose-built for cleared and
government recruiting.  It surfaces candidates who hold active or recent
security clearances, indexes defense-sector job history, and provides
structured profile data compliant with enterprise data-licensing agreements.

Claude Code as MCP client
--------------------------
This stub defines the **data contract** only — it does not implement the
transport layer.  When activated, Claude Code acts as the MCP client and
communicates with SeekOut's MCP server over the standard MCP protocol.
Authentication, session management, and JSON-RPC framing are handled by the
MCP runtime, not by this module.

Input schema
------------
The ``search()`` method receives query strings derived from the confirmed
SkillPicture produced during the skill-picture clarification phase.  Those
strings encode:

- Role keywords (job title, function, specialization)
- Location (city, state, metro area, or remote)
- Clearance level (e.g. "TS/SCI", "Secret", "Top Secret with poly")
- Domain context (e.g. "SIGINT", "GEOINT", "cyber", "all-source analysis")
- Experience range (e.g. "5-10 years", "senior", "entry-level")

Callers upstream convert the SkillPicture into these query strings using the
same query-builder logic already used by the Google and job-board scrapers.

Output mapping
--------------
SeekOut profile fields are mapped to ``CandidateRaw`` as follows:

    SeekOut field               -> CandidateRaw field
    --------------------------------------------------
    profile.fullName            -> name
    profile.profileUrl          -> source_url
    profile.currentTitle        -> current_title
    profile.currentEmployer     -> current_employer
    profile.location            -> location
    profile.skills[]            -> skills
    profile.contactEmail        -> email
    profile.rawBio + snippets   -> raw_text
    (scraped timestamp)         -> scraped_at
    "seekout"                   -> source_platform

Clearance signals
-----------------
SeekOut's own clearance-inference output (the ``clearanceInference`` object
returned in profile results) should be passed through verbatim into
``clearance_signals_found`` on the MergedCandidate produced downstream by
the deduplicator.  Do not filter or re-score SeekOut's clearance labels —
they are based on proprietary government-contract employment signals and are
more accurate than the regex-based inference in
``scripts/clearance_inference.py`` for profiles that originate from SeekOut.

LinkedIn data access
--------------------
SeekOut provides compliant, licensed access to LinkedIn-sourced profile data
through an enterprise licensing agreement.  This fully replaces the
``google_scraper.py`` LinkedIn-snippet workaround (source_platform =
"linkedin_snippet") once the integration is active.  Unlike the snippet
approach, SeekOut delivers structured field-level data — not just the name
and title visible in a Google SERP snippet — and does so without violating
LinkedIn's terms of service.

Activation checklist
--------------------
1. Obtain the MCP server URL from the SeekOut account team.
2. Add it to ``config/sources.yaml`` as:
       seekout_mcp_url: "https://<your-tenant>.seekout.io/mcp"
3. Place the bearer token provided by SeekOut in ``.env``:
       SEEKOUT_TOKEN=<token>
4. Toggle the integration on in ``config/sources.yaml``:
       seekout_mcp: true
5. The skill router in ``scripts/recruiter_agent.py`` checks
   ``sources.yaml`` at startup and will begin routing queries here
   automatically.
"""

from __future__ import annotations

import logging
from typing import List

from scripts.scrapers.base import BaseScraper
from scripts.deduplicator import CandidateRaw

logger = logging.getLogger(__name__)


class SeekOutMCPScraper(BaseScraper):
    """
    Stub implementation of the SeekOut MCP integration.

    Returns an empty list until the MCP server URL and auth token are
    configured and the integration is toggled on in sources.yaml.
    """

    source_platform: str = "seekout"

    def search(self, queries: List[str]) -> List[CandidateRaw]:
        """
        Query SeekOut via MCP for candidates matching each query string.

        This is a no-op stub.  When activated, each query string is forwarded
        to the SeekOut MCP server, and the response profiles are mapped to
        CandidateRaw records according to the output mapping in this module's
        docstring.

        Parameters
        ----------
        queries:
            Structured query strings derived from the confirmed SkillPicture.

        Returns
        -------
        list[CandidateRaw]
            Empty list (stub).  Will return matched candidate records once
            the integration is active.
        """
        logger.info(
            "SeekOutMCPScraper.search() called with %d queries — "
            "integration not yet active (stub). "
            "Configure seekout_mcp_url and SEEKOUT_TOKEN to enable.",
            len(queries),
        )
        return []
