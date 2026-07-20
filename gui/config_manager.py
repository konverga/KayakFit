"""Single source of truth for KayakFit configuration (YAML).

Manages Strava credentials, device settings, and user preferences for both
the GUI and the headless data-collection / export workers: one shared set of
defaults, YAML storage in the user's home directory, invalid-file quarantine
for the GUI, and shared strict validation for GUI and workers. Secrets are overlaid from
the OS credential vault (``app.secret_store``) rather than kept in the
plaintext file. Deliberately has no GUI dependencies so headless workers can
import it.
"""

import contextlib
import os
import re
import stat
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar

import yaml

from app import secret_store
from app.power import DEFAULT_PULL_LENGTH_M

# Address fields that may accidentally carry a "Name (address)" dropdown label.
_ADDRESS_KEYS = ("ergometer_mac", "hrm_mac")


def _extract_addr(value: str) -> str:
    """Return the bare BLE address from a possible 'Name (ADDRESS)' label.

    Device dropdowns display 'Name (address)' and CTkOptionMenu writes that
    label back into its bound variable, so it can leak into stored values. A
    real MAC/UUID has no parentheses, so this is safe for clean values too.
    """
    value = (value or "").strip()
    if value in ("", "—"):
        return ""
    match = re.search(r"\(([^()]+)\)\s*$", value)
    return match.group(1).strip() if match else value


class ConfigError(Exception):
    """Raised when configuration cannot be read or fails validation."""


