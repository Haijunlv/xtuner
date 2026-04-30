#!/bin/bash
# Standalone validate runner — debug judgers without re-running infer.
#
# Reuses the deliverables from a previous full demo_run.sh execution
# (under work_dir/dump/<task_id>/workspace/workspace/) and replays only
# the rule_judger + simple_judger stages against a fresh sandbox.
set -euo pipefail

export PYTHONPATH=./:/mnt/shared-storage-user/songdemin/user/haijun/code/gitlab/claude_code/xtuner_agent_dev/xtuner/projects:${PYTHONPATH:-}

# Judge LLM configuration
export JUDGE_MODEL_BASE_URL="${JUDGE_MODEL_BASE_URL:-http://s-20260104203038-22bhb.ailab-evalservice.pjh-service.org.cn/v1}"
export JUDGE_MODEL_API_KEY="${JUDGE_MODEL_API_KEY:-sk-admin}"
export JUDGE_MODEL_NAME="${JUDGE_MODEL_NAME:-cv_32b}"

cd ../../

TASK_ID="${TASK_ID:-iso_seed_aaa_cosacc_data_03_rea_app}"
TASK_ROOT="${TASK_ROOT:-/mnt/shared-storage-user/songdemin/user/haijun/code/gitlab/claude_code/dataset/gdpval_synth_v2/phase5_llm_verify/outputv1_glm5/rl_query_extract_dedup/xtuner_rl_data_python_rubrics_output_v2/${TASK_ID}}"
WORKSPACE_DIR="${WORKSPACE_DIR:-./work_dir/dump/${TASK_ID}/workspace/workspace}"

python projects/gdpval_synth/validate_only.py \
    --task-root "$TASK_ROOT" \
    --workspace-dir "$WORKSPACE_DIR" 2>&1 | tee work_dir/validate_only.log
