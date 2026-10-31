"""User-editable charging price preferences: Public / Home / Office, each
with its own AC and DC rate, plus which one to assume by default.

Persisted in the ``settings`` key-value table (see state.py) so they can be
changed from the dashboard's Rates page without touching .env or restarting
— unlike the ENERGY_PRICE_* env vars, which stay the fallback default for
Public until the user first saves this page.

A new charge auto-prices by matching its location against any Place named
"Home" or "Office" within HOME_OFFICE_MATCH_RADIUS_KM (wider than the tight
geofence radius used for trip labelling, so a car park near the office still
counts) — falling back to the saved default source when nothing matches.
Either way, the result is just a starting cost: the ✎ edit button (and the
🏠/🏢 quick-fill shortcuts) always let a session be corrected afterwards.
"""
from __future__ import annotations

import json
from datetime import datetime

from sqlalchemy.orm import Session

from . import state, tariff
from .analysis import haversine_km
from .models import Charge, Place
from .sync import now_local

# Wider than a Place's own (typically ~150m) geofence radius — this is about
# "did this charge plausibly happen at/near home or the office", not the
# precise trip-labelling match, so a car park a couple of streets over still
# counts as "home" for pricing purposes.
HOME_OFFICE_MATCH_RADIUS_KM = 2.0

# Sensible starting points shown the first time the Rates page is opened —
# never applied silently to an already-saved preference.
DEFAULT_RATES = {
    "home_ac": 0.90,
    "home_dc": 1.13,
    "office_ac": 0.57,
    "office_dc": 0.57,
}

_RATE_KEYS = {
    "public_ac": state.PRICE_PUBLIC_AC_KEY,
    "public_dc": state.PRICE_PUBLIC_DC_KEY,
    "home_ac": state.PRICE_HOME_AC_KEY,
    "home_dc": state.PRICE_HOME_DC_KEY,
    "office_ac": state.PRICE_OFFICE_AC_KEY,
    "office_dc": state.PRICE_OFFICE_DC_KEY,
}

SOURCES = ("public", "home", "office")

# Valid values for a *logged charge's* price_source — SOURCES plus "other"
# for a fully custom rate (a promo, a one-off price) that isn't one of the
# three configured presets. "other" is never a valid default/auto-match
# source (there's no configured rate to auto-price a charge at), so it's
# kept out of SOURCES and only accepted where a charge is being tagged.
EDIT_SOURCES = SOURCES + ("other",)


def get_rates(session: Session, settings) -> dict[str, float]:
    """All six rates: a DB-saved value if the user has ever saved the Rates
    page, else a fallback — Public falls back to the ENERGY_PRICE_AC/DC_KWH
    env vars (0 there means "use flat/ToU", same as today); Home/Office fall
    back to DEFAULT_RATES."""
    out = {}
    for key, setting_key in _RATE_KEYS.items():
        raw = state.get(session, setting_key, "")
        if raw:
            out[key] = float(raw)
        elif key == "public_ac":
            out[key] = settings.energy_price_ac_kwh
        elif key == "public_dc":
            out[key] = settings.energy_price_dc_kwh
        else:
            out[key] = DEFAULT_RATES[key]
    return out


def get_default_source(session: Session) -> str:
    return state.get(session, state.DEFAULT_PRICE_SOURCE_KEY, "home") or "home"


def get_updated_at(session: Session) -> str | None:
    """When the Rates page was last saved (ISO date) — no live TNB/public
    charger rate feed exists to auto-refresh from, so the Rates page shows
    this as a manual-review reminder instead."""
    return state.get(session, state.PRICE_UPDATED_AT_KEY, "") or None


def save(session: Session, rates: dict[str, float], default_source: str) -> None:
    for key, setting_key in _RATE_KEYS.items():
        if key in rates and rates[key] is not None:
            state.put(session, setting_key, str(rates[key]))
    if default_source in SOURCES:
        state.put(session, state.DEFAULT_PRICE_SOURCE_KEY, default_source)
    # The date shown as "rates last reviewed". utcnow() reported yesterday
    # for the first eight hours of every local day.
    state.put(session, state.PRICE_UPDATED_AT_KEY, now_local().date().isoformat())


