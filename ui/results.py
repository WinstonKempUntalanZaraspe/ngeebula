"""STEP 3: the schedule.

Was it legal, what did it score, who is late, and the two grids. Every number
on this screen comes from the pipeline's own report — ``result["report"]`` and
``result["results"]``. Nothing here re-checks a rule or recomputes a score; it
reads what the validator already decided and lays it out.

The two grids and the explain panel are owned by another module. They are
imported lazily so this screen still runs before they exist.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd
import streamlit as st

from ui import state
from ui.state import (
    ECLO_POINTS,
    EXCESS_POINTS,
    FILE_BLURB,
    SUBMISSION_FILES,
)

_COMING_SOON = "The grid view is coming soon."


# --------------------------------------------------------------------------- #
# the week-range control
# --------------------------------------------------------------------------- #

def _week_range(horizon_weeks: int) -> List[int]:
    """Which weeks the grids should draw."""
    last = max(1, horizon_weeks)
    presets = {
        "All": (1, last),
        "1–10": (1, min(10, last)),
        "11–20": (min(11, last), min(20, last)),
        f"21–{last}": (min(21, last), last),
    }
    options = [*presets.keys(), "Custom"]

    choice = st.segmented_control(
        "Weeks shown",
        options,
        default="All",
        key="week_preset",
        label_visibility="collapsed",
    )
    if choice == "Custom":
        first, final = st.slider(
            "Weeks shown",
            min_value=1,
            max_value=last,
            value=(1, last),
            key="week_slider",
        )
    else:
        first, final = presets.get(choice or "All", (1, last))
    return [w for w in range(1, last + 1) if first <= w <= final]


# --------------------------------------------------------------------------- #
# the pieces of the screen
# --------------------------------------------------------------------------- #

def _verdict(result: Dict[str, Any], rows: List[Dict[str, Any]]) -> None:
    report = result["report"]
    feasible = bool(report["feasible"])
    violations = report["hard_violations"]
    late = [r for r in rows if r["late"] > 0]

    if not feasible:
        plural = "" if len(violations) == 1 else "s"
        st.error(
            f"**RULES BROKEN** — this schedule breaks {len(violations)} hard rule{plural} "
            "and cannot be submitted as it stands.",
            icon=":material/gpp_bad:",
        )
        return

    if not late:
        st.success(
            "**VALID SCHEDULE** — every contract finishes on or before its deadline and "
            "no safety rule is broken.",
            icon=":material/verified:",
        )
        return

    worst = late[0]  # rows are sorted by priority, then by how late
    st.success(
        f"**VALID SCHEDULE** — {len(late)} of {len(rows)} contracts finish late. The most "
        f"important late one is {worst['contract_number']} (Priority {worst['priority']}), "
        f"{worst['late']} days late.",
        icon=":material/verified:",
    )


def _metrics(result: Dict[str, Any], rows: List[Dict[str, Any]]) -> None:
    scores = result["report"]["soft_scores"]
    metrics = result["metrics"]
    wall = metrics.get("wall_time_seconds")

    top = st.columns(3)
    top[0].metric("Rules used", f"Scenario {result['scenario']}", border=True)
    top[1].metric("Score", state.pretty_number(scores["objective_score"]), border=True)
    top[1].caption("lower is better, 0 is perfect")
    top[2].metric(
        "Contracts late",
        f"{scores['contracts_overrunning']} / {len(rows)}",
        border=True,
    )
    top[2].caption(f"{scores['overrun_days_total']} late days in total")

    bottom = st.columns(3)
    bottom[0].metric("Longer nights", scores["eclo_nights_total"], border=True)
    bottom[0].caption("ECLO nights used")
    bottom[1].metric("Extra nights", scores["excess_access_nights_total"], border=True)
    bottom[1].caption("over a spot's weekly limit")
    bottom[2].metric("Scheduler", metrics.get("solver_status") or "—", border=True)
    bottom[2].caption(
        "run time not reported" if wall is None else f"{state.pretty_number(wall)}s of thinking"
    )


def _score_breakdown(result: Dict[str, Any]) -> None:
    """Where the score comes from. Display only — the total is the reported one."""
    scores = result["report"]["soft_scores"]
    st.markdown("##### Where the score comes from")
    st.table(
        pd.DataFrame(
            [
                {
                    "Line": "Late contracts",
                    "How": "priority weight × late days, plus the activity nudge",
                    "Points": state.pretty_number(scores["priority_weighted_score"]),
                },
                {
                    "Line": "Longer nights (ECLO)",
                    "How": f"{scores['eclo_nights_total']} × {ECLO_POINTS}",
                    "Points": state.pretty_number(scores["eclo_nights_total"] * ECLO_POINTS),
                },
                {
                    "Line": "Extra nights over a spot's limit",
                    "How": f"{scores['excess_access_nights_total']} × {EXCESS_POINTS}",
                    "Points": state.pretty_number(
                        scores["excess_access_nights_total"] * EXCESS_POINTS
                    ),
                },
                {
                    "Line": f"Total score for Scenario {result['scenario']}",
                    "How": "what the checker reported",
                    "Points": state.pretty_number(scores["objective_score"]),
                },
            ]
        ),
        hide_index=True,
    )
    st.caption(
        "The total is the score the checker calculated for these rules. Anything a "
        "scenario forbids outright cannot appear here at all."
    )


def _downloads() -> None:
    st.markdown("##### Download the schedule")
    st.caption("The 3 files the official validator reads.")
    csvs: Dict[str, bytes] = st.session_state["csvs"]
    for name in SUBMISSION_FILES:
        blob = csvs.get(name)
        st.download_button(
            f"{name} — {FILE_BLURB[name]}",
            data=blob if blob is not None else b"",
            file_name=name,
            mime="text/csv",
            disabled=blob is None,
            icon=":material/download:",
            key=f"dl_{name}",
            width="stretch",
        )


def _contracts_table(rows: List[Dict[str, Any]]) -> None:
    st.markdown("##### Contracts, most important first")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "Contract": r["contract_number"],
                    "Description": r["description"],
                    "Priority": f"P{r['priority']}",
                    "Deadline": state.format_date(r["deadline"]),
                    "Finishes": state.format_date(r["simulated_completion_date"]),
                    "Late by": f"{r['late']} days" if r["late"] > 0 else "on time",
                }
                for r in rows
            ]
        ),
        hide_index=True,
    )


def _grids(result: Dict[str, Any], weeks: List[int]) -> Optional[str]:
    """The two grids. Returns an activity id if a grid reported a click."""
    picked: Optional[str] = None
    gantt_tab, heat_tab = st.tabs(
        ["Jobs by week", "Track spots by week"], on_change="rerun", key="grid_tabs"
    )

    if gantt_tab.open:
        with gantt_tab:
            try:
                from ui.gantt import render_gantt
            except ImportError:
                st.info(_COMING_SOON, icon=":material/hourglass_top:")
            else:
                picked = render_gantt(
                    result, weeks, st.session_state["selected_activity"], key="gantt"
                )

    if heat_tab.open:
        with heat_tab:
            try:
                from ui.heatmap import render_heatmap
            except ImportError:
                st.info(_COMING_SOON, icon=":material/hourglass_top:")
            else:
                picked = render_heatmap(result, weeks, key="heatmap") or picked

    return picked


def _explain(result: Dict[str, Any]) -> None:
    """Pick a job and read why it landed where it did."""
    ids = sorted({str(r["activity_id"]) for r in result["schedule_access"]})
    if not ids:
        return

    selected = st.session_state["selected_activity"]
    index = ids.index(selected) if selected in ids else 0
    chosen = st.selectbox("Job to explain", ids, index=index, key="activity_picker")
    if chosen != selected:
        st.session_state["selected_activity"] = chosen

    try:
        from ui.explain import render_explain
    except ImportError:
        st.info(_COMING_SOON, icon=":material/hourglass_top:")
        return
    render_explain(result, str(chosen))


# --------------------------------------------------------------------------- #
# the screen
# --------------------------------------------------------------------------- #

def render() -> None:
    job = st.session_state["job"] or {}
    result: Optional[Dict[str, Any]] = job.get("result")
    if not result:
        st.warning("There is no schedule to show yet.", icon=":material/info:")
        if st.button("Back: change rules", icon=":material/arrow_back:"):
            state.go("rules")
        return

    report = result["report"]

    # RESULTS.csv joined with the contract rows: most important first, then latest.
    by_number = {
        str(c.get("contract_number", "")): c for c in result["instance"]["contracts"]
    }
    rows = sorted(
        (
            {
                **r,
                "priority": str(
                    r.get("contract_priority")
                    or by_number.get(str(r["contract_number"]), {}).get("contract_priority")
                    or "3"
                ),
                "description": by_number.get(str(r["contract_number"]), {}).get(
                    "contract_description", ""
                ),
                "deadline": r.get("planned_completion_date")
                or by_number.get(str(r["contract_number"]), {}).get(
                    "planned_completion_date", ""
                ),
                "late": int(state.as_number(r.get("overrun_days", 0))),
            }
            for r in result["results"]
        ),
        key=lambda r: (state.as_number(r["priority"]), -r["late"]),
    )

    _verdict(result, rows)
    st.space("small")
    _metrics(result, rows)

    if not report["feasible"]:
        st.space("medium")
        with st.container(border=True):
            st.markdown("##### Rules broken")
            st.caption(
                "Reported by the checker. Each line names the rule and what it found."
            )
            for violation in report["hard_violations"]:
                st.markdown(f"- :red-badge[{violation['rule']}] {violation['detail']}")

    warnings = result.get("warnings") or []
    if warnings:
        st.space("medium")
        with st.container(border=True):
            st.markdown("##### Worth knowing")
            for warning in warnings:
                st.markdown(f"- {warning}")

    st.space("medium")
    left, right = st.columns([3, 2], gap="medium")
    with left, st.container(border=True, height="stretch"):
        _score_breakdown(result)
    with right, st.container(border=True, height="stretch"):
        _downloads()

    st.space("medium")
    with st.container(border=True):
        _contracts_table(rows)

    # The two grids get the full width of the page: at 2 AM on a laptop the old
    # 3:1 split squeezed the week columns until the labels were unreadable. The
    # explain panel now sits underneath, where it also has room to breathe.
    st.space("medium")
    with st.container(border=True):
        weeks = _week_range(int(result["instance"]["horizon_weeks"]))
        picked = _grids(result, weeks)

    if picked and picked != st.session_state["selected_activity"]:
        st.session_state["selected_activity"] = picked
        st.rerun()

    st.space("medium")
    with st.container(border=True):
        _explain(result)

    st.space("medium")
    with st.container(horizontal=True, horizontal_alignment="distribute"):
        if st.button("Back: change rules", icon=":material/arrow_back:"):
            state.go("rules")
        if st.button("Start over with new files", icon=":material/restart_alt:"):
            state.reset()
            state.go("upload")
