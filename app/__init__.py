"""KayakFit - Workout Data Logger.

Modular package for logging kayak workout data from Bluetooth devices.

Import submodules directly (e.g. ``from app.read_csv import CsvReader``).
This __init__ deliberately imports nothing: eager re-exports used to pull in
heavy optional dependencies (bleak, fit_tool) for callers that only needed a
pure module like ``app.recovery`` or ``app.segmentation``, slowing startup and
breaking headless/test imports.

The project follows semantic versioning starting from 0.1.0 (see the
"Versioning" section in ``CONTRIBUTING.md``).
"""

__version__ = "0.1.0"
