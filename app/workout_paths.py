"""The fixed on-disk layout for one KayakFit workout.

Every workout owns a timestamped directory below ``workouts/<year>``. Keeping
all of its artifacts together makes it safe to move, inspect and discard a
single workout without relying on filename prefixes scattered across modules.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

WORKOUT_DIRECTORY_PREFIX = "workout_"
CSV_FILENAME = "workout.csv"
METADATA_FILENAME = "metadata.json"
STEPS_FILENAME = "steps.json"
FIT_FILENAME = "activity.fit"


@dataclass(frozen=True)
class WorkoutPaths:
    """Known artifact paths inside one workout directory."""

    directory: Path

    @property
    def csv(self) -> Path:
        return self.directory / CSV_FILENAME

    @property
    def metadata(self) -> Path:
        return self.directory / METADATA_FILENAME

    @property
    def steps(self) -> Path:
        return self.directory / STEPS_FILENAME

    @property
    def fit(self) -> Path:
        return self.directory / FIT_FILENAME

    @property
    def fit_temp(self) -> Path:
        return self.directory / f"{FIT_FILENAME}.tmp"

    @classmethod
    def from_csv(cls, csv_path: str | Path) -> WorkoutPaths:
        """Resolve the fixed artifact paths beside a workout CSV."""
        return cls(Path(csv_path).parent)

    @classmethod
    def reserve(cls, workouts_root: Path, started_at: datetime) -> WorkoutPaths:
        """Atomically reserve a unique directory for a new workout.

        ``mkdir`` is the reservation operation: if another process has already
        claimed the timestamp, try a deterministic numeric suffix instead.
        """
        year_dir = workouts_root / started_at.strftime("%Y")
        year_dir.mkdir(parents=True, exist_ok=True)
        timestamp = started_at.strftime("%Y%m%d_%H%M%S")
        for suffix in range(1000):
            name = (
                f"{WORKOUT_DIRECTORY_PREFIX}{timestamp}"
                if suffix == 0
                else f"{WORKOUT_DIRECTORY_PREFIX}{timestamp}_{suffix:03d}"
            )
            candidate = cls(year_dir / name)
            try:
                candidate.directory.mkdir()
                return candidate
            except FileExistsError:
                continue
        raise FileExistsError(
            f"Could not reserve a unique workout directory for {timestamp}"
        )

    def is_owned_by(self, workouts_root: str | Path) -> bool:
        """Whether this is a direct, owned workout directory beneath the root."""
        try:
            root = Path(workouts_root).resolve()
            directory = self.directory.resolve()
            relative = directory.relative_to(root)
        except (OSError, ValueError):
            return False
        return (
            len(relative.parts) == 2
            and len(relative.parts[0]) == 4
            and relative.parts[0].isdigit()
            and directory.name.startswith(WORKOUT_DIRECTORY_PREFIX)
        )
