# Anker SOLIX X1 — self-hosted toolkit

The Anker SOLIX X1 is managed through Anker's cloud app. This toolkit puts a **live wall
dashboard** and a **Time-of-Use (TOU) editor** in front of the same private API, using your
own account. It runs on a local machine, NAS or small server.

<p align="center">
  <img src="docs/daylight-dashboard.webp" width="480"
       alt="Daylight wall dashboard: solar 1.75 kW, grid idle, battery 95 percent charging at 1.41 kW, home load 0.34 kW, expected 98 percent by sunset">
</p>
<p align="center"><em>Live dashboard on a portrait wall display. Card surfaces follow the
active meter band; values, units and icons stay page ink.</em></p>

> **Not affiliated with Anker.** This uses an undocumented private API. It can change or
> break without notice. You are responsible for what you write to your system.

## What it does

- **Live wall dashboard.** One composition around a large central battery: solar top-left,
  grid top-right and home load below. The SVG flow paths are measured from the rendered
  cards, so the wires meet the cards at any aspect ratio. The server holds one MQTT subscription
  per site and inverter. Telemetry is read-only; the only publish is the realtime wake
  request. The browser checks the local server at one third of the MQTT interval,
  with a **2 s floor** (2 s at 5 s, 20 s at 60 s).
  A drawer slider selects a **5–60 s** MQTT cadence; the choice persists across sign-out and
  account switches in its own preference cookie. Stale thresholds follow the cadence.
- **Solar-aware battery ETA.** The signed ten-minute battery average sets idle/charge/discharge;
  the ten-minute averaged PV (instant fallback) and instantaneous grid readings choose the
  formula. Discharge and grid charging stay linear. Solar charging between the estimated
  sunrise and sunset (supplied by `app/daylight.py`) anchors a half-sine at that averaged
  PV, subtracts the house load and clips at the runtime inverter cap. The old 0.5-risk curve
  is kept as the low case in the tooltip, so the headline no longer shows a discharge while
  the battery is charging. When the sun cannot fill the pack, the dashboard shows the expected
  SOC at the effective sunset (the estimate, or **18:00** when no location is available)
  instead of a false finish time; a small `under assumed load` note explains an expected
  afternoon decline. Estimates over 12 hours carry the target’s calendar hint
  (`tomorrow 16:30`, `Sat 04 Jul 02:38`); shorter ones print only the time. The cards stay
  instantaneous. See [`docs/SOLAR-ESTIMATE.md`](docs/SOLAR-ESTIMATE.md).
- **Day / night backdrop.** For one hour after sunset the scene crossfades from the daylight
  house to a moonlit copy with lit windows and stars, and for one hour before sunrise it fades
  back. Both edges follow the same server-supplied `view.daylight` window as the solar icon
  and the ETA, so the real estimate moves them automatically; with the fallback window they
  are **19:00–20:00** and **06:00–07:00**. It follows the site timezone and the existing
  once-a-second clock; the UI palette does not change. The two scenes share one pixel-aligned
  geometry, so the crossfade does not move the house or the hills.
