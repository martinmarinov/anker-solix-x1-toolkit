# Solar-aware battery ETA

*Technical note. This is an estimate for the wall display, not a charge controller.*

The dashboard already showed a linear “time to full” from the current battery rate. That
number is misleading when the battery is charging from PV: solar output falls through the
afternoon, so a rate sampled at 10:30 does not hold until sunset. This change adds a
solar-curve mode for the charging case and leaves the existing discharge and grid-charge
formulas untouched.

The model uses one live PV observation and the local time. The observation is the ten-minute
time-weighted mean from the live feed, with the instantaneous reading as a fallback. It does
**not** use location, weather, season, panel orientation or historical generation. It is
deliberately a rough estimate.

## When each formula applies

`B_w` is the signed ten-minute average battery power, positive while charging. `S̄_w` is the
ten-minute averaged PV (instant fallback), `S_w` the instantaneous PV, `G_w` grid import and
`X_w` grid export. `t0` is the site-local decimal hour.

| # | Condition | Mode | ETA |
|---|---|---|---|
| 0 | Nominal capacity, SOC or reserve missing | `unavailable` | none |
| 1 | `B_w <= -50 W` | `discharging` | existing: `available_kwh × 0.95 / rate_kw` |
| 2 | `-50 W < B_w < 0` | `idle` | none |
| 3 | `B_w == 0`, SOC below 100 | `idle` | none |
| 4 | `B_w == 0`, SOC at 100 | `full` | `Full` |
| 5 | `B_w > 0`, `G_w > 50 W` | `grid` | existing: `deficit / (rate_kw × 0.95)` |
| 6 | `B_w > 0`, `S < t0 < E`, `S̄_w > 50 W`, `X_w <= 50 W` | `solar` | curve below |
| 7 | `B_w > 0`, anything else | `charging` | linear fallback from `B_w` |

`S` and `E` are the site's sunrise and sunset as decimal local hours. `app/daylight.py`
supplies them from the estimate described below; when no location is available the fallback
window is **07:00–18:00**.

The 50 W gates are **thresholds**, not measurements: a discharge below 50 W is treated as
idle so inverter noise does not produce a multi-day ETA. Grid input above 50 W wins over
solar, because the battery may be charging from the grid even when PV is present. Outside
the supplied window the curve is not defined, so charging is linear again.

The estimator consumes the battery, house-load and PV means from the live feed's shared
600 s window. `batt_avg_*`, `load_avg_kw` and `solar_avg_kw` are exposed for diagnostics and
are never rendered as card power. The battery card’s watts, state, label and SOC, every flow,
and the solar/grid/home cards still follow instantaneous telemetry, so the card can read
“Discharging” while the ETA is hidden or while the averaged estimate says it is charging.

## One daylight window

`app/daylight.py` is the single authoritative source for `S` and `E`. The server resolves it
once per `/api/live` request from the snapshot timestamp and the effective timezone, then
sends it in `view.daylight` on both available and unavailable views:

```json
"daylight": {
  "sunrise": "04:47",
  "sunset": "21:21",
  "source": "ip",
  "approximate": true
}
```

The same object drives all four day-boundary behaviours: the solar-card moon/cloud icon, the
one-hour dawn/dusk backdrop crossfade, the solar-estimate window above and the battery
caption’s fallback sunset clock.

### Location and calculation

The window is a local calculation, not a per-request cloud lookup.

1. At startup `app/solar_location.py` resolves the approximate site:
   - all three of `SOLAR_LATITUDE`, `SOLAR_LONGITUDE` and `SOLAR_TIMEZONE` when set (an exact fix);
   - otherwise the cache at `~/.anker-tou/solar-location.json`, valid for 30 days;
   - otherwise one GeoJS request from the server’s public IP.
2. `daylight.sun_times()` passes the date and IANA timezone to Astral’s `Observer`,
   `sunrise` and `sunset`. Astral returns aware datetimes in the site zone, so daylight-saving
   time is applied before the decimal hour is calculated.
3. Every `/api/live` request reads the in-process calculation cache. There is no network call
   per poll; only a cold start or an expired cache can contact GeoJS.

If GeoJS fails and a cache exists, the stale fix is used and retries wait 24 hours. If there
is no usable location, the canonical **07:00–18:00** window is returned with
`source="default"`. The same fallback covers an invalid timezone, a polar day or night, and a
window outside `0 <= sunrise < sunset <= 24`.

