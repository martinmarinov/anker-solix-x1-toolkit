#!/usr/bin/env python3
"""Local web UI for editing the Anker SOLIX X1 Time-of-Use plan.

Runs on 127.0.0.1 by default. Credentials are used once to obtain a token,
then dropped; the token lives in this process's memory only. Nothing secret is
written to disk or to the logs.
"""

from __future__ import annotations

import argparse
import json
import math
import subprocess
from collections import deque
import os
import secrets
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from live import (  # noqa: E402
    DEFAULT_INTERVAL_S, INTERVAL_STEP_S, MAX_INTERVAL_S, MIN_INTERVAL_S,
    LiveFeed, normalise_interval, stale_after_s,
)
from home_view import build_home_view
import daylight
import solar_location
from anker_client import (  # noqa: E402
    AnkerClient, AnkerError, assert_plan_only, dumps, now_stamp, plan_diff, site_id_of,
)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_BODY = 512 * 1024
SESSION_TTL = 7 * 24 * 3600  # absolute session lifetime (token usually expires first)
STATE_DIR = os.path.expanduser("~/.anker-tou")
SESSION_FILE = os.path.join(STATE_DIR, "session.bin")
KEYCHAIN_SERVICE = "anker-tou"
KEYCHAIN_ACCOUNT = "session-key"
COOKIE = "anker_tou_sid"
PREF_COOKIE = "anker_tou_mqtt_s"
PREF_MAX_AGE = 365 * 24 * 3600

CONFIG = {"host": "127.0.0.1", "port": 8770, "max_charge_kw": 4.2, "persist": True,
          "secure_cookie": False}


def interval_bounds() -> dict:
    return {"min": MIN_INTERVAL_S, "max": MAX_INTERVAL_S,
            "step": INTERVAL_STEP_S, "default": DEFAULT_INTERVAL_S}


_LOCATION_WARNING_SHOWN = False


def valid_timezone(tz) -> bool:
    """True for a usable IANA zone name."""
    if not isinstance(tz, str) or not tz.strip():
        return False
    try:
        ZoneInfo(tz)
    except Exception:
        return False
    return True


def resolve_site_location():
    """Best-effort approximate location; never breaks ``/api/live``."""
    global _LOCATION_WARNING_SHOWN
    try:
        return solar_location.get_location()
    except Exception as exc:
        if not _LOCATION_WARNING_SHOWN:
            _LOCATION_WARNING_SHOWN = True
            log(f"Solar location lookup failed ({type(exc).__name__}); "
                "using the daylight fallback")
        return None


def pref_cookie_header(value) -> str:
    """One-year display preference. Never a credential; never cleared on logout."""
    cookie = (f"{PREF_COOKIE}={normalise_interval(value)}; HttpOnly; "
              f"SameSite=Strict; Path=/; Max-Age={PREF_MAX_AGE}")
    if CONFIG.get("secure_cookie"):
        cookie += "; Secure"
    return cookie


def half_hour_offenders(plan: dict) -> list[str]:
    """Boundaries that are not on the hour (24:00 / 23:59 allowed)."""
    bad = []
    for sess in plan.get("peak_sessions") or []:
        if not isinstance(sess, dict):
            continue
        for row in sess.get("peak_valley_prices") or []:
            if not isinstance(row, dict):
                continue
            for key in ("start_time_period", "end_time_period"):
                value = row.get(key)
                if isinstance(value, str) and value not in ("24:00", "23:59") \
                        and not value.endswith(":00"):
                    bad.append(f"{key}={value}")
    return bad

SESSIONS: dict[str, dict] = {}
SESSIONS_LOCK = threading.Lock()
LOGIN_ATTEMPTS: dict[str, list[float]] = {}
DIAG: deque = deque(maxlen=60)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def stop_live_feeds(sess: dict | None) -> None:
    """Stop every MQTT feed held by a session (one feed per site+inverter)."""
    if not isinstance(sess, dict):
        return
    live = sess.get("live")
    if not live:
        return
    feeds = list(live.values()) if isinstance(live, dict) else [live]
    for feed in feeds:
        try:
            feed.stop()
        except Exception:
            pass
    if isinstance(live, dict):
        live.clear()


def log(msg: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    sys.stderr.write(f"[{ts}] {msg}\n")
    sys.stderr.flush()


def capture(data: dict, tag: str) -> str:
    out_dir = os.path.join(STATE_DIR, "captures")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"plan-{now_stamp()}-{tag}.json")
    os.chmod(out_dir, 0o700)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    os.chmod(path, 0o600)
    return path


def _buy2(x) -> float:
    """Anker truncates prices to two decimals; compare at that precision."""
    try:
        return math.floor(float(x) * 100 + 1e-9) / 100
    except (TypeError, ValueError):
        return 0.0


def price_precision_issues(plan: dict) -> list:
    """Prices the write path would round, which would silently change them."""
    out = []
    for season in plan.get("peak_sessions") or []:
        for r in season.get("peak_valley_prices") or []:
            for key in ("buy", "sell"):
                try:
                    v = float(r.get(key) or 0)
                except (TypeError, ValueError):
                    continue
                if abs(v * 100 - round(v * 100)) > 1e-9:
                    out.append({"day_type": r.get("day_type"), "peak_type": r.get("peak_type"),
                                "field": key, "from": v, "to": _buy2(v)})
    return out


