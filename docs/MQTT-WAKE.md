# What wakes the live MQTT stream

**Status:** investigation (2026-10-28). Raw captures live under
`captures/` and are **gitignored**; this page contains sanitized findings only.

> **Correction (2026-10-28, later):** the live dashboard was broken by our own
> exact-topic change, not by the broker. The realtime stream is
> `dt/anker_power/<pn>/<sn>/troubleshot_info/ems_second`, arriving every ~5 s
> while the device stream is active. Our exact-topic subscription omitted `ems_second`.
> `app/live.py` now includes it and the dashboard shows live data again.
>
> The `cmd:10000` / cloud `cmd:9` material below is the app’s separate aggregated
> control channel. It is useful context, but **not required** for the web feed.

## Observation

With the web page open, the live card stays at “waiting for telemetry”. Opening the
Android app on an independent phone immediately starts frames on the laptop. The two
clients share no local state, so the phone causes the cloud/device to publish.

## What the app actually does

The app-launch HTTP sequence contains no “start streaming” endpoint:

```
get_user_mqtt_info
get_system_running_info
get_user_fault_info
get_device_card_list
report_device_data
```

The MQTT log from the instrumented app shows the subscriptions it creates:

```
cmd/anker_power/<account_id>-anker_power/req       # cloud -> app data/commands
cmd/anker_power/<account_id>-anker_power/res       # cloud responses
cmd/anker_power/<pn>/<sn>/app/res                   # command responses
dt/anker_power/<pn>/<sn>/troubleshot_info/ems_second # realtime telemetry (~5 s)
dt/anker_power/<pn>/<sn>/basic_info
dt/anker_power/<pn>/<sn>/param_info
dt/anker_power/<pn>/<sn>/state_info
dt/anker_power/<battery_pn>/<battery_sn>/param_info # battery telemetry
```

The live site snapshot arrives on the **`cmd/.../<account>-anker_power/req`** topic,
while the ordinary `dt/.../basic_info`, `param_info` and `state_info` subscriptions also
succeed. A captured `cmd` frame had:

- `head.client_id: "cloud"`
- `head.cmd: 10000`
- `payload`: base64-encoded JSON containing grid/PV/battery/load values and the
  site/device card data

So the app is not merely a passive `dt` subscriber. It receives a cloud-pushed
snapshot on a `cmd` topic. Our web feed currently subscribes only to
`dt/anker_power/<pn>/<sn>/#`, which may be the missing piece.

## `realtime_trigger` / `0x0057`

