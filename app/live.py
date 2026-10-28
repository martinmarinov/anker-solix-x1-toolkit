"""Live telemetry from Anker's MQTT broker, per authenticated session.

One long-lived mTLS subscription plus a read-only realtime wake publish when no
frame arrives. Credentials are fetched with the user's own client and live in a
private temp directory that is removed when the feed stops. No polling of the
device and no state-changing writes.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
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

# X1 accepts a JSON cmd:9 request for 60 samples at 5 s intervals (5 minutes).
TRIGGER_GRACE_S = 12
TRIGGER_COOLDOWN_S = 240


def build_trigger(pn: str, sn: str, account_id: str, now: int | None = None) -> tuple[str, str]:
    """Return (topic, message) for the X1 realtime-data request."""
    now = int(time.time() if now is None else now)
    trans = {
        "sn": sn,
        "subDeviceType": "ems",
        "subSn": sn,
        "messageId": secrets.token_hex(16),
        "cmdId": 4,
        "config": {"count": 60, "interval": 5},
        "startTime": 0,
        "duration": 0,
        "primary": 0,
        "type": 0,
    }
    payload = {
        "account_id": account_id,
        "device_sn": sn,
        "trans": base64.b64encode(
            json.dumps(trans, separators=(",", ":")).encode()).decode(),
    }
    head = {
        "version": "1.0.0.1",
        "client_id": "cloud",
        "sess_id": secrets.token_hex(16),
        "msg_seq": 1,
        "seed": "",
        "timestamp": now,
        "cmd_status": 2,
        "cmd": 9,
        "sign_code": 0,
    }
    message = json.dumps(
        {"head": head,
         "payload": json.dumps(payload, separators=(",", ":"))},
        separators=(",", ":"))
    return f"cmd/anker_power/{pn}/{sn}/req", message


def parse_frame(raw: bytes) -> dict | None:
    try:
        outer = json.loads(raw)
        inner = json.loads(outer["payload"])
        trans = json.loads(base64.b64decode(inner["trans"]))
    except Exception:
        return None
    if not isinstance(trans, dict) or trans.get("type") != "ems":
        return None
    data = trans.get("data")
    if not isinstance(data, dict):
        return None
    if not any(k in data for k in POWER_KEYS + FLOW_KEYS + STATUS_KEYS + ("soc",)):
        return None
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
        self._connected_at: float | None = None
        self._last_trigger_at = 0.0

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
        now = time.time()
        trigger = False
        with self.lock:
            snap = dict(self.snapshot)
            stale = (bool(snap.get("available"))
                     and now - snap.get("ts", 0) > 30)
            if ((not snap.get("available") or stale) and self._connected_at
                    and now - self._connected_at >= TRIGGER_GRACE_S
                    and now - self._last_trigger_at >= TRIGGER_COOLDOWN_S
                    and self._mqtt is not None and self._mqtt.is_connected()):
                self._last_trigger_at = now
                snap = {"available": False, "reason": "waking"}
                self.snapshot = snap
                trigger = True
        if trigger:
            self._publish_trigger()
        if snap.get("available"):
            snap["age_s"] = max(0, int(now) - snap.get("ts", 0))
            snap["stale"] = snap["age_s"] > 30
        return snap

    def _publish_trigger(self) -> None:
        if self._mqtt is None or not self._mqtt.is_connected():
            return
        topic, message = build_trigger(
            self.pn, self.sn, self.api.user_id or self.client.user_id)
        try:
            info = self._mqtt.publish(topic, message, qos=1)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                self._set(available=False, reason="wake publish failed")
        except Exception:
            self._set(available=False, reason="wake publish failed")

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

    @staticmethod
    def _client_id(info: dict) -> str:
        """Build the app's authorized MQTT identity.

        The broker ACL is keyed on the app's generated client-id:
        ``android-<app_name>-<user_id>-<certificate_id>``. The old
        ``thing_name_<random>`` form connects but gets SUBACK 128 (denied)
        for every subscription. A suffix is allowed, so ``-web`` lets the
        feed coexist with the phone app instead of taking over its session.
        """
        account = info.get("user_id") or str(info.get("thing_name", "")).split(
            "-anker_power")[0]
        cert_id = info.get("certificate_id")
        if not cert_id:
            raise ValueError("get_user_mqtt_info returned no certificate_id")
        app_name = info.get("app_name") or "anker_power"
        return f"android-{app_name}-{account}-{cert_id}-web"

    def _on_connect(self, client, userdata, flags, rc) -> None:
        if rc != 0:
            self._set(available=False, reason=f"mqtt connect rc={rc}")
            return
        # ems_second is the actual realtime telemetry topic; the other
        # dt topics are metadata/partial updates and are kept as well.
        for suffix in ("troubleshot_info/ems_second", "basic_info",
                       "param_info", "state_info"):
            client.subscribe(f"dt/anker_power/{self.pn}/{self.sn}/{suffix}", qos=0)
        self._connected_at = time.time()

    def _on_subscribe(self, client, userdata, mid, granted_qos, properties=None) -> None:
        if any(qos == 128 for qos in granted_qos):
            self._set(available=False, reason="mqtt subscription denied")

    def _run(self) -> None:
        backoff = 5
        while not self.stop_event.is_set():
            client = None
            try:
                info = self._fetch_info()
                ca, cert, key = self._write_certs(info)
                client = mqtt.Client(
                    client_id=self._client_id(info),
                    protocol=mqtt.MQTTv311)
                client.tls_set(ca_certs=ca, certfile=cert, keyfile=key,
                               tls_version=ssl.PROTOCOL_TLS_CLIENT)
                client.on_connect = self._on_connect
                client.on_subscribe = self._on_subscribe
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
