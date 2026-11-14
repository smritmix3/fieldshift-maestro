# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

**FieldShift Maestro**: a NASA Space Apps 2026 ("Field Shift: Adapting Farms with NASA Data") entry. A farmer gives an address/PIN/ZIP or draws a field; the app fetches live NASA and partner data and scores three crop-rotation options, then a GenAI advisor explains them. Submission deadline: Sun 2026-11-15 11:59 pm local (IST). Free tools only. Target host: Hugging Face Space.

`PLAN_4H.md` is the current build plan and the source of truth for scope, timeline and cut order (cut order: field-history NDVI, then OpenRouter tier, then USGS NWIS, then extra languages). `DEVELOPMENT_PLAN.md` is the older, broader design doc. Where they differ, `PLAN_4H.md` wins.

This folder is separate from the sibling `codecrafters-claude-code-python/` repo (a different project with its own CLAUDE.md). Do not edit it.

## Current status (updated 2026-11-14, after building the advisor)

**Done and tested (71 tests pass; live-verified on Punjab farmland (30.62, 75.95) and Ames ZIP 50010):**
- Address/PIN/ZIP search, click-to-pin and polygon drawing on a map with NASA GIBS overlays, field validation. Demo buttons: "Punjab" (fixed farmland point, because a city-centre PIN has no SoilGrids data) and "Ames" (ZIP lookup). The Punjab demo is warmed in a background thread at server start (`warm_demo`).
- Live data layer: NASA POWER (recent + 35-year history), CMR pass freshness, ERA5, SRTM slope, soil (SSURGO US / SoilGrids elsewhere), nearby water (OSM + JRC), FAO EcoCrop, live futures prices, FAOSTAT yields, **Agmarknet mandi prices (verified live 2026-11-14 with the user's key, all 13 crops except cowpea and alfalfa)**.
- Real crop engine, planting windows, 35-season replay, three picks, scoring and confidence. No mock data remains.
- **GenAI advisor (`maestro/advisor.py`)**: plain-language brief (English/Hindi) and a what-if chat. The LLM never produces numbers; it gets a fact sheet and every number it writes is checked against it (`ungrounded`); one regeneration, then the deterministic template. Scenario chat = call 1 parses the question (clamped in code, regex fallback when no LLM), `History.scenario()` perturbs the real 35-year record, the engine re-scores, call 2 explains. Off-topic questions cost no second call. Numbers typed by the farmer are NOT trusted (prompt-injection guard).
- **LLM chain hardening**: truncated replies (`MAX_TOKENS`/`length`) fall through to the next endpoint; hard wall-clock deadline per request (streamed, because httpx's timeout resets on every byte and OpenRouter drips keep-alive bytes: a 55 s stall was observed) and per chain call (`deadline=20`); OpenRouter gets 1500 extra tokens (reasoning models); timing trail in `LLMResult.attempts`.
- UI: brief shows the template instantly and swaps in the AI text when ready; chat panel (fragment) with example buttons; language switch; AI provider status; data-sources and AI-use disclosure expander.
- Deploy files written locally but NOT pushed: `Dockerfile` (HF Docker Space, port 8501), `README.md` with Space front matter, `.dockerignore`, `.streamlit/config.toml`, complete `requirements.txt` (verified by a clean-venv install and headless start), `requirements-dev.txt`.

**Left, in priority order (deadline Sun 2026-11-15 11:59 pm IST):**
1. **Deploy to a Hugging Face Space.** The user has NOT created the Space yet. Give exact steps; ask before pushing or using their account. Keys go in Space **secrets**. Docker is not installed locally, so the image build is untested here (the same requirements were verified in a clean venv).
2. **Submission pack**: 50-200 word summary, demo (7 slides max or a 30 s video), AI-use disclosure, resource list, no NASA logos. Be precise: SMAP/ECOSTRESS are pass-freshness and map layers only.
3. **Field NDVI history** (stretch): Sentinel-2 via Planetary Computer titiler statistics for the polygon, 12 months (verified working, about 1 s per scene).
4. Run `/code-review` and `/security-review` before publishing. Browser check of the map (draw/click) is still only covered by AppTest.

**Known behaviour:** the free Gemini 3.5-flash primary model hits its quota quickly (an 821 s cooldown was seen), so most answers come from `gemini-secondary`; that is the chain working as designed. Economics (revenue) exists only for wheat, rice, maize and soybean (live price x FAOSTAT yield); other crops show the live price only.

**User preferences:** free tools only (hard cap $8, expected $0), no mocked data, fast loading (caching is fine), India-first demo plus a US demo for NASA reviewers, time zone IST. Pause for approval before irreversible or outward-facing steps (deploying, publishing, anything using their accounts).

## Commands

Run from this folder. Use the project venv (`.venv`, Python 3.12):

- Run the app: `.venv/Scripts/python -m streamlit run app.py` (serves on http://localhost:8501)
- All tests: `.venv/Scripts/python -m pytest -q tests`
- One test: `.venv/Scripts/python -m pytest -q tests/test_llm.py::test_cache_prevents_second_call`
- Validate keys and the LLM fallback chain against the real providers: `.venv/Scripts/python -m maestro.check_keys`
- Install deps: `.venv/Scripts/python -m pip install -r requirements-dev.txt`

No linter or build step is configured. On Windows with Git Bash, avoid giant multi-file heredocs; a parse error aborts the whole command and writes nothing. Create files with the Write tool, and for multi-edit patches write a small throwaway `_patch.py` with the Write tool, run it, then delete it. Set `PYTHONIOENCODING=utf-8` when scripts print emoji or non-ASCII text. UI tests use `streamlit.testing.v1.AppTest` (see `tests/test_app_smoke.py`); the map component itself can only be verified in a real browser.

## Keys and secrets

- Keys live in a local `.env` (copy of `.env.example`), loaded by `maestro/config.py`. `.env` is gitignored. On Hugging Face, add them as **secrets** (not variables).
- Needed: `GEMINI_API_KEY` (free, aistudio.google.com). Optional: `OPENROUTER_API_KEY`.
- Never print, log or commit a key. Use `config.mask()` for display.
- Free-tier prompts may be used by the provider to improve products, so send only field-level numbers, never personal data. The UI should say so.

## Architecture

Single Streamlit app (no separate API server). Package `maestro/`:

- `nasa.py`: live clients for NASA POWER (daily point data) and CMR (latest-granule freshness for SMAP, ECOSTRESS, GPM IMERG, HLS). Both need no key. `timed()` wraps calls as `(result, error, ms)`.
- `orchestrator.py`: `run_pipeline(ctx, queue, simulate_fail)` runs in a background thread: `snapshot.collect` (parallel field data) -> `evaluate.crop_agent` -> `evaluate.evaluator_agent` -> `evaluate.synthesis_agent`, streaming events through a `queue.Queue` so the UI shows live progress, and ends with `(DONE, result_or_None)`. `ctx` keys: lat, lon, tier, area_ha, source, crop, priority, irrigated, country, state. `agents.py` only holds `nasa_data_agent` (POWER recent features + CMR freshness; no features if POWER is down).
- `geo.py`: geocoding (Nominatim for address/PIN/ZIP with Open-Meteo fallback), `Field` geometry, validation with farmer-readable `GeometryError`s, region tier (`US` vs `ROW`), 150 m default circle around a pin.
- `providers.py`: soil (SSURGO for US, SoilGrids elsewhere, queried at the pin only), SRTM terrain and slope, nearby water (OpenStreetMap Overpass raced across mirrors + JRC satellite surface water), ERA5. Each returns a dict with `available`, `source`, `provenance`, `resolution`. Failures raise `SourceError`.
- `snapshot.py`: runs all field sources plus the NASA agent in parallel and returns per-source status. `gibs.py`: NASA GIBS overlay layer table and tile URLs. `cache.py`: SQLite TTL cache (`@cache.cached`). `http.py`: shared HTTP helper and `SourceError`.
- `reference.py`: published reference constants per crop (FAO-56 Kc, root depth, cycle length; literature N-fixation ranges; futures symbols; yield series slugs). The only non-live crop data, each cited in the file.
- `ecocrop.py`: FAO EcoCrop requirements fetched at runtime from the OpenCLIM GitHub mirror and cached 30 days; fit functions (blank source values stay `None`, never guessed).
- `climate.py`: 35 years of NASA POWER for the field's 0.5-degree cell (cached 30 days), Hargreaves ET0, per-crop planting-window search and season replay (stress index = temperature fit x rain coverage).
- `crops.py`: suitability (EcoCrop limiting-factor rule) for 13 crops and the three strategy picks (baseline = current crop; resilient = best 10th-percentile season among suitable crops from a different plant family; N-fixer = best-suited legume, deepest roots among near-ties).
- `markets.py`: live futures (Yahoo chart API), national yields (FAOSTAT via Our World in Data), optional Agmarknet mandi prices. Economics = price x yield (gross revenue, no costs); unavailable if either is missing.
- `advisor.py`: fact sheet -> LLM brief/chat with number grounding, scenario parsing (`clamp_params`, `regex_parse`) and `run_scenario` (re-scores on `History.scenario(rain_pct, temp_c)`). The pipeline result carries `history` so scenarios can re-run the engine. The UI generates the AI brief after the cards are on screen (`ss.briefs` keyed by result id and language); `evaluate.synthesis_agent` stays the instant template and the fallback.
- `evaluate.py`: crop agent, evaluator (Suitability, Water, Soil, Erosion, Economics; weights renormalised over metrics that exist) and the template synthesis. `agents.py` now only holds the NASA data agent.
- `llm.py`: LLM fallback chain (details below).
- `config.py`: `.env` loader and settings helpers.
- `app.py` (root): Streamlit UI.

Still to do: deployment, submission pack, optional Sentinel-2 field NDVI history (see "Current status"). USGS NWIS (US water gauges) was cut. The climate stress test is done (per-crop 35-season replay, shown as a per-year bar chart on each card).

### Design rules that matter

- **The LLM never produces numbers.** Deterministic code computes scores; the LLM only narrates tool/structured output. Text is checked so every number matches the computed data, with one regeneration and then a template fallback.
- **Provenance on every input**: `live`, `modeled`, `reference` (published tables), `heuristic`, or `missing`, carried into confidence scores (weights in `evaluate.PROV`). SMAP/ECOSTRESS appear only as pass-freshness (CMR) and map layers unless a stretch goal ingests pixels. Soil moisture comes from MERRA-2 (POWER) or ERA5 and is labeled modeled. Do not overclaim satellite use in the UI or submission text.
- **Honest resolution**: POWER is about 50 km and ERA5 about 10-25 km, so climate inputs are regional for any farm. Field-specific signal comes from soil, terrain, NDVI and nearby water. The UI should say which inputs are regional versus field-specific.
- **Degrade, never crash**: every external call has a timeout; failures fall back and are shown in the status panel. Missing data lowers confidence and says why.
- **Scenario chat** uses two plain JSON LLM calls (parse the question into scenario settings, then explain the deterministic result), not native function calling, so behavior is identical across providers.

## LLM fallback chain (`maestro/llm.py`)

Order: `gemini-primary` -> `gemini-secondary` -> `openrouter-1/2` (free models) -> caller's deterministic template (`complete()` returns `None`).

- Each endpoint has a circuit breaker (cooldown from `Retry-After`/Gemini `retryDelay` on 429; exponential backoff 20s to 300s on transient errors; permanent disable on auth errors) and a local per-minute and per-day budget.
- A 404 on a model advances that endpoint to its next candidate model instead of disabling it.
- Results are cached by prompt hash; JSON mode validates output and falls through to the next endpoint on bad JSON without tripping the breaker.
- Endpoint failures are expected on free tiers. Tests cover this with `httpx.MockTransport` in `tests/test_llm.py`; add cases there when changing chain behavior.

### Gemini facts verified against a real free key on 2026-11-14 (re-verify if behavior changes)

- `gemini-2.5-*` appear in the model list but return 404 "no longer available to new users". Listing models is only a hint; do not trust it as availability.
- Working: `gemini-3.5-flash`, `gemini-3.5-flash-lite`, `gemini-3.1-flash-lite`. `gemini-3.8-flash` hung for 25s; `-latest` aliases were slow. Defaults avoid both.
- 3.x models spend output tokens on hidden thinking, which can leave short replies empty. The code adds `TOKEN_HEADROOM` to `maxOutputTokens` and sets `thinkingConfig.thinkingLevel = "low"` for `gemini-3*` models.
- The primary Flash model intermittently returned 503 or timed out, which is why the chain exists. Per-call timeout is 6s.
- OpenRouter free models are reasoning models too; give them token headroom. Free-model ids change, so they are discovered at startup from the public model list.

## No-mock rule

Nothing in the pipeline is invented. If a source fails, the metric/feature is omitted or confidence drops, and the UI says why (e.g. no economics for crops without a free live price AND yield; no recent-weather features if POWER is down). Do not reintroduce placeholder numbers, baseline tables or default fallback values. Heuristics that remain (cover class from EcoCrop habit, erosion slope/rain constants, metric weights, 0.6/0.2 texture-drainage fit levels) are documented as methods in the code, not data.

Known limits: economics exists only where a live price and a live yield both exist (wheat, rice, maize, soybean via futures + FAOSTAT; more in India with `DATA_GOV_IN_API_KEY`, unverified until a key is available). Futures are global benchmarks, not farm-gate prices. Planting windows are indicative (EcoCrop temperature fit plus rain coverage), e.g. rice in Ludhiana lands mid-August. EcoCrop `KTMP` is informational only (it would "kill" winter wheat everywhere). SoilGrids is queried at the pin only (slow server; one request; built-up pixels return no data and the UI tells the farmer to click the field).

## Data-layer behavior to preserve

- Cache TTLs: geocode 7 d, POWER recent 3 h, CMR 1 h, 35-yr history 30 d, EcoCrop 30 d, soil 30 d, prices 1 h, yields 7 d. Only successful results are cached; a failed or partial lookup must raise `SourceError` (not return an empty dict) so it is retried later. Water caches OSM and JRC separately for this reason.
- Public Overpass servers were at capacity on 2026-11-14 (every mirror timed out). `osm_water` therefore has a 5 s deadline and a 5-minute circuit breaker, and water falls back to JRC satellite detection. SoilGrids is slow (3-13 s), queues concurrent requests (a parallel fan-out made it slower, so do not parallelise it) and returns null on built-up pixels (Ludhiana PIN centre). It is therefore one request at the pin with a 3-minute skip after a failure; a legitimate "no data" result is cached and the UI tells the farmer to click the field. Nearby-cell retries were tried and removed for speed.
- Map: the UI has two modes (drop pin by click, or draw a boundary) because draw-tool clicks would otherwise move the pin. Streamlit reruns on every map interaction, so analysis only runs from the Analyze button and results live in `st.session_state`.
- GIBS layer ids and date lags are in `gibs.py` (verified 2026-11-14). IMERG and SMAP tiles only exist to zoom 6; the map upscales beyond that.

## External data notes (verified live 2026-11-14)

- NASA POWER daily point API and CMR granule search: no key. POWER uses `-999` as a fill value (masked in `nasa.py`). Latest day is usually 2-3 days old.
- Also working without keys: Open-Meteo ERA5 archive, OpenTopoData SRTM, ISRIC SoilGrids (returns `null` on urban pixels and for some depths; 5-15 cm worked where 0-5 cm was null), USDA Soil Data Access (SSURGO, US only), Nominatim geocoding (India PIN `141001` and US ZIP `50010` resolve; needs a descriptive User-Agent and about 1 req/s), Planetary Computer Sentinel-2 STAC search and titiler statistics (field NDVI for a polygon in about 1s).
- OpenStreetMap Overpass is flaky (one mirror down, another needed a retry): always use a timeout, cache, and report "unknown" rather than "none".
- USDA CDL (CropScape) point query returned HTTP 500: out of scope.
- No free API found for India groundwater depth: state "not available".

## Submission requirements to respect while building

- Must use at least one NASA data source and list all resources. No NASA logos or branding anywhere in the app or deck.
- Disclose AI use: AI-assisted code, the in-app Gemini advisor, any AI-generated text. AI-generated images or video need a visible watermark.
- Demo: 7-slide-max deck or a video of 30 seconds or less, publicly accessible with no login. English on the submission page.
