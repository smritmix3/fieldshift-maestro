"""NASA GIBS map overlays (WMTS, EPSG:3857). Layer ids, tile levels and latest-date lags verified 2026-11-14.
Dates lag real time; `lag_days` is chosen so the tile for that date exists."""
from __future__ import annotations

from datetime import date, timedelta

BASE = "https://gibs.earthdata.nasa.gov/wmts/epsg3857/best"

# key -> (label, layer id, max native zoom, lag in days, note)
LAYERS: dict[str, tuple[str, str, int, int, str]] = {
    "ndvi": ("Vegetation greenness (MODIS NDVI, 8-day)", "MODIS_Terra_NDVI_8Day", 9, 4, "Greener = more vigorous plants. About 250 m."),
    "smap": ("Soil moisture (SMAP L3, satellite swaths)", "SMAP_L3_Passive_Enhanced_Day_Soil_Moisture", 6, 5, "Wetter = bluer. About 9 km, gaps between passes."),
    "rain": ("Rainfall rate (GPM IMERG)", "IMERG_Precipitation_Rate", 6, 3, "About 10 km, last day."),
    "lst": ("Land surface temperature (MODIS, daily)", "MODIS_Terra_L3_Land_Surface_Temp_Daily_Day", 7, 4, "Hotter = redder. About 1 km."),
}


def tile_url(key: str, today: date | None = None) -> tuple[str, int, str]:
    """(leaflet url template, max native zoom, date used)."""
    _, layer, zmax, lag, _ = LAYERS[key]
    d = ((today or date.today()) - timedelta(days=lag)).isoformat()
    return f"{BASE}/{layer}/default/{d}/GoogleMapsCompatible_Level{zmax}/{{z}}/{{y}}/{{x}}.png", zmax, d
