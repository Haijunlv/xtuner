"""LLM-based simple judge for GDPVal Synth criterion verification.

Can be used as a standalone script or imported as a module.
Uses synchronous urllib.request for LLM calls (no extra dependencies).

Standalone usage:
    TASK_WORKSPACE=/path/to/task \
    RUBRIC_PATH=/path/to/rubric.json \
    DELIVERABLE_SPEC_PATH=/path/to/spec.json \
    JUDGER_NAME=simple_judge \
    JUDGE_MODEL_BASE_URL=http://localhost:4000/v1 \
    JUDGE_MODEL_API_KEY=EMPTY \
    JUDGE_MODEL_NAME=glm-5-fp8 \
    python simple_judge.py

Output: JudgerResult JSON to stdout.
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
import urllib.error

import openpyxl
import pdfplumber
from docx import Document as DocxDocument

try:
    from .rule_judge import (
        Criterion,
        _to_str,
        extract_file_meta,
        _scan_deliverables,
    )
except ImportError:
    from rule_judge import (  # type: ignore[no-redef]
        Criterion,
        _to_str,
        extract_file_meta,
        _scan_deliverables,
    )


# ─── File summarization ──────────────────────────────────────────────────────


def summarize_xlsx(path: str, max_rows: int = 500) -> str:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    parts = [f"Workbook: {os.path.basename(path)}", f"Sheets: {wb.sheetnames}"]
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        parts.append(f"\n--- Sheet: {sheet_name} ---")
        row_count = 0
        for row in ws.iter_rows(values_only=True):
            if row_count >= max_rows:
                parts.append(f"... (truncated at {max_rows} rows)")
                break
            cells = [str(c) if c is not None else "" for c in row]
            if any(c is not None for c in row):
                parts.append("\t".join(cells))
                row_count += 1
    wb.close()
    return "\n".join(parts)


def summarize_pdf(path: str, max_pages: int = 20) -> str:
    parts = [f"PDF: {os.path.basename(path)}"]
    with pdfplumber.open(path) as pdf:
        parts.append(f"Pages: {len(pdf.pages)}")
        for i, page in enumerate(pdf.pages[:max_pages]):
            text = page.extract_text() or ""
            parts.append(f"\n--- Page {i + 1} ---")
            parts.append(text)
        if len(pdf.pages) > max_pages:
            parts.append(f"... (truncated at {max_pages} pages)")
    return "\n".join(parts)


def summarize_docx(path: str, max_paragraphs: int = 500) -> str:
    parts = [f"Document: {os.path.basename(path)}"]
    doc = DocxDocument(path)
    parts.append(f"Paragraphs: {len(doc.paragraphs)}")
    count = 0
    for para in doc.paragraphs:
        if count >= max_paragraphs:
            parts.append(f"... (truncated at {max_paragraphs} paragraphs)")
            break
        text = para.text.strip()
        if text:
            parts.append(text)
            count += 1
    for i, table in enumerate(doc.tables):
        parts.append(f"\n--- Table {i + 1} ---")
        for row in table.rows:
            cells = [cell.text.strip() for cell in row.cells]
            parts.append("\t".join(cells))
    return "\n".join(parts)


def summarize_file(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".xlsx", ".xls", ".xlsm"):
        return summarize_xlsx(path)
    elif ext == ".pdf":
        return summarize_pdf(path)
    elif ext in (".docx", ".doc"):
        return summarize_docx(path)
    return f"Unsupported file type: {ext}"


def format_file_metadata(all_meta: list[dict]) -> str:
    lines = [f"Files found: {len(all_meta)}"]
    for i, m in enumerate(all_meta, 1):
        ext = m.get("ext", "")
        if "parse_error" in m:
            lines.append(f"  {i}. {m.get('filename', '?')} ({ext}) — PARSE ERROR: {m['parse_error']}")
            continue
        if ext in (".xlsx", ".xls", ".xlsm"):
            total_rows = sum(m.get("row_counts", {}).values())
            lines.append(f"  {i}. {m.get('filename', '?')} ({ext}, {len(m.get('sheet_names', []))} sheets, {total_rows} rows)")
            for sn in m.get("sheet_names", []):
                hdrs = m.get("headers", {}).get(sn, [])
                rc = m.get("row_counts", {}).get(sn, 0)
                lines.append(f"     Sheet '{sn}' ({rc} rows): {hdrs}")
        elif ext == ".pdf":
            lines.append(f"  {i}. {m.get('filename', '?')} ({ext}, {m.get('page_count', '?')} pages)")
        elif ext in (".docx", ".doc"):
            lines.append(f"  {i}. {m.get('filename', '?')} ({ext}, {m.get('paragraph_count', '?')} paragraphs)")
            if m.get("headings"):
                lines.append(f"     Headings: {m['headings']}")
    return "\n".join(lines)


# ─── LLM judge prompts ───────────────────────────────────────────────────────
#
# 每个 prompt 版本都有唯一编号，方便 A/B 测试对比。
# 通过 get_judge_prompt(version) 获取，默认 "v1"。
#
# 使用方式：
#   - gdpval_synth_verify.py CLI:  --prompt-version v2
#   - standalone simple_judge.py:  JUDGE_PROMPT_VERSION=v2
#

JUDGE_PROMPTS: dict[str, str] = {}

# ── v1: 原始 prompt（baseline） ──────────────────────────────────────────────
JUDGE_PROMPTS["v1"] = """You are a strict grader evaluating whether a deliverable meets a rubric criterion.

