---
name: office-file-extractor
description: >
  Extract structured text content from office files: PDF, XLSX/XLS/XLSM (Excel),
  DOCX/DOC (Word), PPTX/PPT (PowerPoint). Use this skill when you need to read,
  analyze, or extract information from office files in the workspace.
---

## Overview

This skill extracts full text content from office files into a JSON file,
which you then read to answer questions or process the data.

**Supported types:**

| Extension | Library |
|-----------|---------|
| `.pdf` | pdfplumber |
| `.xlsx` / `.xls` / `.xlsm` | openpyxl |
| `.docx` / `.doc` | python-docx |
| `.pptx` / `.ppt` | python-pptx |

## Usage

Run the extractor on any supported file:

```bash
python $TASK_WORKSPACE/skills/office-file-extractor/scripts/extract_office.py <file_path>
```

The script prints the output JSON path to stdout. By default it writes
`<original_filename>.extracted.json` next to the source file.

To specify a custom output path:

```bash
python $TASK_WORKSPACE/skills/office-file-extractor/scripts/extract_office.py <file_path> \
    --output /tmp/result.json
```

## Output Format

**PDF:**
```json
{
  "filename": "report.pdf",
  "ext": ".pdf",
  "size_bytes": 102400,
  "metadata": { "page_count": 15 },
  "content": [
    { "page": 1, "text": "...", "tables": [[["col1","col2"],["val1","val2"]]] }
  ]
}
```

**Excel (.xlsx / .xls / .xlsm):**
```json
{
  "filename": "data.xlsx",
  "ext": ".xlsx",
  "size_bytes": 51200,
  "metadata": {
    "sheet_names": ["Sheet1", "Sheet2"],
    "row_counts": { "Sheet1": 200, "Sheet2": 50 },
    "headers": { "Sheet1": ["Name","Value","Date"], "Sheet2": ["ID","Label"] }
  },
  "content": [
    { "sheet": "Sheet1", "rows": [["Name","Value","Date"], ["Alice","100","2024-01"]] }
  ]
}
```

**Word (.docx / .doc):**
```json
{
  "filename": "document.docx",
  "ext": ".docx",
  "size_bytes": 20480,
  "metadata": {
    "paragraph_count": 85,
    "table_count": 3,
    "headings": ["Introduction", "Methods", "Results"]
  },
  "content": [
    { "type": "heading", "level": 1, "text": "Introduction" },
    { "type": "paragraph", "text": "..." },
    { "type": "table", "table_index": 0, "rows": [["Header1","Header2"],["val1","val2"]] }
  ]
}
```

**PowerPoint (.pptx / .ppt):**
```json
{
  "filename": "slides.pptx",
  "ext": ".pptx",
  "size_bytes": 307200,
  "metadata": { "slide_count": 20 },
  "content": [
    {
      "slide": 1,
      "shapes": [
        { "type": "text", "text": "Slide Title" },
        { "type": "table", "rows": [["A","B"],["1","2"]] }
      ]
    }
  ]
}
```

## Tips

- **Large JSON files**: Read the output in sections, or focus on `metadata` first to understand structure.
- **Excel with many sheets**: Check `metadata.sheet_names` first to identify which sheet is relevant.
- **PDF with tables**: Tables are extracted per-page under `content[n].tables` as 2D arrays.
