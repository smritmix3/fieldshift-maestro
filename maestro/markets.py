"""Live market data: futures prices (Yahoo Finance chart API), national yields (Our World in Data / FAOSTAT),
optional Indian mandi prices (data.gov.in Agmarknet, needs a free DATA_GOV_IN_API_KEY).

Economics = gross revenue per hectare = national average yield x market price. Production costs are NOT
included (no free live source), and futures are global benchmarks, not local farm-gate prices. Both caveats
are returned to the UI. If either price or yield is unavailable the crop has no economics (never guessed).
"""
from __future__ import annotations

import csv
import io
import statistics

import httpx

from . import cache, config
from .http import SourceError, UA, get_json
from .reference import CropRef

YAHOO = "https://query1.finance.yahoo.com/v8/finance/chart/{}"
AGMARKNET = "https://api.data.gov.in/resource/9ef84268-d588-465a-a308-a864a43d0070"


@cache.cached("yahoo", 3600)
def quote(symbol: str) -> dict:
    d = get_json(YAHOO.format(symbol), {"range": "5d", "interval": "1d"}, timeout=12, headers={"User-Agent": "Mozilla/5.0 " + UA})
    try:
        m = d["chart"]["result"][0]["meta"]
        return dict(price=float(m["regularMarketPrice"]), time=int(m["regularMarketTime"]), currency=m.get("currency"))
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise SourceError(f"no quote for {symbol}") from e


def futures_usd_per_tonne(ref: CropRef) -> dict:
    sym, div, mult = ref.futures
    q = quote(sym)
    return dict(usd_per_t=q["price"] / div * mult, source=f"Futures {sym} (Yahoo Finance)", as_of=q["time"],
                scope="global benchmark, not local farm-gate price")


def parse_agmarknet(payload: dict, usd_inr: float, scope: str = "state mandi median modal price") -> dict | None:
    def ok(v):
        try:
            return float(v) > 0
        except (TypeError, ValueError):
            return False
    recs = [r for r in payload.get("records", []) if ok(r.get("modal_price"))]
    if not recs:
        return None
    modal = statistics.median(float(r["modal_price"]) for r in recs)  # Rs per quintal
    return dict(usd_per_t=modal * 10 / usd_inr, rs_per_quintal=modal, markets=len({r.get("market") for r in recs}),
                latest=max((r.get("arrival_date") or "" for r in recs), default=""), source="Agmarknet via data.gov.in", scope=scope)


MIN_STATE_RECORDS = 3


@cache.cached("agmarknet", 6 * 3600)
def _agmarknet_raw(commodity: str, state: str | None) -> dict:
    """Latest daily mandi records (the feed holds only the current day's arrivals). The API key is never part of the cache key."""
    key = config.get("DATA_GOV_IN_API_KEY")
    if not key:
        raise SourceError("DATA_GOV_IN_API_KEY not set")
    params = {"api-key": key, "format": "json", "limit": 1000, "filters[commodity]": commodity}
    if state:
        params["filters[state.keyword]"] = state
    return get_json(AGMARKNET, params, timeout=20)


def agmarknet_usd_per_tonne(commodity: str, state: str | None) -> dict:
    """State median modal price; if the state has fewer than 3 mandi quotes today (off-season), the all-India median."""
    fx = quote("INR=X")["price"]
    r = None
    if state:
        d = _agmarknet_raw(commodity, state)
        if len([x for x in d.get("records", [])]) >= MIN_STATE_RECORDS:
            r = parse_agmarknet(d, fx)
    if r is None:
        r = parse_agmarknet(_agmarknet_raw(commodity, None), fx, scope="all-India mandi median modal price (few or no quotes in your state today)")
    if not r:
        raise SourceError(f"no mandi prices for {commodity}")
    return r


def parse_owid(csv_text: str, country: str) -> dict | None:
    rows = [(int(r["year"]), float(r[next(k for k in r if k not in ("entity", "code", "year"))]))
            for r in csv.DictReader(io.StringIO(csv_text)) if r["entity"] == country and r.get("year")]
    if not rows:
        return None
    rows.sort()
    last5 = [v for _, v in rows[-5:]]
    return dict(yield_t_ha=statistics.mean(last5), latest_year=rows[-1][0], latest=rows[-1][1], scope=country)


@cache.cached("owid", 7 * 86400)
def national_yield(slug: str, country: str) -> dict:
    """Mean of the last 5 years of national average yield (t/ha), FAOSTAT via Our World in Data; falls back to World."""
    try:
        r = httpx.get(f"https://ourworldindata.org/grapher/{slug}.csv", params={"v": 1, "csvType": "full", "useColumnShortNames": "true"},
                      headers={"User-Agent": UA}, timeout=40, follow_redirects=True)
    except httpx.HTTPError as e:
        raise SourceError(f"yield data unavailable ({type(e).__name__})") from e
    if r.status_code != 200:
        raise SourceError(f"yield data unavailable (HTTP {r.status_code})")
    out = parse_owid(r.text, country) or parse_owid(r.text, "World")
    if not out:
        raise SourceError("no yield rows")
    return dict(out, source="FAOSTAT via Our World in Data")


def economics(ref: CropRef, country: str | None, state: str | None = None) -> dict:
    """Gross revenue (USD/ha) from live price x national yield, or available=False with the reason."""
    why, price, yld = [], None, None
    if ref.agmarknet and state and (country or "").lower() == "india":
        try:
            price = agmarknet_usd_per_tonne(ref.agmarknet, state)
        except SourceError as e:
            why.append(f"local price: {e}")
    if price is None and ref.futures:
        try:
            price = futures_usd_per_tonne(ref)
        except SourceError as e:
            why.append(f"price: {e}")
    if price is None and not ref.futures and not why:
        why.append("no free live price source for this crop here")
    if ref.owid_yield:
        try:
            yld = national_yield(ref.owid_yield, country or "World")
        except SourceError as e:
            why.append(f"yield: {e}")
    else:
        why.append("no free live yield series for this crop")
    if price is None or yld is None:
        return dict(available=False, missing=why, price=price)   # a live price alone is still shown, but no revenue is computed
    return dict(available=True, revenue_usd_ha=price["usd_per_t"] * yld["yield_t_ha"], price=price, yield_info=yld,
                note="Gross revenue = national average yield x market price. Production costs are not included.")
