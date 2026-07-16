"""Type-checking-only shared surface for the KayakFitGUI mixins.

``main_gui.KayakFitGUI`` is assembled from four focused mixins
(``ProgramPanelMixin``, ``DeviceStatusMixin``, ``RecordingLifecycleMixin``,
``ExportUploadMixin``). Each mixin freely reads attributes and calls methods
that are actually defined on a *sibling* mixin or on ``KayakFitGUI`` itself, so
mypy -- which checks every mixin in isolation -- cannot see them and reports
spurious ``attr-defined`` / ``has-type`` errors.

``GuiSharedState`` declares that shared surface in one place. Every member is
annotated ``Any`` on purpose: this class only exists to tell mypy the names
exist, not to pin their types (the real definitions -- widgets, StringVars,
methods, properties -- live in the mixins and in ``KayakFitGUI.__init__``).
Using ``Any`` keeps those real definitions from tripping override/redefinition
checks against this base.

The mixins inherit this base **only under ``TYPE_CHECKING``** (see the
``if TYPE_CHECKING: _MixinBase = GuiSharedState else: _MixinBase = object``
idiom in each mixin module), so there is no runtime base-class change and no
behavioural effect whatsoever.
"""

from typing import Any

from .controllers import ExportController, ProgramController, RecordingController


class GuiSharedState:
    """Declared-only view of the composed ``KayakFitGUI`` surface (see module docstring)."""

    _advance_program: Any
    _apply_program: Any
    _apply_status: Any
    _apply_upload: Any
    _auto_export: Any
    _clear_hr_zone: Any
    _clear_target_indicator: Any
    _free_label: Any
    _hide_banner: Any
    _hide_program_panel: Any
    _hrm_battery: Any
    _init_chips: Any
    _last_data_ts: Any
    _last_hr_ts: Any
    _latest_data: Any
    _pace_distance: Any
    _primary_fg: Any
    _primary_hover: Any
    _program_labels: Any
    _program_warn_sig: Any
    _programs: Any
    _selected_program_name: Any
    _refresh_hr_legend: Any
    _reload_cfg: Any
    _reset_device_chips: Any
    _restore_tile_brightness: Any
    _screen_awake: Any
    _set_chip: Any
    _set_export_buttons: Any
    _set_hr_zone: Any
    _show_banner: Any
    _show_summary: Any
    _sound: Any
    _start_export: Any
    _stats: Any
    _strava_connected: Any
    _tiles_dimmed: Any
    _update_live_label: Any
    _update_target_indicator: Any
    export: ExportController
    program: ProgramController
    recording: RecordingController
    _zones_cfg: Any
    boat_weight_var: Any
    boat_entry: Any
    cfg: Any
    config_mgr: Any
    discard_btn: Any
    hr_legend_labels: Any
    hr_zone_var: Any
    hrm_chip: Any
    lap_label: Any
    live_label: Any
    log_message: Any
    metric_sub_vars: Any
    metric_vars: Any
    open_config: Any
    person_weight_var: Any
    person_entry: Any
    primary_btn: Any
    program_menu: Any
    program_advance_btn: Any
    program_panel: Any
    program_preview_var: Any
    program_var: Any
    recording_pill: Any
    root: Any
    input_error_var: Any
    savefit_btn: Any
    strava_btn: Any
    summary_card: Any
    summary_label: Any
    tile_defaults: Any
    tile_frames: Any
    tile_value_colors: Any
    tile_value_labels: Any
    upload_btn: Any
    view_summary_btn: Any
    worker_mgr: Any
    workouts_btn: Any
