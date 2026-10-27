"""Anker SOLIX client for the local Time-of-Use editor.

Design rules baked in here:
  * credentials and tokens live in memory only -- never written to disk, never logged;
  * every API error is turned into a typed exception the UI can explain;
  * the plan-change validator refuses anything that would alter the control law
    (mode, price_type, dynamic_price) while allowing normal plan edits.
"""

from __future__ import annotations

import base64
import hashlib
import copy
import json
import time
import uuid
from datetime import datetime, timezone

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

# Server's static ECDH P-256 public key (uncompressed point, hex). Public
# material used to encrypt the login password; same key the Android app uses.
API_PUBLIC_KEY_HEX = (
    "04c5c00c4f8d1197cc7c3167c52bf7acb054d722f0ef08dcd7e0883236e0d72a3"
    "868d9750cb47fa4619248f3d83f0f662671dadc6e2d31c2f41db0161651c7c076"
)

BASES = {
    "com": "https://ankerpower-api.anker.com",
    "eu": "https://ankerpower-api-eu.anker.com",
}
EU_COUNTRIES = {
    "AT", "BE", "BG", "CH", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR",
    "GB", "GR", "HR", "HU", "IE", "IS", "IT", "LI", "LT", "LU", "LV", "MT",
    "NL", "NO", "PL", "PT", "RO", "SE", "SI", "SK",
}

P_LOGIN = "passport/login"
P_SITES = "power_service/v1/site/get_site_list"
P_DEVICES = "power_service/v1/site/list_user_devices"
P_GET_PLAN_HES = "charging_hes_svc/get_utility_rate_plans"   # NOTE: plural
P_HES_PLANS = "charging_hes_svc/get_utility_rate_plans"
P_DEVICE_PARAM = "power_service/v1/site/get_site_device_param"
P_SCHEDULE = "power_service/v1/site/get_schedule"
P_DEVICE_COMMAND = "charging_hes_svc/device_command"
P_GET_DEVICE_COMMAND = "charging_hes_svc/get_device_command"
P_DEVICE_ATTRS = "power_service/v1/app/device/get_device_attrs"
P_GET_PLAN_ENERGY = "charging_energy_service/get_utility_rate_plan"
P_SAVE_PLAN_HES = "charging_hes_svc/update_hes_utility_rate_plan"
P_POWER_LIMIT = "power_service/v1/site/get_power_limit"
P_BOUND_DEVICES = "power_service/v1/app/get_relate_and_bind_devices"
P_SITE_DETAIL = "power_service/v1/site/get_site_detail"

# Plan content the editor may change. Anything else -- above all mode /
# price_type / dynamic_price -- is controller state and must stay identical.
ALLOWED_PLAN_KEYS = {
    "start_time", "end_time",           # season boundaries
    "start_time_period", "end_time_period",
    "peak_type", "day_type",
    "buy", "sell", "price", "energy_unit", "name", "id",
}
# Lists that may legitimately change length when the user adds/removes a period.
ALLOWED_LIST_GROWTH = {"peak_valley_prices"}


class AnkerError(Exception):
    """Business/API error with a code the UI can show."""

    def __init__(self, message: str, code=None, kind: str = "api"):
        super().__init__(message)
        self.message = message
        self.code = code
        self.kind = kind  # api | auth | network | rate_limit


def _utc_ms() -> int:
    return int(time.time() * 1000)


def base_for(country: str) -> str:
    return BASES["eu" if country.upper() in EU_COUNTRIES else "com"]


SITE_ID_KEYS = ("site_id", "siteId", "site_no", "siteNo", "site_code", "siteCode",
                "id", "site_id_str", "siteIdStr")


def site_id_of(site) -> str | None:
    """Extract the site id from any of the shapes Anker uses."""
    if not isinstance(site, dict):
        return None
    for k in SITE_ID_KEYS:
        v = site.get(k)
        if v not in (None, ""):
            return str(v)
    for nest in ("site_info", "site_detail", "siteInfo", "siteDetail"):
        v = site.get(nest)
        if isinstance(v, dict):
            got = site_id_of(v)
            if got:
                return got
    return None


