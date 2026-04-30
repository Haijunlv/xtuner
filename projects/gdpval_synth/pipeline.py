"""gdpval-synth pipeline factory — infer + rule/simple judger validation.

Read :func:`gdpval_synth_pipeline` top-to-bottom: the infer stage pre-hooks
upload wrappers, install lagent, pick an agent, mirror the task tree, then
run the agent entry.  Two judgers (rule + simple) share the infer sandbox
and parse their own JSON stdout.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

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
    Hook,
    ReadFileHook,
    SandboxStage,
    UploadHook,
    upload_tar_and_extract,
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
    message="/tmp/message.json",
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
    f"--trajectory-out {PATHS.trajectory} "
    f"--message-out {PATHS.message}"
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
    image="ubuntu2404-v1", ttl_seconds=1800, workspace_path="/task_dir/home/workspace",
)


# ─────────────────────────────────────────────────────────────────
# Judgers
# ─────────────────────────────────────────────────────────────────


class UploadPythonTestHook(Hook):
    """Upload python_test scripts from the host into the sandbox workspace.

    Scripts live at ``ctx["task_root"]/environment/data/python_test/`` on the
    host (excluded from the infer-stage upload to avoid leaking answers to the
    agent).  This hook uploads them into ``{workspace}/python_test/`` during
    the validate stage so the extractor judger can execute them.

    Falls back to ``python_test_path_prefix`` from the rubric if
    ``environment/data/python_test/`` does not exist.
    """

    name = "upload_python_test"

    def __init__(self, *, workspace: str) -> None:
        self.workspace = workspace

    async def __call__(self, client: Any, ctx: dict[str, Any]) -> None:
        task_root = Path(ctx["task_root"])

        # Primary: scripts live alongside the task data
        python_test_dir = task_root / "environment" / "data" / "python_test"

        # Fallback: read python_test_path_prefix from rubric
        if not python_test_dir.is_dir():
            rubric_path = task_root / "rubric.json"
            if not rubric_path.exists():
                return
            rubric = json.loads(rubric_path.read_text(encoding="utf-8"))
            for c in rubric.get("criteria", []):
                p = c.get("python_test_path_prefix", "")
                if p:
                    python_test_dir = Path(p) / "python_test"
                    break
            else:
                return

        if not python_test_dir.is_dir():
            return

        files: dict[str, Path] = {
            f"{self.workspace}/python_test/{f.name}": f
            for f in python_test_dir.iterdir()
            if f.is_file() and f.suffix == ".py"
        }
        if files:
            await upload_tar_and_extract(client, files, "/")


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
            timeout=1000,
            post=[ParseJudgerStdout("rule_judger")],
        ),
    )


def _simple_judger(ws: str) -> Judger:
    """Simple judger: lightweight checks (file counts, non-empty content,
    basic format validation).

    Not used in the default pipeline — kept for reference / manual testing.
    The default pipeline uses _extractor_judger instead.
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
                    # simple_judge.py imports from rule_judge — ship it too.
                    {"base": str(WRAPPERS / "gdpval_synth"),
                     "source": "rule_judge.py", "target": f"{root}/rule_judge.py"},
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
                "JUDGE_MODEL_BASE_URL": os.environ["JUDGE_MODEL_BASE_URL"],
                "JUDGE_MODEL_API_KEY": os.environ["JUDGE_MODEL_API_KEY"],
                "JUDGE_MODEL_NAME": os.environ["JUDGE_MODEL_NAME"],
            },
            timeout=1800,
            post=[ParseJudgerStdout("simple_judger")],
        ),
    )


