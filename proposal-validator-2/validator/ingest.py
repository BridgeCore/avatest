"""
Source document ingestion and indexing.
Parses .xlsx/.xls, .csv, .docx/.doc, .pdf, .pptx/.ppt files into a flat list of
searchable text chunks, each tagged with provenance metadata.
"""
import csv
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".xlsx", ".xls", ".csv", ".docx", ".doc", ".pdf", ".pptx", ".ppt"}


def ingest_sources(folder_path: str) -> list[dict]:
    """
    Walk a folder, parse every supported file, and return a flat list of chunks.

    Each chunk dict has:
        filename, filetype, location, sheet, page_or_slide, row, text
    """
    folder = Path(folder_path)
    if not folder.exists() or not folder.is_dir():
        raise ValueError(f"Source folder does not exist: {folder_path}")

    chunks = []
    for file_path in sorted(folder.iterdir()):
        if file_path.is_dir():
            continue
        ext = file_path.suffix.lower()
        if ext not in SUPPORTED_EXTENSIONS:
            logger.info("Skipping unsupported file: %s", file_path.name)
            continue
        try:
            if ext in (".xlsx", ".xls"):
                chunks.extend(_parse_excel(file_path))
            elif ext == ".csv":
                chunks.extend(_parse_csv(file_path))
            elif ext in (".docx", ".doc"):
                chunks.extend(_parse_word(file_path))
            elif ext == ".pdf":
                chunks.extend(_parse_pdf(file_path))
            elif ext in (".pptx", ".ppt"):
                chunks.extend(_parse_pptx(file_path))
        except Exception as exc:
            logger.warning("Failed to parse %s: %s — skipping.", file_path.name, exc)

    logger.info("Ingested %d chunks from %s", len(chunks), folder_path)
    return chunks


# ---------------------------------------------------------------------------
# Per-format parsers
# ---------------------------------------------------------------------------

def _chunk(filename, filetype, location, sheet, page_or_slide, row, text):
    return {
        "filename": filename,
        "filetype": filetype,
        "location": location,
        "sheet": sheet,
        "page_or_slide": page_or_slide,
        "row": row,
        "text": text.strip(),
    }


def _parse_excel(file_path: Path) -> list[dict]:
    chunks = []
    ext = file_path.suffix.lower()

    # openpyxl handles .xlsx; fall back to pandas+xlrd for .xls
    try:
        import openpyxl
        wb = openpyxl.load_workbook(file_path, data_only=True)
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            headers = [
                str(c.value) if c.value is not None else f"Col{i+1}"
                for i, c in enumerate(next(ws.iter_rows(min_row=1, max_row=1)))
            ]
            for row_idx, row in enumerate(ws.iter_rows(min_row=1, values_only=True), start=1):
                parts = []
                for col_idx, val in enumerate(row):
                    if val is not None:
                        hdr = headers[col_idx] if col_idx < len(headers) else f"Col{col_idx+1}"
                        parts.append(f"{hdr}: {val}")
                if parts:
                    loc = f"Sheet: {sheet_name}, Row: {row_idx}"
                    chunks.append(_chunk(file_path.name, "xlsx", loc, sheet_name, None, row_idx, " | ".join(parts)))
    except Exception:
        # Fallback: pandas (works for both .xlsx and .xls)
        try:
            import pandas as pd
            engine = "xlrd" if ext == ".xls" else "openpyxl"
            xl = pd.ExcelFile(file_path, engine=engine)
            for sheet_name in xl.sheet_names:
                df = xl.parse(sheet_name, dtype=str)
                for row_idx, (_, row) in enumerate(df.iterrows(), start=2):
                    parts = [f"{col}: {val}" for col, val in row.items() if str(val) not in ("nan", "")]
                    if parts:
                        loc = f"Sheet: {sheet_name}, Row: {row_idx}"
                        chunks.append(_chunk(file_path.name, "xlsx", loc, sheet_name, None, row_idx, " | ".join(parts)))
        except Exception as exc2:
            logger.warning("pandas fallback also failed for %s: %s", file_path.name, exc2)

    return chunks


def _parse_csv(file_path: Path) -> list[dict]:
    chunks = []
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            with open(file_path, newline="", encoding=encoding) as f:
                reader = csv.DictReader(f)
                for row_idx, row in enumerate(reader, start=2):
                    parts = [f"{k}: {v}" for k, v in row.items() if v and v.strip()]
                    if parts:
                        loc = f"Row: {row_idx}"
                        chunks.append(_chunk(file_path.name, "csv", loc, None, None, row_idx, " | ".join(parts)))
            break  # success
        except UnicodeDecodeError:
            continue
    return chunks


