"""Saved Home charger profile (the condo's public DC charger)."""
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app import pricing_prefs
from app.analysis.charging import analyze
from app.config import get_settings
from app.main import app
from app.models import Charge, Place, Vehicle

T0 = datetime(2026, 10, 31, 12, 1)


@pytest.fixture
def home(session):
    session.add(Place(name="Home", lat=3.1390, lon=101.6869, radius_km=0.15, created_at=datetime.now()))
    session.add(Vehicle(vin="V1", name="Car", model="M"))
    session.commit()
    return session


def _charge(session, kind="DC", kw=37.0, loc="Home", kwh=20.0, cost=21.38, src="home", **kw_):
    c = Charge(vehicle_id=1, start_time=T0, end_time=T0.replace(hour=12, minute=30),
               duration_min=29, start_soc=40, end_soc=60, energy_added_kwh=kwh,
               charge_type=kind, max_power_kw=kw, location=loc, cost=cost,
               price_source=src, **kw_)
    session.add(c)
    session.commit()
    return c


def test_dc_at_home_with_profile_is_public_dc_at_profile_rate(home):
    pricing_prefs.save_profile(home, "Condo DC charger", 1.069)
    src, rate, name = pricing_prefs.resolve_pricing(
        home, get_settings(), "Home", True, T0, 37.0)
    assert (src, rate, name) == ("public", 1.069, "Condo DC charger")


def test_over_15kw_counts_as_dc_even_if_flagged_ac(home):
    pricing_prefs.save_profile(home, "Condo DC charger", 1.069)
    _, _, name = pricing_prefs.resolve_pricing(home, get_settings(), "Home", False, T0, 22.0)
    assert name == "Condo DC charger"


def test_dc_at_home_without_profile_is_unchanged(home):
    src, rate, name = pricing_prefs.resolve_pricing(
        home, get_settings(), "Home", True, T0, 37.0)
    assert (src, name) == ("home", "")
    assert rate == pricing_prefs.get_rates(home, get_settings())["home_dc"]


def test_ac_at_home_gets_the_label_but_keeps_the_home_ac_price(home):
    """All Home charging is on the condo's chargers (31 October): AC there is
    labelled too, and stays Home AC at the Home AC rate."""
    pricing_prefs.save_profile(home, "Condo DC charger", 1.069)
    src, rate, name = pricing_prefs.resolve_pricing(
        home, get_settings(), "Home", False, T0, 7.4)
    assert (src, name) == ("home", "Condo DC charger")
    assert rate == pricing_prefs.get_rates(home, get_settings())["home_ac"]


def test_dc_away_from_home_is_unchanged_with_profile(home):
    pricing_prefs.save_profile(home, "Condo DC charger", 1.069)
    # 5 km from the Home place, and a plain name that is not Home.
    _, _, name = pricing_prefs.resolve_pricing(home, get_settings(), "3.1840, 101.6869", True, T0, 60.0)
    assert name == ""
    _, _, name = pricing_prefs.resolve_pricing(home, get_settings(), "Mall Supercharger", True, T0, 60.0)
    assert name == ""


def test_past_preview_then_apply_changes_only_the_tag(home):
    old_dc = _charge(home)                              # like charge #102
    ac = _charge(home, kind="AC", kw=7.4, cost=18.0)
    away = _charge(home, loc="Mall", src="public")
    pricing_prefs.save_profile(home, "Condo DC charger", 1.069)

    ids = [c.id for c in pricing_prefs.past_profile_candidates(home)]
    assert ids == [old_dc.id, ac.id]                    # preview only
    assert old_dc.price_source == "home"                # nothing changed yet

    before = (old_dc.energy_added_kwh, old_dc.start_soc, old_dc.end_soc,
              old_dc.cost, old_dc.location, old_dc.payable_kwh)
    ac_before = (ac.charge_type, ac.cost, ac.payable_kwh)
    assert pricing_prefs.apply_to_past(home) == 2
    assert (old_dc.price_source, old_dc.charger_profile) == ("public", "Condo DC charger")
    assert before == (old_dc.energy_added_kwh, old_dc.start_soc, old_dc.end_soc,
                      old_dc.cost, old_dc.location, old_dc.payable_kwh)
    # The AC one gets the label only: still Home AC, same cost.
    assert (ac.price_source, ac.charger_profile) == ("home", "Condo DC charger")
    assert (ac.charge_type, ac.cost, ac.payable_kwh) == ac_before
    assert (away.price_source, away.charger_profile) == ("public", "")
    assert pricing_prefs.apply_to_past(home) == 0       # idempotent


def test_home_ac_rate_leaves_out_the_condo_dc_charger(home):
    ac = _charge(home, kind="AC", kw=7.4, kwh=10.0, cost=9.0)
    dc = _charge(home)
    pricing_prefs.save_profile(home, "Condo DC charger", 1.069)
    pricing_prefs.apply_to_past(home)
    out = analyze([ac, dc])
    assert out["home_ac_cost_per_kwh"] == 0.9
    assert out["home_ac_sessions"] == 1
    assert out["condo_dc_sessions"] == 1
    assert out["dc_sessions"] == 1 and out["dc_energy_kwh"] == 20.0
    rec = [r for r in out["recent_charges"] if r["charger_profile"] and r["charge_type"] == "DC"]
    assert rec and rec[0]["source"] == "public" and rec[0]["location"] == "Home"


def test_api_flow_save_from_card_auto_apply_and_apply_past():
    with TestClient(app) as client:
        from app.database import SessionLocal
        with SessionLocal() as s:
            if not s.query(Place).filter(Place.name == "Home").first():
                s.add(Place(name="Home", lat=3.1390, lon=101.6869, radius_km=0.15, created_at=datetime.now()))
            c = Charge(vehicle_id=s.query(Vehicle).first().id,
                       start_time=datetime(2026, 10, 31, 12, 1),
                       end_time=datetime(2026, 10, 31, 12, 40), duration_min=39,
                       start_soc=30, end_soc=62, energy_added_kwh=24.0, charge_type="DC",
                       max_power_kw=37.0, location="Home", cost=25.66, price_source="home")
            s.add(c)
            s.commit()
            cid = c.id
        try:
            # Without a profile the card is untouched.
            assert client.get("/api/charger-profile").json()["profile"] is None
            saved = client.post("/api/charger-profile", json={"charge_id": cid}).json()
            assert saved["profile"]["rate"] == 1.069        # default: that charge's rate
            assert saved["profile"]["name"] == "Condo DC charger"
            # Preview does not write.
            pv = client.post("/api/charger-profile/apply-past", json={}).json()
            assert pv["preview"] is True
            # A new DC session at Home is tagged automatically; label stays Home.
            new = client.post("/api/charges/manual", json={
                "start_time": "2026-11-01T10:00:00", "end_time": "2026-11-01T10:30:00",
                "energy_added_kwh": 20.0, "charge_type": "DC", "max_power_kw": 40,
                "location": "Home"}).json()["id"]
            with SessionLocal() as s:
                n = s.get(Charge, new)
                assert (n.price_source, n.charger_profile, n.location) == ("public", "Condo DC charger", "Home")
                assert n.cost == round(20.0 * 1.069, 2)
                s.delete(n)
                s.commit()
        finally:
            client.delete("/api/charger-profile")
            with SessionLocal() as s:
                s.delete(s.get(Charge, cid))
                s.commit()
