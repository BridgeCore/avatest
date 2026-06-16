# Proposal Validation Tool — Claude Code Task

This project validates factual claims in a proposal document against a library of source documents.
Claude Code **is** the AI reasoning engine — no external API calls are made.

---

## When You Are Invoked

The Gradio UI has already:
1. Parsed the proposal and all source documents
2. Written a `runs/<run_id>/context.json` file containing the extracted content

Your job is to:
1. Read `runs/<run_id>/context.json`
2. Classify and validate every proposal paragraph
3. Write `runs/<run_id>/results.json` in the exact schema below

The UI watches for `results.json` to appear and immediately generates the highlighted `.docx` and provenance report.

---

## context.json Schema (what you read)

```json
{
  "run_id": "20240101_120000",
  "proposal_file": "proposal.docx",
  "source_folder": "/path/to/sources",
  "proposal_paragraphs": [
    { "id": 1, "text": "paragraph text from proposal" }
  ],
  "source_chunks": [
    {
      "chunk_id": 1,
      "filename": "financials.xlsx",
      "filetype": "xlsx",
      "location": "Sheet: Budget FY24, Row: 14",
      "text": "52,700,000"
    }
  ]
}
```

---

## results.json Schema (what you write)

Write this to `runs/<run_id>/results.json`:

```json
{
  "run_id": "20240101_120000",
  "completed_at": "<ISO 8601 timestamp>",
  "paragraphs": [
    {
      "span_id": 1,
      "text": "<exact paragraph text from context>",
      "is_factual": true,
      "claim_type": "financial_figure",
      "verdict": "GREEN",
      "confidence": 0.95,
      "source_file": "financials.xlsx",
      "source_type": "xlsx",
      "source_location": "Sheet: Budget FY24, Row: 14",
      "source_excerpt": "52,700,000",
      "explanation": "Proposal states '~$53M' which rounds from 52,700,000 in cell Row 14 of Budget FY24 sheet."
    }
  ],
  "summary": {
    "total_claims": 45,
    "green": 30,
    "red": 5,
    "yellow": 10,
    "non_factual": 100
  }
}
```

**For non-factual paragraphs**, set:
```json
{
  "span_id": 2,
  "text": "We are committed to excellence.",
  "is_factual": false,
  "claim_type": "non_factual",
  "verdict": null,
  "confidence": null,
  "source_file": null,
  "source_type": null,
  "source_location": null,
  "source_excerpt": null,
  "explanation": null
}
```

---

## What Is a Factual Claim?

A paragraph is **factual** if it asserts at least one verifiable fact:

| Type | Examples |
|------|---------|
| `financial_figure` | "$52.7M", "53 million dollars", "contract value of 52,700,000" |
| `date` | "FY2024", "Q3 2023", "October 15, 2024", "since 2018" |
| `percentage` | "95% availability", "20% cost reduction" |
| `count` | "35 projects", "200 staff", "12 locations" |
| `name` | Client names, company names, person names |
| `location` | "CONUS", "Fort Meade", "Indo-Pacific" |
| `technical_spec` | "AES-256 encryption", "TS/SCI cleared", "FedRAMP authorized" |
| `timeline` | "within 30 days", "Phase 1 runs 18 months" |
| `mission_detail` | "supports SIGINT mission", "IOC by FY25 Q2" |
| `other_factual` | Any other verifiable assertion |

A paragraph is **non-factual** if it contains only:
- Headings or section labels with no embedded facts
- Generic statements: "We are committed to...", "Our team will..."
- Pure transitions: "In addition,", "Furthermore,"
- Blank lines or formatting artifacts

---

## Verdicts

| Verdict | Meaning |
|---------|---------|
| `GREEN` | A source chunk clearly supports this claim |
| `RED` | A source chunk directly contradicts this claim |
| `YELLOW` | No relevant source found, or evidence is ambiguous |

---

## Matching Rules — Read Carefully

You MUST reason through these equivalences — do NOT do literal string matching:

1. **Rounding**: `$52.7M` ≈ `52,700,000` ≈ `"approximately 53 million"` ≈ `"~$53M"`
2. **Abbreviations**: `CONUS` = "Continental United States"; `DoD` = "Department of Defense"; `FY24` = "Fiscal Year 2024"
3. **Paraphrasing**: "managed 35 projects" matches "executed 35 contracts" or "led 35 task orders"
4. **Date formats**: `Q4 FY24` ≈ `Oct–Dec 2024` ≈ `fourth quarter fiscal year 2024`
5. **Unit variants**: `53M` = `$53 million` = `53,000,000` = `USD 53M`
6. **Acronym expansion**: Check if the claim's acronym or the source's expansion refer to the same thing
7. **Name variations**: "DIA" matches "Defense Intelligence Agency"; "USSOCOM" matches "Special Operations Command"

When a source chunk is close but not exact, use your judgment: a 1–2% rounding difference is GREEN; a 10%+ discrepancy that can't be explained by rounding is RED or YELLOW.

---

## Step-by-Step Process

1. Read `runs/<run_id>/context.json` using the Read tool
2. For **each** paragraph in `proposal_paragraphs` (process them all — do not skip any):
   a. Determine `is_factual` and `claim_type`
   b. If factual: scan `source_chunks` for the best matching evidence
   c. Assign verdict + confidence + source citation + plain-English explanation
3. Count summary statistics (total_claims = count of is_factual=true paragraphs)
4. Write the complete `results.json` using the Write tool

Take your time. Completeness matters — every paragraph must appear in the output `paragraphs` array, in the same order as the input, preserving the original `id` as `span_id`.

---

## /validate Slash Command

Running `/validate` triggers the validation task above on the most recent pending run (a run folder containing `context.json` but no `results.json`).