The public sources are `ip`, `config`, `explicit` and `default`; only `ip` sets
`approximate=true`. The browser’s `static/daylight.js` remains display-only: it parses
`view.daylight` and never estimates sun times. `/api/live` has no `sunrise`/`sunset` query
parameters. The small bottom-corner watermark renders the window and source as a debug aid.

### Why the app keeps decimal hours

The original Python brief returned ISO-8601 datetimes. This app passes decimal local hours
between modules because the half-sine curve, ETA labels and `HH:MM` cards already use that
contract. Astral still does the astronomy; `daylight._decimal_hour` converts the aware result
once. A timezone or DST change therefore moves all four consumers together, with no second
source of truth.

## The solar curve

The model is a symmetric half-sine between sunrise `S` and sunset `E`, peaking at the
midpoint `(S + E) / 2`. The 07:00/18:00 pair is the fallback window; a resolved day uses the
estimated values.

```
W      = E - S
s(t)   = sin(pi * (t - S) / W)                  inside S..E, else 0
P_exp  = min(P̄_obs / s(t0), 2.5 * P̄_obs)        expected, anchored at t0
P_low  = 0.5 * min(P̄_obs / s(t0), 2.5 * P̄_obs)  conservative low case
P(t)   = P_peak * s(t)                          W
E(u,v) = P_peak * W / (1000 * pi) *
         [cos(pi*(A-S)/W) - cos(pi*(B-S)/W)]     kWh
```

`A = max(S,u)`, `B = min(E,v)`. `P̄_obs` is the ten-minute averaged PV in W (with the
instantaneous reading as fallback). The expected curve anchors at the observation, so at
`t0` it equals `P̄_obs` unless the 2.5× cap binds. The observation already contains today's
cloudiness; the old 0.5 factor modelled a different, twice-as-cloudy day and is now kept as
`solar.low`, not used for the headline. The cap is applied before the risk factor in both
curves. `SOLAR_EXPECTED_RISK_FACTOR`, `SOLAR_RISK_FACTOR` and
`SOLAR_MAX_PEAK_MULTIPLIER` are named constants in `app/solar_forecast.py`; there is no UI
or environment override.

The window is resolved once per estimate. `app/solar_forecast.py` stores `start_hour` and
`end_hour` on each `SolarCurve`, and every energy helper uses those fields rather than the
module defaults. A direct caller of `estimate_battery` may pass `solar_start_hour` and
`solar_end_hour`; a missing or invalid pair falls back to 07:00–18:00. The strict interior
`S < t0 < E` is unchanged: at exactly sunrise or sunset the half-sine is zero, so the model
stays linear there while the display may treat those minutes as day.

## Averaged observation

`app/live.py` keeps a 600 s deque alongside the battery/load samples. Each frame stores PV
as `solar_w(snap)/1000` only when the frame carries a PV field. `solar_avg_kw` is the
time-weighted trapezoidal mean over that same window, using the same cutoff and weighting as
the battery average; an interval with a missing PV endpoint contributes nothing. It is
rounded to three decimals.

The estimator treats the mean as the PV value at `t0` and anchors the expected half-sine
through `s(t0)`; the low curve applies the same treatment with the 0.5 factor. That keeps
steady-state numbers consistent with the observation, but a rising morning mean is slightly
below the instantaneous value at `t0`, and a falling afternoon mean is slightly above it.
The phase bias around sunrise and sunset is accepted; only the observation is averaged, not
the curve.

Fallback rules:

- a frame without a PV field is skipped, not averaged as 0 W;
- no mean yet → the instantaneous `S_w` is used;
- mean `0.0` → a real zero, so the solar branch is not taken even if `S_w` spikes;
- mean negative → clamped to 0;
- short history → whatever mean exists is used as-is.

The same observation drives the 50 W solar gate and the curve. Grid import/export gates
remain instantaneous. The load energy-balance fallback also stays instantaneous because it
balances live flows.

## House load and inverter cap

The curve is gross PV. The battery only receives the surplus after the house:

```
L_kw = load_avg_kw
     or max(0, (S_w + G_w - X_w - max(0, B_w)) / 1000)
     or home_w / 1000
```

`load_avg_kw` is the same ten-minute household average the battery average uses. If it is
missing, the instantaneous energy balance is tried first; if that balance is zero or
negative, the instantaneous `home_w` reading is used. Both fallbacks are approximate and are
labelled by the diagnostics.