def hourly_boundary_issues(plan: dict) -> list:
    """The device applies whole hours; reject :30 before touching the cloud."""
    out = []
    for season in plan.get("peak_sessions") or []:
        for r in season.get("peak_valley_prices") or []:
            for field in ("start_time_period", "end_time_period"):
                value = str(r.get(field) or "")
                if value == "24:00":
                    continue
                try:
                    hh, mm = value.split(":")[:2]
                    int(hh); int(mm)
                except (TypeError, ValueError):
                    out.append({"field": field, "value": value})
                    continue
                if int(mm) != 0:
                    out.append({"field": field, "value": value})
    return out


def price_adjustments(sent: dict, stored: dict) -> list:
    """Rows whose buy/sell price the cloud changed (truncation to 2 decimals)."""
    def index(plan):
        out = {}
        for season in plan.get("peak_sessions") or []:
            for r in season.get("peak_valley_prices") or []:
                key = (r.get("day_type"), r.get("peak_type"),
                       r.get("start_time_period"), r.get("end_time_period"))
                out[key] = r
        return out
    a, b = index(sent), index(stored)
    changes = []
    for key, row in a.items():
        other = b.get(key)
        if not other:
            continue
        for field in ("buy", "sell"):
            try:
                if abs(float(row.get(field) or 0) - float(other.get(field) or 0)) > 1e-9:
                    changes.append({"day_type": key[0], "peak_type": key[1],
                                    "field": field,
                                    "from": row.get(field), "to": other.get(field)})
            except (TypeError, ValueError):
                pass
    return changes


def normalize_plan(data: dict) -> list:
    """Reduce a plan to the fields the editor owns, for write verification."""
    out = []
    for key in ("peak_sessions", "use_time"):
        for lst in _find_lists(data, key):
            for row in lst:
                if not isinstance(row, dict):
                    continue
                if "peak_valley_prices" in row:
                    for p in row["peak_valley_prices"]:
                        out.append((row.get("start_time"), row.get("end_time"),
                                    p.get("day_type"), p.get("peak_type"),
                                    p.get("start_time_period"), p.get("end_time_period"),
                                    f"{_buy2(p.get('buy')):.2f}",
                                    f"{_buy2(p.get('sell')):.2f}"))
                else:
                    out.append((row.get("day_type"), row.get("peak_type") or row.get("type"),
                                row.get("start_time_period") or row.get("start_time"),
                                row.get("end_time_period") or row.get("end_time")))
    return sorted(map(str, out))


def _find_lists(node, key):
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, list):
                yield v
            else:
                yield from _find_lists(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from _find_lists(v, key)


_SESSION_KEY: bytes | None = None


def _state_dir() -> None:
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(STATE_DIR, 0o700)
    except OSError:
        pass


def _key_from_keychain() -> bytes | None:
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", KEYCHAIN_ACCOUNT, "-w"],
            capture_output=True, text=True, timeout=5)
    except (FileNotFoundError, subprocess.SubprocessError):
        return None
    if r.returncode == 0:
        try:
            key = bytes.fromhex(r.stdout.strip())
            if len(key) == 32:
                return key
        except ValueError:
            pass
    return None


def _key_to_keychain(key_hex: str) -> bool:
    try:
        r = subprocess.run(
            ["security", "add-generic-password", "-s", KEYCHAIN_SERVICE,
             "-a", KEYCHAIN_ACCOUNT, "-w", key_hex, "-U"],
            capture_output=True, timeout=5)
        return r.returncode == 0
    except (FileNotFoundError, subprocess.SubprocessError):
        return False


def _write_key_file(key: bytes) -> None:
    _state_dir()
    path = os.path.join(STATE_DIR, "key")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(key.hex())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _read_key_file() -> bytes | None:
    path = os.path.join(STATE_DIR, "key")
    try:
        key = bytes.fromhex(open(path, encoding="utf-8").read().strip())
        return key if len(key) == 32 else None
    except (OSError, ValueError):
        return None


def session_key() -> bytes:
    """32-byte key for the session blob.

    Default: ~/.anker-tou/key, mode 0600 (no prompts, deterministic).
    Opt-in --use-keychain: macOS Keychain, falling back to the file.
    """
    global _SESSION_KEY
    if _SESSION_KEY:
        return _SESSION_KEY

    if CONFIG.get("use_keychain"):
        key = _key_from_keychain()
        if key:
            _SESSION_KEY = key
            return key
        key = secrets.token_bytes(32)
        if _key_to_keychain(key.hex()):
            _SESSION_KEY = key
            return key
        log("Keychain unavailable - falling back to ~/.anker-tou/key (0600)")

    key = _read_key_file()
    if not key:
        key = secrets.token_bytes(32)
        _write_key_file(key)
    _SESSION_KEY = key
    return key


def persist_session(sid: str, rec: dict) -> None:
    if not CONFIG["persist"]:
        return
    try:
        _state_dir()
        blob = json.dumps({"v": 1, "sid": sid, **rec}).encode()
        nonce = secrets.token_bytes(12)
        ct = AESGCM(session_key()).encrypt(nonce, blob, b"anker-tou-session-v1")
        tmp = SESSION_FILE + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(nonce + ct)
        os.replace(tmp, SESSION_FILE)
        try:
            os.chmod(SESSION_FILE, 0o600)
        except OSError:
            pass
    except Exception as e:  # never let persistence break a login
        log(f"could not persist session: {e.__class__.__name__}")


def load_persisted() -> dict | None:
    if not CONFIG["persist"] or not os.path.exists(SESSION_FILE):
        return None
    try:
        raw = open(SESSION_FILE, "rb").read()
        nonce, ct = raw[:12], raw[12:]
        blob = AESGCM(session_key()).decrypt(nonce, ct, b"anker-tou-session-v1")
        return json.loads(blob)
    except Exception as e:
        log(f"stored session unreadable ({e.__class__.__name__}); ignoring it")
        return None


