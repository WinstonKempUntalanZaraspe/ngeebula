"""'Jobs by week' — one column per week, one row per contract (folded) or job.

A filled square = one night of work that week. Everything shown is read from the
backend payload: nights from ``schedule_access``, lateness from ``results``,
deadlines from each contract's ``planned_completion_date``. Nothing is scored,
validated or re-scheduled here.

Plotly cannot fold rows on its own, so the fold lives in a ``st.multiselect``
above the chart: contracts are folded by default and the picker opens the ones
the controller wants to look at.
"""

from __future__ import annotations

from typing import Any, Mapping

import plotly.graph_objects as go
import streamlit as st

from .grid_helpers import (
    DEADLINE_COLOR,
    WAITING_COLOR,
    activity_week_key,
    build_index,
    fmt_date,
    fmt_day_month,
    late_label,
    num,
    rows,
    sorted_contracts,
    text,
    unique_labels,
    week_end,
    week_of_date,
    week_range_label,
    week_start,
)
from ui.theme import (
    AXIS_FONT_PX,
    CHART_FONT_PX,
    CONTRACT_ROW_PX,
    JOB_ROW_PX,
    LEGEND_FONT_PX,
    MARKER_PX,
    body_color,
    is_dark,
)

_CHROME_PX = 150  # week headers on top + the priority key underneath
_LATE_BAR = "rgba(193,63,78,0.30)"
_OK_BAR = "rgba(136,142,150,0.28)"

#: Priority colours, one set per theme, both well clear of WCAG AA against
#: their own background. Kept here rather than in grid_helpers so the Gantt can
#: swap them when the Night/Day switch flips.
_PRIORITY_DARK = {1: "#FF6B6B", 2: "#FFAE4D", 3: "#4DABF7"}
_PRIORITY_LIGHT = {1: "#B3121F", 2: "#9A4E00", 3: "#0F4CA8"}
_PRIORITY_NAME = {1: "Priority 1 (most important)", 2: "Priority 2", 3: "Priority 3"}


def _priority_color(priority: int) -> str:
    table = _PRIORITY_DARK if is_dark() else _PRIORITY_LIGHT
    return table.get(priority, "#8A9099")


def _emit(key: str, activity_id: str | None) -> str | None:
    """Only report a click when it is a NEW click.

    Plotly selection state survives reruns, so without this the chart would keep
    re-announcing the same job and fight the heat-map for the explain panel.
    """
    slot = f"{key}__last_click"
    if st.session_state.get(slot) == activity_id:
        return None
    st.session_state[slot] = activity_id
    return activity_id


