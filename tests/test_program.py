"""
Tests for the training-program model and the online ProgramRunner.

Both modules are stdlib-only (``app/__init__.py`` deliberately imports
nothing), so plain imports work headless — no stubbing needed.
"""

import contextlib
import logging
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import program, program_runner  # noqa: E402

ProgramRunner = program_runner.ProgramRunner


def test_flatten_repeat_blocks() -> None:
    spec = {
        "name": "test",
        "steps": [
            {"type": "warmup", "duration": {"kind": "time", "seconds": 300}},
            {"repeat": 4, "steps": [
                {"type": "work", "duration": {"kind": "distance", "meters": 500}},
                {"type": "rest", "duration": {"kind": "time", "seconds": 60}},
            ]},
            {"type": "cooldown", "duration": {"kind": "time", "seconds": 300}},
        ],
    }
    prog = program.load_program(spec)
    # warmup + 4*(work+rest) + cooldown = 10
    assert len(prog) == 10, f"expected 10 steps, got {len(prog)}"
    assert prog.steps[1].duration_kind == "distance"
    assert prog.steps[1].duration_value == 500
    print(f"  ok: repeat blocks flattened to {len(prog)} steps")


def test_builtins_load() -> None:
    progs = program.builtin_programs()
    assert len(progs) >= 3
    assert all(len(p) > 0 for p in progs)
    print(f"  ok: {len(progs)} built-in programs load")


def test_install_examples_once() -> None:
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        dest = Path(tmp) / "programs"
        n = program.install_examples(dest)
        assert n >= 3, f"expected examples copied, got {n}"
        assert (dest / program._EXAMPLES_MARKER).exists()
        # Second run is a no-op, even after the user deletes an example.
        next(dest.glob("*.json")).unlink()
        assert program.install_examples(dest) == 0, "must not re-install"
        # Existing files are never overwritten when forced by a fresh marker.
        (dest / program._EXAMPLES_MARKER).unlink()
        kept = next(dest.glob("*.json"))
        kept.write_text('{"steps": []}', encoding="utf-8")
        program.install_examples(dest)
        assert kept.read_text(encoding="utf-8") == '{"steps": []}'
    print(f"  ok: {n} examples installed once, no overwrite / re-install")


def test_time_and_distance_progression() -> None:
    spec = {"name": "t", "steps": [
        {"type": "work", "duration": {"kind": "time", "seconds": 5}},
        {"type": "work", "duration": {"kind": "distance", "meters": 20}},
    ]}
    events = []
    r = ProgramRunner(program.load_program(spec), on_event=lambda e: events.append(e["transition"]))
    # t=0..4s, distance flat -> still on step 1
    for t in range(0, 5):
        r.update(elapsed_s=t, distance_m=0, sample_duration_s=0 if t == 0 else 1)
    assert r.index == 0 and not r.done
    # t=5s -> step 1 (time) completes, step 2 (distance) starts
    r.update(elapsed_s=5, distance_m=0, sample_duration_s=1)
    assert r.index == 1, f"expected step 2 active, got index {r.index}"
    # advance distance to 20 -> program completes
    r.update(elapsed_s=6, distance_m=20, sample_duration_s=1)
    assert r.done, "program should be complete after distance step"
    assert events[-1] == "program_complete"
    assert events.count("step_start") == 2
    print(f"  ok: time->distance progression, transitions {events}")


def test_open_step_needs_advance() -> None:
    spec = {"name": "o", "steps": [
        {"type": "work", "duration": {"kind": "open"}},
        {"type": "cooldown", "duration": {"kind": "time", "seconds": 5}},
    ]}
    r = ProgramRunner(program.load_program(spec))
    for t in range(0, 100):  # lots of time passes; open step never auto-completes
        r.update(elapsed_s=t, distance_m=t, sample_duration_s=0 if t == 0 else 1)
    assert r.index == 0 and not r.done, "open step must not auto-complete"
    r.advance()  # manual lap/skip
    assert r.index == 1, "advance should move to next step"
    print("  ok: open step waits for manual advance")


