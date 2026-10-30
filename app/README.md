# Anker SOLIX X1 — wall dashboard + Time-of-Use editor

A self-hosted web app built for a small wall display, including the Skylight pane. The Home view is a
full-screen, daylight live-energy dashboard; the **Plan editor** is an always-visible burger
button away. It talks to Anker's cloud with **your own account**; nothing about a particular
user, site or device is baked in.

Read [`../REPORT.md`](../REPORT.md) for the protocol evidence behind every call.

```
app/
  anker_client.py     protocol client: login, sites, devices, plan, write, validation
  power_flow.py       pure edge/aggregate parsing -> signed solar/grid/battery/home W
  solar_forecast.py   single-reading half-sine solar model
  battery_estimate.py mode selection, load compensation, charge-cap energy math
  limits.py           dynamic charge-limit extraction and the 6 kW fallback
  home_view.py        pure telemetry -> dashboard view-model mapping (tested)
  live.py             per-session MQTT feed (read-only, mTLS)
  server.py           stdlib HTTP server, sessions, write/verify/rollback
  static/
    index.html        login, Home stage, plan drawer
    home-view.js      formatting, state attributes, clock, flow layout
    css/              base, home, drawer
    assets/           house.svg + house-night.svg (same geometry, day/night palette)
  tests/              unittest: view, solar model, limits, live and server contracts
```

---

## 1. Quick start

```sh
python3 -m pip install --user requests cryptography paho-mqtt tzdata
./pandacode-run.sh                 # http://127.0.0.1:8770
```

Sign in with your Anker account at `/`. Sites and devices are discovered from the account; the
plan is read from the cloud. The dashboard starts as soon as the inverter list is known; the
plan request loads in the background. This is the only UI: there is no demo/preview fixture.

Until telemetry arrives, live cards show `—` and the flow layer is hidden. Until the real plan
and strategy load, plan-specific controls are blank/disabled, not pre-filled with sample data.

---

## 2. What is derived, never hardcoded

| Value | Source |
|---|---|
| Region / base URL | country chosen at sign-in |
| Sites | `get_site_list` (including site timezone when present) |
| Inverter + battery modules | `get_site_device_list` / device buckets, classified by model prefix |
| TOU plan | `charging_hes_svc/get_utility_rate_plans` (site-scoped) |
| Live power | `get_user_mqtt_info` → MQTT `dt/anker_power/<model>/<sn>/#` |
| Write target | the **inverter** on the selected site, resolved server-side on every save |

`main_sn` is always an inverter, classified by model prefix. Battery modules are shown as
capacity, never offered as a plan target.

---

## 3. Wall dashboard (Home)

- **Purpose-built for distance.** One composition built around a large central battery:
  Solar top-left, Grid top-right and Home Load below it (bottom-left in landscape, centred
  in portrait), with the vector house behind the battery and colour-coded flow paths
  connecting the physical elements.
- **Elastic scaling, no build step.** The stage uses one `--u` token
  (`clamp(4.6px, min(0.598vw, 1.063vh), 14px)`), refined in JS on resize. Every card, icon,
  gap and flow stroke is a multiple of it. No `transform: scale()` for the page, so text
  stays crisp. Reference composition is 1672×941; 4:3 and ultrawide are graceful. Solar,
  Grid and Home share one width token for symmetry. Portrait aspect ratios (≤0.95) keep the
  same around-the-house composition with 46%-wide cards, a 28%-wide battery and Home centred
  at the bottom; no stacked fallback. The three cards share one height as well as one width,
  giving the cluster a level top and bottom. Portrait card/battery type and card icons use a
  1.5× scale for readability; phones at 599px or narrower keep the compact 1× sizing.
  Portrait detection uses `(max-aspect-ratio: 95/100)`: Chromium 95 in the Skylight WebView
  does not match the decimal `0.95` form, which silently fell back to the narrow-card layout.
