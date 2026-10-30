"""Pure charge-limit extraction from telemetry or device attributes.

The solar ETA must not bake a charge cap into the formula.  A real value can
arrive from two dynamic places:

1. a realtime EMS field (``mcp`` / ``max_charge_power`` / ... on firmware
   that exposes one);
2. the device-attribute response the app itself writes
   (``ac_power_limit``) or the site power-limit response.

The X1 documented EMS map has no max-charge field, so the caller must not
assume one exists.  When neither source has a valid value the fallback is
6.0 kW, the X1's documented full recharge rating.  That fallback is a named
constant, never an inline number in the solar formula.
"""
from __future__ import annotations

FALLBACK_MAX_CHARGE_KW = 6.0

# Exact field names seen in the app/upstream maps.  `gmp` (max load) and
# `mpp` (micro-inverter power) are deliberately absent.
_CHARGE_KEYS = (
    "mcp",
    "max_charge_power",
    "maxChargingPower",
    "charge_power_limit",
    "chargePowerLimit",
    "ac_power_limit",
    "acPowerLimit",
    "gridChargingPower",
    "powerLimit",
)
_ATTR_KEYS = _CHARGE_KEYS + ("ac_input_power", "legal_power_limit")
_KEY_SET = {k.lower() for k in _ATTR_KEYS}
_UNIT_KEYS = ("unit", "ac_input_power_unit", "power_unit")


def _number(value):
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _normalise_kw(value, unit: str | None = None) -> float | None:
    """Convert W/kW to kW; unknown units use the app's >100 heuristic."""
    number = _number(value)
    if number is None or number <= 0:
        return None
    unit = (unit or "").strip().upper()
    if unit in ("W", "WATT", "WATTS"):
        kw = number / 1000.0
    elif unit in ("KW",):
        kw = number
    else:
        # ac_power_limit / mcp are written in W by the app; small values are
        # treated as kW so a 4.2 response does not become 4.2 W.
        kw = number / 1000.0 if number > 100 else number
    if not 0 < kw <= 20:
        return None
    return round(kw, 3)


def _unit_for(node: dict, key: str) -> str | None:
    for name in (f"{key}_unit", *_UNIT_KEYS):
        if isinstance(node.get(name), str):
            return node[name]
    return None


def _candidates(node, out: list, path: str = "") -> None:
    """Walk a response and collect charge-limit candidates."""
    if isinstance(node, dict):
        for key, value in node.items():
            lower = str(key).lower()
            if lower in _KEY_SET:
                if lower == "legal_power_limit" and isinstance(value, dict):
                    inner = value.get("limit", value.get("value"))
                    unit = value.get("unit") or _unit_for(node, key)
                    kw = _normalise_kw(inner, unit)
                    if kw is not None:
                        out.append({"kw": kw, "key": key, "raw": value})
                elif not isinstance(value, (dict, list)):
                    kw = _normalise_kw(value, _unit_for(node, key))
                    if kw is not None:
                        out.append({"kw": kw, "key": key, "raw": value})
            _candidates(value, out, f"{path}.{key}" if path else str(key))
    elif isinstance(node, list):
        for value in node:
            _candidates(value, out, path)


def _result(candidates: list, prefix: str) -> dict:
    if not candidates:
        return {"kw": None, "source": "unavailable", "key": None, "raw": None}
    best = min(candidates, key=lambda item: item["kw"])
    source = f"{prefix}.{best['key']}" if prefix else str(best["key"])
    return {"kw": best["kw"], "source": source, "key": best["key"],
            "raw": best["raw"], "candidates": candidates}


def limit_from_ems(data: dict | None) -> dict:
    """Extract a cap from one `trans.data` EMS object."""
    out: list = []
    _candidates(data, out)
    return _result(out, "realtime.ems_second")


def limit_from_attrs(payload: dict | None, prefix: str = "device_attrs") -> dict:
    """Extract a cap from get_device_attrs / get_power_limit data."""
    out: list = []
    _candidates(payload, out)
    return _result(out, prefix)


def fallback_limit() -> dict:
    return {"kw": FALLBACK_MAX_CHARGE_KW, "source": "default_6kw",
            "key": None, "raw": None}


def resolve_charge_limit(realtime: dict | None = None,
                         attrs: dict | None = None) -> dict:
    """Resolve dynamic cap -> device attribute cap -> 6 kW fallback."""
    for candidate in (realtime, attrs):
        if isinstance(candidate, dict):
            kw = _number(candidate.get("kw"))
            if kw is not None and 0 < kw <= 20:
                return candidate
    return fallback_limit()
