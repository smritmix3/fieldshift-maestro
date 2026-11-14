"""Pure-logic tests for geometry validation and provider parsing (no network)."""
import pytest

from maestro import geo, providers as pv


def square(lat, lon, side_m):
    kx, ky = 111320 * __import__("math").cos(__import__("math").radians(lat)), 110574
    dx, dy = side_m / kx, side_m / ky
    return [(lon, lat), (lon + dx, lat), (lon + dx, lat + dy), (lon, lat + dy)]


def test_area_of_100m_square_is_one_hectare():
    f = geo.make_field(square(30.9, 75.85, 100))
    assert f.area_ha == pytest.approx(1.0, rel=0.02)
    assert f.ring[0] == f.ring[-1]


def test_pin_circle_is_about_7_hectares():
    f = geo.field_from_pin(30.9, 75.85)
    assert 6.5 < f.area_ha < 7.5 and f.source == "pin-circle"
    assert f.lat == pytest.approx(30.9, abs=1e-3) and f.lon == pytest.approx(75.85, abs=1e-3)


@pytest.mark.parametrize("ring,msg", [
    ([(0, 0), (1, 1)], "three corners"),
    (square(30.9, 75.85, 5), "very small"),
    (square(30.9, 75.85, 20000), "too large"),
    ([(75.85, 30.9), (75.86, 30.91), (75.86, 30.9), (75.85, 30.91)], "crosses itself"),
    ([("a", "b"), (1, 2), (3, 4)], "not valid"),
    ([(200, 10), (201, 10), (201, 11)], "valid map range"),
])
def test_invalid_fields_give_farmer_readable_errors(ring, msg):
    with pytest.raises(geo.GeometryError, match=msg):
        geo.make_field(ring)


def test_tier_detection():
    assert geo.tier_for(42.0, -93.5) == "US"           # Iowa
    assert geo.tier_for(21.3, -157.8) == "US"          # Hawaii
    assert geo.tier_for(30.9, 75.85) == "ROW"          # Punjab, India
    assert geo.tier_for(30.9, 75.85, "us") == "US"     # explicit country wins
    assert geo.tier_for(42.0, -93.5, "in") == "ROW"


def test_query_params_detect_pin_and_zip():
    assert geo._query_params("141001") == {"postalcode": "141001", "country": "India"}
    assert geo._query_params("50010") == {"postalcode": "50010", "country": "US"}
    assert geo._query_params("Khanna, Punjab") == {"q": "Khanna, Punjab"}


def test_texture_class_known_cases():
    assert pv.texture_class(30.7, 35.9) == "clay loam"   # Punjab sample from live SoilGrids
    assert pv.texture_class(5, 90) == "sand"
    assert pv.texture_class(15, 40) == "loam"
    assert pv.texture_class(50, 20) == "clay"
    assert pv.texture_class(10, 25) == "silt loam"
    assert pv.texture_class(10, 10) == "silt"


def test_parse_soilgrids_converts_units_and_handles_null():
    payload = {"properties": {"layers": [
        {"name": "clay", "unit_measure": {"d_factor": 10}, "depths": [{"values": {"mean": 307}}, {"values": {"mean": 307}}]},
        {"name": "sand", "unit_measure": {"d_factor": 10}, "depths": [{"values": {"mean": 359}}, {"values": {"mean": None}}]},
        {"name": "soc", "unit_measure": {"d_factor": 10}, "depths": [{"values": {"mean": 86}}]},
    ]}}
    s = pv.parse_soilgrids(payload)
    assert s["available"] and s["clay_pct"] == 30.7 and s["sand_pct"] == 35.9 and s["organic_carbon_g_kg"] == 8.6
    nulls = {"properties": {"layers": [{"name": "clay", "unit_measure": {"d_factor": 10}, "depths": [{"values": {"mean": None}}]}]}}
    assert pv.parse_soilgrids(nulls)["available"] is False


def test_parse_ssurgo():
    payload = {"Table": [["muname", "compname", "drainagecl", "hydgrp", "slope_r", "claytotal_r", "sandtotal_r", "om_r", "awc_r"],
                         ["Harps clay loam", "Harps", "Poorly drained", "C/D", "1", "30", "35", "4", "0.18"]]}
    s = pv.parse_ssurgo(payload)
    assert s["available"] and s["drainage"] == "Poorly drained" and s["hydgrp"] == "C/D" and s["texture"] == "clay loam"
    assert pv.parse_ssurgo({"Table": [["muname"]]})["available"] is False


def test_slope_math():
    assert pv.slope_from_elevations(100, 100, 100, 100, 100, 90) == 0
    assert pv.slope_from_elevations(100, 109, 91, 100, 100, 90) == pytest.approx(10.0)  # 18 m over 180 m = 10 %
    assert pv.slope_class(1) == "flat" and pv.slope_class(8) == "moderate" and pv.slope_class(20) == "steep"


def test_parse_overpass_nearest_and_access():
    lat, lon = 30.70, 76.22
    payload = {"elements": [
        {"type": "way", "tags": {"waterway": "drain"}, "geometry": [{"lat": 30.7005, "lon": 76.22}]},          # ~55 m, weak kind
        {"type": "way", "tags": {"waterway": "canal", "name": "Sirhind Canal"}, "geometry": [{"lat": 30.708, "lon": 76.22}]},  # ~890 m
        {"type": "way", "tags": {"highway": "road"}, "geometry": [{"lat": 30.7, "lon": 76.22}]},                 # ignored
    ]}
    w = pv.parse_overpass(payload, lat, lon)
    assert w["nearby"][0]["kind"] == "drain"
    assert w["nearest_irrigation"]["kind"] == "canal" and w["irrigation_access"] == "medium"
    assert pv.parse_overpass({"elements": []}, lat, lon)["irrigation_access"] == "none mapped"


def test_parse_era5():
    payload = {"daily": {"precipitation_sum": [1.0] * 30, "et0_fao_evapotranspiration": [4.0] * 30},
               "hourly": {"soil_moisture_0_to_7cm": [0.2] * 100}}
    e = pv.parse_era5(payload)
    assert e["precip_30d_mm"] == 30.0 and e["et0_mean_mm_day"] == 4.0 and e["soil_moisture_m3m3"] == 0.2
    assert pv.parse_era5({"daily": {"precipitation_sum": []}})["available"] is False


def test_parse_jrc_and_merge_water():
    assert pv.parse_jrc({300: 0, 1000: 0, 2500: 0})["within_m"] is None
    j = pv.parse_jrc({300: 0, 1000: 40, 2500: 80})
    assert j["within_m"] == 1000
    osm = pv.parse_overpass({"elements": [{"type": "way", "tags": {"waterway": "canal"}, "geometry": [{"lat": 30.7005, "lon": 76.22}]}]}, 30.70, 76.22)
    m = pv.merge_water(osm, j)
    assert m["irrigation_access"] == "high" and m["nearest_distance_m"] < 100        # canal 55 m beats satellite 1000 m
    only_sat = pv.merge_water(None, j)
    assert only_sat["irrigation_access"] == "medium" and "OpenStreetMap was unreachable" in only_sat["note"]
    none = pv.merge_water({"nearest_irrigation": None, "nearby": [], "source": "osm"}, pv.parse_jrc({300: 0, 1000: 0, 2500: 0}))
    assert none["irrigation_access"] == "none found"
    assert pv.merge_water(None, None)["irrigation_access"] == "unknown"
