"""FieldShift Maestro UI. Run: streamlit run app.py"""
import queue
import threading

import folium
import pandas as pd
import streamlit as st
from folium.plugins import Draw
from streamlit_folium import st_folium

from maestro import advisor, climate, geo, gibs, llm
from maestro import providers as pv
from maestro.reference import CROPS
from maestro.http import SourceError
from maestro.orchestrator import DONE, run_pipeline

st.set_page_config(page_title="FieldShift Maestro", page_icon="🛰️", layout="wide")
ICON = {"start": "▶️", "ok": "✅", "warn": "⚠️", "fail": "❌"}
PROV_BADGE = {"live": "🟢 live", "modeled": "🟡 modeled", "reference": "🔵 reference", "heuristic": "🟠 heuristic", "missing": "🔴 missing"}
# Demo places. The Punjab point is farmland (a city-centre PIN has no soil data); Ames is looked up by ZIP.
PUNJAB_DEMO = dict(lat=30.62, lon=75.95, label="Farmland near Khanna, Punjab, India", cc="in", country="India", state="Punjab")
DEMO_PLACES = {"Punjab, India": None, "Ames, Iowa, USA": "50010"}
LANG_LABEL = {"en": "English", "hi": "हिन्दी"}


@st.cache_resource
def get_chain() -> llm.LLMChain:
    """One shared LLM chain (circuit breakers and rate budgets are process-wide, as the providers' limits are)."""
    return llm.build_chain()


@st.cache_resource
def warm_demo() -> bool:
    """Pre-fetch the Punjab demo field once per server process so the first demo click is fast (real data, cached)."""
    def work():
        la, lo = PUNJAB_DEMO["lat"], PUNJAB_DEMO["lon"]
        for fn, args in ((climate.history, (la, lo)), (pv.soil_for, (la, lo, "ROW")), (pv.terrain, (la, lo))):
            try:
                fn(*args)
            except Exception:  # warming is best effort
                pass
    threading.Thread(target=work, daemon=True).start()
    return True


warm_demo()

ss = st.session_state
ss.setdefault("pin", None)          # dict(lat, lon, label, cc)
ss.setdefault("ring", None)         # drawn polygon ring [(lon, lat), ...]
ss.setdefault("view", None)         # dict(center=[lat, lon], zoom=int)
ss.setdefault("results", [])        # geocode candidates
ss.setdefault("last_click", None)
ss.setdefault("result", None)
ss.setdefault("field", None)
ss.setdefault("chat", [])           # list of dict(role, content, meta)
ss.setdefault("briefs", {})         # (result_id, lang) -> advisor brief
ss.setdefault("result_id", 0)
ss.setdefault("lang", "en")
ss.setdefault("pending_q", None)


def set_pin(lat, lon, label, cc=None, country=None, state=None):
    ss.pin = dict(lat=lat, lon=lon, label=label, cc=cc, country=country, state=state)
    ss.ring = None
    ss.view = dict(center=[lat, lon], zoom=16)


st.markdown("""
<style>
.fs-hero {border:1px solid #243256; border-left:4px solid #fc3d21; border-radius:10px; padding:16px 22px; margin-bottom:14px;
          background:radial-gradient(1200px 220px at 85% -40%, #1d3b8a 0%, rgba(10,16,32,0) 70%), linear-gradient(135deg,#0d1a3d 0%,#0a1020 100%);}
.fs-hero h1 {margin:0; padding:0; font-size:2rem; letter-spacing:.02em; color:#fff;}
.fs-hero .tag {margin:2px 0 8px 0; font-size:.78rem; letter-spacing:.18em; text-transform:uppercase; color:#8fb3ff;}
.fs-hero p {margin:0; color:#c3cde4; font-size:.95rem;}
.fs-chips {margin-top:10px;} .fs-chips span {display:inline-block; margin:0 6px 4px 0; padding:2px 10px; border:1px solid #2c4079; border-radius:999px;
          font-size:.74rem; color:#a9c1f5; background:rgba(76,141,255,.08);}
[data-testid="stSidebar"] {border-right:1px solid #1c2747;}
[data-testid="stSidebar"] h2 {font-size:.95rem; letter-spacing:.12em; text-transform:uppercase; color:#8fb3ff;}
[data-testid="stVerticalBlockBorderWrapper"] {border-color:#243256;}
[data-testid="stHeading"] h3 {letter-spacing:.04em;}
.fs-foot {margin-top:28px; padding-top:10px; border-top:1px solid #1c2747; font-size:.75rem; color:#7f8db0;}
</style>
<div class="fs-hero">
  <div class="tag">NASA Space Apps Challenge 2026 &middot; Field Shift</div>
  <h1>🛰️ FieldShift Maestro</h1>
  <p>Find your farm, mark your field, and compare three crop rotations using live NASA data, FAO crop requirements and live market prices.
     Where a number is not available, it is left out, never guessed.</p>
  <div class="fs-chips"><span>NASA POWER</span><span>NASA CMR</span><span>NASA GIBS</span><span>Live data, no mocks</span><span>AI explains, never invents numbers</span></div>
</div>
""", unsafe_allow_html=True)