def clear_persisted() -> None:
    try:
        os.remove(SESSION_FILE)
    except OSError:
        pass


def restore_session() -> None:
    rec = load_persisted()
    if not rec:
        return
    if float(rec.get("expires") or 0) < time.time():
        clear_persisted()
        log("stored session expired - sign-in required")
        return
    try:
        client = AnkerClient.resume(rec["token"], rec.get("user_id", ""),
                                    rec.get("country", "GB"), rec.get("base"),
                                    rec.get("nick", ""))
    except Exception as e:
        log(f"could not restore session: {e.__class__.__name__}")
        return
    sites = rec.get("sites", [])
    if not sites or any(not x.get("id") for x in sites):
        try:
            raw = client.sites()
            if raw:
                sites = [{"id": site_id_of(x),
                          "name": x.get("site_name") or x.get("name") or x.get("siteName") or "Site"}
                         for x in raw]
        except AnkerError:
            pass
    SESSIONS[rec["sid"]] = {
        "client": client,
        "profile": {"nick": rec.get("nick", ""), "user_id": rec.get("user_id", ""),
                    "country": rec.get("country", "GB"), "base": rec.get("base", "")},
        "sites": sites,
        "expires": float(rec["expires"]),
        "lock": threading.Lock(),
        "mqtt_interval_s": normalise_interval(rec.get("mqtt_interval_s")),
    }
    log(f"restored session for {rec.get('nick') or rec.get('user_id')}")


def startup_probe() -> None:
    """Read-only tracer: exercise the power-limit path once, at startup.

    Uses the restored session so we learn whether param_type 32 is readable
    without needing a browser round-trip. Never writes anything.
    """
    with SESSIONS_LOCK:
        if not SESSIONS:
            return
        sid, sess = next(iter(SESSIONS.items()))
    site = (sess.get("sites") or [{}])[0].get("id")
    if not site:
        return
    t0 = time.time()
    status, note = 200, ""
    try:
        data = sess["client"].power_limits(site)
        lv = limits_verdict(data)
        keys = sorted(data.keys())[:10] if isinstance(data, dict) else None
        note = (f"startup probe: found={lv.get('found')} kw={lv.get('kw')} "
                f"keys={keys} err={getattr(sess['client'], 'last_limits_error', None)}")
    except AnkerError as e:
        status, note = 500, f"startup probe failed: {e.message}"
    rec = {"t": int(time.time()), "method": "PROBE", "path": "/api/limits",
           "status": status, "ms": int((time.time() - t0) * 1000),
           "error": "" if status == 200 else note, "note": note}
    DIAG.append(rec)
    log(note)


def limits_verdict(limits: dict | None) -> dict:
    """Read the station AC-input (grid charge) ceiling.

    Decoded from the app's StationPowerLimitModel: the field is
    `ac_input_power` with `ac_input_power_unit`, plus a `legal_power_limit`
    cap. (It is NOT `acPowerLimit`, which only exists on BLE/EV paths.)
    """
    if not isinstance(limits, dict):
        return {"found": False, "ok": None,
                "message": "Could not read the grid charge power limit. "
                           "Verify it in the Anker app before saving."}

    unit = str(limits.get("ac_input_power_unit") or "").strip().upper()
    val = None
    for k in ("ac_input_power", "acPowerLimit", "maxChargingPower",
              "gridChargingPower", "powerLimit"):
        if isinstance(limits.get(k), (int, float)):
            val = float(limits[k])
            break
    if val is None:
        for k, v in _walk(limits):
            if k in ("ac_input_power", "maxChargingPower", "gridChargingPower"):
                try:
                    val = float(v)
                except (TypeError, ValueError):
                    continue
                unit = ""
                break

    if val is None:
        return {"found": False, "ok": None, "keys": sorted(limits.keys())[:10],
                "message": "No AC-input power field in the power-limit response. "
                           "Verify the ceiling in the Anker app before saving."}

    if unit in ("W", "WATT", "WATTS"):
        kw = val / 1000.0
    elif unit in ("KW",):
        kw = val
    else:
        kw = val / 1000.0 if val > 100 else val

    ok = kw <= CONFIG["max_charge_kw"] + 1e-9
    legal = limits.get("legal_power_limit")
    legal_txt = ""
    if isinstance(legal, dict) and isinstance(legal.get("limit"), (int, float)):
        legal_txt = f" Legal cap: {legal['limit']}."
    elif isinstance(legal, (int, float)):
        legal_txt = f" Legal cap: {legal}."
    return {"found": True, "value": val, "unit": unit or "?", "kw": round(kw, 2), "ok": ok,
            "message": (f"Grid charge ceiling is {kw:.2f} kW "
                        f"({'within' if ok else 'ABOVE'} the {CONFIG['max_charge_kw']} kW target)."
                        + legal_txt)}


def _walk(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield k, v
            yield from _walk(v, k)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v, path)


def check_origin(handler: BaseHTTPRequestHandler) -> bool:
    """Reject cross-site POSTs even if a cookie leaks."""
    origin = handler.headers.get("Origin")
    if not origin:
        return True  # non-browser client (curl); cookie still required
    host = handler.headers.get("Host", "")
    return origin.endswith("://" + host)


