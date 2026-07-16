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

The shipped default is `L = 0.623 m`, fitted against manually logged display
values (2026-07-03 BULL session, 59 paired readings): RMSE 1.1 W, R² 0.89,
98% of points within ±5 W. The same `L` also reproduces the display's session
*Average Power*, which the firmware computes as
`L × avg_force × avg_cadence / 60` (product of cumulative means).

Notes on expected small deviations (see `app/summary.py`):

- KayakFit's session average power is the time-average of per-second estimates
  (mean of products); the machine display uses the product of means, which
  reads a few watts higher when force and cadence co-vary.
- The display's "Max Power" is a sub-second within-stroke peak (~2.2× the
  mean) that a 1 Hz log cannot reproduce; KayakFit's max power is the highest
  one-second estimate.

## Refitting for another machine or firmware

If your machine or firmware reads differently, refit `L`:

1. Record a workout with KayakFit while noting the machine display's power at
   a range of intensities (easy, steady, hard) — the more paired readings, the
   better; vary cadence and force independently if you can.
2. For each paired reading, compute a length estimate from the model:
   `L_i = 60 × display_power_W / (pull_force_N × cadence_spm)`. Use the
   stroke-averaged CSV fields (`pull_force__n`, `cadence__spm`) — the
   instantaneous fields are single-stroke snapshots with ~25× the noise.
3. Fit a single `L` across all points (a least-squares fit of display power
   against `force × cadence / 60`, or simply the mean of the `L_i` values if
   they are tight). Sanity-check the residuals: RMSE within a few watts means
   the model holds for your firmware.
4. Set the result under `pull_length_m` in `~/KayakFit/config.yml`. Values
   outside the sanity range **0.3–1.2 m** are treated as a config mistake and
   use the calibrated default (see `normalize_pull_length` in `app/power.py`).

The value is passed explicitly through live, export, and summary processing to
the same `app.power.estimate_power`, so the on-screen watts, summary, and
exported FIT agree without process-global state.
