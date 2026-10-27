"""Live telemetry from Anker's MQTT broker, per authenticated session.

One long-lived, read-only mTLS subscription; credentials are fetched with the
user's own client and certificates live in a private temp directory that is
removed when the feed stops. No polling, no publishing, no global state.
"""
from __future__ import annotations

import base64
import json
import os
import random
import shutil
import ssl
import tempfile
import threading
import time

import paho.mqtt.client as mqtt

from anker_client import AnkerClient

FLOW_KEYS = ("p2lp", "p2bp", "p2gp", "g2lp", "g2bp", "b2lp", "b2gp")
POWER_KEYS = ("pp", "gp", "bp", "lp")
STATUS_KEYS = ("ps", "gs", "bs", "es")


def parse_frame(raw: bytes) -> dict | None:
    try:
        outer = json.loads(raw)
        inner = json.loads(outer["payload"])
        trans = json.loads(base64.b64decode(inner["trans"]))
    except Exception:
        return None
    if trans.get("type") != "ems":
        return None
    data = trans.get("data") or {}
    snap = {"local_time": trans.get("localTime"), "soc": data.get("soc")}
    for k in POWER_KEYS + FLOW_KEYS + STATUS_KEYS:
        snap[k] = data.get(k)
    return snap


class LiveFeed:
    def __init__(self, client, pn: str, sn: str):
        self.client = client
        # Own HTTP session: the feed thread must not share requests.Session
        # with the web request threads.
        self.api = AnkerClient.resume(client.token, client.user_id,
                                      client.country, client.base, client.nick)
        self.pn, self.sn = pn, sn
        self.dir = tempfile.mkdtemp(prefix="anker-live-")
        os.chmod(self.dir, 0o700)
        self.snapshot: dict = {"available": False, "reason": "starting"}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._mqtt = None

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="anker-live", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.stop_event.set()
        if self._mqtt is not None:
            try:
                self._mqtt.disconnect()
            except Exception:
                pass
        shutil.rmtree(self.dir, ignore_errors=True)

    def _set(self, **kw) -> None:
        with self.lock:
            self.snapshot = kw

    def get(self) -> dict:
        with self.lock:
            snap = dict(self.snapshot)
        if snap.get("available"):
            snap["age_s"] = max(0, int(time.time()) - snap.get("ts", 0))
            snap["stale"] = snap["age_s"] > 30
        return snap

    # -- worker --------------------------------------------------------

    def _fetch_info(self) -> dict:
        js = self.api._post("v1/openapi/devicemanage/get_user_mqtt_info", {},
                            timeout=20, attempts=2)
        return js["data"]

    def _write_certs(self, info: dict) -> tuple[str, str, str]:
        paths = []
        for name, key in (("ca.pem", "aws_root_ca1_pem"),
                          ("cert.pem", "certificate_pem"),
                          ("key.pem", "private_key")):
            path = os.path.join(self.dir, name)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as fh:
                fh.write(info[key])
            os.chmod(path, 0o600)
            paths.append(path)
        return tuple(paths)

    def _run(self) -> None:
        backoff = 5
        while not self.stop_event.is_set():
            client = None
            try:
                info = self._fetch_info()
                ca, cert, key = self._write_certs(info)
                client = mqtt.Client(
                    client_id=f"{info['thing_name']}_{random.randint(0, 99999):05d}",
                    protocol=mqtt.MQTTv311)
                client.tls_set(ca_certs=ca, certfile=cert, keyfile=key,
                               tls_version=ssl.PROTOCOL_TLS_CLIENT)
                topic = f"dt/anker_power/{self.pn}/{self.sn}/#"
                client.on_connect = (
                    lambda c, u, f, rc: c.subscribe(topic, qos=0) if rc == 0 else None)
                client.on_message = self._on_message
                self._mqtt = client
                client.connect(info["endpoint_addr"], 8883, 30)
                backoff = 5
                self._set(available=False, reason="connecting")
                client.loop_forever(retry_first_connection=True)
            except Exception as e:
                self._set(available=False, reason=type(e).__name__)
            finally:
                if client is not None:
                    try:
                        client.disconnect()
                    except Exception:
                        pass
            if self.stop_event.wait(backoff):
                break
            backoff = min(60, backoff * 2)

    def _on_message(self, client, userdata, msg) -> None:
        snap = parse_frame(msg.payload)
        if not snap:
            return
        snap.update(available=True, ts=int(time.time()))
        with self.lock:
            self.snapshot = snap
