"""LLM fallback chain: Gemini Flash -> Gemini Flash-Lite -> OpenRouter free model(s) -> (caller's template).

Efficiency features:
  * circuit breaker per endpoint: after a 429/5xx/timeout it is skipped until its cooldown ends
    (uses Retry-After / Gemini retryDelay when given), so a dead provider costs no time on later calls
  * local per-minute and per-day budget per endpoint, so we stop BEFORE hitting the provider limit
  * short timeouts, no retries on the same endpoint (we move to the next one instead)
  * response cache keyed by prompt hash, so repeated clicks cost zero calls
Keys are only ever sent to their own provider and never logged.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import httpx

from . import config

GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta"
OPENROUTER_BASE = "https://openrouter.ai/api/v1"
# Verified against a real free key on 2026-11-14: 2.5 models are "no longer available to new users" even though
# they still appear in the model list, 3.8-flash hung for 25s, "-latest" aliases were slow. Order = preference.
# If a model returns 404 the endpoint moves to the next candidate automatically.
GEMINI_PRIMARY_CANDIDATES = ["gemini-3.5-flash", "gemini-3.6-flash", "gemini-3.7-flash"]
GEMINI_SECONDARY_CANDIDATES = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
TOKEN_HEADROOM = 512  # thinking/reasoning models spend output tokens before answering
OPENROUTER_HEADROOM = 1500  # free OpenRouter models are often reasoning models that think at length first
OPENROUTER_PREFERRED = ["gemini", "llama-3.3", "qwen", "mistral", "deepseek"]


class LLMError(Exception):
    """kind: rate_limit | transient | auth | not_found | bad_output"""

    def __init__(self, msg: str, kind: str = "transient", retry_after: float | None = None):
        super().__init__(msg)
        self.kind, self.retry_after = kind, retry_after


@dataclass
class LLMResult:
    text: str
    provider: str
    model: str
    cached: bool = False
    attempts: list[str] = field(default_factory=list)  # human-readable trail for the UI


def extract_json(text: str) -> dict:
    """Parse a JSON object from model output, tolerating code fences and surrounding prose."""
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        v = json.loads(text)
    except ValueError:
        m = re.search(r"\{.*\}", text, re.S)
        if not m:
            raise LLMError("no JSON object in output", "bad_output")
        try:
            v = json.loads(m.group(0))
        except ValueError as e:
            raise LLMError("invalid JSON in output", "bad_output") from e
    if not isinstance(v, dict):
        raise LLMError("JSON output is not an object", "bad_output")
    return v


def _retry_after(resp: httpx.Response) -> float | None:
    h = resp.headers.get("retry-after")
    if h and h.replace(".", "", 1).isdigit():
        return float(h)
    m = re.search(r'"retryDelay"\s*:\s*"(\d+(?:\.\d+)?)s"', resp.text)  # Gemini style
    return float(m.group(1)) if m else None


class Endpoint:
    """One (provider, model) pair with its own circuit breaker and local rate budget."""

    def __init__(self, name: str, kind: str, model: str | list[str], key: str, rpm: int, daily: int,
                 client: httpx.Client | None = None, clock=time.monotonic):
        self.name, self.kind, self.key = name, kind, key
        self.models = [model] if isinstance(model, str) else list(model)  # candidates, best first
        self.rpm, self.daily = rpm, daily
        self.client = client or httpx.Client()
        self.clock = clock
        self._calls: deque[float] = deque()
        self._day_calls: deque[float] = deque()
        self.cooldown_until = 0.0
        self.disabled: str | None = None
        self.last_error: str | None = None
        self.fail_streak = 0  # consecutive transient failures -> exponential backoff

    @property
    def model(self) -> str:
        return self.models[0]

    # -- state -------------------------------------------------------------------------
    def _trim(self, now: float) -> None:
        while self._calls and now - self._calls[0] > 60:
            self._calls.popleft()
        while self._day_calls and now - self._day_calls[0] > 86400:
            self._day_calls.popleft()

    def state(self) -> str:
        now = self.clock()
        self._trim(now)
        if not self.key:
            return "no key"
        if self.disabled:
            return f"disabled: {self.disabled}"
        if now < self.cooldown_until:
            return f"cooling down {self.cooldown_until - now:.0f}s ({self.last_error})"
        if len(self._calls) >= self.rpm:
            return "local per-minute budget used"
        if len(self._day_calls) >= self.daily:
            return "local daily budget used"
        return "ready"

    def ready(self) -> bool:
        return self.state() == "ready"

    def _penalize(self, e: LLMError) -> None:
        now = self.clock()
        self.last_error = f"{e.kind}"
        if e.kind == "not_found" and len(self.models) > 1:
            self.models.pop(0)  # model retired for this key: try the next candidate on the next request
        elif e.kind in ("auth", "not_found"):
            self.disabled = f"{e.kind} ({e})"
        elif e.kind == "rate_limit":
            self.cooldown_until = now + min(e.retry_after or 60.0, 900.0)
        elif e.kind == "transient":
            self.cooldown_until = now + min(20.0 * 2 ** self.fail_streak, 300.0)
            self.fail_streak += 1
        # bad_output: no cooldown, the model is fine, just try the next endpoint for this request

    # -- call --------------------------------------------------------------------------
    def complete(self, system: str, user: str, json_mode: bool, max_tokens: int, timeout: float) -> str:
        now = self.clock()
        self._calls.append(now)
        self._day_calls.append(now)
        try:
            text = self._request(system, user, json_mode, max_tokens, timeout)
        except LLMError as e:
            self._penalize(e)
            raise
        self.fail_streak = 0
        if json_mode:
            extract_json(text)  # validate here so the chain can fall through on bad output
        return text

    def _post_with_deadline(self, url: str, body: dict, headers: dict, timeout: float) -> httpx.Response:
        """POST with a hard wall-clock limit. httpx's own timeout restarts on every received byte, and OpenRouter
        trickles keep-alive bytes while a free model queues (observed: 55 s), so elapsed time is checked per chunk."""
        t0, limit = time.monotonic(), timeout * 1.5
        try:
            with self.client.stream("POST", url, json=body, headers=headers, timeout=timeout) as resp:
                buf = bytearray()
                for chunk in resp.iter_bytes():
                    buf += chunk
                    if time.monotonic() - t0 > limit:
                        raise LLMError("timeout", "transient")
                    if len(buf) > 2_000_000:
                        break
                # iter_bytes() already decoded the body, so drop the encoding headers or httpx would decode it twice
                hdrs = {k: v for k, v in resp.headers.items() if k.lower() not in ("content-encoding", "content-length", "transfer-encoding")}
                return httpx.Response(resp.status_code, headers=hdrs, content=bytes(buf))
        except httpx.TimeoutException as e:
            raise LLMError("timeout", "transient") from e
        except httpx.HTTPError as e:
            raise LLMError(type(e).__name__, "transient") from e

    def _request(self, system: str, user: str, json_mode: bool, max_tokens: int, timeout: float) -> str:
        if self.kind == "gemini":
            url = f"{GEMINI_BASE}/models/{self.model}:generateContent"
            headers = {"x-goog-api-key": self.key}
            gen = {"temperature": 0.2, "maxOutputTokens": max_tokens + TOKEN_HEADROOM}
            if self.model.startswith("gemini-3"):
                gen["thinkingConfig"] = {"thinkingLevel": "low"}  # faster, cheaper, fewer wasted tokens
            if json_mode:
                gen["responseMimeType"] = "application/json"
            body = {"systemInstruction": {"parts": [{"text": system}]},
                    "contents": [{"role": "user", "parts": [{"text": user}]}], "generationConfig": gen}
        else:
            url = f"{OPENROUTER_BASE}/chat/completions"
            headers = {"Authorization": f"Bearer {self.key}", "X-Title": "FieldShift Maestro"}
            suffix = "\n\nReturn ONLY a JSON object." if json_mode else ""
            body = {"model": self.model, "temperature": 0.2, "max_tokens": max_tokens + OPENROUTER_HEADROOM,
                    "messages": [{"role": "system", "content": system}, {"role": "user", "content": user + suffix}]}
        r = self._post_with_deadline(url, body, headers, timeout)
        if r.status_code == 429:
            raise LLMError("rate limited", "rate_limit", _retry_after(r))
        if r.status_code in (401, 403) or (r.status_code == 400 and "API key" in r.text):
            raise LLMError(f"HTTP {r.status_code}", "auth")
        if r.status_code == 404:
            raise LLMError("model not found", "not_found")
        if r.status_code >= 500:
            raise LLMError(f"HTTP {r.status_code}", "transient")
        if r.status_code != 200:
            raise LLMError(f"HTTP {r.status_code}", "transient")
        try:
            data = r.json()
            if self.kind == "gemini":
                cand = data["candidates"][0]
                text, finish = cand["content"]["parts"][0]["text"], cand.get("finishReason")
                cut = finish == "MAX_TOKENS"
            else:
                choice = data["choices"][0]
                text, finish = choice["message"]["content"] or "", choice.get("finish_reason")
                cut = finish == "length"
        except (KeyError, IndexError, TypeError, ValueError) as e:
            raise LLMError("empty or blocked response", "bad_output") from e
        if cut:  # a half-finished answer must never be shown as if it were complete
            raise LLMError("answer cut off by the token limit", "bad_output")
        return text


class LLMChain:
    def __init__(self, endpoints: list[Endpoint], cache_ttl: float = 1800.0, clock=time.monotonic):
        self.endpoints = endpoints
        self.cache_ttl = cache_ttl
        self.clock = clock
        self._cache: dict[str, tuple[float, str, str, str]] = {}
        self._lock = threading.Lock()
        self.last_attempts: list[str] = []  # trail of the most recent call that fell through to the template

    def status(self) -> list[dict]:
        return [dict(provider=e.name, model=e.model, state=e.state()) for e in self.endpoints]

    def complete(self, system: str, user: str, *, json_mode: bool = False, max_tokens: int = 700,
                 timeout: float = 6.0, deadline: float = 20.0) -> LLMResult | None:
        """Try endpoints in order. Returns None if every endpoint is unavailable or fails, or `deadline` seconds
        have passed before the next attempt starts (the caller then uses its deterministic template)."""
        key = hashlib.sha256(f"{system}\x00{user}\x00{json_mode}\x00{max_tokens}".encode()).hexdigest()
        with self._lock:
            hit = self._cache.get(key)
            if hit and self.clock() - hit[0] < self.cache_ttl:
                return LLMResult(hit[1], hit[2], hit[3], cached=True, attempts=["cache hit"])
        attempts: list[str] = []
        t_start = self.clock()
        for ep in self.endpoints:
            st = ep.state()
            if st != "ready":
                attempts.append(f"{ep.name} skipped: {st}")
                continue
            if self.clock() - t_start > deadline:
                attempts.append(f"{ep.name} skipped: {deadline:.0f}s deadline reached")
                continue
            t0 = self.clock()
            try:
                text = ep.complete(system, user, json_mode, max_tokens, timeout)
            except LLMError as e:
                attempts.append(f"{ep.name} failed: {e.kind} ({e}) after {self.clock() - t0:.1f}s")
                continue
            attempts.append(f"{ep.name} ok in {self.clock() - t0:.1f}s")
            with self._lock:
                self._cache[key] = (self.clock(), text, ep.name, ep.model)
            self.last_attempts = attempts
            return LLMResult(text, ep.name, ep.model, attempts=attempts)
        self.last_attempts = attempts
        return None

    def complete_json(self, system: str, user: str, **kw) -> tuple[dict, LLMResult] | None:
        r = self.complete(system, user, json_mode=True, **kw)
        return (extract_json(r.text), r) if r else None


# ------------------------------------------------------------------------------ construction
def discover_gemini(key: str, client: httpx.Client | None = None) -> set[str]:
    """Names of Gemini models this key can call generateContent on. Empty set on any failure."""
    try:
        r = (client or httpx).get(f"{GEMINI_BASE}/models", params={"pageSize": 200},
                                  headers={"x-goog-api-key": key}, timeout=6.0)
        if r.status_code != 200:
            return set()
        return {m["name"].removeprefix("models/") for m in r.json().get("models", [])
                if "generateContent" in m.get("supportedGenerationMethods", [])}
    except (httpx.HTTPError, ValueError, KeyError):
        return set()


def discover_openrouter_free(client: httpx.Client | None = None) -> list[str]:
    """Free OpenRouter model ids (public list, no key), best-guess ordered. Empty list on failure."""
    try:
        r = (client or httpx).get(f"{OPENROUTER_BASE}/models", timeout=6.0)
        ids = [m["id"] for m in r.json().get("data", []) if m["id"].endswith(":free")]
    except (httpx.HTTPError, ValueError, KeyError):
        return []
    ids.sort(key=lambda i: next((n for n, p in enumerate(OPENROUTER_PREFERRED) if p in i), 99))
    return ids


def build_chain(client: httpx.Client | None = None) -> LLMChain:
    g_key, o_key = config.get("GEMINI_API_KEY"), config.get("OPENROUTER_API_KEY")
    eps: list[Endpoint] = []
    if g_key:
        avail = discover_gemini(g_key, client)
        def pick(env: str, cands: list[str]) -> list[str]:
            if config.get(env):
                return [config.get(env)]
            listed = [c for c in cands if c in avail]  # listing is a hint only; 404 handling does the rest
            return listed + [c for c in cands if c not in listed]

        eps.append(Endpoint("gemini-primary", "gemini", pick("GEMINI_MODEL_PRIMARY", GEMINI_PRIMARY_CANDIDATES), g_key,
                            config.get_int("GEMINI_PRIMARY_RPM", 8), config.get_int("GEMINI_PRIMARY_DAILY", 200), client))
        eps.append(Endpoint("gemini-secondary", "gemini", pick("GEMINI_MODEL_SECONDARY", GEMINI_SECONDARY_CANDIDATES), g_key,
                            config.get_int("GEMINI_SECONDARY_RPM", 12), config.get_int("GEMINI_SECONDARY_DAILY", 800), client))
    if o_key:
        models = [config.get("OPENROUTER_MODEL")] if config.get("OPENROUTER_MODEL") else discover_openrouter_free(client)[:2]
        for i, m in enumerate(models or ["meta-llama/llama-3.3-70b-instruct:free"]):
            eps.append(Endpoint(f"openrouter-{i + 1}", "openrouter", m, o_key,
                                config.get_int("OPENROUTER_RPM", 10), config.get_int("OPENROUTER_DAILY", 40), client))
    return LLMChain(eps)
