"""
Proposal Validation Tool -- Gradio UI

Two buttons:
  1. Check Files  -- instant pre-flight: verifies files are accessible, shows
                     names/sizes/page counts before any parsing happens
  2. Run Validation -- parses docs, writes context.json, waits for /validate
"""
import json
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import gradio as gr

from validator.ingest import ingest_sources, SUPPORTED_EXTENSIONS
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

MAX_CHUNKS = 5_000
CHUNK_MAX_CHARS = 400
TIMEOUT = 600


# ---------------------------------------------------------------------------
# Pre-flight check -- instant, no parsing
# ---------------------------------------------------------------------------

def check_files(proposal_file, source_folder: str) -> str:
    """
    Quickly verify proposal and source files are accessible.
    Opens each file just enough to confirm it is readable and report metadata.
    No text extraction -- this should complete in under 3 seconds.
    """
    lines = []

    # -- Proposal ------------------------------------------------------------
    if not proposal_file:
        return "No proposal file selected."

    proposal_path = (
        proposal_file.name if hasattr(proposal_file, "name") else str(proposal_file)
    )
    p = Path(proposal_path)
    size_kb = p.stat().st_size // 1024

    ext = p.suffix.lower()
    try:
        if ext == ".docx":
            from docx import Document
            doc = Document(p)
            n = len(doc.paragraphs)
            lines.append(f"Proposal : {p.name} ({size_kb} KB) -- {n} paragraphs -- OK")
        elif ext == ".pdf":
            import pdfplumber
            with pdfplumber.open(p) as pdf:
                n = len(pdf.pages)
            lines.append(f"Proposal : {p.name} ({size_kb} KB) -- {n} pages -- OK")
        elif ext == ".pptx":
            from pptx import Presentation
            prs = Presentation(p)
            n = len(prs.slides)
            lines.append(f"Proposal : {p.name} ({size_kb} KB) -- {n} slides -- OK")
        else:
            lines.append(f"Proposal : {p.name} -- unsupported format '{ext}'")
    except Exception as exc:
        lines.append(f"Proposal : {p.name} -- ERROR: {exc}")

    # -- Source folder -------------------------------------------------------
    source_folder = (source_folder or "").strip()
    if not source_folder:
        lines.append("\nSource folder: no path entered.")
        return "\n".join(lines)

    folder = Path(source_folder)
    if not folder.is_dir():
        lines.append(f"\nSource folder: NOT FOUND -- '{source_folder}'")
        return "\n".join(lines)

    files = [
        f for f in sorted(folder.iterdir())
        if not f.is_dir() and f.suffix.lower() in SUPPORTED_EXTENSIONS
    ]

    if not files:
        lines.append(f"\nSource folder: no supported files found in '{source_folder}'")
        return "\n".join(lines)

    lines.append(f"\nSource files ({len(files)} found):")
    for f in files:
        kb = f.stat().st_size // 1024
        fext = f.suffix.lower()
        try:
            if fext in (".xlsx", ".xls"):
                import openpyxl
                wb = openpyxl.load_workbook(f, read_only=True, data_only=True)
                sheets = wb.sheetnames
                wb.close()
                lines.append(f"  {f.name} ({kb} KB) -- {len(sheets)} sheet(s): {', '.join(sheets)} -- OK")
            elif fext == ".pdf":
                import pdfplumber
                with pdfplumber.open(f) as pdf:
                    n = len(pdf.pages)
                lines.append(f"  {f.name} ({kb} KB) -- {n} pages -- OK")
            elif fext == ".csv":
                lines.append(f"  {f.name} ({kb} KB) -- CSV -- OK")
            elif fext in (".docx", ".doc"):
                lines.append(f"  {f.name} ({kb} KB) -- Word -- OK")
            elif fext in (".pptx", ".ppt"):
                lines.append(f"  {f.name} ({kb} KB) -- PowerPoint -- OK")
            else:
                lines.append(f"  {f.name} ({kb} KB) -- OK")
        except Exception as exc:
            lines.append(f"  {f.name} ({kb} KB) -- ERROR: {exc}")

    lines.append("\nAll checks passed. Ready to run validation.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main validation pipeline
# ---------------------------------------------------------------------------

def run_validation(proposal_file, source_folder: str, progress=gr.Progress()):
    """
    Parse documents, write context.json, then wait for Claude Code to
    write results.json (triggered by /validate in Claude Code window).
    """
    # -- Input checks --------------------------------------------------------
    if not proposal_file:
        return None, None, "No proposal file selected."

    proposal_path = (
        proposal_file.name if hasattr(proposal_file, "name") else str(proposal_file)
    )
    source_folder = (source_folder or "").strip()

    if not source_folder or not Path(source_folder).is_dir():
        return None, None, f"Source folder not found: '{source_folder}'"

    # -- Create run directory ------------------------------------------------
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    results_path = run_dir / "results.json"

    # -- Extract proposal text -----------------------------------------------
    progress(0.1, desc="Extracting proposal text...")
    try:
        paragraphs = extract_proposal_text(proposal_path)
    except ValueError as exc:
        return None, None, f"Could not read proposal: {exc}"

    para_list = [{"id": i + 1, "text": t} for i, t in enumerate(paragraphs)]
    logger.info("Proposal: %d paragraphs", len(para_list))

    # -- Ingest source documents ---------------------------------------------
    progress(0.3, desc="Parsing source documents...")

    parsed_files = []
    def on_file(name, num, total):
        parsed_files.append(name)
        progress(0.3 + 0.4 * (num / total), desc=f"Parsing [{num}/{total}]: {name}")

    try:
        raw_chunks = ingest_sources(source_folder, on_file=on_file)
    except Exception as exc:
        return None, None, f"Source ingestion failed: {exc}"

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

    # -- Write context.json --------------------------------------------------
    progress(0.75, desc="Writing context.json...")
    context = {
        "run_id": run_id,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "proposal_file": Path(proposal_path).name,
        "source_folder": source_folder,
        "proposal_paragraphs": para_list,
        "source_chunks": chunks,
    }
    (run_dir / "context.json").write_text(
        json.dumps(context, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    source_files_count = len({c["filename"] for c in chunks})
    trunc_note = f"\n  (source chunks capped at {MAX_CHUNKS})" if truncated else ""
    parse_summary = (
        f"Context ready -- runs/{run_id}/context.json\n"
        f"  Proposal paragraphs : {len(para_list)}\n"
        f"  Source files parsed : {source_files_count}\n"
        f"  Source chunks       : {len(chunks)}{trunc_note}"
    )

    # -- Try auto-invoke, fall back to manual --------------------------------
    progress(0.80, desc="Starting Claude Code...")
    ready_event = threading.Event()
    observer, _ = watch_for_results(results_path, ready_event)

    proc = start_validation(run_id, PROJECT_ROOT)
    manual_mode = (proc is None)

    if manual_mode:
        progress(0.85, desc="Waiting for /validate...")
        return None, None, (
            f"{parse_summary}\n\n"
            f"================================================\n"
            f"  Type /validate in your Claude Code window\n"
            f"  and press Enter.\n"
            f"================================================\n\n"
            f"This UI will update automatically when done.\n"
            f"(You can keep this tab open and wait here.)"
        )

    # -- Wait for results.json (CLI mode) ------------------------------------
    start_time = time.monotonic()
    while not ready_event.is_set():
        elapsed = int(time.monotonic() - start_time)
        if results_path.exists() and results_path.stat().st_size > 0:
            ready_event.set()
            break
        if elapsed > TIMEOUT:
            proc.terminate()
            observer.stop()
            observer.join()
            return None, None, "Timeout -- run /validate in Claude Code manually."
        if proc.poll() is not None:
            time.sleep(2)
            if results_path.exists() and results_path.stat().st_size > 0:
                ready_event.set()
                break
            observer.stop()
            observer.join()
            return None, None, f"Claude Code CLI exited (code {proc.returncode}) without writing results.json."
        progress(0.85 + 0.1 * ((elapsed % 10) / 10), desc=f"Claude Code validating... ({elapsed}s)")
        time.sleep(3)

    observer.stop()
    observer.join()
    return _build_outputs(results_path, proposal_path, source_folder, run_id)


# ---------------------------------------------------------------------------
# Watch for results written externally (manual /validate mode)
# ---------------------------------------------------------------------------

def poll_for_results(proposal_file, source_folder: str, progress=gr.Progress()):
    """
    Called by the 'Results Ready' button after the user has run /validate.
    Reads the most recent results.json and generates outputs.
    """
    latest_run = _find_latest_pending_results()
    if not latest_run:
        return None, None, "No completed results.json found yet. Run /validate in Claude Code first."

    proposal_path = (
        proposal_file.name if hasattr(proposal_file, "name") else str(proposal_file)
    ) if proposal_file else ""

    return _build_outputs(latest_run, proposal_path, source_folder or "", latest_run.parent.name)


def _find_latest_pending_results() -> Path | None:
    """Find the most recent results.json in any run folder."""
    if not RUNS_DIR.exists():
        return None
    candidates = sorted(
        (r / "results.json" for r in RUNS_DIR.iterdir() if r.is_dir()),
        reverse=True,
    )
    for c in candidates:
        if c.exists() and c.stat().st_size > 0:
            return c
    return None


def _build_outputs(results_path: Path, proposal_path: str, source_folder: str, run_id: str):
    try:
        results = json.loads(results_path.read_text(encoding="utf-8"))
    except Exception as exc:
        return None, None, f"Could not parse results.json: {exc}"

    try:
        docx_bytes = build_highlighted_docx(results)
        report = build_json_report(results, proposal_path, source_folder)
    except Exception as exc:
        return None, None, f"Output generation failed: {exc}"

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = results_path.parent
    docx_out = run_dir / f"validated_proposal_{ts}.docx"
    json_out = run_dir / f"provenance_report_{ts}.json"

    docx_out.write_bytes(docx_bytes)
    json_out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    s = report["summary"]
    return str(docx_out), str(json_out), (
        f"Validation complete -- Run {run_id}\n\n"
        f"  Claims found : {s['total_claims']}\n"
        f"  Green        : {s['green']}\n"
        f"  Red          : {s['red']}\n"
        f"  Yellow       : {s['yellow']}\n"
        f"  Non-factual  : {s['non_factual']}"
    )


# ---------------------------------------------------------------------------
# Gradio UI
# ---------------------------------------------------------------------------

DESCRIPTION = """
## Proposal Validation Tool
Upload a proposal, point to your source folder, then **Check Files** first to
confirm everything is readable before you run the full validation.
"""

with gr.Blocks(title="Proposal Validator") as app:
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
                placeholder=r"C:\path\to\sources",
            )
            with gr.Row():
                check_btn = gr.Button("Check Files", variant="secondary")
                run_btn   = gr.Button("Run Validation", variant="primary")
            results_btn = gr.Button(
                "Generate Outputs (after /validate)", variant="secondary"
            )

        with gr.Column(scale=1):
            status_box = gr.Textbox(
                label="Status",
                lines=14,
                interactive=False,
            )
            docx_output = gr.File(label="Download Highlighted .docx", interactive=False)
            json_output = gr.File(label="Download Provenance Report .json", interactive=False)

    check_btn.click(
        fn=check_files,
        inputs=[proposal_file, source_folder],
        outputs=[status_box],
    )

    run_btn.click(
        fn=run_validation,
        inputs=[proposal_file, source_folder],
        outputs=[docx_output, json_output, status_box],
    )

    results_btn.click(
        fn=poll_for_results,
        inputs=[proposal_file, source_folder],
        outputs=[docx_output, json_output, status_box],
    )

app.queue()

if __name__ == "__main__":
    app.launch(server_name="127.0.0.1", server_port=7860, inbrowser=True)