class ConfigManager:
    """Manages configuration file persistence (single source of truth)."""

    DISPLAY_MAP: ClassVar[dict[int, str]] = {
        0: "Elapsed time",
        2: "Total distance (*)",
        3: "Avg. speed (*)",
        4: "Inst. speed",
        5: "Avg. stroke rate",
        6: "Inst. stroke rate",
        7: "Avg. 200m pace (sec)",
        8: "Inst. 200m pace (sec)",
        9: "Avg. 500m pace (sec)",
        10: "Inst. 500m pace (sec)",
        11: "Avg. 1000m pace (sec)",
        12: "Inst. 1000m pace (sec)",
        13: "Avg. force",
        14: "Inst. force",
        15: "Total Expended Energy (*) (kCal)",
        17: "Inst. power (W)",
        18: "Avg. power (W)",
    }

    # Canonical defaults, shared by the GUI and the workers. Values are stored
    # in their on-disk (string/int/list) form; runtime validation happens in
    # load_runtime_config().
    DEFAULTS: ClassVar[dict[str, Any]] = {
        "ergometer_name": "",
        "ergometer_mac": "",
        # Display label of the chosen strap. HRMs are discovered by the BLE
        # Heart Rate Service (not by name), so this is informational only.
        "hrm_name": "",
        "hrm_mac": "",
        # "no" skips the HRM entirely at workout start: no connection attempt
        # and no "continue without HRM?" prompt (for users without a strap).
        "hrm_enabled": "yes",
        "person_weight_default": 75,
        "boat_weight_default": 12,
        # Effective handle pull length (m) used for power estimation
        # (power = instantaneous force x pull_length x strokes/s). See
        # docs/POWER_MODEL.md to refit it.
        "pull_length_m": DEFAULT_PULL_LENGTH_M,
        "max_hr": 185,
        # HR zones. "auto" derives the five zone bounds from max_hr; "manual"
        # uses hr_zones (five ascending bpm lower bounds, Z1..Z5).
        "hr_zone_mode": "auto",
        "hr_zones": [],
        "pace_distance": 500,
        "appearance_mode": "system",
        "activity_log_visible": True,
        "display_config_numbers": [0, 2, 4, 14, 17],
        "log_interval": 5,
        "log_level": "info",
        # BLE connection stability settings.
        "ble_connect_timeout": 15,
        "ble_connect_retries": 3,
        "ble_auto_reconnect": "yes",
        "ble_reconnect_delay": 5,
        # Startup connection attempts for the HRM. Kept low so a missing strap
        # surfaces the "continue without HRM?" prompt quickly; mid-session
        # auto-reconnect is unaffected.
        "hrm_connect_retries": 1,
        "hrm_stale_timeout": 10,
        "data_stall_timeout": 8,
        "auto_upload": "no",
        # End (and save) a workout after this many minutes without forward
        # movement, so a forgotten session doesn't become a multi-hour activity.
        # 0 disables it.
        "inactivity_autostop_minutes": 30,
        # Set once the first-run setup wizard has been shown.
        "wizard_seen": False,
        # Play a sound when a program phase (work/rest) changes.
        "sound_cues": "yes",
        # Seconds a program target must stay out of (or back in) range before the
        # on-screen badge changes color — smooths normal metric fluctuation.
        "target_grace_seconds": 3,
        "output_dir": "workouts",
        "strava_client_id": "",
        "strava_client_secret": "",
        "strava_access_token": "",
        "strava_refresh_token": "",
        "strava_expires_at": 0,
    }

    def __init__(self) -> None:
        """Initialize config manager and ensure the config directory exists."""
        self.config_folder: Path = Path.home() / "KayakFit"
        self.config_folder.mkdir(parents=True, exist_ok=True)
        self.config_file: Path = self.config_folder / "config.yml"
        self.config_warning: str | None = None

    def _read_raw(self) -> dict[str, Any]:
        """Read the raw YAML config from disk.

        Returns:
            Parsed dictionary (empty if the file does not exist).

        Raises:
            ConfigError: If the file exists but cannot be read or parsed.
        """
        if not self.config_file.exists():
            return {}
        try:
            with open(self.config_file, encoding="utf-8") as f:
                data = yaml.safe_load(stream=f) or {}
            if not isinstance(data, dict):
                raise ConfigError("Configuration root must be a mapping")
            return data
        except yaml.YAMLError as e:
            raise ConfigError(f"Could not parse config file {self.config_file}: {e}") from e
        except OSError as e:
            raise ConfigError(f"Could not read config file {self.config_file}: {e}") from e

    def _merge_with_defaults(self, data: dict[str, Any]) -> dict[str, Any]:
        """Merge file data onto defaults, keeping only recognized keys."""
        allowed = set(self.DEFAULTS)
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ConfigError(f"Unknown configuration keys: {unknown}")
        cfg: dict[str, Any] = dict(self.DEFAULTS)
        cfg.update({k: v for k, v in data.items() if k in allowed})
        self._apply_credential_store(cfg)
        # Self-heal device addresses that may have been stored as 'Name (addr)'.
        for key in _ADDRESS_KEYS:
            cfg[key] = _extract_addr(str(cfg.get(key, "")))
        return self.validate_config(cfg)

    def validate_config(self, cfg: dict[str, Any]) -> dict[str, Any]:
        """Return one canonical validated configuration."""
        validated = dict(cfg)
        ranges: dict[str, tuple[type[int] | type[float], float, float]] = {
            "person_weight_default": (int, 20, 300),
            "boat_weight_default": (int, 0, 100),
            "pull_length_m": (float, 0.3, 1.2),
            "max_hr": (int, 100, 240),
            "pace_distance": (int, 200, 1000),
            "log_interval": (int, 1, 3600),
            "ble_connect_timeout": (float, 1, 120),
            "ble_connect_retries": (int, 1, 20),
            "ble_reconnect_delay": (float, 0, 120),
            "hrm_connect_retries": (int, 1, 20),
            "hrm_stale_timeout": (float, 0, 300),
            "data_stall_timeout": (float, 1, 300),
            "inactivity_autostop_minutes": (float, 0, 1440),
            "target_grace_seconds": (int, 1, 60),
            "strava_expires_at": (int, 0, 4_102_444_800),
        }
        for key, (kind, minimum, maximum) in ranges.items():
            value = validated.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ConfigError(f"{key} must be numeric")
            if kind is int and not isinstance(value, int):
                raise ConfigError(f"{key} must be a whole number")
            if not minimum <= float(value) <= maximum:
                raise ConfigError(f"{key} must be between {minimum:g} and {maximum:g}")
            validated[key] = kind(value)
        if validated["pace_distance"] not in (200, 500, 1000):
            raise ConfigError("pace_distance must be 200, 500, or 1000")
        if str(validated.get("appearance_mode", "")).lower() not in (
            "system", "light", "dark"
        ):
            raise ConfigError("appearance_mode must be system, light, or dark")
        if not isinstance(validated.get("activity_log_visible"), bool):
            raise ConfigError("activity_log_visible must be true or false")
        if str(validated.get("log_level", "")).lower() not in (
            "debug", "info", "warning", "error", "critical"
        ):
            raise ConfigError("log_level is invalid")
        if validated.get("hr_zone_mode") not in ("auto", "manual"):
            raise ConfigError("hr_zone_mode must be 'auto' or 'manual'")
        zones = validated.get("hr_zones")
        if not isinstance(zones, list) or any(
            isinstance(value, bool) or not isinstance(value, int) for value in zones
        ):
            raise ConfigError("hr_zones must contain whole numbers")
        if validated["hr_zone_mode"] == "manual" and (
            len(zones) != 5 or zones != sorted(set(zones))
        ):
            raise ConfigError("manual hr_zones must contain five ascending values")
        display = validated.get("display_config_numbers")
        if (
            not isinstance(display, list)
            or not 1 <= len(display) <= 5
            or any(
                isinstance(value, bool) or not isinstance(value, int)
                for value in display
            )
            or len(display) != len(set(display))
        ):
            raise ConfigError("display_config_numbers must contain unique integer slots")
        if any(value not in self.DISPLAY_MAP for value in display):
            raise ConfigError("display_config_numbers contains an unsupported slot")
        for key in ("hrm_enabled", "ble_auto_reconnect", "auto_upload", "sound_cues"):
            if validated.get(key) not in ("yes", "no"):
                raise ConfigError(f"{key} must be 'yes' or 'no'")
        if not isinstance(validated.get("wizard_seen"), bool):
            raise ConfigError("wizard_seen must be true or false")
        for key in (
            "ergometer_name", "ergometer_mac", "hrm_name", "hrm_mac",
            "output_dir", "strava_client_id", "strava_client_secret",
            "strava_access_token", "strava_refresh_token",
        ):
            value = validated.get(key)
            if not isinstance(value, str):
                raise ConfigError(f"{key} must be text")
            # PyObjC and other platform libraries can expose string subclasses
            # that pass isinstance(value, str) but PyYAML refuses to represent.
            # Canonical configuration contains plain built-in strings only.
            validated[key] = str(value)
        return validated

    def _quarantine_invalid_config(self, message: str) -> None:
        """Preserve invalid input and expose a warning for the GUI."""
        if not self.config_file.exists():
            return
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        invalid = self.config_file.with_name(f"config.invalid-{stamp}.yml")
        try:
            os.replace(self.config_file, invalid)
            location = invalid.name
        except OSError as exc:
            location = self.config_file.name
            message = f"{message}; could not rename it: {exc}"
        self.config_warning = f"Invalid configuration remains at {location}: {message}"

    @staticmethod
    def _apply_credential_store(cfg: dict[str, Any]) -> None:
        """Overlay complete Strava credentials from the OS vault (vault wins)."""
        for key, value in secret_store.load_credentials().items():
            if value:
                cfg[key] = value

    def _harden_permissions(self) -> None:
        """Restrict the config file to the owner (no-op effect on some platforms)."""
        with contextlib.suppress(OSError):
            os.chmod(self.config_file, stat.S_IRUSR | stat.S_IWUSR)  # 0o600

    def load_config(self) -> dict[str, Any]:
        """Load GUI configuration, quarantining invalid explicit input.

        Returns:
            Configuration dictionary with all default keys present.
        """
        try:
            return self._merge_with_defaults(self._read_raw())
        except ConfigError as exc:
            self._quarantine_invalid_config(str(exc))
            return self.validate_config(dict(self.DEFAULTS))

    def load_runtime_config(self, require_ergometer: bool = True) -> dict[str, Any]:
        """Load and validate configuration for the data-collection worker.

        Returns:
            Validated configuration dictionary.

        Raises:
            ConfigError: If the file is invalid or required fields are missing.
        """
        cfg = self._merge_with_defaults(self._read_raw())

        ergometer_mac = str(cfg.get("ergometer_mac", "")).strip()
        if require_ergometer and not ergometer_mac:
            raise ConfigError(
                "No ergometer configured. Open Settings and scan/select your ergometer."
            )

        return cfg

    def save_config(self, cfg: dict[str, Any]) -> None:
        """Save configuration, keeping secrets out of the plaintext file when possible.

        Sensitive Strava credentials are written to the OS credential vault if one
        is available; the on-disk YAML then stores blanks for those keys. When no
        vault is available the secrets remain in the file, which is restricted to
        the owner. Either way the file is permission-hardened.

        Args:
            cfg: Configuration dictionary to save.
        """
        to_disk = self.validate_config(cfg)

        # Prefer the OS vault; blank the secrets on disk only if it accepted them.
        if secret_store.store_credentials(cfg):
            for key in secret_store.SENSITIVE_KEYS:
                if key in to_disk:
                    to_disk[key] = ""

        # Write to a temp file in the same directory, then atomically replace the
        # real config. A crash or full disk mid-write can only ever damage the
        # throwaway temp file, never truncate the live config into unparseable
        # YAML. Create the temp owner-only (0600) from the start so secrets are
        # never briefly world-readable (os.open applies the mode at creation).
        tmp_path = self.config_file.with_name(self.config_file.name + ".tmp")
        fd = os.open(
            tmp_path,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC,
            stat.S_IRUSR | stat.S_IWUSR,
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(data=to_disk, stream=f, sort_keys=False)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self.config_file)
        except yaml.YAMLError as exc:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise ConfigError(f"Could not serialize configuration: {exc}") from exc
        except OSError:
            # Best-effort cleanup so a failed save doesn't leave a stale temp.
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

        # Keep the final file owner-only on platforms that support POSIX modes.
        self._harden_permissions()