def test_large_sample_jump_preserves_all_exact_step_boundaries() -> None:
    spec = {
        "name": "jump",
        "steps": [
            {"type": "work", "duration": {"kind": "time", "seconds": 5}},
            {"type": "rest", "duration": {"kind": "time", "seconds": 5}},
            {"type": "work", "duration": {"kind": "time", "seconds": 5}},
        ],
    }
    runner = ProgramRunner(program.load_program(spec))
    runner.update(elapsed_s=0, distance_m=0, sample_duration_s=0)
    state = runner.update(elapsed_s=20, distance_m=0, sample_duration_s=0)
    assert runner.done
    assert state["transition"] == "program_complete"
    transitions = state["transitions"]
    assert [item["transition"] for item in transitions] == [
        "step_start",
        "step_start",
        "program_complete",
    ]
    assert [item["boundary_elapsed"] for item in transitions] == [5.0, 10.0, 15.0]
    assert transitions[0]["step"].kind == "rest"
    assert transitions[1]["step"].kind == "work"
    print("  ok: a large sample jump preserves every exact intermediate boundary")


def test_first_represented_window_counts_toward_program() -> None:
    spec = {
        "name": "first window",
        "steps": [
            {"type": "work", "duration": {"kind": "time", "seconds": 1}},
        ],
    }
    runner = ProgramRunner(program.load_program(spec))
    state = runner.update(elapsed_s=1, distance_m=3, sample_duration_s=1)

    assert runner.done
    assert [item["transition"] for item in state["transitions"]] == [
        "step_start",
        "program_complete",
    ]
    assert state["transitions"][0]["boundary_elapsed"] == 0.0
    assert state["transitions"][1]["boundary_elapsed"] == 1.0


def test_mixed_step_overshoot_interpolates_time_and_distance() -> None:
    spec = {
        "name": "mixed jump",
        "steps": [
            {"type": "work", "duration": {"kind": "time", "seconds": 5}},
            {"type": "work", "duration": {"kind": "distance", "meters": 10}},
            {"type": "work", "duration": {"kind": "time", "seconds": 2}},
        ],
    }
    runner = ProgramRunner(program.load_program(spec))
    runner.update(elapsed_s=0, distance_m=0, sample_duration_s=0)
    state = runner.update(elapsed_s=10, distance_m=30, sample_duration_s=10)
    transitions = state["transitions"]
    assert len(transitions) == 2
    assert transitions[0]["boundary_elapsed"] == 5.0
    assert transitions[0]["boundary_distance"] == 15.0
    assert transitions[1]["boundary_distance"] == 25.0
    assert abs(transitions[1]["boundary_elapsed"] - (25.0 / 3.0)) < 1e-9
    assert runner.index == 2 and not runner.done
    assert abs(state["remaining"] - (1.0 / 3.0)) < 1e-9


def test_remaining_reported() -> None:
    spec = {"name": "r", "steps": [
        {"type": "work", "duration": {"kind": "time", "seconds": 60}},
    ]}
    r = ProgramRunner(program.load_program(spec))
    r.update(elapsed_s=0, distance_m=0, sample_duration_s=0)
    st = r.update(elapsed_s=20, distance_m=0, sample_duration_s=0)
    assert st["remaining_kind"] == "time"
    assert abs(st["remaining"] - 40) < 0.01, st["remaining"]
    print(f"  ok: remaining time reported ({st['remaining']:.0f}s left)")


