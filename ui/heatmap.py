"""'Track spots by week' — one row per bookable location, one column per week.

The number in a cell is the backend's ``used`` from ``result["capacity_usage"]``;
the colour compares it with that location's ``supply_capacity``. No capacity is
computed, scored or validated here.

Clicking a cell lists the jobs booked in it; picking one returns its activity_id
so the caller can open the explain panel.

Why square markers and not ``go.Heatmap``: a heatmap trace cannot be selected,
and its presence stops Plotly emitting a selection for ANY trace in the same
figure, so Streamlit never hears the click. One square marker per cell keeps the
same grid, the same colours and the same in-cell text, and stays clickable.
"""

from __future__ import annotations

from typing import Any, Mapping

import plotly.graph_objects as go
import streamlit as st

from .grid_helpers import (
    HEAT_COLORS,
    HEAT_GLYPH,
    HEAT_WORDS,
    build_index,
    cell_key,
    fmt_day_month,
    group_locations,
    heat_level,
    location_label,
    num,
    rows,
    text,
    unique_labels,
    week_range_label,
    week_start,
)
from ui.theme import (
    AXIS_FONT_PX,
    CHART_FONT_PX,
    HEAT_ROW_PX,
    MARKER_PX,
    body_color,
)

MAX_ROWS = 100
_ROW_PX = HEAT_ROW_PX
_CHROME_PX = 130
_GROUP_PREFIX = "── "

#: Legend swatches: colour AND a glyph, so "full" / "over" never read on hue alone.
_LEGEND_SWATCH = (
    ":gray[■]",
    ":blue[■]",
    ":orange[■]",
    ":red[■]◼",
    ":red[■]▲",
)


def _emit(key: str, activity_id: str | None) -> str | None:
    """Only report a pick when it is a NEW pick (widget state survives reruns)."""
    slot = f"{key}__last_pick"
    if st.session_state.get(slot) == activity_id:
        return None
    st.session_state[slot] = activity_id
    return activity_id


