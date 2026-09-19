"""Shared constants, session state and small pure helpers for the Streamlit app.

Nothing in this module schedules, validates or scores anything. The numbers on
the result screen all come from ``result["report"]`` and ``result["results"]``,
which the solver and our validator produced. What lives here is the wizard's
own bookkeeping: which step we are on, which file bytes we hold, and the date
arithmetic needed to print a week as a pair of dates.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
from pathlib import Path
from typing import Any, Dict, List

import streamlit as st

REPO_ROOT = Path(__file__).resolve().parent.parent

#: the official PS1 sample instance bundled in the repo, for the "load sample" button
SAMPLE_DIR = REPO_ROOT / "frontend" / "public" / "sample"

#: the 8 instance files, in order. Uploads match these names case-insensitively.
INSTANCE_FILES = (
    "01_LINES.csv",
    "02_STATIONS.csv",
    "03_SECTORS.csv",
    "04_LOCATION_SUPPLY.csv",
    "05_BUFFER_LOCATION.csv",
    "06_PARAMETERS.csv",
    "07_PROJECT_DETAILS.csv",
    "08_ACTIVITY_DETAILS.csv",
)

SUBMISSION_FILES = ("SCHEDULE_ACCESS.csv", "SCHEDULE_OCCUPANCY.csv", "RESULTS.csv")

#: what each instance file is, in one phrase (CLAUDE.md §4)
WHAT_IS_IT: Dict[str, str] = {
    "01_LINES.csv": "the train lines",
    "02_STATIONS.csv": "stations in order, hubs flagged",
    "03_SECTORS.csv": "tunnels between stations",
    "04_LOCATION_SUPPLY.csv": "bookable spots and their weekly limit",
    "05_BUFFER_LOCATION.csv": "safety buffer per kind of work",
    "06_PARAMETERS.csv": "first week and how many weeks",
    "07_PROJECT_DETAILS.csv": "the contracts and their deadlines",
    "08_ACTIVITY_DETAILS.csv": "the jobs to schedule",
}

#: what the 3 output files hold
FILE_BLURB: Dict[str, str] = {
    "SCHEDULE_ACCESS.csv": "when each job works",
    "SCHEDULE_OCCUPANCY.csv": "where it works",
    "RESULTS.csv": "when each contract finishes",
}

#: columns every file must carry (CLAUDE.md §4). Extra columns are fine.
EXPECTED_COLUMNS: Dict[str, List[str]] = {
    "01_LINES.csv": ["line_code", "line_name"],
    "02_STATIONS.csv": ["station_id", "line_code", "seq", "is_interchange"],
    "03_SECTORS.csv": [
        "sector_id",
        "line_code",
        "from_station_id",
        "to_station_id",
        "seq",
        "is_shared",
    ],
    "04_LOCATION_SUPPLY.csv": [
        "location_id",
        "location_kind",
        "line_code",
        "bound",
        "supply_capacity",
    ],
    "05_BUFFER_LOCATION.csv": [
        "nature_of_works",
        "up_to_buffer_sectors",
        "opposite_bound_required",
    ],
    "06_PARAMETERS.csv": ["key", "value"],
    "07_PROJECT_DETAILS.csv": [
        "contract_number",
        "contract_description",
        "contract_award_date",
        "activity_type",
        "nature_of_activity",
        "contract_priority",
        "contract_completion_date",
        "planned_completion_date",
        "number_of_workfronts",
        "access_type",
        "number_of_maximum_access_per_week",
    ],
    "08_ACTIVITY_DETAILS.csv": [
        "activity_id",
        "contract_number",
        "activity_type",
        "start_location_id",
        "end_location_id",
        "total_accesses",
        "planned_start_date",
        "predecessor_activity_id",
        "activity_priority",
    ],
}

#: penalty weights from PS1_README §2.5 — used for LABELS only. The score itself
#: is whatever the validator reported; nothing here recomputes it.
ECLO_POINTS = 5
EXCESS_POINTS = 7
LATE_POINTS = {"1": 100, "2": 10, "3": 1}

TIME_LIMIT_MIN, TIME_LIMIT_MAX, TIME_LIMIT_DEFAULT = 5, 300, 60

STEPS = ("Load files", "Choose rules", "Schedule")

#: which of the 3 steps each phase belongs to
STEP_OF = {"upload": 1, "rules": 2, "running": 3, "result": 3, "failed": 3}

_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
)


# --------------------------------------------------------------------------- #
# session state
# --------------------------------------------------------------------------- #

def init() -> None:
    """Set every key this app reads, once, in one place."""
    st.session_state.setdefault("phase", "upload")
    st.session_state.setdefault("files", {})           # official name -> bytes
    st.session_state.setdefault("bad_headers", {})     # official name -> why it was rejected
    st.session_state.setdefault("extras", [])          # names that are not one of the 8
    st.session_state.setdefault("duplicates", [])      # official names loaded twice at once
    st.session_state.setdefault("problem", None)
    st.session_state.setdefault("uploader_nonce", 0)
    st.session_state.setdefault("scenario", "A")
    st.session_state.setdefault("time_limit", TIME_LIMIT_DEFAULT)
    st.session_state.setdefault("job", None)           # the finished run_pipeline body
    st.session_state.setdefault("csvs", {})            # output file name -> bytes
    st.session_state.setdefault("selected_activity", None)
    st.session_state.setdefault("run_in_progress", False)  # a solve is on screen right now
    st.session_state.setdefault("file_problems", {})    # official name -> catalogue code


def go(phase: str) -> None:
    """Move to another step and redraw."""
    st.session_state["phase"] = phase
    st.rerun()


def clear_files() -> None:
    st.session_state["files"] = {}
    st.session_state["bad_headers"] = {}
    st.session_state["extras"] = []
    st.session_state["duplicates"] = []
    st.session_state["problem"] = None
    st.session_state["file_problems"] = {}
    st.session_state["uploader_nonce"] += 1  # resets the file_uploader widget


def reset() -> None:
    """Start over with new files."""
    clear_files()
    st.session_state["scenario"] = "A"
    st.session_state["time_limit"] = TIME_LIMIT_DEFAULT
    st.session_state["job"] = None
    st.session_state["csvs"] = {}
    st.session_state["selected_activity"] = None


# --------------------------------------------------------------------------- #
# file names and header rows
# --------------------------------------------------------------------------- #

def canonical_name(raw_name: str) -> str:
    """Match an upload to one of the 8 official names, ignoring case and folders."""
    base = str(raw_name).replace("\\", "/").split("/")[-1].strip()
    lower = base.lower()
    for name in INSTANCE_FILES:
        if name.lower() == lower:
            return name
    return base


def header_of(blob: bytes) -> List[str]:
    """The first row of a CSV, lower-cased. Empty list when it cannot be read."""
    try:
        text = blob[:8192].decode("utf-8-sig", errors="replace")
    except Exception:
        return []
    first = text.splitlines()[0] if text.splitlines() else ""
    try:
        cells = next(csv.reader([first]))
    except StopIteration:
        return []
    return [c.strip().lstrip("﻿").lower() for c in cells]


def missing_columns(name: str, blob: bytes) -> List[str]:
    """Which expected columns the header row does not have."""
    expected = EXPECTED_COLUMNS.get(name, [])
    have = set(header_of(blob))
    return [c for c in expected if c not in have]


def read_rows(blob: bytes) -> List[Dict[str, str]]:
    """Whole CSV as dicts keyed by the lower-cased header row."""
    text = blob.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows: List[Dict[str, str]] = []
    for row in reader:
        rows.append({(k or "").strip().lower(): (v or "").strip() for k, v in row.items()})
    return rows


# --------------------------------------------------------------------------- #
# dates and weeks (week N starts on horizon_start + 7*(N-1); CLAUDE.md §12)
# --------------------------------------------------------------------------- #

def parse_iso(value: Any) -> dt.date | None:
    try:
        return dt.date.fromisoformat(str(value).strip()[:10])
    except (TypeError, ValueError):
        return None


def format_date(value: Any) -> str:
    """``"2027-05-24"`` -> ``"24 May 2027"``. Unreadable input comes back as-is."""
    day = parse_iso(value)
    if day is None:
        return "" if value in (None, "") else str(value)
    return f"{day.day} {_MONTHS[day.month - 1]} {day.year}"


def week_start(horizon_start: Any, week: int) -> str:
    start = parse_iso(horizon_start)
    if start is None:
        return ""
    return (start + dt.timedelta(days=7 * (week - 1))).isoformat()


def week_end(horizon_start: Any, week: int) -> str:
    """The Sunday that ends `week`."""
    start = parse_iso(horizon_start)
    if start is None:
        return ""
    return (start + dt.timedelta(days=7 * week - 1)).isoformat()


def as_number(value: Any) -> float:
    """Best-effort number for display and sorting only."""
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0.0


def pretty_number(value: Any) -> str:
    """``25.2`` -> ``"25.2"``, ``30.0`` -> ``"30"``. Display only."""
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return "—"
    return str(int(number)) if number.is_integer() else f"{number:.1f}"
