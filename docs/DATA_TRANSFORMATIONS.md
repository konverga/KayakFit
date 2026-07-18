# From recorded points to Summary, FIT, and Strava

This document is the reproducible processing contract for KayakFit workouts.
An end user can apply these rules to `workout.csv`, plus `steps.json` for a
planned workout, and obtain the same timing, distance, lap, and speed values as
the app summary and generated FIT file.

## What is guaranteed

KayakFit calculates the summary and FIT session/lap messages from one finalized
workout and one aggregate result. Within KayakFit:

- session active distance equals the sum of lap active distances;
- session active time equals the sum of lap active times;
- paused intervals never lower speed, heart-rate, cadence, or power averages;
- average speed is no greater than maximum speed when speed is available;
- chart, summary, and FIT use recorded instantaneous speed;
- main-window sensor cards consistently use averaged device fields;
- decoded FIT session/lap metrics match the summary, subject to FIT precision.

Strava is outside this guarantee. It can recalculate moving time, distance, and
speed after import.

## The complete path

```text
KayakFirst + HR monitor
        |
        v
workout.csv + metadata.json (+ steps.json for a plan)
        |
        v
FinalizedWorkout
  canonical sample windows, activity runs, distance, laps
        |
        +-----------------------+
        |                       |
        v                       v
FinalizedMetrics          active record series
  session + laps            speed, HR, cadence, power
        |                       |
        +-----------+-----------+
                    |
             +------+------+
             |             |
             v             v
       app summary      generated FIT -> Strava
```

The CSV is the durable observation log. It retains inactive and repeated rows.
Finalization, not CSV reading, decides which intervals are active.

## 1. Canonical elapsed timeline

The CSV stores the processing clock explicitly:

- `session_elapsed__s`: monotonic sample endpoint since session start;
- `sample_duration__s`: non-overlapping duration represented by the row;
- `timestamp`: epoch milliseconds at that same endpoint.

For rows numbered `i = 0..n-1`:

```text
interval i = [session_elapsed[i] - sample_duration[i], session_elapsed[i]]
elapsed workout time = session_elapsed[n-1]
```

Endpoints must be finite, non-negative, and non-decreasing. Durations must be
finite, non-negative, no greater than their endpoint, and must not overlap the
preceding row endpoint. This permits an initial or repeated zero-duration
observation without inventing time. All rows must share one epoch origin:

```text
timeline epoch start = timestamp[i] - session_elapsed[i] * 1000
```

`elapsed_time__s`, `window_size__s`, `kayakfirst_timestamp`, and other raw
device fields remain audit data only. There is no processing fallback.

At capture, `RecordingTimeline` advances normally by the raw elapsed-counter
delta. A stalled or reset counter rebases the endpoint to monotonic receipt
time. It clips the raw window to the time available after the preceding
endpoint. Any uncovered interval is therefore explicit pause time instead of
being assigned to the next sample.

The complete recording bounds, including an inactive prefix or tail, define the
FIT session and chart time axis.

## 2. Active and elapsed time

`active_paddling` is required and authoritative:

- `1`: the complete row interval is active;
- `0`: the complete row interval is inactive;
- missing or invalid: reject the workout instead of inferring from another
  sensor.

```text
active time = sum(sample_duration of intervals with active_paddling = 1)
elapsed time = final session_elapsed__s
pause time = elapsed time - active time
```

Heart-rate notifications never create records or extend time.

Every exact `0 -> 1` transition begins a new active run. Every exact `1 -> 0`
transition closes it. An uncovered interval also closes a run, even when the
next row is active. There is no debounce or configurable gap threshold. The
same rules drive the live pause indicator, free laps, chart breaks, and FIT
timer events. Zero-duration observations belong to no active run.

## 3. Active distance

`distance__m` is the recorded cumulative odometer. Processing is deliberately
simple and never rewrites it using speed:

1. Keep the latest valid odometer value as the baseline.
2. For an active interval, credit `max(current - baseline, 0)` metres.
3. For an inactive interval, advance the baseline but credit zero metres.
4. Credit the complete raw delta on the first active record after a pause. Do
   not cap it using instantaneous speed.
5. If an active odometer reading is missing, defer the span to the next valid
   reading and distribute that delta over the pending active intervals by
   duration.
6. Round the session to centimetres and allocate lap centimetres so lap
   distances sum exactly to the session distance.

This preserves what was actually recorded. It also means an odometer jump at
resume can disagree with the speed channel; the app does not invent a value to
hide that disagreement.

## 4. Speed

`speed_instant__mps` is the canonical finalized speed channel, independent of
the cumulative odometer:

```text
lap average speed = sum(speed[i] * active overlap seconds[i])
                    / sum(active overlap seconds[i])

session average speed = the same calculation over all active intervals

maximum speed = largest valid active instantaneous-speed record
```

Zero is a valid active speed. Missing, non-finite, negative, or FIT-incompatible
speed readings are excluded. If the recorded speed channel has no valid values,
both its average and maximum stay unavailable; it is not silently replaced by
odometer-derived speed.

Finalized metric sources are fixed: speed always uses the retained
instantaneous column. There is no processing-mode setting.

