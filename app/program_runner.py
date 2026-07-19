"""Online execution engine for a structured training program.

The runner consumes workout elapsed time and cumulative distance samples. A
single delayed sample can cross several step boundaries; every boundary is
preserved in ``state["transitions"]`` and delivered to ``on_event`` in order.
Each transition includes the exact interpolated elapsed/distance coordinates so
the recording layer can persist a precise performed-step timeline.
"""

from collections.abc import Callable
from math import isfinite
from typing import Any

from .coerce import to_float
from .program import DISTANCE, OPEN, TIME, Program, Step


class ProgramRunner:
    """Advance a flattened program from live elapsed-time/distance samples."""

    def __init__(
        self,
        program: Program,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.program = program
        self.steps: list[Step] = list(program.steps)
        self.on_event = on_event
        self.reset()

    def reset(self) -> None:
        """Rewind to the first step and clear all elapsed/distance progress."""
        self.index = 0
        self.done = len(self.steps) == 0
        self._started = False
        self._step_start_elapsed = 0.0
        self._step_start_distance = 0.0
        self._elapsed = 0.0
        self._distance = 0.0
        self._previous_elapsed = 0.0
        self._last_raw_distance: float | None = None

    @property
    def current(self) -> Step | None:
        """Return the active step, or None when the program is finished."""
        if self.done or self.index >= len(self.steps):
            return None
        return self.steps[self.index]

    def _next_step(self) -> Step | None:
        nxt = self.index + 1
        return self.steps[nxt] if nxt < len(self.steps) else None

    def _remaining(self) -> dict[str, Any]:
        step = self.current
        if step is None:
            return {"remaining": None, "remaining_kind": None}
        if step.duration_kind == TIME:
            used = self._elapsed - self._step_start_elapsed
            return {
                "remaining": max((step.duration_value or 0.0) - used, 0.0),
                "remaining_kind": TIME,
            }
        if step.duration_kind == DISTANCE:
            used = self._distance - self._step_start_distance
            return {
                "remaining": max((step.duration_value or 0.0) - used, 0.0),
                "remaining_kind": DISTANCE,
            }
        return {"remaining": None, "remaining_kind": OPEN}

    def _fraction(self) -> float | None:
        """Return progress through the current step in the range 0..1."""
        step = self.current
        if step is None or step.is_open() or not step.duration_value:
            return None
        if step.duration_kind == TIME:
            used = self._elapsed - self._step_start_elapsed
        else:
            used = self._distance - self._step_start_distance
        return max(0.0, min(used / step.duration_value, 1.0))

    def _state(
        self,
        transition: str | None,
        *,
        boundary_elapsed: float | None = None,
        boundary_distance: float | None = None,
        initial: bool = False,
    ) -> dict[str, Any]:
        return {
            "transition": transition,
            "step_index": self.index,
            "total": len(self.steps),
            "step": self.current,
            "next": self._next_step(),
            "done": self.done,
            "fraction": self._fraction(),
            "boundary_elapsed": boundary_elapsed,
            "boundary_distance": boundary_distance,
            "initial": initial,
            **self._remaining(),
        }

    def _result(self, transitions: list[dict[str, Any]]) -> dict[str, Any]:
        """Return current state plus transitions crossed by one action."""
        state = dict(transitions[-1]) if transitions else self._state(None)
        state["transitions"] = transitions
        return state

    def _emit_transition(
        self,
        transition: str,
        boundary_elapsed: float,
        boundary_distance: float,
        *,
        initial: bool = False,
    ) -> dict[str, Any]:
        """Build and notify one exact transition state."""
        state = self._state(
            transition,
            boundary_elapsed=boundary_elapsed,
            boundary_distance=boundary_distance,
            initial=initial,
        )
        if self.on_event is not None:
            self.on_event(state)
        return state

    def _begin_step(self, elapsed: float, distance: float) -> None:
        self._step_start_elapsed = elapsed
        self._step_start_distance = distance

    @staticmethod
    def _interpolate(
        target: float,
        start_x: float,
        end_x: float,
        start_y: float,
        end_y: float,
    ) -> float:
        """Linearly interpolate y where a sample crosses ``target`` on x."""
        if end_x <= start_x:
            return end_y
        fraction = max(0.0, min((target - start_x) / (end_x - start_x), 1.0))
        return start_y + fraction * (end_y - start_y)

    def _completion_boundary(
        self,
        sample_start_elapsed: float,
        sample_start_distance: float,
    ) -> tuple[float, float] | None:
        """Return exact elapsed/distance coordinates where the step ends."""
        step = self.current
        if step is None or step.is_open():
            return None
        if step.duration_kind == TIME:
            boundary_elapsed = self._step_start_elapsed + (step.duration_value or 0.0)
            if self._elapsed < boundary_elapsed:
                return None
            boundary_distance = self._interpolate(
                boundary_elapsed,
                sample_start_elapsed,
                self._elapsed,
                sample_start_distance,
                self._distance,
            )
            return boundary_elapsed, boundary_distance
        if step.duration_kind == DISTANCE:
            boundary_distance = self._step_start_distance + (step.duration_value or 0.0)
            if self._distance < boundary_distance:
                return None
            boundary_elapsed = self._interpolate(
                boundary_distance,
                sample_start_distance,
                self._distance,
                sample_start_elapsed,
                self._elapsed,
            )
            return boundary_elapsed, boundary_distance
        return None

    def update(
        self,
        elapsed_s: Any = None,
        distance_m: Any = None,
        sample_duration_s: Any = None,
    ) -> dict[str, Any]:
        """Feed one sample and return current state plus crossed transitions."""
        if self.done:
            return self._result([])

        current_elapsed = to_float(elapsed_s)
        represented_s = to_float(sample_duration_s)
        if (
            current_elapsed is None
            or not isfinite(current_elapsed)
            or current_elapsed < self._previous_elapsed
            or represented_s is None
            or not isfinite(represented_s)
            or represented_s < 0
            or represented_s > current_elapsed - self._previous_elapsed
        ):
            raise ValueError("ProgramRunner requires canonical non-overlapping timing")

        sample_start_elapsed = current_elapsed - represented_s
        sample_start_distance = self._distance
        self._elapsed = current_elapsed
        raw_distance = to_float(distance_m)
        if raw_distance is not None and isfinite(raw_distance) and raw_distance >= 0:
            if self._last_raw_distance is None:
                self._distance += raw_distance
            else:
                self._distance += max(raw_distance - self._last_raw_distance, 0.0)
            self._last_raw_distance = raw_distance
        transitions: list[dict[str, Any]] = []

        if not self._started:
            self._started = True
            self._begin_step(0.0, 0.0)
            transitions.append(
                self._emit_transition(
                    "step_start",
                    0.0,
                    0.0,
                    initial=True,
                )
            )
        # One delayed sample may cross several short steps. Carry both time and
        # distance overshoot across every exact boundary in this sample,
        # including the first represented sensor window.
        for _ in range(len(self.steps)):
            boundary = self._completion_boundary(
                sample_start_elapsed, sample_start_distance
            )
            if boundary is None:
                break
            boundary_elapsed, boundary_distance = boundary
            transition = self._finish_current(
                boundary_elapsed, boundary_distance
            )
            transitions.append(
                self._emit_transition(
                    transition, boundary_elapsed, boundary_distance
                )
            )
            if self.done:
                break

        self._previous_elapsed = self._elapsed
        return self._result(transitions)

    def advance(self) -> dict[str, Any]:
        """Manually finish the current step (for OPEN steps or skipping)."""
        if self.done:
            return self._result([])
        transition = self._finish_current(self._elapsed, self._distance)
        event = self._emit_transition(transition, self._elapsed, self._distance)
        return self._result([event])

    def _finish_current(self, boundary_elapsed: float, boundary_distance: float) -> str:
        """Move to the next step or finish the program."""
        if self.index + 1 < len(self.steps):
            self.index += 1
            self._begin_step(boundary_elapsed, boundary_distance)
            return "step_start"
        self.done = True
        return "program_complete"
