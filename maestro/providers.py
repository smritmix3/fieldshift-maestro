"""Field-level data providers (all free, no key): soil, terrain, nearby water, ERA5.

Each provider returns a plain dict that always includes `source`, `provenance` ('live' or 'modeled'),
`resolution`, and `available` (False when the source had nothing for this point), so the scoring and UI
code never needs to know which region or provider produced it. Failures raise SourceError.
"""
from __future__ import annotations

import math
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, as_completed
from datetime import date, timedelta

from . import cache
from .http import SourceError, get_json

# ============================================================================ soil
def texture_class(clay: float, sand: float) -> str:
    """USDA soil texture class from clay and sand percent (silt = remainder); standard triangle rules."""
    silt = 100 - clay - sand
    if silt + 1.5 * clay < 15:
        return "sand"
    if silt + 1.5 * clay >= 15 and silt + 2 * clay < 30:
        return "loamy sand"
    if (7 <= clay < 20 and sand > 52 and silt + 2 * clay >= 30) or (clay < 7 and silt < 50 and silt + 2 * clay >= 30):
        return "sandy loam"
    if 7 <= clay < 27 and 28 <= silt < 50 and sand <= 52:
        return "loam"
    if (silt >= 50 and 12 <= clay < 27) or (50 <= silt < 80 and clay < 12):
        return "silt loam"
    if silt >= 80 and clay < 12:
        return "silt"
    if 20 <= clay < 35 and silt < 28 and sand > 45:
        return "sandy clay loam"
    if 27 <= clay < 40 and 20 < sand <= 45:
        return "clay loam"
    if 27 <= clay < 40 and sand <= 20:
        return "silty clay loam"
    if clay >= 35 and sand > 45:
        return "sandy clay"
    if clay >= 40 and silt >= 40:
        return "silty clay"
    if clay >= 40 and sand <= 45 and silt < 40:
        return "clay"
    return "loam"


def parse_soilgrids(payload: dict) -> dict:
    """Average the available depth layers per property; values are converted with SoilGrids' d_factor."""
    props: dict[str, list[float]] = {}
    for layer in payload.get("properties", {}).get("layers", []):
        d = (layer.get("unit_measure") or {}).get("d_factor") or 1
        for depth in layer.get("depths", []):
            v = (depth.get("values") or {}).get("mean")
            if v is not None:
                props.setdefault(layer["name"], []).append(v / d)
    mean = {k: sum(v) / len(v) for k, v in props.items()}
    base = dict(source="ISRIC SoilGrids", provenance="modeled", resolution="250 m grid", drainage=None, hydgrp=None)
    if "clay" not in mean or "sand" not in mean:
        return dict(base, available=False, note="No SoilGrids value for this point (often built-up land or water).")
    clay, sand = mean["clay"], mean["sand"]
    return dict(base, available=True, clay_pct=round(clay, 1), sand_pct=round(sand, 1), silt_pct=round(100 - clay - sand, 1),
                organic_carbon_g_kg=round(mean["soc"], 1) if "soc" in mean else None,
                ph=round(mean["phh2o"], 1) if "phh2o" in mean else None,
                texture=texture_class(clay, sand), note="Soil depth layers 5-30 cm, modeled from global samples; verify with a local soil test.")


SOILGRIDS_TIMEOUT_S = 12.0
_soilgrids_down_until = [0.0]
BUILT_UP_NOTE = ("No SoilGrids value at this exact spot: it is usually built-up land or water. "
                 "Click your actual field on the map to get soil data.")


@cache.cached("soilgrids", 30 * 86400)   # successful answers are cached, including a legitimate "no data here"
def soilgrids(lat: float, lon: float) -> dict:
    """SoilGrids at the point only. ISRIC's server is slow (3-6 s, sometimes more) and queues concurrent requests, so we make
    one request, never a fan-out of neighbour lookups. A timeout or error raises (not cached) and the server is skipped for
    3 minutes so one hiccup cannot keep delaying later analyses."""
    now = time.monotonic()
    if now < _soilgrids_down_until[0]:
        raise SourceError(f"SoilGrids recently slow (retrying in {_soilgrids_down_until[0] - now:.0f}s)")
    params = [("lon", lon), ("lat", lat), ("property", "clay"), ("property", "sand"), ("property", "soc"),
              ("property", "phh2o"), ("depth", "5-15cm"), ("depth", "15-30cm"), ("value", "mean")]
    try:
        data = get_json("https://rest.isric.org/soilgrids/v2.0/properties/query", params, timeout=SOILGRIDS_TIMEOUT_S)
    except SourceError:
        _soilgrids_down_until[0] = time.monotonic() + 180.0
        raise
    r = parse_soilgrids(data)
    if not r["available"]:
        r["note"] = BUILT_UP_NOTE
    return r


