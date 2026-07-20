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
- chart, summary, and FIT use recorded instantaneous sensor points, including
  estimated power derived from instantaneous cadence and pull force;
- main-window sensor cards consistently use instantaneous device fields;
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
next row is active. Exact runs drive active time, metrics, distance attribution,
and FIT timer events. Zero-duration observations belong to no active run.

Free-workout presentation applies a fixed five-second meaningful-pause delay.
The live pause indicator, lap counter, free laps, and free-workout chart breaks
only split after at least five continuous canonical seconds without activity.
Shorter gaps remain inside one lap but their inactive samples still contribute
neither active time nor metric averages. The threshold uses elapsed duration,
not record count, and is not configurable. Planned laps continue to follow the
performed program steps.

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
workout aggregate. Its speed, pace, cadence, and pull-force cards use only the
ergometer's instantaneous fields; live estimated power uses instantaneous force
and cadence. Each card's secondary average/max and planned-target feedback use
the same instantaneous sources as the primary value. There is no fallback to an
averaged field. Sensor-card headings are explicitly labelled `Live` so these
values are not mistaken for finalized statistics. Finalized power uses those
same instantaneous inputs.

### Why speed is not distance / active time

The cumulative odometer and instantaneous speed are independent sensor
channels. Firmware behavior, sampling boundaries, and odometer changes around
pauses can make `distance / active time` differ from the time-weighted sensor
speed. KayakFit preserves both observations: the odometer owns distance and the
instantaneous speed channel owns speed. Therefore
`lap speed = lap distance / lap active time` is intentionally not an invariant.

### Chart points

The graph and hover readouts show the raw active sensor points without a moving
average. For long workouts, the Canvas selects real recorded boundary and
extreme points to fit the available pixels; it never replaces them with
calculated averages. FIT records and session/lap aggregates use the same
unsmoothed source points. Every time-axis point uses the row's canonical CSV
sample endpoint, which is also the timestamp written to its FIT sensor record.

## 5. Other active averages

Only active interval overlaps participate in heart-rate, cadence, and estimated
power aggregates. Summary/FIT cadence and the summary pull-force chart use the
recorded instantaneous protocol fields. Heart rate has one recorded source.

- Missing or zero readings mean no valid signal and are excluded.
- Averages are weighted by active overlap seconds.
- Maximum is the largest valid active reading.
- Missing-HR active time remains visible as `No HR signal` in the zone chart.

Power is estimated at every retained record:

```text
power W = pull force N * configured pull length m * cadence spm / 60
```

Cadence and pull force use `cadence_instant__spm` and
`pull_force_instant__n` from the same ergometer packet. Lap/session average
power is the active-time-weighted mean of those per-record estimates, not the
product of whole-lap/session averages. Maximum power is the largest valid
instantaneous-point estimate. The saved pull length makes historical power
processing reproducible; no metric-source preference is needed.

## 6. Free-workout laps

Each exact contiguous active run remains one FIT timer run. For presentation,
consecutive runs separated by less than five inactive seconds are grouped into
one free-workout lap. A gap of five seconds or more starts a new lap when
activity resumes. Inactive rows are not lap records and do not enter averages;
a grouped lap can therefore have more elapsed time than active time without its
averages being lowered. An inactive recording prefix or tail remains part of
session elapsed time and the graph bounds.

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
- unsmoothed recorded instantaneous speed and cadence;
- valid active HR and estimated power derived from instantaneous cadence/force;
- meaningful-pause free laps or performed-step planned laps;
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