MAIN_DEVICE_PREFIXES = ("A5101", "A5102", "A5103", "A17B1", "A17C1")


def device_role(code) -> str:
    """Classify a device: the inverter owns the TOU plan; A5220 are batteries."""
    c = str(code or "").upper()
    if any(c.startswith(p) for p in MAIN_DEVICE_PREFIXES):
        return "inverter"
    if c.startswith("A5220"):
        return "battery"
    if not c:
        return "unknown"
    return "other"


def norm_device(d) -> dict | None:
    """Normalise the several device shapes Anker uses."""
    if not isinstance(d, dict):
        return None
    sn = (d.get("device_sn") or d.get("sn") or d.get("deviceSn")
          or d.get("serial_number") or d.get("serialNumber"))
    if not sn:
        return None
    code = (d.get("product_code") or d.get("device_pn") or d.get("productCode")
            or d.get("devicePn") or d.get("model") or d.get("device_type"))
    return {"sn": str(sn), "code": str(code or ""),
            "role": device_role(code),
            "site_id": d.get("site_id") or d.get("siteId")}


def iter_lists(node, depth: int = 0):
    """Yield lists anywhere in a response, shallow-first."""
    if depth > 4:
        return
    if isinstance(node, dict):
        for v in node.values():
            yield from iter_lists(v, depth + 1)
    elif isinstance(node, list):
        yield node
        for v in node[:5]:
            yield from iter_lists(v, depth + 1)


# `list_user_devices` returns categorised buckets, not a flat `device_list`.
# The X1 / HES lives in `home_backup_system_list`.
DEVICE_BUCKETS = (
    "home_backup_system_list", "hardware_list", "grid_list",
    "combiner_box_list", "charging_pile_list",
    "device_list", "devices", "deviceList", "list", "records",
    "relate_device_list", "bind_device_list", "device_info_list",
)


def collect_devices(data) -> list:
    """Flatten every recognised device bucket, in priority order."""
    if isinstance(data, list):
        return data
    if not isinstance(data, dict):
        return []
    out = []
    for key in DEVICE_BUCKETS:
        bucket = data.get(key)
        if isinstance(bucket, list):
            out.extend(bucket)
    return out


def deep_devices(data) -> list:
    """Last-resort scan: first list of dicts that look like devices."""
    for lst in iter_lists(data):
        cand = [d for d in lst if isinstance(d, dict) and norm_device(d)]
        if cand:
            return cand
    return []