# ----------------------------------------------------------------------------- sidebar
with st.sidebar:
    st.header("1. Where is your farm?")
    with st.form("search", clear_on_submit=False):
        q = st.text_input("Address, village, PIN code or ZIP", placeholder="e.g. 141001 or Khanna, Punjab")
        found = st.form_submit_button("Search", width="stretch")
    if found:
        try:
            with st.spinner("Searching..."):
                ss.results = geo.geocode(q)
            if not ss.results:
                st.warning("No match. Try a nearby town or a PIN/ZIP code.")
        except SourceError as e:
            st.error(f"Search is unavailable right now ({e}). Click the map to place the pin instead.")
    if ss.results:
        idx = st.selectbox("Pick the match", range(len(ss.results)), format_func=lambda i: ss.results[i].name[:70])
        if st.button("Use this place", width="stretch"):
            p = ss.results[idx]
            set_pin(p.lat, p.lon, p.name, p.country_code, p.country, p.state)
            st.rerun()
    st.caption("Quick demo:")
    c1, c2 = st.columns(2)
    for col, (label, code) in zip((c1, c2), DEMO_PLACES.items()):
        if col.button(label.split(",")[0], width="stretch"):
            if code is None:
                d = PUNJAB_DEMO
                set_pin(d["lat"], d["lon"], d["label"], d["cc"], d["country"], d["state"])
                st.rerun()
            try:
                p = geo.geocode(code)[0]
                set_pin(p.lat, p.lon, p.name, p.country_code, p.country, p.state)
                st.rerun()
            except (SourceError, IndexError):
                st.error("Search unavailable. Click the map instead.")

    st.header("2. Mark your field")
    mode = st.radio("On the map", ["📍 Drop pin (click)", "✏️ Draw boundary"], label_visibility="collapsed")
    st.caption("A PIN code covers several km, so click the exact spot. With no boundary we analyse a 150 m circle (about 7 ha).")

    st.header("3. Your crop")
    crop = st.selectbox("Current crop", list(CROPS), format_func=lambda k: CROPS[k].name)
    priority = st.radio("Main priority", ["Water", "Soil", "Income"], horizontal=True)
    irrigated = st.toggle("I can irrigate this field", value=False,
                          help="Off: scores assume rain only. On: water shortage is not counted against crops, only temperature.")

    st.header("Map layers (NASA GIBS)")
    layer_keys = [k for k in gibs.LAYERS if st.checkbox(gibs.LAYERS[k][0], value=(k == "ndvi"), key=f"lyr_{k}")]
    simulate_fail = st.toggle("Simulate NASA outage", help="See how the app degrades when data sources fail")

    st.header("Advisor")
    ss.lang = st.radio("Advisor language", list(LANG_LABEL), format_func=LANG_LABEL.get, horizontal=True, index=list(LANG_LABEL).index(ss.lang))
    with st.expander("AI provider status"):
        chain_status = get_chain().status()
        st.dataframe(pd.DataFrame(chain_status or [{"provider": "none", "state": "no API key configured: template text is used"}]),
                     hide_index=True, width="stretch")

# ----------------------------------------------------------------------------- map
pin = ss.pin
center = ss.view["center"] if ss.view else [20.6, 78.9]  # India overview until a place is chosen
zoom = ss.view["zoom"] if ss.view else 4
m = folium.Map(location=center, zoom_start=zoom, tiles=None, control_scale=True)
folium.TileLayer("OpenStreetMap", name="Streets").add_to(m)
folium.TileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
                 attr="Imagery © Esri, Maxar, Earthstar Geographics", name="Satellite imagery", show=True).add_to(m)
for k in layer_keys:
    url, zmax, d = gibs.tile_url(k)
    folium.TileLayer(url, attr="NASA GIBS", name=f"{gibs.LAYERS[k][0]} · {d}", overlay=True, opacity=0.65,
                     max_native_zoom=zmax, max_zoom=19).add_to(m)
