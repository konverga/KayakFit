# From sensor readings to Summary, FIT, and Strava

This document explains what KayakFit records, which transformations it applies,
and why the app summary, generated FIT file, and Strava can sometimes look
slightly different.

## What is guaranteed

KayakFit calculates the workout summary and the FIT session/lap messages from
the same finalized workout and the same aggregate values. Within KayakFit:

- session active distance equals the sum of lap active distances;
- session active time equals the sum of lap active times;
- session elapsed time equals lap elapsed time plus explicit gaps after laps;
- lap average speed is `lap distance / lap active time`;
- session average speed is `session distance / session active time`;
- session average speed is therefore the active-time-weighted lap average;
- paused time never lowers speed, heart-rate, cadence, or power averages;
- every reported average is less than or equal to its reported maximum;
- the summary and decoded FIT session/lap fields use the same values, subject
  only to the FIT format's normal field precision.

Strava is outside this guarantee. It receives the coherent FIT file, but it can
recalculate moving time, distance, speed, and other presentation values with its
own algorithms.

## The complete path

```text
KayakFirst + HR monitor
        |
        v
raw BLE packet
        |
        v
workout.csv + metadata.json (+ steps.json for a plan)
        |
        v
validated records selected with the saved workout settings
        |
        v
FinalizedWorkout
  active windows, pauses, active distance, laps, active/elapsed time
        |
        +------------------------+
        |                        |
        v                        v
FinalizedMetrics           active record series
  session + lap values       distance, speed, HR, cadence, power
        |                        |
        +-----------+------------+
                    |
             +------+------+
             |             |
             v             v
       app summary      generated FIT
                             |
                             v
                          Strava
```

The CSV remains the durable observation log. Reopening or re-exporting a
workout uses the processing settings saved beside that workout, not the user's
current settings.

## 1. Recorded sensor values

Each ergometer packet contains, among other fields:

- a packet timestamp;
- `window_size__s`: how many preceding seconds the packet represents;
- `active_paddling`: `1` for an active window, `0` for an idle window;
- cumulative distance;
- averaged and instantaneous speed, cadence, and pull force;
- ergometer elapsed time.

The latest fresh heart-rate reading is attached to the ergometer packet. A
stale or missing HR signal is stored as no reading rather than repeating an old
value indefinitely.

The packet timestamp marks the **end** of its represented window. A packet at
`12:00:05` with `window_size__s = 2` represents `12:00:03` through `12:00:05`.

## 2. Choosing active records

`active_paddling` is authoritative:

- positive value: the represented window belongs to the activity;
- zero: it is pause/idle time and is not emitted as a FIT sensor record;
- missing or invalid: the workout is rejected rather than guessed from speed,
  cadence, distance, or heart rate.

This produces three time values:

```text
active time  = sum of active sample-window durations
elapsed time = first active-window start to final active-window end
pause time   = elapsed time - active time
```

## 3. Active distance

The ergometer distance is cumulative and can still change while the athlete is
idle. KayakFit converts it to active-only distance:

1. During an active window, the increase from the previous valid odometer
   reading is considered for the activity.
2. During an idle window, the odometer baseline advances, but the increase is
   credited to no lap and no session distance.
3. The first packet after a pause can contain a coarse odometer jump. KayakFit
   caps the credited distance to:

   ```text
   instantaneous sensor speed x represented active duration
   ```

   The instantaneous speed is used only as this resume sanity limit. Reported
   average, maximum, chart, and FIT speed come from active distance.
4. If an active odometer reading is missing, its duration waits for the next
   valid reading. The later distance increase is spread over the combined
   active duration instead of producing a one-packet spike.
5. Session distance is rounded to centimetres. Lap centimetres are allocated so
   the displayed/FIT lap distances sum exactly to the session distance.

## 4. Speed

KayakFit uses one canonical speed channel:

```text
record speed  = active distance attributed to the record
                / represented active duration

lap average   = lap active distance / lap active time

session average = session active distance / session active time

maximum speed = largest canonical record speed
```

The selected instant/averaged sensor display mode does not change finalized
speed. It only affects sensor metrics such as cadence and pull force; the
instantaneous speed remains available for the resume-distance sanity cap.

### Chart smoothing

The summary chart applies a five-record centered moving average for readability.
That smoothing changes only the drawn line and preserves the raw maximum point
used in the summary cards:

- FIT record speed remains the unsmoothed canonical speed;
- summary/FIT maximum speed remains the raw canonical record peak;
- the highest visible/hovered chart point reaches the same maximum shown in the
  summary card.

This is not a different calculation of the workout. It is a presentation filter
over the same raw distance-derived series.

### Centimetre boundary edge

A planned-step boundary can split one sensor window between two laps. FIT lap
distance has centimetre precision, so the last centimetre must belong to one of
the two laps. In the rare case where that makes a tiny lap's distance/time
average exceed its raw record peak, that lap maximum is raised to its average.
The session maximum is raised with it. This preserves `average <= maximum`
without changing distance, time, or average speed.

## 5. Heart rate, cadence, and estimated power

Only active records participate in these aggregates.

- Missing or zero readings mean “no valid signal” and are excluded.
- Averages are weighted by represented sample duration.
- Maximum is the largest valid active reading.
- Missing-HR active time remains visible as “No HR signal” in the zone chart.

Power is estimated per record:

