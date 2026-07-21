# Power model and `pull_length_m` calibration

The KayakFirst ergometer has no power sensor. Its display derives power from
flywheel-based pull force and stroke rate, and KayakFit reproduces that model
(see `app/power.py`, the single definition used by the live dashboard, the
summary window and the FIT exporter):

```
power (W) = pull_force (N) × pull_length (m) × cadence (spm) / 60
```

Physics: work per stroke = force × pull distance (joules); strokes per second
= cadence / 60; power = joules per second = watts. The only free parameter is
the **effective handle pull length** `L`.

## Current calibration

The shipped default is `L = 0.600 m`. Every live, chart, summary, and FIT power
value uses `pull_force_instant__n` and `cadence_instant__spm` from the same
recorded packet. This is 3.7% lower than the former 0.623 m calibration and
keeps power aligned with the other instantaneous sensor channels.

Future workouts use the configured pull length, which defaults to 0.600 m.
Historical workouts continue to use the pull length saved in their
`metadata.json`.

KayakFit's session/lap average is the represented-time average of the positive
per-record estimates. Its maximum is the highest recorded-point estimate. The
ergometer display can use a different aggregation or a sub-second within-stroke
peak, so its average and maximum need not match KayakFit exactly.

## Refitting for another machine or firmware

If your machine or firmware reads differently, refit `L`:

1. Record a workout with KayakFit while noting the machine display's power at
   a range of intensities (easy, steady, hard) — the more paired readings, the
   better; vary cadence and force independently if you can.
2. For each paired reading, compute a length estimate from the model:
   `L_i = 60 × display_power_W / (pull_force_N × cadence_spm)`. Use the
   instantaneous CSV fields (`pull_force_instant__n`,
   `cadence_instant__spm`) from that same moment.
3. Fit a single `L` across all points (a least-squares fit of display power
   against `force × cadence / 60`, or simply the mean of the `L_i` values if
   they are tight). Sanity-check the residuals: RMSE within a few watts means
   the model holds for your firmware.
4. Set the result under `pull_length_m` in `~/KayakFit/config.yml`. Values
   outside the sanity range **0.3–1.2 m** are treated as a config mistake and
   use the calibrated default (see `normalize_pull_length` in `app/power.py`).

The value is passed explicitly through live, export, and summary processing to
the same `app.power.estimate_power` formula without process-global state. All
surfaces supply instantaneous force and cadence.