## File Metadata
{file_metadata}

## File Content
{file_summary}

## Criterion
{criterion}

## Expected
{expected}

## Instructions
1. Identify what the criterion requires.
2. Search the file content for evidence that satisfies or contradicts the criterion.
3. If the file content is truncated, base your judgment only on the available content.
4. Respond in this exact JSON format (no other text):
{{"result": "YES or NO or UNCERTAIN", "evidence": "quote or describe the specific evidence found", "reason": "one sentence explanation"}}

- YES: clear evidence the criterion is satisfied.
- NO: clear evidence it is NOT satisfied, or required content is absent.
- UNCERTAIN: file content is insufficient to judge."""

# ── v2: 严格匹配 prompt（针对 cv32 偏宽松问题优化） ──────────────────────────
JUDGE_PROMPTS["v2"] = """You are a strict grader evaluating whether a deliverable meets a rubric criterion.
You must judge based ONLY on what is explicitly present in the file content below. Do NOT infer, assume, or fabricate any information.

## File Metadata
{file_metadata}

## File Content
{file_summary}

## Criterion
{criterion}

## Expected
{expected}

## Strict Matching Rules (CRITICAL — apply every rule before answering)
1. **Exact name matching**: Sheet names, column headers, section titles, and file names must match the expected values EXACTLY (case-insensitive). "Sales Analysis Pivot Table" ≠ "Sales Analysis Pivot". "Target Date" ≠ "Due Date". "Responsible Party" ≠ "Owner". If names differ, answer NO.
2. **Exact value matching**: Numerical values must match the expected values precisely. 0.0728 ≠ 7.3%. $4,800 ≠ $4,200. If values differ, answer NO.
3. **Completeness**: If the criterion requires N items (e.g., 5 CPT codes, 3 columns), ALL N must be present. Partial fulfillment (e.g., 4 out of 5) is NO.
4. **Formatting requirements**: If the criterion specifies a format (currency with 2 decimals, date format, percentage format), the actual data must visibly conform. Plain integers do not satisfy "currency with 2 decimal places".
5. **No evidence fabrication**: Only cite data you can directly quote from the file content above. If a sheet, column, or value is not visible in the content, do not claim it exists.
6. **When in doubt, answer NO**: If you cannot find clear, direct evidence in the file content, the answer is NO, not UNCERTAIN.

## Instructions
1. Identify what the criterion requires — list each specific requirement.
2. For EACH requirement, search the file content for EXACT matching evidence. Quote the specific text found.
3. If ANY requirement is not met, the result is NO.
4. Respond in this exact JSON format (no other text):
{{"result": "YES or NO", "evidence": "quote the exact text from file content that supports your judgment", "reason": "one sentence explaining which specific requirement passed or failed"}}

- YES: every requirement is explicitly satisfied with exact evidence in the file content.
- NO: any requirement is not met, or required content is absent, or values/names do not match exactly."""

# ── v3: 逐项对比 prompt（解决"声称 exact 但实际不 exact"问题） ────────────────
# v2 的问题：cv32 会在 evidence 里写 "matches exactly" 但引用的文本和 expected 实际
# 不同（语义等价、空格/下划线差异、同义词替换）。v3 通过强制逐项拆解 + 字面对比来解决。
JUDGE_PROMPTS["v3"] = """You are a strict grader evaluating whether a deliverable meets a rubric criterion.
You must judge based ONLY on what is explicitly present in the file content below.

## File Metadata
{file_metadata}

## File Content
{file_summary}

## Criterion
{criterion}

## Expected
{expected}

## Verification Procedure (follow these steps IN ORDER)

