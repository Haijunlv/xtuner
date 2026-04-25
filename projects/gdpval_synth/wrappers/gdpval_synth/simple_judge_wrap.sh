#!/bin/bash
set -uo pipefail
: "${TASK_WORKSPACE:?TASK_WORKSPACE not set}"
: "${TASK_JUDGER_DIR:?TASK_JUDGER_DIR not set}"

WRAPPER_DIR="$(dirname "$0")"

# Ensure dependencies
pip install -q openpyxl python-docx 2>/dev/null || true

/mnt/llm-ai-infra/miniconda3/envs/train/bin/python3 "$WRAPPER_DIR/simple_judge.py"
