"""Track Access Scheduler — the Streamlit front end.

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
    page_title="Track Access Scheduler",
    page_icon="🚆",
    layout="wide",
)

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
