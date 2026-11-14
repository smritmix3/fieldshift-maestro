"""35-year NASA POWER history for the field's grid cell, and per-crop growing-season simulation.

For each crop we search the planting date that best fits its EcoCrop temperature range, then replay that
season in every historical year to get a season stress index (0-1, higher is better):
    rainfed    = temperature_fit x min(1, rain_in_cycle / crop_water_need)
    irrigated  = temperature_fit
crop_water_need = Kc_mid x sum(ET0) over the cycle (an upper bound: Kc_mid applies to mid-season).
It is a transparent limiting-factor index, not a yield forecast.
"""
from __future__ import annotations

import copy
import math
from datetime import date, timedelta

import numpy as np

from . import cache
from .ecocrop import temp_fit
from .http import SourceError, get_json

POWER_URL = "https://power.larc.nasa.gov/api/temporal/daily/point"
START_YEAR = 1991
GOOD_INDEX = 0.7


def snap(v: float) -> float:
    """POWER cells are about 0.5 degrees: nearby farms share one cached history."""
    return round(v * 2) / 2


@cache.cached("power_hist", 30 * 86400)
def _fetch(lat: float, lon: float) -> dict:
    end = date.today() - timedelta(days=2)
    data = get_json(POWER_URL, {"parameters": "T2M,T2M_MAX,T2M_MIN,PRECTOTCORR", "community": "AG", "latitude": lat, "longitude": lon,
                                "start": f"{START_YEAR}0101", "end": end.strftime("%Y%m%d"), "format": "JSON"}, timeout=45)
    try:
        p = data["properties"]["parameter"]
        days = sorted(p["T2M"])
        out = {"start": days[0], "end": days[-1]}
        for k in ("T2M", "T2M_MAX", "T2M_MIN", "PRECTOTCORR"):
            out[k] = [None if p[k][d] < -900 else p[k][d] for d in days]
    except (KeyError, TypeError) as e:
        raise SourceError("unexpected POWER history payload") from e
    return out


def ra_mm(lat: float, doy: np.ndarray) -> np.ndarray:
    """FAO-56 extraterrestrial radiation (mm/day equivalent)."""
    phi = math.radians(lat)
    dr = 1 + 0.033 * np.cos(2 * np.pi * doy / 365)
    dec = 0.409 * np.sin(2 * np.pi * doy / 365 - 1.39)
    ws = np.arccos(np.clip(-math.tan(phi) * np.tan(dec), -1, 1))
    ra = 24 * 60 / np.pi * 0.0820 * dr * (ws * math.sin(phi) * np.sin(dec) + math.cos(phi) * np.cos(dec) * np.sin(ws))
    return 0.408 * ra


class History:
    def __init__(self, raw: dict, lat: float):
        s = raw["start"]
        self.start = date(int(s[:4]), int(s[4:6]), int(s[6:]))
        f = lambda k: np.array([np.nan if v is None else v for v in raw[k]], dtype=float)  # noqa: E731
        self.t, self.tmax, self.tmin, self.p = f("T2M"), f("T2M_MAX"), f("T2M_MIN"), f("PRECTOTCORR")
        self.n = len(self.t)
        self.lat = lat
        self.doy = np.array([(self.start + timedelta(i)).timetuple().tm_yday for i in range(self.n)], dtype=float)
        self.et0 = self._et0()
        self.last_year = (self.start + timedelta(self.n - 1)).year

    def _et0(self) -> np.ndarray:
        return 0.0023 * (self.t + 17.8) * np.sqrt(np.clip(self.tmax - self.tmin, 0, None)) * ra_mm(self.lat, self.doy)

    def scenario(self, rain_change_pct: float = 0.0, temp_change_c: float = 0.0) -> "History":
        """A what-if copy of the real record: every day's rain scaled and every temperature shifted (ET0 is recomputed).
        This replays history under a changed climate; it is not a forecast."""
        h = copy.copy(self)
        h.p = self.p * (1 + rain_change_pct / 100.0)
        h.t, h.tmax, h.tmin = self.t + temp_change_c, self.tmax + temp_change_c, self.tmin + temp_change_c
        h.et0 = h._et0()
        return h

    def years(self) -> list[int]:
        return [y for y in range(self.start.year, self.last_year + 1)]

    def idx(self, year: int, doy: int) -> int:
        return (date(year, 1, 1) + timedelta(doy - 1) - self.start).days

    def window(self, year: int, doy: int, length: int):
        """Slice of the cycle that starts on `doy` of `year`, or None if outside the data or too many gaps."""
        i = self.idx(year, doy)
        if i < 0 or i + length > self.n:
            return None
        sl = slice(i, i + length)
        if np.isnan(self.t[sl]).mean() > 0.1 or np.isnan(self.p[sl]).mean() > 0.1:
            return None
        return sl

    def annual_rain_mm(self) -> float | None:
        sums = []
        for y in self.years():
            a, b = self.idx(y, 1), self.idx(y, 1) + (366 if y % 4 == 0 else 365)
            if a >= 0 and b <= self.n and not np.isnan(self.p[a:b]).any():
                sums.append(float(self.p[a:b].sum()))
        return float(np.mean(sums)) if sums else None

    def annual_mean_temp(self) -> float | None:
        v = self.t[~np.isnan(self.t)]
        return float(v.mean()) if v.size else None