def as_list(data, *keys) -> list:
    """Tolerate the several envelope shapes Anker uses for collections."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for k in keys:
            v = data.get(k)
            if isinstance(v, list):
                return v
    return []


def _has_plan_fields(d) -> bool:
    return isinstance(d, dict) and any(
        k in d for k in ("peak_sessions", "use_time", "peak_valley_prices"))


def unwrap_plan(data):
    """Find the plan object inside whatever wrapper the service returns."""
    if isinstance(data, str):
        try:
            return unwrap_plan(json.loads(data))
        except ValueError:
            return {}
    if isinstance(data, list):
        if data and _has_plan_fields(data[0]):
            return data[0]
        if data and isinstance(data[0], dict) and "peak_valley_prices" in data[0]:
            return {"peak_sessions": data}
        return {"peak_sessions": data} if data else {}
    if not isinstance(data, dict):
        return {}
    if _has_plan_fields(data):
        return data
    for k in ("plan", "rate_plan", "utility_rate_plan", "tou_plan",
              "plan_list", "rate_plan_list", "data", "param_data", "paramData"):
        v = data.get(k)
        if isinstance(v, (dict, list, str)):
            got = unwrap_plan(v)
            if _has_plan_fields(got):
                return got
    return data


def diff_paths(a, b, path: str = ""):
    """Yield (path, kind) where two JSON structures differ."""
    if type(a) is not type(b):
        yield path or "<root>", "type"
        return
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                yield f"{path}.{k}".lstrip("."), "added_or_removed"
            else:
                yield from diff_paths(a[k], b[k], f"{path}.{k}".lstrip("."))
    elif isinstance(a, list):
        if len(a) != len(b):
            yield f"{path}[len]", "len"
            return
        for i, (x, y) in enumerate(zip(a, b)):
            yield from diff_paths(x, y, f"{path}[{i}]")
    elif a != b:
        yield path or "<root>", "value"


def plan_diff(before: dict, after: dict) -> list[dict]:
    """Human-readable list of changes, for the confirm screen."""
    out = []
    for path, kind in diff_paths(before, after):
        out.append({"path": path, "kind": kind})
    return out


def assert_plan_only(before: dict, after: dict) -> None:
    """Refuse changes to controller state; allow normal plan edits.

    Raises AnkerError with kind='validation' and the offending paths.
    """
    offenders = []
    for path, kind in diff_paths(before, after):
        leaf = path.rsplit(".", 1)[-1]
        leaf = leaf.split("[")[0]
        if kind == "len":
            parent = path.rsplit(".", 1)[-1].split("[")[0]
            if parent not in ALLOWED_LIST_GROWTH:
                offenders.append(path)
            continue
        if kind == "added_or_removed":
            # A field appearing/disappearing at the top of a session or row is
            # a schema change, not an edit; only allow it for known plan keys.
            if leaf not in ALLOWED_PLAN_KEYS:
                offenders.append(path)
            continue
        if leaf not in ALLOWED_PLAN_KEYS:
            offenders.append(path)

    if offenders:
        raise AnkerError(
            "Refusing to write: this would change controller state, not just the plan "
            f"({', '.join(offenders[:6])}). Mode, price_type and dynamic_price must stay "
            "identical so the target-driven Time-of-Use controller is preserved.",
            kind="validation",
        )


class AnkerClient:
    """One instance per authenticated session. Holds credentials only until login."""

    def __init__(self, email: str = "", password: str = "", country: str = "GB",
                 base: str | None = None, timeout: int = 25,
                 token: str | None = None, user_id: str = "", nick: str = ""):
        self.email = email
        self._password = password
        self.country = country.upper()
        self.base = base or base_for(self.country)
        self.timeout = timeout
        self.http = requests.Session()
        self.token: str | None = None
        self.user_id: str = ""
        self.gtoken: str = ""
        self.nick: str = ""
        self.expires_at: float | None = None
        self.last_probe: list[dict] = []
        self.last_source: str = "none"
        self.last_limits_error: str | None = None
        if token:
            # Rehydrated session: no password, no ECDH -- just the bearer token.
            self.token = token
            self.user_id = str(user_id or "")
            self.gtoken = hashlib.md5(self.user_id.encode()).hexdigest()
            self.nick = nick or ""
            self._priv = self._pub = self._shared = None
        else:
            self._make_keys()

    @classmethod
    def resume(cls, token: str, user_id: str, country: str, base: str | None = None,
               nick: str = "") -> "AnkerClient":
        return cls(country=country, base=base, token=token, user_id=user_id, nick=nick)

    # -- crypto ------------------------------------------------------------

    def _make_keys(self) -> None:
        self._priv = ec.generate_private_key(ec.SECP256R1(), default_backend())
        self._pub = self._priv.public_key()
        self._shared = self._priv.exchange(
            ec.ECDH(),
            ec.EllipticCurvePublicKey.from_encoded_point(
                ec.SECP256R1(), bytes.fromhex(API_PUBLIC_KEY_HEX)),
        )

    def _pub_hex(self) -> str:
        return self._pub.public_bytes(
            serialization.Encoding.X962,
            serialization.PublicFormat.UncompressedPoint).hex()

    def _encrypt(self, raw: str) -> str:
        cipher = Cipher(algorithms.AES(self._shared), modes.CBC(self._shared[:16]),
                        backend=default_backend())
        padder = padding.PKCS7(128).padder()
        data = padder.update(raw.encode()) + padder.finalize()
        enc = cipher.encryptor()
        return base64.b64encode(enc.update(data) + enc.finalize()).decode()

    # -- transport ---------------------------------------------------------

    def _headers(self) -> dict:
        h = {
            "content-type": "application/json",
            "model-type": "PHONE",
            "app-name": "anker_power",
            "app-version": "3.24.3",
            "os-type": "android",
            "country": self.country,
            "timezone": "GMT+00:00",
            "language": "en",
            "x-request-id": str(uuid.uuid4()),
        }
        if self.token:
            h["x-auth-token"] = self.token
            h["gtoken"] = self.gtoken
        return h

    def _post(self, path: str, body: dict | None = None,
              timeout: float | None = None, attempts: int = 3) -> dict:
        url = f"{self.base}/{path}"
        last = None
        for attempt in range(max(1, attempts)):
            try:
                r = self.http.post(url, json=body or {}, headers=self._headers(),
                                   timeout=timeout or self.timeout)
            except requests.Timeout:
                last = AnkerError("Anker cloud timed out. Check your connection and retry.",
                                  kind="network")
                time.sleep(1.5 * (attempt + 1))
                continue
            except requests.RequestException as e:
                raise AnkerError(f"Network error talking to Anker: {e.__class__.__name__}",
                                 kind="network") from e

            if r.status_code >= 500:
                hint = ("Anker is throttling or temporarily unavailable (HTTP 503). "
                        "Wait about a minute, then retry.") if r.status_code == 503 else \
                       f"Anker cloud error (HTTP {r.status_code}). Try again shortly."
                last = AnkerError(hint, kind="network")
                time.sleep(1.5 * (attempt + 1))
                continue
            if r.status_code == 429:
                raise AnkerError("Rate limited by Anker. Wait a minute before retrying.",
                                 kind="rate_limit")
            if r.status_code in (401, 403):
                raise AnkerError("Session rejected by Anker. Please sign in again.",
                                 code=r.status_code, kind="auth")
            if r.status_code != 200:
                raise AnkerError(f"Unexpected HTTP {r.status_code} from Anker.",
                                 code=r.status_code)

            try:
                js = r.json()
            except ValueError:
                raise AnkerError("Anker returned a non-JSON response.", kind="network")

            code = js.get("code")
            if code in (26084, 26085, 401):
                raise AnkerError(js.get("msg") or "Signed in elsewhere or session expired.",
                                 code=code, kind="auth")
            if code not in (0, None):
                raise AnkerError(js.get("msg") or f"Anker error {code}", code=code)
            return js

        raise last or AnkerError("Anker cloud unreachable.", kind="network")

    # -- api ---------------------------------------------------------------

    def login(self) -> dict:
        js = self._post(P_LOGIN, {
            "ab": self.country,
            "client_secret_info": {"public_key": self._pub_hex()},
            "enc": 0,
            "email": self.email,
            "password": self._encrypt(self._password),
            "time_zone": 0,
            "transaction": str(_utc_ms()),
        })
        data = js.get("data") or {}
        self.token = data.get("auth_token")
        if not self.token:
            raise AnkerError("Login succeeded but no token was returned.", kind="auth")
        self.user_id = str(data.get("user_id") or "")
        self.gtoken = hashlib.md5(self.user_id.encode()).hexdigest()
        self.nick = data.get("nick_name") or self.email
        exp = data.get("token_expires_at")
        try:
            exp = float(exp)
            self.expires_at = exp / 1000 if exp > 1e12 else exp
        except (TypeError, ValueError):
            self.expires_at = None
        # Drop the password reference as soon as it is no longer needed.
        self._password = ""
        return {"nick": self.nick, "user_id": self.user_id,
                "country": self.country, "base": self.base,
                "expires_at": self.expires_at}

    def sites(self) -> list[dict]:
        data = self._post(P_SITES, {}).get("data") or {}
        return as_list(data, "site_list", "sites", "list", "records")

    def devices(self, site_id) -> list[dict]:
        """Resolve the device list across the several endpoints/shapes Anker uses.

        `list_user_devices` is a *user*-level call, so a site filter can return
        an empty list even when devices exist. We try site-scoped shapes first,
        then the unfiltered user call, then the bound-devices endpoint.
        """
        attempts = [
            ("list_user_devices(site_id)", P_DEVICES, {"site_id": site_id}),
            ("list_user_devices(siteId)", P_DEVICES, {"siteId": site_id}),
            ("list_user_devices(paged)", P_DEVICES,
             {"site_id": site_id, "page": 1, "page_size": 200}),
            ("list_user_devices(all)", P_DEVICES, {}),
            ("bound_devices", P_BOUND_DEVICES, {}),
            ("site_detail", P_SITE_DETAIL, {"site_id": site_id}),
        ]
        self.last_probe = []
        found: dict[str, dict] = {}
        for label, path, body in attempts:
            try:
                js = self._post(path, body, timeout=15, attempts=1)
            except AnkerError as e:
                if e.kind in ("auth", "rate_limit"):
                    raise
                self.last_probe.append({"source": label, "error": e.message[:120]})
                continue

            data = js.get("data")
            items = collect_devices(data)
            if not items:
                items = deep_devices(data)
            got = [n for n in (norm_device(d) for d in items) if n]
            self.last_probe.append({
                "source": label,
                "data_type": type(data).__name__,
                "data_keys": sorted(data.keys())[:10] if isinstance(data, dict) else None,
                "items": len(items), "matched": len(got),
                **({"code": js.get("code")} if js.get("code") not in (0, None) else {}),
            })
            for n in got:
                found.setdefault(n["sn"], n)
            if found:
                self.last_source = label
                return list(found.values())

        self.last_source = "none"
        return []

    def power_limits(self, site_id, sn: str | None = None) -> dict | None:
        # Best-effort: never let a slow/unsupported limits call block the editor,
        # but remember why it failed so the UI can say something useful.
        self.last_limits_error = None
        errors = []
        for body in ({"site_id": site_id}, {"station_id": site_id}):
            try:
                js = self._post(P_POWER_LIMIT, body, timeout=8, attempts=1)
                data = js.get("data")
                if isinstance(data, dict):
                    return data
                errors.append(f"empty data (code={js.get('code')})")
            except AnkerError as e:
                if e.kind in ("auth", "rate_limit"):
                    raise
                errors.append(f"{e.message}")
        # Site grid-power config (param_type 32) -- decoded from
        # grid_connect_util.dart / SiteGridSettingInfo. Read-only here.
        # param_type 32 is the site grid-power config (site_input_power_limit),
        # but it is refused for consumer accounts -- kept as a single probe.
        for body in ({"site_id": site_id, "param_type": "32", "cmd": 17},):
            try:
                js = self._post(P_DEVICE_PARAM, body, timeout=8, attempts=1)
            except AnkerError as e:
                if e.kind in ("auth", "rate_limit"):
                    raise
                errors.append(f"param32: {e.message}")
                continue
            data = js.get("data")
            if isinstance(data, dict):
                for k in ("param_data", "paramData"):
                    if isinstance(data.get(k), str):
                        try:
                            data = {**data, **json.loads(data[k])}
                        except ValueError:
                            pass
                return data
            errors.append(f"param32: empty (code={js.get('code')})")

        if sn:
            for body in ({"site_id": site_id, "device_sn": sn}, {"sn": sn}):
                try:
                    js = self._post(P_DEVICE_ATTRS, body, timeout=8, attempts=1)
                    data = js.get("data")
                    if isinstance(data, dict) and any(
                            "power" in str(k).lower() for k in data):
                        return data
                    errors.append("attrs: no power fields")
                except AnkerError as e:
                    if e.kind in ("auth", "rate_limit"):
                        raise
                    errors.append(f"attrs: {e.message}")
        self.last_limits_error = " · ".join(errors)
        return None

    def get_plan(self, site_id, sn: str) -> dict:
        """Read the TOU plan, trying the endpoints the app actually uses.

        The HES service exposes `get_utility_rate_plans` (plural); the singular
        `get_utility_rate_plan` lives on `charging_energy_service`. We probe a
        small ladder, one attempt each, and stop early if Anker starts throttling.
        """
        # Decoded from the app: `getUtilityRatePlans` posts `station_id`
        # (+ accuracy / notIncludeNem), NOT site_id/sn. That mismatch is what
        # produced HTTP 400.
        candidates = [
            ("device_command", P_GET_DEVICE_COMMAND, {"station_id": site_id, "sn": sn}),
            ("hes_plans(sn)", P_HES_PLANS, {"station_id": site_id, "sn": sn}),
            ("hes_plans(min)", P_HES_PLANS, {"station_id": site_id}),
            ("device_param(6)", P_DEVICE_PARAM, {"site_id": site_id, "param_type": "6"}),
            ("device_param(12)", P_DEVICE_PARAM, {"site_id": site_id, "param_type": "12"}),
            ("energy", P_GET_PLAN_ENERGY, {"siteId": site_id, "sn": sn}),
            ("schedule", P_SCHEDULE, {"site_id": site_id, "sn": sn}),
        ]
        tried = []
        last = None
        for label, path, body in candidates:
            try:
                js = self._post(path, body, attempts=1)
            except AnkerError as e:
                if e.kind in ("auth",):
                    raise
                tried.append(f"{label}: {e.message}")
                last = e
                if e.kind == "rate_limit" or "503" in e.message:
                    break          # do not make throttling worse
                continue

            data = js.get("data")
            plan = unwrap_plan(data)
            if _has_plan_fields(plan):
                self.last_plan_endpoint = path
                return {"data": plan, "endpoint": f"{label} · {path}", "tried": tried}
            keys = sorted(plan.keys())[:6] if isinstance(plan, dict) else type(plan).__name__
            tried.append(f"{label}: no plan fields (keys={keys})")
            last = AnkerError(f"{label}: response had no plan fields", kind="api")

        raise AnkerError("Could not read a rate plan. Tried — " + " · ".join(tried),
                         kind=(last.kind if last else "api"))

    def put_plan(self, site_id, sn: str, plan: dict) -> dict:
        """Write the TOU plan exactly as the Android app does.

        Captured from the app's plaintext payload (2026-10-27): accuracy 5,
        null main_sn/ack, {mode:1} strategy, id 0 on every row, and a formatted
        local time string. This path stores prices to **two** decimals; the
        older minimal body silently rounded them to one.
        """
        plan = copy.deepcopy(plan)
        for season in plan.get("peak_sessions") or []:
            for r in season.get("peak_valley_prices") or []:
                r["id"] = 0
        body = {
            "station_id": site_id,
            "main_sn": None,
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "utility_rate_plan": plan,
            "ack_peak_valley": None,
            "accuracy": 5,
            "electricity_strategy": {"mode": 1},
            "price_source_type": 5,
            "price_source_sub_type": 51,
            "version": "1.0.0",
        }
        return self._post(P_DEVICE_COMMAND, body, timeout=30, attempts=2)

    def save_plan(self, endpoint: str, body: dict) -> dict:
        if "dynamic" in endpoint:
            raise AnkerError("Dynamic Tariff writes are disabled in this tool by design.",
                             kind="validation")
        return self._post(endpoint, body)


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def redact(obj):
    """Mask anything token-shaped before it could reach a log or an error page."""
    SECRET = {"auth_token", "gtoken", "password", "token", "x-auth-token",
              "certificate_pem", "private_key", "pkcs12"}
    if isinstance(obj, dict):
        return {k: ("***" if k.lower() in SECRET else redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


def dumps(obj) -> str:
    return json.dumps(redact(obj), ensure_ascii=False)
