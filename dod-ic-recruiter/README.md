# DOD/IC Recruiting Assistant — dod-ic-recruiter Skill

## Overview

This is a Claude Code skill that helps defense and intelligence community recruiters source and evaluate cleared candidates. You give it a job description. It builds a complete picture of what a competitive candidate looks like, searches multiple sources, scores every candidate it finds, and presents ranked results ready for your review.

It is designed for cleared recruiting specifically — meaning it understands clearance inference, defense contractor career patterns, IC agency names, and the gap between what job descriptions say and what candidates actually look like in the real market.

You do not need to know how to code to use it. You need to be able to run a command in a terminal and edit a plain text file.

---

## Quick Start

1. Open Claude Code in this workspace.
2. Paste a job description into the chat and ask: "Find me candidates for this role" or "Run a candidate search."
3. Follow the prompts. The tool will ask you to review and confirm the Skill Picture before it starts searching.

That is all you need to do for a basic run.

---

## Setup

### Install dependencies

Run this once in a terminal from the `dod-ic-recruiter/` directory:

```
pip install -r requirements.txt
playwright install
```

### Create your .env file

Create a file named `.env` inside the `dod-ic-recruiter/` directory. This file holds your API keys. It is never committed to source control.

Copy the block below and fill in your values:

```
USAJOBS_EMAIL=your-email@example.com
GITHUB_TOKEN=
STACKOVERFLOW_KEY=
REDDIT_CLIENT_ID=
REDDIT_SECRET=
PDL_KEY=
SEEKOUT_TOKEN=
ICIMS_TOKEN=
```

Key-by-key explanation:

| Key | Required? | What it does | How to get it |
|---|---|---|---|
| `USAJOBS_EMAIL` | Required | Lets the tool query the USAJOBS API for official government postings. Free. | Use any email registered at usajobs.gov |
| `GITHUB_TOKEN` | Optional but recommended | Raises GitHub search from 10 to 60 requests per minute. Without it, the tool slows down significantly on GitHub sourcing. | github.com/settings/tokens — create a free personal access token with no scopes required |
| `STACKOVERFLOW_KEY` | Optional | Raises the Stack Overflow API rate limit. Free. | Register a free app at stackapps.com |
| `REDDIT_CLIENT_ID` | Optional | Required for Reddit API access. Reddit is useful for surfacing community-active practitioners in niche technical areas. | reddit.com/prefs/apps — create a "script" type app. Free. |
| `REDDIT_SECRET` | Optional | Pair with `REDDIT_CLIENT_ID`. Same registration step. | See above |
| `PDL_KEY` | Optional | People Data Labs enrichment. Used only for the top-ranked candidates after initial scoring — adds employer history and education data. 100 free calls per month. | peopledatalabs.com |
| `SEEKOUT_TOKEN` | Not yet active | Reserved for the SeekOut integration when activated. Leave blank. | See Future Integrations section |
| `ICIMS_TOKEN` | Not yet active | Reserved for the iCIMS live feed when activated. Leave blank. | See Future Integrations section |

---

## How the iCIMS CSV Import Works

Your existing iCIMS pipeline candidates can be scored alongside externally sourced candidates. Here is the process:

1. Log into iCIMS and run a candidate search or pull a requisition pipeline.
2. Export results as CSV. Include at minimum: Candidate Name, Current Title, Current Employer, Skills, Location. Additional columns are used if present.
3. Drop the CSV file into the `imports/` folder inside the `dod-ic-recruiter/` directory.
4. When you run the skill, it will detect the file and ask whether to include those candidates.
5. After processing, the file is automatically moved to `imports/processed/` with a timestamp added to the filename. It will not be imported again on future runs.
6. iCIMS candidates are scored using the exact same scoring logic as candidates found from any other source — no special treatment.

If your iCIMS export uses different column names than expected, see the `icims_columns.yaml` section below — you can remap columns without touching any code.

---

## Config Files Explained

The `config/` directory contains four YAML files you may want to adjust. You can open any of them in a text editor. They use simple `key: value` format.

### scoring_weights.yaml

Controls how candidates are scored. There are four dimensions and they must always add up to 1.0 (100%):

| Dimension | Default | What it measures |
|---|---|---|
| `skill_match` | 0.40 (40%) | How well the candidate's skills match what the role requires |
| `clearance_signal` | 0.25 (25%) | Strength of evidence that the candidate holds the required clearance |
| `experience_match` | 0.20 (20%) | Years of experience and seniority level alignment |
| `domain_alignment` | 0.15 (15%) | How well the candidate fits the IC/DoD operational environment |

**When to adjust:**

- If clearance is non-negotiable for the role, raise `clearance_signal` to `0.35` and lower `domain_alignment` to `0.05`.
- If the role is uncleared but highly technical, raise `skill_match` to `0.55` and lower `clearance_signal` to `0.10`.
- After any change, confirm the four values still add up to 1.0.

### clearance_signals.yaml

This is the core logic behind clearance inference. It contains several lists and thresholds you can extend without writing code:

