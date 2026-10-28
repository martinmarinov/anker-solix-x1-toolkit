#!/usr/bin/env python3
"""
anker_tou.py — reference client for Anker SOLIX X1 (A17B1/A5101) Time-of-Use plans.

Why this exists
---------------
The Anker app's TOU editor uses 24 one-hour ticks. The payload field is a plain
"HH:mm" string, so the cloud can store `:30`, but the X1/D156 device applies the
previous whole hour (23:30 -> 23:00, 05:30 -> 05:00). This POC only emits whole
hours.

This tool never invents a payload: it reads YOUR current plan, changes only the
time strings (or the valley/peak pair you ask for), and posts the same envelope
back. Everything it does not understand is passed through untouched.

Safety model
------------
  * Read-only unless you pass --apply.
  * Every write first saves a timestamped snapshot under captures/.
  * After a write it re-reads the plan and compares; on mismatch it restores
    the snapshot automatically.
  * It refuses to touch firmware/OTA/mode/SOC endpoints. Only the rate-plan
    endpoints below are reachable from this file.
  * It refuses the Dynamic Tariff service outright. That path is what Anker
    support suggested for sub-hour slots, and it is the configuration that
    caused a documented overcurrent breaker trip: it charges at full power with
    no load-aware limiting. Change the TIMES, keep the MODE on Time of Use.
  * `cmd` is never guessed: it is copied from --cmd or omitted.

Usage
-----
  python3 -m pip install --user requests cryptography

  # offline crypto self-test, no network:
  python3 anker_tou.py --selftest

  # read-only: who am I, which sites/devices, what is the live plan?
  python3 anker_tou.py --email you@example.com --country GB --dump

  # dry run: show the 23:00->05:00 rewrite, write nothing
  python3 anker_tou.py --dump --valley 23:00-05:00 --peak 05:00-23:00 --from captures/plan-*.json

  # for real (prompts for confirmation)
  python3 anker_tou.py --apply --valley 23:00-05:00 --peak 05:00-23:00 --from captures/plan-<ts>.json

  # restore a snapshot
  python3 anker_tou.py --apply --rollback captures/plan-<ts>.json

The password is read from $ANKER_PASSWORD or prompted with getpass; it is never
written to disk or logged.

Status: the crypto and request shapes are taken from the shipped app and a
public client implementation. The X1 *write* path is NOT device-tested by the
author. Run --dump first, then a dry run, then one manual write, and verify the
result on the device before trusting it.
"""

from __future__ import annotations

import argparse
import base64
import getpass
import glob
import hashlib
import json
import os
import sys
import time
import uuid
from datetime import datetime, timezone

try:
    import requests
except ImportError:  # only needed for network calls, not for --selftest
    requests = None

try:
    from cryptography.hazmat.backends import default_backend
    from cryptography.hazmat.primitives import hashes, padding, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
except ImportError:
    sys.exit("missing dependency: python3 -m pip install --user cryptography")


# --- constants -------------------------------------------------------------

# Server's static ECDH P-256 public key (uncompressed point, hex). This is the
# key the real app encrypts the login password against; it is public material.
API_PUBLIC_KEY_HEX = (
    "04c5c00c4f8d1197cc7c3167c52bf7acb054d722f0ef08dcd7e0883236e0d72a3"
    "868d9750cb47fa4619248f3d83f0f662671dadc6e2d31c2f41db0161651c7c076"
)

BASES = {
    "com": "https://ankerpower-api.anker.com",
    "eu": "https://ankerpower-api-eu.anker.com",
}

# Countries served by the EU cluster. Anything else -> com. Wrong region still
# logs in but returns zero sites, so --base lets you override.
EU_COUNTRIES = {
    "AT", "BE", "BG", "CH", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR",
    "GB", "GR", "HR", "HU", "IE", "IS", "IT", "LI", "LT", "LU", "LV", "MT",
    "NL", "NO", "PL", "PT", "RO", "SE", "SI", "SK",
}

APP_NAME = "anker_power"
APP_VERSION = "3.24.3"

