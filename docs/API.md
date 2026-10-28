# API schema (reverse-engineered)

Everything here was derived from the Android app (`com.anker.charging`) and verified
against the live cloud. Identifiers are redacted: `<site_uuid>`, `<inverter_sn>`,
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
| `day_type` | 1 = weekday, 2 = weekend |
| `buy` / `sell` | import / export price, `£/kWh`, 2 decimals, **truncated** |
| `start_time_period` / `end_time_period` | `HH:mm`; last end `24:00` |
| `id` | server-assigned on read; send `0` on write |

## Plan write

`POST /charging_hes_svc/device_command`, plain JSON:

```json
{
  "station_id": "<site_uuid>",
  "main_sn": null,
  "time": "YYYY-MM-DD HH:MM:SS",
  "utility_rate_plan": { "peak_sessions": [ … ids 0 … ] },
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
| period `id` | `0` | |
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
 "certificate_pem":"…","private_key":"…","aws_root_ca1_pem":"…"}
```

Connect MQTT over TLS **8883** with mutual TLS, subscribe read-only to:

```
dt/anker_power/<model>/<sn>/#
```

The EMS frame used by the home screen is `…/troubleshot_info/ems_second`, arriving about
every **5 s**. Outer JSON:

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

The flow fields are non-negative per edge, so direction is explicit. Subscribe-only;
never publish commands from this app.

## Validation quirks

Server errors are useful: they name the first missing/mistyped field.

- `time` must be a **string**, with the shape above.
- `electricity_strategy` must be an **object**.
- `main_sn` must be `null` (the app shape), not a serial.
- Missing prices are **cleared**, not preserved.
