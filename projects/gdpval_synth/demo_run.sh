export PYTHONPATH=./:/mnt/shared-storage-user/songdemin/user/haijun/code/gitlab/claude_code/xtuner_agent_dev/xtuner/projects:$PYTHONPATH

# Judge LLM configuration (for simple_judger)
export JUDGE_MODEL_BASE_URL="${JUDGE_MODEL_BASE_URL:-http://s-20260104203038-22bhb.ailab-evalservice.pjh-service.org.cn/v1}"
export JUDGE_MODEL_API_KEY="${JUDGE_MODEL_API_KEY:-sk-admin}"
export JUDGE_MODEL_NAME="${JUDGE_MODEL_NAME:-cv_32b}"

cd ../../

export RL_LLM_MODEL="qwen35_35b_a3b"
export RL_LLM_BASE_URL="http://s-20260104203038-22bhb.ailab-evalservice.pjh-service.org.cn/v1"

work_dir="./work_dir2"
mkdir -p  $work_dir 
python xtuner/v1/ray/environment/rl_task/runner.py \
    --config /mnt/shared-storage-user/songdemin/user/haijun/code/gitlab/claude_code/xtuner_agent_dev/xtuner/projects/gdpval_synth/configs/default.py \
    --lagent-src /mnt/shared-storage-user/songdemin/user/haijun/code/gitlab/claude_code/lagent \
    --limit 10 \
    --concurrency 10 \
    --dump-dir $work_dir/dump \
    --report-dir $work_dir/reports \
    --verbose  | tee $work_dir/verbose_output.txt