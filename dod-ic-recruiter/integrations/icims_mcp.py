"""
icims_mcp.py — iCIMS MCP integration stub for dod-ic-recruiter.

Overview
--------
This module is the planned live MCP connection to iCIMS — the enterprise ATS
already in use at BCore.  When active, it is a direct replacement for the
manual CSV export workflow currently implemented in
``scripts/icims_import.py``.

Current state vs. future state
-------------------------------
Today, the recruiter must:
  1. Log in to the iCIMS portal.
  2. Run a candidate search or pull a requisition.
  3. Export results as a CSV.
  4. Drop the CSV into the ``imports/`` directory.
  5. The skill then picks it up via ``scripts/icims_import.py``.

When this integration is active, none of those manual steps are needed.
The skill queries iCIMS directly, in real time, using the iCIMS MCP server.
Results arrive as structured records and are mapped to ``CandidateRaw``
without any intermediate file.

Claude Code as MCP client
--------------------------
This stub defines the **data contract** only.  Claude Code acts as the MCP
client; the iCIMS MCP server handles authentication and data retrieval.
The transport layer (JSON-RPC over HTTP, session tokens, pagination) is
managed by the MCP runtime, not by this module.

Input schema
------------
The ``search()`` method accepts query strings derived from the confirmed
SkillPicture.  In practice, callers may also pass a requisition ID directly
as the sole query string (e.g. ``["REQ-00412"]``) to pull all candidates
already associated with an open requisition in iCIMS.  Both modes are
supported by the iCIMS MCP API.

Search-based input fields (when not using a requisition ID):
- Keywords (job title, required skills, certifications)
- Location or work-site code
- Clearance level (mapped to iCIMS custom field ``cf_clearance_level``)
- Workflow status filter (e.g. "Active", "New Applicant", "In Process")

Output mapping
--------------
iCIMS candidate record fields are mapped to ``CandidateRaw`` as follows:

    iCIMS field                   -> CandidateRaw field
    -----------------------------------------------------
    Person.formattedName          -> name
    Person.profileUrl (ATS link)  -> source_url
    Person.currentTitle           -> current_title
    Person.currentEmployer        -> current_employer
    Person.addresses[0].formatted -> location
    Person.skills[]               -> skills
    Person.email                  -> email
    Person.resumeText             -> raw_text
    (scraped timestamp)           -> scraped_at
    "icims"                       -> source_platform

Clearance data present in ``cf_clearance_level`` should be appended to
``raw_text`` so that ``scripts/clearance_inference.py`` can process it
alongside the rest of the candidate text.

Activation checklist
--------------------
1. Obtain the iCIMS MCP server URL from the iCIMS account team (available
   through the iCIMS Marketplace or enterprise support).
2. Add it to ``config/sources.yaml`` as:
       icims_mcp_url: "https://<tenant>.icims.com/mcp"
3. Place the credential (API key or OAuth client secret) in ``.env``:
       ICIMS_TOKEN=<token>
4. Toggle the integration on in ``config/sources.yaml``:
       icims_mcp: true
5. Once active, the skill router will use this integration instead of
   watching the ``imports/`` directory for CSV drops.  The manual CSV
   workflow in ``scripts/icims_import.py`` remains available as a fallback
   but will not be invoked when ``icims_mcp: true``.
"""

from __future__ import annotations

import logging
from typing import List

from scripts.scrapers.base import BaseScraper
from scripts.deduplicator import CandidateRaw

logger = logging.getLogger(__name__)


class ICIMSMCPScraper(BaseScraper):
    """
    Stub implementation of the iCIMS MCP integration.

    Returns an empty list until the MCP server URL and auth credential are
    configured and the integration is toggled on in sources.yaml.
    """

    source_platform: str = "icims"

    def search(self, queries: List[str]) -> List[CandidateRaw]:
        """
        Query iCIMS via MCP for candidates matching each query string or
        requisition ID.

        This is a no-op stub.  When activated, queries are forwarded to the
        iCIMS MCP server, responses are mapped to CandidateRaw records, and
        the recruiter no longer needs to manually export and drop CSVs.

        Parameters
        ----------
        queries:
            Structured query strings derived from the confirmed SkillPicture,
            or a list containing a single requisition ID.

        Returns
        -------
        list[CandidateRaw]
            Empty list (stub).  Will return matched candidate records once
            the integration is active.
        """
        logger.info(
            "ICIMSMCPScraper.search() called with %d queries — "
            "integration not yet active (stub). "
            "Configure icims_mcp_url and ICIMS_TOKEN to enable.",
            len(queries),
        )
        return []
