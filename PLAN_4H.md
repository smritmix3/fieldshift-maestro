# FieldShift Maestro: 4-Hour Build and Deploy Plan (v3, global with India and US tiers)

Submission closes **Sun Nov 15, 11:59 pm local (IST for an India event)**. This plan covers build, deploy and the submission artifacts in 4 hours.

## 1. Product in one paragraph

A farmer **draws their field on a map** (or searches their village), picks the current crop and a priority, and presses one button. Maestro pulls live NASA and partner data for that exact polygon, scores three rotation options, **stress-tests each one against 10+ past seasons**, shows what satellites saw on the field over the last year, and explains everything through a **chat advisor in English, Hindi or another Indian language**. Every number is traceable and checked.

## 2. Changes from the prototype

| Prototype | 4-hour version |
|---|---|
| Four rule-based "agents" | **One GenAI Advisor agent with tool calling**, backed by deterministic services |
| Preset lat/lon | **Drawable field boundary on a map** with NASA GIBS overlays, plus place search |
| Mock crop table | **India-first crop table** (kharif and rabi crops, sowing windows, MSP-style prices), each row with a source |
| POWER and CMR only | + ERA5, SRTM, SoilGrids (global soil), Sentinel-2 or HLS field NDVI, GIBS layers |
| One score per option | **Climate stress test**: each option replayed over past seasons |
| Template summary | LLM brief with **number-grounding validation** |
| Local only | Deployed, public, no login |

**Cut:** LangGraph, vector DB, separate FastAPI service (one Streamlit app), RAG agent (the crop table fits in the prompt), CDL, PRISM, NASS, WSS.

## 3. Data sources (verified live today unless noted)

| Source | Use for the drawn field | Status |
|---|---|---|
| NASA POWER | Rain (IMERG-based), temperature, solar, root-zone wetness, 40+ year history for the stress test | Verified |
| NASA CMR | Freshness of SMAP, ECOSTRESS, GPM, HLS passes over the field | Verified |
| NASA GIBS | Map overlays: NDVI, land surface temperature, SMAP, IMERG | Capabilities verified, **exact layer IDs to confirm at build** |
| ERA5 via Open-Meteo | Hourly soil moisture, ET₀, second opinion on rain | Verified |
| SRTM via OpenTopoData | Elevation, and slope from points sampled inside the polygon | Verified |
| ISRIC SoilGrids | Clay, sand, organic carbon for **India and everywhere else** | Verified: returns data on rural cropland, **null on urban pixels**, which we handle |
| Planetary Computer: Sentinel-2 (ESA), HLS (NASA) | **Field-level NDVI for the drawn polygon**, about 1 s per scene, no key | Sentinel-2 verified; HLS to test at build and preferred if it works, since it is NASA data |
| USDA SSURGO | US soil detail only | Verified, optional bonus for US points |
| Crop table | Rooting depth, water need, N fixation, season, sowing window, price band | Written during the build, cited per row |
| Agmarknet (market prices) | Live mandi prices | **Stretch**: needs a free data.gov.in key |
| SMAP pixel soil moisture | True SMAP values | **Stretch**: needs an Earthdata token. Default is ERA5 soil moisture labeled as modeled, plus SMAP pass freshness and the GIBS layer. |

Out of scope: CDL (my probe returned HTTP 500), PRISM, NAIP, NLCD, OpenET, GEDI, Meteomatics, Google Maps API.

## 4. Architecture

```
Streamlit app (one service, Hugging Face Space)
 ├─ Map             folium + Draw plugin: polygon/rectangle, GIBS overlay toggle, place search
 ├─ Data layer      parallel fetch, cache, timeouts, provenance tags
 ├─ Scoring engine  deterministic: 4 metrics x 3 rotations + confidence
 ├─ Stress test     replays each rotation over past seasons (POWER history)
 ├─ Field history   NDVI time series for the polygon (Sentinel-2 / HLS)
 ├─ Advisor (GenAI) tool calling; brief, chat, language choice
 └─ Validator       every number in LLM text must match tool output
```

Seamless flow: draw, press **Analyze**, and a live status shows each source finishing. Cards and charts render as soon as data is ready, and the LLM brief streams in afterwards. Results are cached per field, so changing the priority or asking a chat question does not refetch anything.

## 4b. Location input, small farms and country-adaptive data (added in v3)

**Decision: global, with two data tiers, demonstrated with one India farm and one US farm.** Not US-only, not India-only. Reasons:
- The challenge is global, so a tool that only works in the US scores lower on *impact* and *relevance*.
- NASA reviewers may well test with a US address, and the hackathon's resource list is US-heavy (soil, water, crop and price datasets). The US path has to be excellent.
- So the app picks the best sources for wherever the farm is. Only **soil**, **water** and the **crop table** differ by region. The NASA core is the same everywhere.

