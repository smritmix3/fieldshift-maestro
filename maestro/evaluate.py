"""Crop agent, evaluator and synthesis: strategies scored from real inputs only.

Metrics (0-1, higher is better):
  Suitability   EcoCrop limiting-factor fit to the field's climate (live NASA POWER history) and soil
  Water         share of the crop's water need met by rain over its cycle, averaged over 35 real seasons
  Soil          nitrogen fixation (legumes), rotation break (different plant family), rooting depth
  Erosion       protection from ground cover vs. slope (SRTM) and rainfall (POWER)
  Economics     gross revenue per ha (live price x national yield), relative to the other options.
                Only when at least two options have both a live price and a yield; otherwise omitted, never guessed.
Weights by farmer priority are renormalised over the metrics that exist.
"""
from __future__ import annotations

import time

from . import crops as cr
from . import markets
from .reference import CROPS

WEIGHTS = {  # Suitability, Water, Soil, Erosion, Economics
    "Water": dict(Suitability=0.25, Water=0.35, Soil=0.15, Erosion=0.10, Economics=0.15),
    "Soil": dict(Suitability=0.25, Water=0.15, Soil=0.35, Erosion=0.15, Economics=0.10),
    "Income": dict(Suitability=0.25, Water=0.10, Soil=0.15, Erosion=0.10, Economics=0.40),
}
PROV = {"live": 1.0, "modeled": 0.7, "reference": 0.8, "heuristic": 0.5, "missing": 0.2}
SLOPE_FULL_RISK_PCT, RAIN_FULL_RISK_MM = 12.0, 1200.0   # method parameters for the erosion rule of thumb


def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, x))


def crop_agent(ctx: dict, snap: dict, emit) -> dict:
    emit("Crop Engine", "start", "Scoring 13 crops with FAO EcoCrop limits, 35 years of NASA POWER weather and your soil")
    t0 = time.perf_counter()
    h = snap.get("history")
    if h is None:
        raise RuntimeError("climate history unavailable: cannot assess crops (no data is invented)")
    analyses = cr.analyse_all(h, snap.get("soil"), ctx["irrigated"])
    if ctx["crop"] not in analyses:
        raise RuntimeError(f"no EcoCrop record for {ctx['crop']}")
    picks = cr.pick_strategies(ctx["crop"], analyses, ctx["irrigated"])
    for slot, p in picks.items():
        a = analyses[p["key"]]
        sim = a["sim"]
        when = f", plant {sim['start_label']}" if sim.get("available") else ""
        emit("Crop Engine", "ok", f"{slot}: {CROPS[p['key']].name} (suitability {a['suitability']:.2f}{when})")
    emit("Crop Engine", "ok", f"Crop analysis took {(time.perf_counter() - t0) * 1000:.0f} ms")
    return dict(analyses=analyses, picks=picks)


