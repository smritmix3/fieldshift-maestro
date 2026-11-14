"""Logic tests for the crop engine using synthetic data (no network)."""
import math
from datetime import date, timedelta

import pytest

from maestro import climate, crops, ecocrop, markets
from maestro.reference import CROPS


# ---------------------------------------------------------------- EcoCrop
CSV = ("EcoPortCode,ScientificName,FAMNAME,LISPA,HABI,CAT,TOPMN,TOPMX,TMIN,TMAX,ROPMN,ROPMX,RMIN,RMAX,PHOPMN,PHOPMX,PHMIN,PHMAX,TEXT,TEXTR,DRA,DRAR,KTMP,GMIN,GMAX,ABITOL,ABISUS,SAL,DEP\n"
       "1,Triticum aestivum,Liliopsida:Gramineae,annual,erect,cereals,15,23,5,27,750,900,300,1600,6,7,5.5,8.5,\"medium, organic\",\"heavy, medium\",well (dry spells),well (dry spells),0,90,250,,,low,medium\n"
       "2,Zea mays,Liliopsida:Gramineae,,,,18,33,10,47,600,1200,400,1800,5,7,4.5,8.5,,,,,,65,365,,,low,medium\n")


def test_parse_keeps_blank_as_none():
    d = ecocrop.parse(CSV, ["Triticum aestivum", "Zea mays", "Nonexistent crop"])
    assert set(d) == {"Triticum aestivum", "Zea mays"}
    assert d["Triticum aestivum"]["TOPMN"] == 15.0 and d["Triticum aestivum"]["TEXT"] == "medium, organic"
    assert d["Zea mays"]["TEXT"] is None and d["Zea mays"]["KTMP"] is None


def test_trapezoid_and_factor_fits():
    assert ecocrop.trapezoid(20, 5, 15, 23, 27) == 1.0
    assert ecocrop.trapezoid(25, 5, 15, 23, 27) == pytest.approx(0.5)
    assert ecocrop.trapezoid(30, 5, 15, 23, 27) == 0.0
    assert ecocrop.trapezoid(None, 5, 15, 23, 27) is None
    wheat = ecocrop.parse(CSV, ["Triticum aestivum"])["Triticum aestivum"]
    assert ecocrop.texture_fit(wheat, "loam") == 1.0          # medium is optimal
    assert ecocrop.texture_fit(wheat, "clay") == 0.6          # heavy only tolerable
    assert ecocrop.texture_fit(wheat, "sand") == 0.2          # light not listed
    assert ecocrop.drainage_fit(wheat, "Well drained") == 1.0
    assert ecocrop.drainage_fit(wheat, "Poorly drained") == 0.2
    maize = ecocrop.parse(CSV, ["Zea mays"])["Zea mays"]
    assert ecocrop.texture_fit(maize, "loam") is None         # no information is never turned into a number
    assert ecocrop.ph_fit(wheat, 6.5) == 1.0 and ecocrop.ph_fit(wheat, None) is None


# ---------------------------------------------------------------- climate simulation on a synthetic year-round climate
def synthetic_raw(lat_mean=20.0, amp=10.0, wet_peak_doy=200, years=6, start_year=2000):
    """Sinusoidal temperature (warmest ~day 200) and a rainy season around day 200."""
    days, t, tmax, tmin, p = [], [], [], [], []
    d = date(start_year, 1, 1)
    end = date(start_year + years - 1, 12, 31)
    while d <= end:
        doy = d.timetuple().tm_yday
        T = lat_mean + amp * math.cos(2 * math.pi * (doy - 200) / 365)
        days.append(d.strftime("%Y%m%d")); t.append(T); tmax.append(T + 6); tmin.append(T - 6)
        p.append(max(0.0, 12 * math.cos(2 * math.pi * (doy - wet_peak_doy) / 365)))
        d += timedelta(days=1)
    return {"start": days[0], "end": days[-1], "T2M": t, "T2M_MAX": tmax, "T2M_MIN": tmin, "PRECTOTCORR": p}


def test_history_basics():
    h = climate.History(synthetic_raw(), lat=20.0)
    assert h.n > 2000 and h.start.year == 2000
    assert 600 < h.annual_rain_mm() < 2500 and 19 < h.annual_mean_temp() < 21
    assert h.window(2000, 1, 100) is not None and h.window(2005, 300, 200) is None   # runs past the data


def test_warm_crop_planted_in_warm_wet_season_and_irrigation_never_hurts():
    h = climate.History(synthetic_raw(), lat=20.0)
    rice = ecocrop.parse(CSV.replace("Zea mays", "Oryza sativa"), ["Oryza sativa"])["Oryza sativa"]
    rice.update(TOPMN=20.0, TOPMX=30.0, TMIN=10.0, TMAX=36.0, GMIN=80.0, GMAX=180.0)
    r = climate.evaluate_crop(h, rice, 1.2, 120)
    assert r["available"] and 100 <= r["start_doy"] <= 260       # not mid-winter
    assert all(i >= w - 1e-9 for i, w in zip(r["irrigated"], r["rainfed"]))
    assert 0 <= min(r["rainfed"]) and max(r["irrigated"]) <= 1.0
    s = r["rainfed_stats"]
    assert s["p10"] <= s["mean"] and s["worst"] <= s["p10"] and s["n"] == len(r["years"])


def test_crop_with_no_temperature_data_is_unavailable_not_guessed():
    h = climate.History(synthetic_raw(), lat=20.0)
    r = climate.evaluate_crop(h, {"GMIN": 90.0, "GMAX": 120.0}, 1.0, None)
    assert r["available"] is False


