"""PyInstaller runtime hook: harden ``platform.mac_ver()`` in frozen builds.

Some macOS environments (notably virtual machines and stripped-down images)
leave the frozen app unable to read
``/System/Library/CoreServices/SystemVersion.plist``, in which case
``platform.mac_ver()`` returns an empty version string. ``darkdetect`` —
imported by customtkinter at startup — then executes
``int(''.split('.')[0])`` at module import time and the app dies with
"invalid literal for int() with base 10: ''" before any window appears.

Runtime hooks run before application imports, so wrapping ``mac_ver`` here
guarantees every consumer sees a usable version string. Fallback order:
the original implementation, then ``sysctl kern.osproductversion``, then a
modern floor version (so feature checks like dark-mode support enable
rather than crash).

Sunset condition: re-test a frozen build *without* this hook whenever
customtkinter/darkdetect is bumped — if darkdetect no longer crashes on an
empty ``mac_ver()``, delete this file (and its entry in ``KayakFit.spec``).
"""

import sys

if sys.platform == "darwin":
    import platform

    _orig_mac_ver = platform.mac_ver

    def _safe_mac_ver(
        release: str = "",
        versioninfo: tuple[str, str, str] = ("", "", ""),
        machine: str = "",
    ) -> tuple[str, tuple[str, str, str], str]:
        try:
            info = _orig_mac_ver(release, versioninfo, machine)
            if info and info[0]:
                return info
        except Exception:
            pass
        try:
            import subprocess

            out = subprocess.run(
                ["/usr/sbin/sysctl", "-n", "kern.osproductversion"],
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            if out:
                return (out, ("", "", ""), platform.machine())
        except Exception:
            pass
        return ("12.0", ("", "", ""), platform.machine())

    platform.mac_ver = _safe_mac_ver
