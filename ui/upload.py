"""STEP 1: load the 8 planning files.

We check two things here and nothing else: is the file one of the 8 official
names, and does its header row carry the columns the scheduler needs. The
scheduler reads the contents itself. The summary below the checklist is plain
row counting — it decides nothing.
"""

from __future__ import annotations

from typing import Any, Dict, List

import streamlit as st

from ui import errors, state
from ui.state import (
    INSTANCE_FILES,
    SAMPLE_DIR,
    WHAT_IS_IT,
)


def _accept(incoming: List[Any]) -> None:
    """Merge newly chosen files into what we already hold, then re-check.

    The only checks here are the cheap ones: is the file one of the 8, is it a
    readable CSV at all, and does its header row carry the columns the
    scheduler needs. Everything deeper is the scheduler's own job, and comes
    back through ``ui.errors``.
    """
    files: Dict[str, bytes] = dict(st.session_state["files"])
    bad: Dict[str, str] = dict(st.session_state["bad_headers"])
    codes: Dict[str, str] = dict(st.session_state["file_problems"])
    extras: List[str] = []
    seen: List[str] = []
    duplicates: List[str] = []

    for uploaded in incoming:
        name = state.canonical_name(uploaded.name)
        if name not in INSTANCE_FILES:
            extras.append(str(uploaded.name).replace("\\", "/").split("/")[-1])
            continue
        if name in seen:
            duplicates.append(name)  # keep the last one chosen, say so below
        seen.append(name)

        blob = uploaded.getvalue()
        if not blob.strip():
            files.pop(name, None)
            bad[name] = "the file is empty"
            codes[name] = "NOT_CSV"
            continue
        header = state.header_of(blob)
        if len(header) < 2:
            files.pop(name, None)
            bad[name] = "no readable comma-separated header row — is this really a CSV?"
            codes[name] = "NOT_CSV"
            continue

        missing = state.missing_columns(name, blob)
        if missing:
            files.pop(name, None)
            bad[name] = "header is missing: " + ", ".join(missing)
            codes[name] = "BAD_HEADER"
        else:
            files[name] = blob
            bad.pop(name, None)
            codes.pop(name, None)

    st.session_state["files"] = files
    st.session_state["bad_headers"] = bad
    st.session_state["file_problems"] = codes
    st.session_state["extras"] = extras
    st.session_state["duplicates"] = sorted(set(duplicates))


def _horizon_problem(files: Dict[str, bytes]) -> str:
    """``""`` when the planning period reads cleanly, else why it does not."""
    blob = files.get("06_PARAMETERS.csv")
    if blob is None:
        return ""
    parameters = {
        r.get("key", "").strip().lower(): r.get("value", "").strip()
        for r in state.read_rows(blob)
    }
    if "horizon_start" not in parameters:
        return "there is no `horizon_start` row"
    if state.parse_iso(parameters.get("horizon_start")) is None:
        return f"`horizon_start` is not a date: {parameters.get('horizon_start')!r}"
    weeks = parameters.get("horizon_weeks", "")
    if not weeks:
        return "there is no `horizon_weeks` row"
    if state.as_number(weeks) < 1:
        return f"`horizon_weeks` is not a whole number above zero: {weeks!r}"
    return ""


def _load_sample() -> None:
    """Read the 8 official PS1 sample CSVs bundled in the repo."""
    try:
        files = {name: (SAMPLE_DIR / name).read_bytes() for name in INSTANCE_FILES}
    except OSError as exc:
        st.session_state["problem"] = (
            f"Could not read the bundled sample instance: {exc}. "
            f"It should be in {SAMPLE_DIR}."
        )
        return
    state.clear_files()
    st.session_state["files"] = files


def _summary(files: Dict[str, bytes]) -> Dict[str, Any]:
    """What is in these files, by counting rows. No scheduling, no scoring."""
    rows = {name: state.read_rows(blob) for name, blob in files.items()}

    parameters = {
        r.get("key", "").strip().lower(): r.get("value", "").strip()
        for r in rows.get("06_PARAMETERS.csv", [])
    }
    horizon_start = parameters.get("horizon_start", "")
    horizon_weeks = int(state.as_number(parameters.get("horizon_weeks", 0)))

    stations = rows.get("02_STATIONS.csv", [])
    contracts = rows.get("07_PROJECT_DETAILS.csv", [])
    activities = rows.get("08_ACTIVITY_DETAILS.csv", [])

    return {
        "horizon_start": horizon_start,
        "horizon_end": state.week_end(horizon_start, horizon_weeks) if horizon_weeks else "",
        "horizon_weeks": horizon_weeks,
        "lines": len(rows.get("01_LINES.csv", [])),
        "stations": len(stations),
        "hubs": sum(1 for r in stations if r.get("is_interchange", "") == "1"),
        "sectors": len(rows.get("03_SECTORS.csv", [])),
        "spots": len(rows.get("04_LOCATION_SUPPLY.csv", [])),
        "contracts": len(contracts),
        "priority1": sum(1 for r in contracts if r.get("contract_priority", "") == "1"),
        "jobs": len(activities),
        "nights": int(sum(state.as_number(r.get("total_accesses", 0)) for r in activities)),
    }