def _extractor_judger(ws: str) -> Judger:
    """Extractor judger: runs python_test scripts in the sandbox then LLM-confirms.

    Handles all criteria that have a ``python_test`` field in the rubric.
    Failure is reported as ERROR — no fallback to simple_judger.
    """
    root = f"{PATHS.verifier}/extractor_judger"
    return Judger(
        name="extractor_judger",
        weight=1.0,
        sandbox="shared",
        stage=SandboxStage(
            pre=[
                UploadHook([
                    {"base": str(WRAPPERS / "gdpval_synth"),
                     "source": "extractor_judge*", "target": f"{root}/"},
                    # extractor_judge.py imports _llm_call_sync / parse_judge_response
                    {"base": str(WRAPPERS / "gdpval_synth"),
                     "source": "simple_judge.py", "target": f"{root}/simple_judge.py"},
                    # simple_judge.py imports from rule_judge — ship it too.
                    {"base": str(WRAPPERS / "gdpval_synth"),
                     "source": "rule_judge.py", "target": f"{root}/rule_judge.py"},
                ]),
                # Upload python_test scripts from host into sandbox workspace
                UploadPythonTestHook(workspace=ws),
                # rubric + spec 被 infer 阶段排除了，judger 单独上传
                UploadHook([
                    {"source": "rubric.json", "target": f"{ws}/"},
                    {"source": "deliverable_spec.json", "target": f"{ws}/"},
                ]),
            ],
            entry=f"bash {root}/extractor_judge_wrap.sh",
            env={
                "JUDGER_NAME": "extractor_judger",
                "TASK_WORKSPACE": ws,
                "TASK_JUDGER_DIR": root,
                "RUBRIC_PATH": f"{ws}/rubric.json",
                "DELIVERABLE_SPEC_PATH": f"{ws}/deliverable_spec.json",
                "JUDGE_MODEL_BASE_URL": os.environ["JUDGE_MODEL_BASE_URL"],
                "JUDGE_MODEL_API_KEY": os.environ["JUDGE_MODEL_API_KEY"],
                "JUDGE_MODEL_NAME": os.environ["JUDGE_MODEL_NAME"],
            },
            timeout=1800,
            post=[ParseJudgerStdout("extractor_judger")],
        ),
    )


def _combined_judger(ws: str) -> Judger:
    """Combined judger: runs rule_judge + extractor_judge in sequence,
    merges all criteria into a single flat pool for scoring.
    """
    root = f"{PATHS.verifier}/combined_judger"
    return Judger(
        name="combined_judger",
        weight=1.0,
        sandbox="shared",
        stage=SandboxStage(
            pre=[
                UploadHook([
                    {"base": str(WRAPPERS / "gdpval_synth"),
                     "source": "combined_judge*", "target": f"{root}/"},
                    {"base": str(WRAPPERS / "gdpval_synth"),
                     "source": "rule_judge*", "target": f"{root}/"},
                    {"base": str(WRAPPERS / "gdpval_synth"),
                     "source": "extractor_judge*", "target": f"{root}/"},
                    {"base": str(WRAPPERS / "gdpval_synth"),
                     "source": "simple_judge.py", "target": f"{root}/simple_judge.py"},
                ]),
                # Upload python_test scripts
                UploadPythonTestHook(workspace=ws),
                # rubric + spec
                UploadHook([
                    {"source": "rubric.json", "target": f"{ws}/"},
                    {"source": "deliverable_spec.json", "target": f"{ws}/"},
                ]),
            ],
            entry=f"bash {root}/combined_judge_wrap.sh",
            env={
                "JUDGER_NAME": "combined_judger",
                "TASK_WORKSPACE": ws,
                "TASK_JUDGER_DIR": root,
                "RUBRIC_PATH": f"{ws}/rubric.json",
                "DELIVERABLE_SPEC_PATH": f"{ws}/deliverable_spec.json",
                "JUDGE_MODEL_BASE_URL": os.environ["JUDGE_MODEL_BASE_URL"],
                "JUDGE_MODEL_API_KEY": os.environ["JUDGE_MODEL_API_KEY"],
                "JUDGE_MODEL_NAME": os.environ["JUDGE_MODEL_NAME"],
            },
            timeout=1800,
            post=[ParseJudgerStdout("combined_judger")],
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
            #    fixture files, judger config, and python_test (contains answers).
            UploadHook([
                {"source": "**/*", "target": f"{ws}/",
                 "exclude": ["metadata.json", "deliverable_files/**",
                             "rubric.json", "deliverable_spec.json",
                             "environment/data/python_test/**"]},
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
        env=BenchEnv(
            workspace=ws,
            extras={"WORKSPACE": ws, "GDPVAL_WORKSPACE": ws},
        ),
        timeout=1800,
        post=[
            DownloadHook([ws, "/tmp/agent_response.txt"]),
            ReadFileHook("/tmp/message.json", "message"),
        ],
    )

    validate = JudgerValidator(
        judgers=[_combined_judger(ws)],
        aggregator="weighted_sum",
    )

    return Runner(infer=infer, validate=validate)
