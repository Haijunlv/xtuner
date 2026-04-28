#!/bin/bash
# gdpval_synth agent install-deps — install data analysis packages + deploy skills
set -euo pipefail

# ── 依赖安装 ──
pip install -q openpyxl pandas python-docx pdfplumber python-pptx 2>/dev/null || true
echo "[internclaw] install-deps: ok (openpyxl, pandas, python-docx, pdfplumber, python-pptx)"

# ── 部署 skills 到 workspace/skills/（SkillsLoader 发现路径）──
AGENT_DIR="${TASK_WORKSPACE:-/workspace}/agent/internclaw"
SKILLS_TARGET="${TASK_WORKSPACE:-/workspace}/skills"

if [ -d "$AGENT_DIR/skills" ]; then
    mkdir -p "$SKILLS_TARGET"
    cp -r "$AGENT_DIR/skills/"* "$SKILLS_TARGET/" 2>/dev/null || true
    echo "[internclaw] skills deployed to $SKILLS_TARGET"
fi
