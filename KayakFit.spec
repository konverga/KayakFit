# -*- mode: python ; coding: utf-8 -*-
#
# Cross-platform build spec. Builds a macOS .app bundle on macOS and a Windows
# onedir distribution on Windows (PyInstaller is run on each target OS).
from PyInstaller.utils.hooks import collect_all
import os
import sys

IS_MAC = sys.platform == "darwin"
IS_WINDOWS = sys.platform.startswith("win")

# Per-platform application icon (macOS uses .icns, Windows uses .ico).
APP_ICON = "assets/icon_kayakfit.icns" if IS_MAC else "assets/icon_kayakfit.ico"

# App version: the release workflow sets KAYAKFIT_VERSION from the git tag
# (e.g. "1.2.3" from tag "v1.2.3") so the built .app/.exe report the same
# version as the GitHub Release. Local/manual builds fall back to 0.1.0.
APP_VERSION = os.environ.get("KAYAKFIT_VERSION", "0.1.0").lstrip("vV")


def _version_tuple(version):
    """Parse 'X.Y.Z' into a 4-int tuple for Windows FixedFileInfo."""
    parts = []
    for p in version.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    parts += [0] * (4 - len(parts))
    return tuple(parts[:4])

# Add local packages as data files
datas = [
    ('assets', 'assets'),
    ('presets', 'presets'),  # built-in training program JSON files
]
binaries = []
hiddenimports = [
    'workout_worker',
    'export_worker',
    # Third-party packages
    'yaml',  # PyYAML for config management
    'requests',  # HTTP library for API calls
    # Tkinter modules
    'tkinter',
    'tkinter.ttk',  # Tkinter themed widgets
    'tkinter.messagebox',  # Tkinter message boxes
    'tkinter.filedialog',  # Tkinter file dialogs
    'tkinter.scrolledtext',  # Tkinter scrolled text widget
    'customtkinter',  # Modern themed widgets used by the GUI
    'darkdetect',  # Appearance detection dependency of customtkinter
    # Secure credential storage (optional; falls back to file if missing).
    'keyring',
    'keyring.backends.macOS',     # macOS Keychain backend
    'keyring.backends.Windows',   # Windows Credential Locker backend
    'keyring.backends.SecretService',  # Linux backend (best-effort)
    # Standard library http modules
    'http.server',
    'urllib.parse',
    # platform.mac_ver() imports plistlib lazily; darkdetect needs it to read
    # the macOS version at startup. Make sure it is always bundled.
    'plistlib',
    # Bleak (Bluetooth) modules
    'bleak',
    'bleak.backends.characteristic',
    'bleak.exc',
    # Explicitly list all modules
    'app.ble_device',
    'app.export_fit',
    'app.field_mapping',
    'app.heart_rate_monitor_bluetooth',
    'app.kayakfirst_ergometer_bluetooth',
    'app.keep_awake',
    'app.logger',
    'app.power',
    'app.program',
    'app.program_runner',
    'app.read_csv',
    'app.recovery',
    'app.secret_store',
    'app.segmentation',
    'app.sound',
    'app.stats',
    'app.strava_api',
    'app.strava_auth',
    'app.strava_uploader',
    'app.summary',
    'app.table',
    'app.workout_session',
    'app.write_csv',
    'gui.config_manager',
    'gui.config_window',
    'gui.dashboard_constants',
    'gui.device_scanner',
    'gui.device_status',
    'gui.export_upload',
    'gui.history_window',
    'gui.main_gui',
    'gui.metrics_format',
    'gui.program_panel',
    'gui.recording_lifecycle',
    'gui.setup_wizard',
    'gui.summary_window',
    'gui.window_utils',
    'gui.worker_manager',
]

tmp_ret = collect_all('bleak')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
tmp_ret = collect_all('fit_tool')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# customtkinter ships theme/asset JSON files that must be bundled.
tmp_ret = collect_all('customtkinter')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
# keyring discovers backends via entry points; collect them so the secure
# credential path works in frozen builds. Optional — app falls back if absent.
try:
    tmp_ret = collect_all('keyring')
    datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
except Exception:
    pass


a = Analysis(
    ['kayakfit_gui.py', 'workout_worker.py', 'export_worker.py'],
    pathex=['.'],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    # Hardens platform.mac_ver() so darkdetect/customtkinter can't crash on
    # startup when the OS version string is unreadable (e.g. inside VMs).
    runtime_hooks=['hooks/pyi_rth_mac_ver.py'],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

if IS_WINDOWS:
    # Embed file/product version info so Explorer's Properties > Details tab
    # and installers report the same version as the GitHub Release.
    from PyInstaller.utils.win32.versioninfo import (
        VSVersionInfo, FixedFileInfo, StringFileInfo, StringTable,
        StringStruct, VarFileInfo, VarStruct,
    )

    _ver = _version_tuple(APP_VERSION)
    version_info = VSVersionInfo(
        ffi=FixedFileInfo(
            filevers=_ver,
            prodvers=_ver,
            mask=0x3F,
            flags=0x0,
            OS=0x40004,
            fileType=0x1,
            subtype=0x0,
            date=(0, 0),
        ),
        kids=[
            StringFileInfo([
                StringTable('040904B0', [
                    StringStruct('CompanyName', 'Konverga'),
                    StringStruct('FileDescription', 'KayakFit'),
                    StringStruct('FileVersion', APP_VERSION),
                    StringStruct('InternalName', 'KayakFit'),
                    StringStruct('OriginalFilename', 'KayakFit.exe'),
                    StringStruct('ProductName', 'KayakFit'),
                    StringStruct('ProductVersion', APP_VERSION),
                ]),
            ]),
            VarFileInfo([VarStruct('Translation', [1033, 1200])]),
        ],
    )

    # Single-file executable: binaries/datas are embedded directly in the
    # EXE (exclude_binaries=False) and unpacked to a temp dir at runtime, so
    # the final deliverable is one standalone dist-win\KayakFit.exe file.
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name='KayakFit',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        # Windowed app: no console window behind the GUI. Diagnostics go to
        # the rotating log at ~/KayakFit/logs/kayakfit.log (the logger skips
        # the console sink when stdout is absent in a windowed build).
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=[APP_ICON],
        version=version_info,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name='KayakFit',
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
        icon=[APP_ICON],
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name='KayakFit',
    )

# macOS .app bundle (with Bluetooth usage strings). On Windows the EXE above
# (dist-win\KayakFit.exe) is the final single-file deliverable.
if IS_MAC:
    app = BUNDLE(
        coll,
        name='KayakFit.app',
        icon='assets/icon_kayakfit.icns',
        bundle_identifier='com.kayakfit.app',
        info_plist={
            'CFBundleShortVersionString': APP_VERSION,
            'CFBundleVersion': APP_VERSION,
            'NSHighResolutionCapable': True,
            'NSBluetoothAlwaysUsageDescription': 'KayakFit needs Bluetooth access to connect to your KayakFirst ergometer and heart rate monitor for workout tracking.',
            'NSBluetoothPeripheralUsageDescription': 'KayakFit needs Bluetooth access to connect to your KayakFirst ergometer and heart rate monitor for workout tracking.',
        },
    )