def _is_coords(text: str) -> bool:
    if "," not in text:
        return False
    try:
        lat, lon = (float(p.strip()) for p in text.split(",", 1))
    except ValueError:
        return False
    return -90 <= lat <= 90 and -180 <= lon <= 180


def match_source(location_or_coords: str, session: Session) -> str | None:
    """"home"/"office" if this charge's location plausibly matches a Place
    the user has actually named that, else None (caller falls back to the
    saved default source) — never a blind guess off arbitrary text (a
    manually-typed "Home Depot Parking" shouldn't price as home charging).

    Accepts either raw "lat, lon" (the live sync path, before _place()
    resolves it to a display name — matched by distance) or a plain place
    name (manual entry / CSV import, which has no coordinates — matched by
    a case-insensitive substring against the registered Place's own name)."""
    text = (location_or_coords or "").strip()
    if not text:
        return None
    homes_and_offices = [
        p for p in session.query(Place).all() if p.name.strip().lower() in ("home", "office")
    ]
    if not homes_and_offices:
        return None
    if _is_coords(text):
        best_source, best_km = None, None
        for p in homes_and_offices:
            d = haversine_km(text, f"{p.lat}, {p.lon}")
            if d is not None and d <= HOME_OFFICE_MATCH_RADIUS_KM and (best_km is None or d < best_km):
                best_source, best_km = p.name.strip().lower(), d
        return best_source
    low = text.lower()
    for p in homes_and_offices:
        name = p.name.strip().lower()
        if name in low:
            return name
    return None


def resolve_source_and_rate(
    session: Session, settings, location_or_coords: str, dc: bool, dt: datetime,
) -> tuple[str, float]:
    """(source, rate) for a newly-priced charge — auto-matched Home/Office
    location wins, else the saved default source. Public still falls back to
    flat/ToU pricing (tariff.price_at) when its own rate is 0, exactly as
    tariff.charge_price_at already does for the non-preference-aware path.

    Callers should persist the returned source on the Charge row
    (price_source) rather than re-deriving "was this home/office/public"
    later by comparing the stored cost to whatever rates happen to be
    configured *then* — rates change over time, so that comparison quietly
    stops matching for anything already logged."""
    source = match_source(location_or_coords, session) or get_default_source(session)
    return source, rate_for_source(session, settings, source, dc, dt)


def resolve_pricing(
    session: Session, settings, location_or_coords: str, dc: bool, dt: datetime,
    max_power_kw: float = 0.0,
) -> tuple[str, float, str]:
    """resolve_source_and_rate plus the charger profile that applied, as
    (source, rate, profile_name); profile_name is "" when none did.

    A DC session (or one over CONDO_DC_MIN_KW) that starts inside the Home
    area, with a Home charger profile saved, is priced as public DC at the
    profile's rate rather than the Home rate. Callers store the name on
    Charge.charger_profile; the location label is left as it was."""
    profile = get_profile(session)
    if profile and is_profile_match(session, location_or_coords, dc, max_power_kw):
        rate = profile["rate"]
        if rate is None:
            rate = rate_for_source(session, settings, "public", True, dt)
        return "public", float(rate), profile["name"]
    source, rate = resolve_source_and_rate(session, settings, location_or_coords, dc, dt)
    return source, rate, ""


# A session faster than this cannot be AC at home, so it is treated as DC.
CONDO_DC_MIN_KW = 15.0
PROFILE_DEFAULT_NAME = "Condo DC charger"


def get_profile(session: Session) -> dict | None:
    """The saved Home charger profile: {name, kind, rate} or None. Holds only
    a label and an optional RM/kWh (None = use the public DC rate)."""
    raw = state.get(session, state.CHARGER_PROFILE_KEY, "")
    if not raw:
        return None
    try:
        data = json.loads(raw)
        rate = data.get("rate")
        return {"name": str(data.get("name") or PROFILE_DEFAULT_NAME)[:40],
                "kind": "public_dc",
                "rate": float(rate) if rate is not None else None}
    except (ValueError, TypeError, AttributeError):
        return None