### Step 1: Extract checklist
List every concrete requirement from the Criterion and Expected fields. Each requirement is ONE of:
  - A specific name (sheet name, column header, section title, file name)
  - A specific value (number, percentage, date, text string)
  - A formatting rule (currency format, decimal places, date format)
  - A structural rule (number of sheets, rows, columns, sections)

### Step 2: Search and quote
For EACH requirement from Step 1, search the File Content and File Metadata above.
  - If found: copy the EXACT text from the file content (verbatim, including spacing and punctuation).
  - If not found: write "NOT FOUND in file content".

### Step 3: Compare literally
For EACH requirement, place the expected value and the actual quoted value side by side. Check:
  - Names: must be CHARACTER-BY-CHARACTER identical (case-insensitive). These are ALL different and count as mismatches:
    * "Product Name" vs "Product" (extra word)
    * "Unit Cost ($)" vs "Unit Cost" (extra suffix)
    * "Enrollment_Metrics" vs "Enrollment Metrics" (underscore vs space)
    * "Seller" vs "Vendor" (synonym is NOT a match)
    * "Active_Patients" vs "Active Participants" (different word)
    * "Due Date" vs "Target Date" (different word)
  - Values: must be numerically identical. 0.0728 ≠ 7.3%. $4,800 ≠ $4,200. 2025-07-31 ≠ 2025-07-20.
  - Formatting: the displayed text must visibly conform. "50500000" does not satisfy "currency with 2 decimal places". Tables using "%" do not satisfy "spell out per cent".
  - Completeness: if N items required, all N must pass. 4 out of 5 = FAIL.

### Step 4: Verdict
  - If ALL requirements pass Step 3: result is YES.
  - If ANY requirement fails Step 3 or was NOT FOUND: result is NO.

## Output format
Respond with ONLY this JSON (no other text):
{{"result": "YES or NO", "evidence": "For each requirement: expected='X' vs actual='Y' — PASS/FAIL", "reason": "one sentence summary: which requirement(s) failed, or all passed"}}"""

# ── v4: 明确容忍/不容忍边界（基于人工验证校准） ──────────────────────────────
# v3 的问题：cv32 对 "字面匹配" 执行不一致，completeness 类放过下划线/空格差异
# 而 formatting 类拒绝。v4 明确列出哪些差异可容忍、哪些不可容忍。
JUDGE_PROMPTS["v4"] = """You are a strict grader evaluating whether a deliverable meets a rubric criterion.
You must judge based ONLY on what is explicitly present in the file content below.

## File Metadata
{file_metadata}

## File Content
{file_summary}

## Criterion
{criterion}

## Expected
{expected}

## Verification Procedure (follow these steps IN ORDER)

### Step 1: Extract checklist
List every concrete requirement from the Criterion and Expected fields. Each requirement is ONE of:
  - A specific name (sheet name, column header, section title, file name)
  - A specific value (number, percentage, date, text string)
  - A formatting rule (currency format, decimal places, date format)
  - A structural rule (number of sheets, rows, columns, sections)

### Step 2: Search and quote
For EACH requirement from Step 1, search the File Content and File Metadata above.
  - If found: copy the EXACT text from the file content (verbatim).
  - If not found: write "NOT FOUND in file content".

### Step 3: Compare with tolerance rules
For EACH requirement, compare the expected value with the actual quoted value using THESE RULES:

TOLERATED differences (still counts as PASS):
  ✓ Underscore vs space: "Total_Tests" = "Total Tests" — PASS
  ✓ Numeric equivalence: "3750.0" = "3750" — PASS
  ✓ Leading numbering prefix: "1. Executive Summary" = "Executive Summary" — PASS
  ✓ Case difference: "revenue" = "Revenue" — PASS

NOT TOLERATED differences (counts as FAIL):
  ✗ Missing words: "Division_Name" ≠ "Division" — FAIL (word removed)
  ✗ Extra words: "Community Garden" ≠ "Community Garden Zones" — FAIL (word added)
  ✗ Parenthetical suffix: "Tier 1" ≠ "Tier 1 (Excellent)" — FAIL (suffix added)
  ✗ Synonyms: "Seller" ≠ "Vendor", "Due Date" ≠ "Target Date" — FAIL
  ✗ Different abbreviation: "Completed_Surveys" ≠ "Completed" — FAIL (word removed)
  ✗ Column name suffix: "Unit Cost" ≠ "Unit Cost ($)" — FAIL (suffix added)
  ✗ Numeric mismatch: 0.0728 ≠ 7.3%, $4,800 ≠ $4,200, 2025-07-31 ≠ 2025-07-20 — FAIL
  ✗ Format violation: "50500000" does not satisfy "currency with 2 decimal places" — FAIL
  ✗ NOT FOUND in file content — FAIL

