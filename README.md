# KayakFit

KayakFit is a desktop app for logging **KayakFirst Bull** ergometer workouts. It
connects over Bluetooth to your ergometer (and, optionally, a Garmin HRM‑Dual
heart‑rate strap), shows a live metric dashboard while you paddle, and saves each
session as a `.csv` and a `.fit` file — with one‑click upload to Strava.

Runs on **macOS** and **Windows**.

---

## Features

- **Live dashboard** — time, distance, speed, pace (200/500/1000 m), stroke rate,
  heart rate, pull force and estimated power, updated once per second.
- **Heart‑rate zones** — colored zones with bpm ranges, derived from your max HR
  or set manually.
- **Auto‑pause & laps** — recording pauses automatically when you stop and resumes
  when you move again; each effort becomes a lap in the FIT file. When you run a
  **structured program**, the FIT laps follow the program steps instead, so each
  interval (warm‑up, work, rest…) shows up as its own lap on Strava.
- **Structured training programs** — run interval sessions (e.g. *4 × 500 m /
  1:00 rest*) with a live step panel, countdowns, and audible cues on each phase
  change.
- **Workout summary** — per‑workout detail screen with stacked charts for
  speed, power, heart rate, cadence and pull force on one shared time axis
  (pick the metrics you want, hover for exact values), time in HR zones, and
  per‑lap splits.
- **Estimated power** — the ergometer has no power sensor; KayakFit tries to reproduce
  the machine display's power model
- **Strava upload** — convert to FIT and upload automatically or on demand. FIT
  records map active ergometer distance onto a fixed Hazewinkel course so
  Strava can infer pauses from stationary positions in its overview.
- **Robustness** — crash recovery for interrupted sessions, inactivity
  auto‑stop, the screen is kept awake during a workout, the HRM battery level
  is shown so a dying strap isn’t a surprise, and the Stop button stays
  responsive even while the ergometer connection is retrying.
- **First‑run setup wizard** to get your devices connected quickly.

---

## Requirements

**Hardware**
- A KayakFirst Bull ergometer (Bluetooth).
- *(Optional)* A Garmin HRM‑Dual (or any standard BLE heart‑rate strap).
- A Mac or Windows PC with Bluetooth.

