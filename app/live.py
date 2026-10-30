"""Live telemetry from Anker's MQTT broker, per authenticated session.

One long-lived mTLS subscription plus a read-only realtime wake publish when no
frame arrives. Credentials are fetched with the user's own client and live in a
private temp directory that is removed when the feed stops. No polling of the
device and no state-changing writes.

The user can choose a 5-60 s refresh interval. The device accepts a 5 s stream
of up to 60 samples. A larger window above 5 s is not yet confirmed (the wall
panel's own 5 s wake made the probe ambiguous), so the feed asks for two frames
at a time and renews after the second one. A concurrent
5 s wake from another client (for example the wall panel's older build) is
harmless: its frames arrive faster than the configured interval, so the feed
simply does not publish its own wake.
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
from collections import deque

import paho.mqtt.client as mqtt

from anker_client import AnkerClient
from limits import fallback_limit, limit_from_ems
from power_flow import battery_net_w, has_solar_fields, home_w, solar_w

FLOW_KEYS = ("p2lp", "p2bp", "p2gp", "g2lp", "g2bp", "b2lp", "b2gp")
POWER_KEYS = ("pp", "gp", "bp", "lp")
STATUS_KEYS = ("ps", "gs", "bs", "es")

MIN_INTERVAL_S = 5
MAX_INTERVAL_S = 60
INTERVAL_STEP_S = 5
DEFAULT_INTERVAL_S = 5

# The captured production frame is 60 samples at 5 s. Above 5 s the device
# accepts a small count reliably; two frames is the confirmed shape. Keeping
# the window short also bounds a mid-window settings change and any queued
# wake from another client.
REALTIME_MAX_COUNT = 60
FAST_INTERVAL_S = 5
FOLLOW_ON_COUNT = 2

TRIGGER_GRACE_S = 12
WAKE_TIMEOUT_S = 60
# Suppress the Refreshing notice for the first moments of a wake. A healthy
# renewal is answered on the next frame; flashing the label for one poll is
# noise. The snapshot still reports waking=True; only the display flag waits.
WAKING_NOTICE_GRACE_S = 2
BATTERY_AVG_WINDOW_S = 600
CHARGE_LIMIT_GRACE_S = 300
CHARGE_LIMIT_REFRESH_S = 6 * 3600

BATTERY_MODULE_KWH = 5.0
BATTERY_RESERVE_SOC = 5.0


def normalise_interval(value) -> int:
    """Clamp any stored or submitted value to a supported 5 s step."""
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError):
        return DEFAULT_INTERVAL_S
    number = max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, number))
    number = int(round(number / INTERVAL_STEP_S) * INTERVAL_STEP_S)
    return max(MIN_INTERVAL_S, min(MAX_INTERVAL_S, number))


def count_for_interval(interval_s: int) -> int:
    """Number of frames to request for one wake.

    The 5 s mode keeps the proven 60-frame, 5-minute production window. Above
    5 s, use the confirmed two-frame shape; the server renews after the second
    frame instead of asking the device for a long, unverified window.
    """
    interval = normalise_interval(interval_s)
    if interval <= FAST_INTERVAL_S:
        return REALTIME_MAX_COUNT
    return FOLLOW_ON_COUNT


def stale_after_s(interval_s: int) -> int:
    """Age at which a frame is genuinely old for this cadence."""
    interval = normalise_interval(interval_s)
    return max(30, int(round(interval * 1.5)) + 5)


def trigger_cooldown_s(interval_s: int) -> int:
    """Do not publish another wake immediately after a failed one."""
    return max(30, normalise_interval(interval_s))


def build_trigger(pn: str, sn: str, account_id: str, now: int | None = None,
                  interval_s: int = DEFAULT_INTERVAL_S) -> tuple[str, str]:
    """Return (topic, message) for the X1 realtime-data request."""
    now = int(time.time() if now is None else now)
    interval = normalise_interval(interval_s)
    count = count_for_interval(interval)
    trans = {
        "sn": sn,
        "subDeviceType": "ems",
        "subSn": sn,
        "messageId": secrets.token_hex(16),
        "cmdId": 4,
        "config": {"count": count, "interval": interval},
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
    snap = {"local_time": trans.get("localTime"), "soc": data.get("soc"),
            "_ems_keys": tuple(sorted(data))}
    for k in POWER_KEYS + FLOW_KEYS + STATUS_KEYS:
        snap[k] = data.get(k)
    info = limit_from_ems(data)
    if info.get("kw") is not None:
        snap["charge_limit_w"] = int(round(info["kw"] * 1000))
        snap["charge_limit_source"] = info["source"]
    return snap


class LiveFeed:
    def __init__(self, client, pn: str, sn: str, battery_modules: int = 0,
                 reserve_soc: float = BATTERY_RESERVE_SOC,
                 interval_s: int = DEFAULT_INTERVAL_S,
                 site: str | None = None):
        self.client = client
        # Own HTTP session: the feed thread must not share requests.Session
        # with the web request threads.
        self.api = AnkerClient.resume(client.token, client.user_id,
                                      client.country, client.base, client.nick)
        self.pn, self.sn = pn, sn
        self.battery_modules = max(0, int(battery_modules))
        self.nominal_capacity_kwh = (
            round(self.battery_modules * BATTERY_MODULE_KWH, 2)
            if self.battery_modules else None)
        self.reserve_soc = max(0.0, min(100.0, float(reserve_soc)))
        self.interval_s = normalise_interval(interval_s)
        self.count = count_for_interval(self.interval_s)
        self.dir = tempfile.mkdtemp(prefix="anker-live-")
        os.chmod(self.dir, 0o700)
        self.snapshot: dict = {"available": False, "reason": "starting"}
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self._mqtt = None
        self._connected_at: float | None = None
        self._last_trigger_at = 0.0
        self._last_frame_at = 0.0
        self._last_gap_s: float | None = None
        self._window_frames = 0
        self._frames_total = 0
        self._pending_interval_s: int | None = None
        self._waking_since: float | None = None
        self._last_trigger_error: str | None = None
        self.site = site
        self._samples: deque = deque()
        self._charge_limit: dict | None = None
        self._device_limit: dict | None = None
        self._last_frame_keys: tuple = ()
        self._limits_thread: threading.Thread | None = None
        self._last_limits_error: str | None = None
        # A fixed suffix lets an old broker session collide with a restarted
        # feed. Keep an instance-unique suffix so reconnects are clean.
        self._client_suffix = f"-web-{secrets.token_hex(3)}"

    # -- lifecycle -----------------------------------------------------

    def start(self) -> None:
        if self.thread and self.thread.is_alive():
            return
        self.thread = threading.Thread(target=self._run, name="anker-live", daemon=True)
        self.thread.start()
        if self.site and (self._limits_thread is None
                          or not self._limits_thread.is_alive()):
            self._limits_thread = threading.Thread(
                target=self._load_limits, name="anker-limits", daemon=True)
            self._limits_thread.start()

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

    # -- read path -----------------------------------------------------

    def _averages(self, now: float) -> dict:
        """Time-weighted signed battery, house-load and PV averages.

        Frames do not arrive on a fixed grid: a concurrent 5 s wake can be
        mixed with our 60 s window. Weighting by elapsed time keeps the
        estimate stable across both.  Load and PV samples can be missing;
        only intervals with both endpoints present contribute to a given
        average.
        """
        cutoff = now - BATTERY_AVG_WINDOW_S
        while self._samples and self._samples[0][0] < cutoff:
            self._samples.popleft()
        samples = list(self._samples)
        if not samples:
            return {"batt_avg_net_kw": None, "batt_avg_kw": None,
                    "load_avg_kw": None, "solar_avg_kw": None}
        if len(samples) == 1:
            net = samples[0][1]
            load = samples[0][2]
            solar = samples[0][3]
            return {
                "batt_avg_net_kw": round(net, 3),
                "batt_avg_kw": round(abs(net), 2),
                "load_avg_kw": round(load, 3) if load is not None else None,
                "solar_avg_kw": round(solar, 3) if solar is not None else None,
            }

        def weighted(index: int):
            total = 0.0
            total_t = 0.0
            prev_t = samples[0][0]
            prev_v = samples[0][index]
            for sample in samples[1:]:
                t = sample[0]
                v = sample[index]
                if prev_v is not None and v is not None:
                    dt = t - prev_t
                    if dt > 0:
                        total += (prev_v + v) / 2.0 * dt
                        total_t += dt
                prev_t, prev_v = t, v
            return total / total_t if total_t > 0 else None

        net = weighted(1)
        if net is None:
            net = samples[-1][1]
        load = weighted(2)
        solar = weighted(3)
        return {
            "batt_avg_net_kw": round(net, 3),
            "batt_avg_kw": round(abs(net), 2),
            "load_avg_kw": round(load, 3) if load is not None else None,
            "solar_avg_kw": round(solar, 3) if solar is not None else None,
        }

    def _cap_fields(self, now: float) -> dict:
        """Resolve the runtime charge cap; caller holds the lock."""
        realtime = self._charge_limit
        if realtime is not None:
            try:
                age = max(0, int(now - float(realtime.get("ts", 0))))
            except (TypeError, ValueError):
                age = None
            if age is None or age <= CHARGE_LIMIT_GRACE_S:
                return {"charge_limit_kw": realtime.get("kw"),
                        "charge_limit_source": realtime.get("source")
                        or "unavailable",
                        "charge_limit_age_s": age}
        device = self._device_limit
        if isinstance(device, dict) and device.get("kw") is not None:
            try:
                age = max(0, int(now - float(device.get("ts", 0))))
            except (TypeError, ValueError):
                age = None
            return {"charge_limit_kw": device.get("kw"),
                    "charge_limit_source": device.get("source")
                    or "device_attrs",
                    "charge_limit_age_s": age}
        fallback = fallback_limit()
        return {"charge_limit_kw": fallback["kw"],
                "charge_limit_source": fallback["source"],
                "charge_limit_age_s": None}

    def set_charge_limit(self, kw, source: str = "plan-save") -> None:
        try:
            value = float(kw)
        except (TypeError, ValueError):
            return
        if 0 < value <= 20:
            with self.lock:
                now = time.time()
                entry = {"kw": round(value, 3), "source": source, "ts": now}
                self._charge_limit = entry
                self._device_limit = dict(entry)

    def get(self) -> dict:
        now = time.time()
        trigger = False
        with self.lock:
            snap = dict(self.snapshot)
            available = bool(snap.get("available"))
            age = None
            if available:
                try:
                    age = max(0, int(now) - int(snap.get("ts", 0)))
                except (TypeError, ValueError):
                    age = None

            stale_after = stale_after_s(self.interval_s)
            stale = available and (age is None or age > stale_after)

            if (self._waking_since is not None
                    and now - self._waking_since >= WAKE_TIMEOUT_S):
                self._waking_since = None
            waking_since = self._waking_since
            waking = waking_since is not None
            waking_notice = (waking_since is not None
                             and now - waking_since >= WAKING_NOTICE_GRACE_S)

            # The requested window has delivered its last frame and one full
            # interval has passed since it. Renew now, even before the data is
            # stale, so the next frame arrives close to the chosen cadence.
            window_done = (available and age is not None
                           and self._window_frames >= self.count
                           and age >= self.interval_s)
            needs = (not available) or stale or window_done

            connected = self._mqtt is not None and self._mqtt.is_connected()
            can_wake = (connected and self._connected_at is not None
                        and now - self._connected_at >= TRIGGER_GRACE_S
                        and now - self._last_trigger_at >= trigger_cooldown_s(self.interval_s)
                        and not waking)

            if needs and can_wake:
                # A slider change waits for the active window to end, then
                # applies once with the new count/interval.
                if self._pending_interval_s is not None:
                    self.interval_s = self._pending_interval_s
                    self.count = count_for_interval(self.interval_s)
                    self._pending_interval_s = None
                    self._window_frames = 0
                    stale_after = stale_after_s(self.interval_s)
                    stale = available and (age is None or age > stale_after)

                self._last_trigger_at = now
                self._waking_since = now
                # A wake starts a new frame budget. Without this reset the
                # 5 s/60-frame window stays "complete" forever and renews on
                # every poll once the cooldown has passed.
                self._window_frames = 0
                waking = True
                waking_notice = False
                if not available:
                    snap = {"available": False, "reason": "waking"}
                self.snapshot = snap
                trigger = True

            if available:
                # Stale wins over waking: an overdue frame should say so even
                # while the retry is in flight. The notice is debounced so a
                # normal renewal does not flash "Refreshing" for one poll.
                snap["stale"] = bool(stale)
                snap["waking"] = bool(waking)
                snap["waking_notice"] = bool(waking_notice)
                if age is not None:
                    snap["age_s"] = age
                snap.update(self._averages(now))
            snap.update(self._cap_fields(now))
            snap["interval_s"] = self.interval_s
            snap["count"] = self.count
            snap["stale_after_s"] = stale_after

        if trigger:
            self._publish_trigger()
        return snap

    def set_interval(self, value) -> dict:
        """Select a new cadence.

        If a window is active, keep the old cadence until it ends; this avoids
        a wake fight with another client and makes a failed mid-window publish
        harmless. The result reports the active and pending values.
        """
        interval = normalise_interval(value)
        now = time.time()
        apply_now = False
        with self.lock:
            if interval == self.interval_s:
                self._pending_interval_s = None
                return {"interval_s": self.interval_s,
                        "pending_interval_s": None,
                        "applied": True, "count": self.count}

            snap = self.snapshot
            available = bool(snap.get("available"))
            age = None
            if available:
                try:
                    age = max(0, int(now) - int(snap.get("ts", 0)))
                except (TypeError, ValueError):
                    age = None
            window_done = (not available or (
                age is not None and self._window_frames >= self.count
                and age >= self.interval_s))
            if window_done:
                self.interval_s = interval
                self.count = count_for_interval(interval)
                self._pending_interval_s = None
                self._window_frames = 0
                self._last_trigger_at = now
                self._waking_since = now
                if available:
                    new_stale_after = stale_after_s(self.interval_s)
                    snap["waking"] = True
                    snap["waking_notice"] = False
                    snap["stale"] = bool(age is None
                                         or age > new_stale_after)
                    snap["interval_s"] = self.interval_s
                    snap["count"] = self.count
                    snap["stale_after_s"] = new_stale_after
                    self.snapshot = snap
                else:
                    self.snapshot = {
                        "available": False, "reason": "waking",
                        "interval_s": self.interval_s, "count": self.count,
                        "stale_after_s": stale_after_s(self.interval_s),
                    }
                apply_now = True
            else:
                self._pending_interval_s = interval

            result = {"interval_s": self.interval_s,
                      "pending_interval_s": self._pending_interval_s,
                      "applied": apply_now, "count": self.count}
        if apply_now:
            self._publish_trigger()
        return result

    def stats(self) -> dict:
        with self.lock:
            cap = self._cap_fields(time.time())
            return {
                "interval_s": self.interval_s,
                "count": self.count,
                "frames": self._frames_total,
                "window_frames": self._window_frames,
                "last_gap_s": self._last_gap_s,
                "waking": self._waking_since is not None,
                "pending_interval_s": self._pending_interval_s,
                "last_trigger_error": self._last_trigger_error,
                "battery_samples": len(self._samples),
                "last_frame_keys": list(self._last_frame_keys)[:64],
                "charge_limit_source": cap.get("charge_limit_source"),
                "charge_limit_kw": cap.get("charge_limit_kw"),
                "limits_error": self._last_limits_error,
            }

    def _publish_trigger(self) -> None:
        mqtt_client = self._mqtt
        if mqtt_client is None or not mqtt_client.is_connected():
            return
        with self.lock:
            interval = self.interval_s
        topic, message = build_trigger(
            self.pn, self.sn, self.api.user_id or self.client.user_id,
            interval_s=interval)
        try:
            info = mqtt_client.publish(topic, message, qos=1)
            if info.rc != mqtt.MQTT_ERR_SUCCESS:
                with self.lock:
                    self._last_trigger_error = f"publish rc={info.rc}"
        except Exception as exc:
            with self.lock:
                self._last_trigger_error = type(exc).__name__

    # -- worker --------------------------------------------------------

    def _load_limits(self) -> None:
        """Fetch the device-attribute cap in the background, then refresh slowly."""
        while not self.stop_event.is_set():
            if not self.site:
                return
            try:
                api = AnkerClient.resume(
                    self.client.token, self.client.user_id, self.client.country,
                    self.client.base, self.client.nick)
                info = api.read_charge_limit(self.site, self.sn)
                with self.lock:
                    self._last_limits_error = (
                        info.get("error") or getattr(api, "last_limits_error", None))
                    if info.get("kw") is not None:
                        self._device_limit = {
                            "kw": round(float(info["kw"]), 3),
                            "source": info.get("source") or "device_attrs",
                            "ts": time.time(),
                        }
            except Exception as exc:
                with self.lock:
                    self._last_limits_error = type(exc).__name__
            if self.stop_event.wait(CHARGE_LIMIT_REFRESH_S):
                break

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

    def _client_id(self, info: dict) -> str:
        """Build the app's authorized MQTT identity.

        The broker ACL is keyed on the app's generated client-id:
        ``android-<app_name>-<user_id>-<certificate_id>``. The old
        ``thing_name_<random>`` form connects but gets SUBACK 128 (denied)
        for every subscription. A suffix is allowed, so a unique ``-web-*``
        suffix lets the feed coexist with the phone app without colliding
        with a previous feed session.
        """
        account = info.get("user_id") or str(info.get("thing_name", "")).split(
            "-anker_power")[0]
        cert_id = info.get("certificate_id")
        if not cert_id:
            raise ValueError("get_user_mqtt_info returned no certificate_id")
        app_name = info.get("app_name") or "anker_power"
        return f"android-{app_name}-{account}-{cert_id}{self._client_suffix}"

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
        # A reconnect starts a fresh subscription; do not let the old
        # window's frame count trigger an immediate renewal.
        self._window_frames = 0

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
        now = time.time()
        keys = snap.pop("_ems_keys", ())
        snap.update(
            available=True,
            ts=int(now),
            nominal_capacity_kwh=self.nominal_capacity_kwh,
            reserve_soc=self.reserve_soc,
        )
        net_kw = battery_net_w(snap) / 1000.0
        load_kw = home_w(snap) / 1000.0
        solar_kw = (solar_w(snap) / 1000.0
                    if has_solar_fields(snap) else None)
        with self.lock:
            if self._last_frame_at:
                gap = now - self._last_frame_at
                if gap >= 1.0:
                    self._last_gap_s = round(gap, 1)
            self._last_frame_at = now
            self._window_frames += 1
            self._frames_total += 1
            self._waking_since = None
            self._last_trigger_error = None
            self._last_frame_keys = tuple(keys)
            self._samples.append((now, net_kw, load_kw, solar_kw))
            self._averages(now)
            limit_w = snap.pop("charge_limit_w", None)
            limit_source = snap.pop("charge_limit_source", None)
            if limit_w is not None:
                self._charge_limit = {
                    "kw": round(float(limit_w) / 1000.0, 3),
                    "source": limit_source or "realtime.ems_second",
                    "ts": now,
                }
            self.snapshot = snap
