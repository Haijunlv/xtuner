#!/bin/bash
set -euo pipefail
: "${TASK_WORKSPACE:?TASK_WORKSPACE not set}"
# Flatten data files into workspace root
if [ -d "$TASK_WORKSPACE/environment/data" ]; then
    cp -r "$TASK_WORKSPACE/environment/data/." "$TASK_WORKSPACE/"
fi
# Ensure data analysis deps available
python3 -c "import openpyxl" 2>/dev/null || pip install -q openpyxl pandas python-docx pdfplumber 2>/dev/null || true