# --------------------------------------------------------------------------
# request handler
# --------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    server_version = "AnkerTOU/1.0"
    protocol_version = "HTTP/1.1"

    # -- plumbing ---------------------------------------------------------

    def log_message(self, fmt, *args):  # silence default (it logs paths + referers)
        return

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self._status = code
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; "
                         "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                         "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            if isinstance(v, (list, tuple)):
                for item in v:
                    self.send_header(k, item)
            else:
                self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json(self, code: int, obj, extra: dict | None = None):
        self._send(code, dumps(obj).encode(), "application/json; charset=utf-8", extra)

    def fail(self, code: int, message: str, kind: str = "error", extra=None):
        self._error = f"[{kind}] {message[:180]}"
        self.json(code, {"ok": False, "error": {"kind": kind, "message": message,
                                                **(extra or {})}})

    def body_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise AnkerError("Request too large.", kind="validation")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode())
        except (ValueError, UnicodeDecodeError):
            raise AnkerError("Malformed JSON body.", kind="validation")
        if not isinstance(data, dict):
            raise AnkerError("Expected a JSON object.", kind="validation")
        return data

    def cookie_value(self, name: str) -> str | None:
        raw = self.headers.get("Cookie")
        if not raw:
            return None
        c = SimpleCookie()
        try:
            c.load(raw)
        except Exception:
            return None
        m = c.get(name)
        return m.value if m else None

    def cookie_sid(self) -> str | None:
        return self.cookie_value(COOKIE)

    def pref_interval(self) -> int:
        return normalise_interval(self.cookie_value(PREF_COOKIE))

    def resolve_interval(self, sess: dict) -> int:
        """Cookie wins; the persisted session is the fallback after a restart."""
        raw = self.cookie_value(PREF_COOKIE)
        if raw is not None:
            return normalise_interval(raw)
        return normalise_interval(sess.get("mqtt_interval_s"))

    def session(self) -> dict | None:
        sid = self.cookie_sid()
        if not sid:
            return None
        with SESSIONS_LOCK:
            s = SESSIONS.get(sid)
            if not s:
                return None
            if s["expires"] < time.time():
                SESSIONS.pop(sid, None)
                stop_live_feeds(s)
                return None
            s["expires"] = time.time() + SESSION_TTL
            return s

    def drop_session(self) -> None:
        sid = self.cookie_sid()
        if sid:
            with SESSIONS_LOCK:
                sess = SESSIONS.pop(sid, None)
            stop_live_feeds(sess)
        clear_persisted()

    def require_session(self) -> dict | None:
        s = self.session()
        if not s:
            self.fail(401, "Not signed in, or the session expired.", "auth")
            return None
        return s

    # -- routes -----------------------------------------------------------

    def do_GET(self):
        self._timed("GET", self._route_get)

    def _timed(self, method, fn):
        t0 = time.time()
        self._error = ""
        self._note = ""
        path = urlparse(self.path).path
        try:
            fn()
        finally:
            if path.startswith("/api/"):
                rec = {"t": int(time.time()), "method": method, "path": path,
                       "status": getattr(self, "_status", 0),
                       "ms": int((time.time() - t0) * 1000),
                       "error": getattr(self, "_error", ""),
                       "note": getattr(self, "_note", "")}
                DIAG.append(rec)
                tail = f"  {rec['error']}" if rec["error"] else (
                    f"  {rec['note']}" if rec.get("note") else "")
                log(f"{method} {path} -> {rec['status']} in {rec['ms']}ms{tail}")

    def _route_get(self):
        u = urlparse(self.path)
        try:
            if u.path in ("/", "/index.html"):
                return self.serve_static("index.html")
            if u.path.startswith("/static/"):
                return self.serve_static(u.path[len("/static/"):])
            if u.path == "/api/health":
                return self.json(200, {"ok": True, "time": int(time.time())})
            if u.path == "/api/state":
                return self.api_state()
            if u.path == "/api/plan":
                return self.api_get_plan(parse_qs(u.query))
            if u.path == "/api/devices":
                return self.api_devices(parse_qs(u.query))
            if u.path == "/api/limits":
                return self.api_limits(parse_qs(u.query))
            if u.path == "/api/live":
                return self.api_live()
            if u.path == "/api/diag":
                if not self.require_session():
                    return
                return self.json(200, {"ok": True, "requests": list(DIAG)})
            self.fail(404, "Not found.")
        except AnkerError as e:
            if e.kind == "auth":
                self.drop_session()
            self.fail(self.status_for(e), e.message, e.kind)
        except Exception:
            log("GET " + u.path + " crashed\n" + traceback.format_exc())
            self.fail(500, "Internal error. See the server console.")

    def do_HEAD(self):
        # Same routing as GET; _send() suppresses the body for HEAD.
        self.do_GET()

    def do_POST(self):
        self._timed("POST", self._route_post)

    def _route_post(self):
        u = urlparse(self.path)
        if not check_origin(self):
            return self.fail(403, "Cross-site request rejected.", "security")
        if self.headers.get("X-Anker-Client") != "1":
            return self.fail(403, "Missing client header.", "security")
        try:
            if u.path == "/api/login":
                return self.api_login()
            if u.path == "/api/logout":
                return self.api_logout()
            if u.path == "/api/plan":
                return self.api_save_plan()
            if u.path == "/api/live/interval":
                return self.api_live_interval()
            self.fail(404, "Not found.")
        except AnkerError as e:
            if e.kind == "auth":
                self.drop_session()
            self.fail(self.status_for(e), e.message, e.kind)
        except Exception:
            log("POST " + u.path + " crashed\n" + traceback.format_exc())
            self.fail(500, "Internal error. See the server console.")

    def status_for(self, e: AnkerError) -> int:
        return {"auth": 401, "rate_limit": 429, "network": 502,
                "validation": 422}.get(e.kind, 400)

    def serve_static(self, name: str):
        safe = os.path.normpath(name).lstrip("/")
        if safe.startswith("..") or os.path.isabs(safe):
            return self.fail(404, "Not found.")
        path = os.path.join(STATIC_DIR, safe)
        if not os.path.isfile(path):
            return self.fail(404, "Not found.")
        ctype = {"html": "text/html; charset=utf-8", "js": "text/javascript; charset=utf-8",
                 "css": "text/css; charset=utf-8", "svg": "image/svg+xml",
                 "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
                 "webp": "image/webp", "avif": "image/avif"}.get(
                     safe.rsplit(".", 1)[-1], "application/octet-stream")
        with open(path, "rb") as fh:
            self._send(200, fh.read(), ctype)

    # -- api --------------------------------------------------------------

    def api_login(self):
        ip = self.client_address[0]
        now = time.time()
        hits = [t for t in LOGIN_ATTEMPTS.get(ip, []) if now - t < 300]
        if len(hits) >= 8:
            return self.fail(429, "Too many sign-in attempts. Wait a few minutes.", "rate_limit")

        body = self.body_json()
        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
        country = (body.get("country") or "GB").strip().upper()[:2]
        if not email or not password:
            raise AnkerError("Email and password are required.", kind="validation")

        client = AnkerClient(email, password, country)
        try:
            profile = client.login()
            sites = client.sites()
        except AnkerError:
            LOGIN_ATTEMPTS.setdefault(ip, []).append(now)
            raise

        if not sites:
            raise AnkerError(
                "Signed in, but no sites were returned. This usually means the account "
                "region does not match the selected country.", kind="validation")

        sid = secrets.token_urlsafe(32)
        expires = time.time() + SESSION_TTL
        if profile.get("expires_at"):
            expires = min(expires, float(profile["expires_at"]))
        interval = self.pref_interval()
        view_sites = [self.site_view(x) for x in sites]
        with SESSIONS_LOCK:
            SESSIONS[sid] = {"client": client, "profile": profile, "sites": view_sites,
                             "expires": expires, "lock": threading.Lock(),
                             "mqtt_interval_s": interval}
        persist_session(sid, {
            "token": client.token, "user_id": profile.get("user_id", ""),
            "nick": profile.get("nick", ""), "country": client.country,
            "base": client.base, "sites": view_sites, "expires": expires,
            "mqtt_interval_s": interval,
        })
        LOGIN_ATTEMPTS.pop(ip, None)
        cookie = f"{COOKIE}={sid}; HttpOnly; SameSite=Strict; Path=/"
        if CONFIG["persist"]:
            cookie += f"; Max-Age={int(expires - time.time())}"
        if CONFIG.get("secure_cookie"):
            cookie += "; Secure"
        self.json(200, {"ok": True, "user": profile, "sites": view_sites,
                        "mqtt_interval_s": interval,
                        "mqtt_bounds": interval_bounds()},
                  extra={"Set-Cookie": [cookie, pref_cookie_header(interval)],
                         "X-Session-Persisted": str(CONFIG['persist']).lower()})

    def site_view(self, s: dict) -> dict:
        tz = s.get("timezone") or s.get("time_zone") or s.get("tz") or ""
        return {"id": site_id_of(s),
                "name": s.get("site_name") or s.get("name") or s.get("siteName") or "Site",
                "timezone": tz if isinstance(tz, str) else ""}

    def api_logout(self):
        sid = self.cookie_sid()
        if sid:
            with SESSIONS_LOCK:
                sess = SESSIONS.pop(sid, None)
            stop_live_feeds(sess)
        clear_persisted()
        # Deliberately leave PREF_COOKIE in place: the refresh cadence is a
        # display preference and must survive sign-out and account switches.
        self.json(200, {"ok": True}, extra={"Set-Cookie": f"{COOKIE}=; Path=/; Max-Age=0"})

    def api_state(self):
        s = self.require_session()
        if not s:
            return
        first = s["sites"][0] if s["sites"] else {}
        keys = sorted(first.keys())[:14] if isinstance(first, dict) else []
        interval = self.resolve_interval(s)
        s["mqtt_interval_s"] = interval
        self._note = f"site_keys={keys}"
        self.json(200, {"ok": True, "user": s["profile"],
                        "sites": [self.site_view(x) for x in s["sites"]],
                        "site_keys": keys,
                        "mqtt_interval_s": interval,
                        "mqtt_bounds": interval_bounds()})

    def api_devices(self, q):
        s = self.require_session()
        if not s:
            return
        site = (q.get("site") or [None])[0]
        if not site:
            return self.fail(422, "site is required.", "validation")
        devices = s["client"].devices(site)
        probe = []
        for pr in getattr(s["client"], "last_probe", []):
            if pr.get("error"):
                probe.append(f"{pr['source']}=ERR")
            else:
                probe.append(f"{pr['source']}={pr.get('items')}/{pr.get('matched')}"
                             f"({pr.get('data_type')}:{(pr.get('data_keys') or [])[:5]})")
        self._note = (f"devices={len(devices)} source={getattr(s['client'], 'last_source', '?')}"
                      f" probe=[{' | '.join(probe)}]")[:700]
        self.json(200, {"ok": True, "devices": devices,
                        "source": getattr(s["client"], "last_source", "none"),
                        "probe": getattr(s["client"], "last_probe", [])})

    def _session_live(self, sess, site=None, sn=None):
        """Return (feed, site id, inverter sn), starting it lazily.

        Sessions can hold more than one feed (multi-site accounts), keyed by
        site+inverter. The old single-feed behaviour always used sites[0] and
        ignored the site the user had selected in the UI.
        """
        sites = sess.get("sites") or []
        site = site or (sites[0].get("id") if sites else None)
        if not site:
            raise AnkerError("No site available for live telemetry.", kind="validation")

        lock = sess.setdefault("lock", threading.Lock())
        with lock:
            feeds = sess.setdefault("live", {})
            if not isinstance(feeds, dict):
                # Defensive: a session restored by an older build may hold one feed.
                try:
                    feeds.stop()
                except Exception:
                    pass
                feeds = sess["live"] = {}

            # A site+sn key is enough to reuse an existing feed. When sn is not
            # supplied, resolve the first inverter below.
            if sn:
                existing = feeds.get(f"{site}:{sn}")
                if existing:
                    return existing, site, sn

        devices = sess["client"].devices(site) or []
        inverters = [d for d in devices if d.get("role") == "inverter"]
        if not inverters:
            raise AnkerError("No inverter found on this site.", kind="validation")
        main = next((d for d in inverters if sn and d.get("sn") == sn), inverters[0])
        chosen_sn = main.get("sn")
        if not chosen_sn:
            raise AnkerError("The inverter has no serial number.", kind="validation")
        key = f"{site}:{chosen_sn}"

        with lock:
            existing = feeds.get(key)
            if existing:
                return existing, site, chosen_sn

            # Keep a bounded number of sockets for accounts with many sites.
            while len(feeds) >= 4:
                old_key, oldest = next(iter(feeds.items()))
                feeds.pop(old_key, None)
                try:
                    oldest.stop()
                except Exception:
                    pass
            battery_modules = sum(
                1 for d in devices if d.get("role") == "battery")
            try:
                reserve_soc = float(sess.get("battery_reserve_soc", 5.0))
            except (TypeError, ValueError):
                reserve_soc = 5.0
            interval = self.resolve_interval(sess)
            sess["mqtt_interval_s"] = interval
            live = LiveFeed(
                sess["client"], main.get("code") or "device", chosen_sn,
                battery_modules=battery_modules, reserve_soc=reserve_soc,
                interval_s=interval, site=site)
            feeds[key] = live
            live.start()
            return live, site, chosen_sn

    def _site_timezone(self, sess, site):
        for item in sess.get("sites") or []:
            if item.get("id") == site:
                tz = item.get("timezone") or ""
                return tz if isinstance(tz, str) else ""
        return ""

    def api_live(self):
        s = self.require_session()
        if not s:
            return
        q = parse_qs(urlparse(self.path).query)
        site = (q.get("site") or [None])[0] or None
        sn = (q.get("sn") or [None])[0] or None
        try:
            live, site, sn = self._session_live(s, site, sn)
        except AnkerError as e:
            if e.kind == "auth":
                self.drop_session()
            return self.fail(self.status_for(e), e.message, e.kind)
        snap = live.get()
        tz = self._site_timezone(s, site)
        location = resolve_site_location()
        if location is not None and not valid_timezone(tz):
            tz = location.timezone
        view = build_home_view(
            snap, now=snap.get("ts"), tz=tz,
            latitude=location.latitude if location else None,
            longitude=location.longitude if location else None,
            location_source=location.source if location else "default")
        interval = snap.get("interval_s", live.interval_s)
        self.json(200, {"ok": True, "site": site, "sn": sn,
                        "timezone": tz,
                        "mqtt_interval_s": normalise_interval(
                            s.get("mqtt_interval_s", interval)),
                        "mqtt_bounds": interval_bounds(),
                        "stale_after_s": snap.get(
                            "stale_after_s", stale_after_s(interval)),
                        "live_stats": live.stats(),
                        "live": snap, "view": view})

    def api_live_interval(self):
        s = self.require_session()
        if not s:
            return
        body = self.body_json()
        raw = body.get("interval_s")
        if raw is None:
            return self.fail(422, "interval_s is required.", "validation")
        try:
            requested = int(round(float(raw)))
        except (TypeError, ValueError):
            return self.fail(422, "interval_s must be a number.", "validation")
        interval = normalise_interval(requested)
        s["mqtt_interval_s"] = interval
        results = []
        feeds = s.get("live") or {}
        if isinstance(feeds, dict):
            for feed in list(feeds.values()):
                results.append(feed.set_interval(interval))
        pending = next((r.get("pending_interval_s") for r in results
                        if r.get("pending_interval_s")), None)
        applied = all(r.get("applied") for r in results) if results else True
        self.json(200, {
            "ok": True,
            "mqtt_interval_s": interval,
            "mqtt_bounds": interval_bounds(),
            "stale_after_s": stale_after_s(interval),
            "applied": applied,
            "pending_interval_s": pending,
            "feeds": results,
        }, extra={"Set-Cookie": pref_cookie_header(interval)})

    def api_limits(self, q):
        s = self.require_session()
        if not s:
            return
        site = (q.get("site") or [None])[0]
        if not site:
            return self.fail(422, "site is required.", "validation")
        sn = (q.get("sn") or [None])[0]
        limits = s["client"].power_limits(site, sn)
        lv = limits_verdict(limits)
        self._note = (f"found={lv.get('found')} kw={lv.get('kw')} ok={lv.get('ok')} "
                      f"keys={(sorted(limits.keys())[:8] if isinstance(limits, dict) else None)} "
                      f"err={getattr(s['client'], 'last_limits_error', None)}")
        self.json(200, {"ok": True, "power_limits": limits,
                        "limits_check": limits_verdict(limits)})

    def api_get_plan(self, q):
        s = self.require_session()
        if not s:
            return
        site = (q.get("site") or [None])[0]
        sn = (q.get("sn") or [None])[0]
        if site in (None, "", "null", "undefined"):
            return self.fail(422, "No site id available for this account.", "validation")

        # The TOU plan is SITE-scoped; only the inverter (main device) may be
        # used as main_sn. Battery modules (A5220) are never valid targets.
        devices = s["client"].devices(site) or []
        inverters = [d for d in devices if d.get("role") == "inverter"]
        chosen = None
        if sn and any(d.get("sn") == sn for d in inverters):
            chosen = next(d for d in inverters if d.get("sn") == sn)
        elif inverters:
            chosen = inverters[0]
        elif devices:
            chosen = devices[0]
        sn = chosen.get("sn") if chosen else None
        if not sn:
            return self.fail(422, "No device found for this site.", "validation")
        self._note = f"site={site} sn={sn}"

        plan = s["client"].get_plan(site, sn)
        try:
            sess = plan["data"].get("peak_sessions") or []
            n_sessions = len(sess)
            n_rows = sum(len(x.get("peak_valley_prices") or []) for x in sess
                         if isinstance(x, dict))
            shape = sorted(plan["data"].keys())[:6]
        except Exception:
            n_sessions, n_rows, shape = -1, -1, []
        sig = []
        try:
            for sess in plan["data"].get("peak_sessions") or []:
                for r in sess.get("peak_valley_prices") or []:
                    sig.append((r.get("day_type"), r.get("peak_type"),
                                r.get("start_time_period"), r.get("end_time_period"),
                                r.get("buy")))
        except Exception:
            pass
        mode = plan["data"].get("price_source_type") or plan["data"].get("electricity_strategy_mode")
        self._note = (f"sn={sn} via={plan['endpoint']} sessions={n_sessions} rows={n_rows} "
                      f"mode={mode} keys={shape} sig={sig}")
        # Limits are fetched separately (/api/limits) so a slow or unsupported
        # limits call can never block the editor from loading.
        limits = None
        strategy = None
        strategy_error = ""
        try:
            strategy = s["client"].get_strategy(site, sn)
            if strategy is None:
                strategy_error = "Anker returned no electricity_strategy object."
        except AnkerError as e:
            strategy_error = e.message
        if strategy and strategy.get("conserve_percent") is not None:
            try:
                s["battery_reserve_soc"] = float(strategy["conserve_percent"])
            except (TypeError, ValueError):
                pass
        self.json(200, {
            "ok": True, "site": site, "sn": sn, "endpoint": plan["endpoint"],
            "plan": plan["data"], "strategy": strategy,
            "strategy_error": strategy_error, "main_sn": sn,
            "devices": [{**d, "main": d.get("sn") == sn} for d in devices],
        })

    def api_save_plan(self):
        s = self.require_session()
        if not s:
            return
        body = self.body_json()
        site = body.get("site")
        sn = body.get("sn")
        plan = body.get("plan")
        ack = bool(body.get("ack"))
        if not site or not sn or not isinstance(plan, dict):
            return self.fail(422, "site, sn and plan are required.", "validation")
        try:
            limit_kw = float(body.get("max_charge_kw") or CONFIG["max_charge_kw"])
        except (TypeError, ValueError):
            return self.fail(422, "max_charge_kw must be a number.", "validation")
        if not 0 < limit_kw <= 20:
            return self.fail(422, "max_charge_kw must be between 0 and 20 kW.", "validation")
        if not ack:
            return self.fail(422, "Confirm that you have verified the grid charge limit.",
                             "validation")
        strategy_extra = {}
        raw_strategy = body.get("strategy")
        if raw_strategy is not None:
            if not isinstance(raw_strategy, dict):
                return self.fail(422, "strategy must be an object.", "validation")
            if "conserve_percent" in raw_strategy:
                try:
                    reserve = int(raw_strategy["conserve_percent"])
                except (TypeError, ValueError):
                    return self.fail(422, "conserve_percent must be an integer.", "validation")
                if not 0 <= reserve <= 100:
                    return self.fail(422, "conserve_percent must be 0–100.", "validation")
                strategy_extra["conserve_percent"] = reserve
            if "grid_recharging" in raw_strategy:
                grid = raw_strategy["grid_recharging"]
                if grid not in (0, 1, True, False):
                    return self.fail(422, "grid_recharging must be 0 or 1.", "validation")
                strategy_extra["grid_recharging"] = int(bool(grid))

        try:
            devices = s["client"].devices(site) or []
            inverters = [d for d in devices if d.get("role") == "inverter"]
            if inverters:
                sn = inverters[0].get("sn")
        except AnkerError:
            pass

        if not s["lock"].acquire(blocking=False):
            return self.fail(429, "A save is already in progress for this session.", "busy")
        try:
            live = s["client"].get_plan(site, sn)
            before = live["data"]
            assert_plan_only(before, plan)

            boundary_issues = hourly_boundary_issues(plan)
            if boundary_issues:
                return self.fail(422,
                    "This device only supports whole-hour boundaries; the :30 values were "
                    "floored by the firmware. Nothing was written.",
                    "hourly", {"issues": boundary_issues[:8]})

            issues = price_precision_issues(plan)
            if issues:
                return self.fail(409,
                    "Saving this schedule would change Anker's stored prices: "
                    + ", ".join(f"{i['field']} {i['from']} → {i['to']}" for i in issues[:4])
                    + ". Anker's schedule write truncates prices to two decimals. Nothing was written.",
                    "precision", {"issues": issues})

            # The plan body has no power field. Set the AC input ceiling FIRST
            # so the inverter is already throttled when the new schedule lands.
            try:
                limit_result = s["client"].set_ac_power_limit(sn, int(round(limit_kw * 1000)))
            except AnkerError as e:
                return self.fail(502,
                    "Could not set the AC charge limit; the plan was not written. "
                    f"({e.message})", "limit", {"limit_kw": limit_kw})

            # The solar ETA can use the value we just wrote immediately, even
            # if the read-back endpoint is empty on this firmware.
            feeds = s.get("live") or {}
            if isinstance(feeds, dict):
                for feed in list(feeds.values()):
                    if (getattr(feed, "sn", None) == sn
                            and getattr(feed, "site", site) == site):
                        try:
                            feed.set_charge_limit(limit_kw, "plan-save")
                        except Exception:
                            pass

            # Whole-hour boundaries: the device floors :30 to the previous hour.

            snap = capture(before, "before-write")
            s["client"].put_plan(site, sn, plan, strategy_extra or None)

            # The cloud applies writes asynchronously; poll briefly before
            # deciding the plan was rejected.
            after = None
            for attempt in range(7):
                time.sleep(1 if attempt else 0.5)
                after = s["client"].get_plan(site, sn)["data"]
                if normalize_plan(after) == normalize_plan(plan):
                    break
            capture(after, "after-write")

            if normalize_plan(after) != normalize_plan(plan):
                # Read-back mismatch: put the user's previous plan back.
                sent_norm = normalize_plan(plan)
                after_norm = normalize_plan(after)
                sent_snap = capture(plan, "sent-write")
                try:
                    s["client"].put_plan(site, sn, before)
                    restored = "Previous plan restored automatically."
                except AnkerError as e:
                    restored = f"Rollback FAILED: {e.message}. Restore {snap} manually."
                return self.fail(409,
                                 "The cloud did not store the plan as sent after 6s — it may "
                                 "have rejected a boundary. " + restored,
                                 "mismatch", {"readback": after, "sent": plan,
                                              "snapshot": snap, "sent_snapshot": sent_snap,
                                              "only_sent": list(set(sent_norm) - set(after_norm))[:8],
                                              "only_readback": list(set(after_norm) - set(sent_norm))[:8]})

            after_strategy = None
            strategy_ok = True
            if strategy_extra:
                try:
                    after_strategy = s["client"].get_strategy(site, sn)
                except AnkerError:
                    after_strategy = None
                strategy_ok = bool(after_strategy) and all(
                    after_strategy.get(k) == v for k, v in strategy_extra.items())
            adjustments = price_adjustments(plan, after)
            message = (f"AC charge limit set to {limit_kw:g} kW, then plan written "
                       f"and verified by read-back.")
            if strategy_extra and not strategy_ok:
                message += " WARNING: EMS settings did not verify."
            elif strategy_extra:
                message += " EMS settings verified."
            if adjustments:
                message += (f" Anker truncates prices to two decimals, so "
                            f"{len(adjustments)} price(s) were adjusted.")
            self.json(200, {
                "ok": True, "verified": True, "snapshot": snap,
                "diff": plan_diff(before, plan), "plan": after,
                "adjustments": adjustments, "limit_kw": limit_kw,
                "limit_set": True,
                "limit_response": limit_result,
                "limit_error": "",
                "strategy": after_strategy,
                "strategy_ok": strategy_ok,
                "message": message,
            })
        finally:
            s["lock"].release()