- **Vector-only artwork.** `static/assets/house.svg` is hand-built (walls, roof, solar array,
  windows, trees, lawn, ground). Icons are inline SVG; the sky, sun and hills are inline
  SVG/CSS. No external images, fonts, CDNs or runtime AI calls; the UI uses the browser's
  system sans, which resolves to Roboto in the Skylight WebView.
- **Night scenery without a second theme.** `house-night.svg` is a colour-only copy of the day
  house: same viewBox, paths, positions, opacities and gradients. The night sky reuses the
  day cloud and hill paths and swaps the sun glow for a cool moon glow; only the stars are new
  geometry. Both scenes are stacked, pixel-aligned layers. From **19:00–20:00** the clock
  crossfades day→night and from **06:00–07:00** night→day, using the same once-a-second clock
  as the drawer (two opacity writes per minute, no extra timer, no CSS animation). Outside
  those windows the scene is fully day or fully night; `data-sky` is `day`, `dusk` or `night`.
  The crossfade follows the site timezone, and the night palette is a moonlit sage/blue set
  rather than a dark/black screen: the hills behind the battery caption stay above 4.5:1
  against the page ink, so the card, battery and text colours do not change. The night house
  lights its windows warm; the only cool shadow is the CSS `drop-shadow` on the night image.
- **Live state in one place.** The server normalises raw MQTT into a small `view`:
  `importing/exporting/idle`, `charging/discharging/idle`, clamped SOC, per-edge flow
  activity and `live/waking/stale/waiting/offline`. The browser renders values and sets
  `data-available`, `data-stale`, `data-solar`, `data-grid`, `data-battery`, `data-status`,
  `data-sky` and `data-estimate`.
  The battery accent interpolates with SOC: green above 80%, blending through amber at 30%,
  then red at 5% (smoothstep between stops). Each tone pairs a bright fill with a dark ink
  for the shell outline; the SOC, power and remaining-charge text all use the page ink, so
  they remain legible on the pale headroom and on the fill at every charge level. The battery shimmer uses
  `data-battery`: it travels down while discharging and up while charging. That is the only
  looping motion; fill-height, colours and flow-opacity changes are short transitions. It
  stops when idle/unavailable and is disabled by `prefers-reduced-motion`.
- **Battery ETA is source-aware.** The signed ten-minute battery average decides
  idle/charge/discharge; the instantaneous solar/grid readings decide which formula to use.
  Discharge and grid charging keep the existing linear formulas. Solar charging inside
  07:00–18:00 uses a half-sine curve scaled from the current PV reading, subtracts the
  ten-minute house load, and clips at the runtime inverter cap. The cap comes from the live
  EMS field when present, then the device attribute, then a named **6 kW** fallback; it is
  never a constant in the formula. On the tested A5102 the realtime frame and the attribute
  APIs exposed **no** charge cap, so the 6 kW fallback is the path in use; a just-saved plan
  supplies its written value until the next read. If the curve cannot fill the pack, the
  caption shows the expected SOC at 18:00 (`87% by 18:00`) instead of a fake finish time.
  The model and its limits are in [`docs/SOLAR-ESTIMATE.md`](../docs/SOLAR-ESTIMATE.md).
- **Flows are measured, not guessed.** `home-view.js` measures the solar/grid/home cards and
  the battery shell, then redraws the SVG paths on resize/orientation change. Solar runs
  card → battery; grid runs card ↔ battery (import/export); home runs battery → card when the
  house is consuming. Active paths also carry a slow dotted "walking wire" in the same colour
  as the arrow, moving toward the arrowhead. The battery is the visual hub; charge/discharge
  is shown by its fill and the summary line. The house is background artwork, not a flow node.