if pin:
    folium.Marker([pin["lat"], pin["lon"]], tooltip="Your pin", icon=folium.Icon(color="green", icon="leaf")).add_to(m)
    if not ss.ring:
        folium.Polygon(geo.circle_ring(pin["lat"], pin["lon"]), color="#4c8dff", weight=2, fill=True, fill_opacity=0.15,
                       tooltip="Default analysis area (150 m)").add_to(m)
if ss.ring:
    folium.Polygon([(lat, lon) for lon, lat in ss.ring], color="#d95f02", weight=3, fill=True, fill_opacity=0.2, tooltip="Your field").add_to(m)
if mode.startswith("✏️"):
    Draw(export=False, draw_options={"polyline": False, "circle": False, "marker": False, "circlemarker": False,
                                     "polygon": True, "rectangle": True}, edit_options={"edit": False}).add_to(m)
folium.LayerControl(collapsed=True).add_to(m)

out = st_folium(m, height=520, use_container_width=True, key="map",
                returned_objects=["last_clicked", "last_active_drawing", "zoom", "center"])
if out:
    if out.get("zoom") and out.get("center"):
        ss.view = dict(center=[out["center"]["lat"], out["center"]["lng"]], zoom=out["zoom"])
    if mode.startswith("📍") and out.get("last_clicked") and out["last_clicked"] != ss.last_click:
        ss.last_click = out["last_clicked"]
        c = out["last_clicked"]
        where = geo.reverse(c["lat"], c["lng"])
        set_pin(c["lat"], c["lng"], f"Pin at {c['lat']:.4f}, {c['lng']:.4f}", where["cc"], where["country"], where["state"])
        ss.view = dict(center=[c["lat"], c["lng"]], zoom=out.get("zoom") or 16)
        st.rerun()
    draw = out.get("last_active_drawing")
    if mode.startswith("✏️") and draw and draw.get("geometry", {}).get("type") == "Polygon":
        new_ring = [tuple(p) for p in draw["geometry"]["coordinates"][0]]
        if new_ring != ss.ring:
            ss.ring = new_ring
            st.rerun()

# ----------------------------------------------------------------------------- field summary + run
field, field_err = None, None
if ss.ring:
    try:
        field = geo.make_field(ss.ring, "drawn", pin["cc"] if pin else None)
    except geo.GeometryError as e:
        field_err = str(e)
elif pin:
    field = geo.field_from_pin(pin["lat"], pin["lon"], country_code=pin["cc"])

left, right = st.columns([3, 1])
with left:
    if field_err:
        st.error(field_err)
    elif field:
        kind = "your drawn field" if field.source == "drawn" else "a 150 m circle around your pin"
        st.success(f"Analysing {kind}: **{field.area_ha:.1f} ha** near {field.lat:.4f}, {field.lon:.4f} · region tier "
                   f"**{'US (SSURGO soil)' if field.tier == 'US' else 'Global (SoilGrids soil)'}**")
    else:
        st.info("Search for a place, or click the map to drop a pin.")
with right:
    go = st.button("Analyze field", type="primary", width="stretch", disabled=field is None)

if go and field:
    ctx = dict(lat=field.lat, lon=field.lon, tier=field.tier, area_ha=field.area_ha, source=field.source, crop=crop, priority=priority,
               irrigated=irrigated, country=pin.get("country") if pin else None, state=pin.get("state") if pin else None)
    q: queue.Queue = queue.Queue()
    run_pipeline(ctx, q, simulate_fail)
    st.subheader("Live activity")
    log_box = st.empty()
    lines, result = [], None
    while True:
        item = q.get()
        if isinstance(item, tuple) and item[0] == DONE:
            result = item[1]
            break
        lines.append(f"`{item['t']:7.0f} ms` {ICON[item['status']]} **{item['agent']}** · {item['msg']}")
        log_box.markdown("\n\n".join(lines))
    ss.result, ss.field, ss.ctx = result, field, ctx
    ss.result_id += 1
    ss.chat, ss.pending_q = [], None
    if result is None:
        st.error("Analysis failed. See the log above.")

# ----------------------------------------------------------------------------- results
RESULT_KEYS = {"nasa", "soil", "terrain", "water", "era5", "history", "evals", "brief", "total_ms"}
if ss.result is not None and not (isinstance(ss.result, dict) and RESULT_KEYS <= set(ss.result)):
    # A tab kept open across a code update can hold a result in an older shape. Drop it instead of crashing.
    ss.result, ss.chat = None, []
    st.warning("The app was updated while this page was open, so the previous result was cleared. Press **Analyze field** again.")
