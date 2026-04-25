"""gdpval-synth dataset: iterate materialized task dirs, load TaskData.

The dataset is deliberately thin:
  - ``iter_tasks()`` yields ``(task_dir, TaskData)`` for every ``metadata.json``
    under ``tasks_root``.
  - ``pipeline`` is whatever the caller's config built — :class:`GdpvalSynth`
    never constructs one on its own.

Example config::

    from gdpval_synth.dataset import GdpvalSynth

    dataset = GdpvalSynth(
        tasks_root="/data/bench/gdpval-synth/tasks",
        pipeline=my_pipeline(),
    )
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterator

from xtuner.v1.ray.environment.rl_task.runner import Runner
from xtuner.v1.ray.environment.rl_task.schemas import TaskData

logger = logging.getLogger(__name__)


class GdpvalSynth:
    """gdpval-synth dataset iterator.  One pipeline for every task it yields."""

    name = "gdpval-synth"

    def __init__(self, tasks_root: str | Path, *, pipeline: Runner):
        self.tasks_root = Path(tasks_root).resolve()
        self.pipeline = pipeline

    def iter_tasks(self) -> Iterator[tuple[Path, TaskData]]:
        """Yield ``(task_dir, TaskData)`` for every metadata.json under ``tasks_root``."""
        for task_dir in sorted(self.tasks_root.iterdir()):
            if not task_dir.is_dir():
                continue
            meta_path = task_dir / "metadata.json"
            if not meta_path.exists():
                continue
            try:
                yield task_dir, self.load_task(task_dir)
            except Exception as exc:
                logger.warning("skipping %s: %s", task_dir, exc)

    def load_task(self, task_dir: Path) -> TaskData:
        meta = json.loads((task_dir / "metadata.json").read_text(encoding="utf-8"))
        return TaskData(
            id=meta.get("task_id") or task_dir.name,
            data_source=self.name,
            ability=meta.get("task_type"),
            tags=[meta.get("sector", ""), meta.get("occupation", "")],
            instruction="instruction.md",
        )
