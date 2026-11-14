"""Fallback-chain tests with a fake HTTP transport (no network, no keys)."""
import json

import httpx
import pytest

from maestro.llm import Endpoint, LLMChain, extract_json, LLMError


def gemini_ok(text='{"ok": true}'):
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": text}]}}]})


def make_chain(handlers, clock=None):
    """handlers: {model: callable(request)->Response}; calls are counted per model."""
    calls = {m: 0 for m in handlers}

    def route(request: httpx.Request):
        model = next(m for m in handlers if m in str(request.url) or m in request.content.decode())
        calls[model] += 1
        return handlers[model](request)

    client = httpx.Client(transport=httpx.MockTransport(route))
    eps = []
    for m in handlers:
        kind = "openrouter" if "/" in m else "gemini"
        eps.append(Endpoint(m, kind, m, "k", rpm=100, daily=1000, client=client, **({"clock": clock} if clock else {})))
    return LLMChain(eps, **({"clock": clock} if clock else {})), calls


def test_falls_back_on_429_then_skips_via_circuit_breaker():
    chain, calls = make_chain({
        "flash": lambda r: httpx.Response(429, headers={"retry-after": "30"}, text="quota"),
        "lite": lambda r: gemini_ok("hello"),
    })
    r = chain.complete("s", "u1")
    assert r.provider == "lite" and r.text == "hello"
    chain.complete("s", "u2")  # different prompt -> no cache hit
    assert calls["flash"] == 1, "flash must be skipped while cooling down"
    assert calls["lite"] == 2


def test_cooldown_expires():
    t = [0.0]
    chain, calls = make_chain({
        "flash": lambda r: httpx.Response(429, headers={"retry-after": "30"}, text="quota"),
        "lite": lambda r: gemini_ok(),
    }, clock=lambda: t[0])
    chain.complete("s", "u1")
    t[0] = 31
    chain.complete("s", "u2")
    assert calls["flash"] == 2


def test_cache_prevents_second_call():
    chain, calls = make_chain({"flash": lambda r: gemini_ok("x")})
    chain.complete("s", "u")
    r = chain.complete("s", "u")
    assert r.cached and calls["flash"] == 1


def test_all_fail_returns_none_for_template():
    chain, _ = make_chain({"flash": lambda r: httpx.Response(500), "lite": lambda r: httpx.Response(503)})
    assert chain.complete("s", "u") is None
    assert len(chain.last_attempts) == 2


def test_auth_error_disables_endpoint_permanently():
    chain, calls = make_chain({"flash": lambda r: httpx.Response(403, text="bad key"), "lite": lambda r: gemini_ok()})
    chain.complete("s", "a")
    chain.complete("s", "b")
    assert calls["flash"] == 1
    assert chain.status()[0]["state"].startswith("disabled")


def test_bad_json_falls_through_to_next_endpoint():
    chain, calls = make_chain({"flash": lambda r: gemini_ok("not json at all"), "lite": lambda r: gemini_ok('{"a": 1}')})
    out = chain.complete_json("s", "u")
    assert out[0] == {"a": 1} and out[1].provider == "lite"
    assert chain.endpoints[0].ready(), "bad output must not trip the circuit breaker"


def test_timeout_is_transient_and_falls_back():
    def boom(r):
        raise httpx.ReadTimeout("slow")
    chain, _ = make_chain({"flash": boom, "lite": lambda r: gemini_ok("fine")})
    assert chain.complete("s", "u").text == "fine"


def test_local_budget_stops_before_provider_limit():
    chain, calls = make_chain({"flash": lambda r: gemini_ok("a")})
    chain.endpoints[0].rpm = 2
    for i in range(4):
        chain.complete("s", f"u{i}")
    assert calls["flash"] == 2


def test_extract_json_variants():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Sure! {"a": 2} hope that helps') == {"a": 2}
    with pytest.raises(LLMError):
        extract_json("nothing")


def test_not_found_moves_to_next_candidate_model():
    seen = []

    def route(request: httpx.Request):
        seen.append(str(request.url))
        if "old-model" in str(request.url):
            return httpx.Response(404, text="retired")
        return gemini_ok("fine")

    client = httpx.Client(transport=httpx.MockTransport(route))
    ep = Endpoint("g", "gemini", ["old-model", "new-model"], "k", rpm=10, daily=100, client=client)
    chain = LLMChain([ep])
    assert chain.complete("s", "u1") is None            # this request falls through
    assert chain.complete("s", "u2").text == "fine"     # next request uses the next candidate
    assert ep.model == "new-model" and not ep.disabled


