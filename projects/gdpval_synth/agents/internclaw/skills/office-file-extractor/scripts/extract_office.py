#!/usr/bin/env python3
"""Office file content extractor.

Extracts full text content and structure from PDF, XLSX, DOCX, DOC, PPTX, PPT
and writes a JSON file alongside the source file (or to --output path).

Usage:
    python extract_office.py <file_path> [--output <json_path>]

Output JSON structure varies by file type — see below.
"""
from __future__ import annotations

import argparse
import json
import os
import sys


# ── PDF ──────────────────────────────────────────────────────────────────────

def extract_pdf(path: str) -> dict:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        pages = []
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            tables = []
            for tbl in page.extract_tables():
                tables.append(tbl)
            pages.append({
                "page": i + 1,
                "text": text,
                "tables": tables,
            })
        return {
            "metadata": {"page_count": len(pdf.pages)},
            "content": pages,
        }


# ── XLSX / XLS / XLSM ────────────────────────────────────────────────────────

def extract_xlsx(path: str) -> dict:
    import openpyxl

    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    sheets = []
    sheet_names = wb.sheetnames
    row_counts: dict[str, int] = {}
    headers: dict[str, list] = {}

    for name in sheet_names:
        ws = wb[name]
        rows = []
        for row in ws.iter_rows(values_only=True):
            # Skip entirely empty rows
            if any(c is not None for c in row):
                rows.append([str(c) if c is not None else "" for c in row])
        row_counts[name] = len(rows)
        headers[name] = rows[0] if rows else []
        sheets.append({"sheet": name, "rows": rows})

    wb.close()
    return {
        "metadata": {
            "sheet_names": sheet_names,
            "row_counts": row_counts,
            "headers": headers,
        },
        "content": sheets,
    }


# ── DOCX / DOC ────────────────────────────────────────────────────────────────

def extract_docx(path: str) -> dict:
    from docx import Document

    doc = Document(path)
    blocks = []
    headings = []

    for para in doc.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        style = para.style.name
        if style.startswith("Heading"):
            try:
                level = int(style.split()[-1])
            except ValueError:
                level = 1
            headings.append(text)
            blocks.append({"type": "heading", "level": level, "text": text})
        else:
            blocks.append({"type": "paragraph", "text": text})

    for i, table in enumerate(doc.tables):
        tbl_rows = []
        for row in table.rows:
            tbl_rows.append([cell.text.strip() for cell in row.cells])
        blocks.append({"type": "table", "table_index": i, "rows": tbl_rows})

    return {
        "metadata": {
            "paragraph_count": len(doc.paragraphs),
            "table_count": len(doc.tables),
            "headings": headings,
        },
        "content": blocks,
    }


# ── PPTX / PPT ────────────────────────────────────────────────────────────────

def extract_pptx(path: str) -> dict:
    from pptx import Presentation

    prs = Presentation(path)
    slides = []

    for i, slide in enumerate(prs.slides):
        shapes = []
        for shape in slide.shapes:
            if shape.has_text_frame:
                text = shape.text_frame.text.strip()
                if text:
                    shapes.append({"type": "text", "text": text})
            if shape.has_table:
                tbl_rows = []
                for row in shape.table.rows:
                    tbl_rows.append([cell.text.strip() for cell in row.cells])
                shapes.append({"type": "table", "rows": tbl_rows})
        slides.append({"slide": i + 1, "shapes": shapes})

    return {
        "metadata": {"slide_count": len(prs.slides)},
        "content": slides,
    }


# ── Dispatch ──────────────────────────────────────────────────────────────────

def extract(path: str) -> dict:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".pdf":
        type_data = extract_pdf(path)
    elif ext in (".xlsx", ".xls", ".xlsm"):
        type_data = extract_xlsx(path)
    elif ext in (".docx", ".doc"):
        type_data = extract_docx(path)
    elif ext in (".pptx", ".ppt"):
        type_data = extract_pptx(path)
    else:
        print(f"Unsupported file type: {ext}", file=sys.stderr)
        sys.exit(1)

    return {
        "filename": os.path.basename(path),
        "ext": ext,
        "size_bytes": os.path.getsize(path),
        **type_data,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Extract office file content to JSON")
    parser.add_argument("file", help="Path to PDF/XLSX/DOCX/DOC/PPTX/PPT file")
    parser.add_argument(
        "--output", "-o",
        help="Output JSON path (default: <file>.extracted.json alongside source file)",
    )
    args = parser.parse_args()

    if not os.path.isfile(args.file):
        print(f"File not found: {args.file}", file=sys.stderr)
        sys.exit(1)

    result = extract(args.file)

    if args.output:
        out_path = args.output
    else:
        base = os.path.splitext(args.file)[0]
        out_path = base + ".extracted.json"

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    print(out_path)


if __name__ == "__main__":
    main()