def render() -> None:
    files: Dict[str, bytes] = st.session_state["files"]
    bad: Dict[str, str] = st.session_state["bad_headers"]

    st.subheader("Load the planning files")
    st.write(
        "Drop in the 8 CSV files for this planning period. We check the file names "
        "and header rows here; the scheduler reads the contents itself."
    )

    with st.container(border=True):
        chosen = st.file_uploader(
            "The 8 planning CSV files",
            type="csv",
            accept_multiple_files=True,
            key=f"uploader_{st.session_state['uploader_nonce']}",
            help="Choose all 8 at once, or a few at a time. A file you load again replaces the earlier one.",
        )
        if chosen:
            _accept(chosen)
            files = st.session_state["files"]
            bad = st.session_state["bad_headers"]

        with st.container(horizontal=True):
            if st.button(
                "Load the official PS1 sample instance",
                icon=":material/download:",
                type="primary" if not files else "secondary",
            ):
                _load_sample()
                st.rerun()
            if files or bad:
                if st.button("Clear files", icon=":material/close:"):
                    state.clear_files()
                    st.rerun()

    # ---- the checklist, one row per official file ----
    ok_count = 0
    for name in INSTANCE_FILES:
        if name in files:
            ok_count += 1
            mark, detail = ":green-badge[OK]", f"{max(1, round(len(files[name]) / 1024))} KB · {WHAT_IS_IT[name]}"
        elif name in bad:
            mark, detail = ":red-badge[FIX]", bad[name]
        else:
            mark, detail = ":gray-badge[MISSING]", "not chosen yet"
        st.markdown(f"{mark} &nbsp; `{name}` &nbsp; :gray[{detail}]")

    st.space("small")
    horizon_problem = _horizon_problem(files) if "06_PARAMETERS.csv" in files else ""
    ready = ok_count == 8 and not horizon_problem

    if ready:
        st.success("8 of 8 files ready.", icon=":material/check_circle:")
    else:
        st.info(
            f"{ok_count} of 8 files ready. Every file must be present with its official name.",
            icon=":material/info:",
        )

    # ---- say exactly what is wrong, and what to do about it ----
    for code in sorted({c for n, c in st.session_state["file_problems"].items() if n in bad}):
        title, cause, fix = errors.entry(code)
        with st.container(border=True):
            st.markdown(f"**{title}**")
            st.write(cause)
            st.markdown(f"**Fix:** {fix}")
    if ok_count < 8 and not bad:
        title, cause, fix = errors.entry("MISSING_FILE")
        st.caption(f"{cause} {fix}")
    if horizon_problem:
        title, cause, fix = errors.entry("BAD_HORIZON")
        st.error(f"**{title}** — {horizon_problem}.", icon=":material/error:")
        with st.container(border=True):
            st.write(cause)
            st.markdown(f"**Fix:** {fix}")

    duplicates = st.session_state.get("duplicates") or []
    if duplicates:
        title, cause, fix = errors.entry("DUPLICATE_FILE")
        st.warning(
            f"**{title}**: {', '.join(duplicates)}. {cause} {fix}",
            icon=":material/warning:",
        )

    extras = st.session_state["extras"]
    if extras:
        shown = ", ".join(extras[:6]) + ("…" if len(extras) > 6 else "")
        title, cause, fix = errors.entry("BAD_FILENAME")
        st.warning(
            f"Ignored {len(extras)} file{'' if len(extras) == 1 else 's'} that "
            f"{'is' if len(extras) == 1 else 'are'} not one of the 8: {shown}. {fix}",
            icon=":material/warning:",
        )
    if st.session_state["problem"]:
        st.error(st.session_state["problem"], icon=":material/error:")

    # ---- what is in these files ----
    if ready:
        summary = _summary(files)
        st.space("medium")
        st.subheader("What is in these files")

        top = st.columns(3)
        top[0].metric(
            "Planning period",
            f"{summary['horizon_weeks']} weeks",
            help="From 06_PARAMETERS.csv.",
            border=True,
        )
        top[0].caption(
            f"{state.format_date(summary['horizon_start'])} to {state.format_date(summary['horizon_end'])}"
        )
        top[1].metric("Lines", summary["lines"], border=True)
        top[1].caption(f"{summary['sectors']} tunnels between stations")
        top[2].metric("Stations", summary["stations"], border=True)
        top[2].caption(f"{summary['hubs']} interchange hubs")

        bottom = st.columns(3)
        bottom[0].metric("Bookable spots", summary["spots"], border=True)
        bottom[0].caption("tunnels and platforms, per direction")
        bottom[1].metric("Contracts", summary["contracts"], border=True)
        bottom[1].caption(f"{summary['priority1']} are Priority 1")
        bottom[2].metric("Jobs to schedule", summary["jobs"], border=True)
        bottom[2].caption(f"{summary['nights']} work-nights needed in total")

        st.caption(
            "A work-night is one night of track time at one spot, between the last train "
            "and the first. Counts come straight from the files you loaded."
        )

    # ---- next ----
    st.space("medium")
    with st.container(horizontal=True, horizontal_alignment="right"):
        if st.button(
            "Next: choose rules",
            type="primary",
            disabled=not ready,
            icon=":material/arrow_forward:",
        ):
            state.go("rules")
