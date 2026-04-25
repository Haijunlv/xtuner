"""Materialize a JSONL file into per-task directories for gdpval_synth.

Usage:
    python materialize.py --input FILE --output DIR [--limit N]

Each JSONL line becomes a directory named ``<task_id>/`` under ``--output``.
Idempotent: skips tasks whose directory already contains ``metadata.json``.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

METADATA_FIELDS = (
    "task_id",
    "sector",
    "occupation",
    "task_type",
    "source_phase",
    "seed_id",
)


def _decode_files(encoded_files: dict[str, str], dest_dir: Path) -> None:
    """Decode base64-encoded files into *dest_dir*, using only the filename."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for key, b64_data in encoded_files.items():
        filename = Path(key).name
        out_path = dest_dir / filename
        out_path.write_bytes(base64.b64decode(b64_data))


def materialize_task(record: dict, output_dir: Path) -> bool:
    """Write one task directory.  Returns True if newly created, False if skipped."""
    task_id = record["task_id"]
    task_dir = output_dir / task_id

    # Idempotent: skip if already materialized
    if (task_dir / "metadata.json").exists():
        return False

    task_dir.mkdir(parents=True, exist_ok=True)

    # instruction.md — first user message
    messages = record.get("messages", [])
    instruction = messages[0]["content"] if messages else ""
    (task_dir / "instruction.md").write_text(instruction, encoding="utf-8")

    # metadata.json
    metadata = {k: record.get(k) for k in METADATA_FIELDS}
    (task_dir / "metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # environment/data/ — reference_files base64 decoded
    ref_files = record.get("reference_files") or {}
    if ref_files:
        _decode_files(ref_files, task_dir / "environment" / "data")

    # rubric.json
    rubric = record.get("rubric_json")
    if rubric is not None:
        (task_dir / "rubric.json").write_text(
            json.dumps(rubric, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # deliverable_spec.json
    spec = record.get("deliverable_files_spec")
    if spec is not None:
        (task_dir / "deliverable_spec.json").write_text(
            json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # deliverable_files/ — base64 decoded
    del_files = record.get("deliverable_files") or {}
    if del_files:
        _decode_files(del_files, task_dir / "deliverable_files")

    return True


def main() -> None:
    parser = argparse.ArgumentParser(description="Materialize gdpval_synth JSONL to task dirs")
    parser.add_argument("--input", required=True, help="Input JSONL file")
    parser.add_argument("--output", required=True, help="Output directory for task dirs")
    parser.add_argument("--limit", type=int, default=0, help="Max tasks to process (0 = all)")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    input_path = Path(args.input)
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    created = 0
    skipped = 0
    total = 0

    with input_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            total += 1

            if materialize_task(record, output_dir):
                created += 1
            else:
                skipped += 1

            if args.limit and total >= args.limit:
                break

    logger.info(
        "Done: %d read, %d materialized, %d skipped (already exist)",
        total,
        created,
        skipped,
    )


if __name__ == "__main__":
    main()
