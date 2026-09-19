"""RailWise IQ — the Streamlit front end.

This is the file Streamlit Community Cloud runs. It is a three-step wizard:
load the 8 planning files, choose the rulebook, read the schedule. The solving
happens in ``app.pipeline``, which is the same code path the FastAPI backend
uses, so both front doors produce exactly the same answer.

Run it locally with:  py -m streamlit run streamlit_app.py
"""

from __future__ import annotations

import time

import streamlit as st

from ui import results, rules, run, state, theme, upload

st.set_page_config(
    page_title="RailWise IQ",
    page_icon="🚆",
    layout="wide",
)

# --------------------------------------------------------------------------- #
# responsive layout
# --------------------------------------------------------------------------- #
# One CSS block for the whole app. Desktop keeps Streamlit's own layout; the
# media queries below take over for a tablet (~768-1024 px) and a phone
# (~375-430 px), where a works controller is using a thumb, not a mouse.
RESPONSIVE_CSS = """
<style>
/* --- desktop baseline ------------------------------------------------- */
html, body, [data-testid="stAppViewContainer"] { font-size: 17px; }
[data-testid="stPlotlyChart"] { overflow-x: auto; }
[data-testid="stPlotlyChart"] > div { min-width: 320px; }

/* --- tablet and below: stop side-by-side columns squeezing ------------- */
@media (max-width: 900px) {
  [data-testid="stHorizontalBlock"] {
    flex-wrap: wrap !important;
    gap: 0.75rem !important;
  }
  [data-testid="stHorizontalBlock"] > [data-testid="column"],
  [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
    min-width: 100% !important;
    flex: 1 1 100% !important;
    width: 100% !important;
  }
  .stButton > button, .stDownloadButton > button, [data-testid="stBaseButton-secondary"],
  [data-testid="stBaseButton-primary"] {
    min-height: 48px;
  }
}

/* --- phone ------------------------------------------------------------- */
@media (max-width: 640px) {
  html, body, [data-testid="stAppViewContainer"] { font-size: 16px; }
  [data-testid="stAppViewContainer"] .block-container {
    padding-left: 0.75rem !important;
    padding-right: 0.75rem !important;
    padding-top: 2.5rem !important;
  }
  /* a button inside a horizontal container is a flex item: give it the row */
  [data-testid="stHorizontalBlock"] > [data-testid="stElementContainer"]:has(.stButton),
  [data-testid="stHorizontalBlock"] > [data-testid="stElementContainer"]:has(.stDownloadButton),
  [data-testid="stHorizontalBlock"] > [data-testid="stElementContainer"]:has(.stFormSubmitButton) {
    flex: 1 1 100% !important;
    min-width: 100% !important;
    width: 100% !important;
  }
  .stButton, .stDownloadButton, .stFormSubmitButton {
    width: 100% !important;
    flex: 1 1 100% !important;
  }
  .stButton > button, .stDownloadButton > button, .stFormSubmitButton > button {
    width: 100% !important;
    min-height: 48px !important;
    font-size: 16px !important;
  }
  /* big enough to hit: inputs, tabs, expander headers */
  .stSelectbox div[data-baseweb="select"] > div,
  .stMultiSelect div[data-baseweb="select"] > div,
  .stNumberInput input, .stTextInput input { min-height: 44px; }
  [data-testid="stTabs"] button { min-height: 44px; padding: 0 0.9rem; }
  [data-testid="stExpander"] summary { min-height: 44px; font-size: 16px; }
  /* the three step stations sit one under the other, still readable */
  [data-testid="stHorizontalBlock"] { gap: 0.25rem !important; }
  [data-testid="stMetricValue"] { font-size: 1.5rem; }
  /* explain panel body text stays at a readable 16 px */
  [data-testid="stExpander"] p, [data-testid="stMarkdownContainer"] p,
  [data-testid="stMarkdownContainer"] li { font-size: 16px; }
  h1 { font-size: 1.6rem !important; }
  h2 { font-size: 1.3rem !important; }
}
</style>
"""
st.markdown(RESPONSIVE_CSS, unsafe_allow_html=True)

state.init()

# Re-apply the remembered Night/Day choice before anything is drawn.
theme.apply_saved_theme()

try:  # owned by another module; the app must still run before it lands
    from ui.errors import render_global_notices

    render_global_notices()
except ImportError:
    pass


# --------------------------------------------------------------------------- #
# the loading screen between steps
# --------------------------------------------------------------------------- #

#: One line per step, in the controller's own words.
_LOADING_LINE = {
    "upload": "Pulling in to Step 1 — the planning files…",
    "rules": "Loading the rules…",
    "running": "Clearing the line for the scheduler…",
    "failed": "Bringing back what went wrong…",
    "result": "Rolling the schedule in…",
}

#: Long enough to read, short enough not to be in the way.
_LOADING_SECONDS = 0.35


def show_train(phase_name: str) -> None:
    """Flash a full-width train placeholder when the step has just changed.

    The "flag set on click" is the step itself: every Next / Back / Build /
    Start over button ends in ``state.go(...)``, which changes ``phase``. We
    remember the step we last drew, so a change means a transition. The
    placeholder is drawn into an ``st.empty()`` and cleared again in the same
    run, so it costs a third of a second and no extra rerun.
    """
    if st.session_state.get("_drawn_phase") == phase_name:
        return
    st.session_state["_drawn_phase"] = phase_name

    holder = st.empty()
    with holder.container(border=True, horizontal_alignment="center"):
        st.markdown("# 🚆")
        st.subheader(_LOADING_LINE.get(phase_name, "Loading…"))
        with st.spinner("One moment…"):
            time.sleep(_LOADING_SECONDS)
    holder.empty()


theme.header()
theme.step_train(state.STEP_OF.get(st.session_state["phase"], 1))

phase = st.session_state["phase"]
show_train(phase)

if phase == "upload":
    upload.render()
elif phase == "rules":
    rules.render()
elif phase == "running":
    run.render_running()
elif phase == "failed":
    run.render_failed()
elif phase == "result":
    results.render()
else:  # pragma: no cover - defensive
    state.go("upload")
