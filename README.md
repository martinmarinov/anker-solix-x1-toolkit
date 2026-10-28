# Anker SOLIX X1 — self-hosted toolkit

A self-hosted toolkit for the Anker SOLIX X1. The first module is a **Time-of-Use editor**
with whole-hour boundaries (the device's practical limit); the same backend also exposes **live telemetry** and a documented
reverse-engineered API surface for future modules.

It talks to Anker's cloud with **your own account**. Nothing about a particular user,
site or device is built in.

> **Not affiliated with Anker.** This uses an undocumented private API. It can change or
> break without notice. You are responsible for what you write to your system.

## Current modules

- **Plan editor** — reads the site, inverter and TOU plan from your Anker account.
- Edits periods at whole-hour boundaries, with the app's full-day rules (00:00–24:00,
  contiguous, wraps split, one buy/sell price per tariff).
- Writes through the same `charging_hes_svc/device_command` call the Android app uses,
  then polls the read-back and rolls back on a real mismatch.
- **Live monitor** — a house card fed by the inverter's MQTT telemetry (~5 s push, no polling).
- Supports multiple sites and multiple inverters.

The server, session handling and MQTT feed are shared infrastructure: new features (settings,
schedules, energy history, automations) can be added as additional modules without changing
the authentication or telemetry layer.

The plan editor never uses the Dynamic Tariff (window-driven) scheduler and never sends a
charge-power value.

## Quick start

Requires Python 3.10+.

```sh
python3 -m pip install -r requirements.txt
./pandacode-run.sh                 # http://127.0.0.1:8770
```

Sign in with your Anker account. Sites and devices are discovered automatically.

For a server or container:

```sh
docker build -t anker-tou .
docker run --rm -p 8770:8770 -v anker-tou:/root/.anker-tou anker-tou
```

Put it behind HTTPS (Caddy/nginx) and run with `--secure-cookies`. Use `--no-persist`
on a shared instance. See [`app/README.md`](app/README.md) for deployment and security.

### Can it be hosted on GitHub Pages?

**No.** GitHub Pages serves static files only; this app needs a Python process for login,
encrypted sessions, Anker API calls and MQTT. Use a small VPS/container, or run it locally.

## Documentation

| Document | Contents |
|---|---|
| [`docs/API.md`](docs/API.md) | Reverse-engineered endpoints, schemas, MQTT telemetry |
| [`docs/REVERSE-ENGINEERING.md`](docs/REVERSE-ENGINEERING.md) | Playbook for continuing the RE with a real device, mitmproxy and Frida |
| [`docs/FIRMWARE-LIMITS.md`](docs/FIRMWARE-LIMITS.md) | Why the device only honours whole-hour boundaries |
| [`thomluther/anker-solix-api`](https://github.com/thomluther/anker-solix-api) | Independent community API; issue #313 cross-checked the HES enums/id semantics |
| [`REPORT.md`](REPORT.md) | Full investigation, evidence and findings |
| [`app/README.md`](app/README.md) | App architecture, security model, operation |

## Known limits

- Anker stores TOU prices to **two decimals**, truncated: 7.62p becomes 7p.
- The cloud is **eventually consistent**; writes are verified by polling.
- The API is private and undocumented. Expect breakage.
- **Scheduling is effectively one-hour granularity.** The cloud stores `:30`, but the
  D156/X1 device applies the previous whole hour. See [`docs/FIRMWARE-LIMITS.md`](docs/FIRMWARE-LIMITS.md).

## License

MIT — see [`LICENSE`](LICENSE).
