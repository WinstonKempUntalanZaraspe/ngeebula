"""The page heading, the Night/Day switch and the step indicator.

The indicator is drawn like a train line: three stations, a line between them,
and the train sitting at the step you are on. It is built from native Streamlit
elements and Markdown colour directives, so it follows whichever theme is live.

This module also owns the chart typography constants. Everything on this screen
is read by a works controller on a laptop at arm's length at 2 AM, so the floor
for chart text is 15 px and for body text 17 px (``baseFontSize`` in
``.streamlit/config.toml``).
"""

from __future__ import annotations

import streamlit as st

from ui.state import STEPS

# --------------------------------------------------------------------------- #
# chart typography — imported by gantt.py, heatmap.py and explain.py
# --------------------------------------------------------------------------- #

#: Base text inside a chart (hover text, in-marker letters, bar labels).
CHART_FONT_PX = 16
#: Axis tick labels (week headers, row names down the left).
AXIS_FONT_PX = 15
#: Legend / key text under a chart.
LEGEND_FONT_PX = 14
#: Minimum height of one contract row in the Gantt.
CONTRACT_ROW_PX = 34
#: Minimum height of one job row in the Gantt.
JOB_ROW_PX = 30
#: Minimum height of one row in the heat-map.
HEAT_ROW_PX = 28
#: Smallest a clickable marker may be.
MARKER_PX = 16

# --------------------------------------------------------------------------- #
# the Night / Day switch
# --------------------------------------------------------------------------- #

#: The two palettes the switch writes. "Night" mirrors .streamlit/config.toml.
PALETTES: dict[str, dict[str, str]] = {
    "Night": {
        "theme.base": "dark",
        "theme.primaryColor": "#3b82f6",
        "theme.backgroundColor": "#0f1115",
        "theme.secondaryBackgroundColor": "#181b21",
        "theme.textColor": "#e8eaea",
    },
    "Day": {
        "theme.base": "light",
        "theme.primaryColor": "#1d4ed8",
        "theme.backgroundColor": "#ffffff",
        "theme.secondaryBackgroundColor": "#eef1f5",
        "theme.textColor": "#14171c",
    },
}

_DEFAULT_MODE = "Night"


def is_dark() -> bool:
    """True when the live theme is the dark one.

    Prefers Streamlit's supported ``st.context.theme``; falls back to whatever
    the switch last wrote, then to the dark default from config.toml.
    """
    try:
        kind = st.context.theme.type
        if kind in ("dark", "light"):
            return kind == "dark"
    except Exception:  # pragma: no cover - hosts without st.context.theme
        pass
    return st.session_state.get("theme_mode", _DEFAULT_MODE) == "Night"


def body_color() -> str:
    """Text colour for marks Plotly's Streamlit template does not reach."""
    return "#E8EAEA" if is_dark() else "#14171c"


def _apply(mode: str) -> None:
    """Push one palette into Streamlit's live config.

    CAVEAT — PRIVATE API. Streamlit 1.63 has no public runtime theme setter:
    ``st.context.theme`` only *reads* the active theme, and the supported
    switch (a ``[theme.light]`` + ``[theme.dark]`` pair in config.toml) lives
    in Streamlit's own ⋮ Settings menu, which is too well hidden for a 2 AM
    control room. So we write the theme options through ``st._config``. This
    works because Streamlit re-sends the theme with the NewSession message on
    every script run, so the next rerun picks the new palette up. It is a
    private module: if a future Streamlit renames it, the ``except`` below
    keeps the app running with the config.toml default instead of crashing.
    """
    try:
        for key, value in PALETTES[mode].items():
            st._config.set_option(key, value)
    except Exception:  # pragma: no cover - private API moved or renamed
        pass


def apply_saved_theme() -> None:
    """Re-apply the remembered choice. Call once, before anything is drawn."""
    _apply(st.session_state.get("theme_mode", _DEFAULT_MODE))


def _switch() -> None:
    """Draw the Night/Day control and act on a change."""
    current = st.session_state.get("theme_mode", _DEFAULT_MODE)
    choice = st.segmented_control(
        "Screen",
        options=["Night", "Day"],
        default=current,
        key="theme_switch",
        label_visibility="collapsed",
        help="Night is the dark control-room look. Day is for a bright office.",
    )
    if choice and choice != current:
        st.session_state["theme_mode"] = choice
        _apply(choice)
        st.rerun()


# --------------------------------------------------------------------------- #
# the header
# --------------------------------------------------------------------------- #

def header() -> None:
    title_area, switch_area = st.columns([5, 2], gap="medium", vertical_alignment="top")
    with title_area:
        st.title("Track Access Scheduler")
        st.caption(
            "Plans which contractor works on which stretch of track, each week, "
            "without breaking a safety rule or a deadline more than it has to."
        )
    with switch_area:
        with st.container(horizontal=True, horizontal_alignment="right"):
            _switch()


def step_train(step: int) -> None:
    """Three stations on a line. `step` is 1, 2 or 3."""
    columns = st.columns(len(STEPS), gap="small")
    for index, label in enumerate(STEPS):
        number = index + 1
        with columns[index]:
            if number < step:
                st.markdown(":green[━━━━━━━━━━━━━━━━━━━━━━━]")
                st.markdown(f":green[**● Step {number}**] · :green[{label}]")
            elif number == step:
                st.markdown(":blue[━━━━━━━━━━━━━━━━━━━━━━━]")
                st.markdown(f":blue[**🚆 Step {number}**] · **{label}**")
            else:
                st.markdown(":gray[━━━━━━━━━━━━━━━━━━━━━━━]")
                st.markdown(f":gray[○ Step {number} · {label}]")
    st.space("small")
