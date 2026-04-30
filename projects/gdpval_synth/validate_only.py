"""Standalone validate runner — skip infer, run only judgers on a mock workspace.

Use this to iterate on judger code without re-running a full agent rollout.
The "agent's deliverables" come from a local directory you supply (typically
``work_dir/dump/<task_id>/workspace/workspace`` from a previous full run).

Usage::

    python projects/gdpval_synth/validate_only.py \\
        --task-root /path/to/source/task_dir \\
        --workspace-dir /path/to/local/workspace_with_deliverables \\
        [--gateway http://env-gateway.ailab.ailab.ai]

Required env vars (for extractor_judger LLM call):
    JUDGE_MODEL_BASE_URL, JUDGE_MODEL_API_KEY, JUDGE_MODEL_NAME

The script:
  1. Boots a fresh sandbox.
  2. Uploads ``workspace-dir`` contents to ``/task_dir/home/workspace/``.
  3. Runs the same rule_judger + extractor_judger as the full pipeline.
  4. Prints the AggregatedScore JSON to stdout.

Why this exists: the full pipeline is infer (slow, calls LLM) + validate
(also slow but cheaper).  When debugging judger code, re-running infer to
regenerate identical deliverables is pure waste.  This skips it.
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_RUNNER_DIR = os.path.abspath(os.path.join(
    _HERE, "..", "..", "xtuner", "v1", "ray", "environment", "rl_task"
))
if _RUNNER_DIR not in sys.path:
    sys.path.insert(0, _RUNNER_DIR)

from lagent.serving.sandbox.providers.gateway import GatewayProvider  # noqa: E402

from gdpval_synth.pipeline import (  # noqa: E402
    DEFAULT_SANDBOX,
    _rule_judger,
    _extractor_judger,
)
from xtuner.v1.ray.environment.rl_task.sandbox import (  # noqa: E402
    exec_in,
    http_upload,
    tar_dir,
)
from xtuner.v1.ray.environment.rl_task.schemas import TaskData  # noqa: E402
from xtuner.v1.ray.environment.rl_task.validator import JudgerValidator  # noqa: E402


DEFAULT_GATEWAY = "http://env-gateway.ailab.ailab.ai"


async def _upload_workspace(client: Any, local_dir: Path, remote_root: str) -> None:
    """Tar local_dir and extract under remote_root in the sandbox."""
    blob = tar_dir(local_dir, arcname_prefix="")
    tmp = "/tmp/_validate_ws.tar.gz"
    await http_upload(client, tmp, base64.b64encode(blob).decode())
    await exec_in(client, f"mkdir -p {remote_root} && cd {remote_root} && tar xzf {tmp} && rm {tmp}")


async def main_async(args: argparse.Namespace) -> int:
    task_root = Path(args.task_root).resolve()
    ws_dir = Path(args.workspace_dir).resolve()
    assert task_root.is_dir(), f"--task-root not a dir: {task_root}"
    assert ws_dir.is_dir(), f"--workspace-dir not a dir: {ws_dir}"
    assert (task_root / "rubric.json").exists(), f"rubric.json missing under {task_root}"

    sandbox = DEFAULT_SANDBOX
    ws = sandbox.workspace_path

    provider = GatewayProvider(args.gateway)
    print(f"acquiring sandbox image={sandbox.image}", file=sys.stderr)
    client, env_id = await asyncio.to_thread(
        provider.create, image_tag=sandbox.image, ttl_seconds=sandbox.ttl_seconds,
    )
    print(f"sandbox env_id={env_id}", file=sys.stderr)

    # Wait until /health is OK before uploading.
    for _ in range(20):
        try:
            h = await asyncio.to_thread(client.health_check)
            if h.get("ok"):
                break
        except Exception:
            pass
        await asyncio.sleep(1)

    try:
        # 1. Mock infer: just upload the agent-produced workspace.
        print(f"uploading {ws_dir} -> {ws}", file=sys.stderr)
        await _upload_workspace(client, ws_dir, ws)

        # 2. Run validate (pipeline's judgers expect ctx["task_root"] to point
        #    at the host-side task dir so UploadHook can resolve rubric.json).
        validator = JudgerValidator(
            judgers=[_rule_judger(ws), _extractor_judger(ws)],
            aggregator="weighted_sum",
        )
        ctx: dict[str, Any] = {
            "task_root": task_root,
            "data": TaskData(
                id=task_root.name,
                data_source="gdpval-synth-validate-only",
                instruction="instruction.md",
            ),
            "uid": {"root_id": 0, "action_id": 0, "observation_id": 0},
            "runtime": {},
            "workspace": ws,
        }

        print("running validate", file=sys.stderr)
        aggregated = await validator.run(client, ctx, provider, ws)

        print(json.dumps(aggregated.model_dump(), ensure_ascii=False, indent=2))
        return 0 if not aggregated.failed else 1
    finally:
        try:
            await asyncio.to_thread(client.close)
        except Exception as exc:
            print(f"teardown failed: {exc}", file=sys.stderr)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--task-root", required=True,
                   help="Source task dir (must contain rubric.json, deliverable_spec.json).")
    p.add_argument("--workspace-dir", required=True,
                   help="Local dir whose contents become /workspace/ in the sandbox "
                        "(e.g. work_dir/dump/<task_id>/workspace/workspace).")
    p.add_argument("--gateway", default=DEFAULT_GATEWAY)
    args = p.parse_args()

    # Fail fast if the extractor_judger LLM env vars are not set — otherwise
    # pipeline.py's _extractor_judger() raises KeyError deep inside.
    for key in ("JUDGE_MODEL_BASE_URL", "JUDGE_MODEL_API_KEY", "JUDGE_MODEL_NAME"):
        assert key in os.environ, f"{key} env var must be set"

    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
