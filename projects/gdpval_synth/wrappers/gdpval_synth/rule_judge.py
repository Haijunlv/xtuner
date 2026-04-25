"""Rule-based judger for gdpval_synth Agent Rollout.

Reads rubric.json and deliverable_spec.json, filters criteria with
judge_method == "rule", and checks whether the agent produced correct
output files.  Outputs a single JSON line to stdout.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Logging helper (all logs to stderr so stdout stays clean for JSON output)
# ---------------------------------------------------------------------------

def _log(msg: str) -> None:
    print(f"[rule_judge] {msg}", file=sys.stderr)


# ---------------------------------------------------------------------------
# File-checking helpers
# ---------------------------------------------------------------------------

def _resolve_file(workspace: Path, spec_filename: str) -> Path | None:
    """Try full relative path first, then basename-only fallback."""
    candidate = workspace / spec_filename
    if candidate.exists():
        return candidate
    candidate = workspace / Path(spec_filename).name
    if candidate.exists():
        return candidate
    return None


def _check_excel_sheets(filepath: Path, expected_sheets: list[str]) -> tuple[int, int]:
    """Return (found, total) for expected sheet names in an xlsx file."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
        actual = {s.lower().strip() for s in wb.sheetnames}
        wb.close()
    except Exception as exc:
        _log(f"openpyxl error reading {filepath}: {exc}")
        return 0, len(expected_sheets)

    found = sum(1 for s in expected_sheets if s.lower().strip() in actual)
    return found, len(expected_sheets)


def _check_docx_sections(filepath: Path, expected_sections: list[str]) -> tuple[int, int]:
    """Return (found, total) for expected headings/sections in a docx file."""
    try:
        import docx
        doc = docx.Document(str(filepath))
        headings = set()
        for para in doc.paragraphs:
            if para.style and para.style.name and para.style.name.startswith("Heading"):
                headings.add(para.text.lower().strip())
            # Also check bold-only paragraphs as pseudo-headings
            elif para.runs and all(r.bold for r in para.runs if r.text.strip()):
                headings.add(para.text.lower().strip())
    except Exception as exc:
        _log(f"python-docx error reading {filepath}: {exc}")
        return 0, len(expected_sections)

    found = sum(1 for s in expected_sections if s.lower().strip() in headings)
    return found, len(expected_sections)


# ---------------------------------------------------------------------------
# Parse the "expected" field for completeness criteria
# ---------------------------------------------------------------------------

def _parse_expected(expected: str) -> tuple[list[str], list[str]]:
    """Parse expected field like 'Excel sheets: A, B, C; DOCX sections: X, Y, Z'.

    Returns (excel_sheets, docx_sections).
    """
    excel_sheets: list[str] = []
    docx_sections: list[str] = []

    # Split on semicolons
    parts = [p.strip() for p in expected.split(";")]
    for part in parts:
        lower = part.lower()
        if lower.startswith("excel sheets:") or lower.startswith("excel sheet:"):
            items_str = part.split(":", 1)[1].strip()
            excel_sheets = [s.strip() for s in items_str.split(",") if s.strip()]
        elif lower.startswith("docx sections:") or lower.startswith("docx section:"):
            items_str = part.split(":", 1)[1].strip()
            docx_sections = [s.strip() for s in items_str.split(",") if s.strip()]

    return excel_sheets, docx_sections


# ---------------------------------------------------------------------------
# Build a lookup from deliverable_spec
# ---------------------------------------------------------------------------

def _build_spec_lookup(spec: list[dict]) -> dict[str, dict]:
    """Map filename -> spec entry, also keyed by basename."""
    lookup: dict[str, dict] = {}
    for entry in spec:
        fn = entry.get("filename", "")
        lookup[fn] = entry
        lookup[Path(fn).name] = entry
    return lookup


# ---------------------------------------------------------------------------
# Per-criterion judge functions
# ---------------------------------------------------------------------------

def _judge_completeness(
    criterion: dict,
    workspace: Path,
    spec: list[dict],
    spec_lookup: dict[str, dict],
) -> float:
    """Check file existence, Excel sheets, DOCX sections."""
    expected_str = criterion.get("expected", "")
    excel_sheets, docx_sections = _parse_expected(expected_str)

    total_checks = 0
    passed_checks = 0

    # Check file existence for all deliverables
    for entry in spec:
        fn = entry.get("filename", "")
        total_checks += 1
        resolved = _resolve_file(workspace, fn)
        if resolved is not None:
            passed_checks += 1
        else:
            _log(f"  missing file: {fn}")

    # Check excel sheets
    if excel_sheets:
        # Find xlsx files in spec
        xlsx_files = [e for e in spec if e.get("type") == "xlsx"]
        for xlsx_entry in xlsx_files:
            resolved = _resolve_file(workspace, xlsx_entry.get("filename", ""))
            if resolved is not None:
                found, total = _check_excel_sheets(resolved, excel_sheets)
                total_checks += total
                passed_checks += found
                if found < total:
                    _log(f"  xlsx {resolved.name}: {found}/{total} expected sheets found")
            else:
                total_checks += len(excel_sheets)

    # Check docx sections
    if docx_sections:
        docx_files = [e for e in spec if e.get("type") == "docx"]
        for docx_entry in docx_files:
            resolved = _resolve_file(workspace, docx_entry.get("filename", ""))
            if resolved is not None:
                found, total = _check_docx_sections(resolved, docx_sections)
                total_checks += total
                passed_checks += found
                if found < total:
                    _log(f"  docx {resolved.name}: {found}/{total} expected sections found")
            else:
                total_checks += len(docx_sections)

    if total_checks == 0:
        return 1.0
    return passed_checks / total_checks