```text
power W = pull force N x configured pull length m x cadence spm / 60
```

The saved pull-length calibration and selected instant/averaged sensor mode are
reused when the workout is reopened or exported later.

## 6. Free-workout laps

Every exact active run gets its own FIT timer `START` and `STOP_ALL` events.
Short activity-flag interruptions can remain inside one user-facing lap; a
longer pause or timestamp gap starts another free-workout lap.

Inactive records never enter lap distance or averages. A brief pause inside a
lap contributes to its elapsed time but not its active time. A long pause
between laps appears as the previous lap's “Gap after” in the app summary.

### Free-workout example

Assume one-second sensor windows and the default five-idle-record lap threshold:

| Window | Active | Raw cumulative distance | Instant speed | Credited distance |
|---|---:|---:|---:|---:|
| 1 | yes | 3 m | 3 m/s | 3 m |
| 2 | yes | 6 m | 3 m/s | 3 m |
| 3-7 | no | rises from 7 to 11 m | 0 | 0 m |
| 8 | yes | jumps to 16 m | 3 m/s | 3 m, not 5 m |
| 9 | yes | 19 m | 3 m/s | 3 m |

Result:

```text
Lap 1:  6 m / 2 active s = 3.0 m/s = 10.8 km/h
Gap:    5 elapsed s, no active time or distance
Lap 2:  6 m / 2 active s = 3.0 m/s = 10.8 km/h

Session distance = 6 + 6 = 12 m
Session active time = 2 + 2 = 4 s
Session elapsed time = 2 + 5 + 2 = 9 s
Session pause time = 9 - 4 = 5 s
Session average speed = 12 / 4 = 3.0 m/s = 10.8 km/h
```

The raw four metres added while idle are ignored. The resume jump is capped to
`3 m/s x 1 s = 3 m`.

FIT contains two active timer runs, two free laps, session distance `12 m`,
timer time `4 s`, elapsed time `9 s`, and average speed `3.0 m/s`.

## 7. Planned-workout laps

For a planned workout, performed step boundaries from `steps.json` define the
laps. The plan does not make an idle packet active:

- every performed warm-up/work/rest/cool-down step remains a lap;
- active records are intersected with each step's exact time interval;
- a fully inactive rest lap has elapsed time but zero active time and distance;
- pause time never lowers work or session averages.

### Planned-workout example

```text
Work A: 2 elapsed s, both active, 6 m
Rest:   2 elapsed s, both idle,   0 m
Work B: 2 elapsed s, both active, 8 m
```

Result:

| Lap | Elapsed | Active | Distance | Average speed |
|---|---:|---:|---:|---:|
| Work A | 2 s | 2 s | 6 m | 3.0 m/s = 10.8 km/h |
| Rest | 2 s | 0 s | 0 m | — |
| Work B | 2 s | 2 s | 8 m | 4.0 m/s = 14.4 km/h |

```text
Session distance = 6 + 0 + 8 = 14 m
Session active time = 2 + 0 + 2 = 4 s
Session elapsed time = 2 + 2 + 2 = 6 s
Session pause time = 6 - 4 = 2 s
Session average speed = 14 / 4 = 3.5 m/s = 12.6 km/h
```

The same result is obtained as the active-time-weighted lap average:

```text
(3.0 m/s x 2 s + 4.0 m/s x 2 s) / 4 s = 3.5 m/s
```

FIT contains two active timer runs but three lap messages, including the empty
rest lap.

### One packet crossing a step boundary

If one two-second active packet covers `8 m` and a step boundary falls exactly
halfway through it:

```text
Step A receives 1 s and 4 m
Step B receives 1 s and 4 m
```

HR, cadence, and power averages receive the same one-second weighting in each
step. The sensor packet is still emitted only once in the FIT record stream.

## 8. FIT construction

The generated FIT contains:

- timer `START`/`STOP_ALL` events around every exact active run;
- one sensor record per active CSV record;
- active-only cumulative distance;
- canonical unsmoothed record speed;
- valid active HR, cadence, and estimated power;
- free laps or planned-step laps;
- session and lap elapsed time, timer time, distance, averages, and maxima;
- one activity summary.

The summary screen consumes the same finalized lap objects and metrics before
FIT encoding, which is why its lap/session values match the generated file.

## 9. Synthetic GPS and Strava

Strava does not always honor FIT timer fields for indoor kayaking. It can derive
overview moving time from positions instead. KayakFit therefore maps active-only
cumulative distance onto a fixed two-kilometre out-and-back line at Hazewinkel.

At every active-run start, a metric-free GPS anchor is emitted at the previous
active distance. During a pause:

- no sensor/GPS movement records are emitted;
- the next run starts from the same active-distance position;
- idle odometer changes cannot move the synthetic route.

This gives Strava the same active-distance and pause structure as the FIT timer
events. The route is compatibility data, not a claim that the indoor ergometer
measured real GPS coordinates.

### Why Strava can still differ

KayakFit controls the uploaded FIT file, not Strava's processing. Strava may:

- recalculate distance from coordinates;
- recalculate moving time or average speed;
- apply its own rounding, smoothing, or outlier rules;
- omit or rename FIT lap details in its UI.

Small Strava differences therefore do not imply that the KayakFit summary and
FIT disagree. To audit KayakFit itself, compare the app summary with the decoded
FIT session and lap messages; those are regression-tested for parity.