# Only these paths are ever called. Anything else is intentionally absent.
P_LOGIN = "passport/login"
P_SITES = "power_service/v1/site/get_site_list"
P_DEVICES = "power_service/v1/site/list_user_devices"
P_GET_PLAN_HES = "charging_hes_svc/get_utility_rate_plan"
P_GET_PLAN_ENERGY = "charging_energy_service/get_utility_rate_plan"
P_SAVE_PLAN_HES = "charging_hes_svc/update_hes_utility_rate_plan"
P_SAVE_TOU = "charging_hes_svc/save_time_of_use"
P_SITE_PRICE = "power_service/v1/site/update_site_price"
P_POWER_LIMIT = "power_service/v1/site/get_power_limit"

CAPTURE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "captures")


# --- helpers ---------------------------------------------------------------

def die(msg: str, code: int = 1) -> None:
    print(f"error: {msg}", file=sys.stderr)
    raise SystemExit(code)


def utc_ms() -> int:
    return int(time.time() * 1000)


def hhmm(value: str) -> str:
    """Validate a 'HH:mm' clock string, 00:00..23:59."""
    try:
        h, m = value.split(":")
        h, m = int(h), int(m)
    except Exception:
        die(f"bad time {value!r}; expected HH:mm")
    if not (0 <= h <= 24 and 0 <= m <= 59) or (h == 24 and m != 0):
        die(f"bad time {value!r}; expected 00:00..24:00")
    if m != 0:
        die(f"{value!r} is not a whole hour; the X1/D156 device floors :30 to the hour. "
            "Use HH:00 (see docs/FIRMWARE-LIMITS.md).")
    return f"{h:02d}:{m:02d}"


def parse_range(spec: str) -> tuple[str, str]:
    if "-" not in spec:
        die(f"bad range {spec!r}; expected HH:mm-HH:mm")
    a, b = spec.split("-", 1)
    return hhmm(a.strip()), hhmm(b.strip())


def find_lists(node, key: str):
    """Yield every list stored under `key` anywhere in a nested structure."""
    if isinstance(node, dict):
        for k, v in node.items():
            if k == key and isinstance(v, list):
                yield v
            else:
                yield from find_lists(v, key)
    elif isinstance(node, list):
        for v in node:
            yield from find_lists(v, key)


# --- client ----------------------------------------------------------------

