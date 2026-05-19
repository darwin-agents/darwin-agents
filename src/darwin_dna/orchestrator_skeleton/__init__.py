"""The founding orchestrator. A Darwin BOOTS from this; its soma copy mutates.

The node functions are framework-agnostic on purpose (good engineering and the
boot guarantee both want it). ``prompts/`` is version-controlled text — the
planner system prompt is the most-iterated artefact in the project.
"""

from pathlib import Path

from .graph import OrchestratorContext, build_graph
from .state import OrchestratorState

_PROMPT_DIR = Path(__file__).parent / "prompts"


def load_prompt(name: str) -> str:
    """Read a version-controlled system prompt by stem (e.g. 'planner_system')."""
    return (_PROMPT_DIR / f"{name}.txt").read_text(encoding="utf-8")


__all__ = [
    "OrchestratorContext",
    "build_graph",
    "OrchestratorState",
    "load_prompt",
]
