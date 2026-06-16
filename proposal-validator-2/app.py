"""
Proposal Validation Tool — Gradio UI

Flow:
  1. User picks proposal + source folder → clicks Run Validation
  2. App parses all documents (no AI); writes runs/<run_id>/context.json
  3. App spawns Claude Code as a headless subprocess
     (claude --dangerously-skip-permissions -p "...")
  4. watchdog watches runs/<run_id>/ for results.json to appear
  5. Once detected: build highlighted .docx + provenance .json → download links

No Anthropic API key required — Claude Code IS the AI runtime.
"""
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import gradio as gr

from validator.ingest import ingest_sources
from validator.extractor import extract_proposal_text
from validator.runner import start_validation
from validator.watcher import watch_for_results
from validator.output import build_highlighted_docx, build_json_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).parent
RUNS_DIR = PROJECT_ROOT / "runs"
RUNS_DIR.mkdir(exist_ok=True)

MAX_CHUNKS = 5_000       # cap to stay within Claude Code's context window
CHUNK_MAX_CHARS = 400    # truncate individual source chunks
TIMEOUT = 600            # seconds before giving up on Claude Code


# ---------------------------------------------------------------------------
# Context preparation
# ---------------------------------------------------------------------------

def _prepare_context(run_dir: Path, proposal_path: str, source_folder: str) -> dict:
    """
    Parse all documents and write context.json to run_dir.
    Returns a stats dict for the status display.
    """
    paragraphs = extract_proposal_text(proposal_path)
    para_list = [{"id": i + 1, "text": t} for i, t in enumerate(paragraphs)]

    raw_chunks = ingest_sources(source_folder)
    truncated = len(raw_chunks) > MAX_CHUNKS
    chunks = [
        {
            "chunk_id": i + 1,
            "filename": c["filename"],
            "filetype": c["filetype"],
            "location": c["location"],
            "text": c["text"][:CHUNK_MAX_CHARS],
        }
        for i, c in enumerate(raw_chunks[:MAX_CHUNKS])
    ]

    context = {
        "run_id": run_dir.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "proposal_file": Path(proposal_path).name,
        "source_folder": str(source_folder),
        "proposal_paragraphs": para_list,
        "source_chunks": chunks,
    }

    (run_dir / "context.json").write_text(
        json.dumps(context, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return {
        "paragraphs": len(para_list),
        "source_files": len({c["filename"] for c in chunks}),
        "source_chunks": len(chunks),
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# Main pipeline (Gradio generator — yields status updates in real time)
# ---------------------------------------------------------------------------

def run_validation(proposal_file, source_folder: str):
    """
    Gradio streaming generator.
    Yields (docx_file, json_file, status_text) on each update.
    """
    # ── Input checks ─────────────────────────────────────────────────────
    if not proposal_file:
        yield None, None, "⚠ No proposal file selected."
        return

    proposal_path = (
        proposal_file.name if hasattr(proposal_file, "name") else str(proposal_file)
    )
    source_folder = (source_folder or "").strip()

    if not source_folder or not Path(source_folder).is_dir():
        yield None, None, f"⚠ Source folder not found: '{source_folder}'"
        return

    # ── Create run directory ──────────────────────────────────────────────
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    results_path = run_dir / "results.json"

    yield None, None, f"Run {run_id} started.\nParsing documents…"

    # ── Parse + write context.json ────────────────────────────────────────
    try:
        stats = _prepare_context(run_dir, proposal_path, source_folder)
    except ValueError as exc:
        yield None, None, f"⚠ Extraction error: {exc}"
        return
    except Exception as exc:
        logger.exception("Context preparation failed.")
        yield None, None, f"⚠ Failed to prepare context: {exc}"
        return

    trunc_warn = "\n  ⚠ Source chunks truncated to 5 000 max." if stats["truncated"] else ""
    yield None, None, (
        f"Context written to runs/{run_id}/context.json\n"
        f"  Proposal paragraphs : {stats['paragraphs']}\n"
        f"  Source files        : {stats['source_files']}\n"
        f"  Source chunks       : {stats['source_chunks']}{trunc_warn}\n\n"
        f"Launching Claude Code…"
    )

    # ── Start watchdog ────────────────────────────────────────────────────
    ready_event = threading.Event()
    observer, _ = watch_for_results(results_path, ready_event)

    # ── Spawn Claude Code ─────────────────────────────────────────────────
    try:
        proc = start_validation(run_id, PROJECT_ROOT)
    except FileNotFoundError as exc:
        observer.stop()
        observer.join()
        yield None, None, f"⚠ {exc}"
        return

    # ── Poll until results.json appears, process exits, or timeout ─────────
    start_time = time.monotonic()
    tick = 0

    while not ready_event.is_set():
        elapsed = int(time.monotonic() - start_time)

        # Belt-and-suspenders: also poll the file directly (watchdog can be
        # slow on Windows on network shares)
        if results_path.exists() and results_path.stat().st_size > 0:
            ready_event.set()
            break

        if elapsed > TIMEOUT:
            proc.terminate()
            observer.stop()
            observer.join()
            yield None, None, (
                f"⚠ Timeout after {TIMEOUT}s.\n"
                f"Claude Code did not write results.json within the time limit.\n"
                f"Check your terminal for Claude Code output."
            )
            return

        if proc.poll() is not None:
            # Claude Code exited — give watchdog/filesystem a moment to settle
            time.sleep(2)
            if results_path.exists() and results_path.stat().st_size > 0:
                ready_event.set()
                break
            stderr_tail = ""
            if proc.stderr:
                try:
                    stderr_tail = proc.stderr.read()[-800:]
                except Exception:
                    pass
            observer.stop()
            observer.join()
            yield None, None, (
                f"⚠ Claude Code exited (code {proc.returncode}) "
                f"without writing results.json.\n\n"
                f"stderr:\n{stderr_tail}"
            )
            return

        tick = (tick % 3) + 1
        yield None, None, (
            f"Claude Code is validating{'.' * tick}\n"
            f"  Elapsed : {elapsed}s\n"
            f"  Run ID  : {run_id}\n\n"
            f"Check your Claude Code terminal window for live progress.\n"
            f"The UI will update automatically when results.json is written."
        )
        time.sleep(3)

    observer.stop()
    observer.join()

    # ── Read results & generate outputs ───────────────────────────────────
    yield None, None, "Claude Code finished. Generating outputs…"

    try:
        results = json.loads(results_path.read_text(encoding="utf-8"))
    except Exception as exc:
        yield None, None, f"⚠ Could not parse results.json: {exc}"
        return

    try:
        docx_bytes = build_highlighted_docx(results)
        report = build_json_report(results, proposal_path, source_folder)
    except Exception as exc:
        logger.exception("Output generation failed.")
        yield None, None, f"⚠ Output generation failed: {exc}"
        return

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    docx_out = run_dir / f"validated_proposal_{ts}.docx"
    json_out = run_dir / f"provenance_report_{ts}.json"

    docx_out.write_bytes(docx_bytes)
    json_out.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    s = report["summary"]
    yield str(docx_out), str(json_out), (
        f"✅ Validation complete — Run {run_id}\n\n"
        f"  Claims found : {s['total_claims']}\n"
        f"  🟢 Green     : {s['green']}\n"
        f"  🔴 Red       : {s['red']}\n"
        f"  🟡 Yellow    : {s['yellow']}\n"
        f"  Non-factual  : {s['non_factual']}"
    )


# ---------------------------------------------------------------------------
# Gradio UI
# ---------------------------------------------------------------------------

DESCRIPTION = """
## Proposal Validation Tool

Upload a proposal and point to your source documents folder.
**Claude Code** reasons through every factual claim and produces:

- **Highlighted .docx** — green / red / yellow per claim
- **Provenance .json** — full audit trail with source citations

*Claude Code must be installed and on your PATH. No API key required.*
"""

with gr.Blocks(title="Proposal Validator", theme=gr.themes.Soft()) as app:
    gr.Markdown(DESCRIPTION)

    with gr.Row():
        with gr.Column(scale=1):
            proposal_file = gr.File(
                label="Proposal Document (.docx / .pdf / .pptx)",
                file_types=[".docx", ".pdf", ".pptx"],
                file_count="single",
            )
            source_folder = gr.Textbox(
                label="Source Documents Folder (full path)",
                placeholder=r"C:\path\to\sources   or   /path/to/sources",
            )
            run_btn = gr.Button("▶  Run Validation", variant="primary", size="lg")

        with gr.Column(scale=1):
            status_box = gr.Textbox(
                label="Status / Progress",
                lines=10,
                interactive=False,
                show_copy_button=True,
            )
            docx_output = gr.File(label="⬇ Highlighted .docx", interactive=False)
            json_output = gr.File(label="⬇ Provenance Report .json", interactive=False)

    run_btn.click(
        fn=run_validation,
        inputs=[proposal_file, source_folder],
        outputs=[docx_output, json_output, status_box],
    )

app.queue()

if __name__ == "__main__":
    app.launch(server_name="0.0.0.0", server_port=7860, inbrowser=True)
