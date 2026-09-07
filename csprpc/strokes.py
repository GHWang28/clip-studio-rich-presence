"""Count drawing strokes from pointer-down edges.

CLIP STUDIO PAINT does not publish a stroke count. Each time the pen or left
mouse button goes down while CLIP STUDIO PAINT is in front is treated as one
stroke. Palette clicks count too; this is an estimate, not CSP's own tally.

The counter never installs an input hook. The daemon polls button state the
same way it already polls idle time.
"""

from __future__ import annotations

from typing import Optional, Tuple


class StrokeCounter:
    """In-memory stroke totals for the current launch."""

    def __init__(self) -> None:
        self.session_count = 0
        self.file_count = 0
        self._document: Optional[str] = None
        self._prev_down = False

    def set_document(self, key: Optional[str]) -> None:
        """Reset the per-canvas count when the open file changes."""
        if key != self._document:
            self._document = key
            self.file_count = 0

    def sample(self, down: bool, counting: bool) -> None:
        """Record a stroke on the rising edge while ``counting`` is true."""
        rising = down and not self._prev_down
        self._prev_down = down
        if counting and rising:
            self.session_count += 1
            self.file_count += 1

    def totals(self) -> Tuple[int, int]:
        return self.file_count, self.session_count
