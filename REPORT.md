# Anker SOLIX X1 — Cloud Protocol Reverse Engineering & TOU Plan (device: hourly)

**Target:** `com.anker.charging` v3.24.3 (code 237) · Flutter 3.24 / Dart 3.11.0 AOT · device family **X1 / X1-H**
**Method:** static RE of the pulled APK splits (Dart AOT via `blutter`, RN bundle, dex, resources) + cross-check against the public `anker-solix-api` implementation.
**Confidence tags:** **seen** = read directly in the binary/source; **inferred** = conclusion from evidence; **unknown** = not verified.

> **Redaction:** every account id, site UUID, serial number and model SKU in this document is a placeholder (`<account_id>`, `<site_uuid>`, `<inverter_sn>`, `<battery_sn>`, `<model>`). Endpoints, field names and timing are real; identities are not.

> **Device execution is whole-hour — observed 2026-10-28.** The cloud **accepts and returns**
> `:30` values (a `23:30`/`05:30` plan read back exactly as sent), but the inverter changed mode
> at **23:00**/**05:00**. It truncates the minutes — floors to the previous hour — and only uses
> the hour in practice. Read-back is therefore **not** proof of execution. All examples below use
> whole hours; `:30` appears only where it is explicitly labelled as a historical/cloud-only test.

---

## 1. Verdict up front

| Question | Answer |
|---|---|
| Is the 1-hour step a hardware limit? | **Effectively yes for TOU.** The wire format carries `"HH:mm"` strings and the cloud stores `:30` verbatim, but the X1/D156 device floors TOU boundaries to the previous whole hour. The app’s picker is also hourly. *(seen in cloud and on device)* |
| Can a 30-minute boundary be represented? | **In the cloud: yes. On the device: no.** A `23:30–05:30` window ran via the **Dynamic Tariff** path (Anker case `#<case-ref>`), but a TOU plan stored as `23:30`/`05:30` switched at **23:00**/**05:00**. The device floors TOU boundaries to the hour. See [`docs/FIRMWARE-LIMITS.md`](docs/FIRMWARE-LIMITS.md). |
| What actually enforces 1 hour? | **The device’s TOU scheduler** (a `23:30` plan switched at `23:00`) plus the app’s hourly picker. The protocol still accepts `HH:mm`; only the device execution is hourly. |
| Can I control the X1 from outside the app? | **Yes, two ways:** (a) the app's own cloud API, which the app uses for this exact screen; (b) **official local Modbus TCP** — sanctioned by Anker, but you must get it enabled by your installer. |
| Should I patch the Anker app? | **No.** Flutter AOT snapshot, no Dart source, signed APK, breaks on every update. Worst effort/benefit ratio of all options. |
| Lowest-cost / highest-success route? | **A small "plan writer" that reuses the app's cloud call, snapshots the current plan, writes the hourly version, reads it back, and can roll back** — i.e. a one-shot script or a tiny web page. Not a new full app. |

> **The one-line version:** the app and cloud can express `:30`, but the X1/D156 device applies the **previous whole hour**. A saved `23:30` switched at `23:00`; `05:30` switched at `05:00`. See [`docs/FIRMWARE-LIMITS.md`](docs/FIRMWARE-LIMITS.md).

### Observed behaviour: accepted by the cloud ≠ executed by the device

| Layer | What happened |
|---|---|
| Write | `device_command` sent `start_time_period: "23:30"`, `end_time_period: "05:30"` |
| Cloud read-back | `get_utility_rate_plans` returned **23:30** / **05:30** exactly |
| Device execution | mode changed at **23:00** / **05:00** |
| Conclusion | the device **truncates/floors the minutes** and uses only the whole hour |

This is not a rounding-to-nearest behaviour and not a read-back rounding: the cloud value is
correct, but the device ignores the `:30`. The project therefore emits whole hours only.

---

## 0. Non-negotiable design requirement

The final product is a **replacement for one screen** — not a new energy controller. Everything below follows from that.

