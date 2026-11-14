"""FAO EcoCrop crop requirements, fetched at runtime from the public OpenCLIM mirror and cached.

Dataset: FAO EcoCrop database (Hijmans et al.), 2,568 crops; mirror https://github.com/OpenCLIM/ecocrop
Fields used: temperature (TOPMN/TOPMX optimal, TMIN/TMAX absolute), annual rainfall (ROPMN/ROPMX, RMIN/RMAX),
soil pH, texture, drainage, growing-cycle length (GMIN/GMAX), plant family, life span, habit, category.
A blank value in the source stays None; fit functions treat None as "no information", never as a guess.
"""
from __future__ import annotations

import csv
import io

import httpx

from . import cache
from .http import UA, SourceError

URL = "https://raw.githubusercontent.com/OpenCLIM/ecocrop/main/EcoCrop_DB.csv"
NUM = ["TOPMN", "TOPMX", "TMIN", "TMAX", "ROPMN", "ROPMX", "RMIN", "RMAX", "PHOPMN", "PHOPMX", "PHMIN", "PHMAX", "KTMP", "GMIN", "GMAX"]
TXT = ["FAMNAME", "LISPA", "HABI", "CAT", "TEXT", "TEXTR", "DRA", "DRAR", "ABITOL", "ABISUS", "SAL", "DEP"]


def _num(v: str):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse(csv_text: str, scientific_names: list[str]) -> dict[str, dict]:
    """Pick the requested species (exact ScientificName match, else first prefix match)."""
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    out: dict[str, dict] = {}
    for sci in scientific_names:
        hit = next((r for r in rows if r["ScientificName"].strip() == sci), None) or \
              next((r for r in rows if r["ScientificName"].strip().startswith(sci)), None)
        if not hit:
            continue
        d = {k: _num(hit.get(k)) for k in NUM}
        d.update({k: (hit.get(k) or "").strip() or None for k in TXT})
        d["EcoPortCode"] = hit.get("EcoPortCode")
        out[sci] = d
    return out


def load(scientific_names: list[str]) -> dict[str, dict]:
    """EcoCrop records for the given species. Cached 30 days after a successful download."""
    key = "ecocrop:" + "|".join(sorted(scientific_names))
    hit = cache.get(key, 30 * 86400)
    if hit:
        return hit
    try:
        r = httpx.get(URL, headers={"User-Agent": UA}, timeout=30, follow_redirects=True)
    except httpx.HTTPError as e:
        raise SourceError(f"EcoCrop download failed ({type(e).__name__})") from e
    if r.status_code != 200:
        raise SourceError(f"EcoCrop download failed (HTTP {r.status_code})")
    data = parse(r.content.decode("utf-8", errors="replace"), scientific_names)
    if not data:
        raise SourceError("EcoCrop file had none of the requested crops")
    cache.put(key, data)
    return data


# ---------------------------------------------------------------- fit functions (0..1, None = no information)
def trapezoid(x: float, abs_min, opt_min, opt_max, abs_max) -> float | None:
    """1 inside the optimal range, falling linearly to 0 at the absolute limits."""
    if x is None or opt_min is None or opt_max is None:
        return None
    if opt_min <= x <= opt_max:
        return 1.0
    if x < opt_min:
        return 0.0 if abs_min is None or x <= abs_min else (x - abs_min) / (opt_min - abs_min)
    return 0.0 if abs_max is None or x >= abs_max else (abs_max - x) / (abs_max - opt_max)


TEXTURE_GROUP = {"sand": "light", "loamy sand": "light", "sandy loam": "light", "loam": "medium", "silt loam": "medium", "silt": "medium",
                 "sandy clay loam": "medium", "clay loam": "heavy", "silty clay loam": "heavy", "sandy clay": "heavy",
                 "silty clay": "heavy", "clay": "heavy"}


def texture_fit(rec: dict, texture: str | None) -> float | None:
    """EcoCrop lists optimal (TEXT) and tolerable (TEXTR) texture groups. 1.0 optimal, 0.6 tolerable, 0.2 outside."""
    group = TEXTURE_GROUP.get(texture or "")
    opt, tol = (rec.get("TEXT") or ""), (rec.get("TEXTR") or "")
    if not group or not (opt or tol):
        return None
    if "wide" in opt or "wide" in tol or group in opt:
        return 1.0
    return 0.6 if group in tol else 0.2


def drainage_group(ssurgo_class: str | None) -> str | None:
    c = (ssurgo_class or "").lower()
    if not c:
        return None
    if "excessively" in c:
        return "excessive"
    if "poorly" in c:
        return "poorly"
    if "moderately well" in c:
        return "imperfectly"
    return "well" if "well" in c else None


def drainage_fit(rec: dict, ssurgo_class: str | None) -> float | None:
    g = drainage_group(ssurgo_class)
    opt, tol = (rec.get("DRA") or ""), (rec.get("DRAR") or "")
    if not g or not (opt or tol):
        return None
    if g in opt or (g == "imperfectly" and "imperfect" in opt):
        return 1.0
    return 0.6 if (g in tol or (g == "imperfectly" and "imperfect" in tol)) else 0.2


def ph_fit(rec: dict, ph: float | None) -> float | None:
    return trapezoid(ph, rec.get("PHMIN"), rec.get("PHOPMN"), rec.get("PHOPMX"), rec.get("PHMAX"))


def rain_fit(rec: dict, annual_mm: float | None) -> float | None:
    return trapezoid(annual_mm, rec.get("RMIN"), rec.get("ROPMN"), rec.get("ROPMX"), rec.get("RMAX"))


def temp_fit(rec: dict, mean_c: float | None) -> float | None:
    return trapezoid(mean_c, rec.get("TMIN"), rec.get("TOPMN"), rec.get("TOPMX"), rec.get("TMAX"))
