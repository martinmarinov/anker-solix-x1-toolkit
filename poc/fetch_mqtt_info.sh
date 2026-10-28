#!/bin/sh
# Force the interpreter that has cryptography on this Mac.
# Usage: ./poc/fetch_mqtt_info.sh --email <you@example.com> --country GB
set -eu
DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec /usr/bin/python3 "$DIR/fetch_mqtt_info.py" "$@"
