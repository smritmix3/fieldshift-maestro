"""NASA data agent: live NASA POWER + CMR for the field. Takes plain dicts and an `emit(agent, status, msg)` callback
so the UI can show progress in real time. (Crop analysis and scoring live in crops.py / evaluate.py.)"""
from __future__ import annotations

import math
import statistics
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

from . import nasa

STRATEGY_BLURB = {
    "Current Baseline": "Keep to your usual rotation.",
    "Climate-Resilient Cover Crop": "A drought-tolerant crop that keeps the ground covered.",
    "Deep-Root N-Fixer": "A deep-rooted legume that restores soil and saves fertilizer.",
}
WEIGHTS = {  # [water, soil, erosion, econ]
    "Water": (0.45, 0.20, 0.15, 0.20),
    "Soil": (0.20, 0.45, 0.20, 0.15),
    "Income": (0.15, 0.20, 0.15, 0.50),
}


def _clip(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


# ----------------------------------------------------------------------------- NASA Data Agent
def _extraterrestrial_mm(lat: float, d: date) -> float:
    """FAO-56 extraterrestrial radiation, in mm/day equivalent."""
    phi, j = math.radians(lat), d.timetuple().tm_yday
    dr = 1 + 0.033 * math.cos(2 * math.pi * j / 365)
    dec = 0.409 * math.sin(2 * math.pi * j / 365 - 1.39)
    ws = math.acos(_clip(-math.tan(phi) * math.tan(dec), -1, 1))
    ra = 24 * 60 / math.pi * 0.0820 * dr * (ws * math.sin(phi) * math.sin(dec) + math.cos(phi) * math.cos(dec) * math.sin(ws))
    return 0.408 * ra


def _window(series: dict[date, float], end: date, days: int) -> list[float]:
    return [series[end - timedelta(days=i)] for i in range(days) if (end - timedelta(days=i)) in series]


def _feature(value, unit, prov, age=0.0, source="", note="", z=None, tau=7.0):
    return dict(value=value, unit=unit, provenance=prov, age_days=age, source=source, note=note, z=z,
                fresh=math.exp(-age / tau))


def _power_features(power: dict, lat: float) -> dict:
    last = max(power["PRECTOTCORR"])
    age = (date.today() - last).days
    src = "NASA POWER"

    def zscore(series: dict[date, float], days: int, agg) -> tuple[float, float | None]:
        cur = agg(_window(series, last, days))
        hist = []
        for k in (1, 2, 3):
            w = _window(series, last - timedelta(days=365 * k), days)
            if len(w) >= days * 0.8:
                hist.append(agg(w))
        if len(hist) < 2:
            return cur, None
        mu = statistics.mean(hist)
        sd = max(statistics.stdev(hist), 0.15 * abs(mu), 1e-6)  # floor: only 3 prior years
        return cur, (cur - mu) / sd

    p30, z30 = zscore(power["PRECTOTCORR"], 30, sum)
    p7, z7 = zscore(power["PRECTOTCORR"], 7, sum)
    wet, zw = zscore(power["GWETROOT"], 7, statistics.mean)
    t30 = statistics.mean(_window(power["T2M"], last, 30))
    tmax = statistics.mean(_window(power["T2M_MAX"], last, 30))
    tmin = statistics.mean(_window(power["T2M_MIN"], last, 30))
    et0 = 0.0023 * (t30 + 17.8) * math.sqrt(max(tmax - tmin, 0.1)) * _extraterrestrial_mm(lat, last)
    note_p = "GPM IMERG-based precipitation"
    return {
        "precip_30d": _feature(round(p30, 1), "mm", "live", age, src, note_p, z30),
        "precip_7d": _feature(round(p7, 1), "mm", "live", age, src, note_p, z7),
        "soil_wetness_root": _feature(round(wet, 2), "0-1", "modeled", age, src, "MERRA-2 modeled root-zone wetness (not SMAP pixels)", zw),
        "temp_30d": _feature(round(t30, 1), "°C", "live", age, src),
        "et0": _feature(round(et0, 2), "mm/day", "live", age, src, "Hargreaves-Samani from POWER temperature"),
    }


def nasa_data_agent(ctx: dict, emit, simulate_fail: bool = False) -> dict:
    lat, lon = ctx["lat"], ctx["lon"]
    emit("NASA Data", "start", f"Querying NASA POWER + 4 CMR collections in parallel for ({lat:.2f}, {lon:.2f})")
    sources: dict[str, dict] = {}
    power = None
    sat_age: dict[str, float] = {}
    with ThreadPoolExecutor(max_workers=5) as pool:
        futs = {}
        names = ["NASA POWER", *nasa.CMR_COLLECTIONS]
        if simulate_fail:
            emit("NASA Data", "warn", "Simulated outage: skipping all NASA calls")
            for n in names:
                sources[n] = dict(status="failed", ms=0, detail="simulated outage")
        else:
            futs[pool.submit(nasa.timed, nasa.fetch_power, lat, lon)] = "NASA POWER"
            for label, short in nasa.CMR_COLLECTIONS.items():
                futs[pool.submit(nasa.timed, nasa.fetch_cmr_latest, short, lat, lon)] = label
        for f in as_completed(futs):
            label = futs[f]
            res, err, ms = f.result()
            if err:
                sources[label] = dict(status="failed", ms=ms, detail=err)
                emit("NASA Data", "warn", f"{label}: {err} ({ms:.0f} ms)")
                continue
            if label == "NASA POWER":
                power = res
                detail = f"{len(res['PRECTOTCORR'])} daily values, latest {max(res['PRECTOTCORR'])}"
            else:
                sat_age[label] = res["age_days"]
                detail = f"latest granule {res['age_days']:.1f} days old"
            sources[label] = dict(status="live", ms=ms, detail=detail)
            emit("NASA Data", "ok", f"{label}: {detail} ({ms:.0f} ms)")

    if power:
        features = _power_features(power, lat)
    else:
        emit("NASA Data", "warn", "NASA POWER unavailable: no recent-weather features (nothing is substituted or invented)")
        features = {}
    for label, age in sat_age.items():
        features[f"pass:{label}"] = _feature(round(age, 1), "days since last pass", "live", age, "NASA CMR",
                                             "availability only; pixel values not ingested", tau=5.0)

    z = (features.get("precip_30d") or {}).get("z")
    if z is not None:
        emit("NASA Data", "ok", f"Anomaly check: 30-day rain z={z:+.1f} vs prior years ({'wetter' if z > 1 else 'drier' if z < -1 else 'near normal'})")
    return {"features": features, "sources": sources}