class AnkerClient:
    def __init__(self, email: str, password: str, country: str = "GB",
                 base: str | None = None, verbose: bool = False):
        self.email = email
        self._password = password
        self.country = country.upper()
        self.base = base or BASES["eu" if self.country in EU_COUNTRIES else "com"]
        self.verbose = verbose
        if requests is None:
            die("missing dependency: python3 -m pip install --user requests")
        self.http = requests.Session()
        self.token: str | None = None
        self.user_id: str | None = None
        self.gtoken: str | None = None
        self.nick: str | None = None
        self._make_keys()

    # -- crypto ------------------------------------------------------------

    def _make_keys(self) -> None:
        self._priv = ec.generate_private_key(ec.SECP256R1(), default_backend())
        self._pub = self._priv.public_key()
        self._shared = self._priv.exchange(
            ec.ECDH(),
            ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256R1(), bytes.fromhex(API_PUBLIC_KEY_HEX)
            ),
        )

    def _pub_hex(self) -> str:
        return self._pub.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint,
        ).hex()

    def _encrypt(self, raw: str) -> str:
        """AES-256-CBC, key = ECDH shared secret, IV = first 16 bytes, PKCS7."""
        cipher = Cipher(algorithms.AES(self._shared), modes.CBC(self._shared[:16]),
                        backend=default_backend())
        padder = padding.PKCS7(128).padder()
        data = padder.update(raw.encode()) + padder.finalize()
        enc = cipher.encryptor()
        return base64.b64encode(enc.update(data) + enc.finalize()).decode()

    # -- http --------------------------------------------------------------

    def _headers(self) -> dict:
        h = {
            "content-type": "application/json",
            "model-type": "PHONE",
            "app-name": APP_NAME,
            "app-version": APP_VERSION,
            "os-type": "android",
            "country": self.country,
            "timezone": "GMT+00:00",
            "language": "en",
            "x-request-id": str(uuid.uuid4()),
        }
        if self.token:
            h["x-auth-token"] = self.token
        if self.gtoken:
            h["gtoken"] = self.gtoken
        return h

    def call(self, path: str, body: dict | None = None) -> dict:
        url = f"{self.base}/{path}"
        if self.verbose:
            print(f"  -> POST {path}", file=sys.stderr)
        r = self.http.post(url, json=body or {}, headers=self._headers(), timeout=30)
        if r.status_code != 200:
            die(f"HTTP {r.status_code} from {path}: {r.text[:300]}")
        try:
            js = r.json()
        except ValueError:
            die(f"non-JSON reply from {path}: {r.text[:300]}")
        if js.get("code") not in (0, None):
            die(f"{path} -> code={js.get('code')} msg={js.get('msg')}")
        return js

    # -- api ---------------------------------------------------------------

    def login(self) -> None:
        body = {
            "ab": self.country,
            "client_secret_info": {"public_key": self._pub_hex()},
            "enc": 0,
            "email": self.email,
            "password": self._encrypt(self._password),
            "time_zone": 0,
            "transaction": str(utc_ms()),
        }
        js = self.call(P_LOGIN, body)
        data = js.get("data") or {}
        self.token = data.get("auth_token")
        self.user_id = str(data.get("user_id") or "")
        self.nick = data.get("nick_name")
        if not self.token:
            die(f"login returned no auth_token: {json.dumps(js)[:300]}")
        self.gtoken = hashlib.md5(self.user_id.encode()).hexdigest()
        exp = data.get("token_expires_at")
        print(f"logged in as {self.nick or self.email} (user {self.user_id[:8]}…) "
              f"via {self.base}" + (f", token expires {exp}" if exp else ""))

    def sites(self) -> list[dict]:
        js = self.call(P_SITES, {})
        data = js.get("data") or {}
        return data.get("site_list") or data.get("sites") or []

    def devices(self, site_id) -> list[dict]:
        out = []
        for body in ({"site_id": site_id}, {"siteId": site_id}):
            try:
                js = self.call(P_DEVICES, body)
            except SystemExit:
                continue
            data = js.get("data") or {}
            if isinstance(data, dict):
                out = data.get("device_list") or data.get("devices") or []
            elif isinstance(data, list):
                out = data
            if out:
                return out
        return out

    def power_limits(self, site_id) -> dict | None:
        """Best-effort read of the station's configured power limits.

        Read-only. Returns None if this build/site does not expose the call.
        The limits that matter for breaker safety are the AC/grid charge limit
        (`acPowerLimit`) and any station output limit.
        """
        try:
            js = self.call(P_POWER_LIMIT, {"site_id": site_id})
        except SystemExit:
            return None
        return js.get("data")

    def get_plan(self, site_id, sn: str) -> dict:
        """Try the HES endpoint, fall back to the energy-service one."""
        for path, body in (
            (P_GET_PLAN_HES, {"site_id": site_id, "sn": sn}),
            (P_GET_PLAN_ENERGY, {"siteId": site_id, "sn": sn}),
        ):
            try:
                js = self.call(path, body)
            except SystemExit as e:
                if self.verbose:
                    print(f"  ({path} failed: {e})", file=sys.stderr)
                continue
            if js.get("data"):
                js["_endpoint"] = path
                return js
        die("could not read a rate plan from either known endpoint")

    def save_plan(self, path: str, body: dict) -> dict:
        return self.call(path, body)


# --- plan surgery ----------------------------------------------------------

def plan_payload(read_response: dict) -> tuple[dict, str]:
    """Return (payload, endpoint) from a get_plan response.

    The response wraps the plan in `data`; we keep the whole data object so
    unknown/extra fields survive the round-trip.
    """
    data = read_response.get("data")
    if not isinstance(data, dict):
        die("plan response has no object under 'data'")
    return data, read_response.get("_endpoint", P_GET_PLAN_HES)