The key rule: after normalizing underscores/spaces/case/numeric decimals, the CORE WORDS must be identical. Adding, removing, or replacing any word is FAIL.

### Step 4: Verdict
  - ALL requirements PASS → result is YES
  - ANY requirement FAIL or NOT FOUND → result is NO

## Output format
Respond with ONLY this JSON (no other text):
{{"result": "YES or NO", "evidence": "For each requirement: expected='X' vs actual='Y' — PASS/FAIL (reason)", "reason": "one sentence: which requirement(s) failed, or all passed"}}"""

# ── 默认版本 & 兼容别名 ─────────────────────────────────────────────────────
JUDGE_PROMPT_DEFAULT = JUDGE_PROMPTS["v1"]
DEFAULT_PROMPT_VERSION = "v3"


def get_judge_prompt(version: str | None = None) -> str:
    """按编号获取 judge prompt。未指定或不存在时返回默认版本。"""
    ver = (version or DEFAULT_PROMPT_VERSION).strip().lower()
    if ver not in JUDGE_PROMPTS:
        available = ", ".join(sorted(JUDGE_PROMPTS.keys()))
        raise ValueError(f"Unknown prompt version '{ver}'. Available: {available}")
    return JUDGE_PROMPTS[ver]


def _sanitize_json_string(raw: str) -> str:
    """Clean special characters in LLM output that break JSON parsing."""
    raw = re.sub(r"```json\s*", "", raw)
    raw = re.sub(r"```\s*$", "", raw)

    def _fix_control_chars(m: re.Match) -> str:
        val = m.group(0)
        val = val.replace("\t", "\\t")
        val = val.replace("\n", "\\n")
        val = val.replace("\r", "\\r")
        return val

    raw = re.sub(r'"(?:[^"\\]|\\.)*"', _fix_control_chars, raw, flags=re.DOTALL)
    return raw


def _extract_field_by_regex(raw: str, field: str) -> str:
    """Extract field value from malformed JSON using regex."""
    m = re.search(rf'"{field}"\s*:\s*"((?:[^"\\]|\\.)*)"', raw, re.DOTALL)
    if m:
        return m.group(1).replace("\\n", "\n").replace("\\t", "\t")[:500]
    m = re.search(rf'"{field}"\s*:\s*([^"{{}}][^,}}]*)', raw)
    if m:
        return m.group(1).strip()[:500]
    return ""


def parse_judge_response(raw: str) -> tuple[str, dict]:
    """Parse LLM JSON output. Returns (result, extras)."""
    # Pass 1: direct parse
    try:
        decoder = json.JSONDecoder()
        idx = raw.find('{')
        while idx >= 0:
            try:
                obj, _ = decoder.raw_decode(raw, idx)
                if isinstance(obj, dict) and "result" in obj:
                    result = str(obj["result"]).upper()
                    if result not in ("YES", "NO", "UNCERTAIN"):
                        result = "UNCERTAIN"
                    extras = {k: v for k, v in obj.items() if k != "result"}
                    return result, extras
            except json.JSONDecodeError:
                pass
            idx = raw.find('{', idx + 1)
    except Exception:
        pass

    # Pass 2: sanitize then parse
    sanitized = _sanitize_json_string(raw)
    try:
        decoder = json.JSONDecoder()
        idx = sanitized.find('{')
        while idx >= 0:
            try:
                obj, _ = decoder.raw_decode(sanitized, idx)
                if isinstance(obj, dict) and "result" in obj:
                    result = str(obj["result"]).upper()
                    if result not in ("YES", "NO", "UNCERTAIN"):
                        result = "UNCERTAIN"
                    extras = {k: v for k, v in obj.items() if k != "result"}
                    return result, extras
            except json.JSONDecodeError:
                pass
            idx = sanitized.find('{', idx + 1)
    except Exception:
        pass

    # Pass 3: regex extraction
    result_val = _extract_field_by_regex(raw, "result").upper().strip()
    if result_val in ("YES", "NO", "UNCERTAIN"):
        return result_val, {
            "evidence": _extract_field_by_regex(raw, "evidence"),
            "reason": _extract_field_by_regex(raw, "reason"),
        }

    # Fallback: plain yes/no
    lower = raw.strip().lower()
    if lower.startswith("yes"):
        return "YES", {}
    elif lower.startswith("no"):
        return "NO", {}
    return "UNCERTAIN", {"raw_fallback": raw[:200]}


def _llm_call_sync(
    base_url: str, api_key: str, model: str,
    prompt: str, max_tokens: int = 2000,
) -> str:
    """Synchronous LLM call using urllib.request (no extra dependencies)."""
    url = f"{base_url}/chat/completions"
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
    }).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read().decode("utf-8"))

    return (data["choices"][0]["message"]["content"] or "").strip()


# ─── Standalone main ─────────────────────────────────────────────────────────


def main():
    """Standalone entry point. Reads env vars and outputs JudgerResult JSON."""
    task_workspace = os.environ.get("TASK_WORKSPACE", "")
    rubric_path = os.environ.get("RUBRIC_PATH", "")
    deliverable_spec_path = os.environ.get("DELIVERABLE_SPEC_PATH", "")
    judger_name = os.environ.get("JUDGER_NAME", "simple_judge")
    llm_base_url = os.environ.get("JUDGE_MODEL_BASE_URL", "")
    llm_api_key = os.environ.get("JUDGE_MODEL_API_KEY", "EMPTY")
    llm_model = os.environ.get("JUDGE_MODEL_NAME", "")

    assert task_workspace, "TASK_WORKSPACE env var is required"
    assert rubric_path, "RUBRIC_PATH env var is required"
    assert llm_base_url, "JUDGE_MODEL_BASE_URL env var is required"
    assert llm_model, "JUDGE_MODEL_NAME env var is required"

    prompt_version = os.environ.get("JUDGE_PROMPT_VERSION", DEFAULT_PROMPT_VERSION)
    judge_prompt_tpl = get_judge_prompt(prompt_version)
    # 打印版本号 + prompt 前 3 行，便于确认选对了
    _preview = "\n".join(judge_prompt_tpl.splitlines()[:3])
    print(f"[simple_judge] prompt_version={prompt_version}\n{_preview}\n...", file=sys.stderr)

    # Load rubric
    with open(rubric_path) as f:
        rubric_data = json.load(f)

    raw_criteria = rubric_data.get("criteria", [])
    assert raw_criteria, f"No criteria found in rubric: {rubric_path}"

    # Filter simple_judge criteria
    sj_criteria = [c for c in raw_criteria if c.get("judge_method") == "simple_judge"]
    if not sj_criteria:
        result = {
            "judger_name": judger_name,
            "criteria_results": [],
            "summary": {"total": 0, "yes": 0, "no": 0, "uncertain": 0, "error": 0},
            "prompt_version": prompt_version,
        }
        print(json.dumps(result, ensure_ascii=False))
        return

    # Scan deliverables
    deliverables = _scan_deliverables(task_workspace)

    # Extract file metadata
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

    file_metadata_str = format_file_metadata(all_meta)

    # Summarize files
    summaries = []
    for fpath in deliverables:
        try:
            summaries.append(summarize_file(fpath))
        except Exception as e:
            summaries.append(f"Error parsing {os.path.basename(fpath)}: {e}")
    file_summary = "\n\n".join(summaries)[:30000]

    # Judge each criterion
    criteria_results = []
    stats = {"yes": 0, "no": 0, "uncertain": 0, "error": 0}

    for i, c in enumerate(sj_criteria):
        cid = c.get("criterion_id", f"c_{i}")
        description = _to_str(c.get("description", ""))
        expected = _to_str(c.get("expected", ""))

        prompt = judge_prompt_tpl.format(
            file_metadata=file_metadata_str,
            file_summary=file_summary,
            criterion=description,
            expected=expected,
        )

        try:
            answer_raw = _llm_call_sync(llm_base_url, llm_api_key, llm_model, prompt)
            assert answer_raw, "LLM returned empty content"
            result_str, extras = parse_judge_response(answer_raw)
        except Exception as e:
            result_str = "ERROR"
            extras = {"reason": f"LLM call failed: {e}"}

        # Score mapping
        score_map = {"YES": 1.0, "NO": 0.0, "UNCERTAIN": 0.5, "ERROR": 0.0}
        score = score_map.get(result_str, 0.0)

        stats_key = result_str.lower()
        if stats_key in stats:
            stats[stats_key] += 1

        criteria_results.append({
            "criterion_id": cid,
            "score": score,
            "result": result_str,
            "satisfied": result_str == "YES",
            "evidence": extras.get("evidence", ""),
            "reason": extras.get("reason", ""),
        })

    result = {
        "judger_name": judger_name,
        "criteria_results": criteria_results,
        "summary": {
            "total": len(sj_criteria),
            **stats,
        },
        "prompt_version": prompt_version,
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