def parse_ssurgo(payload: dict) -> dict:
    rows = payload.get("Table") or []
    base = dict(source="USDA SSURGO", provenance="live", resolution="mapped soil polygon")
    if len(rows) < 2:
        return dict(base, available=False, note="No SSURGO map unit at this point.")
    r = dict(zip(rows[0], rows[1]))
    f = lambda k: float(r[k]) if r.get(k) not in (None, "") else None  # noqa: E731
    clay, sand = f("claytotal_r"), f("sandtotal_r")
    out = dict(base, available=True, map_unit=r.get("muname"), component=r.get("compname"), drainage=r.get("drainagecl"),
               hydgrp=r.get("hydgrp"), slope_pct_mapped=f("slope_r"),
               organic_carbon_g_kg=round(f("om_r") * 5.8, 1) if f("om_r") is not None else None,  # organic matter % -> organic carbon g/kg (OC = 0.58 x OM)
               awc=f("awc_r"), note="Official USDA survey values for the dominant soil component.")
    if clay is not None and sand is not None:
        out.update(clay_pct=clay, sand_pct=sand, silt_pct=round(100 - clay - sand, 1), texture=texture_class(clay, sand))
    return out


@cache.cached("ssurgo", 30 * 86400)
def ssurgo(lat: float, lon: float) -> dict:
    sql = ("SELECT TOP 1 mu.muname, c.compname, c.drainagecl, c.hydgrp, c.slope_r, h.claytotal_r, h.sandtotal_r, h.om_r, h.awc_r "
           "FROM mapunit mu INNER JOIN component c ON c.mukey=mu.mukey LEFT JOIN chorizon h ON h.cokey=c.cokey AND h.hzdept_r=0 "
           f"WHERE mu.mukey IN (SELECT * FROM SDA_Get_Mukey_from_intersection_with_WktWgs84('POINT({lon} {lat})')) "
           "ORDER BY c.comppct_r DESC")
    data = get_json("https://sdmdataaccess.sc.egov.usda.gov/Tabular/post.rest", method="POST",
                    json_body={"format": "JSON+COLUMNNAME", "query": sql}, timeout=25)
    return parse_ssurgo(data)


def soil_for(lat: float, lon: float, tier: str) -> dict:
    """Best soil source for the region. US tries SSURGO then falls back to SoilGrids."""
    if tier == "US":
        try:
            s = ssurgo(lat, lon)
            if s["available"]:
                return s
        except SourceError:
            pass
    return soilgrids(lat, lon)


# ============================================================================ terrain
def slope_from_elevations(center: float, east: float, west: float, north: float, south: float, d_m: float) -> float:
    """Slope percent from 4 neighbours at distance d_m (central differences)."""
    dzdx, dzdy = (east - west) / (2 * d_m), (north - south) / (2 * d_m)
    return math.hypot(dzdx, dzdy) * 100


def slope_class(pct: float) -> str:
    return "flat" if pct < 2 else "gentle" if pct < 6 else "moderate" if pct < 12 else "steep"


@cache.cached("terrain", 90 * 86400)
def terrain(lat: float, lon: float, d_m: float = 90.0) -> dict:
    """SRTM 30 m elevation at the point and slope from 4 neighbours (3 pixels away to reduce noise)."""
    dlat = d_m / 110_574.0
    dlon = d_m / (111_320.0 * math.cos(math.radians(lat)))
    pts = [(lat, lon), (lat, lon + dlon), (lat, lon - dlon), (lat + dlat, lon), (lat - dlat, lon)]
    data = get_json("https://api.opentopodata.org/v1/srtm30m", {"locations": "|".join(f"{a},{b}" for a, b in pts)}, timeout=15)
    z = [r.get("elevation") for r in data.get("results", [])]
    base = dict(source="NASA SRTM (via OpenTopoData)", provenance="live", resolution="30 m")
    if len(z) != 5 or any(v is None for v in z):
        return dict(base, available=False, note="No SRTM elevation here (water, void or outside coverage).")
    pct = slope_from_elevations(*z, d_m)
    return dict(base, available=True, elevation_m=round(z[0]), slope_pct=round(pct, 1), slope_class=slope_class(pct),
                note="Slope from 5 SRTM samples around the field centre.")


# ============================================================================ nearby water (OpenStreetMap)
_osm_down_until = [0.0]
OVERPASS_MIRRORS = ["https://overpass-api.de/api/interpreter", "https://overpass.private.coffee/api/interpreter"]
IRRIGATION_KINDS = {"river", "canal", "reservoir", "lake", "pond", "well"}  # drains/streams/ditches carry weaker access