@contextlib.contextmanager
def _capture_program_logs(level=logging.INFO):
    """Capture records emitted by app.program's module logger."""
    records = []

    class _ListHandler(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _ListHandler()
    handler.setLevel(level)
    program.logger.addHandler(handler)
    old_level = program.logger.level
    program.logger.setLevel(level)
    try:
        yield records
    finally:
        program.logger.removeHandler(handler)
        program.logger.setLevel(old_level)


def _write(directory: Path, name: str, text: str, encoding: str = "utf-8") -> Path:
    path = directory / name
    path.write_text(text, encoding=encoding)
    return path


def test_malformed_file_does_not_wipe_others() -> None:
    # #1: a single malformed file must never take down the whole list.
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write(d, "good.json",
               '{"name": "good", "steps": [{"type": "work", '
               '"duration": {"kind": "time", "seconds": 60}}]}')
        # "steps" is an object, not an array -> old code raised AttributeError.
        _write(d, "steps_obj.json", '{"name": "a", "steps": {"a": 1}}')
        # A step is a bare string, not an object -> old code raised AttributeError.
        _write(d, "step_str.json", '{"name": "b", "steps": ["justastring"]}')
        # A step is a list.
        _write(d, "step_list.json", '{"name": "c", "steps": [[1, 2, 3]]}')
        with _capture_program_logs(logging.WARNING) as recs:
            progs = program._load_programs_from(d)
        names = [p.name for p in progs]
        assert names == ["good"], f"only the good file should load, got {names}"
        # Each of the three bad files logged a skip reason.
        assert len(recs) >= 3, f"expected >=3 skip warnings, got {len(recs)}"
    print("  ok: one malformed file no longer wipes the rest (3 skipped, 1 kept)")


def test_deep_repeat_is_capped_not_hung() -> None:
    # #2: build a nested repeat 40 deep; naive expansion is 2^40 -> hang/OOM.
    spec: dict[str, Any] = {"type": "work", "duration": {"kind": "time", "seconds": 30}}
    for _ in range(40):
        spec = {"repeat": 2, "steps": [spec, spec]}
    full = {"name": "bomb", "steps": [spec]}
    raised = False
    try:
        program.load_program(full)
    except program.ProgramError:
        raised = True
    assert raised, "deeply nested repeat should raise ProgramError, not hang/OOM"
    print("  ok: deeply nested repeat rejected quickly via ProgramError")


def test_step_cap_enforced() -> None:
    # #2: a flat program over MAX_STEPS is rejected during flattening.
    spec = {"name": "big", "steps": [
        {"repeat": program.MAX_STEPS + 10, "steps": [
            {"type": "work", "duration": {"kind": "time", "seconds": 1}},
        ]},
    ]}
    raised = False
    try:
        program.load_program(spec)
    except program.ProgramError:
        raised = True
    assert raised, f"program over {program.MAX_STEPS} steps should be rejected"
    print(f"  ok: {program.MAX_STEPS}-step cap enforced")


def test_unknown_and_noncanonical_step_types_are_rejected() -> None:
    for step_type in ("Restx", "REST"):
        spec = {"name": "bad type", "steps": [
            {"type": step_type, "duration": {"kind": "time", "seconds": 60}},
        ]}
        try:
            program.load_program(spec)
        except program.ProgramError:
            continue
        raise AssertionError(f"step type {step_type!r} should be rejected")


def test_bad_durations_rejected_cleanly() -> None:
    # #4: negative / zero / non-numeric / list durations reject as ProgramError,
    # not a raw traceback from label generation.
    for bad in (-30, 0, "abc", [30], None, True):
        spec = {"name": "d", "steps": [
            {"type": "work", "duration": {"kind": "time", "seconds": bad}},
        ]}
        raised = False
        try:
            program.load_program(spec)
        except program.ProgramError:
            raised = True
        except Exception as e:
            raise AssertionError(f"duration {bad!r} raised {type(e).__name__}, "
                                 "expected ProgramError") from e
        assert raised, f"duration {bad!r} should be rejected"
    print("  ok: negative/zero/non-numeric durations rejected cleanly")


def test_duration_kinds_and_repeat_counts_are_strict() -> None:
    bad_steps = [
        {"type": "work", "duration": {"kind": "unknown"}},
        {"type": "work"},
    ]
    for step in bad_steps:
        try:
            program.load_program({"name": "bad duration", "steps": [step]})
        except program.ProgramError:
            continue
        raise AssertionError(f"step {step!r} should be rejected")

    for repeat in (True, 1.5, 0, -1, "2"):
        spec = {
            "name": "bad repeat",
            "steps": [{"repeat": repeat, "steps": [
                {"type": "work", "duration": {"kind": "time", "seconds": 1}},
            ]}],
        }
        try:
            program.load_program(spec)
        except program.ProgramError:
            continue
        raise AssertionError(f"repeat {repeat!r} should be rejected")


def test_malformed_targets_are_rejected_at_load_time() -> None:
    bad_targets = [
        "zone 3",
        {"metric": "unknown", "low": 1, "high": 2},
        {"metric": "cadence", "low": 60, "high": 70},
        {"metric": "stroke_rate", "low": 60, "high": 70},
        {"metric": "power", "low": 300, "high": 200},
        {"metric": "hr_zone", "low": 0, "high": 6},
    ]
    for target in bad_targets:
        spec = {
            "name": "bad target",
            "steps": [
                {
                    "type": "work",
                    "duration": {"kind": "time", "seconds": 60},
                    "target": target,
                }
            ],
        }
        try:
            program.load_program(spec)
        except program.ProgramError:
            continue
        raise AssertionError(f"target {target!r} should be rejected")
    print("  ok: malformed targets rejected before workout execution")


def test_bad_duration_skips_only_that_file_with_log() -> None:
    # #4: a bad-duration file is skipped with a logged reason, others survive.
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write(d, "good.json",
               '{"name": "g", "steps": [{"type": "work", '
               '"duration": {"kind": "time", "seconds": 60}}]}')
        _write(d, "neg.json",
               '{"name": "n", "steps": [{"type": "work", '
               '"duration": {"kind": "time", "seconds": -30}}]}')
        with _capture_program_logs(logging.WARNING) as recs:
            warnings: list[str] = []
            progs = program._load_programs_from(d, warnings)
        assert [p.name for p in progs] == ["g"]
        assert any("neg.json" in r.getMessage() for r in recs)
        assert any("neg.json" in w for w in warnings)
    print("  ok: bad-duration file skipped with logged reason")


def test_bom_empty_and_invalid_json_logged() -> None:
    # #5: BOM, empty-steps, missing-steps, and invalid JSON are skipped and
    # logged. Only a schema-valid empty program is informational.
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write(d, "bom.json",
               '﻿{"name": "b", "steps": [{"type": "work", '
               '"duration": {"kind": "time", "seconds": 60}}]}')
        _write(d, "empty.json", '{"name": "e", "steps": []}')
        _write(d, "nosteps.json", '{"name": "no"}')
        _write(d, "invalid.json", '{"name": "i", "steps": [}')
        with _capture_program_logs(logging.INFO) as recs:
            progs = program._load_programs_from(d)
        assert progs == [], f"none of these should load, got {[p.name for p in progs]}"
        msgs = [r.getMessage() for r in recs]
        assert any("bom.json" in m for m in msgs), "BOM skip must be logged"
        assert any("empty.json" in m for m in msgs), "empty-steps skip must be logged"
        assert any("nosteps.json" in m for m in msgs), "missing-steps skip must be logged"
        assert any("invalid.json" in m for m in msgs), "invalid JSON skip must be logged"
        # Empty is valid but non-runnable; missing steps violates the schema.
        empty_rec = next(r for r in recs if "empty.json" in r.getMessage())
        assert empty_rec.levelno == logging.INFO, "empty program is INFO, not a failure"
        missing_rec = next(r for r in recs if "nosteps.json" in r.getMessage())
        assert missing_rec.levelno == logging.WARNING
    print("  ok: malformed and non-runnable programs are logged distinctly")


def test_empty_program_cannot_bypass_exact_top_level_schema() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        directory = Path(tmp)
        _write(
            directory,
            "extra.json",
            '{"name": "empty", "steps": [], "unsupported": true}',
        )
        warnings: list[str] = []
        assert program._load_programs_from(directory, warnings) == []
        assert len(warnings) == 1
        assert "exactly 'name' and 'steps'" in warnings[0]


def test_duplicate_names_keep_distinct_file_identifiers() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        _write(d, "a.json",
               '{"name": "Dup", "steps": [{"type": "work", '
               '"duration": {"kind": "time", "seconds": 60}}]}')
        _write(d, "b.json",
               '{"name": "Dup", "steps": [{"type": "rest", '
               '"duration": {"kind": "time", "seconds": 30}}]}')
        progs = program._load_programs_from(d)
        assert len(progs) == 2, "both duplicate-named programs still load"
        assert [item.identifier for item in progs] == ["a.json", "b.json"]
        assert [item.name for item in progs] == ["Dup", "Dup"]