**How a farmer gives a location (small farms):**
1. Type an **address, village or PIN/ZIP code** (OpenStreetMap Nominatim). Verified: India PIN `141001` and US ZIP `50010` both resolve.
2. A **pin appears on the map**. A PIN code covers several km, so the farmer drags the pin onto the field and can optionally **draw the boundary**.
3. With no boundary, we analyze a **150 m radius circle** around the pin (about 7 ha), which suits a typical small farm.

**Honest resolution table (shown in the UI as "how local is this?"):**

| Input | Resolution | Is it field-specific? |
|---|---|---|
| Rain, temperature (POWER) | about 50 km | No: regional context |
| ERA5 soil moisture, ET₀ | about 10–25 km | No: regional context |
| SMAP, ECOSTRESS | 9 km / 70 m | Availability only in this build |
| Soil, India (SoilGrids) | 250 m | **Yes** (rural cropland), null on urban pixels |
| Soil, US (SSURGO) | field-scale polygons | **Yes** |
| Terrain (SRTM) | 30 m | **Yes** |
| NDVI (Sentinel-2) | 10 m | **Yes** |
| Nearby water (below) | exact distance | **Yes**, with caveats |

Climate inputs are regional for *any* farm, since no free global source is finer. The field-specific signal comes from soil, terrain, NDVI and water. The UI says this plainly, which supports trust.

**Nearby water sources (new):**

| Region | Source | What we report | Caveat |
|---|---|---|---|
| Everywhere | OpenStreetMap via Overpass (verified for Punjab) | Nearest river, canal, drain, pond or lake, with distance and type | OSM can be incomplete: absence means "none mapped", not "no water". The public Overpass server is sometimes slow or fails, so we use a timeout, a cache, and show "unknown" rather than "none". |
| US | USGS NWIS (verified: stream gauges exist near Ames, IA) | Nearby gauges and groundwater wells with the latest reading | Readings are fetched at build time. |
| India groundwater | No free API found | Not shown. We say "groundwater depth not available". | Honest gap, listed as future work. |

Water access feeds the scoring: a canal or river within about 1 km raises irrigation feasibility for water-hungry crops.

**Country-adaptive tiers:**

| | Tier A: US | Tier B: rest of world (India demo) |
|---|---|---|
| Soil | SSURGO (drainage class, hydrologic group, texture) | SoilGrids (clay, sand, organic carbon) |
| Water | OSM + USGS NWIS | OSM |
| Crop table | corn, soybean, wheat, cotton, alfalfa, cover crops | kharif and rabi crops (rice, wheat, maize, cotton, moong, chickpea, mustard, bajra, and others) |
| Calendar | Spring or fall planting | Kharif (Jun–Oct) and rabi (Nov–Mar) |
| Economics | Placeholder bands, labeled | MSP-style bands, labeled, with a source to confirm |

Soil and water are behind small provider interfaces so the scoring code never knows which region it is in.

## 5. Value-add features (what makes this different from "anyone can build this")

I checked what would be insightful and feasible in the time. Recommended, in priority order:

1. **Climate Stress Test ("Time Machine")**: replay each rotation against every season since 1990 using NASA POWER history (dry years, floods, heat waves). Show "Option B beats the baseline in N of M past seasons", the worst-case season, and the regret if the season goes wrong. This turns a single guess into a *decision under uncertainty*, which is what judges mean by "make uncertainty visible" and "reveal tradeoffs". It needs no new data source.
2. **Field History from Space**: NDVI time series for the farmer's own polygon over the last 12 months. It shows when the field was bare, how many crop cycles it had, and whether the last season looked stressed. The scores use it (long bare periods raise erosion risk) and the advisor explains it. No account or key needed, and it works anywhere in India.
3. **Advisor in Indian languages**: Hindi, Punjabi, Marathi, Tamil and others, because Gemini handles them well. The prompt keeps numerals in ASCII so the validator still works. Cheap to build and a strong impact story for farmers who do not read English.
4. **India context built in**: kharif and rabi calendars with sowing windows, and the Punjab-style rice-wheat water problem as the demo story (shifting to maize, moong or cotton). The demo uses Indian locations, such as Ludhiana, Vidarbha and Marathwada.

Not recommended for 4 hours: WhatsApp or SMS delivery, yield prediction models (we have no ground truth to validate), live mandi prices (key needed). They go on the "future work" slide.

## 6. GenAI and the fallback chain

**Free tier only. No paid account needed.** You create a free key at aistudio.google.com (no card). Model names and free limits change, so I confirm them at build time and keep them in config.

Fallback chain, tried in order:

