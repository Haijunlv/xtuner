#!/bin/bash
set -uo pipefail
: "${TASK_WORKSPACE:?TASK_WORKSPACE not set}"
: "${TASK_JUDGER_DIR:?TASK_JUDGER_DIR not set}"

WRAPPER_DIR="$(dirname "$0")"

# Ensure dependencies (python_test scripts may use any of these)
pip install -q --break-system-packages openpyxl python-docx pdfplumber python-pptx PyMuPDF

python3 "$WRAPPER_DIR/extractor_judge.py"
