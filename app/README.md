# Anker SOLIX X1 — Time-of-Use editor + live monitor

A self-hosted web app that edits the X1 **Time of Use** plan at **30-minute** granularity and
shows a live energy-flow card. It talks to Anker's cloud with **your own account**; nothing
about a particular user, site or device is baked in.

Read [`../REPORT.md`](../REPORT.md) for the protocol evidence behind every call.

```
app/
  anker_client.py   protocol client: login, sites, devices, plan, write, validation
  live.py           per-session MQTT feed (read-only, mTLS)
  server.py         stdlib HTTP server, sessions, write/verify/rollback
  static/           index.html + app.js (vanilla, no build step)
```

---

## 1. Quick start

```sh
python3 -m pip install --user requests cryptography paho-mqtt
./pandacode-run.sh                 # http://127.0.0.1:8770
```

Sign in with your Anker account. Sites and devices are discovered from the account; the plan is
read from the cloud; the live card starts automatically.

---

## 2. What is derived, never hardcoded

| Value | Source |
|---|---|
| Region / base URL | country chosen at sign-in |
| Sites | `get_site_list` |
| Inverter + battery modules | `get_site_device_list` / device buckets, classified by model prefix |
| TOU plan | `charging_hes_svc/get_utility_rate_plans` (site-scoped) |
| Live power | `get_user_mqtt_info` → MQTT `dt/anker_power/<model>/<sn>/#` |
| Write target | the **inverter** on the selected site, resolved server-side on every save |

`main_sn` is always an inverter, classified by model prefix. Battery modules are shown as
capacity, never offered as a plan target.

---

## 3. Live card

- **Transport:** one MQTT over TLS connection per signed-in session, mTLS with certificates
  fetched via `get_user_mqtt_info`, **subscribe-only**.
- **Cadence:** the inverter publishes an `ems` frame every ~5 s. No polling.
- **Payload:** base64 `trans` → JSON → `pp` (PV), `gp` (grid), `bp` (battery), `lp` (load),
  `p2lp`/`p2bp`/`p2gp`/`g2lp`/`g2bp`/`b2lp`/`b2gp` (flow edges), `soc`.
- **Caching:** the browser polls **our** `/api/live` every 2 s; that never reaches Anker.
- **Lifecycle:** feed starts lazily on first `/api/live`, stops on logout, idle expiry or
  auth failure. Certificates live in a `0700` temp dir that is deleted on stop.
- **Failure:** reconnect backoff 5 → 60 s; the UI shows `stale` after 30 s without a frame.

Upstream load is strictly lower than the Android app, which additionally polls REST every 30 s.

---

## 4. Editor

After sign-in you land on the **Home** tab (live energy flow). The editor lives on the **Plan**
tab; the live feed pauses while you are there and resumes on return.

1. **Site** selector when the account has more than one site.
2. **Main inverter** shown read-only; a selector appears only with multiple inverters.
3. **Seasons** with month ranges; **Weekday/Weekend** tabs; up to 4 periods each.
4. **Periods**: tariff, start, end, price — 30-minute steps. Changing an end moves the next
   period's start (and vice versa), so schedules stay contiguous.
   **Apply to both days** is on by default: every edit, add and delete applies to weekday and
   weekend together. Turn it off to edit the two day types independently.
5. **Timeline**: 48 half-hour cells coloured by tariff; gaps and overlaps are obvious.
   The editor enforces the app's full-day rule: the first period starts at **00:00**, the last
   ends at **24:00**, and periods are contiguous. A wrap such as 23:30–05:30 is split into
   00:00–05:30 and 23:30–24:00; deleting a period extends its neighbour; adding one splits the
   final period. The first period's start (`00:00`) and the last period's end (`24:00`) are
   fixed and cannot be changed; with a single period both are locked. Each tariff also has a
   **single buy and single sell price** — editing any
   Super Off-Peak buy or sell updates every Super Off-Peak period across both days and all
   seasons.
6. **Changes**: live diff; nothing is sent until you review and acknowledge.
7. **Save**: snapshot → write → read back → auto-rollback on mismatch.

Only plan fields are writable. Controller state (`price_type`, mode, dynamic pricing) is
rejected server-side before any request.

### Write path

`POST /charging_hes_svc/device_command` accepts **plaintext JSON** (no ECDH envelope):

```json
{"station_id":"…","main_sn":null,"time":"YYYY-MM-DD HH:MM:SS",
 "utility_rate_plan":{…, every period id 0},"ack_peak_valley":null,
 "accuracy":5,"electricity_strategy":{"mode":1},
 "price_source_type":5,"price_source_sub_type":51,"version":"1.0.0"}
```

Read the plan with `get_device_command` and the inverter serial — omitting `sn` returns
one-decimal rounded prices. Captured verbatim from the Android app (full schema: `REPORT.md` §15.2). The nulls,
`accuracy:5`, `{mode:1}` and formatted `time` matter: the older minimal body returned 200 but
silently rounded prices to **one** decimal. This shape stores **two** decimals
(`0.0762 → 0.07`, `0.3123 → 0.31`).

