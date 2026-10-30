# Solar-aware battery ETA

*Technical note. This is an estimate for the wall display, not a charge controller.*

The dashboard already showed a linear “time to full” from the current battery rate. That
number is misleading when the battery is charging from PV: solar output falls through the
afternoon, so a rate sampled at 10:30 does not hold until sunset. This change adds a
solar-curve mode for the charging case and leaves the existing discharge and grid-charge
formulas untouched.

The model uses one live PV reading and the local time. It does **not** use location, weather,
season, panel orientation or historical generation. It is deliberately a rough estimate.

## When each formula applies

`B_w` is the signed ten-minute average battery power, positive while charging. `S_w` is
instantaneous PV, `G_w` grid import and `X_w` grid export. `t0` is the site-local decimal hour.

| # | Condition | Mode | ETA |
|---|---|---|---|
| 0 | Nominal capacity, SOC or reserve missing | `unavailable` | none |
| 1 | `B_w <= -50 W` | `discharging` | existing: `available_kwh × 0.95 / rate_kw` |
| 2 | `-50 W < B_w < 0` | `idle` | none |
| 3 | `B_w == 0`, SOC below 100 | `idle` | none |
| 4 | `B_w == 0`, SOC at 100 | `full` | `Full` |
| 5 | `B_w > 0`, `G_w > 50 W` | `grid` | existing: `deficit / (rate_kw × 0.95)` |
| 6 | `B_w > 0`, `7 < t0 < 18`, `S_w > 50 W`, `X_w <= 50 W` | `solar` | curve below |
| 7 | `B_w > 0`, anything else | `charging` | linear fallback from `B_w` |

The 50 W gates are **thresholds**, not measurements: a discharge below 50 W is treated as
idle so inverter noise does not produce a multi-day ETA. Grid input above 50 W wins over
solar, because the battery may be charging from the grid even when PV is present. Outside
07:00–18:00 the curve is not defined, so charging is linear again.

Only the estimate uses the 50 W idle gate. The battery card still follows the instantaneous
flow, so it can read “Discharging” while the ETA is hidden.

## The solar curve

The supplied model is a symmetric half-sine between 07:00 and 18:00, peaking at 12:30.

```
s(t)   = sin(pi * (t - 7) / 11)                 inside 07:00..18:00, else 0
P_peak = 0.5 * min(P_obs / s(t0), 2.5 * P_obs)
P(t)   = P_peak * s(t)                         W
E(u,v) = P_peak * 11 / (1000 * pi) *
         [cos(pi*(A-7)/11) - cos(pi*(B-7)/11)]  kWh
```

`A = max(7,u)`, `B = min(18,v)`. `P_obs` is instantaneous PV. The 2.5× cap is applied before
the 0.5 risk factor, matching the source model. `SOLAR_RISK_FACTOR` and
`SOLAR_MAX_PEAK_MULTIPLIER` are named constants in `app/solar_forecast.py`; there is no UI or
environment override.

## House load and inverter cap

The curve is gross PV. The battery only receives the surplus after the house:

```
L_kw = load_avg_kw
     or max(0, (S_w + G_w - X_w - max(0, B_w)) / 1000)
     or home_w / 1000
```

`load_avg_kw` is the same ten-minute household average the battery average uses. If it is
missing, the energy-balance fallback is approximate and labelled by the diagnostics.

The runtime charge cap `C_kw` clips the surplus. It is never a value in the formula:

```
P_batt(t) = min(C_kw, max(0, P_solar(t)/1000 - L_kw))

min(C, P-L) = max(0, P-L) - max(0, P-(L+C))

E_plus_capped(t0,t) = E_plus(t0,t; L_kw)
                    - E_plus(t0,t; L_kw + C_kw)
```

`E_plus(t0,t; X)` is the integral of `max(0, P_solar/1000 - X)`. The deficit integral
`E_minus` uses `L_kw` only, because the cap limits charging, not the house load.

### Cap source order

`C_kw` is resolved from the current snapshot on every refresh:

1. the latest realtime EMS field, if the frame carries one (`mcp`,
   `max_charge_power`, `ac_power_limit`, …);
2. the latest device-attribute value (`ac_power_limit`, `ac_input_power`,
   `maxChargingPower`, `gridChargingPower`) or the site power-limit response, read in
   the background;
3. **6.0 kW**, the X1’s documented full recharge rating.

A realtime value is kept for **5 minutes** if a later frame omits the field, then the device
value is used. The 6 kW fallback is a named constant, `FALLBACK_MAX_CHARGE_KW`. It is not a
claim that the inverter will accept 6 kW from PV; it is the best available ceiling when the
API exposes nothing.

The documented X1 EMS field map has **no max-charge field**; the Power Panel map does
(`mcp`). `LiveFeed.stats()` exposes `last_frame_keys` and `limits_error` so a real frame can
confirm whether a firmware adds one.

