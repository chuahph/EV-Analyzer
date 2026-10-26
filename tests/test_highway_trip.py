"""The highway trip estimator's maths lives in app/static/highway_trip.js so the
dashboard (live and static) runs one copy of it. Its unit tests are Node's
built-in test runner; this runs them as part of pytest."""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_highway_trip_js_unit_tests():
    proc = subprocess.run(
        ["node", "--test", str(ROOT / "tests" / "js" / "highway_trip.test.js")],
        cwd=ROOT, capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_trip_plan_save_compare_and_delete():
    """A saved estimate lists back, takes an actual arrival %, reports the
    estimate-vs-actual error, rejects bad input, and can be deleted."""
    from fastapi.testclient import TestClient

    from app.config import get_settings
    from app.main import app

    settings = get_settings()
    old = settings.app_passcode
    settings.app_passcode = ""
    plan = {
        "route_km": 357, "start_pct": 80, "speed_kmh": 110, "passengers": 2,
        "luggage": "light", "aircon": "normal", "weather": "hot",
        "wh_per_km": 146.2, "basis": "default", "est_arrival_pct": 10.0,
        "verdict": "topup", "stop_minutes": 11,
    }
    try:
        with TestClient(app) as client:  # startup seeds demo data
            for p in client.get("/api/trip-plans").json()["plans"]:
                client.delete(f"/api/trip-plans/{p['id']}")

            resp = client.post("/api/trip-plans", json=plan)
            assert resp.status_code == 200, resp.text
            plan_id = resp.json()["id"]
            body = client.get("/api/trip-plans").json()
            assert [p["id"] for p in body["plans"]] == [plan_id]
            assert body["plans"][0]["verdict"] == "topup"
            assert body["n_compared"] == 0 and body["mean_error_pct"] is None

            resp = client.patch(f"/api/trip-plans/{plan_id}", json={"actual_arrival_pct": 12})
            assert resp.status_code == 200
            # Arrived on 12% after the stop vs a planned 10%.
            assert resp.json()["error_pct"] == 2.0
            body = client.get("/api/trip-plans").json()
            assert body["n_compared"] == 1 and body["mean_error_pct"] == 2.0
            # Blank clears it again.
            resp = client.patch(f"/api/trip-plans/{plan_id}", json={"actual_arrival_pct": None})
            assert resp.json()["actual_arrival_pct"] is None

            # Validation.
            assert client.post("/api/trip-plans", json={**plan, "start_pct": 150}).status_code == 400
            assert client.post("/api/trip-plans", json={**plan, "weather": "snow"}).status_code == 400
            assert client.post("/api/trip-plans", json={**plan, "route_km": "x"}).status_code == 400
            bad = {k: v for k, v in plan.items() if k != "speed_kmh"}
            assert client.post("/api/trip-plans", json=bad).status_code == 400
            assert client.patch(f"/api/trip-plans/{plan_id}",
                                json={"actual_arrival_pct": 101}).status_code == 400

            assert client.delete(f"/api/trip-plans/{plan_id}").status_code == 200
            assert client.get("/api/trip-plans").json()["plans"] == []
            assert client.delete(f"/api/trip-plans/{plan_id}").status_code == 404
            assert client.patch(f"/api/trip-plans/{plan_id}", json={}).status_code == 404
    finally:
        settings.app_passcode = old