If the logged charging state and the anchored curve disagree at `t0` (sensor disagreement,
or the 2.5× cap binding near sunrise/sunset), the estimator reconciles the assumed load with
the measured battery rate and re-anchors the current PV. The headline therefore cannot show
a discharge while the battery is measurably charging. This is a defensive path; it does not
fire for the 11:43 screenshot case.

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
100 % and report `~1h 59m · full 12:29`. Here `t2` is the afternoon time when solar falls
back to the house load. The same calculation runs for the low curve; its result is preserved
under `solar.low` and shown in the caption tooltip.

If it is not enough, we integrate to the effective sunset `E` and report the expected SOC
there plus the end time, for example `82% by 16:37`. `solar.charge_until` and
`solar.soc_peak` describe the peak before the afternoon decline, so a charging battery can
legitimately end below its current SOC: it charges to ~92 % by 14:06, then the constant house
load exceeds the falling PV. The caption adds a small `under assumed load` note in that case.
The final SOC is clamped at `min(reserve, soc)`, because the real device would stop
discharging at the reserve and a SOC already below it must not be raised.

Grid and linear charging continue to use the legacy deficit, which includes the reserve.
That preserves the existing numbers (`8.68 h` in the regression fixture) and is intentional:
the user asked not to change those formulas.

## View and API fields

`view.daylight` carries the server-computed window and source described above. It is present
on both the available and unavailable view, so the sky and solar icon stay correct while
telemetry is missing.

`view.battery.estimate` is present on every available snapshot. `mode` is the legacy formula
name; `phase` and `source` describe the averaged estimate that produced the time:

```json
{
  "mode": "solar",
  "phase": "charging",
  "source": "solar",
  "rate_kw": 0.7,
  "hours": 1.975,
  "clock": "12:29",
  "soc_final": null,
  "solar_end": null,
  "eta": {
    "kind": "to_full",
    "hours": 1.975,
    "clock": "12:29",
    "label": "12:29",
    "soc": null,
    "at": null
  },
  "solar": {
    "as_of": "10:30",
    "remaining_kwh": 25.65,
    "charge_kwh": 20.89,
    "needed_kwh": 7.89,
    "load_kw": 0.5,
    "peak_kw": 4.755,
    "power_kw_at_t0": 4.0,
    "net_kw_at_t0": 3.5,
    "batt_avg_kw": 0.7,
    "observed_kw": 4.0,
    "observed_source": "avg",
    "risk_factor": 1.0,
    "reachable": true,
    "capped": false,
    "charge_until": "12:29",
    "soc_peak": 100.0,
    "charge_limit_kw": 6.0,
    "charge_limit_source": "default_6kw",
    "low": {
      "risk_factor": 0.5,
      "kind": "to_full",
      "hours": 4.653,
      "clock": "15:09",
      "label": "15:09",
      "soc": null,
      "at": null
    }
  },
  "reason": "solar_curve"
}
```

`eta.kind` is the closed set `none | at_full | at_empty | to_full | to_empty | to_soc`.
`to_empty`/`to_full` render the duration plus `eta.label`; `to_soc` renders
`{eta.soc}% by {eta.at}`; `at_full`/`at_empty` render `Full`/`Empty`. For `to_soc`, the
legacy top-level `soc_final` and `solar_end` remain set and mirror `eta.soc`/`eta.at`.

`solar.risk_factor` is `1.0` for the headline curve. `solar.low` repeats the ETA shape
(`risk_factor`, `kind`, `hours`, `clock`, `label`, `soc`, `at`) for the 0.5 curve. The
diagnostics also expose `power_kw_at_t0` (modeled PV at the observation), `net_kw_at_t0`
(modeled PV minus the assumed load), `batt_avg_kw` (measured battery average),
`charge_until` (when the battery stops charging: full, or the afternoon crossing) and
`soc_peak` (the high point before any afternoon decline).

`clock` is the actual target time-of-day in the site timezone, not a modulo. `eta.label` adds
the calendar hint and is formatted only on the server:

| Now | Remaining | Target | `eta.label` |
|---|---|---|---|
| 23:00 | 8 h | 07:00 next day | `07:00` |
| 23:00 | 13 h | 12:00 next day | `tomorrow 12:00` |
| 10:30 | 30 h | 16:30 next day | `tomorrow 16:30` |
| 19:35 | 37.05 h | 08:38 two days out | `Sun 01 Nov 08:38` |
| 23:00 | 25 h | 00:00 two days out | `Fri 03 Jul 00:00` |
| 10:30 | 64.125 h | 02:38 three days out | `Sat 04 Jul 02:38` |