### Live finding: A5102 with 3× A5220 (2026-10-30)

A read-only probe of one real X1 feed captured eight `ems_second` frames. The complete key
set was:

```
b2ee b2ep b2gp b2lp bp bs d2bp d2lp dp eap eon es g2bp g2ee g2ep g2lp gp gs
lp n p2bp p2ee p2ep p2gp p2lp pp ps rgs smart_grid_state soc
```

There is no `mcp`, `max_charge_power`, `ac_power_limit`, `legal_power_limit` or equivalent.
A full-frame value check showed only power, energy, status, SOC and the extra
`eap`/`n`/`rgs`/`smart_grid_state` fields; none carried a 6 kW or 4.2 kW ceiling.

- `param_info`, `basic_info` and `state_info` produced no messages during a 45 s
  subscription; only `ems_second` arrived.
- `get_device_attrs` with `["ac_power_limit"]` returned `{"attributes": {}}` for this
  device. Adding `site_id` or more attribute names did not change it.
- `get_power_limit` returned empty data (`code=0`); `param_type 32` and the fallback
  attribute calls returned HTTP 400.

So on this firmware/account the X1 **does not send or expose a charge limit**. The runtime
cap is the named **6 kW fallback** unless the web app itself has just saved a plan, in which
case `plan-save` supplies the value it wrote. `live_stats.limits_error` records the failed
reads for diagnostics.

## Target and result

Solar mode uses the SOC-based target, not the legacy reserve-inclusive deficit:

```
Q_dc = nominal_kwh * max(0, 100 - soc) / 100
Q_ac = Q_dc / 0.95
```

If `E_plus_capped(t0, t2)` is at least `Q_ac`, we bisect for the time the battery reaches
100 % and report `~4h 39m · full 15:09`. Here `t2` is the afternoon time when solar falls
back to the house load.

If it is not enough, we integrate to 18:00 and report the SOC at sunset plus the end time,
for example `87% by 18:00`. The final SOC is clamped at the reserve, because the real device
would stop discharging there.

Grid and linear charging continue to use the legacy deficit, which includes the reserve.
That preserves the existing numbers (`8.68 h` in the regression fixture) and is intentional:
the user asked not to change those formulas.

## View and API fields

`view.battery.estimate` is present on every available snapshot:

```json
{
  "mode": "solar",
  "rate_kw": 0.7,
  "hours": 4.653,
  "clock": "15:09",
  "soc_final": null,
  "solar_end": null,
  "solar": {
    "as_of": "10:30",
    "remaining_kwh": 12.82,
    "charge_kwh": 8.8,
    "needed_kwh": 7.89,
    "load_kw": 0.5,
    "peak_kw": 2.377,
    "risk_factor": 0.5,
    "reachable": true,
    "capped": false,
    "charge_limit_kw": 6.0,
    "charge_limit_source": "default_6kw"
  },
  "reason": "solar_curve"
}
```

`hours_to_empty` and `hours_to_full` remain as aliases for older clients. The browser prefers
`estimate.clock` over its own local-time formatting, so the finish time follows the site
timezone. `data-estimate` on the Home stage carries the mode for styling.

## Worked checks

At `P_obs = 4000 W`, 10:30, `L = 0.5 kW`, 15 kWh / 50 % SOC / 5 % reserve / 0.95 efficiency:

| Resolved cap | Result |
|---|---|
| `6.0 kW` (fallback) | enough, **4.65 h → 15:09**, not capped |
| `4.2 kW` | enough, **4.65 h → 15:09**, not capped |
| `1.5 kW` | enough, **5.70 h → 16:12**, capped |
| `1.0 kW` | not enough, **86.5 % → 87 % at 18:00** |
| `0`/negative | treated as unavailable, falls back to 6 kW |

At `P_obs = 1200 W`, same load and battery, the model reports **50.2 % at 18:00**: the
surplus is too small to replace the house load and fill the pack. The risk factor and the
load subtraction are deliberately conservative; the value can be tuned in one place.

## Known limits

- **The cap may be unavailable.** `get_power_limit` has returned `null` on this X1 account,
  and `get_device_attrs` may be empty on A5102. The estimate then uses 6 kW and says so in
  `charge_limit_source`.
- **`ac_power_limit` is an AC input ceiling**, not necessarily a PV-to-battery limit. Using
  it as a cap is conservative.
- **The curve is fixed to 07:00–18:00.** Before and after that window the fallback is linear,
  so late solar charging is shown at its current rate rather than extrapolated.
- **The house load is assumed constant** over the remaining daylight. A large load change
  moves the real finish time.
- **This is an estimate only.** It does not publish a wake, change a plan or set a limit.