**How the payload was captured.** TLS MITM only shows the app-encrypted body. We loaded a Frida
gadget into a re-signed, debuggable build, installed a smali uncaught-exception handler to survive
the packer's `SecurityCheckThread`, then scanned readable memory for `peak_valley_prices` while
the app saved. The full method is in `REPORT.md` §17.

---

## 5. Security model

| Concern | Handling |
|---|---|
| Credentials | Used once in memory; password reference cleared. Never written to disk or logged. |
| Session | `HttpOnly; SameSite=Strict` cookie → in-process dict; optional AES-256-GCM record at `~/.anker-tou/session.bin` (`0600`). |
| CSRF | SameSite=Strict + `Origin` check + required `X-Anker-Client: 1` header; CORS disabled. |
| Headers | CSP (`default-src 'self'`), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`. |
| Brute force | 8 login attempts per IP / 5 min → 429. |
| Logging | Access log off; errors log method + path only; bodies/headers/tokens never logged. |
| Static files | Path traversal rejected; only known extensions served. |
| Disk | `~/.anker-tou/` `0700`; key/session/certs `0600`; plan snapshots under `~/.anker-tou/captures`. |
| Live certs | Temp dir per session, deleted on stop; never committed. |

**Not protected:** a compromised host account can read the session key. There is no 2FA.

---

## 6. Production deployment

The app is a single Python process with no external database. For a public instance:

1. **Terminate TLS** at a reverse proxy (Caddy/nginx) and forward to `127.0.0.1:8770`.
2. Run with `--secure-cookies` so the session cookie is `Secure`.
3. Prefer `--no-persist` on shared instances: no token is written to disk and each restart
   requires sign-in.
4. Keep `--host 127.0.0.1` unless the proxy is on another host; never expose plain HTTP.
5. One process only. Sessions and live feeds are in memory; multiple workers would each hold
   their own MQTT connections and Anker sessions.
6. Add proxy-level rate limits on `/api/login` and a request-body cap.
7. Watch MQTT connections: each signed-in user holds one. Anker may cap concurrent sessions
   per account; the app already reconnects with backoff.

Example (Caddy):

```
tou.example.com {
    reverse_proxy 127.0.0.1:8770
    encode gzip
}
```

```
python3 app/server.py --host 127.0.0.1 --port 8770 --secure-cookies --no-persist
```

### Operational notes

- Anker's API is undocumented and may change without notice. Treat failures as expected.
- A user can be signed in on only one client at a time in practice; signing in on the phone
  can invalidate the web session (`26084`). The app then returns to sign-in.
- The live feed needs `paho-mqtt`; if it is missing the editor still works and the card shows
  `unavailable`.

---

## 7. Configuration

| Flag | Default | Meaning |
|---|---|---|
| `--host` | `127.0.0.1` | bind address |
| `--port` | `8770` | bind port |
| `--no-persist` | off | memory-only sessions |
| `--secure-cookies` | off | add `Secure` to the session cookie |
| `--use-keychain` | off | store the at-rest key in the macOS Keychain |
| `--allow-half-hours` | off | retained for compatibility; half-hours are supported |

State lives in `~/.anker-tou/` (override with `ANKER_TOU_HOME` if set by your deployment).

---

## 8. Diagnostics

`GET /api/diag` (authenticated) returns the last 60 requests: method, path, status, duration,
redacted error. The UI shows it under **Diagnostics**. It contains no bodies, headers or tokens.

| Situation | Behaviour |
|---|---|
| Bad credentials / expired token | 401, back to sign-in |
| Token kicked (`26084/26085`) | auth error, session and feed torn down |
| Network / 5xx | up to 3 attempts with backoff |
| 429 | no retry storm; asks you to wait |
| Validation | caught in the browser, re-checked server-side |
| Read-back mismatch | polls up to ~6 s (cloud is eventually consistent), then 409 + automatic restore + both plans |
| Live feed down | card shows `stale`/`unavailable`; editor unaffected |

---

## 9. Known limitations

- **Device-side half-hour support is not yet proven.** The cloud stores `23:30`; the
  device-facing preview echoes integer hours. Watch the first charge cycle.
- **Anker's API is private.** Endpoints can change; there is no SLA.
- **Plan shape** assumes `peak_sessions[].peak_valley_prices[]`. A different firmware shape
  shows no periods — re-derive rather than force it.
- **Max 4 periods per day type**, matching the app.
- **No power ceiling control** for consumer accounts; grid-charge limit is installer-only.
- **Prices: the app shows 4 decimals, the server stores 2.** `get_device_command` returns
  `buy`/`sell` truncated to two decimals; the `0.0762` shown in the Android UI is local state.
  The editor truncates to two decimals on input and sends the values back unchanged, so a
  schedule save no longer alters stored prices. Omitting `buy` still clears it — never omit.
- **Live data is per-account**; a site with no inverter exposes no feed.