def test_cycle_length_is_clipped_to_ecocrop():
    assert climate.cycle_length(190, {"GMIN": 150.0, "GMAX": 200.0}) == 190
    assert climate.cycle_length(100, {"GMIN": 150.0, "GMAX": 200.0}) == 150
    assert climate.cycle_length(None, {"GMIN": 100.0, "GMAX": 210.0}) == 155


# ---------------------------------------------------------------- strategy selection with stub analyses
def stub(key, fam, suit, p10, legume_root=None):
    sim = dict(available=True, rainfed_stats=dict(p10=p10, mean=p10 + 0.1, n=35, share_good=0.5, worst=0.1, worst_year=2000),
               irrigated_stats=dict(p10=p10, mean=p10, n=35, share_good=1, worst=0.5, worst_year=2000))
    return dict(ref=CROPS[key], rec={"FAMNAME": fam}, sim=sim, fits={}, suitability=suit, limiting=None)


def test_pick_strategies_rules():
    A = {"wheat": stub("wheat", "Liliopsida:Gramineae", 1.0, 0.3),
         "maize": stub("maize", "Liliopsida:Gramineae", 1.0, 0.9),            # same family as wheat -> not an alternative
         "sorghum": stub("sorghum", "Liliopsida:Gramineae", 1.0, 0.9),
         "cotton": stub("cotton", "Magnoliopsida:Malvaceae", 0.9, 0.7),
         "mustard": stub("mustard", "Magnoliopsida:Cruciferae", 0.2, 0.95),   # unsuitable -> ignored while others fit
         "soybean": stub("soybean", "Magnoliopsida:Leguminosae", 0.9, 0.2),
         "alfalfa": stub("alfalfa", "Magnoliopsida:Fabaceae", 0.95, 0.1)}
    picks = crops.pick_strategies("wheat", A, irrigated=False)
    assert picks["Current Baseline"]["key"] == "wheat"
    assert picks["Climate-Resilient Cover Crop"]["key"] == "cotton"          # best p10 x suitability among suitable, other family
    assert picks["Deep-Root N-Fixer"]["key"] == "alfalfa"                    # both legumes near-tied; alfalfa has deeper roots
    keys = [p["key"] for p in picks.values()]
    assert len(set(keys)) == 3


def test_poor_fit_legume_is_flagged():
    A = {"wheat": stub("wheat", "Liliopsida:Gramineae", 1.0, 0.3),
         "cotton": stub("cotton", "Magnoliopsida:Malvaceae", 0.9, 0.9),
         "alfalfa": stub("alfalfa", "Magnoliopsida:Leguminosae", 0.2, 0.5)}
    picks = crops.pick_strategies("wheat", A, irrigated=False)
    assert picks["Climate-Resilient Cover Crop"]["key"] == "cotton"
    assert "Warning: poor fit" in picks["Deep-Root N-Fixer"]["why"]


def test_fewer_than_three_cards_when_options_run_out():
    A = {"wheat": stub("wheat", "Liliopsida:Gramineae", 1.0, 0.3), "alfalfa": stub("alfalfa", "Magnoliopsida:Leguminosae", 0.9, 0.5)}
    picks = crops.pick_strategies("wheat", A, irrigated=False)
    assert len(picks) == 2 and len({p["key"] for p in picks.values()}) == 2   # never repeats a crop to fill a slot


def test_cover_class_heuristic():
    assert crops.cover_class({"LISPA": "perennial", "CAT": "forage/pasture"})[0] == 0.9
    assert crops.cover_class({"LISPA": "annual", "HABI": "prostrate/procumbent"})[0] == 0.65
    assert crops.cover_class({"LISPA": "annual", "HABI": "erect"})[0] == 0.4


# ---------------------------------------------------------------- markets
def test_futures_conversion_factors():
    wheat = CROPS["wheat"].futures
    assert 593.25 / wheat[1] * wheat[2] == pytest.approx(217.99, rel=1e-3)   # 593.25 US cents/bu -> USD/t
    rice = CROPS["rice"].futures
    assert 18.39 / rice[1] * rice[2] == pytest.approx(405.4, rel=1e-3)       # USD/cwt -> USD/t


def test_parse_owid_and_agmarknet():
    csv_text = "entity,code,year,wheat_yield\nIndia,IND,2020,3.0\nIndia,IND,2021,3.2\nIndia,IND,2022,3.4\nWorld,OWID_WRL,2022,3.5\n"
    o = markets.parse_owid(csv_text, "India")
    assert o["yield_t_ha"] == pytest.approx(3.2) and o["latest_year"] == 2022
    assert markets.parse_owid(csv_text, "Narnia") is None
    payload = {"records": [{"modal_price": "2400", "market": "A", "arrival_date": "10/11/2026"}, {"modal_price": "2500", "market": "B", "arrival_date": "11/11/2026"},
                           {"modal_price": "NA", "market": "C"}]}
    a = markets.parse_agmarknet(payload, usd_inr=85.0)
    assert a["rs_per_quintal"] == 2450 and a["usd_per_t"] == pytest.approx(2450 * 10 / 85.0) and a["markets"] == 2
    assert markets.parse_agmarknet({"records": []}, 85.0) is None


def test_economics_unavailable_when_no_free_source(monkeypatch):
    monkeypatch.setattr(markets, "national_yield", lambda *a, **k: (_ for _ in ()).throw(markets.SourceError("down")))
    e = markets.economics(CROPS["chickpea"], "India", "Punjab")
    assert e["available"] is False and e["missing"]