def haversine_m(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    return 2 * 6_371_000 * math.asin(math.sqrt(a))


def _kind(tags: dict) -> str | None:
    w, n, m = tags.get("waterway"), tags.get("natural"), tags.get("man_made")
    if w in ("river", "canal", "stream", "drain", "ditch"):
        return w
    if n == "water":
        return {"reservoir": "reservoir", "pond": "pond", "lake": "lake"}.get(tags.get("water"), "lake")
    if m in ("water_well", "water_tower", "pumping_station"):
        return "well"
    return None


def parse_overpass(payload: dict, lat: float, lon: float) -> dict:
    found = []
    for el in payload.get("elements", []):
        kind = _kind(el.get("tags", {}))
        if not kind:
            continue
        pts = [(g["lat"], g["lon"]) for g in el.get("geometry", [])] or ([(el["lat"], el["lon"])] if "lat" in el else [])
        if not pts:
            continue
        dist = min(haversine_m(lat, lon, a, b) for a, b in pts)
        found.append(dict(kind=kind, name=el["tags"].get("name"), distance_m=round(dist)))
    found.sort(key=lambda x: x["distance_m"])
    nearest_irrigation = next((f for f in found if f["kind"] in IRRIGATION_KINDS), None)
    access = ("none mapped" if not nearest_irrigation else "high" if nearest_irrigation["distance_m"] <= 500
              else "medium" if nearest_irrigation["distance_m"] <= 1500 else "low")
    return dict(available=True, nearby=found[:6], nearest_irrigation=nearest_irrigation, irrigation_access=access,
                source="OpenStreetMap (Overpass)", provenance="live", resolution="exact distance",
                note="Mapped surface water within 3 km. Missing features mean 'none mapped', not 'no water'. Groundwater depth is not available.")


def _osm_query(lat: float, lon: float, radius_m: int) -> str:
    return (f"[out:json][timeout:10];("
            f'way(around:{radius_m},{lat},{lon})[waterway~"^(river|canal|stream|drain|ditch)$"];'
            f"way(around:{radius_m},{lat},{lon})[natural=water];"
            f'node(around:{radius_m},{lat},{lon})[man_made~"^(water_well|water_tower|pumping_station)$"];'
            f");out geom qt 60;")


@cache.cached("osm_water", 7 * 86400)
def osm_water(lat: float, lon: float, radius_m: int = 3000, deadline_s: float = 5.0) -> dict:
    """Race all Overpass mirrors; first success wins; hard overall deadline (public servers are often at capacity)."""
    if time.monotonic() < _osm_down_until[0]:  # circuit breaker: don't pay the timeout again right after a failure
        raise SourceError(f"OpenStreetMap water recently unavailable (retrying in {_osm_down_until[0] - time.monotonic():.0f}s)")
    q = _osm_query(lat, lon, radius_m)
    pool = ThreadPoolExecutor(max_workers=len(OVERPASS_MIRRORS))
    futs = [pool.submit(get_json, u, None, deadline_s, "POST", None, {"data": q}) for u in OVERPASS_MIRRORS]
    last = "no mirror responded in time"
    try:
        for f in as_completed(futs, timeout=deadline_s + 1):
            try:
                return parse_overpass(f.result(), lat, lon)
            except SourceError as e:
                last = str(e)
    except FuturesTimeout:
        pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    _osm_down_until[0] = time.monotonic() + 300.0
    raise SourceError(f"OpenStreetMap water unavailable ({last})")


_PC = "https://planetarycomputer.microsoft.com/api"
JRC_RINGS_M = (300, 1000, 2500)


@cache.cached("jrc_water", 30 * 86400)
def jrc_water(lat: float, lon: float) -> dict:
    """Satellite-detected surface water (JRC Global Surface Water, 30 m, 1984-2020) within nested boxes around the field.
    Sees rivers, lakes, reservoirs and wide canals; small drains and ponds are usually too small to detect."""
    search = get_json(f"{_PC}/stac/v1/search", method="POST", timeout=15,
                      json_body={"collections": ["jrc-gsw"], "intersects": {"type": "Point", "coordinates": [lon, lat]}, "limit": 1})
    feats = search.get("features", [])
    base = dict(source="JRC Global Surface Water (Landsat, via Planetary Computer)", provenance="live", resolution="30 m")
    if not feats:
        return dict(base, available=False, within_m=None, note="No JRC coverage here.")
    item = feats[0]["id"]

    def box_max(r_m: int) -> float:
        dlat, dlon = r_m / 110_574.0, r_m / (111_320.0 * math.cos(math.radians(lat)))
        ring = [[lon - dlon, lat - dlat], [lon + dlon, lat - dlat], [lon + dlon, lat + dlat], [lon - dlon, lat + dlat], [lon - dlon, lat - dlat]]
        d = get_json(f"{_PC}/data/v1/item/statistics", {"collection": "jrc-gsw", "item": item, "assets": "occurrence"}, 20, "POST",
                     {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [ring]}})
        try:
            return d["properties"]["statistics"]["occurrence_b1"]["max"]
        except (KeyError, TypeError) as e:
            raise SourceError("unexpected JRC payload") from e

    with ThreadPoolExecutor(max_workers=len(JRC_RINGS_M)) as pool:
        maxima = dict(zip(JRC_RINGS_M, pool.map(box_max, JRC_RINGS_M)))
    return parse_jrc(maxima, base)


