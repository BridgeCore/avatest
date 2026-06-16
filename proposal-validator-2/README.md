# Proposal Validation Tool

Validates factual claims in a proposal document against a library of source materials.
**Claude Code is the AI reasoning engine** — no Anthropic API key or external calls required.

---

## How It Works

```
User clicks "Run Validation"
        │
        ▼
  Gradio UI (app.py)
  • Parses proposal (.docx/.pdf/.pptx)
  • Ingests source docs (xlsx/csv/docx/pdf/pptx)
  • Writes runs/<run_id>/context.json
        │
        ▼
  Claude Code (subprocess)
  • Reads CLAUDE.md for validation instructions
  • Reads runs/<run_id>/context.json
  • Classifies every paragraph as factual/non-factual
  • Searches source chunks for evidence
  • Writes runs/<run_id>/results.json
        │
        ▼
  watchdog detects results.json
  • Builds highlighted .docx
  • Builds provenance .json
  • Download links appear in UI
```

---

## Requirements

- Python 3.10+
- **[Claude Code](https://claude.ai/code)** installed and on your PATH (`claude --version` should work)
- *(Optional)* [Tesseract OCR](https://github.com/UB-Mannheim/tesseract/wiki) for scanned-PDF support

---

## Installation

```bash
cd proposal-validator-2
pip install -r requirements.txt
```

---

## Running

```bash
python app.py
```

Open **http://localhost:7860** in your browser.

---

## Interactive Slash Command

If you open this project in Claude Code you can also trigger a validation run manually:

```
/validate
```

Claude Code will find the latest pending `runs/*/context.json` (one without a `results.json`) and process it interactively so you can watch the reasoning in real time.

---

## File Structure

```
proposal-validator-2/
  app.py                       # Gradio UI — entry point
  CLAUDE.md                    # Validation task instructions for Claude Code
  requirements.txt
  README.md
  .claude/
    commands/
      validate.md              # /validate slash command definition
  validator/
    __init__.py
    ingest.py                  # Source document parsing & indexing
    extractor.py               # Proposal text extraction (no AI)
    runner.py                  # Spawns headless Claude Code subprocess
    watcher.py                 # watchdog — detects results.json
    output.py                  # Highlighted .docx + provenance .json
  runs/                        # Created at runtime (gitignore this)
    <run_id>/
      context.json             # Written by app.py (proposal + source chunks)
      results.json             # Written by Claude Code
      validated_proposal_*.docx
      provenance_report_*.json
```

---

## Supported Source Formats

| Format | Notes |
|--------|-------|
| `.docx` | Paragraphs + tables |
| `.xlsx` / `.xls` | All sheets, headers, cell values |
| `.csv` | UTF-8 and Latin-1 |
| `.pdf` | Text-based; OCR fallback for scanned pages (needs Tesseract) |
| `.pptx` | Slide text + table cells |
| `.doc` | Basic support via docx2txt |
| `.ppt` | Not supported — convert to .pptx first |

---

## Output Files

| File | Description |
|------|-------------|
| `validated_proposal_<ts>.docx` | Proposal with 🟢🔴🟡 highlights per claim |
| `provenance_report_<ts>.json` | Full audit trail: claim, verdict, source citation, explanation |

---

## Source Chunk Limits

To stay within Claude Code's context window, source content is capped at **5 000 chunks of 400 characters each** (~2 MB of source text). A warning appears in the UI if your source folder exceeds this. Prioritise the most specific/numerical files.