def _judge_format_compliance(
    criterion: dict,
    workspace: Path,
    spec: list[dict],
    spec_lookup: dict[str, dict],
) -> float:
    """Check file existence and correct extension."""
    total_checks = 0
    passed_checks = 0

    for entry in spec:
        fn = entry.get("filename", "")
        expected_type = entry.get("type", "")

        # Existence check
        total_checks += 1
        resolved = _resolve_file(workspace, fn)
        if resolved is not None:
            passed_checks += 1
        else:
            _log(f"  missing file: {fn}")
            # Extension check still counts but fails
            if expected_type:
                total_checks += 1
            continue

        # Extension check
        if expected_type:
            total_checks += 1
            actual_ext = resolved.suffix.lstrip(".").lower()
            if actual_ext == expected_type.lower():
                passed_checks += 1
            else:
                _log(f"  wrong extension: {resolved.name} expected .{expected_type}")

    if total_checks == 0:
        return 1.0
    return passed_checks / total_checks


def _judge_accuracy_rule(
    criterion: dict,
    workspace: Path,
    spec: list[dict],
    spec_lookup: dict[str, dict],
) -> float:
    """Default accuracy rule: file existence check."""
    if not spec:
        return 1.0
    found = 0
    for entry in spec:
        fn = entry.get("filename", "")
        if _resolve_file(workspace, fn) is not None:
            found += 1
    return found / len(spec)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    workspace_str = os.environ.get("TASK_WORKSPACE", "")
    rubric_path_str = os.environ.get("RUBRIC_PATH", "")
    spec_path_str = os.environ.get("DELIVERABLE_SPEC_PATH", "")
    judger_name = os.environ.get("JUDGER_NAME", "rule_judger")

    if not workspace_str:
        print(json.dumps({"judger_name": judger_name, "total": 0.0, "error": "TASK_WORKSPACE not set"}))
        return

    workspace = Path(workspace_str)

    # Read rubric
    rubric_path = Path(rubric_path_str) if rubric_path_str else workspace / "rubric.json"
    try:
        rubric = json.loads(rubric_path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(json.dumps({"judger_name": judger_name, "total": 0.0, "error": f"Cannot read rubric: {exc}"}))
        return

    # Read deliverable spec
    spec_path = Path(spec_path_str) if spec_path_str else workspace / "deliverable_spec.json"
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
    except Exception as exc:
        _log(f"Cannot read deliverable_spec: {exc}, using empty spec")
        spec = []

    spec_lookup = _build_spec_lookup(spec)

    # Filter rule criteria
    all_criteria = rubric.get("criteria", [])
    rule_criteria = [c for c in all_criteria if c.get("judge_method") == "rule"]

    if not rule_criteria:
        print(json.dumps({
            "judger_name": judger_name,
            "total": 1.0,
            "criteria": {},
            "metadata": {"skipped": True},
        }))
        return

    _log(f"Found {len(rule_criteria)} rule criteria out of {len(all_criteria)} total")

    # Judge each criterion
    criteria_results: dict[str, dict] = {}
    judge_funcs = {
        "completeness": _judge_completeness,
        "format_compliance": _judge_format_compliance,
    }

    for criterion in rule_criteria:
        cid = criterion.get("criterion_id", "unknown")
        ctype = criterion.get("criterion_type", "")
        weight = float(criterion.get("weight", 1.0))

        judge_fn = judge_funcs.get(ctype, _judge_accuracy_rule)
        score = judge_fn(criterion, workspace, spec, spec_lookup)

        criteria_results[cid] = {"score": round(score, 4), "weight": weight}
        _log(f"  {cid} ({ctype}): score={score:.4f}, weight={weight}")

    # Compute weighted total
    total_weight = sum(r["weight"] for r in criteria_results.values())
    if total_weight > 0:
        total_score = sum(r["score"] * r["weight"] for r in criteria_results.values()) / total_weight
    else:
        total_score = 0.0

    result = {
        "judger_name": judger_name,
        "total": round(total_score, 4),
        "criteria": criteria_results,
        "metadata": {"rule_criteria_count": len(rule_criteria)},
    }

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
