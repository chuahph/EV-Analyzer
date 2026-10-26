"use strict";
/* Highway trip estimator — Penang → KL by default (357 km on the E1).
 *
 * Pure calculation, no DOM: app.js feeds it the form values plus what the
 * dashboard payload already knows (this car's measured highway Wh/km and its
 * usable pack size) and renders whatever comes back. Loaded as a plain
 * <script> in the browser (window.HighwayTrip) and as a CommonJS module by
 * the tests (tests/js/highway_trip.test.js), so there is exactly one copy of
 * the maths.
 *
 * Every figure below is an ESTIMATE built from published third-party data and
 * simple physics. None of it is Tesla's own data or Tesla's trip planner.
 */
(function (root) {
  // --- Route ---------------------------------------------------------------
  // George Town → KLCC via the North–South Expressway (E1). The figure CPH
  // drives; editable on the form for any other start/end.
  const ROUTE_KM_DEFAULT = 357;
  // The suggested top-up stop and how far into the trip it is. Bukit Gantang
  // R&R (Perak, E1) sits roughly 110 km south of the Penang Bridge — a TomTom
  // route George Town → Bukit Gantang → KLCC puts it a little under a third
  // of the way. Approximate; it only decides how much charge you arrive at
  // the stop with.
  const STOP_NAME = "Bukit Gantang R&R";
  const STOP_KM_FROM_START = 110;

  // --- Consumption basis ---------------------------------------------------
  // Default highway consumption for a Model 3 Highland RWD, used only when the
  // app has no measured highway drives and no manual figure is entered.
  // Source: EV Database "Highway – Mild Weather" for the Highland RWD
  // (ev-database.org/car/3403), which is a steady ~110 km/h, 23 °C, climate
  // on. 138 Wh/km at the battery.
  const DEFAULT_WH_PER_KM = 138;
  // The speed DEFAULT_WH_PER_KM was measured at; every other speed is scaled
  // from it.
  const REF_SPEED_KMH = 110;
  // The app's own "Highway (90+)" speed band averages trips whose average
  // speed is 90 km/h or more. Real E1 trips with toll plazas and slowdowns
  // average around 100, so a measured band figure is treated as a 100 km/h
  // figure and rescaled from there.
  const MEASURED_HIGHWAY_REF_KMH = 100;

  // --- Speed ---------------------------------------------------------------
  // Aerodynamic drag grows with the square of speed; rolling resistance,
  // drivetrain losses and electronics are roughly flat per km. At 110 km/h
  // aero is about 60% of a Model 3's consumption (Cd 0.219, ~2.2 m² frontal
  // area). Check against a published speed sweep for the Highland RWD
  // (evtrip.dev/ev/tesla-model-3: 154 Wh/km @100, 175 @110, 198 @120):
  // this split gives ×0.90 at 100 and ×1.11 at 120 against their ×0.88 and
  // ×1.13 — close enough for a planning estimate.
  const AERO_SHARE_AT_REF = 0.6;

  // --- Load ----------------------------------------------------------------
  // Mass only moves the rolling-resistance part of consumption on a
  // mostly-flat highway (no net climb Penang → KL to speak of). Rolling is
  // about 30% of consumption at 110 km/h, and scales with total mass.
  const ROLLING_SHARE_AT_REF = 0.3;
  // Highland RWD kerb weight ~1,760 kg + a 75 kg driver.
  const BASE_MASS_KG = 1835;
  const PASSENGER_KG = 75;   // each person beyond the driver
  const LUGGAGE_KG = { none: 0, light: 20, full: 60 };

  // --- Aircon & weather ----------------------------------------------------
  // Average compressor + blower draw in kW. The default/measured figures
  // already include ordinary A/C use, so "normal" is the zero point and the
  // others are a difference against it. Typical Tesla heat-pump figures in
  // hot climates: ~0.5–2 kW depending on setpoint and sun.
  const AIRCON_KW = { off: 0.0, eco: 0.6, normal: 1.0, max: 1.8 };
  // Weather. `factor` scales consumption (wet roads raise rolling resistance
  // and spray adds drag: ~5% in rain, ~12% in a heavy downpour per published
  // EV range tests). `acExtraKw` is the extra A/C work a hot, sunny day
  // costs over the 23 °C reference — only charged when the A/C is on.
  const WEATHER = {
    mild: { factor: 1.0, acExtraKw: 0.0 },        // ≤ 30 °C / overcast
    hot: { factor: 1.0, acExtraKw: 0.5 },         // ≥ 32 °C, full sun
    rain: { factor: 1.05, acExtraKw: 0.0 },
    heavy_rain: { factor: 1.12, acExtraKw: 0.0 },
  };

  // --- Battery & charging --------------------------------------------------
  // Usable pack when the app doesn't know this car's own figure: Model 3
  // Highland RWD LFP pack, 57.5 kWh usable (same figure as
  // app/analysis/battery.LFP_USABLE_KWH).
  const USABLE_KWH_DEFAULT = 57.5;
  // Arrive at KL (and leave the stop) planning on at least this much left.
  const ARRIVAL_BUFFER_PCT = 10;
  // Don't plan to roll into the charger with less than this.
  const STOP_ARRIVAL_MIN_PCT = 5;
  // Charger rating assumed at Bukit Gantang. NB tesla.com's Malaysia
  // Supercharger list did not show a Bukit Gantang site when this was
  // written (Oct 2026); DC chargers from other operators are reported at the
  // R&R. 150 kW is a conservative middle figure — change it to the rating on
  // the stall you actually use (a 250 kW V3 Supercharger is capped by the
  // car anyway, see CAR_PEAK_KW).
  const CHARGER_KW = 150;
  // Peak DC rate the Highland RWD accepts (~170 kW, evtrip.dev / EV Database).
  const CAR_PEAK_KW = 170;
  // Rough DC taper as a share of CAR_PEAK_KW, by state of charge: [up to
  // SoC %, share]. Shaped from published Model 3 RWD curves; gives ~24 min
  // for 10→80%, against Tesla's own "about 25 min" claim for the RWD.
  const CHARGE_CURVE = [
    [20, 0.95], [40, 0.85], [50, 0.70], [60, 0.55],
    [70, 0.45], [80, 0.35], [90, 0.22], [100, 0.12],
  ];
  // Parking, plugging in and the handshake before power flows.
  const CHARGE_OVERHEAD_MIN = 2;

  const clamp = (x, lo, hi) => Math.min(hi, Math.max(lo, x));
  const num = (x) => (x === null || x === undefined || x === "" ? NaN : Number(x));

  // Consumption multiplier for cruising at `kmh` relative to `refKmh`.
  function speedFactor(kmh, refKmh) {
    const r = kmh / refKmh;
    return (1 - AERO_SHARE_AT_REF) + AERO_SHARE_AT_REF * r * r;
  }

  function chargePowerKw(soc) {
    const band = CHARGE_CURVE.find(([upTo]) => soc < upTo) || CHARGE_CURVE[CHARGE_CURVE.length - 1];
    return Math.min(CHARGER_KW, CAR_PEAK_KW * band[1]);
  }

  // Minutes to charge from `fromPct` to `toPct` on a pack of `usableKwh`,
  // integrated in 1% steps along CHARGE_CURVE, plus the plug-in overhead.
  function chargeMinutes(fromPct, toPct, usableKwh) {
    if (!(toPct > fromPct)) return 0;
    let min = 0;
    for (let s = fromPct; s < toPct; s += 1) {
      const step = Math.min(1, toPct - s);
      min += (usableKwh * step / 100) / chargePowerKw(s) * 60;
    }
    return Math.ceil(min + CHARGE_OVERHEAD_MIN);
  }

  // The Wh/km a drive at these conditions would use, and which basis it came
  // from. Exactly one basis is used:
  //   manual   — the figure typed in; taken as-is, no factors applied (you
  //              stated the number; scaling it would second-guess you)
  //   measured — this car's own "Highway (90+)" average, rescaled for speed,
  //              load, A/C and weather
  //   default  — DEFAULT_WH_PER_KM, scaled the same way
  function consumption(inp, ctx) {
    const manual = num(inp.manualWhPerKm);
    if (isFinite(manual) && manual > 0) {
      return { whPerKm: manual, basis: "manual", baseWhPerKm: manual, factorsApplied: false };
    }
    const measured = num(ctx && ctx.measuredHighwayWhPerKm);
    const useMeasured = isFinite(measured) && measured > 0;
    // Bring the basis to the 110 km/h reference first, so the speed scaling
    // below works from one known point whichever basis is in play.
    const base110 = useMeasured
      ? measured / speedFactor(MEASURED_HIGHWAY_REF_KMH, REF_SPEED_KMH)
      : DEFAULT_WH_PER_KM;

    const speed = inp.speedKmh;
    const extraKg = (inp.passengers - 1) * PASSENGER_KG + (LUGGAGE_KG[inp.luggage] || 0);
    const massFactor = 1 + ROLLING_SHARE_AT_REF * extraKg / BASE_MASS_KG;
    const weather = WEATHER[inp.weather] || WEATHER.mild;
    const acKw = AIRCON_KW[inp.aircon] ?? AIRCON_KW.normal;
    // A/C is power over time; at a steady speed that's kW ÷ km/h = kWh/km,
    // ×1000 for Wh/km. Relative to "normal", so "off" comes out negative.
    const acDeltaKw = acKw - AIRCON_KW.normal + (acKw > 0 ? weather.acExtraKw : 0);
    const acWhPerKm = acDeltaKw * 1000 / speed;

    const wh = base110 * speedFactor(speed, REF_SPEED_KMH) * massFactor * weather.factor + acWhPerKm;
    return {
      whPerKm: wh,
      basis: useMeasured ? "measured" : "default",
      baseWhPerKm: useMeasured ? measured : DEFAULT_WH_PER_KM,
      factorsApplied: true,
    };
  }

  // Normalise and range-check the form. Returns {ok:false, error} for input
  // that can't be planned, so the card shows a hint instead of nonsense.
  function normalise(raw) {
    const routeKm = num(raw.routeKm);
    const startPct = num(raw.startPct);
    const speedKmh = num(raw.speedKmh);
    const passengers = num(raw.passengers);
    if (!(routeKm > 0 && routeKm <= 1500)) return { ok: false, error: "Enter a route distance (km)." };
    if (!(startPct >= 0 && startPct <= 100)) return { ok: false, error: "Enter a start battery between 0 and 100%." };
    if (!(speedKmh >= 40 && speedKmh <= 160)) return { ok: false, error: "Enter a highway speed between 40 and 160 km/h." };
    return {
      ok: true,
      routeKm, startPct, speedKmh,
      passengers: isFinite(passengers) ? clamp(Math.round(passengers), 1, 7) : 1,
      luggage: raw.luggage in LUGGAGE_KG ? raw.luggage : "none",
      aircon: raw.aircon in AIRCON_KW ? raw.aircon : "normal",
      weather: raw.weather in WEATHER ? raw.weather : "mild",
      manualWhPerKm: raw.manualWhPerKm,
    };
  }

  /* estimate(raw, ctx) → result
   *   raw: {routeKm, startPct, speedKmh, passengers, luggage, aircon,
   *         weather, manualWhPerKm?}
   *   ctx: {measuredHighwayWhPerKm?, usableKwh?}
   * result.verdict is one of:
   *   "go"           — arrive with ≥ ARRIVAL_BUFFER_PCT, no stop needed
   *   "topup"        — stop at Bukit Gantang for `stop.minutes`
   *   "charge_first" — can't reach the stop safely; charge before leaving
   */
  function estimate(raw, ctx) {
    const inp = normalise(raw || {});
    if (!inp.ok) return inp;
    ctx = ctx || {};
    const usable = num(ctx.usableKwh) > 0 ? num(ctx.usableKwh) : USABLE_KWH_DEFAULT;
    const cons = consumption(inp, ctx);
    const pctPerKm = cons.whPerKm / 1000 / usable * 100;

    const tripPct = inp.routeKm * pctPerKm;
    const arrivalPct = inp.startPct - tripPct;
    // Start charge that would make the whole run without stopping.
    const startNeededPct = tripPct + ARRIVAL_BUFFER_PCT;
    const res = {
      ok: true,
      input: inp,
      whPerKm: cons.whPerKm,
      basis: cons.basis,
      baseWhPerKm: cons.baseWhPerKm,
      factorsApplied: cons.factorsApplied,
      usableKwh: usable,
      usableKwhSource: num(ctx.usableKwh) > 0 ? "car" : "default",
      energyKwh: inp.routeKm * cons.whPerKm / 1000,
      tripPct,
      arrivalPct,
      startNeededPct,
      bufferPct: ARRIVAL_BUFFER_PCT,
      driveMin: Math.round(inp.routeKm / inp.speedKmh * 60),
      verdict: "go",
      stop: null,
    };
    if (arrivalPct >= ARRIVAL_BUFFER_PCT) return res;

    const stopReachable = inp.routeKm > STOP_KM_FROM_START;
    const atStopPct = inp.startPct - STOP_KM_FROM_START * pctPerKm;
    if (stopReachable && atStopPct >= STOP_ARRIVAL_MIN_PCT) {
      // Charge just enough at the stop to finish with the buffer intact.
      const restKm = inp.routeKm - STOP_KM_FROM_START;
      const needLeavePct = restKm * pctPerKm + ARRIVAL_BUFFER_PCT;
      const leavePct = Math.min(100, needLeavePct);
      // Never "charge" downwards: the stop is only suggested when arrival is
      // short, so leavePct > atStopPct always holds here, but guard anyway.
      const fromPct = Math.max(0, atStopPct);
      res.verdict = "topup";
      res.stop = {
        name: STOP_NAME,
        kmFromStart: STOP_KM_FROM_START,
        arrivePct: atStopPct,
        leavePct,
        addPct: Math.max(0, leavePct - fromPct),
        minutes: chargeMinutes(fromPct, leavePct, usable),
        chargerKw: CHARGER_KW,
        // False when even a full charge leaves less than the buffer for the
        // rest of the way at these conditions — slow down or stop again.
        enough: needLeavePct <= 100,
      };
      res.arrivalWithStopPct = leavePct - restKm * pctPerKm;
      return res;
    }
    // Too little to reach Bukit Gantang with a margin (or the route is
    // shorter than the stop is far): charge before leaving instead.
    res.verdict = "charge_first";
    res.chargeBeforePct = startNeededPct <= 100
      ? startNeededPct
      : STOP_KM_FROM_START * pctPerKm + STOP_ARRIVAL_MIN_PCT;
    res.chargeBeforeThenStop = startNeededPct > 100;
    return res;
  }

  const api = {
    estimate, chargeMinutes, speedFactor,
    constants: {
      ROUTE_KM_DEFAULT, STOP_NAME, STOP_KM_FROM_START, DEFAULT_WH_PER_KM,
      REF_SPEED_KMH, MEASURED_HIGHWAY_REF_KMH, AERO_SHARE_AT_REF,
      ROLLING_SHARE_AT_REF, BASE_MASS_KG, PASSENGER_KG, LUGGAGE_KG, AIRCON_KW,
      WEATHER, USABLE_KWH_DEFAULT, ARRIVAL_BUFFER_PCT, STOP_ARRIVAL_MIN_PCT,
      CHARGER_KW, CAR_PEAK_KW, CHARGE_CURVE, CHARGE_OVERHEAD_MIN,
    },
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  else root.HighwayTrip = api;
})(typeof window !== "undefined" ? window : this);
