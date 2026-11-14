"""The UI must render without exceptions in its initial state (no network needed)."""
from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "app.py")


def test_app_renders_initial_state():
    at = AppTest.from_file(APP, default_timeout=30).run()
    assert not at.exception, [e.value for e in at.exception]
    assert any("FieldShift Maestro" in m.value for m in at.markdown)       # the hero banner
    # the Analyze button exists and is disabled until a place is chosen
    analyze = [b for b in at.button if b.label == "Analyze field"]
    assert analyze and analyze[0].disabled


def test_stale_result_from_older_code_is_cleared_not_a_crash():
    at = AppTest.from_file(APP, default_timeout=30)
    at.session_state["result"] = {"nasa": {}, "evals": []}          # shape from an older version: no soil, terrain, ...
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    assert at.session_state["result"] is None and any("updated" in w.value for w in at.warning)
