"""gdpval-synth pipeline factory — infer + rule/simple judger validation.

Read :func:`gdpval_synth_pipeline` top-to-bottom: the infer stage pre-hooks
upload wrappers, install lagent, pick an agent, mirror the task tree, then
run the agent entry.  Two judgers (rule + simple) share the infer sandbox
and parse their own JSON stdout.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from xtuner.v1.ray.environment.rl_task.hooks import (
    BenchEnv,
    InstallLagent,
    ParseJudgerStdout,
    PickAgent,
    RunAgentInstallDeps,
    UploadChosenAgent,
    WriteAgentConfig,
)
from xtuner.v1.ray.environment.rl_task.judgers import Judger
from xtuner.v1.ray.environment.rl_task.runner import Runner
from xtuner.v1.ray.environment.rl_task.sandbox import (
    DownloadHook,
    ExecHook,
    SandboxStage,
    UploadHook,
)
from xtuner.v1.ray.environment.rl_task.schemas import AgentSpec, SandboxSpec
from xtuner.v1.ray.environment.rl_task.validator import JudgerValidator


HERE = Path(__file__).resolve().parent
WRAPPERS = HERE / "wrappers"
AGENT_TEMPLATES = HERE / "agents"


# ─────────────────────────────────────────────────────────────────
# Sandbox runtime paths
# ─────────────────────────────────────────────────────────────────

PATHS = SimpleNamespace(
    wrappers_bench="/tmp/wrappers/gdpval_synth",
    wrappers_lagent="/tmp/wrappers/lagent",
    agent_config="/tmp/agent_config.json",
    trajectory="/tmp/trajectory.json",
    verifier="/tmp/verifier",
)


# ─────────────────────────────────────────────────────────────────
# Entry command
# ─────────────────────────────────────────────────────────────────

AGENT_ENTRY = (
    f"bash {PATHS.wrappers_bench}/pre_entry.sh && "
    f"bash {PATHS.wrappers_lagent}/lagent_entry.sh "
    f"--config {PATHS.agent_config} "
    f"--instruction-file $TASK_INSTRUCTION "
    f"--response-out /tmp/agent_response.txt "
    f"--trajectory-out {PATHS.trajectory}"
)


# ─────────────────────────────────────────────────────────────────
# Defaults
# ─────────────────────────────────────────────────────────────────

DEFAULT_AGENTS: list[AgentSpec] = [
    AgentSpec(
        name="internclaw",
        config="config.py",
        install="install-deps.sh",
        tools="tools",
        weight=1.0,
    ),
]

DEFAULT_SANDBOX = SandboxSpec(
    image="ubuntu2404-v1", ttl_seconds=1800, workspace_path="/workspace",
)


# ─────────────────────────────────────────────────────────────────
# Judgers
# ─────────────────────────────────────────────────────────────────


def _rule_judger(ws: str) -> Judger:
    """Rule-based judger: checks deliverable existence and rubric criteria
    via deterministic rules (no LLM).
    """
    root = f"{PATHS.verifier}/rule_judger"
    return Judger(
        name="rule_judger",
        weight=1.0,
        sandbox="shared",
        stage=SandboxStage(
            pre=[
                UploadHook([
                    {"base": str(WRAPPERS / "gdpval_synth"),
                     "source": "rule_judge*", "target": f"{root}/"},
                ]),
                # rubric + spec 被 infer 阶段排除了，judger 单独上传
                UploadHook([
                    {"source": "rubric.json", "target": f"{ws}/"},
                    {"source": "deliverable_spec.json", "target": f"{ws}/"},
                ]),
            ],
            entry=f"bash {root}/rule_judge_wrap.sh",
            env={
                "JUDGER_NAME": "rule_judger",
                "TASK_WORKSPACE": ws,
                "TASK_JUDGER_DIR": root,
                "RUBRIC_PATH": f"{ws}/rubric.json",
                "DELIVERABLE_SPEC_PATH": f"{ws}/deliverable_spec.json",
            },
            timeout=300,
            post=[ParseJudgerStdout("rule_judger")],
        ),
    )


def _simple_judger(ws: str) -> Judger:
    """Simple judger: lightweight checks (file counts, non-empty content,
    basic format validation).
    """
    root = f"{PATHS.verifier}/simple_judger"
    return Judger(
        name="simple_judger",
        weight=1.0,
        sandbox="shared",
        stage=SandboxStage(
            pre=[
                UploadHook([
                    {"base": str(WRAPPERS / "gdpval_synth"),
                     "source": "simple_judge*", "target": f"{root}/"},
                ]),
                # rubric + spec 被 infer 阶段排除了，judger 单独上传
                UploadHook([
                    {"source": "rubric.json", "target": f"{ws}/"},
                    {"source": "deliverable_spec.json", "target": f"{ws}/"},
                ]),
            ],
            entry=f"bash {root}/simple_judge_wrap.sh",
            env={
                "JUDGER_NAME": "simple_judger",
                "TASK_WORKSPACE": ws,
                "TASK_JUDGER_DIR": root,
                "RUBRIC_PATH": f"{ws}/rubric.json",
                "DELIVERABLE_SPEC_PATH": f"{ws}/deliverable_spec.json",
                "JUDGE_MODEL_BASE_URL": "${JUDGE_MODEL_BASE_URL}",
                "JUDGE_MODEL_API_KEY": "${JUDGE_MODEL_API_KEY}",
                "JUDGE_MODEL_NAME": "${JUDGE_MODEL_NAME}",
            },
            timeout=600,
            post=[ParseJudgerStdout("simple_judger")],
        ),
    )


# ─────────────────────────────────────────────────────────────────
# Pipeline factory
# ─────────────────────────────────────────────────────────────────


def gdpval_synth_pipeline(
    *,
    sandbox: SandboxSpec = DEFAULT_SANDBOX,
    agents: list[AgentSpec] = DEFAULT_AGENTS,
) -> Runner:
    """Build a Runner for a gdpval-synth task."""
    ws = sandbox.workspace_path

    infer = SandboxStage(
        sandbox=sandbox,
        pre=[
            # 1. Ship wrapper scripts (bench + lagent) into /tmp/wrappers/.
            UploadHook([
                {"base": str(WRAPPERS / "gdpval_synth"),
                 "source": "*", "target": PATHS.wrappers_bench + "/", "flatten": True},
                {"base": str(WRAPPERS / "lagent"),
                 "source": "*", "target": PATHS.wrappers_lagent + "/", "flatten": True},
            ]),
            # 2. Install lagent library + /tmp/lagent-py python wrapper.
            InstallLagent(),
            # 3. Weighted-pick one agent; record choice + template_root in ctx.
            PickAgent(agents=agents, template_root=str(AGENT_TEMPLATES)),
            # 4. Mirror task tree into workspace.  Exclude metadata + deliverable
            #    fixture files and judger config the agent should not see.
            UploadHook([
                {"source": "**/*", "target": f"{ws}/",
                 "exclude": ["metadata.json", "deliverable_files/**",
                             "rubric.json", "deliverable_spec.json"]},
            ]),
            # 5. Overlay chosen agent's template at workspace/agent/<name>/.
            UploadChosenAgent(target_dir=f"{ws}/agent/"),
            # 6. Ensure workspace dir exists.
            ExecHook(f"mkdir -p {ws}"),
            # 7. Exec chosen agent's config.py on host -> upload JSON.
            WriteAgentConfig(dst=PATHS.agent_config),
            # 8. Run install-deps.sh if the chosen agent template has one.
            RunAgentInstallDeps(workspace=ws),
        ],
        entry=AGENT_ENTRY,
        env=BenchEnv(workspace=ws),
        timeout=1800,
        post=[DownloadHook(["/workspace", "/tmp/agent_response.txt"])],
    )

    validate = JudgerValidator(
        judgers=[_rule_judger(ws), _simple_judger(ws)],
        aggregator="weighted_sum",
    )

    return Runner(infer=infer, validate=validate)
