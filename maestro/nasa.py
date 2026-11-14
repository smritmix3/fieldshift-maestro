"""Live NASA clients (no API key needed): POWER daily point API and CMR granule search."""
from __future__ import annotations

import time
from datetime import date, datetime, timedelta, timezone

import httpx

from . import cache

POWER_URL = "https://power.larc.nasa.gov/api/temporal/daily/point"
CMR_URL = "https://cmr.earthdata.nasa.gov/search/granules.json"
POWER_PARAMS = ["PRECTOTCORR", "T2M", "T2M_MAX", "T2M_MIN", "GWETROOT", "GWETTOP"]

# label -> CMR short_name
CMR_COLLECTIONS = {
    "SMAP soil moisture": "SPL3SMP_E",
    "ECOSTRESS land temp": "ECO_L2T_LSTE",
    "GPM IMERG rain": "GPM_3IMERGDL",
    "HLS Landsat": "HLSL30",
}


from .http import SourceError as NasaError  # noqa: E402  (one error type for every external source)


def _get(url: str, params: dict, timeout: float) -> dict:
    try:
        r = httpx.get(url, params=params, timeout=timeout)
    except httpx.HTTPError as e:
        raise NasaError(f"{type(e).__name__}: {e}") from e
    if r.status_code == 429:
        raise NasaError("rate limited (HTTP 429)")
    if r.status_code != 200:
        raise NasaError(f"HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError as e:
        raise NasaError("invalid JSON") from e


def fetch_power(lat: float, lon: float, years_back: int = 3, timeout: float = 20.0) -> dict:
    """Cached 3 h per location (rounded to ~100 m). Returns {param: {date: float}} with -999 fill removed."""
    key = f"nasa_power:{round(lat, 3)}:{round(lon, 3)}:{years_back}"
    hit = cache.get(key, 3 * 3600)
    if hit is not None:
        return {p: {date.fromisoformat(k): v for k, v in series.items()} for p, series in hit.items()}
    out = _fetch_power_uncached(lat, lon, years_back, timeout)
    cache.put(key, {p: {k.isoformat(): v for k, v in series.items()} for p, series in out.items()})
    return out


def _fetch_power_uncached(lat: float, lon: float, years_back: int = 3, timeout: float = 20.0) -> dict:
    """One POWER call covering recent weather AND a few prior years (used as a climatology).

    Returns {param: {date(): float}} with -999 fill values removed.
    """
    end = date.today() - timedelta(days=1)
    start = date(end.year - years_back, 1, 1)
    data = _get(POWER_URL, {
        "parameters": ",".join(POWER_PARAMS), "community": "AG",
        "latitude": lat, "longitude": lon,
        "start": start.strftime("%Y%m%d"), "end": end.strftime("%Y%m%d"), "format": "JSON",
    }, timeout)
    try:
        raw = data["properties"]["parameter"]
    except KeyError as e:
        raise NasaError("unexpected POWER payload") from e
    out: dict[str, dict[date, float]] = {}
    for p, series in raw.items():
        out[p] = {datetime.strptime(k, "%Y%m%d").date(): v for k, v in series.items() if v > -900}
    return out


def fetch_cmr_latest(short_name: str, lat: float, lon: float, timeout: float = 15.0) -> dict:
    """Cached 1 h. Age is recomputed from the granule time on every call, so cached results never look fresher than they are."""
    key = f"nasa_cmr:{short_name}:{round(lat, 2)}:{round(lon, 2)}"
    hit = cache.get(key, 3600)
    if hit is None:
        t = _fetch_cmr_time(short_name, lat, lon, timeout)
        cache.put(key, t.isoformat())
    else:
        t = datetime.fromisoformat(hit)
    return {"time": t, "age_days": (datetime.now(timezone.utc) - t).total_seconds() / 86400}


def _fetch_cmr_time(short_name: str, lat: float, lon: float, timeout: float = 15.0) -> datetime:
    """Latest granule for a collection over the point: {'time': datetime, 'age_days': float}."""
    data = _get(CMR_URL, {"short_name": short_name, "point": f"{lon},{lat}",
                          "sort_key": "-start_date", "page_size": 1}, timeout)
    entries = data.get("feed", {}).get("entry", [])
    if not entries:
        raise NasaError("no granules over this point")
    return datetime.fromisoformat(entries[0]["time_start"].replace("Z", "+00:00"))


def timed(fn, *args, **kwargs):
    """Run fn, return (result_or_None, error_or_None, elapsed_ms)."""
    t0 = time.perf_counter()
    try:
        return fn(*args, **kwargs), None, (time.perf_counter() - t0) * 1000
    except NasaError as e:
        return None, str(e), (time.perf_counter() - t0) * 1000
