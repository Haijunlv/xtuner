"""Extractor-based criterion judge.

Phase V: run pre-generated verification script → LLM lightweight confirm.

Script returns {"result": true/false, "detail": "..."}.
LLM only confirms the detail is reasonable, does NOT redo the computation.

Imports parse_judge_response and _llm_call_sync from simple_judge to reuse
response parsing and HTTP call logic.

Standalone usage:
    TASK_WORKSPACE=/task_dir/home/workspace \\
    RUBRIC_PATH=/path/to/rubric.json \\
    DELIVERABLE_SPEC_PATH=/path/to/deliverable_spec.json \\
    JUDGER_NAME=extractor_judger \\
    JUDGE_MODEL_BASE_URL=http://localhost:4000/v1 \\
    JUDGE_MODEL_API_KEY=EMPTY \\
    JUDGE_MODEL_NAME=glm-5-fp8 \\
    python extractor_judge.py

Output: JudgerResult JSON to stdout.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys

try:
    from .simple_judge import parse_judge_response, _llm_call_sync
except ImportError:
    from simple_judge import parse_judge_response, _llm_call_sync  # type: ignore[no-redef]


# ─── Compare prompt ───────────────────────────────────────────────────────────

COMPARE_PROMPT = """你是一个严格的评分员。一个 Python 验证脚本已经对以下评分标准执行了计算验证。

## 评分标准
{description}

## 原始期望 (Expected)
{expected}

## 脚本验证结果
- 结论: {script_result}
- 验证明细: {detail}

## 判定规则
1. 脚本已完成了完整的计算和对比，你只需确认脚本的验证逻辑是否合理
2. 如果脚本结论为 true 且验证明细看起来合理 → 输出 YES
3. 如果脚本结论为 false 且验证明细能说明问题 → 输出 NO
4. 如果脚本验证明细明显不合理（如逻辑错误、数据自相矛盾）→ 输出 UNCERTAIN
5. **不要重新计算**，信任脚本的计算结果，除非发现明显的逻辑漏洞