- **`ic_agencies`** — list of IC agencies. Add any you encounter that are missing.
- **`cleared_contractors`** — list of known defense and IC contractors. Add any contractor you work with regularly.
- **`clearance_title_keywords`** — job title words that imply clearance by definition (e.g., "TS/SCI," "ISSO," "ISSM").
- **`contract_vehicle_keywords`** — program names, network names, and classification markers that signal a classified environment.
- **`signal_weights`** — how much each signal type contributes to the clearance score.
- **`inference_thresholds`** — score cutoffs that determine the inference level label.

**Inference levels explained:**

| Level | What it means |
|---|---|
| `confirmed` | The candidate explicitly stated their clearance level in their profile |
| `probable` | Strong indirect evidence — for example, a 10-year career exclusively at NSA contractors |
| `possible` | Some evidence but not conclusive — for example, a short-term government internship |
| `unconfirmed` | No evidence found in public profile — does not mean they lack clearance |

The tool never disqualifies anyone based on clearance inference. Every candidate appears in results regardless of inferred clearance level. The inference level is one data point for your judgment, not a filter.

### icims_columns.yaml

Maps the column headers in your iCIMS CSV export to the field names the tool uses internally.

If iCIMS changes its export format, or your team uses customized column names, edit this file to remap. No code changes needed — just update the mapping and re-run.

### sources.yaml

Toggles each data source on or off. Set any source to `false` to skip it on future runs. When the SeekOut and iCIMS live-feed integrations are activated, their connection details go here as well.

---

## The Skill Picture

Before searching for anyone, the tool builds a "Skill Picture" — a complete map of what a competitive candidate for this role should know. It does this through five inputs:

1. **JD Text Analysis** — reads the job description you provided, including skills mentioned in narrative paragraphs, not just bullet points.
2. **World Knowledge** — Claude applies its knowledge of what practitioners in this field actually use on the job, going beyond what the JD text explicitly says.
3. **Job-Skill Memory** — checks the running log of every previous run for this type of role. Skills confirmed by recruiters in past searches are surfaced automatically.
4. **USAJOBS** — queries how the US government formally describes this role in official postings. Surfaces formal qualification language and certification requirements.
5. **ClearanceJobs** — queries what cleared employers in the private sector are actually asking for in live job postings. Ground truth for what the cleared contractor market demands right now.

After all five sources contribute, you review the full Skill Picture and confirm or edit it before any candidate searching begins. You can add skills, remove skills, or adjust emphasis. You are always the final authority on what skills matter for this role.

---

## Clearance Inference

Clearance status is almost never posted publicly. Candidates who hold active clearances typically cannot disclose this on a public resume or LinkedIn profile.

The tool infers likely clearance status from signals in a candidate's publicly visible profile: where they have worked, what their job titles were, what programs or networks they mention. It layers these signals using the thresholds in `clearance_signals.yaml` to produce an inference level: confirmed, probable, possible, or unconfirmed.

The tool never disqualifies a candidate based on clearance inference. Every candidate appears in ranked results regardless of inferred clearance level. The inference is one input to your judgment — not a pass/fail gate.

---

## The Memory File (job_skill_memory.json)

The tool gets smarter over time through `job_skill_memory.json`.

Every skill you confirm during the Skill Picture review is recorded in this file with a count of how many times it has appeared across runs. Skills you add manually get the highest weight — the tool treats recruiter-added skills as the strongest signal. Skills you reject are never deleted; their rejection count increases so the tool stops over-surfacing them in future runs.

Over time, the Skill Picture for common role types — Cloud Engineer, IA Analyst, Program Manager, Systems Administrator — becomes more accurate automatically, without you having to do anything beyond your normal review step.

You can open `job_skill_memory.json` in any text editor to inspect it, but you do not need to edit it manually. The tool manages it.

---

## Notes Policy

You can add notes to any candidate during a review session. Those notes are permanent. They cannot be overwritten by re-runs, candidate merges, or any automated process. Notes belong to you and stay exactly as you wrote them.

---

## Future Integrations

These integrations are built and ready to activate but require an enterprise license or self-hosted setup. They are not required for the tool to work today.

### SeekOut (integrations/seekout_mcp.py)

SeekOut is a talent intelligence platform built specifically for cleared recruiting. It provides compliant, licensed access to LinkedIn-sourced candidate data — the correct way to source from LinkedIn without violating LinkedIn's terms of service.

When activated, SeekOut replaces the current Google-snippet LinkedIn workaround with full profile data, dramatically improving result quality for LinkedIn-sourced candidates.

To activate: contact your SeekOut account team for an MCP server URL and auth token. Add them to `sources.yaml` and `.env`.

### iCIMS Live Feed (integrations/icims_mcp.py)

A direct live connection to iCIMS that eliminates the manual CSV export step entirely. When activated, you provide a requisition ID in the chat and the tool pulls candidates directly from iCIMS — no file export, no folder drop, no timestamp management.

To activate: contact your iCIMS account team for an MCP server URL and credentials. Add them to `sources.yaml` and `.env`.

### SearXNG (integrations/searxng.py)

A self-hosted meta-search engine that aggregates Google, Bing, and other engines without rate limits or terms of service concerns. When activated, it replaces the direct Google scraper entirely with a clean, unlimited search backend you control.

To activate: deploy SearXNG using Docker (one command, takes about five minutes), then add your instance URL to `sources.yaml`. No API key required.
