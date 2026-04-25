#!/bin/bash
# 本地模拟 gdpval_synth pipeline — 平铺脚本，逐步执行
# 所有输出同时打印到终端和日志文件
set -euo pipefail

# ====== 配置 ======
TASKS_ROOT="/mnt/shared-storage-user/songdemin/user/haijun/code/gitlab/claude_code/dataset/gdpval_synth_v2/phase5_llm_verify/outputv1_glm5/rl_query_extract_dedup/xtuner_rl_data_demo"
WRAPPER_DIR="$(cd "$(dirname "$0")" && pwd)/wrappers/gdpval_synth"
TASK_ID="iso_seed_aaa_cosacc_data_03_rea_app"
TMP_ROOT="/mnt/shared-storage-user/songdemin/user/haijun/code/gitlab/claude_code/agent_dev_tmp"
WS="$TMP_ROOT/local_test_output/workspace"

# simple_judger 用的 LLM
export JUDGE_MODEL_BASE_URL="http://s-20260104203038-22bhb-decode.ailab-evalservice.svc:4000/v1"
export JUDGE_MODEL_API_KEY="sk-admin"
export JUDGE_MODEL_NAME="cv_32b"

# ====== 日志 ======
LOG_DIR="$TMP_ROOT/local_test_output"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/local_test_$(date +%Y%m%d_%H%M%S).log"
exec > >(tee -a "$LOG_FILE") 2>&1
echo "=== 日志文件: $LOG_FILE ==="

TASK_DIR="$TASKS_ROOT/$TASK_ID"
echo "=== Task: $TASK_ID ==="
echo "=== Task dir: $TASK_DIR ==="

# ====== Step 1: 查看任务信息 ======
echo ""
echo "--- Step 1: 查看任务元信息 ---"
cat "$TASK_DIR/metadata.json" | python3 -m json.tool
echo ""
echo "criteria 分布:"
python3 -c "
import json
from collections import Counter
r = json.load(open('$TASK_DIR/rubric.json'))
c = Counter(x.get('judge_method','?') for x in r.get('criteria',[]))
for k,v in sorted(c.items()): print(f'  {k}: {v}')
"

# ====== Step 1.5: 检查依赖 ======
echo ""
echo "--- Step 1.5: 检查 python 依赖 ---"
pip install -q openpyxl python-docx pdfplumber python-pptx 2>/dev/null || pip3 install -q openpyxl python-docx pdfplumber python-pptx 2>/dev/null || echo "WARN: pip install failed"

# ====== Step 2: 准备 workspace (与沙盒对齐) ======
# 沙盒 infer 阶段上传: instruction.md + environment/data/**
# 排除: metadata.json, rubric.json, deliverable_spec.json, deliverable_files/**
echo ""
echo "--- Step 2: 准备 workspace (模拟沙盒 infer 上传) ---"
rm -rf "$TMP_ROOT/local_test_output/workspace"
mkdir -p "$WS"

# 只拷贝 agent 可见的文件 (与 pipeline.py UploadHook exclude 一致)
cp "$TASK_DIR/instruction.md" "$WS/"
if [ -d "$TASK_DIR/environment" ]; then
    cp -r "$TASK_DIR/environment" "$WS/"
fi

# 模拟 pre_entry.sh: 把 environment/data/ 平铺到 workspace 根目录，然后删除 environment/
if [ -d "$WS/environment/data" ]; then
    cp -r "$WS/environment/data/." "$WS/"
    rm -rf "$WS/environment"
fi

echo "workspace 内容 (agent 可见):"
find "$WS" -type f | sort | sed "s|$WS/||"
echo ""
echo "注意: rubric.json 和 deliverable_spec.json 不在 workspace 中 (agent 不可见)"

# ====== Step 3: 运行 rule_judger ======
# judger 从任务目录直接读 rubric/spec (模拟 judger 阶段单独上传)
echo ""
echo "--- Step 3: 运行 rule_judger ---"
JUDGER_NAME=rule_judger \
TASK_WORKSPACE="$WS" \
RUBRIC_PATH="$TASK_DIR/rubric.json" \
DELIVERABLE_SPEC_PATH="$TASK_DIR/deliverable_spec.json" \
python3 "$WRAPPER_DIR/rule_judge.py"

# ====== Step 4: 运行 simple_judger ======
echo ""
echo "--- Step 4: 运行 simple_judger ---"
if [[ -z "${JUDGE_MODEL_BASE_URL:-}" ]]; then
    echo "JUDGE_MODEL_BASE_URL 未设置，跳过 simple_judger"
    echo "设置方法: export JUDGE_MODEL_BASE_URL=http://xxx/v1"
else
    JUDGER_NAME=simple_judger \
    TASK_WORKSPACE="$WS" \
    RUBRIC_PATH="$TASK_DIR/rubric.json" \
    DELIVERABLE_SPEC_PATH="$TASK_DIR/deliverable_spec.json" \
    JUDGE_MODEL_BASE_URL="$JUDGE_MODEL_BASE_URL" \
    JUDGE_MODEL_API_KEY="${JUDGE_MODEL_API_KEY:-}" \
    JUDGE_MODEL_NAME="${JUDGE_MODEL_NAME:-}" \
    python3 "$WRAPPER_DIR/simple_judge.py"
fi

echo ""
echo "=== Done ==="
echo "Workspace 保留在: $WS"
echo "日志文件: $LOG_FILE"
