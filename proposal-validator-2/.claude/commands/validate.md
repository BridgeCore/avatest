Find the most recent pending validation run and execute it.

A "pending run" is a folder under `runs/` that contains `context.json` but does NOT yet contain `results.json`.

Steps:
1. List the `runs/` directory and identify the pending run (sort by folder name descending to find the latest)
2. Read `runs/<run_id>/context.json`
3. For every paragraph in `proposal_paragraphs`, classify it and (if factual) validate it against `source_chunks` — following all matching rules and verdict definitions in CLAUDE.md
4. Write the complete results to `runs/<run_id>/results.json` using the exact schema in CLAUDE.md

Do not skip any paragraphs. Every paragraph must appear in the output `paragraphs` array in the same order with the same `id` as `span_id`. Non-factual paragraphs get `verdict: null` and null source fields.

Work carefully and reason through rounding, paraphrasing, abbreviations, and date format variations before assigning each verdict.
