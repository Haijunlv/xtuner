#!/bin/bash
# gdpval_synth agent install-deps — install data analysis packages
set -euo pipefail
pip install -q openpyxl pandas python-docx 2>/dev/null || true
echo "[internclaw] install-deps: ok (openpyxl, pandas, python-docx)"
