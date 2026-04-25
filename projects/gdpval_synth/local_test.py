#!/usr/bin/env python3
"""Local simulation of the gdpval_synth Agent Rollout pipeline.

Bypasses sandbox entirely — runs everything on the local filesystem.

Three modes:
  1. --mode oracle   (default)  Copy deliverable_files/ as mock agent output,
                                then run both judgers.  Expect near-perfect scores.
  2. --mode empty               Don't produce any agent output — just run judgers
                                on an empty workspace.  Expect 0 scores.
  3. --mode agent               Actually start the lagent daemon, run the agent,
                                then judge.  Requires lagent + LLM env vars.

Usage:
  # Quick smoke test with oracle answers (no LLM needed for rule_judger)
  python projects/gdpval_synth/local_test.py \
      --tasks-root /tmp/gdpval_synth_tasks \
      --mode oracle --limit 2

  # Run with simple_judger (needs JUDGE_MODEL_* env vars)
  python projects/gdpval_synth/local_test.py \
      --tasks-root /tmp/gdpval_synth_tasks \
      --mode oracle --limit 1 --judger both

  # Only rule_judger (no LLM needed)
  python projects/gdpval_synth/local_test.py \
      --tasks-root /tmp/gdpval_synth_tasks \
      --mode oracle --limit 3 --judger rule
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WRAPPER_DIR = HERE / "wrappers" / "gdpval_synth"


# ── helpers ─────────────────────────────────────────────────────────

def _find_python() -> str:
    """Find a usable python3 with openpyxl/docx available."""
    # Try current interpreter first
    candidates = [
        sys.executable,
        "/mnt/llm-ai-infra/miniconda3/envs/train/bin/python3",
        "python3",
    ]
    for py in candidates:
        if not py:
            continue
        try:
            subprocess.run(
                [py, "-c", "import openpyxl; import docx"],
                capture_output=True, timeout=10, check=True,
            )
            return py
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            continue
    # fallback — best effort
    return sys.executable


def _iter_tasks(tasks_root: Path, limit: int) -> list[Path]:
    """List task directories (sorted, limited)."""
    dirs = sorted(
        d for d in tasks_root.iterdir()
        if d.is_dir() and (d / "metadata.json").exists()
    )
    if limit > 0:
        dirs = dirs[:limit]
    return dirs


# ── workspace preparation ───────────────────────────────────────────

def _prepare_workspace(task_dir: Path, ws: Path, mode: str) -> None:
    """Simulate what the sandbox pre-hooks do: upload task data + optional
    agent output into a flat workspace.
    """
    ws.mkdir(parents=True, exist_ok=True)

    # 1. Copy instruction, rubric, deliverable_spec, environment/
    for item in ("instruction.md", "rubric.json", "deliverable_spec.json"):
        src = task_dir / item
        if src.exists():
            shutil.copy2(src, ws / item)

    env_data = task_dir / "environment" / "data"
    if env_data.is_dir():
        for f in env_data.iterdir():
            if f.is_file():
                shutil.copy2(f, ws / f.name)
            elif f.is_dir():
                shutil.copytree(f, ws / f.name, dirs_exist_ok=True)

    # 2. Mode-specific: copy oracle outputs or leave empty
    if mode == "oracle":
        deliv = task_dir / "deliverable_files"
        if deliv.is_dir():
            for f in deliv.iterdir():
                if f.is_file():
                    shutil.copy2(f, ws / f.name)
                elif f.is_dir():
                    shutil.copytree(f, ws / f.name, dirs_exist_ok=True)
    # mode == "empty" → nothing extra


# ── run a single judger ─────────────────────────────────────────────

def _run_judger(
    judger_script: str,
    ws: Path,
    python: str,
    extra_env: dict[str, str] | None = None,
) -> dict:
    """Run rule_judge.py or simple_judge.py locally, return parsed JSON."""
    script = WRAPPER_DIR / judger_script
    assert script.exists(), f"Judger script not found: {script}"

    env = {
        **os.environ,
        "TASK_WORKSPACE": str(ws),
        "RUBRIC_PATH": str(ws / "rubric.json"),
        "DELIVERABLE_SPEC_PATH": str(ws / "deliverable_spec.json"),
    }
    if extra_env:
        env.update(extra_env)

    proc = subprocess.run(
        [python, str(script)],
        capture_output=True, text=True, timeout=300, env=env,
    )

    # stderr → log
    if proc.stderr:
        for line in proc.stderr.strip().splitlines():
            print(f"  {line}", file=sys.stderr)

    # Parse stdout last line as JSON
    stdout = proc.stdout.strip()
    if not stdout:
        return {"judger_name": judger_script, "total": 0.0, "error": "no output"}

    # Take last non-empty line (judger contract)
    last_line = [l for l in stdout.splitlines() if l.strip()][-1]
    try:
        return json.loads(last_line)
    except json.JSONDecodeError:
        return {"judger_name": judger_script, "total": 0.0, "error": f"invalid JSON: {last_line[:200]}"}


# ── main ────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Local gdpval_synth pipeline simulation")
    parser.add_argument("--tasks-root", required=True, help="Materialized tasks directory")
    parser.add_argument("--mode", choices=["oracle", "empty", "agent"], default="oracle",
                        help="oracle=use reference answers, empty=no agent output, agent=run lagent")
    parser.add_argument("--judger", choices=["rule", "simple", "both"], default="both",
                        help="Which judger(s) to run")
    parser.add_argument("--limit", type=int, default=3, help="Max tasks to process (0=all)")
    parser.add_argument("--keep-ws", action="store_true", help="Don't clean up workspace dirs")
    parser.add_argument("--task-id", type=str, default="", help="Run a single task by ID")
    args = parser.parse_args()

    tasks_root = Path(args.tasks_root).resolve()
    assert tasks_root.is_dir(), f"Not a directory: {tasks_root}"

    python = _find_python()
    print(f"Python: {python}")
    print(f"Mode:   {args.mode}")
    print(f"Judger: {args.judger}")
    print()

    # Collect tasks
    if args.task_id:
        task_dir = tasks_root / args.task_id
        assert task_dir.is_dir(), f"Task not found: {task_dir}"
        task_dirs = [task_dir]
    else:
        task_dirs = _iter_tasks(tasks_root, args.limit)

    print(f"Tasks:  {len(task_dirs)}")
    print("=" * 72)

    # Aggregate stats
    all_results: list[dict] = []

    for i, task_dir in enumerate(task_dirs, 1):
        task_id = task_dir.name
        meta = json.loads((task_dir / "metadata.json").read_text())
        print(f"\n[{i}/{len(task_dirs)}] {task_id}")
        print(f"  sector={meta.get('sector')}, type={meta.get('task_type')}")

        # Count criteria by method
        rubric = json.loads((task_dir / "rubric.json").read_text())
        criteria = rubric.get("criteria", [])
        method_counts = {}
        for c in criteria:
            m = c.get("judge_method", "unknown")
            method_counts[m] = method_counts.get(m, 0) + 1
        print(f"  criteria: {method_counts}")

        # Prepare workspace
        tmp_base = Path(tempfile.mkdtemp(prefix=f"gdpval_{task_id}_"))
        ws = tmp_base / "workspace"
        _prepare_workspace(task_dir, ws, args.mode)

        ws_files = list(ws.iterdir())
        print(f"  workspace files: {[f.name for f in ws_files]}")

        task_result = {"task_id": task_id, "meta": meta}

        # Run rule_judger
        if args.judger in ("rule", "both"):
            print("  --- rule_judger ---")
            rule_res = _run_judger(
                "rule_judge.py", ws, python,
                extra_env={"JUDGER_NAME": "rule_judger"},
            )
            task_result["rule_judger"] = rule_res
            print(f"  rule_judger total: {rule_res.get('total', 'N/A')}")
            if rule_res.get("error"):
                print(f"  rule_judger error: {rule_res['error']}")

        # Run simple_judger
        if args.judger in ("simple", "both"):
            judge_url = os.environ.get("JUDGE_MODEL_BASE_URL", "")
            if not judge_url:
                print("  --- simple_judger SKIPPED (JUDGE_MODEL_BASE_URL not set) ---")
                task_result["simple_judger"] = {"skipped": True, "reason": "no JUDGE_MODEL_BASE_URL"}
            else:
                print("  --- simple_judger ---")
                simple_res = _run_judger(
                    "simple_judge.py", ws, python,
                    extra_env={
                        "JUDGER_NAME": "simple_judger",
                        "JUDGE_MODEL_BASE_URL": judge_url,
                        "JUDGE_MODEL_API_KEY": os.environ.get("JUDGE_MODEL_API_KEY", ""),
                        "JUDGE_MODEL_NAME": os.environ.get("JUDGE_MODEL_NAME", ""),
                    },
                )
                task_result["simple_judger"] = simple_res
                print(f"  simple_judger total: {simple_res.get('total', 'N/A')}")
                if simple_res.get("error"):
                    print(f"  simple_judger error: {simple_res['error']}")

        all_results.append(task_result)

        # Cleanup
        if not args.keep_ws:
            shutil.rmtree(tmp_base, ignore_errors=True)
        else:
            print(f"  workspace kept at: {ws}")

    # ── Summary ──────────────────────────────────────────────────────
    print("\n" + "=" * 72)
    print("SUMMARY")
    print("=" * 72)

    for r in all_results:
        tid = r["task_id"]
        rule_score = r.get("rule_judger", {}).get("total", "-")
        simple_info = r.get("simple_judger", {})
        if simple_info.get("skipped"):
            simple_score = "skipped"
        else:
            simple_score = simple_info.get("total", "-")
        print(f"  {tid}  rule={rule_score}  simple={simple_score}")

    # Averages
    rule_scores = [r["rule_judger"]["total"] for r in all_results
                   if "rule_judger" in r and isinstance(r["rule_judger"].get("total"), (int, float))]
    simple_scores = [r["simple_judger"]["total"] for r in all_results
                     if "simple_judger" in r and isinstance(r["simple_judger"].get("total"), (int, float))]

    if rule_scores:
        print(f"\n  rule_judger avg:   {sum(rule_scores)/len(rule_scores):.4f}  (n={len(rule_scores)})")
    if simple_scores:
        print(f"  simple_judger avg: {sum(simple_scores)/len(simple_scores):.4f}  (n={len(simple_scores)})")

    # Write detailed results
    out_json = Path(f"/tmp/gdpval_local_test_results.json")
    out_json.write_text(json.dumps(all_results, ensure_ascii=False, indent=2))
    print(f"\n  Detailed results: {out_json}")


if __name__ == "__main__":
    main()