def retime(data: dict, valley: tuple[str, str], peak: tuple[str, str]) -> int:
    """Rewrite period boundaries in place. Returns number of rows changed.

    Handles the string shape `start_time_period`/`end_time_period` (minutes).
    Refuses the integer-hour shapes (`start_time`: 0..24), because they are a
    different payload shape; the app uses `HH:mm` strings for whole hours.
    """
    changed = 0
    rows = list(find_lists(data, "peak_valley_prices"))
    if not rows:
        die("no 'peak_valley_prices' in this plan; this does not look like an X1 "
            "rate plan. Dump it and inspect before writing.")

    for row_list in rows:
        for row in row_list:
            if not isinstance(row, dict):
                continue
            if "start_time_period" not in row:
                continue
            ptype = row.get("peak_type")
            # X1 schema runs cheapest -> dearest: 1=Super Off-Peak ... 4=Peak.
            # (Verified against a live plan: type 1 buy=0, type 4 buy=0.30.)
            if ptype == 1:
                s, e = valley
            elif ptype == 4:
                s, e = peak
            else:
                continue
            if (row.get("start_time_period"), row.get("end_time_period")) != (s, e):
                row["start_time_period"] = s
                row["end_time_period"] = e
                changed += 1

    if changed == 0:
        # Could be a no-op (plan already has the requested boundaries) or an
        # unexpected shape. Distinguish the two before complaining.
        for row_list in rows:
            for row in row_list:
                if not isinstance(row, dict) or "start_time_period" not in row:
                    continue
                ptype = row.get("peak_type")
                want = valley if ptype == 1 else peak if ptype == 4 else None
                if want and (row.get("start_time_period"),
                             row.get("end_time_period")) == want:
                    return 0  # already applied, nothing to do
        for row_list in rows:
            for row in row_list:
                if not isinstance(row, dict):
                    continue
                if "start_time" in row and "start_time_period" not in row:
                    die("this plan uses INTEGER HOURS (start_time/end_time). Minutes "
                        "cannot be represented in that shape -- that would be a genuine "
                        "firmware limit. Do not force it.")
        die("no valley(4)/peak(1) rows matched; inspect the snapshot first")
    return changed


# Fields the writer is allowed to touch. Everything else must be byte-identical
# to the live plan: mode, price_type, seasons, day_type, peak_type, prices,
# energy_unit. This is the mechanical enforcement of the project's hard rule --
# change the TIMES, never the control law.
ALLOWED_DIFF_KEYS = {"start_time_period", "end_time_period"}


def diff_paths(a, b, path=""):
    """Yield paths where two JSON structures differ."""
    if type(a) is not type(b):
        yield path or "<root>"
        return
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                yield f"{path}.{k}".lstrip(".")
            else:
                yield from diff_paths(a[k], b[k], f"{path}.{k}".lstrip("."))
    elif isinstance(a, list):
        if len(a) != len(b):
            yield f"{path}[len]"
            return
        for i, (x, y) in enumerate(zip(a, b)):
            yield from diff_paths(x, y, f"{path}[{i}]")
    elif a != b:
        yield path or "<root>"


def assert_times_only(before: dict, after: dict) -> None:
    """Refuse to write anything except period boundaries.

    Mirrors the design requirement: the final client must reproduce the app's
    Time-of-Use editor and nothing else. If a proposed payload changes mode,
    prices, tariff types, seasons or day types, that is a bug or an attempt to
    switch schedulers (e.g. to the window-driven Dynamic Tariff controller).
    """
    bad = [p for p in diff_paths(before, after)
           if p.rsplit(".", 1)[-1].split("[")[0] not in ALLOWED_DIFF_KEYS]
    if bad:
        die("refusing to write: payload changes more than the period times.\n"
            "  Offending fields: " + ", ".join(bad[:8]) + ("…" if len(bad) > 8 else "") +
            "\n  The design rule is: reproduce the Time-of-Use editor, change only\n"
            "  whole-hour boundaries, keep the target-driven controller. A payload\n"
            "  that flips mode/price_type would switch control laws (see REPORT.md 4.6).")


def summarise(data: dict) -> None:
    rows = list(find_lists(data, "peak_valley_prices"))
    if not rows:
        print("  (no peak_valley_prices found; raw keys: "
              f"{sorted(data.keys())[:12]})")
        return
    for i, rl in enumerate(rows):
        print(f"  session {i + 1}:")
        for r in rl:
            if not isinstance(r, dict):
                continue
            print("    day_type={} peak_type={} {} -> {}  buy={} {}".format(
                r.get("day_type"), r.get("peak_type"),
                r.get("start_time_period"), r.get("end_time_period"),
                r.get("buy", "?"), r.get("energy_unit", "")))