The main workout dashboard is a live presentation surface, not a finalized
workout aggregate. Its speed, pace, cadence, pull-force, and power cards use the
ergometer's averaged fields. Each card's secondary average/max is accumulated
from the same field as its primary value. This does not change the recorded
instantaneous speed used by the graph, summary, or FIT file.

### Why speed is not distance / active time

The supplied recording `workout_20260717_210142` demonstrates that the two raw
channels can disagree. Its lap distances and active times imply:

```text
47 m / 17 s = 9.95 km/h
40 m / 13 s = 11.08 km/h
30 m /  9 s = 12.00 km/h
```

Those values cannot coexist with the recorded `10.22 km/h` overall maximum or
the requested lap averages near `8.77`, `9.25`, and `8.96 km/h`. KayakFit keeps
both source measurements honest: odometer for distance, instantaneous sensor
for speed. Therefore `lap speed = lap distance / lap duration` is intentionally
not an invariant for recorded workouts.

The second lap's recorded values produce `9.27 km/h`, not `9.25 km/h`. No
reproducible calculation over the saved two-decimal speed samples produces
`9.25 km/h` without adding an undocumented correction.

### Chart smoothing

The graph applies a five-record centered moving average for readability. This
is presentation-only:

- FIT records retain unsmoothed instantaneous speed;
- session/lap average and maximum values use unsmoothed records;
- smoothing restarts at every active run;
- the raw maximum point is restored after smoothing and included in the y-axis
  bounds, so the maximum shown in the card is visible and hoverable.

## 5. Other active averages

Only active interval overlaps participate in heart-rate, cadence, and estimated
power aggregates.

- Missing or zero readings mean no valid signal and are excluded.
- Averages are weighted by active overlap seconds.
- Maximum is the largest valid active reading.
- Missing-HR active time remains visible as `No HR signal` in the zone chart.

Power is estimated per record:

```text
power W = pull force N * configured pull length m * cadence spm / 60
```

Cadence and pull force always use the ergometer's stroke-averaged fields.
Estimated power uses those same fields because that is the calibrated model.
The saved pull length makes historical power processing reproducible; no
metric-source preference is needed.

## 6. Free-workout laps

Each exact contiguous active run is one free-workout lap and one FIT timer run.
Inactive rows are not lap records and do not enter averages. A pause appears as
the gap between one lap end and the next lap start. An inactive recording prefix
or tail remains part of session elapsed time and the graph bounds.

For `workout_20260717_210142`, applying the rules above gives:

| Lap | Start | Active/elapsed | Distance | Average speed |
|---|---:|---:|---:|---:|
| 1 | 0 s | 17 s | 47 m | 8.77 km/h |
| 2 | 24 s | 13 s | 40 m | 9.27 km/h |
| 3 | 45 s | 9 s | 30 m | 8.96 km/h |

```text
session elapsed time = 62 s
session active time = 39 s
session pause time = 23 s
session active distance = 117 m
session maximum speed = 10.22 km/h
```

The app summary and a decoded regenerated FIT contain those same values.

## 7. Planned-workout laps

For planned workouts, performed boundaries in `steps.json` define laps. New
sidecars contain only `index`, `type`, `label`, `start_elapsed_s`, and
`end_elapsed_s`. Bounds use canonical session elapsed time. Steps must start at
zero, be consecutive, and cover the complete captured session; epoch bounds
and raw device elapsed bounds are unsupported.

- Time advances through warm-up, work, rest, and cool-down using elapsed time.
- `active_paddling` still selects active overlap inside every step.
- A person who paddles slowly through rest contributes active distance and
  metrics to the rest lap.
- A person who stops during warm-up, rest, or cool-down contributes elapsed but
  not active time for that interval.
- A fully inactive step remains visible with elapsed time, zero active time and
  distance, and blank active statistics.
- A record interval crossing a step boundary is split proportionally by overlap
  for time, distance, and weighted metrics. The FIT record itself is emitted
  only once.

The final recorded step owns the captured timeline tail so a naturally
completed plan conserves all recorded active time and distance.

## 8. FIT construction

The generated FIT contains:

- timer `START`/`STOP_ALL` events around every exact active run;
- one sensor record per positive-duration active CSV window;
- active-only cumulative distance;
- unsmoothed recorded instantaneous speed;
- valid active HR, cadence, and estimated power;
- exact-run free laps or performed-step planned laps;
- complete session elapsed time and active timer time;
- lap/session distance, averages, maxima, and one activity summary.

Summary and FIT consume the same finalized objects and metrics, so the viewer
does not recalculate encoded values independently.

## 9. Synthetic GPS and Strava

Strava does not always honor FIT timer fields for indoor kayaking. KayakFit maps
active-only cumulative distance onto a fixed two-kilometre out-and-back line at
Hazewinkel. Each active run starts with a metric-free position anchor at its
previous active distance.

No movement record is emitted during a pause, and idle odometer changes cannot
move the synthetic route. This route is compatibility data, not a claim that
the indoor ergometer measured GPS coordinates.

Strava may still recalculate distance, moving time, speed, rounding, or lap
presentation. To audit KayakFit itself, compare the summary with decoded FIT
session and lap messages; those are regression-tested for parity.