def render_heatmap(result: Mapping[str, Any], weeks: list[int], key: str) -> str | None:
    """Draw the capacity grid. Returns an activity_id when an occupant is picked."""
    idx = build_index(result)
    horizon = idx.horizon_start
    all_locations = rows(result.get("instance", {}), "locations")

    if not all_locations or not weeks:
        st.info("No track spots to show for this week range.")
        return None

    shown = all_locations[:MAX_ROWS]
    groups = group_locations(shown, idx.line_names)

    # ---------------------------------------------------------- build rows
    raw_labels: list[str] = []
    row_location: list[str | None] = []  # None marks a group heading row
    row_supply: list[int] = []

    for _, title, members in groups:
        raw_labels.append(f"{_GROUP_PREFIX}{title}")
        row_location.append(None)
        row_supply.append(0)
        for loc in members:
            location_id = text(loc.get("location_id"))
            supply = num(loc.get("supply_capacity"))
            hub = "◆ " if location_id in idx.hub_locations else ""
            raw_labels.append(f"{hub}{location_label(location_id)} (limit {supply})")
            row_location.append(location_id)
            row_supply.append(supply)

    labels = unique_labels(raw_labels)

    # One bucket per heat level, so each level is its own colour.
    cells: dict[int, dict[str, list[Any]]] = {}
    for label, location_id, supply in zip(labels, row_location, row_supply):
        if location_id is None:
            continue
        for week in weeks:
            usage = idx.usage_by_cell.get(cell_key(location_id, week))
            used = num(usage.get("used")) if usage else 0
            level = heat_level(used, supply)
            bucket = cells.setdefault(level, {"x": [], "y": [], "t": [], "cd": [], "h": []})
            bucket["x"].append(week)
            bucket["y"].append(label)
            bucket["t"].append("" if used <= 0 else f"{used}{HEAT_GLYPH.get(level, '')}")
            bucket["cd"].append([location_id, week])
            bucket["h"].append(
                f"<b>{location_label(location_id)}</b>"
                f"<br>{week_range_label(horizon, week)}"
                f"<br>{used} of {supply} booked — {HEAT_WORDS[level]}"
                + ("<br><i>click to see which jobs</i>" if used > 0 else "")
            )

    # --------------------------------------------------------- the figure
    size = max(MARKER_PX, min(26, round(640 / max(1, len(weeks)))))
    fig = go.Figure()
    for level in sorted(cells):
        bucket = cells[level]
        fig.add_trace(
            go.Scatter(
                x=bucket["x"],
                y=bucket["y"],
                mode="markers+text",
                marker=dict(symbol="square", size=size, color=HEAT_COLORS[level]),
                # Picking one cell must not grey out the rest of the map.
                unselected=dict(marker=dict(opacity=1)),
                text=bucket["t"],
                textposition="middle center",
                textfont=dict(size=CHART_FONT_PX - 4, color="#FFFFFF"),
                customdata=bucket["cd"],
                hovertext=bucket["h"],
                hoverlabel=dict(font=dict(size=CHART_FONT_PX)),
                hovertemplate="%{hovertext}<extra></extra>",
                showlegend=False,
            )
        )

    lo, hi = min(weeks), max(weeks)
    fig.update_layout(
        margin=dict(l=12, r=20, t=12, b=12),
        height=max(320, min(2400, _CHROME_PX + _ROW_PX * len(labels))),
        font=dict(size=CHART_FONT_PX, color=body_color()),
        hovermode="closest",
        hoverlabel=dict(font=dict(size=CHART_FONT_PX)),
        dragmode=False,
        showlegend=False,
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
    )
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
        gridcolor="rgba(136,142,150,0.20)",
        zeroline=False,
        fixedrange=True,
        title=None,
    )
    fig.update_yaxes(
        type="category",
        categoryorder="array",
        categoryarray=list(reversed(labels)),
        showgrid=False,
        zeroline=False,
        fixedrange=True,
        title=None,
        automargin=True,
        tickfont=dict(size=AXIS_FONT_PX),
    )

    legend = " · ".join(
        f"{_LEGEND_SWATCH[level]} {word}" for level, word in enumerate(HEAT_WORDS)
    )
    st.markdown(
        "**Each square is one track spot for one week. "
        "Click a square to see which jobs are booked in it.**"
    )
    st.caption(
        f"{legend}. The number is bookings that week; "
        "◼ means full and ▲ means over the weekly limit, so the warning is "
        "never colour alone. ◆ marks an interchange spot shared between lines. "
        "**Lowest-capacity rows are the bottleneck.** "
        "EB and WB are separate tracks and are booked separately."
    )
    if len(all_locations) > MAX_ROWS:
        st.caption(f"Showing {MAX_ROWS} of {len(all_locations)} spots.")

    event = st.plotly_chart(
        fig,
        theme="streamlit",
        on_select="rerun",
        selection_mode="points",
        key=key,
        width="stretch",
        config={"displayModeBar": False, "scrollZoom": False, "responsive": True},
    )

    cell = _clicked_cell(event, set(weeks))
    if cell is None:
        return _emit(key, None)

    location_id, week = cell
    occupants = idx.occupancy_by_cell.get(cell_key(location_id, week), [])
    st.markdown(f"**{location_label(location_id)}**, {week_range_label(horizon, week)}")
    if not occupants:
        st.caption("Nothing is booked here.")
        return _emit(key, None)

    options: dict[str, str] = {}
    for row in occupants:
        activity_id = text(row.get("activity_id"))
        label = f"{activity_id} · slot {text(row.get('co_share_group'))}"
        options[label] = activity_id
    choice = st.pills(
        "Jobs booked in this slot",
        options=list(options),
        selection_mode="single",
        key=f"{key}__occupants",
        help="Jobs sharing one slot label are on the same night and need no buffer between them.",
    )
    return _emit(key, options.get(choice) if choice else None)


def _clicked_cell(event: Any, valid_weeks: set[int]) -> tuple[str, int] | None:
    """Pull (location_id, week) out of a Plotly selection, if there is one."""
    try:
        points = event.selection["points"]
    except Exception:
        return None
    for point in points or []:
        if not isinstance(point, Mapping):
            continue
        data = point.get("customdata")
        if not isinstance(data, (list, tuple)) or len(data) < 2:
            continue
        location_id, week = text(data[0]), num(data[1])
        if location_id and week in valid_weeks:
            return location_id, week
    return None
