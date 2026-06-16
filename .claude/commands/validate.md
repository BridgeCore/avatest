Find the most recent pending validation run in proposal-validator-2/runs/ and execute it.

A "pending run" is a folder under `proposal-validator-2/runs/` that contains `context.json` but does NOT yet contain `results.json`.

Steps:
1. List `proposal-validator-2/runs/` and identify the pending run (latest folder name without results.json)
2. Read `proposal-validator-2/runs/<run_id>/context.json`
3. Read `proposal-validator-2/CLAUDE.md` for the full validation instructions, schema, and matching rules
4. For every paragraph in `proposal_paragraphs`, classify it and (if factual) validate it against `source_chunks`
5. Write the complete results to `proposal-validator-2/runs/<run_id>/results.json` using the exact schema in CLAUDE.md

Do not skip any paragraphs. Every paragraph must appear in the output `paragraphs` array in the same order with the same `id` as `span_id`. Non-factual paragraphs get `verdict: null` and null source fields.

Work carefully and reason through rounding, paraphrasing, abbreviations, and date format variations before assigning each verdict.