def parse_jrc(maxima: dict, base: dict | None = None) -> dict:
    """maxima: {box half-side in m: max water-occurrence % inside the box}. Water counts at >= 10 % occurrence."""
    base = base or dict(source="JRC Global Surface Water", provenance="live", resolution="30 m")
    within = next((r for r in sorted(maxima) if maxima[r] >= 10), None)
    return dict(base, available=True, within_m=within, max_occurrence_pct=max(maxima.values()),
                note="Water seen by Landsat in at least 10% of 1984-2020 observations within this distance (upper bound). Small drains and ponds are not detectable.")


def nearby_water(lat: float, lon: float) -> dict:
    """OpenStreetMap + JRC satellite water in parallel; either may fail. Raises only if both do."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        fo, fj = pool.submit(osm_water, lat, lon), pool.submit(jrc_water, lat, lon)
    osm = jrc = None
    errs: list[str] = []
    for name, f in (("OpenStreetMap", fo), ("JRC", fj)):
        try:
            v = f.result()
        except SourceError as e:
            errs.append(f"{name}: {e}")
            continue
        if name == "OpenStreetMap":
            osm = v
        else:
            jrc = v
    if not osm and not jrc:
        raise SourceError("; ".join(errs))
    return merge_water(osm, jrc, errs)


def merge_water(osm: dict | None, jrc: dict | None, errs: list[str] | None = None) -> dict:
    dists = []
    if osm and osm.get("nearest_irrigation"):
        dists.append(osm["nearest_irrigation"]["distance_m"])
    if jrc and jrc.get("within_m"):
        dists.append(jrc["within_m"])
    if dists:
        d = min(dists)
        access = "high" if d <= 500 else "medium" if d <= 1500 else "low"
    else:
        access = "none found" if (osm or jrc) else "unknown"
    used = [x["source"] for x in (osm, jrc) if x]
    notes = ["Groundwater depth is not available (no free source)."]
    if not osm:
        notes.append("OpenStreetMap was unreachable, so canals and small drains may be missing.")
    if not jrc:
        notes.append("Satellite water check was unreachable.")
    return dict(available=True, irrigation_access=access, nearest_distance_m=min(dists) if dists else None,
                nearby=(osm or {}).get("nearby", []), osm=osm, satellite=jrc, source=" + ".join(used), provenance="live",
                resolution="mapped features + 30 m satellite", errors=errs or [], note=" ".join(notes))


# ============================================================================ ERA5 via Open-Meteo
def parse_era5(payload: dict) -> dict:
    d = payload.get("daily", {})
    rain = [v for v in d.get("precipitation_sum", []) if v is not None]
    et0 = [v for v in d.get("et0_fao_evapotranspiration", []) if v is not None]
    sm = [v for v in payload.get("hourly", {}).get("soil_moisture_0_to_7cm", []) if v is not None]
    base = dict(source="ECMWF ERA5 (via Open-Meteo)", provenance="modeled", resolution="about 10-25 km")
    if not rain:
        return dict(base, available=False, note="No ERA5 data returned.")
    return dict(base, available=True, precip_30d_mm=round(sum(rain[-30:]), 1), et0_mean_mm_day=round(sum(et0[-30:]) / max(len(et0[-30:]), 1), 2),
                soil_moisture_m3m3=round(sum(sm[-72:]) / len(sm[-72:]), 3) if sm else None,
                note="Reanalysis (model + observations); about 5 days behind real time.")


@cache.cached("era5", 6 * 3600)
def era5(lat: float, lon: float) -> dict:
    end = date.today() - timedelta(days=6)  # ERA5 lags real time
    start = end - timedelta(days=29)
    data = get_json("https://archive-api.open-meteo.com/v1/archive", {
        "latitude": lat, "longitude": lon, "start_date": start.isoformat(), "end_date": end.isoformat(),
        "daily": "precipitation_sum,et0_fao_evapotranspiration", "hourly": "soil_moisture_0_to_7cm", "models": "era5"}, timeout=20)
    return parse_era5(data)