**Software (to run from source or build)**
- [uv](https://docs.astral.sh/uv/) — the dependency manager and runner.
- **Python 3.14** (developed and built with **Python 3.14.5**). You don't
  need to install Python yourself: `uv` fetches the right version for you.

You don’t need Python or uv at all if you use a packaged build (see
[Building a standalone app](#building-a-standalone-app)).

---

## Install & run from source

```bash
# 1. Clone the repo and enter it
cd KayakFit

# 2. Install dependencies into a managed .venv (creates it, fetches Python 3.14
#    if needed, and installs exactly what uv.lock pins)
uv sync

# 3. Run the app
uv run python kayakfit_gui.py
```

`uv run` executes inside the project environment without activating it; if you
prefer, `source .venv/bin/activate` (macOS/Linux) or `.venv\Scripts\activate`
(Windows) once, then run `python kayakfit_gui.py` directly.

No uv? A `requirements.txt` is generated from the lock as a pip fallback:
`python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`.

On macOS you’ll be asked to grant **Bluetooth** permission the first time.

---

## First‑time setup

On first launch a short **setup wizard** appears:

1. Enter the device name printed in large letters on the ergometer, then
   **scan** for Bluetooth devices (turn your ergometer on and keep it nearby).
2. **Pick your ergometer** and, optionally, a heart‑rate monitor.
3. Optionally choose to set up **Strava** afterward.

You can redo any of this later in **Settings**.

### Connecting Strava (optional)

1. Create an API application at <https://developers.strava.com>.
2. Set its **Authorization Callback Domain** to `127.0.0.1`.
3. In KayakFit → **Settings → Strava**, paste your **Client ID** and
   **Client Secret** and click **Connect to Strava**. A browser window opens for
   you to authorize; KayakFit captures the result automatically.

Your complete Strava authorization is stored in the operating-system keychain,
not in a plain file. It survives deletion of `~/KayakFit`; use **Disconnect** in
Settings to remove it from the keychain.

---

## Recording a workout

1. Set **Boat (kg)** and **Person (kg)** in the setup bar (used for power/pace).
2. *(Optional)* Pick a **Program** from the dropdown, or leave it on
   *Free workout*.
3. Click **Start workout** (or press the **spacebar**).
4. Paddle. The dashboard updates live; the heart‑rate tile is tinted by zone.
5. When you’re done, click **Stop & save** — you’ll be asked to **tap again to
   confirm** so a stray click can’t end the session. Press the spacebar twice for
   the same effect.

On stop, the workout is saved as CSV + FIT and (if enabled) uploaded to Strava.

**During the workout you’ll also see:**
- a **⏸ Paused** badge and dimmed tiles during an auto‑pause,
- a **Lap N** counter,
- the **program panel** (if running a program) with the current step, a
  countdown to the next phase, the next step, and two progress bars — one for
  the **current step** and a thin one for the **whole workout**,
- warning **banners** for things like a lost connection or a low HRM battery.

If you forget to stop, the workout **auto‑stops and saves after 30 minutes** of
no movement.

### Workouts

**Workouts** is the one place for past sessions. It lists them newest first;
click one to select it (the most recent is pre‑selected, double‑click opens
its summary) and use the action bar at the bottom: **Summary** and **Save
FIT** (CSV files), and **Upload** (CSV or FIT, needs a connected Strava
account). **Open folder** reveals the files in Finder/Explorer, and **Choose
file…** adds a workout stored somewhere else to the list so the same buttons
work on it.

**Summary** opens a detail screen for a workout: headline stats (active distance,
active/elapsed/pause time, average/max sensor speed, heart rate, power, stroke rate), stacked
per‑metric raw-point charts — sensor speed, power, heart rate, cadence and pull force, each
with its own value axis on one shared time axis — time in each heart‑rate
zone, and a per‑lap splits table. Checkboxes above the chart choose which
metrics are shown, and hovering shows a crosshair with the exact values at
that moment. Heart‑rate elements appear whenever the workout has HR data.
The laps match the FIT export, so Strava receives the same split structure
(although Strava controls how it presents imported laps).
The same screen is available from the **View summary** button on the
"Workout saved" card right after you stop a workout.

For the exact rules and worked examples, see
[`docs/DATA_TRANSFORMATIONS.md`](docs/DATA_TRANSFORMATIONS.md). It explains
active versus elapsed time, pause handling, speed and power calculations,
free/planned laps, FIT fields, the synthetic GPS track, and what Strava may
recalculate after upload.

---

## Training programs

A program is a sequence of steps (warm‑up, work, rest, cool‑down). All plans
live as JSON files in **`~/KayakFit/programs/`** and appear in the Program
dropdown (refreshed when the window regains focus). On first run the bundled
example plans are copied there, so you can edit, rename, or delete them like
any other plan — deleted examples stay deleted. The 📂 button next to the
dropdown opens the folder.

To add your own, drop a `.json` file like this into `~/KayakFit/programs/`:

```json
{
  "name": "5 × 400 m in Zone 4 / 90 s rest",
  "steps": [
    {"type": "warmup", "duration": {"kind": "time", "seconds": 300}},
    {"repeat": 5, "steps": [
      {"type": "work",
       "duration": {"kind": "distance", "meters": 400},
       "target": {"metric": "hr_zone", "low": 4, "high": 4}},
      {"type": "rest", "duration": {"kind": "time", "seconds": 90}}
    ]},
    {"type": "cooldown", "duration": {"kind": "time", "seconds": 300}}
  ]
}
```

### Step fields

| Field | Values |
|---|---|
| `type` | `warmup` · `work` · `rest` · `cooldown` |
| `duration.kind` | `time` (with `seconds`), `distance` (with `meters`), or `open` (advance manually) — the step advances when that time/distance is reached, or immediately when you press **Next step** for an open step |
| `label` | *(optional)* overrides the auto‑generated step name (e.g. `"2 km TT"`) |
| `target` | *(optional)* the intended intensity for the step — see below |
| `repeat` | wraps a list of `steps` and repeats them N times |

### Targets (optional)

A step may carry a `target` of the form `{"metric", "low", "high"}` describing
the effort you’re aiming for. `low`/`high` are inclusive bounds — use the **same
value for both** to target a single zone.

| `metric` | What `low` / `high` mean | Example |
|---|---|---|
| `hr_zone` | heart‑rate zone **1–5** | `{"metric": "hr_zone", "low": 3, "high": 3}` → hold Zone 3 |
| `pace_200` / `pace_500` / `pace_1000` | **seconds** per that split | `{"metric": "pace_500", "low": 130, "high": 145}` → 2:10–2:25 per 500 m |
| `power` | **watts** | `{"metric": "power", "low": 180, "high": 220}` |
| `spm` | **strokes per minute** | `{"metric": "spm", "low": 58, "high": 64}` |

Heart‑rate zones follow your **Settings → Heart‑rate zones** (auto from Max HR,
or your manual bpm bounds), so `hr_zone` targets adapt to your configuration.

While a step with a target is running, the program panel shows a live badge —
**On target** (green), **Too hard ↑** (red) or **Too easy ↓** (amber) — comparing
your current effort to the target range, and a sound plays when you drift out of
range (if sound cues are enabled). Steps without a `target` (like a plain 500 m
interval) simply omit the badge.

To avoid flicker from normal second‑to‑second fluctuation, the badge only
changes once a status has held for a short **grace period** (default **3 s**, set
via **Settings → Logging & processing → Target grace**) — so a brief blip across
the boundary won’t flip the color or trigger the alert.

The program panel has a **Next step ▸** button (also the **→** arrow key) that
advances to the next step — required for `open` steps, and handy to skip ahead on
timed/distance steps. When a program finishes, the workout stops and saves
automatically.

When you record with a program, KayakFit remembers each step's boundaries and
uses them to define the **laps** in the exported FIT file — so a *4 × 500 m*
session appears on Strava as clean interval laps rather than laps guessed from
auto‑pauses. (Free workouts still fall back to auto‑pause laps.)

---

## Settings reference

| Setting | What it does |
|---|---|
| **Devices** | Scan for, select and **test** your ergometer / HRM. A **Use heart‑rate monitor** switch disables the HRM entirely — no connection attempt and no "continue without HRM?" prompt if you don't own a strap. |
| **Weight defaults** | Default boat and person weight, and **Max HR**. |
| **Heart‑rate zones** | `auto` (derive zones from Max HR) or `manual` (set the five bpm bounds). |
| **Display layout** | Which metrics the ergometer’s own screen shows. |
| **Logging & processing** | Log interval/level, handshake mode, data points, auto‑upload, **sound cues** on program phase changes, and the **target grace** (seconds before the target badge changes). |
| **Strava** | Connect/disconnect your Strava account. |

---

## Where your data is stored

Everything lives under a **`KayakFit`** folder in your home directory
(`~/KayakFit` on macOS, `C:\Users\<you>\KayakFit` on Windows):

```
KayakFit/
├── workouts/<year>/workout_YYYYMMDD_HHMMSS/
│   ├── workout.csv                                # raw per-second data
│   ├── metadata.json                              # recorded processing settings
│   ├── steps.json                                 # planned workouts only
│   └── activity.fit                               # created on save/upload
├── programs/*.json                               # training plans (examples + your own)
├── config.yml                                     # settings
└── logs/kayakfit.log                              # rotating diagnostic log
```

The complete Strava authorization is kept in the OS keychain, not in
`config.yml`. It survives deletion of this folder; use **Disconnect** in
Settings to remove it.

One tuning value worth knowing about: `pull_length_m` in `config.yml` is the
effective handle pull length used by the power model (default `0.623`, fitted
against the machine display). If your machine or firmware reads differently,
it can be refitted — the procedure is described in
[`docs/POWER_MODEL.md`](docs/POWER_MODEL.md).

---

## Building a standalone app

Builds use **PyInstaller** with the cross‑platform `KayakFit.spec`. Run the build
**on the OS you’re targeting** (build on a Mac for the Mac app, on Windows for the
Windows app).

PyInstaller lives in the `build` dependency-group, so install it first with
`uv sync --group build`, then run the build with `uv run`.

**macOS**

```bash
uv sync --group build
./pyinstaller_build.sh          # or: uv run pyinstaller KayakFit.spec --distpath dist-mac --workpath build-mac
# Result: dist-mac/KayakFit.app
```

**Windows**

```bat
uv sync --group build
pyinstaller_build.bat           :: or: uv run pyinstaller KayakFit.spec --distpath dist-win --workpath build-win
:: Result: dist-win\KayakFit.exe (single-file executable)
```

Each platform builds into its own `dist-mac`/`dist-win` (and `build-mac`/`build-win`)
folder. This matters if the project directory is shared between a Mac and a Windows
machine (e.g. a mapped network drive): a macOS `.app` bundle contains Unix symlinks
that Windows can't delete, so a shared `dist/`/`build/` folder can break the other
platform's clean step.

The spec bundles the app icon, the `presets/` training plans, and the required
data files for bleak, customtkinter, fit‑tool and keyring. (Strava is reached
through a small `requests`-based client, so no third-party Strava library is
bundled.)

---

## Development

The tests are plain-`assert` `pytest` tests:

```bash
uv run pytest
```

Or run a single suite while working on it:

```bash
uv run pytest tests/test_segmentation.py   # auto‑pause / lap detection
```

The suites cover: `test_pipeline` (CSV → FIT conversion + config validation),
`test_program` (training‑program engine), `test_program_laps` (program‑step →
lap reconciliation), `test_segmentation` (auto‑pause / lap detection),
`test_fit_events` (FIT start/stop event timeline), `test_read_csv` (CSV reader),
`test_hr_zones` (heart‑rate zones), `test_recovery` (crash‑recovery marker),
`test_ble_parsing` (ergometer packet framing), `test_summary` (workout summary,
incl. FIT/summary endpoint agreement), `test_stats` (shared avg/max metric
aggregation) and `test_strava` (Strava REST client).

**Lint and type checks.** Contributions to `main` must also pass `ruff`
(lint + import order) and `mypy` (static types) with zero errors — CI enforces
both, so run them before opening a pull request (both are in the `dev` group
installed by `uv sync`):

```bash
uv run ruff check .
uv run mypy . --strict
```

See `CONTRIBUTING.md` for the full workflow (branching, PRs, releases).

Repository documentation is split by purpose:

- [`docs/AGENTS.md`](docs/AGENTS.md) — instructions for AI assistants.
- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — runtime and data flow.
- [`docs/DATA_TRANSFORMATIONS.md`](docs/DATA_TRANSFORMATIONS.md) — sensor-to-summary,
  FIT, and Strava calculations with worked examples.
- [`docs/FEATURES.md`](docs/FEATURES.md) — implemented user-facing features.
- [`docs/ROADMAP.md`](docs/ROADMAP.md) — prioritized open findings.
- [`docs/DECISIONS.md`](docs/DECISIONS.md) — architecture decisions and invariants.
- [`docs/STYLE_GUIDE.md`](docs/STYLE_GUIDE.md) — coding and test conventions.

**Project layout**

```
app/     device I/O (BLE), CSV/FIT, Strava, programs, session logic
gui/     the customtkinter interface
tests/   unit/integration tests
presets/ example training programs (JSON), copied to ~/KayakFit/programs on first run
assets/  icons
docs/    assistant instructions, architecture, features, roadmap, decisions, and style
kayakfit_gui.py   application entry point
KayakFit.spec     PyInstaller build spec
```

---

## Troubleshooting

- **No devices found when scanning** — check that the ergometer device name is
  entered exactly as printed on the machine, make sure Bluetooth is on, the
  ergometer is powered and nearby, and (macOS) that KayakFit has Bluetooth
  permission. Use **Settings → Test connection** to verify a device is reachable.
- **Workout won’t start / HRM not found** — if a configured strap can’t be
  reached you’ll be asked whether to continue without heart rate. If you don’t
  use a strap at all, turn off **Use heart‑rate monitor** in Settings →
  Devices and the question goes away for good.
- **Strava upload fails** — check that your API app’s callback domain is
  `127.0.0.1`, and that KayakFit shows *Strava · connected* in the status bar.
- **A workout was interrupted** — on the next launch KayakFit offers to recover
  and export the unfinished session; the CSV is always preserved in Workouts.
