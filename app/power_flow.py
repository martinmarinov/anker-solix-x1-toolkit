"""Pure interpretation of one Anker EMS telemetry snapshot.

The inverter reports non-negative edge flows (`p2lp`, `g2lp`, ...) plus
aggregate powers.  This module is the single place that turns those raw
fields into signed watts, so the Home view, the live feed averages and the
battery estimator cannot disagree about direction.

Field meanings (see REPORT.md section 12):

    p2lp / p2bp / p2gp   PV -> load / battery / grid
    g2lp / g2bp          grid -> load / battery
    b2lp / b2gp          battery -> load / grid

Edge flows always win when present.  Fallbacks use the aggregate sign
convention observed on the device: a positive `bp` is discharging, a
positive `gp` is importing.
"""
from __future__ import annotations

ACTIVE_W = 5.0
POWER_KEYS = ("pp", "gp", "bp", "lp")
FLOW_KEYS = ("p2lp", "p2bp", "p2gp", "g2lp", "g2bp", "b2lp", "b2gp")


def number(value):
    """Return a finite float, or None for missing/invalid values."""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return out


def positive(value) -> float:
    value = number(value)
    return max(0.0, value) if value is not None else 0.0


def watts(value) -> int:
    return int(round(max(0.0, value)))


def has_power_fields(snap: dict | None) -> bool:
    if not isinstance(snap, dict):
        return False
    return any(snap.get(k) is not None for k in POWER_KEYS + FLOW_KEYS)


def solar_w(snap: dict) -> float:
    pv = positive(snap.get("pp"))
    pv_edge = (positive(snap.get("p2lp")) + positive(snap.get("p2bp"))
               + positive(snap.get("p2gp")))
    return max(pv, pv_edge)


def grid_import_w(snap: dict) -> float:
    grid_in = positive(snap.get("g2lp")) + positive(snap.get("g2bp"))
    gp = number(snap.get("gp"))
    if not any(snap.get(k) is not None for k in ("g2lp", "g2bp", "p2gp", "b2gp")):
        if gp is not None and gp > 0:
            grid_in = gp
    return grid_in


def grid_export_w(snap: dict) -> float:
    grid_out = positive(snap.get("p2gp")) + positive(snap.get("b2gp"))
    gp = number(snap.get("gp"))
    if not any(snap.get(k) is not None for k in ("g2lp", "g2bp", "p2gp", "b2gp")):
        if gp is not None and gp < 0:
            grid_out = -gp
    return grid_out


def grid_net_w(snap: dict) -> float:
    return grid_import_w(snap) - grid_export_w(snap)


def battery_charge_w(snap: dict) -> float:
    charge = positive(snap.get("p2bp")) + positive(snap.get("g2bp"))
    bp = number(snap.get("bp"))
    if not any(snap.get(k) is not None for k in ("p2bp", "g2bp", "b2lp", "b2gp")):
        if bp is not None and bp < 0:
            charge = -bp
    return charge


def battery_discharge_w(snap: dict) -> float:
    discharge = positive(snap.get("b2lp")) + positive(snap.get("b2gp"))
    bp = number(snap.get("bp"))
    if not any(snap.get(k) is not None for k in ("p2bp", "g2bp", "b2lp", "b2gp")):
        if bp is not None and bp > 0:
            discharge = bp
    return discharge


def battery_net_w(snap: dict) -> float:
    """Signed battery power in W; positive means charging."""
    return battery_charge_w(snap) - battery_discharge_w(snap)


def home_w(snap: dict) -> float:
    home_raw = positive(snap.get("lp"))
    home_edge = (positive(snap.get("p2lp")) + positive(snap.get("g2lp"))
                 + positive(snap.get("b2lp")))
    return home_raw if home_raw > ACTIVE_W else home_edge


def grid_display_w(snap: dict) -> float:
    """Aggregate meter value for the Grid card, preserving the old fallback."""
    gp = number(snap.get("gp"))
    if gp is not None and abs(gp) > ACTIVE_W:
        return abs(gp)
    return abs(grid_net_w(snap))


def battery_display_w(snap: dict) -> float:
    """Aggregate meter value for the battery card."""
    bp = number(snap.get("bp"))
    if bp is not None and abs(bp) > ACTIVE_W:
        return abs(bp)
    return abs(battery_net_w(snap))
