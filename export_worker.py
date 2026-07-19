"""Export worker: CSV-to-FIT conversion and Strava upload.

Runs the post-workout export side of a session as a worker coordinated by the
main GUI, without blocking it. Given a workout file it converts a CSV to a
Garmin FIT (via :class:`app.export_fit.FitExporter`) and optionally uploads the
result to Strava (via :class:`app.strava_uploader.StravaUploader`): a CSV is
converted first and then uploaded if requested, while an existing FIT is
uploaded directly; with uploading off, a CSV is only converted and a FIT needs
no work. The companion :mod:`workout_worker` handles the live recording side.
"""

from collections.abc import Callable
from pathlib import Path

from app.config import parse_bool
from app.export_fit import FitExporter
from app.strava_uploader import StravaUploader
from app.worker_result import WorkerOutcome, WorkerResult
from gui.config_manager import ConfigManager


def run_export_worker(
    file_path: str,
    access_token: str,
    refresh_token: str,
    client_id: str,
    client_secret: str,
    strava_upload: str = "yes",
    log_callback: Callable[[str], None] | None = None,
    stop_event: object | None = None,
) -> WorkerResult:
    """Run export worker for CSV to FIT conversion and optional Strava upload.

    Args:
        file_path: Path to the workout file (CSV or FIT)
        access_token: Strava access token
        refresh_token: Strava refresh token
        client_id: Strava client ID
        client_secret: Strava client secret
        strava_upload: Whether to upload to Strava ('yes'/'no')
        log_callback: Optional callback function for logging
        stop_event: Optional threading.Event to signal stop request

    Returns:
        Dictionary with result status and updated tokens if refreshed
    """

    def log(msg: str) -> None:
        return log_callback(msg + "\n") if log_callback else print(msg)

    file_obj = Path(file_path)
    is_fit_file = file_obj.suffix.lower() == ".fit"
    do_upload = parse_bool(strava_upload)

    log(f"Processing: {file_obj.name}")
    log(f"File type: {'FIT' if is_fit_file else 'CSV'}")
    log(f"Upload to Strava: {'Yes' if do_upload else 'No'}")
    log("-" * 50)

    # Load configuration
    config_mgr = ConfigManager()
    try:
        cfg = config_mgr.load_runtime_config(require_ergometer=False)
    except Exception as exc:
        return WorkerResult(
            outcome="failed",
            stage="configuration",
            message=str(exc),
            retryable=True,
        )

    # Convert CSV to FIT first (needed for both the save-only and upload paths).
    if not is_fit_file:
        log("Converting CSV to FIT...")
        convert_result = FitExporter.convert_csv_to_fit(file_path)
        if not convert_result.get("success"):
            log(f"✗ Conversion failed: {convert_result.get('message')}")
            return WorkerResult(
                outcome="failed",
                stage="fit_conversion",
                message=str(convert_result.get("message") or "FIT conversion failed"),
                csv_path=file_path,
                retryable=True,
            )
        fit_path = convert_result.get("file_path")
        if not isinstance(fit_path, (str, Path)):
            log("✗ Conversion returned no file path")
            return WorkerResult(
                outcome="failed",
                stage="fit_conversion",
                message="Conversion failed: no output file",
                csv_path=file_path,
                retryable=True,
            )
        log(f"✓ Converted to FIT: {Path(fit_path).name}")
        if not do_upload:
            log("-" * 50)
            return WorkerResult(
                outcome="success",
                stage="fit_conversion",
                message="FIT file created successfully",
                csv_path=str(file_obj),
                fit_path=str(fit_path),
            )
        file_path = str(fit_path)  # Use converted FIT file for upload
    elif not do_upload:
        log("✓ No action required")
        log("File is already in FIT format and upload not requested.")
        return WorkerResult(
            outcome="success",
            stage="fit_conversion",
            message="No action required",
            fit_path=str(file_obj),
        )

    log("Uploading to Strava...")
    uploader = StravaUploader(
        access_token=access_token,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        config=cfg,
        log_callback=log_callback,
        expires_at=cfg.get("strava_expires_at", 0),
        stop_event=stop_event,
    )
    result = uploader.upload_file(
        file_path=file_path,
        name=None,  # Let Strava auto-generate name
        description=None,
    )

    # Persist refreshed tokens even when the upload itself failed: Strava
    # rotates refresh tokens, so dropping the new one after a failed upload
    # (e.g. offline) would strand the user with a dead credential and force
    # a full reconnect.
    new_access_token, new_refresh_token, new_expires_at = uploader.get_updated_tokens()
    if new_access_token != access_token or new_refresh_token != refresh_token:
        log("\nTokens were refreshed. Updating configuration...")
        cfg["strava_access_token"] = new_access_token
        cfg["strava_refresh_token"] = new_refresh_token
        cfg["strava_expires_at"] = new_expires_at
        config_mgr.save_config(cfg)
        log("Configuration updated with new tokens.")
        tokens_updated = True
    else:
        tokens_updated = False

    # Display result
    if result["success"]:
        log("✓ SUCCESS!")
        log(result["message"])
    else:
        log("✗ UPLOAD FAILED")
        log(result["message"])

    log("-" * 50)
    log("Export process complete.")

    outcome: WorkerOutcome = "pending" if result.get("pending") else (
        "success" if result.get("success") else "failed"
    )
    activity_id_raw = result.get("activity_id")
    upload_id_raw = result.get("upload_id")
    return WorkerResult(
        outcome=outcome,
        stage="strava_upload",
        message=str(result.get("message") or "Upload finished"),
        csv_path=str(file_obj) if not is_fit_file else None,
        fit_path=str(file_path),
        retryable=outcome in ("failed", "pending"),
        activity_id=(
            int(activity_id_raw) if activity_id_raw is not None else None
        ),
        upload_id=int(upload_id_raw) if upload_id_raw is not None else None,
        tokens_updated=tokens_updated,
        new_access_token=new_access_token if tokens_updated else None,
        new_refresh_token=new_refresh_token if tokens_updated else None,
    )
