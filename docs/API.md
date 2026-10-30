# API schema (reverse-engineered)

Everything here was derived from the Android app (`com.anker.charging`) and verified
against the live cloud. It is cross-checked against the community
[`thomluther/anker-solix-api`](https://github.com/thomluther/anker-solix-api) project,
especially [issue #313](https://github.com/thomluther/anker-solix-api/issues/313)
(Power Panel TOU live probes). That issue is the source for the **`day_type` 2=weekday,
1=weekend** mapping and the row-id/insert semantics below. Identifiers are redacted: `<site_uuid>`, `<inverter_sn>`,
`<account_id>`. This is an undocumented private API; treat it as unstable.

## Hosts

| Region | Base |
|---|---|
| US / global | `https://ankerpower-api.anker.com` |
| EU / UK | `https://ankerpower-api-eu.anker.com` |
| Account | `https://passport.anker.com` |

Region follows the account's `country`. A wrong region logs in but returns zero sites.

## Auth

`POST /passport/login`

```json
{"ab":"GB","email":"<email>","password":"<encrypted>",
 "client_secret_info":{"public_key":"04…"},
 "enc":0,"time_zone":"Europe/London","transaction":"…"}
```

`password` is AES-256-CBC encrypted to an ECDH P-256 key (see `REPORT.md` §3.2).
The response carries `auth_token` (~7 days, no refresh) and `user_id`; the app computes
`gtoken = md5(user_id)`.

Common headers:

```
x-auth-token: <auth_token>
gtoken: <md5(user_id)>
app-name: anker_power
country: GB
timezone: Europe/London
model-type: PHONE
os-type: android
content-type: application/json
```

## Request envelope

Most app calls add `x-encryption-info: algo_ecdh`, `x-key-ident`, `x-request-once`,
`x-request-ts`, `x-signature`, and a base64 ciphertext body. **The TOU read/write used
here works with plain JSON and no envelope** — do not implement the envelope unless a
future endpoint requires it.

## Endpoints

| Method | Path | Body | Purpose |
|---|---|---|---|
| POST | `/passport/login` | credentials | sign in |
| POST | `/power_service/v1/site/get_site_list` | `{}` | sites |
| POST | `/power_service/v1/site/list_user_devices` | `{"site_id":…}` | devices (bucketed) |
| POST | `/charging_hes_svc/get_device_command` | `{"station_id":…,"sn":"<inverter>"}` | **richest plan read** + settings |
| POST | `/charging_hes_svc/get_utility_rate_plans` | `{"station_id":…,"sn":"<inverter>"}` | plan read |
| POST | `/charging_hes_svc/device_command` | write body below | **plan write** |
| POST | `/power_service/v1/app/device/set_device_attrs` | `{"device_sn":…,"attributes":{"ac_power_limit":W}}` | **AC charge power ceiling** |
| POST | `/power_service/v1/app/device/get_device_attrs` | `{"device_sn":…,"attributes":["ac_power_limit"]}` | read attributes (may be empty on A5102) |
| POST | `/v1/openapi/devicemanage/get_user_mqtt_info` | `{}` | MQTT broker + mTLS certs |
| POST | `/charging_hes_svc/get_system_running_info` | encrypted | runtime snapshot (encrypted response) |
| POST | `/charging_hes_svc/get_energy_statistics` | encrypted | energy chart |
| POST | `/charging_hes_svc/get_device_card_list` | encrypted | home cards |

**`sn` matters.** `get_utility_rate_plans` without `sn` rounds prices to **one** decimal;
with the inverter serial it returns the stored two-decimal values.

## Plan schema (read)

```json
{
  "peak_sessions": [{
    "id": 0, "name": "Season1", "type": 2,
    "start_time": "YYYY-01-01 00:00:00", "start_desc": "January",
    "end_time": "YYYY-12-01 00:00:00", "end_desc": "December",
    "peak_valley_prices": [{
      "id": 0, "buy": 0.07, "sell": 0.12, "energy_unit": "kWh",
      "start_time_period": "00:00", "end_time_period": "05:00",
      "peak_type": 1, "day_type": 1
    }]
  }],
  "price_source_type": 5,
  "price_source_sub_type": 51,
  "price_unit": "£",
  "tou_last_set_type": 51,
  "electricity_strategy_mode": 1,
  "fixed_rate": null,
  "nem_plan_info": null
}
```

| Field | Meaning |
|---|---|
| `peak_type` | **1 = Super Off-Peak … 4 = Peak** (price-ascending) |
| `day_type` | **2 = weekday, 1 = weekend** (HES enum) |
| `buy` / `sell` | import / export price, `£/kWh`, 2 decimals, **truncated** |
| `start_time_period` / `end_time_period` | `HH:mm`; last end `24:00` |
| `id` | server-assigned on read; **keep it** for modified rows, use `0` for a new row |

## Plan write

`POST /charging_hes_svc/device_command`, plain JSON:

```json
{
  "station_id": "<site_uuid>",
  "main_sn": null,
  "time": "YYYY-MM-DD HH:MM:SS",
  "utility_rate_plan": { "peak_sessions": [ … existing ids kept, 0 for new … ] },
  "ack_peak_valley": null,
  "accuracy": 5,
  "electricity_strategy": {"mode": 1},
  "price_source_type": 5,
  "price_source_sub_type": 51,
  "version": "1.0.0"
}
```

| Field | Correct | Notes |
|---|---|---|
| `main_sn` | `null` | cloud resolves the inverter from the site |
| `time` | site-local `YYYY-MM-DD HH:MM:SS` | **not** Unix seconds, not UTC |
| `accuracy` | `5` | `1` silently rounds prices to 1 dp |
| `ack_peak_valley` | `null` | |
| `electricity_strategy` | `{"mode":1}` | `{}` is not equivalent |
| period `id` | existing id, `0` for a new row | preserving ids avoids unnecessary row churn |
| prices | exact `buy`/`sell` | cloud truncates both to 2 dp |

The cloud applies writes **asynchronously**: an immediate read often returns the old plan.
Poll for ~3–6 s before concluding a write failed.

### AC charge power ceiling

`ack_peak_valley` is **null** and `electricity_strategy` starts as **`{"mode":1}`**, exactly
as the app sends it. Two EMS fields may be added when the user changes them:

- `conserve_percent` — reserve SOC, integer 0–100
- `grid_recharging` — 0 or 1

The cloud **merges** these into the existing strategy. Do not send the full read object back;
that risks overwriting untouched EMS state. The editor reads the live strategy on load,
populates the two controls, and sends **only fields the user changed**. A save with no changes
sends just `{"mode":1}` — the exact app shape — so reserve SOC and grid charging are untouched. The TOU body has **no power field**. The Android
app sets the grid-charge ceiling in a separate call:

```json
POST /power_service/v1/app/device/set_device_attrs
{"device_sn": "<inverter_sn>", "attributes": {"ac_power_limit": 4200}}
```

`ac_power_limit` is in **watts**. Without this call the inverter can recharge at its full
6 kW rating, even though the TOU schedule is correct. The web app sets this **before**
writing the plan, so the inverter is already throttled when the schedule lands. It is a
separate endpoint from `device_command`; there is no documented plan-body field for it.

## Rules the app enforces

- **Device granularity is one hour.** The cloud accepts `:30`, but the X1/D156 device floors
  boundaries to the previous hour (`23:30 → 23:00`, `05:30 → 05:00`). The web editor uses
  hourly slots; see [`FIRMWARE-LIMITS.md`](FIRMWARE-LIMITS.md).
- **Full day, contiguous.** First period starts `00:00`, last ends `24:00`.
  A window like `23:00–05:00` is three periods:
  `00:00–05:00`, `05:00–23:00`, `23:00–24:00`.
  The cloud can store `:30`, but the device floors it to the hour; the editor does the same.
- **One buy and one sell price per tariff type**, across both day types and all seasons.
- Maximum **4 periods per day type**.
- Prices are **truncated to 2 decimals** (`0.0762 → 0.07`, `0.3123 → 0.31`).
- `buy` cannot be omitted: a missing/`null` value clears the price.

## Live telemetry (MQTT)

`POST /v1/openapi/devicemanage/get_user_mqtt_info` (plain JSON) returns:

```json
{"endpoint_addr":"aiot-mqtt-<region>.anker.com",
 "thing_name":"<account_id>-anker_power",
 "app_name":"anker_power","user_id":"<account_id>",
 "certificate_id":"<certificate_id>",
 "certificate_pem":"…","private_key":"…","aws_root_ca1_pem":"…"}
```

The broker ACL is keyed on the app’s generated client-id:

```
android-<app_name>-<user_id>-<certificate_id>
```

Using an arbitrary `thing_name_<random>` identity connects but every
subscription returns **SUBACK 128**. A unique suffix such as
`-web-<instance>` is allowed and lets a second client coexist with the phone app.
Use a fresh suffix per feed process; a fixed suffix can collide with a stale
broker session after restart. Connect over TLS **8883** with mutual TLS, then
subscribe read-only to:

```
dt/anker_power/<model>/<sn>/troubleshot_info/ems_second
dt/anker_power/<model>/<sn>/basic_info
dt/anker_power/<model>/<sn>/param_info
dt/anker_power/<model>/<sn>/state_info
```

The EMS frame used by the home screen is `…/troubleshot_info/ems_second`, arriving at
the cadence requested in the wake (`interval`); the captured stream was about **5 s**. Wildcards are rejected by the broker ACL; subscribe to the exact topics.
Outer JSON:

```json
{"head":{"client_id":"<sn>","cmd":8,"timestamp":1700000000,"…":"…"},
 "payload":"{\"trans\":\"<base64>\"}"}
```

`trans` decodes to:

```json
{"sn":"<sn>","subSn":"<sn>","localTime":"YYYY-MM-DD HH:MM:SS","type":"ems",
 "data":{"soc":61.3,"pp":0,"gp":0,"bp":1080,"lp":1080,
         "p2lp":0,"p2bp":0,"p2gp":0,"g2lp":0,"g2bp":0,"b2lp":1080,"b2gp":0,
         "ps":0,"gs":0,"bs":2,"es":1}}
```

| Field | Meaning |
|---|---|
| `pp` / `gp` / `bp` / `lp` | PV / grid / battery / load power (W) |
| `p2lp` / `p2bp` / `p2gp` | PV → load / battery / grid |
| `g2lp` / `g2bp` | grid → load / battery |
| `b2lp` / `b2gp` | battery → load / grid |
| `soc` | state of charge (%) |
| `ps` / `gs` / `bs` / `es` | PV / grid / battery / EMS state |

The flow fields are non-negative per edge, so direction is explicit. The feed
subscribes read-only and publishes only the read-only realtime wake request
below; it never writes a plan, controller mode or charge power.

### Realtime wake (`cmd: 9`)

If no frame arrives, the local feed publishes:

```text
cmd/anker_power/<model>/<sn>/req

{"head":{"version":"1.0.0.1","client_id":"cloud","cmd":9,
         "cmd_status":2,"sign_code":0,...},
 "payload":"{\"account_id\":\"…\",\"device_sn\":\"…\",\"trans\":\"<base64>\"}"}
```

`trans`:

```json
{"sn":"<sn>","subDeviceType":"ems","subSn":"<sn>","messageId":"<32 hex>",
 "cmdId":4,"config":{"count":2,"interval":60},
 "startTime":0,"duration":0,"primary":0,"type":0}
```

The device accepts the production `count=60, interval=5` window. Above 5 s the
confirmed shape is `count=2`; the server renews after the second frame. Longer
windows above 5 s are not requested because they fell back to a 5 s stream in
the 2026-10-29 probe (the Skylight's older 5 s wake was also running, so the
fallback was not conclusively attributed to the device).

Local server contract:

| Method | Path | Meaning |
|---|---|---|
| `GET` | `/api/state` | `mqtt_interval_s` + `mqtt_bounds` alongside the session |
| `GET` | `/api/live` | `mqtt_interval_s`, `stale_after_s`, `live_stats` (`interval_s`, `count`, `pending_interval_s`, `last_gap_s`, `waking`, `last_frame_keys`, `limits_error`) and the view below |
| `POST` | `/api/live/interval` | `{"interval_s": 5…60}`; clamps to a 5 s step, updates the session and every active feed, sets the preference cookie |

The preference cookie is `anker_tou_mqtt_s` (`HttpOnly; SameSite=Strict; Path=/;
Max-Age=31536000`, plus `Secure` with `--secure-cookies`). It is not the session
cookie and deliberately survives sign-out and account switches. A change while
a window is active is stored as pending and applied at the next window boundary.

On an available snapshot, `live.waking` is the command state: true from the
publish until the first frame or the 60 s timeout. `live.waking_notice` is the
debounced display flag; it turns true 2 s after the publish, so a fast renewal
never flashes `Refreshing`. `live.stale` is independent of both and takes
precedence in `view.status` (`Stale · Ns` even while a wake retries).

### Live view: battery estimate

`/api/live` returns `view.battery.estimate` alongside the existing battery fields. `mode` is
one of `unavailable`, `idle`, `discharging`, `grid`, `charging`, `solar` or `full`; it is the
legacy formula name. `phase` is the averaged state (`unavailable`, `idle`, `full`,
`discharging` or `charging`) and `source` is `none`, `battery`, `solar`, `grid`, `mixed` or
`unknown`.

Only this estimate uses the ten-minute averages for a displayed time; the battery,
house-load and PV means share the same 600 s window. `batt_avg_net_kw`, `batt_avg_kw`,
`load_avg_kw` and `solar_avg_kw` are diagnostics, not card power. `view.battery.w`, `state`,
`label`, `soc`, `available_kwh`, every flow and the `solar`/`grid`/`home` cards stay on
instantaneous telemetry, so the battery shimmer can disagree with the averaged ETA by
design. `view.solar.w` is instantaneous; `view.solar.avg_kw` is the PV mean used by the
estimate.

```json
{
  "mode": "solar",
  "phase": "charging",
  "source": "solar",
  "rate_kw": 0.7,
  "hours": 4.653,
  "clock": "15:09",
  "soc_final": null,
  "solar_end": null,
  "reason": "solar_curve",
  "eta": {
    "kind": "to_full",
    "hours": 4.653,
    "clock": "15:09",
    "label": "15:09",
    "soc": null,
    "at": null
  },
  "solar": {
    "as_of": "10:30",
    "remaining_kwh": 12.82,
    "charge_kwh": 8.8,
    "needed_kwh": 7.89,
    "load_kw": 0.5,
    "peak_kw": 2.377,
    "observed_kw": 4.0,
    "observed_source": "avg",
    "risk_factor": 0.5,
    "reachable": true,
    "capped": false,
    "charge_limit_kw": 6.0,
    "charge_limit_source": "default_6kw"
  }
}
```

A slow discharge from 10:30 shows the calendar hint on `eta.label`, while `clock` stays the
actual target time-of-day:

```json
{
  "mode": "discharging",
  "phase": "discharging",
  "source": "battery",
  "rate_kw": 0.214,
  "hours": 30.0,
  "clock": "16:30",
  "eta": {
    "kind": "to_empty",
    "hours": 30.0,
    "clock": "16:30",
    "label": "tomorrow 16:30",
    "soc": null,
    "at": null
  }
}
```

- `eta.kind` is the closed set `none | at_full | at_empty | to_full | to_empty | to_soc`.
  The browser switches on it: `to_empty`/`to_full` render the duration plus `eta.label`,
  `to_soc` renders `{eta.soc}% by {eta.at}`, and `at_full`/`at_empty` render `Full`/`Empty`.
- `clock` is the actual target time-of-day in the site timezone, not modulo 24 h.
  `eta.label` is the server-owned display string: under 12 h it is just `HH:MM` even when the
  target crosses midnight; 12 h or more over the next calendar day adds `tomorrow`; two or
  more days out uses e.g. `Sat 04 Jul 02:38`, with the year when it differs from now.
- `eta.hours` mirrors `hours` after the view rounds it to three decimals. For `to_soc`,
  `hours` is `null` and `eta.soc`/`eta.at` carry the expected SOC at 18:00 instead.
  The legacy top-level `soc_final` and `solar_end` remain set for that case and mirror
  `eta.soc`/`eta.at`.
- The `hours_to_empty` and `hours_to_full` aliases are derived from `eta.kind`, so a new
  estimate branch does not need another mode list.
- `charge_limit_kw` is resolved at runtime: realtime EMS (`realtime.ems_second.<key>`) →
  device attributes or the site power limit (`device_attrs.<key>`, `power_limits.<key>`) → `default_6kw`. `plan-save` is a temporary
  source set immediately after a successful schedule save; the next frame or read replaces it.
  `capped` is true only when the limit actually clips the solar surplus.
- `solar.observed_kw` is the PV observation that scaled the curve. `solar.observed_source`
  is `avg` when the ten-minute mean was used and `instant` when the estimator fell back to
  the instantaneous reading.
- `batt_avg_net_kw` (signed), `load_avg_kw`, `solar_avg_kw` and the legacy `batt_avg_kw` are
  in the live snapshot. The averages cover ten minutes and are time-weighted. The estimator
  uses `solar_avg_kw` for the 50 W solar gate and the curve, falls back to the instantaneous
  reading when it is `null`, and treats `0.0` as a real zero.

See [`SOLAR-ESTIMATE.md`](SOLAR-ESTIMATE.md) for the mode table and formulas.

## Cross-check with `anker-solix-api` #313

The community [`anker-solix-api`](https://github.com/thomluther/anker-solix-api)
live-probed an A17B1 Power Panel and documented the same TOU endpoint. These
details were missed by our first pass and are now reflected above:

- **`day_type` is 2 = Weekday, 1 = Weekend** — the reverse of our original labels.
- **Existing row ids should be preserved** when changing a slot; `0` is accepted for a
  new/inserted row, and the cloud allocates an id.
- **Clearing a plan** is `peak_sessions` with one season whose `peak_valley_prices` is
  `[]`, not an empty `peak_sessions`.
- **`fixed_price` is a full session object**, not a scalar; that Power Panel shape differs
  from the X1 A5102 plan, which uses `fixed_rate: null`.

## Validation quirks

Server errors are useful: they name the first missing/mistyped field.

- `time` must be a **string**, with the shape above.
- `electricity_strategy` must be an **object**.
- `main_sn` must be `null` (the app shape), not a serial.
- Missing prices are **cleared**, not preserved.