The independent [`anker-solix-api`](https://github.com/thomluther/anker-solix-api)
project implements an MQTT `realtime_trigger` (message type `0057`) that enables
periodic live publishing for 30–600 seconds.

The app log also contains `CmdInvoker sendCommand()` entries for `cmdID = 0x0057`
against the inverter and batteries. In the captured run those calls ended with
`cmdParams: null` / `sendToDevice() cmd is null`, so **`0x0057` was not a successful
trigger in this capture**. No literal `realtime_trigger` or `set_realtime_trigger`
string appeared in the MQTT log.

## Independent subscriber test

Using the account certificate from the local web feed:

- TLS connection succeeds (`CONNACK 0`).
- With arbitrary `thing_name_<random>` client IDs, `cmd/...` and `dt/...`
  subscriptions both return **`SUBACK 128`** (failure).
- With the app's exact generated client-id
  `android-anker_power-<account>-<certificate_id>`:
  - exact `dt/anker_power/<pn>/<sn>/basic_info` → **`SUBACK 0`**
  - exact `dt/.../param_info` and `dt/.../state_info` → **`SUBACK 0`**
  - wildcard `dt/anker_power/<pn>/<sn>/#` → **`SUBACK 128`**
  - wildcard `dt/anker_power/+/+/#` → **`SUBACK 128`**
  - `cmd/anker_power/<thing>/req` → **`SUBACK 128`**
- Appending a **unique** suffix (`-web-<instance>`) keeps the exact `dt` topics
  authorized. The feed can therefore coexist with the phone app instead of
  taking over its client-id. A fixed suffix can collide with a stale broker
  session after a restart and leave the feed stuck in `connecting`.

The certificate is valid until 2035 and matches the app's current certificate, so
this is **not** expiry. The missing pieces were the **client-id ACL** and the
**wildcard rejection**, not the certificate.

`app/live.py` now builds the client-id from `app_name`, `user_id` and
`certificate_id`, adds a unique `-web-<instance>` suffix, subscribes to the four
exact `dt` topics including `troubleshot_info/ems_second`, and reports
`mqtt subscription denied` if any SUBACK is 128 instead of waiting silently.

## How to capture the fresh identity

Use the helper (prompts for the password; does not print secrets):

```sh
cd "$(git rev-parse --show-toplevel)"
./poc/fetch_mqtt_info.sh --email you@example.com --country GB
```

The wrapper forces `/usr/bin/python3` (the interpreter with `cryptography`).
If `python3` points at `/usr/local/bin/python3` on this Mac, the script version
will fail with `ModuleNotFoundError: cryptography`; use the wrapper.

It writes a fresh directory under `captures/mqtt-info-<utc>/` containing:

```
info.json   full get_user_mqtt_info response
ca.pem      AWS root CA
cert.pem    current client certificate
key.pem     current private key
meta.json   endpoint, thing_name, certificate_id, user_id
```

The directory is `0700`, files are `0600`, and `captures/` is gitignored.
Logging in this way may invalidate the phone app session; that is expected.

Then test the current certificate against `cmd` before changing the web app:

```text
client-id: android-<app_name>-<user_id>-<certificate_id>-web-<instance>
subscribe: dt/anker_power/<pn>/<sn>/troubleshot_info/ems_second
wake:      cmd/anker_power/<pn>/<sn>/req
```

If those return `SUBACK 0` and frames, the web feed can use `cmd`; if they still
return `128`, the app is using a separate identity/policy and we should not
pursue this route.

## Fresh-identity result

A fresh `get_user_mqtt_info` capture was obtained under
`captures/mqtt-info-20261028T171650Z/`. Its `certificate_id` differs from the
client-id captured from the running app, and the certificate itself is the same
certificate (same subject/serial).

Testing the fresh identity produced:

- exact `dt/.../basic_info`, `param_info`, `state_info` → `SUBACK 0`
- exact `cmd/anker_power/<thing>/req` and `/res` → `SUBACK 128`

The first test still produced no frames because it subscribed to the wrong
`dt` topics: the realtime payload is on
`dt/.../troubleshot_info/ems_second`. A wildcard test proved that topic receives
~5 s EMS frames while the app is open. The broker ACL rejects wildcards, so the
exact `ems_second` topic must be listed explicitly.

This rules out “stale certificate” and “wrong certificate_id” as the cause. The
app’s `cmd:10000` subscription is a separate aggregated channel and is not needed
for the dashboard.

## Conclusion

The realtime source is:

```text
dt/anker_power/<pn>/<sn>/troubleshot_info/ems_second
```

Opening the app causes the cloud to send the `cmd:9` / `cmdId:4` trigger; the device then
publishes `ems_second` frames every ~5 s. The web feed receives them when it
subscribes to that exact topic. `cmd:10000` is the app’s separate aggregated UI
channel and is not needed for the dashboard.

The earlier “waiting” regression was caused by our exact-topic subscription
omitting `ems_second`. It is fixed in `app/live.py`, and the live endpoint returns
`available: true` with fresh values again.

## Automatic wake (implemented)

If no frame has arrived **12 seconds** after MQTT connects, or the latest frame
is more than **30 seconds** old, `LiveFeed` publishes the X1 request that the app
itself causes the cloud to send:

```text
topic: cmd/anker_power/<pn>/<sn>/req
head.cmd: 9
data.cmdId: 4
config: { "count": 60, "interval": 5 }
```

That is 60 samples at 5 s intervals = a **5-minute** realtime window. The request
is read-only and does not change the plan or controller mode. A **240-second
cooldown** prevents repeated triggering; the dashboard status shows
**“Requesting live data”** while waiting.

Verified against the live broker with the phone app **stopped**: publishing this
frame produced multiple `ems_second` frames within seconds. The app is no longer
required as the trigger, though opening it remains a valid fallback.

Local Modbus was considered but **dropped**: it depends on a guaranteed local
network path and the X1’s Modbus support is unconfirmed. The MQTT feed with the
automatic wake is the current live source.

## UI no-data placeholder (implemented)

`build_home_view()` now requires at least one numeric power/flow field before it
returns an available view. An EMS envelope with empty/unknown data is treated as
unavailable with `kw: null` and the label **No telemetry data**, so the dashboard
renders its `—` placeholder instead of four `0.00 kW` cards. Numeric zeroes are
still valid idle telemetry and remain live.

`parse_frame()` rejects empty/non-dict EMS `data` objects before they can replace
the last good snapshot. Regression tests cover empty frames and true zero values.

## mitmproxy attempt

mitmproxy successfully intercepted the app’s HTTP(S) API traffic after the
instrumented APK’s `nsc_mitm` network-security config trusted the user CA. It
confirmed the app was polling `get_system_running_info`, `get_device_card_list`,
`get_station_config_and_status` and `report_device_data`; no new HTTP wake
endpoint appeared.

It cannot see the raw MQTT TLS socket on 8883. A global Android HTTP proxy also
caused the app’s Paho MQTT client to fail reconnects, which made the app appear
offline. The proxy was removed; the app’s network validated again.

## Phone instrumentation and cleanup

The app was exercised with a Frida-gadget build (v3.24.3) and a Frida memory /
method hook. That produced the client-id and subscription evidence above. The
phone was restored to the original Anker-signed APK, the HTTP proxy, `adb
reverse`, ADB forwards and `/data/local/tmp/frida-server` were removed, and the
phone was locked.

The mitmproxy user CA remains a manual cleanup item in the phone’s user
certificate store; remove it when no longer needed for captures.

## GitHub hygiene

`captures/` is explicitly gitignored and contains no tracked files. Raw MQTT
logs, API session tokens, certificates, private keys, account ids, site ids and
serials stay local. This page uses placeholders such as `<account>`, `<pn>`,
`<sn>` and `<certificate_id>` only.

## Raw evidence

- `captures/mqtt-trigger-20261028T160923Z.log` (0600, gitignored)
- `captures/mqtt-trigger-20261028T160923Z.meta.json` (0600, gitignored)
- `captures/mqtt-info-20261028T171650Z/` (0700/0600, gitignored)

Sanitize account ids, site ids and serials before copying anything into this page or
a commit.