def test_thinking_headroom_and_config_sent_for_gemini_3():
    bodies = []

    def route(request: httpx.Request):
        bodies.append(json.loads(request.content))
        return gemini_ok("ok")

    client = httpx.Client(transport=httpx.MockTransport(route))
    LLMChain([Endpoint("g", "gemini", "gemini-3.5-flash", "k", 10, 100, client)]).complete("s", "u", max_tokens=100)
    gen = bodies[0]["generationConfig"]
    assert gen["maxOutputTokens"] >= 600 and gen["thinkingConfig"]["thinkingLevel"] == "low"


def test_repeated_transient_failures_back_off_exponentially():
    t = [0.0]
    chain, calls = make_chain({"flash": lambda r: httpx.Response(503), "lite": lambda r: gemini_ok()}, clock=lambda: t[0])
    chain.complete("s", "a")          # fail 1 -> cooldown 20s
    t[0] = 21
    chain.complete("s", "b")          # fail 2 -> cooldown 40s
    t[0] = 41
    chain.complete("s", "c")          # still cooling (needs 61) -> flash not called
    assert calls["flash"] == 2


def test_truncated_answer_falls_through_to_next_endpoint():
    cut = httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "Best option is Mung be"}]}, "finishReason": "MAX_TOKENS"}]})
    chain, calls = make_chain({"flash": lambda r: cut, "lite": lambda r: gemini_ok("complete answer.")})
    r = chain.complete("s", "u")
    assert r.provider == "lite" and r.text == "complete answer."
    assert "cut off" in chain.last_attempts[0]
    chain.complete("s", "u2")
    assert calls["flash"] == 2, "a cut-off answer is not a provider outage: no cooldown"


def test_openrouter_length_finish_is_truncation():
    cut = httpx.Response(200, json={"choices": [{"message": {"content": "half"}, "finish_reason": "length"}]})
    chain, _ = make_chain({"x/model:free": lambda r: cut})
    assert chain.complete("s", "u") is None


def test_deadline_stops_trying_more_endpoints():
    t = [0.0]

    def slow_fail(r):
        t[0] += 12.0           # each failing endpoint "takes" 12 s
        return httpx.Response(500)
    chain, calls = make_chain({"a-flash": slow_fail, "b-lite": slow_fail, "c-lite": lambda r: gemini_ok()}, clock=lambda: t[0])
    assert chain.complete("s", "u", deadline=20.0) is None      # third endpoint never starts: 24 s > 20 s deadline
    assert calls["c-lite"] == 0 and "deadline" in chain.last_attempts[-1]


def test_success_records_timing_trail():
    chain, _ = make_chain({"flash": lambda r: gemini_ok("x")})
    r = chain.complete("s", "u")
    assert r.attempts[0].startswith("flash ok in ")


def test_slow_drip_response_is_cut_by_wall_clock_deadline():
    """A server that keeps sending keep-alive bytes must not defeat the timeout (httpx alone would wait for ever)."""
    import time

    class Drip(httpx.SyncByteStream):
        def __iter__(self):
            for _ in range(200):
                time.sleep(0.02)
                yield b" "
            yield b"{}"

    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Drip())))
    ep = Endpoint("slow", "gemini", "m", "k", rpm=10, daily=10, client=client)
    t0 = time.monotonic()
    with pytest.raises(LLMError) as ei:
        ep.complete("s", "u", False, 50, timeout=0.2)
    assert ei.value.kind == "transient" and time.monotonic() - t0 < 1.0


def test_gzip_encoded_response_is_decoded_once():
    """Real providers gzip their replies; the streamed body must not be decoded twice."""
    import gzip
    payload = json.dumps({"candidates": [{"content": {"parts": [{"text": "zipped ok"}]}}]}).encode()
    resp = lambda r: httpx.Response(200, headers={"content-encoding": "gzip", "retry-after": "5"}, content=gzip.compress(payload))  # noqa: E731
    chain, _ = make_chain({"flash": resp})
    assert chain.complete("s", "u").text == "zipped ok"