def snapshot(data: dict, tag: str) -> str:
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(CAPTURE_DIR, f"plan-{stamp}-{tag}.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
    return path


# --- self test -------------------------------------------------------------

def selftest() -> None:
    """Prove the ECDH+AES path works without touching the network."""
    import secrets

    server = ec.generate_private_key(ec.SECP256R1(), default_backend())
    client = ec.generate_private_key(ec.SECP256R1(), default_backend())
    s_shared = server.exchange(
        ec.ECDH(),
        ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(),
            client.public_key().public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint),
        ),
    )
    c_shared = client.exchange(
        ec.ECDH(),
        ec.EllipticCurvePublicKey.from_encoded_point(
            ec.SECP256R1(),
            server.public_key().public_bytes(
                serialization.Encoding.X962,
                serialization.PublicFormat.UncompressedPoint),
        ),
    )
    assert s_shared == c_shared, "ECDH shared secrets differ"
    assert len(c_shared) == 32, "expected 32-byte P-256 secret"

    pw = "correct horse battery staple"
    cipher = Cipher(algorithms.AES(c_shared), modes.CBC(c_shared[:16]),
                    backend=default_backend())
    padder = padding.PKCS7(128).padder()
    padded = padder.update(pw.encode()) + padder.finalize()
    enc = cipher.encryptor()
    blob = enc.update(padded) + enc.finalize()

    dec = Cipher(algorithms.AES(c_shared), modes.CBC(c_shared[:16]),
                 backend=default_backend()).decryptor()
    unpad = padding.PKCS7(128).unpadder()
    out = unpad.update(dec.update(blob) + dec.finalize()) + unpad.finalize()
    assert out.decode() == pw, "AES round-trip failed"
    print("selftest ok: P-256 ECDH + AES-256-CBC/PKCS7 round-trip, 32-byte secret")


