#!/bin/sh
# Local Time-of-Use editor for the Anker SOLIX X1.
# Binds to loopback only; credentials are never persisted.
set -eu
cd "$(dirname "$0")"
exec python3 app/server.py --host 127.0.0.1 --port 8770
# pandacode-preview: http://127.0.0.1:8770
