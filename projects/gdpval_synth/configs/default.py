"""Run gdpval-synth tasks with the default agent pipeline."""

from gdpval_synth.dataset import GdpvalSynth
from gdpval_synth.pipeline import gdpval_synth_pipeline

dataset = GdpvalSynth(
    tasks_root="/mnt/shared-storage-user/songdemin/user/haijun/code/gitlab/claude_code/dataset/gdpval_synth_v2/phase5_llm_verify/outputv1_glm5/rl_query_extract_dedup/gdpval_synth_tasks",
    pipeline=gdpval_synth_pipeline(),
)