输出格式（仅返回 JSON，不要其他文字）：
{{"result": "YES 或 NO 或 UNCERTAIN", "evidence": "确认说明", "reason": "一句话总结"}}"""

# Keep COMPARE_PROMPTS dict for backward compatibility
COMPARE_PROMPTS: dict[str, str] = {"v1": COMPARE_PROMPT}


def build_compare_prompt(
    description: str,
    expected: str,
    extractor_output: dict,
) -> str:
    """Build LLM lightweight confirm prompt for extractor judge.

    extractor_output must contain {"result": bool, "detail": str}.
    """
    return COMPARE_PROMPT.format(
        description=description,
        expected=expected,
        script_result="通过 (true)" if extractor_output.get("result") else "未通过 (false)",
        detail=extractor_output.get("detail", "无"),
    )


def run_extractor(
    task_dir: str, criterion_id: str, timeout: int = 60,
    script_path_override: str = "", path_prefix: str = "",
) -> dict:
    """Execute extractors/<criterion_id>.py in task_dir and return JSON output.

    Expected output format:
      {"result": true/false, "detail": "..."}  on success (new computation-verification format)
      {"predict_value": "..."}                  on success (legacy value-extraction format)
      {"error": "..."}                          on failure

    Args:
        task_dir: Directory with reference_files/, deliverable_files/, extractors/.
                  Used as cwd so relative paths work.
        criterion_id: e.g. "accuracy_1"
        timeout: Subprocess timeout in seconds
        script_path_override: If set, use this script path directly instead of
                              looking up extractors/<criterion_id>.py.
        path_prefix: If set, replace /task_dir/home/workspace in the script
                     with this prefix, run from a temp copy, and use path_prefix
                     as cwd.

    Returns:
        {"result": bool, "detail": ...} or {"predict_value": ...} on success,
        {"error": ...} on failure.
    """
    # Locate script
    if script_path_override and os.path.isfile(script_path_override):
        script_path = script_path_override
    else:
        script_path = os.path.join(task_dir, "extractors", f"{criterion_id}.py")
        if not os.path.isfile(script_path):
            alt_path = os.path.join(task_dir, "python_test", f"{criterion_id}.py")
            if os.path.isfile(alt_path):
                script_path = alt_path
            else:
                return {"error": f"script not found: extractors/{criterion_id}.py"}

    # sed replace /task_dir/home/workspace → path_prefix (in-place)
    actual_script = script_path
    if path_prefix and os.path.isfile(script_path):
        with open(script_path) as f:
            content = f.read()
        # Strip /deliverable_files/ subdir first — python_test scripts reference
        # deliverables there but agent saves directly into workspace root.
        new_content = content.replace("/task_dir/home/workspace/deliverable_files/", f"{path_prefix}/")
        new_content = new_content.replace("/task_dir/home/workspace", path_prefix)
        if new_content != content:
            with open(script_path, "w") as f:
                f.write(new_content)

    actual_cwd = path_prefix or task_dir

    try:
        result = subprocess.run(
            ["python", actual_script],
            cwd=actual_cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"error": f"timeout after {timeout}s"}
    except Exception as e:
        return {"error": f"subprocess error: {e}"}

    stdout = result.stdout.strip()

    if result.returncode != 0 and not stdout:
        stderr = result.stderr.strip()[:500]
        return {"error": f"non-zero exit ({result.returncode}): {stderr}"}

    if not stdout:
        return {"error": "no stdout output from script"}

    # Try direct JSON parse
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        pass

    # Try extracting first JSON object from mixed output
    start = stdout.find("{")
    end = stdout.rfind("}") + 1
    if start >= 0 and end > start:
        try:
            return json.loads(stdout[start:end])
        except json.JSONDecodeError:
            pass

    return {"error": f"invalid JSON output: {stdout[:200]}"}


async def judge_with_extractor(
    client,
    model: str,
    task_dir: str,
    criterion: dict,
    semaphore: asyncio.Semaphore,
    timeout: int = 60,
    max_tokens: int = 2000,
    script_path_override: str = "",
    path_prefix: str = "",
) -> dict:
    """Full extractor judge flow: run verification script → LLM confirm → parse result.

    Handles both new format {"result": bool, "detail": "..."} and legacy
    {"predict_value": "..."} from the script output.

    Returns a dict compatible with simple_judge output format:
      {"criterion_id", "result", "satisfied", "method", "evidence", "reason"}
    """
    criterion_id = criterion.get("criterion_id", "")
    description = criterion.get("description", "")
    expected = criterion.get("expected", "")

    # Step 1: Run extractor script
    extractor_output = run_extractor(
        task_dir, criterion_id, timeout=timeout,
        script_path_override=script_path_override, path_prefix=path_prefix,
    )

    if "error" in extractor_output:
        return {
            "criterion_id": criterion_id,
            "result": "ERROR",
            "satisfied": False,
            "method": "extractor+llm_confirm:error",
            "evidence": "",
            "reason": f"extractor failed: {extractor_output['error']}",
        }

    # Step 2: Build confirm prompt based on output format
    if "result" in extractor_output and isinstance(extractor_output["result"], bool):
        # New computation-verification format
        prompt = build_compare_prompt(description, expected, extractor_output)
        method_tag = "extractor+llm_confirm"
    elif "predict_value" in extractor_output:
        # Legacy value-extraction format — use old-style compare
        predict_value = extractor_output.get("predict_value", str(extractor_output))
        legacy_prompt = (
            f"你是一个严格的评分员。\n\n"
            f"## 评分标准\n{description}\n\n"
            f"## 期望值 (Expected)\n{expected}\n\n"
            f"## 实际提取值 (Predicted)\n{predict_value}\n\n"
            f"对比 Predicted 和 Expected，判断实际值是否满足期望。\n"
            f"输出格式（仅返回 JSON）：\n"
            f'{{\"result\": \"YES 或 NO 或 UNCERTAIN\", \"evidence\": \"具体对比说明\", \"reason\": \"一句话总结\"}}'
        )
        prompt = legacy_prompt
        method_tag = "extractor+llm_judge"
    else:
        return {
            "criterion_id": criterion_id,
            "result": "ERROR",
            "satisfied": False,
            "method": "extractor+llm_confirm:error",
            "evidence": str(extractor_output),
            "reason": "script output missing 'result' bool or 'predict_value' field",
        }

    # Step 3: LLM confirm/compare
    async with semaphore:
        try:
            response = await client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=max_tokens,
            )
            answer_raw = (response.choices[0].message.content or "").strip()
            if not answer_raw:
                return {
                    "criterion_id": criterion_id,
                    "result": "ERROR",
                    "satisfied": False,
                    "method": f"{method_tag}:error",
                    "evidence": str(extractor_output),
                    "reason": "LLM confirm returned empty content",
                }
            result, extras = parse_judge_response(answer_raw)
        except Exception as e:
            return {
                "criterion_id": criterion_id,
                "result": "ERROR",
                "satisfied": False,
                "method": f"{method_tag}:error",
                "evidence": str(extractor_output),
                "reason": f"LLM confirm failed: {e}",
            }

    return {
        "criterion_id": criterion_id,
        "result": result,
        "satisfied": result == "YES",
        "method": method_tag,
        "evidence": extras.get("evidence", str(extractor_output)),
        "reason": extras.get("reason", ""),
    }


# ─── Standalone main ──────────────────────────────────────────────────────────


def main() -> None:
    """Standalone entry point. Reads env vars and outputs JudgerResult JSON."""
    task_workspace = os.environ.get("TASK_WORKSPACE", "")
    rubric_path = os.environ.get("RUBRIC_PATH", "")
    judger_name = os.environ.get("JUDGER_NAME", "extractor_judger")
    llm_base_url = os.environ.get("JUDGE_MODEL_BASE_URL", "")
    llm_api_key = os.environ.get("JUDGE_MODEL_API_KEY", "EMPTY")
    llm_model = os.environ.get("JUDGE_MODEL_NAME", "")

    assert task_workspace, "TASK_WORKSPACE env var is required"
    assert rubric_path, "RUBRIC_PATH env var is required"
    assert llm_base_url, "JUDGE_MODEL_BASE_URL env var is required"
    assert llm_model, "JUDGE_MODEL_NAME env var is required"

    # Load rubric
    with open(rubric_path) as f:
        rubric_data = json.load(f)

    raw_criteria = rubric_data.get("criteria", [])
    assert raw_criteria, f"No criteria found in rubric: {rubric_path}"

    # Filter: only criteria that have a python_test field
    extractor_criteria = [c for c in raw_criteria if c.get("python_test")]
    if not extractor_criteria:
        result = {"judger_name": judger_name, "total": 0.0, "criteria": {}}
        print(json.dumps(result, ensure_ascii=False))
        return

    criteria: dict[str, dict] = {}
    scores: list[float] = []
    debug_details: dict[str, dict] = {}

    for i, c in enumerate(extractor_criteria):
        cid = c.get("criterion_id", f"c_{i}")
        description = c.get("description", "")
        expected = c.get("expected", "")
        python_test_rel = c.get("python_test", "")
        # python_test_path_prefix points to an alternate HOST directory containing
        # python_test/ scripts (used when running on host for testing).  Inside the
        # sandbox, scripts are uploaded to task_workspace/python_test/ by the hook.
        python_test_path_prefix = c.get("python_test_path_prefix", "")

        if python_test_path_prefix and os.path.isdir(python_test_path_prefix):
            script_path = os.path.join(python_test_path_prefix, python_test_rel)
        else:
            script_path = os.path.join(task_workspace, python_test_rel) if python_test_rel else ""

        # Step 1: Run the verification script.
        # Always pass path_prefix=task_workspace so that hardcoded
        # /task_dir/home/workspace/deliverable_files/ paths in scripts get
        # rewritten to point at the actual workspace root.
        extractor_output = run_extractor(
            task_dir=task_workspace,
            criterion_id=cid,
            script_path_override=script_path,
            path_prefix=task_workspace,
        )

        if "error" in extractor_output:
            weight = float(c.get("weight", 1))
            criteria[cid] = {"score": 0.0, "weight": weight}
            debug_details[cid] = {
                "error": extractor_output["error"],
                "method": "extractor:error",
            }
            scores.append(0.0)
            continue

        # Step 2: Build LLM confirm prompt
        if "result" in extractor_output and isinstance(extractor_output["result"], bool):
            prompt = build_compare_prompt(description, expected, extractor_output)
            method_tag = "extractor+llm_confirm"
        elif "predict_value" in extractor_output:
            predict_value = extractor_output.get("predict_value", str(extractor_output))
            prompt = (
                f"你是一个严格的评分员。\n\n"
                f"## 评分标准\n{description}\n\n"
                f"## 期望值 (Expected)\n{expected}\n\n"
                f"## 实际提取值 (Predicted)\n{predict_value}\n\n"
                f"对比 Predicted 和 Expected，判断实际值是否满足期望。\n"
                f"输出格式（仅返回 JSON）：\n"
                f'{{"result": "YES 或 NO 或 UNCERTAIN", "evidence": "具体对比说明", "reason": "一句话总结"}}'
            )
            method_tag = "extractor+llm_judge"
        else:
            weight = float(c.get("weight", 1))
            criteria[cid] = {"score": 0.0, "weight": weight}
            debug_details[cid] = {
                "error": f"script output missing 'result' bool or 'predict_value': {str(extractor_output)[:200]}",
                "method": "extractor:bad_output",
            }
            scores.append(0.0)
            continue

        # Step 3: LLM confirm
        extras: dict = {}
        try:
            answer_raw = _llm_call_sync(llm_base_url, llm_api_key, llm_model, prompt)
            assert answer_raw, "LLM returned empty content"
            result_str, extras = parse_judge_response(answer_raw)
        except Exception as e:
            result_str = "ERROR"
            extras = {"reason": f"LLM confirm failed: {e}"}

        score_map = {"YES": 1.0, "NO": 0.0, "UNCERTAIN": 0.5, "ERROR": 0.0}
        score = score_map.get(result_str, 0.0)
        weight = float(c.get("weight", 1))

        criteria[cid] = {"score": score, "weight": weight}
        debug_details[cid] = {
            "result": result_str,
            "method": method_tag,
            "reason": extras.get("reason", ""),
            "evidence": extras.get("evidence", ""),
        }
        scores.append(score * weight)

    total_weight = sum(float(c.get("weight", 1)) for c in extractor_criteria)
    total = sum(scores) / total_weight if total_weight > 0 else 0.0

    output = {
        "judger_name": judger_name,
        "total": round(total, 4),
        "criteria": criteria,
        "metadata": debug_details,
    }
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