# --- main ------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(
        description="Read/rewrite Anker SOLIX X1 Time-of-Use plans at minute precision.")
    ap.add_argument("--selftest", action="store_true", help="offline crypto self-test")
    ap.add_argument("--email")
    ap.add_argument("--country", default="GB")
    ap.add_argument("--base", help="override API base URL")
    ap.add_argument("--site", help="site id (defaults to the first site)")
    ap.add_argument("--sn", help="device serial (defaults to first A17B1/A5101)")
    ap.add_argument("--dump", action="store_true", help="read-only: list + plan")
    ap.add_argument("--from", dest="from_file", help="plan snapshot JSON to rewrite")
    ap.add_argument("--valley", default="23:00-05:00", help="valley window (default 23:00-05:00)")
    ap.add_argument("--peak", default="05:00-23:00", help="peak window (default 05:00-23:00)")
    ap.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    ap.add_argument("--rollback", help="re-upload a snapshot file")
    ap.add_argument("--endpoint", default=P_SAVE_PLAN_HES, help="write endpoint")
    ap.add_argument("--i-understand-dynamic-risk", action="store_true",
                    help="permit the Dynamic Tariff endpoint (NOT recommended; see REPORT.md 4.4)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        selftest()
        return

    if not args.email:
        die("--email is required (or use --selftest)")
    password = os.environ.get("ANKER_PASSWORD") or getpass.getpass("Anker password: ")

    cli = AnkerClient(args.email, password, args.country, args.base, args.verbose)
    cli.login()

    # Resolve site/device.
    site_id = args.site
    if not site_id:
        sites = cli.sites()
        if not sites:
            die("no sites returned -- wrong region? try the other --base")
        site_id = sites[0].get("site_id") or sites[0].get("siteId")
        print(f"site: {sites[0].get('site_name') or sites[0].get('name')} ({site_id})")

    sn = args.sn
    if not sn:
        devs = cli.devices(site_id)
        for d in devs:
            print(f"  device: {d.get('device_sn') or d.get('sn')} "
                  f"{d.get('product_code') or d.get('device_pn') or ''}")
        for d in devs:
            code = str(d.get("product_code") or d.get("device_pn") or "")
            if code.startswith(("A17B1", "A5101", "A5102", "A5103")):
                sn = d.get("device_sn") or d.get("sn")
                break
        sn = sn or (devs[0].get("device_sn") or devs[0].get("sn") if devs else None)
    if not sn:
        die("could not find a device serial; pass --sn")

    if args.dump or not args.from_file and not args.rollback:
        limits = cli.power_limits(site_id)
        if limits is not None:
            print("power limits (read-only):")
            print(json.dumps(limits, indent=2)[:1200])
            print("  ^ check the grid/AC charge limit BEFORE writing a schedule.")
            print("    If it is at/above the breaker rating, lower it in the app first")
            print("    (Station -> Settings -> Power Limit / Grid Charging).")
        else:
            print("power limits: not exposed by this build/site (skipped)")

        plan = cli.get_plan(site_id, sn)
        data, endpoint = plan_payload(plan)
        print(f"plan from {endpoint}:")
        summarise(data)
        path = snapshot(data, "read")
        print(f"saved {path}")
        if not (args.from_file or args.rollback):
            return

    # Load the payload to write.
    if args.rollback:
        with open(args.rollback, encoding="utf-8") as fh:
            data = json.load(fh)
        print(f"rollback payload: {args.rollback}")
    else:
        with open(args.from_file, encoding="utf-8") as fh:
            data = json.load(fh)
        valley = parse_range(args.valley)
        peak = parse_range(args.peak)
        n = retime(data, valley, peak)
        print(f"rewrote {n} period row(s): valley {valley[0]}-{valley[1]}, "
              f"peak {peak[0]}-{peak[1]}")
        print("resulting plan:")
        summarise(data)

    if not args.apply:
        out = snapshot(data, "dryrun")
        print(f"\nDRY RUN — nothing sent. Proposed payload saved to {out}")
        print("re-run with --apply to write it.")
        return

    if "dynamic" in args.endpoint and not args.i_understand_dynamic_risk:
        die("refusing to write through the Dynamic Tariff service.\n"
            "  That path is associated with an overcurrent incident: it charges at full\n"
            "  power without load-aware limiting (REPORT.md 4.4). The goal is to change\n"
            "  the schedule TIMES, not the MODE. Use the default TOU endpoint.\n"
            "  If you truly mean it, pass --i-understand-dynamic-risk.")

    live = cli.get_plan(site_id, sn)["data"]
    before = snapshot(live, "before-write")
    print(f"snapshot before write: {before}")

    if not args.rollback:
        # Hard invariant: times only. Same mode, same controller, same prices.
        assert_times_only(live, data)
        print("invariant ok: only start_time_period/end_time_period differ")

    body = {"site_id": site_id, "sn": sn, **data}
    try:
        resp = cli.save_plan(args.endpoint, body)
        print(f"write accepted: code={resp.get('code')} msg={resp.get('msg')}")
    except SystemExit as e:
        print(f"write FAILED: {e}. Nothing changed.")
        raise

    # Verify by reading back.
    after = cli.get_plan(site_id, sn)["data"]
    snapshot(after, "after-write")
    print("read-back plan:")
    summarise(after)

    def times(d):
        return sorted(
            (r.get("day_type"), r.get("peak_type"),
             r.get("start_time_period"), r.get("end_time_period"))
            for rl in find_lists(d, "peak_valley_prices") for r in rl
            if isinstance(r, dict)
        )

    if times(after) != times(data):
        print("\n!! read-back does NOT match what was sent.")
        print("   This is the signal that the cloud/firmware changed or ignored "
              "an hourly boundary.")
        print(f"   Restoring snapshot {before} …")
        cli.save_plan(args.endpoint, {"site_id": site_id, "sn": sn,
                                      **json.load(open(before, encoding="utf-8"))})
        print("   restore sent. Verify with --dump.")
        raise SystemExit(2)

    print("\nread-back matches. Watch one night to confirm the inverter actually "
          "starts grid charging at the new boundary before trusting it.")


if __name__ == "__main__":
    main()