The rule is calendar-based, not a 24–48 h bucket: under 12 h the time is printed bare even
when it crosses midnight; 12 h or more over the next day adds `tomorrow`; two or more days
out uses the weekday and date, plus the year when it differs from now. Aware datetimes are
advanced by absolute seconds, so DST transitions keep the correct wall time. The 12 h cutoff
is `ETA_SHORT_HOURS` in `app/eta.py`.

`hours_to_empty` and `hours_to_full` remain as aliases for older clients and are derived from
`eta.kind`. The browser has one display switch on `eta.kind`; it does not format a day or a
clock itself. `data-estimate` on the Home stage still carries the legacy mode for styling.

## Worked checks

These worked examples pin the fallback 07:00–18:00 window, so they stay reproducible when
the live estimate changes. At `P̄_obs = 4000 W` (a steady mean or point reading), 10:30,
`L = 0.5 kW`, 15 kWh / 50 % SOC / 5 % reserve / 0.95 efficiency:

| Resolved cap | Expected | Low (0.5 risk) |
|---|---|---|
| `6.0 kW` (fallback) | enough, **1.98 h → 12:29**, not capped | **4.65 h → 15:09** |
| `4.2 kW` | enough, **1.98 h → 12:29**, capped | **4.65 h → 15:09** |
| `1.5 kW` | enough, **5.26 h → 15:46**, capped | **5.70 h → 16:12** |
| `1.0 kW` | not enough, **92.1 % at 18:00** | **86.5 % at 18:00** |
| `0`/negative | treated as unavailable, falls back to 6 kW | — |

At `P̄_obs = 1200 W`, same load and battery, the expected model reports **74.8 % at 18:00**
and the low case **50.2 %**: the surplus is too small to replace the house load and fill the
pack. The 0.5 factor and the load subtraction remain available as the conservative signal,
but they no longer set the headline.

The screenshot case is the one that motivated the split. At 11:43, `P̄_obs = 1.66 kW`,
`L = 1.20 kW`, SOC 87.33 %, window 06:51–16:37:

| Curve | Current modeled net | Charge until | Peak | Sunset SOC |
|---|---:|---|---:|---:|
| Expected (`1.0`) | **+0.46 kW** | 14:06 | 91.9 % | **82.0 %** |
| Low (`0.5`) | −0.37 kW | — | 87.33 % | **64.3 %** |

The expected headline agrees with the measured `+0.46 kW` battery average. It still ends
below the current SOC because the constant load exceeds PV after 14:06; the caption's
`under assumed load` note explains that. The old 64.3 % remains visible as `solar.low`.

## Known limits

- **The cap may be unavailable.** `get_power_limit` has returned `null` on this X1 account,
  and `get_device_attrs` may be empty on A5102. The estimate then uses 6 kW and says so in
  `charge_limit_source`.
- **`ac_power_limit` is an AC input ceiling**, not necessarily a PV-to-battery limit. Using
  it as a cap is conservative.
- **The curve window is a local estimate.** `app/daylight.py` uses Astral with approximate IP
  coordinates, so a cloud host, VPN or CGNAT can be minutes to hours off. Set the three
  `SOLAR_*` variables for a fixed site. With no usable location, a polar day or night, or an
  invalid window, the canonical **07:00–18:00** fallback applies. Before and after the window
  the fallback is linear, so late solar charging is shown at its current rate rather than
  extrapolated.
- **The PV mean is treated as the current value.** Around sunrise and sunset the ten-minute
  mean lags a rising or falling curve, so the inferred peak is slightly low in the morning and
  slightly high in the late afternoon. This is accepted and documented above.
- **The house load is assumed constant** over the remaining daylight. A large load change
  moves the real finish time. A charging battery can therefore still end below its current
  SOC after the afternoon crossing; the caption says `under assumed load` in that case.
- **The anchored peak can overshoot near the window edges.** When the 2.5× cap binds and the
  battery is measurably charging, the consistency guard re-anchors the current PV. That keeps
  the sign honest but can lift the modeled daily energy. The long-term fix is a physical
  array limit or a daily-energy cap, not a larger multiplier.
- **This is an estimate only.** It does not publish a wake, change a plan or set a limit.