- **Sunrise/sunset estimate.** `app/daylight.py` calculates local sun times with
  [Astral](https://pypi.org/project/astral/) from approximate coordinates. The coordinates and
  IANA timezone come from one GeoJS lookup of the server’s public IP, cached for 30 days in
  `~/.anker-tou/solar-location.json`. The lookup makes no request during daily calculations.
  Set `SOLAR_LATITUDE`, `SOLAR_LONGITUDE` and `SOLAR_TIMEZONE` together to bypass GeoJS when
  the host is in a data centre, behind a VPN, or behind CGNAT. The bottom-corner watermark
  shows the window and its source (`est. ip`, `est. cfg`, `fallback`); it is a quiet debug
  aid, not part of the dashboard.
- **Time-of-Use editor.** Reads the site, inverter and existing plan from your account.
  Edits seasons, weekday/weekend periods, tariffs and buy/sell prices. Periods cover the full
  day and stay contiguous.
- **Verified writes.** Saves use the same `charging_hes_svc/device_command` call as the
  Anker Android app. Each write is read back and rolled back on a real mismatch, because the
  cloud is eventually consistent and a **200** response does not prove the device accepted
  the plan.
- **Whole-hour scheduling.** The editor only emits whole-hour boundaries because the
  D156/X1 applies the previous whole hour even when the cloud stores `:30`; a `:30` boundary
  is floored, so that boundary moves **30 minutes early**. See
  [`docs/FIRMWARE-LIMITS.md`](docs/FIRMWARE-LIMITS.md).
- **Multiple sites and inverters.** Sites and devices are discovered from the account. No
  user, site or device is baked in.
- **No external assets.** The dashboard is plain HTML, CSS and SVG. No CDN, webfont or
  runtime AI call.

The plan editor never uses the window-driven Dynamic Tariff scheduler and never sends a
charge-power value. Only plan fields are writable; controller state is rejected server-side.

## Quick start

Requires Python 3.10+.

```sh
python3 -m pip install -r requirements.txt
./pandacode-run.sh                 # http://127.0.0.1:8770
```

Sign in with your Anker account. Sites, inverters and the real plan are loaded from the
account. This is the app, not a fixture: missing telemetry renders as `—`, never a sample
value.

For a server or container:

```sh
docker build -t anker-tou .
docker run --rm -p 8770:8770 -v anker-tou:/root/.anker-tou anker-tou
```

Run **one process**. Sessions and live MQTT feeds are in memory; multiple workers would hold
separate Anker sessions and subscriptions. Put it behind HTTPS and run with
`--secure-cookies`; use `--no-persist` on a shared instance. See
[`app/README.md`](app/README.md) for deployment, security and operation.

## Documentation

- [`app/README.md`](app/README.md) — app architecture, dashboard, plan drawer, security,
  operation.
- [`docs/API.md`](docs/API.md) — reverse-engineered endpoints, schemas and MQTT telemetry.
- [`docs/FIRMWARE-LIMITS.md`](docs/FIRMWARE-LIMITS.md) — why the device only honours
  whole-hour boundaries.
- [`docs/REVERSE-ENGINEERING.md`](docs/REVERSE-ENGINEERING.md) — playbook for continuing the
  work with a real device, mitmproxy and Frida.
- [`docs/MQTT-WAKE.md`](docs/MQTT-WAKE.md) — how the live MQTT stream is woken.
- [`docs/HOUSE-ASSET.md`](docs/HOUSE-ASSET.md) — source and design notes for the vector
  house.
- [`docs/SOLAR-ESTIMATE.md`](docs/SOLAR-ESTIMATE.md) — solar-aware battery ETA: mode table,
  curve, load compensation and the runtime charge cap.
- [`REPORT.md`](REPORT.md) — full investigation, evidence and findings.
- [`thomluther/anker-solix-api`](https://github.com/thomluther/anker-solix-api) — independent
  community API; issue #313 cross-checked the Home Energy System (HES) enums and id semantics.

## Known limits

- Anker stores TOU prices to **two decimals**, truncated: 7.62p becomes 7p.
- The cloud is **eventually consistent**; every write is verified by polling.
- Scheduling is effectively **one-hour granularity**. The cloud accepts `:30`, but the device
  applies the previous whole hour. See [`docs/FIRMWARE-LIMITS.md`](docs/FIRMWARE-LIMITS.md).
- The API is **private and undocumented**. Expect breakage.
- The slowest selectable cadence is **60 s**. Above 5 s the device is asked for two frames at
  a time, so a wake gap of up to a minute can appear between windows; the dashboard keeps the
  last values and shows `Refreshing` after a 2 s grace, or `Stale` once the frame is old. The
  wall panel's older 5 s build can temporarily make the feed faster than the selected cadence.
- The dashboard targets a **Skylight Calendar WebView** (Chromium 95). Other browsers are
  not a support target.
- The sunrise/sunset window is a real local estimate from `app/daylight.py`. Astral’s maths
  is accurate for the supplied coordinates, but IP geolocation is approximate: a cloud host,
  VPN or CGNAT can put the server minutes to hours away. Set the three `SOLAR_*` variables for
  a fixed site. A missing location, invalid timezone, polar day or polar night falls back to
  **07:00–18:00** so the solar model and backdrop keep working.
- There is **no summary or weather strip yet**; those await a verified Anker stats source.
  The plan editor lives behind the burger, not in a tab.
- A compromised host account can read the stored session. Host the app on a machine you
  control. The password itself is used once and never persisted.

## License

MIT — see [`LICENSE`](LICENSE).
