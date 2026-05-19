"""Layer 0's lessons — empty at genesis (docs/engineering/18).

Generation 0 has no inherited lessons. The first reproduction event's
lesson-distillation pass is what fills Layer 1. This module exists so the
shape is real from day one: the stack loader always finds a Layer 0 lesson
list, it just happens to be ``[]``.
"""

from __future__ import annotations

from ._stack import Lesson

LAYER_0_LESSONS: list[Lesson] = []
