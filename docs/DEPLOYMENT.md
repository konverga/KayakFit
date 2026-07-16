# Build, deployment, and release guide

KayakFit is a local desktop application. It has no server deployment. Delivery
means building platform-native PyInstaller artifacts and publishing them through
a tagged GitHub Release.

`main` is the only releasable branch. Feature branches may produce manual test
artifacts, but they must not publish releases.

## Deliverables

| Platform | Build output | Published archive |
|---|---|---|
| macOS 14 Apple Silicon | `dist-mac/KayakFit.app` | `KayakFit-macOS-arm64.zip` |
| Windows | `dist-win\KayakFit.exe` | `KayakFit-Windows.zip` |

The cross-platform build definition is `KayakFit.spec`. It bundles application
modules, assets, preset programs, BLE/FIT dependencies, CustomTkinter data, and
keyring backends. macOS and Windows builds must run on their target operating
system; PyInstaller does not cross-compile these deliverables.

## Pre-build validation

Before creating an artifact:

```bash
uv lock --check
uv sync --locked
uv run ruff check .
uv run mypy . --strict
uv run pytest
```

Also review the outstanding release-relevant items in [ROADMAP.md](ROADMAP.md)
and complete the manual checks in [TESTING.md](TESTING.md).

## Local macOS build

Run on macOS from the repository root:

```bash
./pyinstaller_build.sh
```

The script clears only `build-mac/` and `dist-mac/`, creates `.nosync` markers,
installs the build dependency group, and invokes `KayakFit.spec`.

Launch the result with:

```bash
open dist-mac/KayakFit.app
```

For console-visible startup diagnostics:

```bash
./dist-mac/KayakFit.app/Contents/MacOS/KayakFit
```

Verify that the bundle contains Tcl/Tk frameworks and resource data. The release
workflow checks these paths explicitly:

```text
Contents/Frameworks/Tcl
Contents/Frameworks/Tk
Contents/Resources/tcl9
Contents/Resources/_tcl_data
Contents/Resources/_tk_data
```

The app bundle declares Bluetooth usage descriptions and uses bundle identifier
`com.kayakfit.app`.

## Local Windows build

Run from Command Prompt on Windows:

```bat
pyinstaller_build.bat
```

The result is the single windowed executable `dist-win\KayakFit.exe`. It writes
diagnostics to the normal KayakFit log because a packaged windowed executable
has no console.

## Testing CI builds without releasing

Run the `Build & Release` workflow manually from GitHub Actions. A
`workflow_dispatch` run builds both platforms and uploads workflow artifacts,
but does not create or modify a GitHub Release.

Manual workflow builds use version `0.0.0-dev`. Tagged builds derive the version
from the tag through `KAYAKFIT_VERSION`.

## Versioning

KayakFit follows semantic versioning and is currently in initial `0.x`
development. Keep these three values aligned:

- `app/__init__.py::__version__`;
- `pyproject.toml` project version;
- release tag `vMAJOR.MINOR.PATCH`.

While the project remains below `1.0.0`:

- internal bug fixes normally bump PATCH;
- new features or breaking config/CSV/FIT changes bump MINOR;
- promotion to a stable compatibility commitment becomes `1.0.0`.

Version changes belong in a reviewed commit on `main` before the release tag is
created.

## Publishing a release

After the intended feature branches have been reviewed and integrated into
`main`:

1. Confirm CI is green on `main`.
2. Confirm versions match in `app/__init__.py` and `pyproject.toml`.
3. Run or review platform smoke tests.
4. Create the matching annotated or lightweight tag on the selected `main`
   commit.
5. Push only that tag when release publication is authorized.

Example:

```bash
git switch main
git pull --ff-only
git tag v0.1.0
git push origin v0.1.0
```

Pushing a `v*` tag triggers `.github/workflows/build-release.yml`. The workflow:

1. builds macOS on `macos-14` using an official Python with Tcl/Tk;
2. verifies the macOS Tcl/Tk bundle;
3. builds Windows on `windows-latest`;
4. uploads both build artifacts;
5. creates or updates the tag's GitHub Release and attaches both ZIP files.

Publishing through the GitHub Release UI does not trigger a build. Release
artifacts are produced only by an authorized `v*` tag on `main`.

## Release verification

Download the archives from the Release page rather than testing only workspace
outputs. On each platform verify:

- the archive extracts successfully;
- the app starts without a source checkout or Python environment;
- the displayed/file version matches the release tag;
- bundled assets and preset workouts are present;
- Bluetooth permission and scanning work;
- a short workout can be recorded, summarized, and exported to FIT;
- the packaged app can read/write `~/KayakFit` data and access credentials;
- logs are written and contain no secrets.

Keep the previous release available until smoke verification completes.

## Signing and platform warnings

Code signing and notarization are not currently configured. Downloaded macOS
builds can show a Gatekeeper “unidentified developer” warning, and Windows can
show a SmartScreen warning. These warnings are an acknowledged deployment gap,
not evidence that the archive is corrupt.

Do not document unsigned artifacts as trusted or instruct users to disable
platform security globally. When signing is introduced, record certificate
ownership, secret handling, notarization, timestamping, and verification here
without committing credentials.

## Failure and rollback

If a release build or smoke test fails:

1. Do not move or reuse the failed version tag for different source history.
2. Keep the last known-good release published.
3. Record the failed platform, workflow run, logs, and reproduction details.
4. Fix through a reviewed branch and issue a new patch version/tag.
5. Mark a bad GitHub Release as a prerelease or remove its downloadable assets
   only with maintainer authorization.

Application rollback means reinstalling the prior desktop artifact. User CSV,
FIT, metadata, program, and configuration files live outside the app bundle and
must not be deleted by an application rollback. If a release changes a durable
format, document forward/backward compatibility and migration behavior in
[DECISIONS.md](DECISIONS.md) before publication.

## Security and operational boundaries

- GitHub Actions release permissions are write-scoped only for the release
  workflow; ordinary CI uses read-only repository contents.
- OAuth tokens and client secrets must never be embedded in builds, logs, test
  fixtures, workflow artifacts, or release notes.
- Runtime credentials use the OS keychain where available, with the documented
  restricted fallback behavior.
- Do not upload real user workout data as CI artifacts.
- Build directories and archives are generated outputs and are not committed.

See `CONTRIBUTING.md` for the repository governance and pull-request workflow;
this document is the operational build/release reference.