result = ss.result
if result:
    st.divider()
    brief_box = st.empty()
    brief_key = (ss.result_id, ss.lang)
    brief = ss.briefs.get(brief_key)

    def show_brief(b):
        with brief_box.container():
            if b is None:
                st.info(result["brief"]["text"])
                st.caption("⏳ Writing the AI advisor note...")
            else:
                st.info(b["text"])
                who = "AI advisor" if b["status"] == "ai" else "Computed summary (template)"
                via = f" · {b['source']} ({b['model']})" if b["status"] == "ai" else ""
                st.caption(f"{who}{via} · {b['note']}. Pipeline time {result['total_ms']:.0f} ms.")
    show_brief(brief)

    st.subheader("Data status")
    sources = result["nasa"]["sources"]
    cols = st.columns(4)
    for i, (name, s) in enumerate(sources.items()):
        icon = {"live": "🟢", "empty": "🟡"}.get(s["status"], "🔴")
        cols[i % 4].metric(name, f"{icon} {s['status']}", f"{s['ms']:.0f} ms", delta_color="off")
        cols[i % 4].caption(s["detail"][:90])

    st.subheader("Your field")
    c1, c2, c3 = st.columns(3)
    soil, terr, water = result["soil"], result["terrain"], result["water"]
    with c1.container(border=True):
        st.markdown("**Soil**")
        if soil and soil.get("available"):
            st.write(f"{soil['texture'].title()} · clay {soil['clay_pct']}% · sand {soil['sand_pct']}%")
            if soil.get("drainage"):
                st.write(f"Drainage: {soil['drainage']} · hydrologic group {soil['hydgrp']}")
            if soil.get("organic_carbon_g_kg") is not None:
                st.write(f"Organic carbon: {soil['organic_carbon_g_kg']} g/kg")
            st.caption(f"{soil['source']} · {soil['resolution']}. {soil['note']}")
        else:
            st.write("Not available for this point." if soil else "Source unreachable right now.")
            st.caption((soil or {}).get("note", "Soil lookup failed; soil-dependent scores have lower confidence. Try again in a few minutes."))
    with c2.container(border=True):
        st.markdown("**Terrain**")
        if terr and terr.get("available"):
            st.write(f"Elevation {terr['elevation_m']} m · slope {terr['slope_pct']}% ({terr['slope_class']})")
            st.caption(f"{terr['source']} · {terr['resolution']}")
        else:
            st.write("Not available.")
    with c3.container(border=True):
        st.markdown("**Nearby water**")
        if water:
            d = water["nearest_distance_m"]
            st.write(f"Irrigation access: **{water['irrigation_access']}**" + (f" · water within ~{d} m" if d else ""))
            for n in water["nearby"][:3]:
                st.write(f"• {n['kind']}{' ' + n['name'] if n.get('name') else ''}: {n['distance_m']} m")
            st.caption(water["note"])
        else:
            st.write("Unavailable right now.")

    st.subheader("Rotation options")
    best = result["brief"]["best"]
    was_irrigated = (ss.get("ctx") or {}).get("irrigated", False)
    cards = st.columns(3)
    for col, e in zip(cards, result["evals"]):
        with col.container(border=True):
            st.markdown(f"### {e['crop']}" + ("  ⭐" if e["slot"] == best else ""))
            st.caption(e["slot"])
            st.write(e["blurb"])
            st.metric("Total score", f"{e['total']:.2f}")
            c = e["confidence"]
            st.markdown(f"**Confidence {c:.2f}** " + ("🟢 High" if c >= 0.75 else "🟡 Medium" if c >= 0.5 else "🔴 Low · verify locally"))
            for mname, v in e["metrics"].items():
                if v is None:
                    st.caption(f"{mname}: not enough live data")
                else:
                    st.progress(v, text=f"{mname}: {v:.2f} ± {e['band']:.2f}")
            sim = e["sim"]
            if sim.get("available"):
                vals = sim["irrigated" if was_irrigated else "rainfed"]
                st.caption(f"Season stress index per year, {'irrigated' if was_irrigated else 'rainfed'} (1 = ideal). Sowing about {sim['start_label']}.")
                st.bar_chart(pd.DataFrame({"index": vals}, index=[str(y) for y in sim["years"]]), height=110, color="#4c8dff")
            for n in e["notes"]:
                st.caption(f"• {n}")

    with st.expander("How local is this? (inputs, provenance and resolution)"):
        rows = [dict(input=k, value=f"{v['value']} {v['unit']}", provenance=PROV_BADGE.get(v["provenance"], v["provenance"]), source=v["source"], note=v["note"])
                for k, v in result["nasa"]["features"].items()]
        for name, d in (("soil", soil), ("terrain", terr), ("water", water), ("ERA5", result["era5"])):
            if d and d.get("available"):
                rows.append(dict(input=name, value="see above", provenance=PROV_BADGE.get(d["provenance"], d["provenance"]),
                                 source=d["source"], note=f"resolution: {d['resolution']}"))
        st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        st.caption("Rain and temperature come at about 10-50 km resolution, so they describe your region. "
                   "Soil, terrain, vegetation and water are specific to your field. SMAP/ECOSTRESS rows show satellite pass freshness only.")

    # ------------------------------------------------------------------------- chat
    @st.fragment
    def chat_panel():
        st.subheader("💬 Ask the advisor")
        st.caption("Ask what-if questions. Numbers always come from the app's calculations on the real 35-year weather record; the AI only explains them. "
                   "Questions are sent to a free AI service together with field-level numbers (no name, address or exact location).")
        ex = ["What if rain drops 30%?", "What if it gets 2 degrees hotter?", "What if I can irrigate?", "अगर बारिश 30% कम हो जाए तो?"]
        for c, q_ in zip(st.columns(len(ex)), ex):
            if c.button(q_, width="stretch", key=f"ex_{q_}"):
                ss.pending_q = q_
        history_box = st.container()
        typed = st.chat_input("Ask about your field, e.g. what if rain drops 30%?")
        question, ss.pending_q = typed or ss.pending_q, None
        if question:  # processed BEFORE the history is drawn, so the new turn appears without any rerun
            ss.chat.append(dict(role="user", content=question))
            with st.spinner("Re-scoring with the real weather record and asking the advisor..."):
                out = advisor.answer(get_chain(), ss.ctx, ss.result, question)
            who = f"{out['source']} ({out['model']})" if out["status"] == "ai" else "computed text (no AI used)"
            ss.chat.append(dict(role="assistant", content=out["text"], meta=dict(
                rows=out.get("rows"), scenario=advisor.describe(out["params"]) if out.get("rows") else "",
                how=f"{who} · {out['note']} · question read by {out.get('parsed_by', 'rules')}")))
        with history_box:
            for m in ss.chat:
                with st.chat_message(m["role"]):
                    st.markdown(m["content"])
                    meta = m.get("meta") or {}
                    if meta.get("scenario"):
                        st.caption(f"What-if applied: {meta['scenario']}")
                    if meta.get("rows"):
                        st.dataframe(pd.DataFrame(meta["rows"]), hide_index=True, width="stretch")
                    if meta.get("how"):
                        st.caption(meta["how"])

    chat_panel()

    with st.expander("Data sources and AI use (disclosure)"):
        st.markdown(
            "**NASA data used:** POWER daily weather and 35-year history (rain and temperature); CMR granule search for SMAP, ECOSTRESS, GPM IMERG and HLS "
            "pass freshness; GIBS map layers (MODIS NDVI, SMAP soil moisture, IMERG rain, land surface temperature). SMAP and ECOSTRESS are shown as satellite pass freshness "
            "and map layers only; their pixel values are not analysed.\n\n"
            "**Other open data:** FAO EcoCrop crop requirements; FAO-56 crop coefficients; ISRIC SoilGrids and USDA SSURGO soil; SRTM elevation via OpenTopoData; "
            "OpenStreetMap and JRC Global Surface Water for nearby water; Open-Meteo ERA5; Nominatim geocoding; futures prices via Yahoo Finance; "
            "FAOSTAT yields via Our World in Data; Agmarknet mandi prices via data.gov.in; Esri World Imagery basemap.\n\n"
            "**AI use:** the advisor text and chat are written by Google Gemini (free tier), with OpenRouter free models as a fallback. The AI never produces numbers: it receives "
            "calculated values and every number in its reply is checked against them (otherwise a plain computed summary is shown). Parts of this app's code were written with "
            "an AI coding assistant. Free-tier AI providers may use prompts to improve their services, so only field-level numbers and the state and country name are sent.")

st.markdown('<div class="fs-foot"><b>Team: The Orchestrator</b> &middot; <b>NASA Space Apps Challenge 2026 &middot; '
            'Challenge: Field Shift: Adapting Farms with NASA Data</b><br>'
            'An independent entry, not affiliated with or endorsed by NASA. '
            'Decision support only: check locally with an agronomist before acting.</div>', unsafe_allow_html=True)

# ----------------------------------------------------------------------------- AI brief (after everything else is on screen)
if ss.result and ss.briefs.get((ss.result_id, ss.lang)) is None:
    ss.briefs[(ss.result_id, ss.lang)] = advisor.write_brief(get_chain(), ss.ctx, ss.result, ss.lang, ss.result["brief"]["text"])
    show_brief(ss.briefs[(ss.result_id, ss.lang)])
