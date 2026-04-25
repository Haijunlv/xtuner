export PYTHONPATH=./:/mnt/shared-storage-user/songdemin/user/haijun/code/gitlab/claude_code/xtuner_agent_dev/xtuner/projects:$PYTHONPATH

# Judge LLM configuration (for simple_judger)
# export JUDGE_MODEL_BASE_URL="${JUDGE_MODEL_BASE_URL:-http://s-20260104203038-22bhb-decode.ailab-evalservice.svc:4000/v1}"
# export JUDGE_MODEL_API_KEY="${JUDGE_MODEL_API_KEY:-sk-admin}"
# export JUDGE_MODEL_NAME="${JUDGE_MODEL_NAME:-cv_32b}"

cd ../../


python xtuner/v1/ray/environment/rl_task/runner.py \
    --config /mnt/shared-storage-user/songdemin/user/haijun/code/gitlab/claude_code/xtuner_agent_dev/xtuner/projects/gdpval_synth/configs/default.py \
    --lagent-src /mnt/shared-storage-user/songdemin/user/haijun/code/gitlab/claude_code/lagent \
    --limit 1 \
    --concurrency 1