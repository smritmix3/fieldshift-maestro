"""Location handling: geocoding (address / PIN / ZIP), field geometry, validation, region tier."""
from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass

from . import cache
from .http import SourceError, get_json

MIN_HA, MAX_HA = 0.05, 1000.0
DEFAULT_RADIUS_M = 150.0
_nominatim_lock = threading.Lock()
_last_nominatim = [0.0]


class GeometryError(ValueError):
    """The drawn field is unusable; the message is safe to show to the farmer."""


@dataclass
class Place:
    name: str
    lat: float
    lon: float
    country_code: str | None
    country: str | None = None   # as named by OpenStreetMap, e.g. "India", "United States"
    state: str | None = None


@dataclass
class Field:
    ring: list[tuple[float, float]]  # closed ring of (lon, lat)
    lat: float                       # centroid
    lon: float
    area_ha: float
    source: str                      # "drawn" | "pin-circle"
    country_code: str | None = None

    @property
    def tier(self) -> str:
        return tier_for(self.lat, self.lon, self.country_code)

    def geojson(self) -> dict:
        return {"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in self.ring]]}}


# ------------------------------------------------------------------ geometry
def _to_xy(ring, lat0: float):
    kx = 111_320.0 * math.cos(math.radians(lat0))
    ky = 110_574.0
    lon0 = ring[0][0]
    return [((lon - lon0) * kx, (lat - lat0) * ky) for lon, lat in ring]


def area_ha(ring: list[tuple[float, float]]) -> float:
    """Planar area on a local equirectangular projection (accurate to ~1% for fields up to a few km)."""
    lat0 = sum(p[1] for p in ring) / len(ring)
    xy = _to_xy(ring, lat0)
    s = sum(xy[i][0] * xy[(i + 1) % len(xy)][1] - xy[(i + 1) % len(xy)][0] * xy[i][1] for i in range(len(xy)))
    return abs(s) / 2 / 10_000


def centroid(ring: list[tuple[float, float]]) -> tuple[float, float]:
    """(lat, lon) of the vertex mean: robust for the small convex-ish fields we handle."""
    pts = ring[:-1] if ring[0] == ring[-1] else ring
    return sum(p[1] for p in pts) / len(pts), sum(p[0] for p in pts) / len(pts)


def circle_ring(lat: float, lon: float, radius_m: float = DEFAULT_RADIUS_M, n: int = 36) -> list[tuple[float, float]]:
    kx, ky = 111_320.0 * math.cos(math.radians(lat)), 110_574.0
    ring = [(lon + radius_m * math.cos(2 * math.pi * i / n) / kx, lat + radius_m * math.sin(2 * math.pi * i / n) / ky) for i in range(n)]
    return ring + [ring[0]]


def _segments_cross(a, b, c, d) -> bool:
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    o1, o2, o3, o4 = orient(a, b, c), orient(a, b, d), orient(c, d, a), orient(c, d, b)
    return (o1 * o2 < 0) and (o3 * o4 < 0)


def make_field(ring_in: list, source: str = "drawn", country_code: str | None = None) -> Field:
    """Validate a ring of (lon, lat) and build a Field. Raises GeometryError with a farmer-readable message."""
    try:
        ring = [(float(p[0]), float(p[1])) for p in ring_in]
    except (TypeError, ValueError, IndexError) as e:
        raise GeometryError("The field outline is not valid. Please draw it again.") from e
    if len(ring) < 3:
        raise GeometryError("Draw at least three corners for the field.")
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    if len(ring) > 500:
        raise GeometryError("The outline has too many points. Please draw a simpler shape.")
    if any(not (math.isfinite(x) and math.isfinite(y) and -180 <= x <= 180 and -85 <= y <= 85) for x, y in ring):
        raise GeometryError("The outline is outside the valid map range.")
    n = len(ring) - 1
    for i in range(n):  # self-intersection check (O(n^2), n is small)
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            if _segments_cross(ring[i], ring[i + 1], ring[j], ring[j + 1]):
                raise GeometryError("The outline crosses itself. Please redraw it without crossing lines.")
    a = area_ha(ring)
    if a < MIN_HA:
        raise GeometryError(f"That field is very small ({a:.3f} ha). Draw at least {MIN_HA} ha or use the pin instead.")
    if a > MAX_HA:
        raise GeometryError(f"That area is {a:,.0f} ha, too large for a single field (max {MAX_HA:,.0f} ha). Draw one field.")
    lat, lon = centroid(ring)
    return Field(ring, lat, lon, a, source, country_code)


def field_from_pin(lat: float, lon: float, radius_m: float = DEFAULT_RADIUS_M, country_code: str | None = None) -> Field:
    return make_field(circle_ring(lat, lon, radius_m), "pin-circle", country_code)


# ------------------------------------------------------------------ region tier
def tier_for(lat: float, lon: float, country_code: str | None = None) -> str:
    """'US' uses SSURGO/NWIS-style sources; 'ROW' (rest of world, incl. India) uses SoilGrids + OSM."""
    if country_code:
        return "US" if country_code.lower() == "us" else "ROW"
    in_conus = 24.0 <= lat <= 50.0 and -125.0 <= lon <= -66.0
    in_ak = 51.0 <= lat <= 72.0 and -170.0 <= lon <= -129.0
    in_hi = 18.5 <= lat <= 22.5 and -161.0 <= lon <= -154.0
    return "US" if (in_conus or in_ak or in_hi) else "ROW"


# ------------------------------------------------------------------ geocoding
def _query_params(q: str) -> dict:
    q = q.strip()
    if re.fullmatch(r"\d{6}", q):
        return {"postalcode": q, "country": "India"}
    if re.fullmatch(r"\d{5}(-\d{4})?", q):
        return {"postalcode": q[:5], "country": "US"}
    return {"q": q}


@cache.cached("geocode", 7 * 86400)
def _geocode_raw(q: str) -> list[dict]:
    with _nominatim_lock:  # Nominatim policy: max ~1 request/second
        wait = 1.1 - (time.monotonic() - _last_nominatim[0])
        if wait > 0:
            time.sleep(wait)
        try:
            data = get_json("https://nominatim.openstreetmap.org/search",
                            {**_query_params(q), "format": "jsonv2", "limit": 5, "addressdetails": 1}, timeout=10)
        finally:
            _last_nominatim[0] = time.monotonic()
    return [{"name": d["display_name"], "lat": float(d["lat"]), "lon": float(d["lon"]),
             "cc": (d.get("address") or {}).get("country_code"), "country": (d.get("address") or {}).get("country"),
             "state": (d.get("address") or {}).get("state")} for d in data]


def _geocode_openmeteo(q: str) -> list[dict]:
    data = get_json("https://geocoding-api.open-meteo.com/v1/search", {"name": q, "count": 5, "language": "en", "format": "json"}, timeout=8)
    return [{"name": ", ".join(x for x in (r.get("name"), r.get("admin1"), r.get("country")) if x),
             "lat": r["latitude"], "lon": r["longitude"], "cc": (r.get("country_code") or "").lower() or None}
            for r in data.get("results", [])]


def geocode(query: str) -> list[Place]:
    """Address, village, PIN or ZIP -> up to 5 places. Raises SourceError if every provider fails."""
    query = (query or "").strip()
    if len(query) < 3:
        raise SourceError("Type at least 3 characters")
    try:
        rows = _geocode_raw(query)
    except SourceError:
        if re.fullmatch(r"[\d\-]+", query):  # PIN/ZIP: Open-Meteo cannot resolve these
            raise
        rows = _geocode_openmeteo(query)
    return [Place(r["name"], r["lat"], r["lon"], r.get("cc"), r.get("country"), r.get("state")) for r in rows]


@cache.cached("reverse", 30 * 86400)
def _reverse_raw(lat: float, lon: float) -> dict:
    with _nominatim_lock:
        wait = 1.1 - (time.monotonic() - _last_nominatim[0])
        if wait > 0:
            time.sleep(wait)
        try:
            d = get_json("https://nominatim.openstreetmap.org/reverse", {"lat": lat, "lon": lon, "format": "jsonv2", "zoom": 10, "addressdetails": 1}, timeout=10)
        finally:
            _last_nominatim[0] = time.monotonic()
    a = d.get("address") or {}
    return {"cc": a.get("country_code"), "country": a.get("country"), "state": a.get("state")}


def reverse(lat: float, lon: float) -> dict:
    """Country code, country and state for a point (used for local prices/yields). Empty values if unavailable."""
    try:
        return _reverse_raw(lat, lon)
    except SourceError:
        return {"cc": None, "country": None, "state": None}