def history(lat: float, lon: float) -> History:
    la, lo = snap(lat), snap(lon)
    return History(_fetch(la, lo), la)


def _label(doy: int) -> str:
    d = date(2001, 1, 1) + timedelta(doy - 1)
    return f"{'early' if d.day <= 10 else 'mid' if d.day <= 20 else 'late'} {d.strftime('%B')}"


def cycle_length(ref_days: int | None, rec: dict) -> int:
    """FAO-56 typical cycle, clipped to the EcoCrop min/max cycle (the dataset is the authority);
    midpoint of EcoCrop when FAO-56 gives none."""
    gmin, gmax = rec.get("GMIN"), rec.get("GMAX")
    if ref_days is None:
        return int(round((gmin + gmax) / 2)) if gmin and gmax else 120
    if gmin and gmax:
        return int(min(max(ref_days, gmin), gmax))
    return ref_days


def summarize(values: list[float], years: list[int]) -> dict:
    if not values:
        return dict(n=0, mean=None, p10=None, worst=None, worst_year=None, share_good=None)
    a = np.array(values)
    w = int(a.argmin())
    return dict(n=len(a), mean=float(a.mean()), p10=float(np.percentile(a, 10)), worst=float(a[w]), worst_year=years[w],
                share_good=float((a >= GOOD_INDEX).mean()))


EARLY_DAYS = 20      # establishment phase: germination and seedling emergence need temperatures inside the crop's limits
PLATEAU = 0.95       # start dates within 5% of the best climatological fit count as equally suitable


def _season_factors(h: History, rec: dict, kc_mid: float, sl: slice):
    """(temperature fit over the cycle x fit over the establishment phase, rain, crop water need) for one season."""
    cyc = temp_fit(rec, float(np.nanmean(h.t[sl])))
    early = temp_fit(rec, float(np.nanmean(h.t[sl.start:min(sl.start + EARLY_DAYS, sl.stop)])))
    parts = [x for x in (cyc, early) if x is not None]
    tf = float(np.prod(parts)) if parts else None
    return tf, float(np.nansum(h.p[sl])), kc_mid * float(np.nansum(h.et0[sl]))


def evaluate_crop(h: History, rec: dict, kc_mid: float, cycle_days: int | None, step: int = 7) -> dict:
    """Choose the planting date, then replay that season in every historical year.

    Date rule (data-driven, no calendar table): among start dates whose mean temperature fit (cycle x establishment
    phase, EcoCrop limits) is within 5% of the best, take the one with the highest mean rain coverage. So rice lands in
    the monsoon and wheat in the cool season without anyone hard-coding dates."""
    L = cycle_length(cycle_days, rec)
    years = h.years()
    cand = []  # (start_doy, mean temperature fit, mean rainfed index)
    for s in range(1, 366, step):
        fits, rainfed = [], []
        for y in years:
            sl = h.window(y, s, L)
            if sl is None:
                continue
            tf, p_sum, etc = _season_factors(h, rec, kc_mid, sl)
            if tf is not None:
                fits.append(tf)
                rainfed.append(tf * (min(1.0, p_sum / etc) if etc > 0 else 1.0))
        if fits:
            cand.append((s, float(np.mean(fits)), float(np.mean(rainfed))))
    if not cand:
        return dict(available=False, cycle_days=L, note="Not enough EcoCrop temperature data or climate history for this crop.")
    best_fit = max(c[1] for c in cand)
    plateau = [c for c in cand if c[1] >= PLATEAU * best_fit] if best_fit > 0 else cand
    best_doy, best_score, _ = max(plateau, key=lambda c: (c[2], -c[0]))
    ys, rainfed, irrigated, frost, need, rain = [], [], [], 0, [], []
    for y in years:
        sl = h.window(y, best_doy, L)
        if sl is None:
            continue
        tf, p_sum, etc = _season_factors(h, rec, kc_mid, sl)
        wf = min(1.0, p_sum / etc) if etc > 0 else 1.0
        ys.append(y); irrigated.append(tf); rainfed.append(tf * wf); need.append(etc); rain.append(p_sum)
        early = slice(sl.start, min(sl.start + 30, sl.stop))
        if rec.get("KTMP") is not None and np.nanmin(h.tmin[early]) < rec["KTMP"]:
            frost += 1
    return dict(available=True, start_doy=best_doy, start_label=_label(best_doy), cycle_days=L, temp_fit_clim=best_score,
                years=ys, rainfed=rainfed, irrigated=irrigated, rainfed_stats=summarize(rainfed, ys), irrigated_stats=summarize(irrigated, ys),
                water_need_mm=float(np.mean(need)), rain_in_cycle_mm=float(np.mean(rain)), frost_years=frost,
                frost_note="Years with a night below the EcoCrop killing temperature in early growth (indicative; area-average data)." if rec.get("KTMP") is not None else None)
