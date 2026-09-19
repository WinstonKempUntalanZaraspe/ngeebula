"""STEP 2: choose which rulebook the scheduler follows, and how long it may think.

Each card says what that rulebook allows and what it charges. The point prices
quoted are the ones in PS1_README §2.5; they are labels printed next to a
choice, not a calculation — the score always comes back from the validator.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

import streamlit as st

from ui import state
from ui.state import (
    ECLO_POINTS,
    EXCESS_POINTS,
    LATE_POINTS,
    TIME_LIMIT_DEFAULT,
    TIME_LIMIT_MAX,
    TIME_LIMIT_MIN,
)

#: badge colour per kind of allowance
_BADGE = {"no": "red", "cost": "orange", "free": "green"}

Allow = Tuple[str, str]  # (text, kind)

RULES: List[Dict[str, object]] = [
    {
        "id": "A",
        "title": "A · Track limits fixed",
        "one_line": "Never overbook a spot. Jobs may finish late.",
        "late": ("Allowed, costs points", "cost"),
        "eclo": ("Not allowed", "no"),
        "extra": ("Not allowed", "no"),
        "use_when": "Track time cannot be stretched: no extra nights, no early closures.",
        "example": (
            "Two jobs want the hub tunnel in week 10. One moves to week 11 and its "
            "contract finishes 7 days late."
        ),
    },
    {
        "id": "B",
        "title": "B · Deadlines fixed",
        "one_line": "Nobody finishes late. Buy track time instead.",
        "late": ("Not allowed at all", "no"),
        "eclo": (f"{ECLO_POINTS} points each", "cost"),
        "extra": (f"{EXCESS_POINTS} points each, no cap", "cost"),
        "use_when": (
            "Contract dates are non-negotiable and you want to see what hitting them costs."
        ),
        "example": (
            "Both jobs stay in week 10. The hub tunnel goes 1 over its limit, so the plan "
            f"pays {EXCESS_POINTS} points instead."
        ),
    },
    {
        "id": "C",
        "title": "C · Balanced",
        "one_line": "A little of both. The cheapest mix wins.",
        "late": ("Allowed, costs points", "cost"),
        "eclo": (f"{ECLO_POINTS} points each", "cost"),
        "extra": (f"{EXCESS_POINTS} points each, max 1 per spot per week", "cost"),
        "use_when": "Normal operations: some slack exists and some dates can slip a little.",
        "example": (
            f"A Priority 1 job would be a week late (about {LATE_POINTS['1'] * 7} points). "
            f"One longer night costs {ECLO_POINTS}, so the scheduler uses the longer night."
        ),
    },
]


def _allowance(label: str, allow: Allow) -> None:
    text, kind = allow
    st.markdown(f":gray[{label}]  \n:{_BADGE[kind]}-badge[{text}]")


def render() -> None:
    scenario = st.session_state["scenario"]

    st.subheader("Choose the planning rules")
    st.write("Pick the rulebook that matches how much flexibility you really have tonight.")

    columns = st.columns(3, gap="medium")
    for column, rule in zip(columns, RULES):
        chosen = rule["id"] == scenario
        with column, st.container(border=True, height="stretch"):
            st.markdown(f"**{rule['title']}**")
            st.caption(str(rule["one_line"]))
            _allowance("Finishing late", rule["late"])  # type: ignore[arg-type]
            _allowance("Longer nights (ECLO)", rule["eclo"])  # type: ignore[arg-type]
            _allowance("Extra nights over a spot's limit", rule["extra"])  # type: ignore[arg-type]
            st.space("small")
            if chosen:
                st.button(
                    "Selected",
                    key=f"pick_{rule['id']}",
                    type="primary",
                    disabled=True,
                    icon=":material/check:",
                    width="stretch",
                )
            elif st.button(
                f"Use scenario {rule['id']}",
                key=f"pick_{rule['id']}",
                width="stretch",
            ):
                st.session_state["scenario"] = rule["id"]
                st.rerun()

    picked = next(r for r in RULES if r["id"] == scenario)

    st.space("medium")
    with st.container(border=True):
        st.markdown(f"**Why {picked['id']}?**")
        st.markdown(f"**Use when** {picked['use_when']}")
        st.markdown(f"**Example** {picked['example']}")
        st.markdown(
            f"**Late day costs** {LATE_POINTS['1']} points for a Priority 1 contract, "
            f"{LATE_POINTS['2']} for Priority 2, {LATE_POINTS['3']} for Priority 3, for every "
            "day past the deadline. A lower total score is better."
        )

    st.space("medium")
    st.subheader("How long may the scheduler think?")
    st.write(
        "More time usually means a lower score. It stops early if it proves the answer "
        "cannot be beaten."
    )
    # Standard Streamlit pattern: a constant default on the widget, a key so the
    # user's choice survives reruns, and NOTHING else ever assigns to that key
    # (Start over pops it instead). Passing a session-state value as `value=`,
    # or pre-seeding the key, made the +/- buttons snap back on the first click.
    st.number_input(
        "Seconds",
        min_value=TIME_LIMIT_MIN,
        max_value=TIME_LIMIT_MAX,
        value=TIME_LIMIT_DEFAULT,
        step=5,
        width=240,
        help=f"Between {TIME_LIMIT_MIN} and {TIME_LIMIT_MAX}. 60 is a good default for an instance this size.",
        key="time_limit",
    )

    st.space("medium")
    with st.container(horizontal=True, horizontal_alignment="distribute"):
        if st.button("Back: change files", icon=":material/arrow_back:"):
            state.go("upload")
        if st.button(
            "Build the schedule",
            type="primary",
            icon=":material/play_arrow:",
        ):
            state.go("running")