def evaluator_agent(ctx: dict, snap: dict, crop_out: dict, emit) -> list[dict]:
    emit("Evaluator", "start", f"Scoring 3 strategies, priority = {ctx['priority']}, {'irrigated' if ctx['irrigated'] else 'rainfed'}")
    h, soil, terr, water, f = snap["history"], snap.get("soil"), snap.get("terrain"), snap.get("water"), snap["nasa"]["features"]
    annual_rain = h.annual_rain_mm() or 0.0
    cur_fam = cr.family(crop_out["analyses"][ctx["crop"]]["rec"])
    z7 = (f.get("precip_7d") or {}).get("z") or 0.0

    econ = {slot: markets.economics(CROPS[p["key"]], ctx.get("country"), ctx.get("state")) for slot, p in crop_out["picks"].items()}
    revs = {s: e["revenue_usd_ha"] for s, e in econ.items() if e["available"]}
    best_rev = max(revs.values()) if len(revs) >= 2 else None

    soil_w = {"live": "live", "modeled": "modeled"}.get((soil or {}).get("provenance", ""), "missing") if soil and soil.get("available") else "missing"
    terr_w = "live" if terr and terr.get("available") else "missing"
    water_w = "live" if water and water.get("available") else "missing"
    out = []
    for slot, p in crop_out["picks"].items():
        a = crop_out["analyses"][p["key"]]
        ref, rec, sim = a["ref"], a["rec"], a["sim"]
        stats = sim.get("rainfed_stats") or {}
        cover, cover_why = cr.cover_class(rec)
        slope = terr["slope_pct"] if terr and terr.get("available") else None
        slope_factor = _clip((slope if slope is not None else SLOPE_FULL_RISK_PCT / 2) / SLOPE_FULL_RISK_PCT, 0.15, 1.0)
        rain_factor = _clip(annual_rain / RAIN_FULL_RISK_MM, 0.3, 1.0)
        n_mid = sum(ref.n_fix) / 2 if ref.n_fix else 0
        metrics = {
            "Suitability": a["suitability"],
            "Water": stats.get("mean") if sim["available"] else None,
            "Soil": _clip(0.5 * min(1, n_mid / 125) + 0.3 * (1.0 if cr.family(rec) != cur_fam else 0.0) + 0.2 * min(1, ref.root_m / 2)),
            "Erosion": _clip(1 - (1 - cover) * slope_factor * rain_factor),
            "Economics": (revs[slot] / best_rev) if (best_rev and slot in revs) else None,
        }
        conf_in = {  # metric -> [(input weight, provenance label)]
            "Suitability": [(1, "live"), (1, "reference"), (1, soil_w)],
            "Water": [(1, "live"), (1, "reference"), (0.6, water_w)],
            "Soil": [(1, "reference"), (1, "reference"), (0.6, "heuristic" if ref.n_fix else "reference")],
            "Erosion": [(1, terr_w), (1, "live"), (1, "heuristic")],
            "Economics": [(1, "modeled"), (1, "reference")],
        }
        mconf = {m: sum(w * PROV[p_] for w, p_ in v) / sum(w for w, _ in v) for m, v in conf_in.items() if metrics.get(m) is not None}
        wts = {m: w for m, w in WEIGHTS[ctx["priority"]].items() if metrics.get(m) is not None}
        total = sum(wts[m] * metrics[m] for m in wts) / sum(wts.values())
        overall = sum(wts[m] * mconf[m] for m in wts) / sum(wts.values())
        notes = []
        if sim["available"]:
            notes.append(f"Indicative sowing window from climate fit: {sim['start_label']} for about {sim['cycle_days']} days. Confirm your local sowing calendar.")
            notes.append(f"Water need up to ~{sim['water_need_mm']:.0f} mm over the season vs ~{sim['rain_in_cycle_mm']:.0f} mm of rain on average.")
        if a["limiting"] and a["suitability"] < 0.7:
            notes.append(f"Main limit: {a['limiting'].replace('_', ' ')} (fit {a['fits'][a['limiting']]:.2f}).")
        if ref.n_fix:
            notes.append(f"Nitrogen fixation: indicative literature range {ref.n_fix[0]}-{ref.n_fix[1]} kg N/ha, varies widely.")
        e = econ[slot]
        if e["available"]:
            notes.append(f"Gross revenue ~${e['revenue_usd_ha']:,.0f}/ha = {e['yield_info']['yield_t_ha']:.1f} t/ha ({e['yield_info']['scope']} average) x ${e['price']['usd_per_t']:.0f}/t ({e['price']['scope']}). Costs not included.")
        else:
            msg = "No revenue estimate: " + "; ".join(e["missing"][:2])
            if e.get("price"):
                msg += f". Live price ${e['price']['usd_per_t']:.0f}/t ({e['price']['scope']})"
            notes.append(msg)
        if soil_w == "missing":
            notes.append("Soil data unavailable here: soil-dependent scores have lower confidence.")
        for m, c in mconf.items():
            if c < 0.5:
                notes.append(f"Verify {m.lower()} locally (confidence {c:.2f}).")
        out.append(dict(slot=slot, key=p["key"], crop=ref.name, blurb=p["why"], metrics=metrics, total=total, metric_conf=mconf,
                        confidence=overall, band=0.25 * (1 - overall), notes=notes, sim=sim, economics=e, suitability=a["suitability"],
                        fits=a["fits"], limiting=a["limiting"], cover=(cover, cover_why)))
        emit("Evaluator", "ok", f"{slot} ({ref.name}): total {total:.2f}, confidence {overall:.2f}")
    return out


def synthesis_agent(ctx: dict, snap: dict, evals: list[dict], emit) -> dict:
    emit("Synthesis", "start", "Writing plain-language brief (template; Gemini advisor next)")
    best = max(evals, key=lambda e: e["total"])
    sim = best["sim"]
    mode = "irrigated" if ctx["irrigated"] else "rainfed"
    st = sim.get("irrigated_stats" if ctx["irrigated"] else "rainfed_stats") or {}
    bits = [f"With a priority on {ctx['priority'].lower()}, **{best['crop']}** ({best['slot']}) scores best at {best['total']:.2f}."]
    if st.get("n"):
        bits.append(f"Replaying {st['n']} real seasons ({mode}), it came out good in {st['share_good']:.0%} of years; the worst was {st['worst_year']} (index {st['worst']:.2f}).")
    bits.append("The winner is not best at everything: compare the bars. Rain and temperature are regional (about 10-50 km); soil, slope and water are specific to your field.")
    emit("Synthesis", "ok", "Brief ready")
    return dict(text=" ".join(bits), best=best["slot"])
