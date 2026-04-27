"""gdpval-synth bench adapter for the rl_task framework."""

from .dataset import GdpvalSynth
from .pipeline import gdpval_synth_pipeline

__all__ = ["GdpvalSynth", "gdpval_synth_pipeline"]
