"""Combined judger: rule + extractor in one flat criteria pool.

Runs rule_judge logic first, then extractor_judge logic, merges all
criteria into a single dict. Total = sum(score*weight) / sum(weight)
across ALL criteria from both sources.

Output: single JudgerResult JSON to stdout.
"""
from __future__ import annotations

import json
import os
import sys

# Both rule_judge and extractor_judge are co-located in the same dir at runtime.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from rule_judge import (  # noqa: E402
    Criterion,
    _to_str,
    extract_file_meta,
    _scan_deliverables,
    try_rule_verify,
)
from extractor_judge import (  # noqa: E402
    run_extractor,
    build_compare_prompt,
)
from simple_judge import (  # noqa: E402
    parse_judge_response,
    _llm_call_sync,
)


def main() -> None:
    task_workspace = os.environ.get("TASK_WORKSPACE", "")
    rubric_path = os.environ.get("RUBRIC_PATH", "")
    judger_name = os.environ.get("JUDGER_NAME", "combined_judger")
    llm_base_url = os.environ.get("JUDGE_MODEL_BASE_URL", "")
    llm_api_key = os.environ.get("JUDGE_MODEL_API_KEY", "EMPTY")
    llm_model = os.environ.get("JUDGE_MODEL_NAME", "")

    assert task_workspace, "TASK_WORKSPACE env var is required"
    assert rubric_path, "RUBRIC_PATH env var is required"

    with open(rubric_path) as f:
        rubric_data = json.load(f)

    raw_criteria = rubric_data.get("criteria", [])
    assert raw_criteria, f"No criteria found in rubric: {rubric_path}"

    criteria: dict[str, dict] = {}
    debug_details: dict[str, dict] = {}
    scores: list[float] = []
    total_weight = 0.0

    # ─── Part 1: Rule-based criteria ─────────────────────────────────────────
    rule_criteria = [c for c in raw_criteria if c.get("judge_method") == "rule"]
    if rule_criteria:
        deliverables = _scan_deliverables(task_workspace)
        all_meta: list[dict] = []
        for fpath in deliverables:
            try:
                all_meta.append(extract_file_meta(fpath))
            except Exception as e:
                all_meta.append({
                    "filename": os.path.basename(fpath),
                    "ext": os.path.splitext(fpath)[1],
                    "parse_error": str(e),
                })

        for i, c in enumerate(rule_criteria):
            crit = Criterion(
                criterion_id=c.get("criterion_id", f"rule_c_{i}"),
                criterion_type=c.get("criterion_type", "unknown"),
                weight=c.get("weight", 1),
                description=_to_str(c.get("description", "")),
                expected=_to_str(c.get("expected", "")),
            )
            rule_result = try_rule_verify(crit, deliverables, all_meta)

            if rule_result is not None:
                score = 1.0 if rule_result["satisfied"] else 0.0
            else:
                score = 0.0
                rule_result = {"satisfied": None, "evidence": "", "reason": "rule:no_match"}

            weight = float(c.get("weight", 1))
            criteria[crit.criterion_id] = {"score": score, "weight": weight}
            debug_details[crit.criterion_id] = {
                "source": "rule",
                "reason": rule_result.get("reason", ""),
                "evidence": rule_result.get("evidence", ""),
            }
            scores.append(score * weight)
            total_weight += weight

    # ─── Part 2: Extractor-based criteria ─────────────────────────────────────
    extractor_criteria = [c for c in raw_criteria if c.get("python_test")]
    if extractor_criteria:
        assert llm_base_url, "JUDGE_MODEL_BASE_URL env var is required"
        assert llm_model, "JUDGE_MODEL_NAME env var is required"

        for i, c in enumerate(extractor_criteria):
            cid = c.get("criterion_id", f"ext_c_{i}")
            description = c.get("description", "")
            expected = c.get("expected", "")
            python_test_rel = c.get("python_test", "")
            python_test_path_prefix = c.get("python_test_path_prefix", "")

            if python_test_path_prefix and os.path.isdir(python_test_path_prefix):
                script_path = os.path.join(python_test_path_prefix, python_test_rel)
            else:
                script_path = os.path.join(task_workspace, python_test_rel) if python_test_rel else ""

            extractor_output = run_extractor(
                task_dir=task_workspace,
                criterion_id=cid,
                script_path_override=script_path,
                path_prefix=task_workspace,
            )

            weight = float(c.get("weight", 1))

            if "error" in extractor_output:
                criteria[cid] = {"score": 0.0, "weight": weight}
                debug_details[cid] = {
                    "source": "extractor",
                    "error": extractor_output["error"],
                    "method": "extractor:error",
                }
                scores.append(0.0)
                total_weight += weight
                continue

            # Build LLM confirm prompt
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
                criteria[cid] = {"score": 0.0, "weight": weight}
                debug_details[cid] = {
                    "source": "extractor",
                    "error": f"bad output: {str(extractor_output)[:200]}",
                    "method": "extractor:bad_output",
                }
                scores.append(0.0)
                total_weight += weight
                continue

            # LLM confirm
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

            criteria[cid] = {"score": score, "weight": weight}
            debug_details[cid] = {
                "source": "extractor",
                "result": result_str,
                "method": method_tag,
                "reason": extras.get("reason", ""),
                "evidence": extras.get("evidence", ""),
            }
            scores.append(score * weight)
            total_weight += weight

    # ─── Compute flat total ───────────────────────────────────────────────────
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