def _parse_word(file_path: Path) -> list[dict]:
    chunks = []
    ext = file_path.suffix.lower()

    # python-docx (.docx only)
    if ext == ".docx":
        try:
            from docx import Document
            doc = Document(file_path)
            for para_idx, para in enumerate(doc.paragraphs, start=1):
                text = para.text.strip()
                if text:
                    loc = f"Paragraph: {para_idx}"
                    chunks.append(_chunk(file_path.name, "docx", loc, None, None, para_idx, text))
            for tbl_idx, table in enumerate(doc.tables, start=1):
                for row_idx, row in enumerate(table.rows, start=1):
                    cells = [c.text.strip() for c in row.cells if c.text.strip()]
                    if cells:
                        loc = f"Table {tbl_idx}, Row {row_idx}"
                        chunks.append(_chunk(file_path.name, "docx", loc, None, None, row_idx, " | ".join(cells)))
            return chunks
        except Exception as exc:
            logger.warning("python-docx failed for %s: %s", file_path.name, exc)

    # .doc fallback via docx2txt
    try:
        import docx2txt
        text = docx2txt.process(str(file_path))
        for line_idx, line in enumerate(text.splitlines(), start=1):
            line = line.strip()
            if line:
                chunks.append(_chunk(file_path.name, "doc", f"Line: {line_idx}", None, None, line_idx, line))
    except Exception as exc:
        logger.warning("docx2txt failed for %s: %s — skipping.", file_path.name, exc)

    return chunks


def _parse_pdf(file_path: Path) -> list[dict]:
    chunks = []
    try:
        import pdfplumber
        with pdfplumber.open(file_path) as pdf:
            for page in pdf.pages:
                pg_num = page.page_number

                # Text lines
                page_text = page.extract_text()
                if page_text and page_text.strip():
                    for line in page_text.splitlines():
                        line = line.strip()
                        if line:
                            loc = f"Page: {pg_num}"
                            chunks.append(_chunk(file_path.name, "pdf", loc, None, pg_num, None, line))
                else:
                    # OCR fallback for scanned pages
                    try:
                        import pytesseract
                        img = page.to_image(resolution=150).original
                        ocr_text = pytesseract.image_to_string(img)
                        if ocr_text.strip():
                            loc = f"Page: {pg_num} (OCR)"
                            chunks.append(_chunk(file_path.name, "pdf", loc, None, pg_num, None, ocr_text.strip()))
                    except Exception as ocr_exc:
                        logger.warning("OCR failed for %s page %d: %s", file_path.name, pg_num, ocr_exc)

                # Tables
                for tbl_idx, table in enumerate(page.extract_tables() or [], start=1):
                    for row_idx, row in enumerate(table, start=1):
                        cells = [str(c).strip() for c in row if c and str(c).strip()]
                        if cells:
                            loc = f"Page: {pg_num}, Table {tbl_idx}, Row {row_idx}"
                            chunks.append(_chunk(file_path.name, "pdf", loc, None, pg_num, row_idx, " | ".join(cells)))
    except Exception as exc:
        logger.warning("pdfplumber failed for %s: %s — skipping.", file_path.name, exc)

    return chunks


def _parse_pptx(file_path: Path) -> list[dict]:
    chunks = []
    ext = file_path.suffix.lower()

    if ext == ".ppt":
        logger.warning("Legacy .ppt format not supported: %s — skipping.", file_path.name)
        return chunks

    try:
        from pptx import Presentation
        prs = Presentation(file_path)
        for slide_idx, slide in enumerate(prs.slides, start=1):
            for shape in slide.shapes:
                if shape.has_text_frame:
                    for para in shape.text_frame.paragraphs:
                        text = para.text.strip()
                        if text:
                            loc = f"Slide: {slide_idx}, Shape: {shape.name}"
                            chunks.append(_chunk(file_path.name, "pptx", loc, None, slide_idx, None, text))
                if shape.has_table:
                    for row_idx, row in enumerate(shape.table.rows, start=1):
                        cells = [c.text.strip() for c in row.cells if c.text.strip()]
                        if cells:
                            loc = f"Slide: {slide_idx}, Table Row: {row_idx}"
                            chunks.append(_chunk(file_path.name, "pptx", loc, None, slide_idx, row_idx, " | ".join(cells)))
    except Exception as exc:
        logger.warning("python-pptx failed for %s: %s — skipping.", file_path.name, exc)

    return chunks
