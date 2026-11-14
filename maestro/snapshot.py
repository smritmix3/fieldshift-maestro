"""Collect everything we know about one field, in parallel, streaming progress events.

Sources: NASA POWER + CMR (via nasa_data_agent), soil (SSURGO or SoilGrids), SRTM terrain, nearby water, ERA5.
A failed source never aborts the snapshot: it is recorded as failed and the rest continue.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed

from . import climate
from . import providers as pv
from .agents import nasa_data_agent
from .http import timed


def _summ(name: str, r: dict) -> str:
    if not r.get("available", True):
        return r.get("note", "no data")
    if name == "soil":
        return f"{r.get('texture', 'n/a')} ({r['source']}, {r['resolution']})"
    if name == "terrain":
        return f"{r['elevation_m']} m, slope {r['slope_pct']}% ({r['slope_class']})"
    if name == "water":
        d = r["nearest_distance_m"]
        return f"irrigation access {r['irrigation_access']}" + (f", nearest water about {d} m" if d else "")
    if name == "era5":
        return f"30-day rain {r['precip_30d_mm']} mm, soil moisture {r['soil_moisture_m3m3']}"
    return "ok"


def _summ_history(h) -> str:
    return f"{h.start.year}-{h.last_year}, annual rain {h.annual_rain_mm():.0f} mm, mean temp {h.annual_mean_temp():.1f} C"


def collect(ctx: dict, emit, simulate_fail: bool = False) -> dict:
    lat, lon, tier = ctx["lat"], ctx["lon"], ctx["tier"]
    emit("Field Data", "start", f"Fetching soil ({'SSURGO' if tier == 'US' else 'SoilGrids'}), terrain, water, ERA5 and 35 years of weather")
    jobs = {"soil": (pv.soil_for, (lat, lon, tier)), "terrain": (pv.terrain, (lat, lon)),
            "water": (pv.nearby_water, (lat, lon)), "era5": (pv.era5, (lat, lon)), "history": (climate.history, (lat, lon))}
    data: dict[str, dict | None] = {k: None for k in jobs}
    sources: dict[str, dict] = {}
    labels = {"soil": "Soil", "terrain": "Terrain (SRTM)", "water": "Nearby water", "era5": "ERA5 climate", "history": "35-yr climate (POWER)"}
    with ThreadPoolExecutor(max_workers=6) as pool:
        nasa_f = pool.submit(nasa_data_agent, ctx, emit, simulate_fail)
        futs = {}
        if simulate_fail:
            for k in jobs:
                sources[labels[k]] = dict(status="failed", ms=0, detail="simulated outage")
            emit("Field Data", "warn", "Simulated outage: skipping all field data sources")
        else:
            futs = {pool.submit(timed, fn, *args): k for k, (fn, args) in jobs.items()}
        for f in as_completed(futs):
            k = futs[f]
            res, err, ms = f.result()
            if err:
                sources[labels[k]] = dict(status="failed", ms=ms, detail=err)
                emit("Field Data", "warn", f"{labels[k]}: {err} ({ms:.0f} ms)")
            else:
                data[k] = res
                detail = _summ_history(res) if k == "history" else _summ(k, res)
                status = "live" if (k == "history" or res.get("available", True)) else "empty"
                sources[labels[k]] = dict(status=status, ms=ms, detail=detail)
                emit("Field Data", "ok" if status == "live" else "warn", f"{labels[k]}: {detail} ({ms:.0f} ms)")
        nasa_out = nasa_f.result()
    sources = {**nasa_out["sources"], **sources}
    return {"nasa": {**nasa_out, "sources": sources}, **data}
