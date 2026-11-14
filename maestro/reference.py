"""Published reference constants per crop. These are NOT live data: no API exists for them. They are
standard agronomy tables, cited below; everything else (climate, soil, terrain, prices, yields,
crop requirement ranges) is fetched live or from the FAO EcoCrop dataset at runtime.

Sources
  FAO-56  Allen et al. (1998) FAO Irrigation and Drainage Paper 56: Table 12 (Kc mid-season),
          Table 22 (maximum root depth, midpoint of the published range), Table 11 (typical total
          growth-stage length, days). Crops FAO-56 does not list use the closest class (noted).
  N-FIX   Indicative biological N2-fixation ranges (kg N/ha per season) after Herridge et al. 2008
          (Plant and Soil 311) and Peoples et al. 2009 (Symbiosis 48): wide, site dependent, shown as a
          range in the UI and never as a precise number.
Verify these against the primary sources before relying on them for real farm decisions.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CropRef:
    key: str
    name: str
    sci: str                                   # EcoCrop ScientificName (exact)
    kc_mid: float                              # FAO-56 Table 12
    root_m: float                              # FAO-56 Table 22 (midpoint)
    cycle_days: int | None                     # FAO-56 Table 11 typical total; clipped to EcoCrop GMIN..GMAX. None -> EcoCrop midpoint
    n_fix: tuple[int, int] | None = None       # kg N/ha range, legumes only
    futures: tuple[str, float, float] | None = None   # (Yahoo symbol, divisor, multiplier) -> USD per tonne = quote / divisor * multiplier
    owid_yield: str | None = None              # Our World in Data (FAOSTAT) yield chart slug, t/ha
    agmarknet: str | None = None               # Agmarknet commodity name (India mandi prices, needs DATA_GOV_IN_API_KEY)
    note: str = ""


CROPS: dict[str, CropRef] = {c.key: c for c in [
    CropRef("wheat", "Wheat", "Triticum aestivum", 1.15, 1.65, 130, None, ("ZW=F", 100.0, 36.7437), "wheat-yields", "Wheat"),
    CropRef("rice", "Rice", "Oryza sativa", 1.20, 0.75, 150, None, ("ZR=F", 1.0, 22.0462), "rice-yields", "Paddy(Dhan)(Common)"),
    CropRef("maize", "Maize", "Zea mays", 1.20, 1.35, 125, None, ("ZC=F", 100.0, 39.3680), "maize-yields", "Maize"),
    CropRef("soybean", "Soybean", "Glycine max", 1.15, 0.95, 140, (30, 100), ("ZS=F", 100.0, 36.7437), "soybean-yields", "Soyabean"),
    CropRef("cotton", "Cotton", "Gossypium hirsutum", 1.175, 1.35, 190, None, ("CT=F", 100.0, 2204.62), None, "Cotton"),
    CropRef("chickpea", "Chickpea", "Cicer arietinum", 1.00, 0.80, None, (30, 100), None, None, "Bengal Gram(Gram)(Whole)", "Kc mid per FAO-56 pulses class"),
    CropRef("mung", "Mung bean", "Vigna radiata", 1.15, 0.75, 95, (20, 80), None, None, "Green Gram(Moong)(Whole)", "FAO-56 dry-bean class"),
    CropRef("cowpea", "Cowpea", "Vigna unguiculata", 1.15, 0.75, 95, (20, 100), None, None, None, "FAO-56 dry-bean class"),
    CropRef("groundnut", "Groundnut", "Arachis hypogaea", 1.15, 0.75, 130, (30, 100), None, None, "Groundnut"),
    CropRef("alfalfa", "Alfalfa", "Medicago sativa", 0.95, 1.50, None, (100, 250), None, None, None, "Perennial forage legume; FAO-56 average-cutting Kc"),
    CropRef("sorghum", "Sorghum", "Sorghum bicolor", 1.05, 1.50, 130, None, None, None, "Jowar(Sorghum)"),
    CropRef("bajra", "Pearl millet", "Pennisetum glaucum", 1.00, 1.50, 105, None, None, None, "Bajra(Pearl Millet/Cumbu)"),
    CropRef("mustard", "Mustard", "Brassica juncea", 1.075, 1.25, 100, None, None, None, "Mustard", "FAO-56 rapeseed class"),
]}