1. **It must ride the target-driven Time-of-Use controller.** Write the TOU rate plan and keep the system in **Time of Use** mode. That controller is what computes a charge rate to reach the target by the end of the valley window, and therefore what ramps and modulates instead of slamming to 6 kW.
2. **It must never use the Dynamic Tariff / window-driven scheduler.** No `save_dynamic_price`, no `charging_hes_dynamic_price_svc/*`, no mode flip. That scheduler is window-driven: it saturates at maximum and forces grid charging on (see §4.6).
3. **It must never send a charge-power value.** The TOU payload has no power field (§4.6). Inventing one would mean driving a different controller. The client's only job is to describe *when* and *which tariff*, and let the EMS derive *how fast*.
4. **It must change only the boundaries.** Every other field — mode, `price_type`, seasons, `day_type`, `peak_type`, prices, `energy_unit` — is copied verbatim from the live plan. `assert_times_only()` in `poc/anker_tou.py` enforces exactly this and refuses anything else.
5. **The UI must mirror the existing TOU editor**, with **1-hour steps** (the device's practical limit; see [`docs/FIRMWARE-LIMITS.md`](docs/FIRMWARE-LIMITS.md)): same season range ("Jan – Dec"), same Weekday/Weekend split, same period rows with tariff type + start + end + price, same add/remove (the app caps at **4 periods**), same Save. Do not add a power slider, a ramp control, or a "smart" scheduler.
6. **Do not write `:30`.** The server stores it, but the device floors it to the hour. Do not switch to Dynamic Tariff for finer granularity — that trades a cosmetic limit for an overcurrent hazard.

**Acceptance criteria — all four must hold before this is called working:**

| # | Check | Pass condition |
|---|---|---|
| 1 | Read-back | Plan returned by `get_utility_rate_plan` differs from the prior plan **only** in `start_time_period` / `end_time_period` |
| 2 | Mode | App still reports **Time of Use**, not Dynamic Tariff; `price_type` unchanged |
| 3 | Ramp | Charge power **rises gradually** after 23:00 — no instantaneous step to maximum |
| 4 | Ceiling | With backup load present, grid draw stays below the configured limit and the breaker rating (see §4.5) |


---

## 2. What the app actually is

`base.apk` contains exactly **one 13,892-byte `classes.dex`**, and it is an obfuscated shell (`s.h.e.l.l.N/C/S/A`) — there is no meaningful Java code to decompile. The app is a three-engine hybrid:

| Engine | Artifact | Role |
|---|---|---|
| **Flutter (Dart AOT)** | `lib/arm64-v8a/libapp.so` — **77.9 MB**, Dart snapshot, 249,350 strings | **Everything you care about**: site/device pages, X1 settings, TOU/rate-plan editor, cloud HTTP client, MQTT client |
| React Native | `assets/rn_bundle_android_charging.zip` → `index.charging.bundle` (4.6 MB, **plain JS, not Hermes**) | Portable power-station ("charging") screens only — **no TOU code** (grep: 0 hits for `utility_rate`, `peak_valley`, `start_time`) |
| Unity IL2CPP | `libil2cpp.so` + `global-metadata.dat` | 3D house/energy-flow rendering, product visuals |

**Implication:** `jadx` is useless here (13 KB of shell classes); RN is a dead end for TOU; the only place the schedule exists is the Dart AOT snapshot. That is why `blutter` was required — it recovered **per-file Dart class/function structure**, including the exact model classes below.

The app is **not obfuscated at the symbol level**: Dart file paths and class names survive (`package:charging/ui/page/device/<model>/device_setting/utility_rate_plan/...`). That is what makes this tractable.

**Model codes:** the family spans several SKUs (US/EU, single- and three-phase) plus a separate battery-module SKU; the exact codes are redacted here. The client classifies devices by model prefix: inverter prefixes own the TOU plan, battery prefixes are capacity only. The TOU editor lives under `.../<model>/device_setting/utility_rate_plan/`.

---

## 3. Cloud protocol

### 3.1 Hosts *(seen — string table of `libapp.so`)*

```
https://ankerpower-api.anker.com          ← US / global (primary)
https://ankerpower-api-eu.anker.com       ← EU / UK
https://ankerpower-api-qa.anker.com  ·  -qa2 · -beta · -eu-qa · -eu-beta   ← staging
https://aiot-api-cn.anker.com.cn          ← China AIoT
https://charging-api-cn-beta.anker.com.cn
https://passport.anker.com                ← account portal (web)
```

Region is selected by the account's `country` (ISO-2). A wrong region **logs in fine but returns zero sites** — so "no devices" usually means wrong base URL.

### 3.2 Auth *(seen in app + verified against `anker-solix-api` v3.9.1 source)*

`POST /passport/login` — JSON body:

```json
{
  "ab": "GB",                                  // ISO-2 country
  "email": "you@example.com",
  "password": "<base64 AES-256-CBC>",           // encrypted, see below
  "client_secret_info": { "public_key": "<hex uncompressed EC point 04||X||Y>" },
  "enc": 0,
  "time_zone": 3600000,                         // UTC offset in ms
  "transaction": "1745103679000"                // unix ms, string
}
```

Password encryption:
1. Generate an ephemeral **ECDH P-256** keypair.
2. `shared = ECDH(priv, server_pubkey)` → derive a symmetric key.
3. `AES-256-CBC` (IV = first 16 bytes of the shared key), **PKCS7**, base64.

Response gives:
- `auth_token` — opaque bearer token, **~7 days**, **no refresh endpoint** (re-login when it expires).
- `user_id` — and `gtoken = md5(user_id)`.
- `nick_name`, `geo_key`, `ap_cloud_user_id`.

Per-request headers *(seen as identifiers; confirmed in library)*:

```
content-type: application/json
model-type: PHONE          app-name: anker_power      os-type: android
country: GB                timezone: GMT+00:00
x-auth-token: <auth_token>
gtoken: <md5(user_id)>
```

> There is **no `X-Api-Key`**. That header does not exist in this protocol. The app sends `x-app-key: ""` (empty) only on the optional encrypted-envelope variant. Anything you read about "Anker API keys" is about a different product line.

Optional hardened envelope (the app uses it for some endpoints): `x-encryption-info: algo_ecdh`, `x-key-ident`, `x-request-once`, `x-request-ts`, `x-signature: HMAC-SHA256(...)`, body as base64 ciphertext. **Plain JSON is still accepted** — the public library relies on that.

### 3.3 The endpoints that matter for this task *(seen)*

| Purpose | Endpoint |
|---|---|
| Site list / detail | `POST /power_service/v1/site/get_site_list` · `/get_site_detail` |
| Devices in site | `POST /power_service/v1/site/list_user_devices` |
| **Generic param read** | `POST /power_service/v1/site/get_site_device_param` |
| **Generic param write** | `POST /power_service/v1/site/set_site_device_param` |
| **Read TOU plan (HES)** | `POST /charging_hes_svc/get_utility_rate_plans` (plural; singular is 404) |
| **Write TOU plan (HES)** | `POST /charging_hes_svc/device_command` — see §11 (`update_hes_utility_rate_plan` is 404) |
| TOU detail / templates | `POST /charging_hes_svc/get_tou_price_plan_detail` |
| Price-plan 3-step handshake | `POST /charging_energy_service/get_utility_rate_plan` → `/preprocess_utility_rate_plan` → `/ack_utility_rate_plan` |
| Save TOU (installer / dynamic price) | `POST /charging_hes_installer_svc/save_time_of_use` · `/charging_hes_dynamic_price_svc/save_time_of_use` |
| **Device command channel (HES)** | `POST /charging_hes_svc/device_command` |
| Site price | `POST /power_service/v1/site/update_site_price` |
| Generic device attrs | `POST /power_service/v1/app/device/get_device_attrs` / `set_device_attrs` |

`set_site_device_param` body shape *(seen in model class names + library)*:

```json
{ "site_id": "...", "param_type": "6", "cmd": 17,
  "param_data": "{\"…\":\"…\"}" }     // param_data is a JSON **string**, not an object
```

`param_type` is a string enum. On X1-class systems the legacy `param_type 6/12` path returns the older Solarbank-shaped schema (empty for this device); the actual TOU write is `device_command` (§11). The captured Save carried **no `cmd` field** — it is plain JSON with `utility_rate_plan`.

### 3.4 MQTT — live telemetry *(verified on-device)*

The live UI is **push**, not polling. Verified end-to-end: subscribe, receive, decode.

**Credentials are plaintext if you ask in plaintext.** `POST /v1/openapi/devicemanage/get_user_mqtt_info` (legacy alias `/app/devicemanage/...`) returns:

```json
{"endpoint_addr":"aiot-mqtt-<region>.anker.com",
 "thing_name":"<account_id>-anker_power",
 "certificate_pem":"-----BEGIN CERTIFICATE-----…",
 "private_key":"-----BEGIN RSA PRIVATE KEY-----…",
 "aws_root_ca1_pem":"-----BEGIN CERTIFICATE-----…"}
```

No ECDH envelope is required for this call, even though the Android app sends one. The same is true of `device_command` (§11).

**Transport.** MQTT over TLS, port **8883**, **mutual TLS** with that per-account certificate. A plain `paho-mqtt` client with `tls_set(ca, cert, key)` connects (`CONNACK rc=0`). Subscribe-only is sufficient for monitoring; publishing is only needed for commands.

**Topics.**
- telemetry: `dt/anker_power/<model>/<sn>/#`
- the EMS frame used by the home screen: `…/troubleshot_info/ems_second`
- commands (not used by this project): `cmd/anker_power/<model>/<sn>/req`; ack `…/app/res`

**Frame.** Outer JSON:

```json
{"head":{"version":"1.0.0.1","client_id":"<sn>","sess_id":"…","msg_seq":123,
         "cmd":8,"cmd_status":1,"timestamp":1700000000},
 "payload":"{\"trans\":\"<base64>\"}"}
```

`payload` is a JSON **string**; `trans` decodes to:

```json
{"sn":"<sn>","subSn":"<sn>","localTime":"2026-10-27 17:58:47","type":"ems",
 "data":{"soc":61.3,"pp":0,"gp":0,"bp":1940,"lp":1940,
         "p2lp":0,"p2bp":0,"p2gp":0,"g2lp":0,"g2bp":0,"b2lp":1940,"b2gp":0,
         "ps":0,"gs":0,"bs":2,"es":1}}
```

**Cadence.** Measured inter-arrival: `6, 5, 5, 5` seconds — the inverter pushes every **~5 s**. This is why the phone's numbers move without matching REST traffic.

**REST is only reconciliation.** The home page also runs `Timer.periodic` with a `Duration` of `0x1c9c380` µs = **30 s**, and the capture shows `get_energy_statistics` roughly every 30 s. So the architecture is: MQTT push for live values, 30 s REST for charts and consistency.

> The earlier note about `wss://` and a binary TLV telemetry payload was wrong for this firmware. The binary/TLV envelope belongs to the **command** channel; telemetry here is JSON+base64.

### 3.5 Local Modbus (the sanctioned path) *(seen in app + community)*

The app has a whole Third-Party Control section:
`/charging_hes_third_party_service_svc/query_modbus_setting`, `/set_modbus_setting`, UI strings **"Home Assistant via Modbus"**, **"Homey via Modbus"**, **"Show Modbus register map"**, **"Modbus RTU"**, **"Modbus TCP"**, and error **`E1133: Modbus TCP communication timeout`**.

- Port **502**, slave **1**; register map is published by Anker (linked from the app; the PDF is gated behind `support.ankersolix.com`, but a community YAML with the actual addresses exists).
- **Enabling requires the installer**, in *Anker SOLIX Professional* → Third-Party Control Settings. It is **not** in the consumer app and **not** part of standard commissioning.
- Community-reported constraints: **Ethernet required** (Wi-Fi reports `0.0.0.0`); **not available with more than one inverter** (master/slave); one Modbus client at a time.
- Known-good registers (community YAML): plant status `10000`, PV power `10002`, third-party PV `10004`, load/feed-in energy `10030`/`10034`, plus holding registers for work mode, export/import limits, output mode.

**Important gap:** the community register map covers **monitoring + power limits + work mode**; **no TOU schedule registers are published or discovered**. So Modbus is the safest *integration* surface, but as of now it is **not** a proven route to a sub-hourly TOU plan. Treat it as plan B / complementary.

---

## 4. The TOU data model (this is the answer to "is it hardware?")

### 4.1 The exact object the X1 saves *(seen — recovered Dart model + string cluster)*

From `package:base_service/bean/peak_valley_param_data.dart`, `.../<model>/.../config_data/<model>_device_command_model.dart` and `package:charging/model/<model>/<model>_device_command_model.dart`:

```
peak_sessions[                        // season-scoped sessions ("session1", "Jan-Dec")
  {
    start_time: "2026-01-01 00:00:00",
    end_time:   "2026-12-01 00:00:00",
    peak_valley_prices: [
      { start_time_period: "00:00",
        end_time_period:   "05:00",     // ← end-of-day is clamped to "23:59", seen literally
        peak_type: 1,                    // cheapest→dearest: 1=Super Off-Peak … 4=Peak
        day_type:  1,                    // 1=Weekend, 2=Weekday
        buy: 0.3164, energy_unit: "kWh" }
    ]
  }
]
+ price_unit: "£/kWh"        (string "…/kWh" seen)
+ utility_rate_plan          (wrapper key seen)
```

The editor's own vocabulary maps 1:1 onto your screenshot:

| UI (your screenshot) | Field |
|---|---|
| "Jan – Dec" season | `peak_sessions[].start_time` / `end_time` |
| Weekday / Weekend tab | `day_type` **2 / 1** |
| "Super Off-Peak" | `peak_type` **1** |
| "Peak" | `peak_type` **4** |

> **Direction of the enum — verified live, and easy to get backwards.** In this schema the numbers ascend with *price*: `1` = cheapest (Super Off-Peak), `4` = dearest (Peak). A live X1 plan returned `00:00–05:00` as type `1` with `buy = 0` and `05:00–24:00` as type `4` with `buy = 0.30`, matching the app screenshot. The older Solarbank enum (`PEAK=1 … VALLEY=4`) is the **opposite**; using it here would move the cheap window onto peak-rate electricity.
| `00:00 – 05:00` row | `start_time_period` / `end_time_period` |
| `£ 0.3164 /kWh` | `buy` + `energy_unit` |
| "Save" | `update_hes_utility_rate_plan` → `device_command` (`cmd 123`) |

### 4.2 Why the hour step is UI-only

Three independent pieces of evidence:

1. **The field is a string, not an hour integer.** `PeakValleyPrices.toJson` reads `field_17` and `field_1b` and puts them straight into the map — **no division by 60, no `~/3600`, no rounding**. The only transformation anywhere is `"23:59"` used as the end-of-day sentinel, which proves minutes are semantically meaningful.
2. **The literal template is `{"start_time":"…"`** — a quoted value, built by interpolation. Hour-integer APIs serialise as `{"start_time":5`.
3. **The slider is 24 discrete ticks.** The "Jan – Dec" editor is a custom vertical range slider labelled `00 / 06 / 12 / 18 / 24` with one tick per hour, and the row label formatter always emits `:00`. Adjacent strings are German `"Stunde einstellen"` ("set the hour") — consistent with an hours-only picker.

**Conclusion:** the app picker emits whole hours, but the wire schema still accepts `"HH:mm"`.
A different client can put `"23:30"` in `start_time_period` and the **cloud stores it**, but the
**device truncates it to 23:00**. Cloud storage is not device execution.

**The honest caveat — now closed for TOU:** *accepting* the string is not the same as *honouring* it. A `23:30–05:30` window ran via the **Dynamic Tariff** path (§4.4), but a TOU plan stored as `23:30`/`05:30` switched at **23:00**/**05:00**. The X1/D156 TOU scheduler floors boundaries to the previous whole hour. See [`docs/FIRMWARE-LIMITS.md`](docs/FIRMWARE-LIMITS.md).

### 4.3 Your target plan, expressed in the real schema

Weekday **23:00 → 05:00 = Valley (4)**; **05:00 → 23:00 = Peak (1)**. Same for weekend unless
you want a different shape. These are the whole-hour values the device actually executes.

```json
{
  "site_id": "<siteId>",
  "sn": "<deviceSn>",
  "peak_sessions": [{
    "start_time": "2026-01-01 00:00:00",
    "end_time":   "2026-12-01 00:00:00",
    "peak_valley_prices": [
      { "start_time_period": "23:00", "end_time_period": "05:00", "peak_type": 1, "day_type": 2, "buy": 0.10, "energy_unit": "kWh" },
      { "start_time_period": "05:00", "end_time_period": "23:00", "peak_type": 4, "day_type": 2, "buy": 0.3164, "energy_unit": "kWh" },
      { "start_time_period": "23:00", "end_time_period": "05:00", "peak_type": 1, "day_type": 1, "buy": 0.10, "energy_unit": "kWh" },
      { "start_time_period": "05:00", "end_time_period": "23:00", "peak_type": 4, "day_type": 1, "buy": 0.3164, "energy_unit": "kWh" }
    ]
  }],
  "price_unit": "£/kWh"
}
```

`day_type` 2 is Weekday, 1 is Weekend. The period wraps midnight (`23:00 → 05:00`); a wrap is
normal in this schema. An old `23:30 → 05:30` version of this example read back exactly from the
cloud, but the device applied **23:00 → 05:00** — do not use it. If a wrap is rejected, the
whole-hour fallback is three rows: `00:00–05:00`, `05:00–23:00`, `23:00–24:00` (both Valley ends,
Peak in the middle).

### 4.4 Field evidence — Anker case #<case-ref>

Anker support's answer to "can TOU granularity be 30 minutes?" was to use **Dynamic Tariff** instead. That is a non-answer to the question, but it is a very useful data point for the reverse engineering — and it carries a safety finding.

**What it proves.** On app **v3.14.0** the Dynamic Tariff feature successfully held a **23:30 → 05:30** charge window. So:

- the cloud accepts `:30` boundaries, and
- the **Dynamic Tariff** path executed them on that older build.

This is **not** evidence that TOU honours `:30`. The TOU experiment below stored `23:30`/`05:30`
and the device switched at `23:00`/`05:00`, so cloud read-back and Dynamic Tariff behaviour must
not be generalised to TOU.

That removes the main unknown from §4.2 for the *Dynamic Tariff* scheduler, but **not for TOU**: the D156 TOU device floors `:30` to the hour in the field. The 1-hour step is therefore both a picker property and a device-execution limit.

**What it also reveals — and why Dynamic Tariff is not a workaround.** In Dynamic Tariff the X1 charged at a flat **6 kW** with no regard for the rest of the supply. On 29 Nov, with ~2 kW of backup load running, the unit pulled **a bit over 8 kW** (6 kW charge + ~2 kW backup) through a circuit protected by a **32 A** breaker (≈7.4 kW at 230 V) for over an hour, until overcurrent protection tripped and took the X1 off grid — requiring a manual breaker reset in the main board.

> By contrast, in **Time of Use / Super Off-Peak** mode the same unit *modulates* charge power to stay within the rated current, whatever the backup load is doing. That load-aware cap is the behaviour you must not lose.

**Consequences for this project:**

1. **Dynamic Tariff is off the table** — not merely because it does not match a fixed 3× off-peak tariff, but because its charge-control path bypasses the load-aware current limit. Two independent reasons.
2. **Write the TOU rate plan, keep the mode on Time of Use.** The goal is to change *times*, not to change *mode*. Any implementation that flips `price_type`/mode to dynamic re-creates the hazard.
3. **Add a hard electrical ceiling as defence in depth.** Whatever the mode does, the site should be configured so the battery cannot exceed the service capacity: set a **grid import / charge power limit** below the breaker rating (the app exposes import-power-limit settings; the Modbus map has import/export limit registers). Then even a misbehaving schedule trips a limit, not the breaker.
4. **Verify the first cycle with current, not just time.** Confirming "charging started at 23:00" is not sufficient — confirm the *power* stayed within the circuit's rating while backup load was present.

### 4.5 Can you limit charge power today? — yes, the controls ship in the app

This matters more than the sub-hourly feature: a hard power ceiling is what makes any schedule experiment safe.

**Seen in the binary** — a dedicated *Station Power Limit* feature:

| Evidence | Meaning |
|---|---|
| `.../station_home_settings/station_power_limit/station_power_limit_logic.dart` with `_getStationPowerLimit`, `showPowerLimitPicker`, `_setDevicePowerLimit`, `showOutputPowerPicker`, `_setOutputPowerLimit` | A real settings page with read + write for station and device power limits |
| `widget/diy_legal_power_limit_sheet.dart`, `DiyLegalPowerLimitType` | A sheet for setting a *custom* limit beyond the preset/legal values |
| `APP_GRID_CHARGING_SETTING_PARAMETER`, `acPowerLimit`, `setSiteDevicePowerLimit` | The parameter keys behind it; `acPowerLimit` is the AC/grid charge ceiling |
| `"Set the maximum grid charging power supported by the device."` | Tooltip for that control |
| `"Battery charging power drawn from the grid will be limited to less than 4.2kW."` | Interpolated explanation, i.e. the setting has a concrete value in kW |
| `"Grid Charging"`, `"Enable this feature to allow grid charging under Time-of-Use mode. When disabled, X1 only charges from solar."` | A separate on/off gate for grid charging in TOU |
| `"Enable this feature to allow grid charging in Schedule and Self-Consumption Modes."` | Same gate for the other modes |

The `system_policy_view.dart` page (`_sliderWidget` / `_sliderTipsWidget`) and the `station_power_limit` page exist in the binary, but **neither is reachable for the X1 in the consumer app** — the device's Settings menu has no System Policy or Power Limit entry (verified on-device). See the corrected navigation note below.

**Where it actually is — verified on the device (27 Oct 2026, app 3.24.3):**

Device → **Settings (gear)** → **Modes** → scroll to **Advanced Options** → **Grid Charging** (a plain on/off switch, currently **on**), and **Peak Shaving** ("peak home power consumption has not been set").

**There is no maximum grid charging power control in the consumer app.** The row is not clickable — only the switch is — so there is no detail page behind it. The `Station Power Limit` page that exists in the binary (`station_power_limit_model.dart`, with `ac_input_power` / `legal_power_limit`) belongs to the AE100 / station product family, not the X1, and `/site/get_power_limit` returns `data: null` for this account. **The earlier advice in this document pointing at "Station → Settings → Power Limit" was wrong for the X1.** The ceiling can only be set through the installer's *Anker SOLIX Professional* app (or by Anker support), which is also where the "legal power limit" lives.

**What to set, concretely.** Your 32 A breaker is ≈7.36 kW at 230 V. With ~2 kW of backup load, a 6 kW charge reaches ~8 kW. A grid-charge ceiling of **≤4.2 kW** (the value in Anker's own string, and a common single-phase limit) leaves ~3.2 kW of headroom and would have prevented the 29 Nov event on its own.

**Can the new tool add this?** Yes — it is the same read/write surface (`get_power_limit` / `acPowerLimit` via the site-device-param path). The plan is:
1. read the current limit and **print it** during every run (the PoC now does this on `--dump`);
2. **refuse to write a schedule** if the limit is missing or above a configured ceiling;
3. optionally offer to lower it first, as an explicit separate action — never silently.

**Caveat:** a limit is necessary but not sufficient. It is not yet established that Dynamic Tariff *honours* `acPowerLimit`; the 29 Nov behaviour suggests the mode's own control law can outrank it, or that the limit was simply set high. Treat the ceiling as defence in depth, not as permission to use the unsafe mode.

### 4.6 Why TOU paces the charge and Dynamic Tariff slams it

The two modes are **different schedulers**, not one scheduler with a hidden power parameter.

**What the payloads show (seen):**

- The X1 TOU payload carries *only* times, `peak_type`, `day_type`, `buy` and `energy_unit`. **There is no power field anywhere in `peak_sessions[].peak_valley_prices[]`.** So the app cannot be "headlessly selecting" a charge power in TOU — there is no field to carry it. Whatever paces the charge is decided downstream.
- Dynamic Tariff Mode is described in the app as supporting *"charging windows, cutoff levels, period count and other parameters"*, and it **forces grid charging on**: *"Grid charging cannot be disabled while using Dynamic Tariff Mode."* The app also calls it an upgrade of the older *Time Slot* mode.
- Power limits are a **separate, mode-independent** setting (§4.5), not part of either schedule.

**The model that fits all the observations (inferred):**

| | Time of Use | Dynamic Tariff |
|---|---|---|
| What the user supplies | price map over a valley/peak window | explicit charging windows + cutoff SOC |
| What the controller is solving | *reach the target by the end of the window* — an energy problem | *charge while this window is open* — a state problem |
| Resulting power | computed rate, ramped, modulated against house load | saturates at max immediately |
| Load awareness | yes (observed: stays within rated current) | apparently none (observed: 6 kW + 2 kW load) |

The tell is the **absence of a ramp**. A target-driven controller ramps: it knows how many kWh it needs and how long is left, so it starts gently and holds a rate. A window-driven controller has no reason to ramp — the window says "charge", the cutoff says "until 100%", and the only brake is a power ceiling.

So: **not a hidden app parameter — a different control law on the backend/device.** That is good news for this project, because it means:

- writing the TOU *plan* does not require reproducing any power computation — the cloud/EMS does it;
- keeping the mode on TOU preserves the pacing;
- a **6-hour** window (23:00–05:00) needs *less* average power than the 5-hour one, so the pacing should be gentler still.

**What remains unknown:** whether `acPowerLimit` is enforced inside Dynamic Tariff (evidence suggests not, or it was set high); and whether the TOU EMS recomputes its rate when the plan is written by a third-party client — it should, since the plan is server-side state, but this is exactly what the first cycle's current measurement will confirm.

---

## 5. Options for rebuilding — scored

| Option | Effort | Cost | Risk | Success odds | Notes |
|---|---|---|---|---|---|
| **A. Plan-writer script / tiny local web UI** (reuse app's cloud call) | **Low** (1–3 days) | £0 | **Low–Med** | **High** | Same request the app makes; snapshot + rollback; no app store, no signing, no re-auth loop. Works from a laptop/phone browser. |
| **B. Local Modbus client** (Home Assistant / Node / Python) | Med | £0 | **Very low** | **Low** *(for TOU)* | Official + fully local + no credentials in the cloud path. But **no published TOU registers**, needs installer + Ethernet, single-inverter only. Best for *monitoring/limits*, not proven for schedule. |
| **C. Web app** (hosted, multi-user) | Med | ~£5/mo | Med | High | Same API as A, plus auth/session handling, CORS proxy (Anker has no CORS), secret storage. Only worth it if you want it from anywhere / for others. |
| **D. Android app from scratch** | **High** (2–4 wks) | £0 | Med | Med | Reimplements auth + TLS + plan model, then you still ship/maintain an APK. Strictly more work than C for the same capability. |
| **E. Patch the existing APK** | **Very high** | £0 | **High** | **Low** | Flutter **AOT** snapshot — no Dart source to recompile; would need binary patching of the snapshot or a hook at runtime. Signed APK, Play Integrity, and it breaks every release. **Not recommended.** |
| **F. Frida runtime hook** (patch slider at runtime, rooted device) | Med | £0 | Med | Med | Faster than E: hook the picker's divisions/value at runtime. But rooted phone only, fragile across versions, and you still need the write path. Useful as a *diagnostic*, not a product. |
| **G. Use Anker's suggested Dynamic Tariff instead** | ~0 | £0 | **High** | **Ruled out** | Anker's own answer to the sub-hour question. It does accept `23:30`, but it bypasses load-aware charge limiting — the documented cause of an overcurrent breaker trip on this installation (§4.4). Excluded on safety grounds, not just tariff mismatch. |

### Recommendation

**Do A, with F used only as a lab instrument if A's write is rejected.**

1. **Phase 0 (read-only, tonight):** authenticate, pull the current plan, dump it to disk. Zero risk. This alone tells you the exact live schema for *your* firmware and site (field names, `param_type`, `cmd` actually used, whether `peak_sessions` or `use_time` comes back).
2. **Phase 1 (one controlled write):** write the whole-hour plan `23:00–05:00` with the snapshot saved, then **read it back**. Do not write `:30`; the device floors it to the hour.
3. **Phase 2 (behavioural proof):** watch one night — did grid charging actually begin at 23:00? On the D156, an old `23:30` plan began at 23:00. This is why the editor is hourly now.
4. If the wrap is rejected, use three whole-hour rows: `00:00–05:00`, `05:00–23:00`, `23:00–24:00`. Then use Modbus for monitoring; do not switch to Dynamic Tariff to obtain finer granularity (safety, §4.4).

Why not D/C first: they are the same protocol work with more scaffolding. Build the thin writer first; if it works and you want a nicer UX, wrap it in a web page later — the protocol layer is reusable.

> **Implemented.** `app/` contains the local web editor described here (stdlib server + single page, **1-hour selects**, live diff, acknowledgement gate, read-back verification with automatic rollback). The 30-minute target UX below is the original design and is now superseded by the device limit. Run `./pandacode-run.sh` → <http://127.0.0.1:8770>. Security model, deployment and the error matrix are in `app/README.md`.

### Target UX (original design) — half-hour steps, superseded

The new client should be recognisably *the same screen*, so the mental model and the payload stay aligned. Concretely:

| Element | Behaviour |
|---|---|
| Season range | "Jan – Dec" (or the installed plan's range), editable at season boundaries only — same as today |
| Day split | Weekday / Weekend segmented control, each with its own period list (`day_type` **2 / 1**) |
| Period list | One row per period: colour dot, tariff label (**Super Off-Peak**, Peak, Mid-Peak, Off-Peak), `HH:mm – HH:mm`, overflow menu for delete |
| Period picker | **1-hour steps** in the current build. The original 30-minute/48-slot design was dropped after the device floored `:30`; see [`docs/FIRMWARE-LIMITS.md`](docs/FIRMWARE-LIMITS.md) |
| Vertical slider | 24 one-hour ticks in the current build |
| Add period | "+ Add Period", capped at **4** (matches the app's own limit: "A maximum of 4 time periods can be configured") |
| Pricing | Per tariff: buy price + unit (`£/kWh`), exactly as the current Pricing section |
| Save | Single sticky action → TOU rate-plan write → read back → show result |

**Validation rules to implement in the editor (the app's own invariants):**
- Periods must tile the day: `00:00 → 23:59`, no gaps, no overlaps.
- Only the off-peak/valley period may wrap midnight (starts late, ends early). A wrapping **peak** period is almost certainly a user error.
- At most one period per `(day_type, tariff)` pair unless the backend allows splits; keep the same shape the live plan already uses.
- Refuse to save if the live plan's schema is the integer-hour form; that is a different payload shape, not the app's `HH:mm` form.

**Explicitly out of scope:** any power, current, ramp-rate or "charge speed" control. The screen describes *when* and *at what tariff*; the target-driven controller decides *how fast*.

---

## 6. Safety — what can and cannot hurt your system

**What is safe:** a TOU plan is a **configuration write**, not a firmware write. There is no code path here that flashes firmware, so **bricking is not a realistic risk**.

**What is not safe:** do not confuse "not firmware" with "not physical". A schedule write also selects *how the inverter charges*, and one of those modes has already caused a real overcurrent event in this installation (Anker case `#<case-ref>`, §4.4): 6 kW of charging plus 2 kW of backup load on a 32 A breaker, for over an hour, until it tripped. The protective device worked; the sequence should never have reached it. Treat charge-control changes as **electrical**, not merely behavioural.

**Risk tiers:**

| Tier | Actions | Verdict |
|---|---|---|
| 🟢 Safe | read site list, read device params, read plan, read energy stats | do freely |
| 🟡 Controlled | rewrite **times only** in the TOU rate plan (`update_hes_utility_rate_plan`), keeping mode = Time of Use | snapshot, read back, check the first cycle's **current** |
| 🟠 Careful | `set_site_device_param` for mode/SOC, `update_site_price`, work-mode changes | these change *behaviour* beyond the schedule; change one at a time |
| 🔴 Avoid entirely | **switching to Dynamic Tariff** (`save_dynamic_price` / `charging_hes_dynamic_price_svc/*`), `set_power_cutoff` outside sane ranges, `set_modbus_setting` toggles, OTA endpoints, `device_command` with unknown `cmd`, any firmware/`/app/ota/*` path | documented overcurrent hazard, or can genuinely misconfigure the system / interrupt supply |

**Hard rules for the tool:**
- **Never switch the plan to Dynamic Tariff to obtain sub-hour slots.** That is Anker's suggestion, and it is the exact configuration that produced the breaker trip. Change times; do not change mode.
- **Set an import/charge power ceiling below the breaker rating** before the first experiment. Defence in depth: whatever the schedule does, the site should not be able to exceed its service capacity. Verify it is active.
- **Never** send `cmd` values you have not seen the app send. The app uses **17**, **123** (HES TOU) and **0** (newer settings pages) — and the library sends 17 everywhere. A wrong `cmd` with a valid body is the single most likely way to get an unexpected effect. Capture what *your* app sends first.
- **Always** snapshot the full plan JSON (timestamped file) before writing, and verify by re-reading.
- **One field at a time.** First attempt: change *only* the two boundary times on an existing plan, keeping seasons, prices, types and `day_type` byte-identical.
- **Never** run writes unattended on a schedule until a manual write has been verified and stable for ≥72 h.
- Keep a sane reserve (the app's "battery reserve" / `backup_reserve`) untouched; do not lower it to buy more arbitrage during testing.
- Don't test during a storm, an outage, or a firmware update window — and don't run the first test on a night when the backup load is unpredictable.
- Respect rate limits (the public library throttles to ~10 req/min; energy endpoints are stricter). A re-login loop is a good way to get `26084 token kicked out`.
- **Expect your phone app to log you out** if you log in from a second client on older app versions; since 3.10 parallel tokens are allowed, but if you see "logged in elsewhere", that's why.

**Rollback:** because you snapshot the exact prior JSON, rollback is `write(snapshot)` — the same call, same shape. Keep the snapshot outside the repo (it contains your site id/SN, not your password).

---

## 7. Implementation plan

### Phase 0 — read-only dump (safe, do this first)

```
POST /passport/login                      → auth_token, user_id, gtoken=md5(user_id)
POST /power_service/v1/site/get_site_list → siteId(s)
POST /power_service/v1/site/list_user_devices → deviceSn, product code (inverter family)
POST /power_service/v1/site/get_power_limit   {site_id}
   ↳ record acPowerLimit / station limit BEFORE touching any schedule
POST /charging_hes_svc/get_utility_rate_plan  {siteId, sn}
   ↳ save the raw response verbatim to captures/plan-<ts>.json
```

Check in the response: which shape you actually get (`peak_sessions` vs `use_time`), the `param_type`, and whether times round-trip as `"HH:mm"`.

### Phase 1 — the hourly write

```
POST /charging_hes_svc/update_hes_utility_rate_plan   (or /save_time_of_use)
  body = { siteId, sn, peak_sessions:[…], price_unit:"£/kWh" }
→ expect {"code":0}
POST /charging_hes_svc/get_utility_rate_plan          → assert start_time_period == "23:00"
```

Then `device_command` (`cmd 123`) only if the app's flow requires it — **capture the app's exact body first** (see §8) rather than guessing.

### Phase 2 — behavioural verification
- Night 1: check grid-charge **start at 23:00 and stop at 05:00** (app chart, Modbus `10002`, or your smart meter).
- Night 1, more important: check **power**, not just timing. With a representative backup load running, confirm the unit **ramps** — compare the first ~10 minutes against steady state; a target-driven controller should climb gradually, not step to maximum — and stays below the breaker rating. The failure mode in Anker case `#<case-ref>` was an overcurrent, not a wrong timestamp.
- Confirm the app still reports **Time of Use Mode**, not Dynamic Tariff.
- Only then consider scheduling it.

---

## 8. How to capture the app's exact request (the highest-value 15 minutes)

*Completed — the captured request is documented in §11; the method below is kept for future app versions.*

Before writing anything, capture what *your* app sends when you press Save. This removes all guesswork about `cmd`, `param_type`, and field names.

**Option 1 — mitmproxy (recommended).** Install a CA on the phone, set a proxy, and filter:

```
mitmproxy --set ignore_hosts='^(?!ankerpower-api)'
```

Open the TOU editor, change one boundary, press Save, and you get the exact path + body + headers. If the app pins certificates, mitmproxy will fail — then:

**Option 2 — Frida (rooted device / emulator).** Hook the Dart HTTP layer or, more simply, `SSL_write`/BoringSSL at the native layer, or hook the model's `toJson` to print the map. This also lets you *read the slider value* and confirm the picker is what quantizes.

**Option 3 — replay from your own client.** Log in with the same credentials from the script, call the read endpoint, and diff against what the app shows. No proxy needed; tells you the schema but not the write body.

Given the app is Flutter AOT, Option 1 usually works because Dart's `HttpClient` honours the system proxy and user CA (no pinning seen in the string table — no `certificate pinning`/`ssl_pinning` symbols were found). Try it first.

---

## 9. What was verified vs. what remains unknown

**Verified on-device (2026-10-27):**
- The TOU **write** is `POST /charging_hes_svc/device_command` with **plain JSON** (no envelope)
  and the three field-type quirks in §11. A whole-hour `23:00–05:00` plan was written and read
  back exactly. An older `23:30–05:30` test also read back exactly from the cloud, but the device
  floored it to `23:00–05:00`.
- The plan is **site-scoped**; the inverter owns `main_sn`, battery modules do not.
- **MQTT credentials are plaintext** when requested in plaintext; mTLS on 8883 works with a
  vanilla client; telemetry arrives as JSON + base64 `trans` every **~5 s** (§3.4).
- The app's home screen uses **push + a 30 s REST reconciliation poll** (`0x1c9c380` µs).
- `client_public_key` = `IV ‖ AES-128-CBC(preset, hex(P-256 point))`; the body key lives in
  packer-encrypted preferences; `X-Signature` is an HMAC whose key/layout were not recovered (§13).
- `update_hes_utility_rate_plan` is **404**; the singular `get_utility_rate_plan` is **404**.
- `get_site_detail` carries metadata only — no live watts.

**Still unknown (must be tested, not assumed):**
- ~~Whether the **inverter firmware** honours `:30`.~~ **Resolved: no.** The cloud stores it,
  but the device floors TOU boundaries to the hour (`23:30→23:00`, `05:30→05:00`); see
  [`docs/FIRMWARE-LIMITS.md`](docs/FIRMWARE-LIMITS.md).
- Whether TOU's load-aware modulation is preserved when the plan is written by a third-party
  client. Mode stays TOU, so it should be — verify ramp on the first cycle.
- The exact `X-Signature` construction and the session-key extraction path.
- Whether the live REST endpoints answer plaintext when asked in plaintext (MQTT made this moot).
- Multi-inverter master/slave selection for `main_sn`.
- Long-term stability of these endpoints across app updates.

## 10. Legal / ToS note

Anker's ToS prohibits reverse engineering and provides **no public cloud API**. The only officially documented third-party interface is **local Modbus TCP**, and Anker's own Home Assistant integration is Modbus-only. Using your own credentials against the app's private API is not a granted right and can break at any time (it has before: `param_type 9` was killed in Jul 2025). Practical risk is account-level (rate-limit/captcha/token invalidation), not legal action against a hobbyist — but do not build a product or share credentials, and prefer Modbus where it can do the job.

---

## 11. Verified write path (proven 2026-10-27)

The TOU plan can be written **without the app's ECDH envelope**. `device_command` accepts plain
JSON — the `x-encryption-info` headers are optional — and stores the exact `HH:mm` strings in the
cloud. The device, however, executes **whole hours only**.

```
POST /charging_hes_svc/device_command
{
  "station_id": "<site uuid>",
  "main_sn": null,
  "time": "2026-10-27 20:19:18",        // formatted local time, NOT unix
  "utility_rate_plan": { … peak_sessions, existing ids kept, 0 for new rows … },
  "ack_peak_valley": null,
  "accuracy": 5,
  "electricity_strategy": { "mode": 1 },
  "price_source_type": 5,
  "price_source_sub_type": 51,
  "version": "1.0.0"
}
→ 200 {"code":0,"msg":"success!"}
```

This is the **exact payload the Android app sends**, captured from memory on 2026-10-27. Earlier
minimal bodies (`accuracy:1`, `ack_peak_valley:1`, `main_sn:<sn>`, unix `time`, ids copied from
the read) also returned `200` but silently rounded prices to **one** decimal. The app shape
stores **two** decimals: `0.0762 → 0.07`, `0.3123 → 0.31`, `sell 0.12` preserved.

**Read-back is eventually consistent.** An immediate `get_plan` after a successful write often returns the **previous** plan; after ~3 s it returns the new one. The writer must poll for a few seconds before treating a difference as rejection, otherwise it rolls back a perfectly good save.

**Evidence:** writing whole-hour `23:00–05:00` / `05:00–23:00` and re-reading
`get_utility_rate_plans` returns exactly those strings for both day types. An older
`23:30–05:30` write also read back exactly, but the device applied **23:00–05:00** — that is the
observation behind the whole-hour limit, not a recommendation to use `:30`.

**Resolved 2026-10-28:** the response echoed integer hours (`S:23, E:5`) for a reason. A stored `23:30`/`05:30` TOU plan switched at **23:00**/**05:00**; the device floors TOU boundaries to the hour. See [`docs/FIRMWARE-LIMITS.md`](docs/FIRMWARE-LIMITS.md).

## 12. Live telemetry contract (implemented)

The web app consumes the frame from §3.4 and exposes a normalised snapshot.

| Field | Meaning | Unit |
|---|---|---|
| `pp` | PV production | W |
| `gp` | grid power (import/export) | W |
| `bp` | battery power (charge/discharge) | W |
| `lp` | house load | W |
| `p2lp` / `p2bp` / `p2gp` | PV → load / battery / grid | W |
| `g2lp` / `g2bp` | grid → load / battery | W |
| `b2lp` / `b2gp` | battery → load / grid | W |
| `soc` | state of charge | % |
| `ps` / `gs` / `bs` / `es` | PV / grid / battery / EMS state | enum |
| `localTime` | inverter clock at publish | local string |

The flow fields are **non-negative per edge**, which is why the UI can animate direction without inferring signs from `gp`/`bp`.

> **Update 2026-10-29 — configurable cadence.** The original implementation fixed the feed at 5 s and a 30 s stale threshold. It now supports a 5–60 s drawer slider. The 5 s setting keeps the proven `60 × 5` request; above 5 s the server asks for `count=2` and renews after the second frame. Stale is `max(30, 1.5 × interval + 5)` (30/50/95 s at 5/30/60 s). A fresh wake in flight is shown as `Refreshing` after a 2 s notice grace, while an overdue frame stays `Stale` and keeps retrying. The historical 30 s value below describes the original 5 s feed, not a universal constant. The wall panel's older 5 s build may still wake the device; its faster frames are treated as live and suppress our own wake.

**Client behaviour**

- one connection per signed-in session, started lazily on first read;
- certificates fetched with the user's own token, written to a `0700` temp dir, deleted on stop;
- subscribe-only except for the read-only `cmd:9` realtime wake; reconnect backoff 5 → 60 s;
- snapshot kept in memory; `age_s`, `waking`, `waking_notice` and `stale` computed per read from the selected cadence;
- the browser checks the local server at one third of the MQTT interval, with a 2 s floor; Anker sees no polling at all.

**Measured upstream load:** ~12 inbound messages/minute on one socket at the 5 s setting. At 60 s the confirmed two-frame shape delivers about one frame per minute. The Android app additionally issues a REST poll every 30 s, so the 5 s feed is strictly lighter; the slower settings are lighter still.

---

## 13. Encryption — what was learned, what remains closed

### 13.1 Request envelope

Most app calls carry:

```
x-encryption-info: algo_ecdh
x-key-ident: <32 hex>
x-replay-info: replay
x-request-once: <32 hex>
x-request-ts: <unix seconds>
x-signature: <64 hex>
body: base64 ciphertext
```

The exchange is `POST /openapi/oauth/key/exchange` with `{"client_public_key": "<base64>"}`.

### 13.2 `client_public_key` — solved

Decoding the captured blob and decrypting it with the app's preset key yields a **130-character hex string starting `04`** — an uncompressed P-256 point. The format is:

```
base64( IV(16) || AES-128-CBC(preset_key, IV, PKCS7(hex(uncompressed_p256_point))) )
```

This was verified by decrypting a live capture: block 1 is the IV, blocks 2+ decrypt cleanly to ASCII hex. The preset key is a **hardcoded 16-byte constant** in `EncryptionUtilImpl` (not reproduced here; two such constants exist in the class).

### 13.3 Signature — not recovered

`EncryptionUtilImpl::getSignature` builds two interpolated strings and calls `Encryption::hMacEncrypt`, then `bytesToHexString`. The HMAC **key and message layout were not recovered**. Brute-forcing the captured signature over key variants (raw/hex preset) × message orders (ts, once, body permutations) × SHA-256/SHA-1/MD5 produced no match.

### 13.4 Session key lives in app-private storage

`getAesKey()` calls `SpUtil.getAeskey()` — the body key is the app's own stored key, not the preset. The packer (`ijiami`) encrypts the entire `shared_prefs` store, so even a `android:debuggable="true"` rebuild plus `run-as` yields ciphertext (`akiot_default.xml`, `FlutterSharedPreferences.xml`). Reading the key would require runtime instrumentation (Frida gadget) or defeating the packer.

### 13.5 Exchange probe results

| Attempt | Result |
|---|---|
| Valid `client_public_key`, empty signature, no ident | `400 field "X-Key-Ident" is not set` |
| Same, random 32-hex ident | `403` |
| HMAC/MD5 candidates with the preset key | `462` (WAF/anti-bot) |

So the exchange is gated on an ident the app already holds plus a valid signature.

### 13.6 Consequence: the envelope is optional for the write path

`device_command` accepts **plain JSON** and answers plain JSON. The server's validation errors reveal the schema (`time` must be a string, `electricity_strategy` an object, `ack_peak_valley` an int). This is why the TOU editor can work without ever decrypting the app's traffic.

The **read** endpoints used by the live UI (`get_system_running_info`, `get_energy_statistics`, `get_device_card_list`) return encrypted `data` in the capture. Whether they answer plaintext when asked in plaintext was not established — the MQTT route made it unnecessary. MQTT telemetry itself is **plaintext** (base64 JSON), which is what the live card uses.

---

## 14. Endpoint inventory

| Method | Path | Envelope? | Purpose | Status |
|---|---|---|---|---|
| POST | `/passport/login` | password field encrypted | sign in | verified |
| POST | `/power_service/v1/site/get_site_list` | no | sites for account | verified |
| POST | `/power_service/v1/site/list_user_devices` | no | devices in buckets | verified |
| POST | `/power_service/v1/site/get_site_detail` | no | site + device metadata (no live watts) | verified |
| POST | `/charging_hes_svc/get_utility_rate_plans` | no | read TOU plan (`station_id`) | verified |
| POST | `/charging_hes_svc/device_command` | **optional** | write TOU plan | verified |
| POST | `/charging_hes_svc/update_hes_utility_rate_plan` | no | legacy write | **404** |
| POST | `/charging_hes_svc/get_system_running_info` | encrypted resp. | runtime snapshot | seen, unreadable |
| POST | `/charging_hes_svc/get_energy_statistics` | encrypted resp. | energy chart | seen, unreadable |
| POST | `/charging_hes_svc/get_device_card_list` | encrypted resp. | home cards | seen, unreadable |
| POST | `/v1/openapi/devicemanage/get_user_mqtt_info` | **no** | broker + mTLS certs | verified |
| POST | `/openapi/oauth/key/exchange` | n/a | ECDH public key | verified (format) |
| POST | `/power_service/v1/app/device/get_device_home_load` | – | home load | `400` without params |
| POST | `/power_service/v1/site/get_home_load_chart` | – | load chart | `200`, empty with `site_id` only |
| MQTT | `dt/anker_power/<model>/<sn>/#` | **plaintext** | live telemetry | verified |

**Scoping.** The TOU plan belongs to the **site** (`station_id`), not to a device. `list_user_devices`
returns buckets (`home_backup_system_list`, `hardware_list`, `grid_list`, `combiner_box_list`,
`charging_pile_list`, …); the inverter in `home_backup_system_list` owns the plan, while battery
modules are capacity only. Selecting a battery as `main_sn` targets the wrong device — the client
therefore resolves the inverter server-side on every write.

---

## 15. Data structures

### 15.1 TOU plan (read + write shape)

Read it with either:

```
POST /charging_hes_svc/get_device_command      {"station_id": "<site_uuid>", "sn": "<inverter_sn>"}
POST /charging_hes_svc/get_utility_rate_plans  {"station_id": "<site_uuid>", "sn": "<inverter_sn>"}
```

**`sn` is required for full precision.** Without it, `get_utility_rate_plans` returns prices
rounded to **one** decimal (`0.1`); with the inverter serial it returns the stored two-decimal
values (`0.07`, `0.31`, `0.12`). `get_device_command` is the richest plaintext object
(`electricity_strategy`, `advanced_setting`, `utility_rate_plan`, `price_source_type`, …) and is
the preferred read. The plan itself:

```json
{
  "peak_sessions": [{
    "id": 0,
    "name": "Season1",
    "start_time": "YYYY-01-01 00:00:00",
    "end_time": "YYYY-12-01 00:00:00",
    "type": 2,
    "peak_valley_prices": [
      {"id": 0, "buy": 0.07, "sell": 0.12, "energy_unit": "kWh",
       "start_time_period": "00:00", "end_time_period": "05:00",
       "peak_type": 1, "day_type": 2},
      {"id": 0, "buy": 0.31, "sell": 0.12, "energy_unit": "kWh",
       "start_time_period": "05:00", "end_time_period": "23:00",
       "peak_type": 4, "day_type": 2},
      {"id": 0, "buy": 0.07, "sell": 0.12, "energy_unit": "kWh",
       "start_time_period": "23:00", "end_time_period": "24:00",
       "peak_type": 1, "day_type": 2}
    ]
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
| `buy` / `sell` | import / export price, `£/kWh`, **2 decimals, truncated** |
| `start_time_period` / `end_time_period` | `"HH:mm"`; end may be `"24:00"` (or `"23:59"` legacy) |
| `id` | server-assigned on read; the app writes **0** on every row |
| `type` | season type (2 in observed plans) |

**The day is a full 24-hour circle.** The app always presents the first period starting at
`00:00`, the last ending at `24:00`, contiguous periods. A window crossing midnight is therefore
**three** periods, not one:

```
23:00–05:00  ⇒  00:00–05:00  +  05:00–23:00  +  23:00–24:00
```

The web editor enforces this by rasterising the 24 one-hour slots, splitting wraps, resolving
overlaps in favour of the edited row, and coalescing neighbours with the same tariff/price.
The first row's start and the last row's end are fixed labels, not dropdowns; a single period is
therefore locked at 00:00–24:00 and only its tariff and prices are editable.

**One buy and one sell price per tariff type.** Every period sharing a `peak_type` carries the
same `buy` and the same `sell`, across both day types and all seasons. Editing any Super
Off-Peak buy (or sell) updates every Super Off-Peak period; changing a period's tariff adopts
that tariff's existing pair. The editor normalises inconsistent incoming plans (first value
wins) and rejects mixed prices. Both fields are independent and stored to two decimals.

**Prices are stored to two decimals (truncated).** `buy` and `sell` both truncate:
`0.0762 → 0.07`, `0.3123 → 0.31`, `sell 0.1234 → 0.12`. The app displays four decimals locally, but no server response
contains more than two. Minimal write bodies silently rounded to **one** decimal.

`buy` cannot be omitted: a missing or `null` value clears the price. Send the stored values back
(or truncated user edits); the normaliser compares at two decimals.

---

### 15.2 Write body (captured from the app)

```json
{
  "station_id": "<site_uuid>",
  "main_sn": null,
  "time": "YYYY-MM-DD HH:MM:SS",
  "utility_rate_plan": {
    "peak_sessions": [{
      "id": 0, "name": "Season1",
      "start_time": "YYYY-01-01 00:00:00",
      "end_time": "YYYY-12-01 00:00:00",
      "type": 2,
      "peak_valley_prices": [
        {"id": 0, "buy": 0.07, "sell": 0.12, "energy_unit": "kWh",
         "start_time_period": "00:00", "end_time_period": "05:00",
         "peak_type": 1, "day_type": 2},
        {"id": 0, "buy": 0.31, "sell": 0.12, "energy_unit": "kWh",
         "start_time_period": "05:00", "end_time_period": "23:00",
         "peak_type": 4, "day_type": 2},
        {"id": 0, "buy": 0.07, "sell": 0.12, "energy_unit": "kWh",
         "start_time_period": "23:00", "end_time_period": "24:00",
         "peak_type": 1, "day_type": 2}
      ]
    }],
    "price_source_type": 5,
    "price_source_sub_type": 51,
    "price_unit": "£",
    "tou_last_set_type": 51,
    "electricity_strategy_mode": 1,
    "fixed_rate": null,
    "nem_plan_info": null
  },
  "ack_peak_valley": null,
  "accuracy": 5,
  "electricity_strategy": {"mode": 1},
  "price_source_type": 5,
  "price_source_sub_type": 51,
  "version": "1.0.0"
}
```

Why each field matters:

| Field | Correct value | Old/minimal body | Consequence if wrong |
|---|---|---|---|
| `main_sn` | **null** | device serial | plan write lands in a legacy path |
| `time` | **site-local `YYYY-MM-DD HH:MM:SS`** | Unix seconds string | parser takes a different branch |
| `accuracy` | **5** | 1 | prices rounded to 1 dp |
| `ack_peak_valley` | **null** | 1 | commit semantics differ; app sends null |
| `electricity_strategy` | **`{"mode":1}`**, optionally plus `conserve_percent`/`grid_recharging` | `{}` | strategy fields reset/invalid |
| period `id` | **existing id, 0 for new rows** | ids read from cloud | preserving ids avoids row churn |
| `price_source_type` / `sub_type` | **5 / 51** | absent | pricing source not applied |

`time` uses the site's local timezone (`Europe/London` observed), not UTC.

### 15.3 Live snapshot (`/api/live`)

```json
{"available":true,"soc":61.3,"pp":0,"gp":0,"bp":1080,"lp":1080,
 "p2lp":0,"p2bp":0,"p2gp":0,"g2lp":0,"g2bp":0,"b2lp":1080,"b2gp":0,
 "ps":0,"gs":0,"bs":2,"es":1,"local_time":"…","ts":1700000000,
 "age_s":1,"stale":false}
```

---

## 16. Open questions

1. **Does the inverter honour `:30`?** **Resolved: no.** The cloud stores it, but a live TOU plan with `23:30`/`05:30` switched at `23:00`/`05:00` — the device floors to the previous hour. See [`docs/FIRMWARE-LIMITS.md`](docs/FIRMWARE-LIMITS.md).
2. **Signature scheme.** Key and message layout for `X-Signature` remain unknown.
3. **Session key extraction.** The body key sits behind packer-encrypted preferences.
4. **Plaintext reads.** Whether `get_system_running_info` answers plaintext when asked in plaintext is untested (MQTT removed the need).
5. **Multi-inverter master selection.** With master/slave pairs, which serial must own `main_sn` is not verified.
6. **`ack_peak_valley` semantics.** The app sends `null`; older docs claiming an int were wrong.
   Its meaning is still unknown.
7. **Price precision is resolved.** The server stores two decimals, truncated (7.62p → 7p); the
   app's four decimals are local display only.

## 17. Capturing the app's plaintext payload (method)

The HTTP body is encrypted at the app layer, so a TLS MITM only yields ciphertext. Plaintext was
recovered by instrumenting the app itself. This section records the method so it can be repeated
on future versions.

**Tooling.** Frida 17.24.3, `frida-gadget-17.24.3-android-arm64.so.xz`, plus `frida-tools`.

**APK preparation.**
1. Decode the base APK and add `lib/arm64-v8a/libgadget.so` and a `libgadget.config.so`
   (`{"interaction":{"type":"listen","address":"0.0.0.0","port":27042}}`).
2. Set `android:extractNativeLibs="true"` so the library is extracted at install.
3. Patch the app component factory's `instantiateApplication` to call
   `System.loadLibrary("gadget")` before the real Application is created. Patching the
   Application's `attachBaseContext`/`onCreate` was not sufficient — the shell delegates to a
   decrypted Application and those methods never ran.
4. Rebuild, zipalign, re-sign, install with the original split APKs.

**Packer obstacle.** The shell's `SecurityCheckThread` calls a Flutter `EventChannel` off the main
thread and throws `Methods marked with @UiThread must be executed on the main thread`. That kills
the process a couple of seconds after start. Fix: a tiny smali
`Thread.UncaughtExceptionHandler` installed before loading the gadget that logs and swallows the
exception. The security thread dies; the UI survives. (Swallow only this thread in production;
a global swallow hides real crashes.)

**Gadget limitations observed.**
- This gadget build exposed **no Java bridge** (`typeof Java === "undefined"`), so Java hooks and
  `MethodChannel` interception were unavailable.
- Frida 17 removed `Module.findExportByName`; use `Module.getGlobalExportByName` /
  `Process.getModuleByName`.
- A `memcpy`/`memmove` hook that inspected every buffer was far too hot and ANR'd the UI.
- Hooking system `EVP_EncryptUpdate` / `AES_cbc_encrypt` produced no hits: the request cipher is
  not OpenSSL in this build.

**Working capture.** A persistent Frida session scanned readable anonymous memory for the ASCII
marker `peak_valley_prices` every 2.5 s:

```js
var hits = Memory.scanSync(range.base, range.size, '70 65 61 6b ...');
hits.forEach(h => send(h.address.readUtf8String(4000)));
```

The working script is `poc/gadget_scan.js`. The saved draft appeared in memory as both an
encoded JSON string and a Dart map `toString`.
The dump contains the complete `device_command` body (§15.2), including the four-decimal UI
values and the exact `accuracy`/`ack`/`main_sn` fields. The scan is read-only; it sends nothing.

**Cleanup.** Uninstall the gadget build, stop the Frida/mitm/MQTT jobs, clear the proxy, remove
the user CA and downloaded certificates, re-enable mobile data.

## Appendix A — evidence index

| Finding | Where |
|---|---|
| 13,892-byte dex, `s.h.e.l.l.*` classes | `unzip -l base.apk`; jadx output |
| Flutter AOT, Dart 3.11.0 | `libapp.so` header + blutter |
| `ankerpower-api*.anker.com`, `aiot-api-cn` | string table, `libapp.str.txt` |
| `/charging_hes_svc/*` endpoints | `libapp.str.txt` |
| `peak_sessions`, `peak_valley_prices`, `start_time_period`, `"23:59"`, `/kWh` | contiguous string cluster at `pp+0x8ad30…0x8ad90` |
| `PeakValleyPrices.toJson` verbatim copy | `asm/charging/model/<model>/<model>_device_command_model.dart:2885+` |
| `<model>_device_command_model.dart` | `asm/.../<model>/.../config_data/` |
| `action_set_tou_system_params` | `libapp.str.txt` (84 `action_*` strings) |
| Modbus strings + `E1133` | `libapp.str.txt` |
| MQTT topics / mTLS | `libapp.str.txt` + `anker-solix-api` source |
| MQTT credentials returned plaintext | live `get_user_mqtt_info` probe |
| MQTT telemetry is base64 JSON, ~5 s | live subscribe, 25 s capture |
| 30 s home refresh | `Duration` `0x1c9c380` µs in `device_main_logic.dart` |
| `device_command` accepts plain JSON | live write, `200 {"code":0}` + read-back |
| `client_public_key` = IV‖AES-CBC(hex P-256) | decrypted captured exchange blob |
| Gadget payload capture | Frida `Memory.scanSync` for `peak_valley_prices` |
| Exact `device_command` fields (`accuracy:5`, null `ack`/`main_sn`, id handling) | captured body + live probes |
| Prices stored 2 dp truncated (`0.0762→0.07`, `0.3123→0.31`) | write + `get_device_command` read-back |
| Full-day 00:00–24:00 rule, wrap split into 3 periods | app UI behaviour + captured plan |
| `day_type` 2=weekday, 1=weekend; keep ids on edits, 0 for inserts | anker-solix-api issue #313 live probes |

## Appendix B — artifacts on disk
- `poc/anker_tou.py` — reference client: login, discover site/device, dump plan, write 30-min plan, verify, rollback.
- `poc/README.md` — how to run it safely.
- `app/live.py` — per-session MQTT feed and frame parser.
- `app/README.md` — deployment and security model.
- `poc/gadget_scan.js` — Frida gadget memory scanner used to capture the plaintext payload.
- `patch/gadget_decoded/` — instrumented APK sources (gadget load + exception handler); research only.
- `report.html` — this document as a readable page.
