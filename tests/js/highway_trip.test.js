// Unit tests for app/static/highway_trip.js. Run with `node --test tests/js/highway_trip.test.js`
// (tests/test_highway_trip.py runs this as part of pytest).
"use strict";
const test = require("node:test");
const assert = require("node:assert/strict");
const H = require("../../app/static/highway_trip.js");

const C = H.constants;
const trip = (over = {}) => ({
  routeKm: C.ROUTE_KM_DEFAULT, startPct: 80, speedKmh: 110, passengers: 1,
  luggage: "none", aircon: "normal", weather: "mild", ...over,
});

test("80% start at 110 km/h lands in a sensible range on the default basis", () => {
  const r = H.estimate(trip(), {});
  assert.equal(r.ok, true);
  assert.equal(r.basis, "default");
  assert.equal(r.usableKwhSource, "default");
  assert.equal(Math.round(r.whPerKm), C.DEFAULT_WH_PER_KM);   // reference conditions → reference figure
  // 357 km × 138 Wh/km ≈ 49 kWh ≈ 86% of a 57.5 kWh pack.
  assert.ok(r.arrivalPct > -15 && r.arrivalPct < 10, `arrival ${r.arrivalPct}`);
});

test("a full start on a Long Range pack is a Go", () => {
  const r = H.estimate(trip({ startPct: 100 }), { usableKwh: 75 });
  assert.equal(r.verdict, "go");
  assert.equal(r.stop, null);
  assert.ok(r.arrivalPct >= C.ARRIVAL_BUFFER_PCT && r.arrivalPct < 50, `arrival ${r.arrivalPct}`);
});

test("a lowish start needs a Bukit Gantang top-up with minutes > 0", () => {
  const r = H.estimate(trip({ startPct: 60 }), {});
  assert.equal(r.verdict, "topup");
  assert.equal(r.stop.name, "Bukit Gantang R&R");
  assert.ok(r.stop.minutes > 0);
  assert.ok(r.stop.arrivePct >= C.STOP_ARRIVAL_MIN_PCT);
  // Charges just enough to finish on the buffer.
  assert.ok(Math.abs(r.arrivalWithStopPct - C.ARRIVAL_BUFFER_PCT) < 1e-9);
  assert.equal(r.stop.enough, true);
});

test("too little to reach the stop says charge before leaving", () => {
  const r = H.estimate(trip({ startPct: 20 }), {});
  assert.equal(r.verdict, "charge_first");
  assert.ok(r.chargeBeforePct > 20 && r.chargeBeforePct <= 100);
});

test("faster, heavier, hotter all cost more; rain costs more; A/C off costs less", () => {
  const wh = (o) => H.estimate(trip(o), {}).whPerKm;
  const ref = wh({});
  assert.ok(wh({ speedKmh: 130 }) > ref);
  assert.ok(wh({ speedKmh: 90 }) < ref);
  assert.ok(wh({ passengers: 4, luggage: "full" }) > ref);
  assert.ok(wh({ weather: "hot" }) > ref);
  assert.ok(wh({ weather: "heavy_rain" }) > wh({ weather: "rain" }));
  assert.ok(wh({ aircon: "off" }) < ref);
  // Hot weather adds A/C work only when the A/C is on.
  assert.equal(wh({ aircon: "off", weather: "hot" }), wh({ aircon: "off" }));
});

test("the car's measured highway figure is used and rescaled from 100 km/h", () => {
  const r = H.estimate(trip(), { measuredHighwayWhPerKm: 150, usableKwh: 60 });
  assert.equal(r.basis, "measured");
  assert.equal(r.usableKwhSource, "car");
  const expected = 150 / H.speedFactor(C.MEASURED_HIGHWAY_REF_KMH, C.REF_SPEED_KMH);
  assert.ok(Math.abs(r.whPerKm - expected) < 1e-9);
});

test("a manual Wh/km overrides everything and is not scaled", () => {
  const r = H.estimate(trip({ manualWhPerKm: "145", speedKmh: 130, passengers: 5 }),
    { measuredHighwayWhPerKm: 150 });
  assert.equal(r.basis, "manual");
  assert.equal(r.whPerKm, 145);
  assert.equal(r.factorsApplied, false);
});

test("blank or zero manual Wh/km falls back to the automatic basis", () => {
  assert.equal(H.estimate(trip({ manualWhPerKm: "" }), {}).basis, "default");
  assert.equal(H.estimate(trip({ manualWhPerKm: 0 }), {}).basis, "default");
});

test("invalid inputs are rejected with a message", () => {
  assert.equal(H.estimate(trip({ startPct: 120 }), {}).ok, false);
  assert.equal(H.estimate(trip({ routeKm: 0 }), {}).ok, false);
  assert.equal(H.estimate(trip({ speedKmh: "" }), {}).ok, false);
  assert.match(H.estimate(trip({ speedKmh: 10 }), {}).error, /speed/);
});

test("charge minutes: 10→80% is in the ~20–30 min range Tesla quotes, and monotone", () => {
  const m = H.chargeMinutes(10, 80, C.USABLE_KWH_DEFAULT);
  assert.ok(m >= 20 && m <= 30, `10→80 took ${m} min`);
  assert.equal(H.chargeMinutes(50, 50, 57.5), 0);
  assert.ok(H.chargeMinutes(10, 90, 57.5) > m);
  // The taper makes the top end slower per % than the bottom.
  assert.ok(H.chargeMinutes(80, 90, 57.5) > H.chargeMinutes(20, 30, 57.5));
});