def main():
    ap = argparse.ArgumentParser(description="Local Anker X1 Time-of-Use editor")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--max-charge-kw", type=float, default=4.2,
                    help="grid charge ceiling you consider safe (advisory check)")
    ap.add_argument("--no-persist", action="store_true",
                    help="do not remember the session across restarts")
    ap.add_argument("--use-keychain", action="store_true",
                    help="keep the session-encryption key in the macOS Keychain")
    ap.add_argument("--secure-cookies", action="store_true",
                    help="mark the session cookie Secure (use behind HTTPS)")
    ap.add_argument("--probe", action="store_true",
                    help="run a read-only power-limit probe at startup")
    args = ap.parse_args()

    CONFIG.update(host=args.host, port=args.port, max_charge_kw=args.max_charge_kw,
                  persist=not args.no_persist, use_keychain=args.use_keychain,
                  startup_probe=args.probe, secure_cookie=args.secure_cookies)
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        log("WARNING: binding to a non-loopback address exposes the UI to your network.")
        log("         There is no TLS. Prefer an SSH tunnel.")

    if CONFIG["persist"]:
        restore_session()
    if CONFIG.get("startup_probe"):
        startup_probe()

    if daylight.astral_available():
        location = solar_location.warm_location()
        if location is None:
            log("sunrise/sunset: no location; using the 07:00-18:00 fallback")
        else:
            log(f"sunrise/sunset: using the {location.source} location estimate")
    else:
        log("sunrise/sunset: Astral unavailable; using the 07:00-18:00 fallback")

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True
    log(f"Anker TOU editor on http://{args.host}:{args.port}  (Ctrl-C to stop)")
    log("credentials are never stored; the session token is encrypted at rest in ~/.anker-tou"
        if CONFIG["persist"] else "session persistence disabled (--no-persist)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("stopping")
    finally:
        srv.server_close()


if __name__ == "__main__":
    main()
