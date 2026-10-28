# One-hour scheduling is the practical D156/X1 limit

**Status:** empirical finding on a live X1-H (A5102) with the D156 firmware, October 2026.
The cloud accepts `HH:30`, but the device applies whole hours.

## What we observed

A schedule was written and verified in the cloud:

```
00:00–05:30   Super Off-Peak
05:30–23:30   Peak
23:30–24:00   Super Off-Peak
```

The Anker app and `get_utility_rate_plans` both returned **23:30** and **05:30**. The
inverter, however, changed mode at:

```
23:00   Peak → Super Off-Peak
05:00   Super Off-Peak → Peak
```

In other words it **floored** both boundaries to the previous whole hour.

## What this means

| Layer | Precision |
|---|---|
| Android app UI | 30 minutes |
| Anker cloud storage | exact string, including `:30` |
| Device execution | **whole hours** (observed: floor) |

The server is not silently rounding: a read-back immediately after the write returns the
half-hour values. The mismatch appears only when the device acts on the plan.

This is why saving a `23:30` schedule from this project produced the same result as the
Android app’s “30-minute” UI: the device ignored the extra half hour.

## Project behaviour after this finding

- The editor now works in **24 one-hour slots**.
- The server **rejects** any plan containing `:30` (`422`, `kind: "hourly"`), so older clients
  cannot accidentally reintroduce the mismatch.
- Existing `:30` plans are **floored to the hour when loaded**, matching device behaviour:
  `23:30 → 23:00`, `05:30 → 05:00`.
- A saved plan contains only `HH:00` boundaries (`24:00` for the day end).
- The API schema is unchanged; the cloud can still store `:30` if another client writes it.
- The web app no longer claims to do something the Android app cannot.

## Why this is not a bug in the write path

The exact Android `device_command` body was captured and the cloud read-back was verified
(see [`REPORT.md`](../REPORT.md) §11–§15). The plan reaches the cloud correctly. The remaining quantisation
is inside the inverter/EMS firmware or its device-facing schedule parser, not in this
project and not fixable by changing JSON fields we have seen so far.

## Reproducing / watching for a firmware fix

1. Write a plan with a `:30` boundary via the API (not this UI, which is hourly).
2. Read it back with `get_utility_rate_plans` and `get_device_command`; both should show `:30`.
3. Watch the inverter’s actual mode transition, or the live `bs`/`es` state, around the
   boundary. If it changes at `:30`, the firmware has gained half-hour support and the UI
   can be widened back.
4. Record the firmware version and date. Do not assume a newer build behaves the same.

## Practical guidance

For a fuse-safe, predictable schedule, use whole hours. The app’s 30-minute selector is
cosmetic on this hardware; this project now reflects the real constraint.
