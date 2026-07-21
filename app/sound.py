"""Simple cross-platform audible cues for structured programs.

Cues signal an upcoming or just-happened program change (warm-up → work, work →
rest, a step about to end, program complete) so the athlete knows a change is
expected without watching the screen. Auto-pause/resume is deliberately silent:
the athlete already knows when they stop and start paddling.

The tones are synthesized at runtime from one shared table, so every platform
sounds identical and no audio files are bundled. Meaning is carried by pitch:
rising = go/start, falling = ease off/stop. Playback is best-effort — any
failure is swallowed so audio can never disrupt a workout.

Cue names: "warmup", "work", "rest", "cooldown", "phase", "complete", "alert",
"countdown".
"""

import contextlib
import hashlib
import math
import os
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import wave

# One tone table drives every platform. Each cue is a sequence of
# (frequency_Hz, duration_ms) notes; pitch direction carries the meaning.
_CUE_TONES: dict[str, list[tuple[int, int]]] = {
    "warmup":    [(523, 140), (659, 140), (784, 240)],     # C5-E5-G5 gentle climb = "ease in"
    "work":      [(1047, 130), (1319, 190)],               # C6->E6 sharp rise = "go"
    "rest":      [(784, 150), (523, 240)],                 # G5->C5 falling = "ease off"
    "cooldown":  [(784, 180), (659, 180), (523, 300)],     # G5-E5-C5 gentle fall = "wind down"
    "phase":     [(880, 140), (1047, 190)],                # A5->C6, generic "next segment"
    "complete":  [(1047, 130), (1319, 130), (1568, 320)],  # C-E-G rising = "done"
    "alert":     [(932, 120), (740, 200)],                 # Bb5->F#5 falling = "check effort"
    "countdown": [(1319, 240)],                            # E6, bright single "about to change"
}

_SAMPLE_RATE = 44100
_CUE_DIR = os.path.join(tempfile.gettempdir(), "kayakfit_cues")


def _cue_wav(name: str) -> str:
    """Return a cached WAV path for the cue, rendering it once on first use."""
    notes = _CUE_TONES.get(name, _CUE_TONES["phase"])
    key = hashlib.md5(repr(notes).encode()).hexdigest()[:8]
    path = os.path.join(_CUE_DIR, f"{name}_{key}.wav")
    if not os.path.exists(path):
        _render_wav(path, notes)
    return path


def _render_wav(path: str, notes: list[tuple[int, int]]) -> None:
    """Synthesize a short sine-tone sequence (with fades) to a mono WAV file."""
    frames = bytearray()
    fade = int(_SAMPLE_RATE * 0.006)          # 6 ms fade in/out, avoids clicks
    gap = b"\x00\x00" * int(_SAMPLE_RATE * 0.03)  # 30 ms silence between notes
    for freq, ms in notes:
        n = int(_SAMPLE_RATE * ms / 1000)
        for i in range(n):
            env = min(1.0, i / fade, (n - i) / fade) if fade else 1.0
            val = int(0.6 * env * 32767 * math.sin(2 * math.pi * freq * i / _SAMPLE_RATE))
            frames += struct.pack("<h", val)
        frames += gap
    os.makedirs(_CUE_DIR, exist_ok=True)
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(_SAMPLE_RATE)
        w.writeframes(bytes(frames))


class SoundPlayer:
    """Plays short audible cues, one at a time, on a single background thread.

    Cues are serialized through a queue so two cues never play simultaneously —
    e.g. an interval countdown and the next step's start cue can fire within a
    second of each other without overlapping. If cues arrive faster than they
    can play, the backlog is capped so playback can't lag behind the workout.
    """

    # Most a cue can wait behind: keep audio roughly in sync with reality.
    _MAX_PENDING = 3

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._queue: queue.Queue[str] = queue.Queue(maxsize=self._MAX_PENDING)
        threading.Thread(target=self._run, daemon=True).start()

    def set_enabled(self, enabled: bool) -> None:
        """Enable or disable audible cue playback."""
        self.enabled = enabled

    def cue(self, name: str) -> None:
        """Queue the named cue (non-blocking, best-effort)."""
        if not self.enabled:
            return
        # Already backed up; drop this cue rather than play it late.
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(name)

    def _run(self) -> None:
        while True:
            self._play(self._queue.get())

    def _play(self, name: str) -> None:
        with contextlib.suppress(Exception):
            path = _cue_wav(name)
            if sys.platform == "darwin":
                subprocess.run(
                    ["afplay", path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
            elif sys.platform.startswith("win"):
                import winsound

                winsound.PlaySound(path, winsound.SND_FILENAME)
            else:
                # Linux / other: best-effort. Try a desktop WAV player, else
                # fall back to the terminal bell.
                if not self._linux_play(path):
                    sys.stdout.write("\a")
                    sys.stdout.flush()

    @staticmethod
    def _linux_play(path: str) -> bool:
        """Try common Linux WAV players; return True if one was invoked."""
        for player in ("paplay", "aplay", "play"):
            try:
                subprocess.run(
                    [player, path],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                return True
            except FileNotFoundError:
                continue
            except Exception:
                return False
        return False
