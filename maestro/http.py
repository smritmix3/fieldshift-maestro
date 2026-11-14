"""Shared HTTP helper for all external data sources: one error type, timeouts, timing."""
from __future__ import annotations

import time

import httpx

UA = "FieldShiftMaestro/0.1 (NASA Space Apps 2026 prototype)"


class SourceError(Exception):
    """An external data source failed (timeout, rate limit, bad payload, no data)."""


def get_json(url: str, params: dict | None = None, timeout: float = 15.0, method: str = "GET",
             json_body: dict | None = None, data: dict | None = None, headers: dict | None = None):
    """Request and decode JSON, converting every failure into SourceError."""
    h = {"User-Agent": UA, **(headers or {})}
    try:
        r = httpx.request(method, url, params=params, json=json_body, data=data, headers=h, timeout=timeout)
    except httpx.HTTPError as e:
        raise SourceError(f"{type(e).__name__}") from e
    if r.status_code == 429:
        raise SourceError("rate limited (HTTP 429)")
    if r.status_code != 200:
        raise SourceError(f"HTTP {r.status_code}")
    try:
        return r.json()
    except ValueError as e:
        raise SourceError("invalid JSON") from e


def timed(fn, *args, **kwargs):
    """Run fn and return (result_or_None, error_or_None, elapsed_ms). Only SourceError is swallowed."""
    t0 = time.perf_counter()
    try:
        return fn(*args, **kwargs), None, (time.perf_counter() - t0) * 1000
    except SourceError as e:
        return None, str(e), (time.perf_counter() - t0) * 1000
