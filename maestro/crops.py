"""Crop analysis and strategy selection from real data only.

For every crop in `reference.CROPS`: FAO EcoCrop requirements + live NASA POWER history for the field's cell +
field soil -> climate/soil suitability (EcoCrop's own rule: the minimum of the individual factor fits) and a
35-season stress replay. Then three strategies are picked by explicit, data-driven rules:
  Current Baseline    the crop the farmer grows now
  Climate-Resilient   best worst-decade (10th percentile) season among suitable crops from a different plant family
  Deep-Root N-Fixer   best-suited legume (a different family from the current crop) with the deepest roots among near-ties
"""
from __future__ import annotations

from . import climate, ecocrop
from .reference import CROPS, CropRef

MIN_SUITABLE = 0.4


def family(rec: dict) -> str:
    fam = (rec.get("FAMNAME") or "").split(":")[-1].strip()
    return "Leguminosae" if fam in ("Fabaceae", "Leguminosae") else fam


def is_legume(rec: dict) -> bool:
    return family(rec) == "Leguminosae"


def cover_class(rec: dict) -> tuple[float, str]:
    """Rule of thumb for soil cover from EcoCrop life span, category and growth habit (heuristic, not measured)."""
    cat, life, habit = (rec.get("CAT") or "").lower(), (rec.get("LISPA") or "").lower(), (rec.get("HABI") or "").lower()
    if "perennial" in life and "annual" not in life or "cover crop" in cat:
        return 0.9, "perennial or cover crop"
    if "prostrate" in habit or "climber" in habit:
        return 0.65, "spreading habit"
    return 0.4, "upright annual"


def analyse_all(h: climate.History, soil: dict | None, irrigated: bool) -> dict[str, dict]:
    """key -> dict(ref, rec, sim, fits, suitability). Crops with no EcoCrop record are skipped."""
    eco = ecocrop.load([c.sci for c in CROPS.values()])
    annual_rain = h.annual_rain_mm()
    soil_ok = bool(soil and soil.get("available"))
    out = {}
    for key, ref in CROPS.items():
        rec = eco.get(ref.sci)
        if not rec:
            continue
        sim = climate.evaluate_crop(h, rec, ref.kc_mid, ref.cycle_days)
        fits = {"climate": sim.get("temp_fit_clim") if sim["available"] else None,
                "rainfall": None if irrigated else ecocrop.rain_fit(rec, annual_rain),
                "soil_ph": ecocrop.ph_fit(rec, soil.get("ph")) if soil_ok else None,
                "texture": ecocrop.texture_fit(rec, soil.get("texture")) if soil_ok else None,
                "drainage": ecocrop.drainage_fit(rec, soil.get("drainage")) if soil_ok else None}
        known = {k: v for k, v in fits.items() if v is not None}
        suit = min(known.values()) if known else 0.0           # EcoCrop's limiting-factor rule
        limiting = min(known, key=known.get) if known else None
        out[key] = dict(ref=ref, rec=rec, sim=sim, fits=fits, suitability=suit, limiting=limiting)
    return out


def _stats(a: dict, irrigated: bool) -> dict:
    return a["sim"]["irrigated_stats" if irrigated else "rainfed_stats"] if a["sim"]["available"] else {}


def pick_strategies(current: str, analyses: dict[str, dict], irrigated: bool) -> dict[str, dict]:
    """Return slot -> dict(key, why). Never repeats a crop; warns when nothing suitable exists."""
    cur_fam = family(analyses[current]["rec"]) if current in analyses else None
    others = {k: a for k, a in analyses.items() if k != current and a["sim"]["available"] and family(a["rec"]) != cur_fam}
    picks: dict[str, dict] = {"Current Baseline": dict(key=current, why="The crop you grow now.")}

    suitable = {k: a for k, a in others.items() if a["suitability"] >= MIN_SUITABLE}
    pool = suitable or others
    if pool:
        k = max(pool, key=lambda k: (_stats(pool[k], irrigated).get("p10") or 0) * pool[k]["suitability"])
        s = _stats(pool[k], irrigated)
        picks["Climate-Resilient Cover Crop"] = dict(
            key=k, why=f"Best result in the toughest seasons: its 10th-percentile season index is {s['p10']:.2f} over {s['n']} years "
                       f"and it is from a different plant family than {CROPS[current].name} (breaks pest cycles)." + ("" if suitable else " No crop fits well here; this is the least bad."))
    legumes = {k: a for k, a in others.items() if is_legume(a["rec"]) and k not in {p["key"] for p in picks.values()}}
    if legumes:
        ok = {k: a for k, a in legumes.items() if a["suitability"] >= MIN_SUITABLE} or legumes
        best = max(a["suitability"] for a in ok.values())
        near = [k for k, a in ok.items() if a["suitability"] >= 0.9 * best]  # near-ties: prefer deeper roots (FAO-56)
        k = max(near, key=lambda k: ok[k]["ref"].root_m)
        suit = ok[k]["suitability"]
        why = f"A legume (fixes nitrogen) with roots to about {ok[k]['ref'].root_m:.1f} m."
        why += (" It suits your climate and soil." if suit >= MIN_SUITABLE else
                f" Warning: poor fit here (suitability {suit:.2f}); it is the best legume available, not a good one.")
        picks["Deep-Root N-Fixer"] = dict(key=k, why=why)
    return picks