- **Cards take their meter's colour.** Each card's top rule, tinted drop shadow and readout
  glow use the same fill as its lit bars: leaf green `#7acc70`, amber `#e6be4a`, blue
  `#447cd6`; red `#b91c1c` and the gray track are unchanged. The card label, kW value, unit
  and icon stay page ink (`#17252f`, the battery percentage's colour), so the number keeps
  maximum contrast. The glow is a dense 1px core in a darker shade of the fill (`#5f9661`,
  `#a18c48`, `#3561a4`, `#862c30`), the fill itself, then two soft blooms at 50% and 22%.
  All four centred layers scale with `--u` and `--type-scale`: 0 offsets read as emission,
  not a drop shadow. The accents are sampled from the calendar chips and battery: the chip
  bodies are 50% tints, so the solid corner and badge are the source colours. A 1px page-ink
  rim (`rgba(23,37,47,.40)`) darkens each lit bar at its own edge — a shade of the fill, not
  a gray outline — and lifts the pale amber/green silhouettes past the 3:1 non-text floor. An
  unavailable card falls back to slate and drops the glow.
- **Home Load meter.** Five segments sit under the Home Load value. Below 2 kW one green
  bar; below 3 kW two green; below 4 kW three amber; below 5 kW four amber; 5 kW and above
  five red. It reads `view.home.w`, stays unlit until a real home reading exists, and its
  accessible meter value reports the bar count plus the kW reading.
- **Solar meter and sky icon.** Five green segments sit under Solar. Any output above zero
  lights one; 2.8 kW and above lights all five, with the intervening bars linear at 0.7 kW
  steps. The same card swaps its sun for a daytime cloud (08:00–20:00) or a night moon when
  solar is not generating. The hour comes from the site timezone.
- **Grid meter.** Five segments sit under Grid. Export uses blue at every level:
  linear from 1 bar above 50 W to 5 bars at 2.8 kW. Import is 1–2 green below 1/2 kW,
  3–4 amber below 5/10 kW, and 5 red at 10 kW or above. Both directions stay unlit below
  50 W so inverter ramp-up/down does not flicker the meter.
- **Stale is quiet but visible.** Cards and battery dim, values keep their last reading, and
  a compact status line under the battery caption becomes amber `Stale · Ns` when the frame
  is older than the cadence's threshold (30 s at 5 s, 50 s at 30 s, 95 s at 60 s). A wake in
  flight shows `Refreshing · Ns` instead, keeping the last values. Unavailable telemetry
  shows `—`, never a fake zero, and hides the flow layer.
- **No summary strip and no weather.** Both were deliberately deferred until an Anker stats
  source exists. The mockup intentionally removes the on-wall clock and site chip; the clock,
  date and site identity now live in the plan drawer header. A status line appears under the
  battery only when telemetry is not live.
- **Clock** uses the site timezone from the account when present, otherwise the browser's
  local time. It updates once a minute (timer ticks every second but only writes on change);
  the same minute change drives the 19:00/06:00 backdrop crossfade, so there is no second
  timer.
- **Kiosk-friendly.** The page never scrolls on Home in landscape; the drawer scrolls
  independently. Fullscreen is available from the drawer. `prefers-reduced-motion` is
  respected from day one.

Transport: one MQTT connection per site+inverter, mTLS. The feed subscribes to the exact
`dt` topics and publishes only the read-only realtime wake request; it never writes a plan
or controller setting. The inverter publishes an `ems` frame at the selected
interval while a window is active. The browser checks **our** `/api/live` at one
third of the MQTT interval, with a 2 s floor (2 s at 5 s, 20 s at 60 s) and
skips hidden tabs; upstream load is lower than the Android app at every cadence
above 5 s.

The **Live refresh** slider in the drawer selects a 5–60 s cadence. The server asks for the
proven 60-frame/5 s window at 5 s and for two frames at a time above 5 s, renewing after the
second frame. A concurrent 5 s wake from the wall panel is allowed: its faster frames are
treated as live and suppress our own wake. The choice is stored in the
`anker_tou_mqtt_s` preference cookie, which survives sign-out and account switches.


---

## 4. Plan editor (burger drawer)

After sign-in the dashboard paints immediately. The **Plan** view is a full-height light sheet
opened by the always-visible top-left burger (or `#plan`), with a backdrop, focus trap,
Escape/backdrop close and focus restoration. The live feed keeps running while it is open.

1. **Site** selector when the account has more than one site; changing it restarts the live
   feed for that site and reloads the plan.
2. **Main inverter** shown read-only; a selector appears only with multiple inverters.
3. **Seasons** with month ranges; **Weekday/Weekend** tabs; up to 4 periods each.
4. **Periods**: tariff, start, end, price — 1-hour steps. Changing an end moves the next
   period's start (and vice versa), so schedules stay contiguous.
   **Apply to both days** is on by default: every edit, add and delete applies to weekday and
   weekend together. Turn it off to edit the two day types independently.
5. **Timeline**: 24 one-hour cells coloured by tariff; gaps and overlaps are obvious.
   The editor enforces the app's full-day rule: the first period starts at **00:00**, the last
   ends at **24:00**, and periods are contiguous. A wrap such as 23:00–05:00 is split into
   00:00–05:00 and 23:00–24:00; deleting a period extends its neighbour; adding one splits the
   final period and gives the new slice the first tariff unused that day, so it is not merged
   straight back. The first period's start and last period's end are fixed. Each tariff also
   has a **single buy and single sell price** — editing any Super Off-Peak buy or sell updates
   every Super Off-Peak period across both days and all seasons.
6. **Changes**: live diff; nothing is sent until you review and acknowledge.
7. **Save**: snapshot → write → read back → auto-rollback on mismatch.
8. **Diagnostics**, result log and sign-out live in the drawer. Errors in the plan load add a
   small amber dot to the burger.

Only plan fields are writable. Controller state (`price_type`, mode, dynamic pricing) is
rejected server-side before any request.

### Write path

`POST /charging_hes_svc/device_command` accepts **plaintext JSON** (no ECDH envelope):

```json
{"station_id":"…","main_sn":null,"time":"YYYY-MM-DD HH:MM:SS",
 "utility_rate_plan":{…, existing ids kept, 0 for new rows},"ack_peak_valley":null,
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
| Refresh preference | Separate `anker_tou_mqtt_s` cookie (`HttpOnly`, one year); never cleared by sign-out; no credential value. |
| CSRF | SameSite=Strict + `Origin` check + required `X-Anker-Client: 1` header; CORS disabled. |
| Headers | CSP (`default-src 'self'`), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store`. |
| Brute force | 8 login attempts per IP / 5 min → 429. |
| Logging | Access log off; errors log method + path only; bodies/headers/tokens never logged. |
| Static files | Path traversal rejected; only known extensions served; image MIME types added for future raster assets. |
| Disk | `~/.anker-tou/` `0700`; key/session/certs `0600`; plan snapshots under `~/.anker-tou/captures`. |
| Live certs | Temp dir per session, deleted on stop; never committed. |
| New UI | No external assets/fonts/CDN and no fixture data. Every static asset is referenced by the Home/editor shell; missing telemetry renders as `—`, never a sample value. |

**Not protected:** a compromised host account can read the session key. There is no 2FA.

---

## 6. Production deployment

The app is a single Python process with no external database. For a public instance:

1. **Terminate TLS** at a reverse proxy (Caddy/nginx) and forward to `127.0.0.1:8770`.
2. Run with `--secure-cookies` so both the session and preference cookies are `Secure`.
3. Prefer `--no-persist` on shared instances: no token is written to disk and each restart
   requires sign-in.
4. Keep `--host 127.0.0.1` unless the proxy is on another host; never expose plain HTTP.
5. One process only. Sessions and live feeds are in memory; multiple workers would each hold
   their own MQTT connections and Anker sessions.
6. Add proxy-level rate limits on `/api/login` and a request-body cap.
7. Watch MQTT connections: each signed-in user holds one per site+inverter, capped at four per
   session. Anker may cap concurrent sessions per account; the app reconnects with backoff.

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
- The live feed needs `paho-mqtt`; if it is missing the editor still works and cards show
  `Unavailable`.
- For a wall frame, use the browser's kiosk/fullscreen mode. Keep the frame on a trusted LAN
  and put the app behind HTTPS if it leaves localhost.

---

## 7. Configuration

| Flag | Default | Meaning |
|---|---|---|
| `--host` | `127.0.0.1` | bind address |
| `--port` | `8770` | bind port |
| `--no-persist` | off | memory-only sessions |
| `--secure-cookies` | off | add `Secure` to both cookies |
| `--use-keychain` | off | store the at-rest key in the macOS Keychain |

State lives in `~/.anker-tou/` (override with `ANKER_TOU_HOME` if set by your deployment).

---

## 8. Diagnostics

`GET /api/diag` (authenticated) returns the last 60 requests: method, path, status, duration,
redacted error. The UI shows it under **Diagnostics** in the drawer. It contains no bodies,
headers or tokens.

| Situation | Behaviour |
|---|---|
| Bad credentials / expired token | 401, back to sign-in |
| Token kicked (`26084/26085`) | auth error, session and feed torn down |
| Network / 5xx | up to 3 attempts with backoff |
| 429 | no retry storm; asks you to wait |
| Validation | caught in the browser, re-checked server-side |
| Read-back mismatch | polls up to ~6 s (cloud is eventually consistent), then 409 + automatic restore + both plans |
| Live feed down | cards and battery show `—` + waiting/offline; flows hide; editor unaffected |
| Live frame older than the cadence threshold | values stay, cards and battery dim, amber `Stale` |
| Wake in flight (up to 60 s) | values stay, dimmed, `Refreshing · Ns`; not `Stale` |

---

## 9. Known limitations

- **The solar ETA is a forecast, not a measurement.** It assumes a fixed 07:00–18:00
  half-sine and a constant house load; outside that window, or below 50 W PV, it falls back
  to the linear battery rate. The charge cap comes from the live frame or device attribute
  when readable, otherwise a 6 kW fallback. It never changes charging.
- **No summary metrics or weather yet.** The previous mockup's "Today's Production / CO₂ /
  Earnings" strip and the weather row are intentionally absent; there is no verified Anker
  stats source in this build.
- **The device only honours whole hours.** The cloud stores `23:30`, but the inverter
  switched at `23:00`; `05:30` became `05:00`. The editor uses hourly slots and floors
  existing `:30` plans on load. See [`docs/FIRMWARE-LIMITS.md`](../docs/FIRMWARE-LIMITS.md).
- **Anker's API is private.** Endpoints can change; there is no SLA.
- **Plan shape** assumes `peak_sessions[].peak_valley_prices[]`. A different firmware shape
  shows no periods — re-derive rather than force it.
- **Max 4 periods per day type**, matching the app.
- **The grid-charge ceiling uses a separate call.** The editor sets
  `set_device_attrs.ac_power_limit` (watts) *before* the plan. If a particular A5102
  firmware ignores it, the ceiling may be BLE-only; verify the value in the Anker app.
- **Prices: the app shows 4 decimals, the server stores 2.** `get_device_command` returns
  `buy`/`sell` truncated to two decimals; the `0.0762` shown in the Android UI is local state.
  The editor truncates to two decimals on input and sends the values back unchanged, so a
  schedule save no longer alters stored prices. Omitting `buy` still clears it — never omit.
- **Live data is per-account**; a site with no inverter exposes no feed. The site timezone
  is used when the account exposes one; otherwise the clock follows the browser.
- **No looping flow/icon animations in this milestone.** The battery shimmer is the only
  looping element; state attributes and icon part classes are ready for a CSS-only M2.
  `prefers-reduced-motion` disables the flow/fill transitions and the shimmer.