| # | Provider | Notes |
|---|---|---|
| 1 | Gemini Flash (free) | Primary, with function calling and JSON output |
| 2 | Gemini Flash-Lite (free) | A different model, which has its own quota bucket |
| 3 | OpenRouter free model | Separate vendor, so it survives a Gemini outage or key problem. Optional key. |
| 4 | Deterministic template | Always works, in English plus a small set of translated phrases |

How it stays efficient:
- **Circuit breaker per provider:** after a 429 or 5xx, that provider is skipped until a cooldown expires (using `Retry-After` when present). A failed provider costs no time on later requests.
- **Per-provider rate budget** tracked locally, so we stop *before* hitting the limit.
- **Short timeouts** (about 8 s) and one retry at most per provider.
- **Few calls per analysis:** one call for the brief and at most two per chat turn.
- **Cache by input hash:** repeated demo clicks cost zero calls.
- **Visible status:** a small label shows which provider answered, or "basic summary mode" for the template.
- **Privacy note in the UI:** on the free tier, prompts may be used by Google to improve products, so no personal data is sent, only field-level numbers. I'll disclose this.

## 7. Trust and robustness

- **Grounding validator:** numbers in LLM text must match tool outputs. Regenerate once, then fall back to the template. A "verified against data" badge shows on pass.
- **Provenance and confidence** on every input, carried through to the cards.
- **Source failures** degrade visibly and never crash. A missing layer lowers confidence and says why.
- **Input checks:** polygon size limits, ocean and invalid points, self-intersecting shapes, and the null urban-soil case.
- **Honest labeling:** modeled versus observed, and "decision support, not agronomic advice".
- **Tests:** scoring bounds, validator against invented numbers, fallback chain with simulated 429 and timeouts, stress test sanity, plus a live smoke test and an offline one.

## 8. Timeline (4 hours)

| Time | Work | Done when |
|---|---|---|
| 0:00–0:20 | Repo, config, **deploy an empty skeleton to a Space** | Public URL is live |
| 0:20–1:05 | Address/PIN/ZIP search, draggable pin, optional boundary draw, GIBS overlays; polygon to centroid, area, slope | Search "141001" and "50010", drag the pin, see area |
| 1:05–1:50 | Data layer with provider interfaces: POWER history, ERA5, SRTM, CMR, SoilGrids and SSURGO, OSM water and USGS NWIS, provenance and cache | Real data for one India and one US field, with tags |
| 1:50–2:20 | India and US crop tables, scoring, **stress test** | Three cards plus the past-seasons chart for both regions |
| 2:20–2:45 | **Field history NDVI** (Sentinel-2 only) | NDVI timeline renders |
| 2:45–3:20 | GenAI Advisor: brief, chat, English and Hindi, fallback chain, validator | Chat answers in Hindi with validated numbers, and survives a forced 429 |
| 3:20–3:45 | Failure drills, tests, redeploy | All drills pass on the live URL |
| 3:45–4:00 | Submission pack | Draft ready to paste |

**Cut order if time runs short:** field history NDVI, then OpenRouter tier, then USGS NWIS, then extra languages (start with English and Hindi only; HLS is dropped, Sentinel-2 only). The core flow (draw, live data, three rotations, stress test, validated brief) ships first. The stress test is the protected feature.

## 9. Submission pack

- 50–200 word summary, a **7-slide-max deck or 30-second video**, hosted publicly with no login.
- Public links to the app and the repo.
- **SI disclosure:** AI-assisted code, the in-app Gemini advisor and any AI-generated text. Any AI-generated image or video carries a visible watermark.
- **NASA resources used:** POWER, CMR, GIBS (plus HLS if it works). SMAP, GPM/IMERG and MODIS are described precisely as map layers or pass freshness, not pixel analysis, unless the stretch goal lands.
- **Other sources:** ERA5, SRTM, SoilGrids, Sentinel-2, OpenStreetMap geocoding.
- No NASA logos or branding in the app or deck.

## 10. Risks

| Risk | Mitigation |
|---|---|
| Free LLM quota hit during judging | The chain above, plus cache and pre-warmed demo fields |
| Free API slow or down | Cache, fallback and a visible status panel |
| Draw plugin reruns slow the UI | Return only the drawn shape from the map, and avoid re-running the analysis on pan or zoom |
| Hugging Face free-tier cold start | Keep-warm ping, deploy early |
| SoilGrids null at some points | Handled: lower confidence and say so |
| Overclaiming satellite use | Provenance tags and precise wording in the submission |

## 11. What I need from you

1. **A Gemini API key** (free, from aistudio.google.com). It goes in a local `.env` and a Space secret, and I won't commit it. An OpenRouter key is optional and only for tier 3.
2. **A Hugging Face account** (free) so you can create the Space. I will give you the exact steps.
3. A "go" to start. Tell me which of the value-add features to drop if you want the scope smaller.
