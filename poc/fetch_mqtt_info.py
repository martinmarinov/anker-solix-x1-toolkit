#!/usr/bin/env python3
"""Fetch a fresh Anker MQTT mTLS identity for live-feed debugging.

Logs in with your own account, calls get_user_mqtt_info, and writes the
CA/certificate/private key + metadata to a private directory under captures/.
Nothing is printed except non-secret identifiers.

The login may invalidate the phone app's token. Everything written is 0600 and
captures/ is gitignored. Do not paste the PEM/key files into a chat or commit.

Usage:
    python3 poc/fetch_mqtt_info.py
    ANKER_EMAIL=you@example.com python3 poc/fetch_mqtt_info.py --country GB

Prompts for email (unless supplied) and password. Optional --base to override
the regional API host.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

try:
    import cryptography  # noqa: F401
except ModuleNotFoundError:
    print(
        "Missing Python package 'cryptography'.\n"
        "On this Mac, run the helper with the interpreter that already has it:\n"
        "  /usr/bin/python3 poc/fetch_mqtt_info.py --email <you@example.com> --country GB\n"
        "Do not use /usr/local/bin/python3 for this helper unless you install cryptography there.",
        file=sys.stderr,
    )
    raise SystemExit(2)

from anker_client import AnkerClient, AnkerError  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--email", default=None,
                    help="Anker account email; otherwise ANKER_EMAIL or prompt")
    ap.add_argument("--country", default="GB")
    ap.add_argument("--base", default=None)
    ap.add_argument("--out", default=None,
                    help="output directory (default captures/mqtt-info-<utc>)")
    args = ap.parse_args()

    email = (args.email or os.environ.get("ANKER_EMAIL") or "").strip()
    if not email:
        try:
            email = input("Anker email: ").strip()
        except EOFError:
            email = ""
    if not email:
        print("No email supplied.", file=sys.stderr)
        return 2

    try:
        password = getpass.getpass("Anker password: ")
    except EOFError:
        print("No terminal available for the password prompt; run this from a terminal.",
              file=sys.stderr)
        return 2
    if not password:
        print("No password supplied.", file=sys.stderr)
        return 2

    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    out = args.out or os.path.join(ROOT, "captures", f"mqtt-info-{stamp}")

    try:
        print("Logging in ...", flush=True)
        client = AnkerClient(email=email, password=password,
                             country=args.country, base=args.base)
        who = client.login()
        print("Logged in; fetching MQTT identity ...", flush=True)
        js = client._post("v1/openapi/devicemanage/get_user_mqtt_info", {},
                          timeout=20, attempts=2)
        info = js.get("data") or {}
    except AnkerError as e:
        print(f"Anker error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # network/TLS/JSON: show the type without dumping secrets
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1

    required = ("certificate_pem", "private_key", "aws_root_ca1_pem",
                "certificate_id", "thing_name", "endpoint_addr")
    missing = [k for k in required if not info.get(k)]
    if missing:
        print("Response is missing: " + ", ".join(missing), file=sys.stderr)
        print("Response keys: " + ", ".join(sorted(info.keys())), file=sys.stderr)
        return 1

    os.makedirs(out, mode=0o700, exist_ok=True)
    os.chmod(out, 0o700)

    # Full response is needed for the test client, so keep it private.
    info_path = os.path.join(out, "info.json")
    fd = os.open(info_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(info, fh, indent=1, sort_keys=True)
    for name, key in (("ca.pem", "aws_root_ca1_pem"),
                      ("cert.pem", "certificate_pem"),
                      ("key.pem", "private_key")):
        path = os.path.join(out, name)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(info[key])
    meta = {
        "fetched_utc": stamp,
        "endpoint_addr": info.get("endpoint_addr"),
        "thing_name": info.get("thing_name"),
        "app_name": info.get("app_name"),
        "user_id": info.get("user_id") or who.get("user_id"),
        "certificate_id": info.get("certificate_id"),
    }
    meta_path = os.path.join(out, "meta.json")
    fd = os.open(meta_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=1, sort_keys=True)

    print("Wrote fresh MQTT material to:", out)
    print("endpoint:", meta["endpoint_addr"])
    print("thing_name:", meta["thing_name"])
    print("certificate_id:", meta["certificate_id"])
    print("The directory is 0700, files are 0600, and captures/ is gitignored.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