def render_gantt(
    result: Mapping[str, Any], weeks: list[int], selected: str | None, key: str
) -> str | None:
    """Draw the week grid. Returns an activity_id when a job marker is clicked."""
    idx = build_index(result)
    horizon = idx.horizon_start
    contracts = sorted_contracts(rows(result.get("instance", {}), "contracts"))

    if not contracts or not weeks:
        st.info("No contracts to show for this week range.")
        return None

    numbers = [text(c.get("contract_number")) for c in contracts]
    show_all = st.checkbox(
        "Show the jobs inside every contract",
        key=f"{key}__all",
        help="Off means one row per contract: the shape of the whole programme on one screen.",
    )
    picked = st.multiselect(
        "Show jobs for",
        options=numbers,
        default=[],
        key=f"{key}__open",
        placeholder="Pick a contract to open its jobs",
        disabled=show_all,
        help="Opens a row per job underneath the contract, one marker per night worked.",
    )
    open_contracts = set(numbers) if show_all else set(picked)

    st.markdown(
        "**Each square is one night of work. Click a square to see why.**"
    )

    # ---------------------------------------------------------- build rows
    # Rows are collected top-down, then handed to Plotly bottom-up.
    labels: list[str] = []
    kinds: list[str] = []  # "contract" | "job"
    payload: list[dict[str, Any]] = []

    # On a narrow screen the week columns need every pixel, so the row names
    # down the left drop to the short form ("C012 · P1 · 14d late").
    compact = len(weeks) * 26 > 700

    for contract in contracts:
        number = text(contract.get("contract_number"))
        priority = num(contract.get("contract_priority"))
        jobs = idx.activities_by_contract.get(number, [])
        late_days, late_text = late_label(idx.result_by_contract.get(number))
        if compact:
            labels.append(f"{number} · P{priority or '?'} · {late_text}")
        else:
            labels.append(
                f"P{priority or '?'} · {number} · {len(jobs)} jobs · {late_text}"
            )
        kinds.append("contract")
        payload.append(
            {
                "number": number,
                "priority": priority,
                "late_text": late_text,
                "late_days": late_days,
                "deadline_week": week_of_date(horizon, contract.get("planned_completion_date")),
                "deadline_date": text(contract.get("planned_completion_date")),
                "span": idx.span_by_contract.get(number),
                "description": text(contract.get("contract_description")),
            }
        )
        if number in open_contracts:
            for job in jobs:
                activity_id = text(job.get("activity_id"))
                nights = num(job.get("total_accesses"))
                if compact:
                    labels.append(f"     {activity_id} · {nights}n")
                else:
                    labels.append(
                        f"     {activity_id} · {nights}"
                        f" {'night' if nights == 1 else 'nights'}"
                    )
                kinds.append("job")
                payload.append(
                    {
                        "activity_id": activity_id,
                        "priority": priority,
                        "total": nights,
                        "span": idx.span_by_activity.get(activity_id),
                    }
                )

    labels = unique_labels(labels)
    y_order = list(reversed(labels))  # Plotly draws categories bottom-up.

    # --------------------------------------------------------- the figure
    fig = go.Figure()
    lo, hi = min(weeks), max(weeks)

    # 1. Contract span bars: first worked week to last worked week.
    bar_y, bar_base, bar_width, bar_color, bar_text, bar_hover = [], [], [], [], [], []
    for label, kind, row in zip(labels, kinds, payload):
        if kind != "contract" or not row["span"]:
            continue
        start, end = row["span"]
        left, right = max(start, lo - 1), min(end, hi + 1)
        if right < lo or left > hi:
            continue
        bar_y.append(label)
        bar_base.append(left - 0.42)
        bar_width.append((right - left) + 0.84)
        bar_color.append(_LATE_BAR if row["late_days"] > 0 else _OK_BAR)
        bar_text.append(row["late_text"])
        bar_hover.append(
            f"<b>{row['number']}</b>"
            + (f" · {row['description']}" if row["description"] else "")
            + f"<br>Priority {row['priority']} · {row['late_text']}"
            + f"<br>Works from {week_range_label(horizon, start)}"
            + f"<br>through {week_range_label(horizon, end)}"
        )
    if bar_y:
        fig.add_trace(
            go.Bar(
                x=bar_width,
                y=bar_y,
                base=bar_base,
                orientation="h",
                marker=dict(color=bar_color, line=dict(width=0)),
                text=bar_text,
                textposition="auto",
                insidetextanchor="start",
                textfont=dict(size=CHART_FONT_PX - 2, color=body_color()),
                cliponaxis=False,
                hovertext=bar_hover,
                hovertemplate="%{hovertext}<extra></extra>",
                showlegend=False,
                width=0.55,
            )
        )

    # 2. Deadline diamonds, one per contract, at the week of planned_completion_date.
    dl_x, dl_y, dl_hover = [], [], []
    for label, kind, row in zip(labels, kinds, payload):
        if kind != "contract":
            continue
        dweek = row["deadline_week"]
        if dweek is None or not (lo <= dweek <= hi):
            continue
        dl_x.append(dweek)
        dl_y.append(label)
        dl_hover.append(
            f"<b>{row['number']} deadline</b><br>{row['deadline_date']}"
            f"<br>week {dweek}, ending {fmt_date(week_end(horizon, dweek))}"
            f"<br>Outcome: {row['late_text']}"
        )
    if dl_x:
        fig.add_trace(
            go.Scatter(
                x=dl_x,
                y=dl_y,
                mode="markers",
                marker=dict(
                    symbol="diamond-tall",
                    size=24,
                    color=DEADLINE_COLOR,
                    line=dict(width=1.4, color="rgba(255,255,255,0.75)"),
                ),
                name="Deadline week",
                hovertext=dl_hover,
                hoverlabel=dict(font=dict(size=CHART_FONT_PX)),
                hovertemplate="%{hovertext}<extra></extra>",
                showlegend=True,
            )
        )

    # 3. Waiting weeks: hollow markers between a job's first and last night.
    wait_x, wait_y, wait_hover = [], [], []
    # 4. Worked nights, one trace per priority so colours stay meaningful.
    by_priority: dict[int, dict[str, list[Any]]] = {}

    for label, kind, row in zip(labels, kinds, payload):
        if kind != "job":
            continue
        activity_id = row["activity_id"]
        span = row["span"]
        for week in weeks:
            night = idx.access_by_activity_week.get(activity_week_key(activity_id, week))
            if night is None:
                if span and span[0] < week < span[1]:
                    wait_x.append(week)
                    wait_y.append(label)
                    wait_hover.append(
                        f"<b>{activity_id}</b> is waiting in {week_range_label(horizon, week)}"
                        "<br>it works before and after, but not this week"
                    )
                continue
            eclo = num(night.get("eclo")) == 1
            bucket = by_priority.setdefault(
                row["priority"], {"x": [], "y": [], "t": [], "cd": [], "h": []}
            )
            bucket["x"].append(week)
            bucket["y"].append(label)
            bucket["t"].append("E" if eclo else "")
            bucket["cd"].append(activity_id)
            bucket["h"].append(
                f"<b>{activity_id}</b> works {week_range_label(horizon, week)}"
                f"<br>Night slot {num(night.get('access_night'))}"
                " of the contract's weekly allowance"
                f"<br>Night {num(night.get('access_seq'))} of {row['total']}"
                + ("<br>ECLO: a longer night (counts 1.5)" if eclo else "")
                + "<br><i>click to see why it sits here</i>"
            )

    if wait_x:
        fig.add_trace(
            go.Scatter(
                x=wait_x,
                y=wait_y,
                mode="markers",
                marker=dict(
                    symbol="circle",
                    size=MARKER_PX - 2,
                    color="rgba(0,0,0,0)",
                    line=dict(width=1.8, color=WAITING_COLOR),
                ),
                name="Waiting that week",
                hovertext=wait_hover,
                hoverlabel=dict(font=dict(size=CHART_FONT_PX)),
                hovertemplate="%{hovertext}<extra></extra>",
                showlegend=True,
            )
        )

    for priority in sorted(by_priority):
        bucket = by_priority[priority]
        fig.add_trace(
            go.Scatter(
                x=bucket["x"],
                y=bucket["y"],
                mode="markers+text",
                marker=dict(
                    symbol="square",
                    size=MARKER_PX + 4,
                    color=_priority_color(priority),
                    line=dict(width=1.0, color="rgba(255,255,255,0.65)"),
                ),
                # Picking one night must not grey out the rest of the programme.
                unselected=dict(marker=dict(opacity=1)),
                text=bucket["t"],
                textposition="middle center",
                textfont=dict(size=CHART_FONT_PX - 3, color="#FFFFFF"),
                customdata=bucket["cd"],
                name=_PRIORITY_NAME.get(priority, f"Priority {priority}"),
                hovertext=bucket["h"],
                hoverlabel=dict(font=dict(size=CHART_FONT_PX)),
                hovertemplate="%{hovertext}<extra></extra>",
                showlegend=True,
            )
        )

    # 5. A ring around whatever the rest of the app currently has open.
    if selected:
        sel_x = [
            week
            for week in weeks
            if activity_week_key(selected, week) in idx.access_by_activity_week
        ]
        sel_label = next(
            (
                label
                for label, kind, row in zip(labels, kinds, payload)
                if kind == "job" and row["activity_id"] == selected
            ),
            None,
        )
        if sel_x and sel_label:
            fig.add_trace(
                go.Scatter(
                    x=sel_x,
                    y=[sel_label] * len(sel_x),
                    mode="markers",
                    marker=dict(
                        symbol="square-open",
                        size=MARKER_PX + 14,
                        color=DEADLINE_COLOR,
                        line=dict(width=2.2, color=DEADLINE_COLOR),
                    ),
                    hoverinfo="skip",
                    showlegend=False,
                )
            )

    # Row height: a contract row carries a bar and its lateness, a job row a
    # line of squares. Both are sized to stay legible at arm's length.
    body_px = sum(CONTRACT_ROW_PX if k == "contract" else JOB_ROW_PX for k in kinds)

    fig.update_layout(
        barmode="overlay",
        bargap=0.40,
        margin=dict(l=12, r=20, t=12, b=12),
        height=max(320, _CHROME_PX + body_px),
        font=dict(size=CHART_FONT_PX, color=body_color()),
        hovermode="closest",
        hoverlabel=dict(font=dict(size=CHART_FONT_PX)),
        dragmode=False,
        showlegend=True,
        legend=dict(
            orientation="h",
            yanchor="top",
            y=-0.02,
            x=0,
            font=dict(size=LEGEND_FONT_PX),
            itemsizing="constant",
            bgcolor="rgba(0,0,0,0)",
        ),
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
    # Week headers read "W5 · 1 Feb", upright. When the range is crowded the
    # date drops off every second week so the labels never collide.
    # How many weeks can carry a written header before they run into each
    # other. Every week still gets its own gridline and its own hover text;
    # this only thins out the WRITING, which is what makes a 30-week range
    # readable on a 1024 px laptop.
    step = 1 if len(weeks) <= 10 else 2 if len(weeks) <= 20 else 3
    fig.update_xaxes(
        range=[lo - 0.6, hi + 0.6],
        tickmode="array",
        tickvals=weeks,
        ticktext=[
            # Two upright lines: the Monday's date, then the week number under
            # it — "18 Jan / W3". Plotly bottom-aligns tick text, so keeping
            # the week number on the last line puts every "W" on one row.
            f"{fmt_day_month(week_start(horizon, w))}<br>W{w}"
            if index % step == 0
            else ""
            for index, w in enumerate(weeks)
        ],
        tickangle=0,
        tickfont=dict(size=AXIS_FONT_PX),
        ticklabelstandoff=8,
        side="top",
        showgrid=True,
        gridcolor="rgba(136,142,150,0.22)",
        zeroline=False,
        fixedrange=True,
        title=None,
    )
    fig.update_yaxes(
        type="category",
        categoryorder="array",
        categoryarray=y_order,
        tickfont=dict(size=AXIS_FONT_PX),
        showgrid=False,
        zeroline=False,
        fixedrange=True,
        title=None,
        automargin=True,  # widens the left margin to fit the whole row name
    )

    event = st.plotly_chart(
        fig,
        theme="streamlit",
        on_select="rerun",
        selection_mode="points",
        key=key,
        width="stretch",
        config={"displayModeBar": False, "scrollZoom": False, "responsive": True},
    )

    st.caption(
        "Each square is one night of work. "
        ":red[■] priority 1 · :orange[■] priority 2 · :blue[■] priority 3 "
        "· **E** inside a square = ECLO, a longer night "
        "· ○ waiting (works before and after, not that week) "
        "· :violet[◆] deadline week. "
        "The bar on a contract row runs from its first working week to its last, "
        "and carries its lateness. Click a square to see why that job sits there."
    )

    return _emit(key, _clicked_activity(event, idx.activity_by_id))


def _clicked_activity(event: Any, known: Mapping[str, Any]) -> str | None:
    """Pull the activity_id out of a Plotly selection event, if there is one."""
    try:
        points = event.selection["points"]
    except Exception:
        return None
    for point in points or []:
        data = point.get("customdata") if isinstance(point, Mapping) else None
        if isinstance(data, (list, tuple)) and data:
            candidate = text(data[0])
        elif isinstance(data, str):
            candidate = data
        else:
            continue
        if candidate in known:
            return candidate
    return None