def save_profile(session: Session, name: str, rate: float | None) -> dict:
    name = (name or "").strip()[:40] or PROFILE_DEFAULT_NAME
    state.put(session, state.CHARGER_PROFILE_KEY,
              json.dumps({"name": name, "kind": "public_dc", "rate": rate}))
    return get_profile(session)


def clear_profile(session: Session) -> None:
    state.put(session, state.CHARGER_PROFILE_KEY, "")


def is_profile_match(session: Session, location_or_coords: str, dc: bool,
                     max_power_kw: float = 0.0) -> bool:
    """DC (or over CONDO_DC_MIN_KW) and inside the Home area. Coordinates
    must fall inside the Home place's own geofence (its radius_km), tighter
    than the 2 km pricing match, so a public charger down the road is not
    swept in."""
    if not (dc or (max_power_kw or 0.0) > CONDO_DC_MIN_KW):
        return False
    text = (location_or_coords or "").strip()
    if not text:
        return False
    homes = [p for p in session.query(Place).all() if p.name.strip().lower() == "home"]
    if _is_coords(text):
        for p in homes:
            d = haversine_km(text, f"{p.lat}, {p.lon}")
            if d is not None and d <= (p.radius_km or 0.15):
                return True
        return False
    return bool(homes) and "home" in text.lower()


def charge_rate(c: Charge) -> float | None:
    """RM/kWh a stored session was actually billed at (cost over payable kWh)."""
    kwh = (c.payable_kwh or 0.0) or (c.energy_added_kwh or 0.0)
    return round(c.cost / kwh, 3) if kwh and not c.is_free else None


def past_profile_candidates(session: Session) -> list[Charge]:
    """Stored sessions the profile would have tagged: DC (or fast), in the
    Home area, not already tagged, not free."""
    out = []
    for c in session.query(Charge).filter(Charge.charger_profile == "").order_by(Charge.start_time):
        if c.is_free:
            continue
        if is_profile_match(session, c.location or "", c.charge_type == "DC", c.max_power_kw):
            out.append(c)
    return out


def apply_to_past(session: Session) -> int:
    """Tag the candidates as public DC under the profile. Changes only
    price_source, charger_profile (and charge_type when over the power
    threshold) — never energy, SoC, kWh, cost or location."""
    profile = get_profile(session)
    if not profile:
        return 0
    rows = past_profile_candidates(session)
    for c in rows:
        c.price_source = "public"
        c.charger_profile = profile["name"]
        c.charge_type = "DC"
    session.commit()
    return len(rows)


def rate_for_source(session: Session, settings, source: str, dc: bool, dt: datetime) -> float:
    """The rate per kWh for a named source (home / office / public) and current
    type, as resolve_source_and_rate prices a new charge."""
    rates = get_rates(session, settings)
    if source != "public":
        return rates[f"{source}_{'dc' if dc else 'ac'}"]
    override = rates["public_dc"] if dc else rates["public_ac"]
    if override > 0:
        return override
    return tariff.price_at(
        dt, settings.energy_price_per_kwh,
        settings.energy_price_peak_kwh, settings.energy_price_offpeak_kwh,
        settings.tariff_peak_start_hour, settings.tariff_peak_end_hour,
        settings.tariff_weekend_offpeak,
    )


def payable_kwh(energy_added_kwh: float, charge_type: str, settings,
                billed_kwh: float = 0.0) -> float:
    """The kWh a session is priced on. A receipt's figure when there is one;
    otherwise an AC session's energy plus the charger-to-pack loss (the meter
    bills more than reached the pack); a DC session's as the car reports it.
    """
    if billed_kwh and billed_kwh > 0:
        return float(billed_kwh)
    kwh = float(energy_added_kwh or 0.0)
    if (charge_type or "AC") != "DC":
        kwh *= 1.0 + max(0.0, float(getattr(settings, "ac_charge_loss_pct", 0.0))) / 100.0
    return kwh
