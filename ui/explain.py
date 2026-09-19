"""The story of one job, in plain English.

The reasons come from the backend (``result["explanations"]``); everything else
is a readable re-print of the two schedule tables plus RESULTS.csv. Nothing is
scored, validated or re-checked here.
"""

from __future__ import annotations

from typing import Any, Mapping

import streamlit as st

from .grid_helpers import (
    build_index,
    cell_key,
    late_label,
    location_label_with_line,
    num,
    text,
    week_range_label,
)

_PRIORITY_BADGE = {1: "red", 2: "orange", 3: "blue"}


def _badge(priority: int) -> str:
    colour = _PRIORITY_BADGE.get(priority, "gray")
    return f":{colour}-badge[P{priority if priority else '?'}]"


def render_explain(result: Mapping[str, Any], activity_id: str) -> None:
    """Write the explain panel for one activity into the current container."""
    idx = build_index(result)
    horizon = idx.horizon_start

    activity = idx.activity_by_id.get(activity_id)
    contract = idx.contract_by_id.get(text(activity.get("contract_number"))) if activity else None
    priority = num(contract.get("contract_priority")) if contract else 0

    # ---------------------------------------------------------------- head
    st.markdown(f"#### Job {activity_id} &nbsp; {_badge(priority)}")
    if contract is not None:
        description = text(contract.get("contract_description"))
        st.caption(
            text(contract.get("contract_number"))
            + (f" · {description}" if description else "")
        )
    else:
        st.caption("This job's contract is not in the instance.")

    if activity is None:
        st.warning(f"{activity_id} is not in this instance's activity list.")
        return

    # ------------------------------------------------- why it is where it is
    st.markdown("##### Why it is where it is")
    explanations = result.get("explanations")
    reasons = explanations.get(activity_id) if isinstance(explanations, Mapping) else None
    if isinstance(reasons, str):
        reasons = [reasons]
    if reasons:
        st.markdown("\n".join(f"- {text(reason)}" for reason in reasons))
    else:
        st.caption("No explanation was returned for this job.")

    # ------------------------------------------------------------- the job
    st.markdown("##### The job")
    facts = [
        ("From", location_label_with_line(activity.get("start_location_id"), idx.line_names)),
        ("To", location_label_with_line(activity.get("end_location_id"), idx.line_names)),
        (
            "Work needed",
            f"{num(activity.get('total_accesses'))} "
            f"{'night' if num(activity.get('total_accesses')) == 1 else 'nights'}",
        ),
        ("Earliest start", text(activity.get("planned_start_date")) or "not set"),
        (
            "Work type",
            (text(contract.get("nature_of_activity")) if contract else "") or "unknown",
        ),
        (
            "Access type",
            (text(contract.get("access_type")) if contract else "") or "unknown",
        ),
    ]
    predecessor = text(activity.get("predecessor_activity_id"))
    if predecessor:
        facts.append(("Waits for", f"{predecessor} to finish first"))
    st.markdown("\n".join(f"- **{name}** — {value}" for name, value in facts))

    # -------------------------------------------------------- when it works
    st.markdown("##### When it works")
    nights = idx.access_by_activity.get(activity_id, [])
    if nights:
        st.markdown(
            "\n".join(
                f"- {week_range_label(horizon, night.get('week'))}"
                + (" — **E** longer night (ECLO)" if num(night.get("eclo")) == 1 else "")
                for night in nights
            )
        )
    else:
        st.caption("No nights were scheduled for this job.")

    # ------------------------------------------------------ spots it books
    st.markdown("##### Spots it books")
    occupancy = idx.occupancy_by_activity.get(activity_id, [])
    spots: list[str] = []
    for row in occupancy:
        location_id = text(row.get("location_id"))
        if location_id and location_id not in spots:
            spots.append(location_id)
    if spots:
        st.markdown(
            "\n".join(f"- {location_label_with_line(s, idx.line_names)}" for s in spots)
        )
    else:
        st.caption("No spots were booked for this job.")

    # ------------------------------------------------ shares possessions with
    st.markdown("##### Shares possessions with")
    sharers: dict[str, set[int]] = {}
    for mine in occupancy:
        week = num(mine.get("week"))
        for other in idx.occupancy_by_cell.get(cell_key(mine.get("location_id"), week), []):
            other_id = text(other.get("activity_id"))
            if other_id == activity_id:
                continue
            if text(other.get("co_share_group")) != text(mine.get("co_share_group")):
                continue
            sharers.setdefault(other_id, set()).add(week)
    if sharers:
        st.markdown(
            "\n".join(
                f"- **{other_id}** · "
                f"{'week' if len(weeks) == 1 else 'weeks'} "
                f"{', '.join(str(w) for w in sorted(weeks))}"
                for other_id, weeks in sorted(sharers.items())
            )
        )
    else:
        st.caption("Nobody else — it has its possessions to itself.")

    # ---------------------------------------------------- contract outcome
    st.markdown("##### Contract outcome")
    number = text(contract.get("contract_number")) if contract else ""
    late_days, late_text = late_label(idx.result_by_contract.get(number))
    planned = text(contract.get("planned_completion_date")) if contract else ""
    sentence = f"{number or 'This contract'} is **{late_text}**"
    if planned:
        sentence += f" against its planned date of {planned}"
    st.markdown((":red[" if late_days > 0 else ":green[") + sentence + "].")
    st.caption(
        "Spot names read as station–station plus the track direction "
        "(EB and WB are separate tracks)."
    )
