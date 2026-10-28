# poc/ — Anker X1 1-hour TOU plan writer

Reference client for the Anker SOLIX X1 family. It reads your live
Time-of-Use rate plan, rewrites only the period boundaries, writes it back, and
verifies by re-reading.

**Read [`../REPORT.md`](../REPORT.md) first** — it explains the protocol. The
1-hour step is a **device execution limit**: the cloud accepts `:30`, but the
X1/D156 applies the previous whole hour. See
[`../docs/FIRMWARE-LIMITS.md`](../docs/FIRMWARE-LIMITS.md).

## Design constraints (non-negotiable)

This tool is a replacement for **one screen**, not a new energy controller.

- **Target-driven only.** Write the Time-of-Use rate plan and keep the system in Time of Use mode.
  That is the controller that ramps and modulates to reach a target by the end of the valley window.
- **Never the Dynamic Tariff scheduler.** No `save_dynamic_price`, no `charging_hes_dynamic_price_svc/*`,
  no mode flip — that path saturates at maximum and bypasses load-aware limiting.
- **Never send a charge power.** The TOU payload has no power field; the EMS derives the rate.
- **Change only `start_time_period` / `end_time_period`.** `assert_times_only()` compares the proposed
  payload with the live plan and refuses any other difference (mode, prices, `peak_type`, `day_type`,
  seasons, `energy_unit`).
- **Mirror the existing TOU editor, with 1-hour steps** — 24 slots, matching what the device
  actually executes.
- **Do not emit `:30`.** The POC now rejects non-hourly boundaries; switching schedulers to get
  finer granularity is forbidden.

## Install

```sh
python3 -m pip install --user requests cryptography
```

## 1. Prove the crypto works offline (no network)

```sh
python3 anker_tou.py --selftest
```

This exercises the same P-256 ECDH + AES-256-CBC/PKCS7 path the login uses,
between two locally generated keys. It must print `selftest ok`.

## 2. Read-only dump (zero risk)

```sh
export ANKER_PASSWORD='…'      # or omit and get prompted
python3 anker_tou.py --email you@example.com --country GB --dump
```

It logs in, lists sites and devices, fetches the plan, prints it and saves a
snapshot to `captures/plan-<ts>-read.json`. Nothing is written.

**Check the output before going further:**
- Product code should be an inverter SKU from the X1 family.
- Rows should show `start_time_period` / `end_time_period` as `HH:mm`.
- If instead you see integer `start_time: 0..24`, stop — that shape cannot
  express an hourly boundary either, and the tool will refuse to force it.

## 3. Dry run

```sh
python3 anker_tou.py --email you@example.com --country GB \
  --from captures/plan-<ts>-read.json \
  --valley 23:00-05:00 --peak 05:00-23:00
```

Prints the rewritten plan and saves it as a `dryrun` snapshot. **No request is
sent** without `--apply`.

## 4. One controlled write

```sh
python3 anker_tou.py --email you@example.com --country GB \
  --from captures/plan-<ts>-read.json \
  --valley 23:00-05:00 --peak 05:00-23:00 --apply
```

What happens:
1. A `before-write` snapshot is saved.
2. The plan is POSTed to `charging_hes_svc/update_hes_utility_rate_plan`.
3. The plan is re-read and compared field-by-field.
4. **On mismatch it automatically restores the snapshot** and exits `2` — that
   is your signal that the cloud or firmware changed a boundary.

## 5. Rollback

```sh
python3 anker_tou.py --email you@example.com --country GB \
  --rollback captures/plan-<ts>-before-write.json --apply
```

## Safety notes

- **Do not use Dynamic Tariff to get sub-hour slots.** Anker support suggests it, and it does
  accept 23:30 — but on this installation it charged at a flat 6 kW with ~2 kW of backup load,
  pulling a bit over 8 kW through a 32 A breaker until it tripped (Anker case #<case-ref>). The TOU path
  modulates charge power against the load; Dynamic Tariff does not. This tool refuses that endpoint
  unless you override it.
- **Set a grid import / charge power limit below your breaker rating before the first write.**
  Then a bad schedule trips a limit, not the breaker.
- **Verify the first cycle with current, not timestamps.** "Charging started at 23:00" is not
  enough; confirm the power stayed within the circuit rating while backup load was present.
- The write path enforces **times-only** changes structurally; a payload that would change mode or
  prices is rejected before it leaves the machine.
- Writes are limited to the rate-plan endpoint. This file cannot reach OTA,
  mode, SOC, cutoff, or Modbus endpoints — there is no code path for them.
- `cmd` is never guessed. If your firmware needs a `device_command` with a
  specific `cmd`, capture the app's own request (REPORT.md §8) and add it
  deliberately.
- One change at a time. Verify a manual write for a few days before scheduling
  anything.
- Don't run this during a storm, an outage, or a firmware update.
- Snapshots contain your site id and serial — keep them out of git.

## Known limitations

- The X1 **write** path is assembled from static analysis; it is not
  device-tested by the author. Read-only calls are the safe, well-understood
  part; treat the first write as an experiment with a rollback ready.
- Endpoint names and field shapes can change with app releases.
- Region must match the account, or you will see zero sites.
