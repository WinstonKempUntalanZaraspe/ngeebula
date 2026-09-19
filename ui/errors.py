"""Every way this app can fail, in plain English, in one place.

Three things live here:

* ``CATALOGUE`` — one entry per failure a controller can actually hit, each a
  ``(title, cause, fix)`` triple. Nothing in here re-checks anything: the
  pipeline already returns ``status`` / ``error_code`` / ``message``, and
  :func:`classify` maps those onto a catalogue code.
* :func:`render_failure` — draws one catalogue entry, plus whatever the server
  itself said, on the "it did not work" screen.
* :func:`render_global_notices` — the banner shown when a previous run vanished
  because the server restarted underneath it (almost always the container
  running out of memory mid-solve). Safe to call on every rerun.

The run marker is deliberately kept in ``st.query_params``, not in
``st.session_state``: when the server is killed mid-solve the browser
reconnects into a brand-new session with empty state, but the URL survives, so
the marker is the only breadcrumb left to tell the controller what happened.
"""

from __future__ import annotations

import time
from typing import Any, Dict, Mapping, Optional, Tuple

import streamlit as st

#: query-parameter name holding the in-flight run marker
RUN_PARAM = "run"

#: how much memory the scheduler really needs, quoted in several messages
MEMORY_FIX = (
    "Give the server at least 2 GiB of memory. On Cloud Run: Edit & deploy new "
    "revision → Memory → 2 GiB. Or lower the time limit so the scheduler has "
    "less time to grow."
)

# --------------------------------------------------------------------------- #
# the catalogue: code -> (title, cause, fix)
# --------------------------------------------------------------------------- #

CATALOGUE: Dict[str, Tuple[str, str, str]] = {
    # ---- step 1: the files themselves -------------------------------------
    "MISSING_FILE": (
        "Some of the 8 planning files are missing",
        "The scheduler needs all 8 files. One or more were never loaded.",
        "Load the missing files, or press “Load the official PS1 sample instance” "
        "to try the app with the judges' own data.",
    ),
    "BAD_FILENAME": (
        "A file is not one of the 8 official names",
        "Files are matched by name, ignoring capitals and folders. Anything else "
        "is ignored, so the scheduler would run on an incomplete instance.",
        "Rename the file to its official name (for example `08_ACTIVITY_DETAILS.csv`) "
        "and load it again. Do not rename the columns inside.",
    ),
    "DUPLICATE_FILE": (
        "The same file was loaded twice",
        "Two uploads matched the same official name, so only one of them can be used.",
        "The newest one was kept. If that is the wrong one, press “Clear files” and "
        "load the 8 files again.",
    ),
    "BAD_HEADER": (
        "A file is missing columns the scheduler needs",
        "The header row of that file does not carry every column listed in the "
        "problem statement. A renamed or deleted column makes the file unreadable.",
        "Open the file, restore the missing column names exactly as shown, and load "
        "it again. Extra columns are fine; missing ones are not.",
    ),
    "NOT_CSV": (
        "A file is empty, or is not a CSV",
        "The file has no readable header row. Excel workbooks (.xlsx) and files "
        "saved as “Excel CSV UTF-16” often land here.",
        "In Excel choose File → Save As → CSV UTF-8 (Comma delimited), and load the "
        "saved file, not the workbook.",
    ),
    "BAD_HORIZON": (
        "The planning period could not be read",
        "`06_PARAMETERS.csv` must contain a `horizon_start` row holding a date like "
        "2027-01-04, and a `horizon_weeks` row holding a whole number. One of them "
        "is missing or cannot be read.",
        "Fix those two rows in `06_PARAMETERS.csv` and load it again.",
    ),
    # ---- step 1.5: things only the scheduler can see -----------------------
    "UNKNOWN_CONTRACT": (
        "A job belongs to a contract that does not exist",
        "A `contract_number` in `08_ACTIVITY_DETAILS.csv` has no matching row in "
        "`07_PROJECT_DETAILS.csv`, so the scheduler has no deadline, priority or "
        "weekly limit for that job.",
        "Either add the contract to `07_PROJECT_DETAILS.csv` or correct the "
        "contract number on the job. The message below names it.",
    ),
    "BAD_PREDECESSOR": (
        "A job waits for a job that does not exist, or for itself",
        "`predecessor_activity_id` must be blank, or the id of another job in the "
        "same file. A typo, or a job pointing at itself, cannot be scheduled.",
        "Blank the predecessor out, or correct it to a real `activity_id`. "
        "The message below names the job.",
    ),
    "PREDECESSOR_CYCLE": (
        "A ring of jobs all wait for each other",
        "The `predecessor_activity_id` chain loops back on itself (A waits for B, "
        "B waits for A). No order can satisfy that, so nothing can be scheduled.",
        "Break the loop: one job in the ring must not have a predecessor. "
        "The message below prints the ring.",
    ),
    "UNKNOWN_NATURE": (
        "A kind of work has no safety buffer defined",
        "Every `nature_of_activity` in `07_PROJECT_DETAILS.csv` must appear as a "
        "`nature_of_works` row in `05_BUFFER_LOCATION.csv`, otherwise the scheduler "
        "does not know how much empty track to keep beside that work.",
        "Add the missing row to `05_BUFFER_LOCATION.csv`, or correct the spelling in "
        "`07_PROJECT_DETAILS.csv`. Capitals and brackets must match exactly.",
    ),
    "UNKNOWN_LOCATION": (
        "A job works at a spot that is not in the supply file",
        "A `start_location_id` or `end_location_id` in `08_ACTIVITY_DETAILS.csv` "
        "is not listed in `04_LOCATION_SUPPLY.csv`, or is not written in the "
        "`KIND:LINE:MIDDLE:BOUND` form, so it has no weekly capacity.",
        "Add the spot to `04_LOCATION_SUPPLY.csv`, or correct the id on the job. "
        "The message below names it.",
    ),
    "BAD_INSTANCE": (
        "The planning files could not be used",
        "The scheduler read the files but found something it cannot work with. "
        "The exact reason is below.",
        "Correct what the message names, then load the files again.",
    ),
    # ---- the solve --------------------------------------------------------
    "INFEASIBLE": (
        "No schedule can satisfy these rules",
        "Every job, every safety buffer and every weekly limit cannot all hold at "
        "the same time in this planning period.",
        "Try Scenario C, which allows one extra night per spot and longer nights, "
        "or Scenario B, which allows as many extra nights as it takes. Also check "
        "that no job needs more nights than there are weeks left after its planned "
        "start date — a job works at most one night a week.",
    ),
    "INFEASIBLE_B": (
        "No schedule can satisfy Scenario B",
        "Scenario B forbids lateness outright: every contract must finish on or "
        "before its `planned_completion_date`, no exceptions. If even one contract "
        "cannot make its deadline, the whole scenario has no answer.",
        "Use Scenario C, which allows lateness and overbooking together, or "
        "Scenario A, which allows lateness and charges points for it. If B is "
        "required, the deadlines in `07_PROJECT_DETAILS.csv` have to move.",
    ),
    "TIMEOUT": (
        "The scheduler ran out of thinking time",
        "It had the time limit you chose and could not yet prove it had a legal "
        "schedule. Bigger instances need more time.",
        "Raise the time limit (up to 300 seconds) and run it again. If 300 is not "
        "enough, try Scenario B or C, which give the scheduler more room to move.",
    ),
    "OUT_OF_MEMORY": (
        "OUT OF MEMORY: the scheduler was stopped by the server",
        "The server ran out of memory and killed the scheduler mid-solve. "
        "Scenario C builds a bigger model than A or B and uses more memory, and "
        "memory keeps growing the longer it searches.",
        MEMORY_FIX,
    ),
    "CRASH": (
        "The scheduler crashed",
        "The scheduler stopped before it produced an answer. The last lines it "
        "printed are below.",
        "Run it again. If it keeps happening, check that the 8 files are the ones "
        "the judges gave you, unedited, and each opens as a plain CSV.",
    ),
    "WAIT_TIMEOUT": (
        "The scheduler did not finish within the wait window",
        "It was still running well past the time limit you chose, so it was stopped "
        "to keep the app responsive. This is usually a sign that the server is short "
        "of memory or CPU and is swapping rather than solving.",
        "Lower the time limit, or give the server more memory and CPU "
        "(Cloud Run: 2 GiB memory, 1 CPU, request timeout 600 s).",
    ),
    "SESSION_LOST": (
        "Your previous run was lost because the server restarted",
        "The server restarted while the scheduler was working — almost always "
        "because it ran out of memory. Everything held in the browser session went "
        "with it, including the 8 files you loaded.",
        MEMORY_FIX,
    ),
    # ---- afterwards --------------------------------------------------------
    "BAD_RESULT": (
        "The answer came back incomplete",
        "The scheduler finished but its answer is missing parts this screen needs, "
        "so nothing can be drawn from it.",
        "Run it again. If it happens twice, report it with the scenario and the "
        "time limit you used.",
    ),
    "NO_DOWNLOAD": (
        "The result files are not available to download",
        "The 3 output CSVs were not written, which happens when the run did not "
        "finish or the server's temporary folder is not writable.",
        "Run the schedule again. On a hosted server, check that the app may write "
        "to its temporary folder.",
    ),
    "UNKNOWN": (
        "Something went wrong",
        "The scheduler could not finish and did not say why.",
        "Run it again. If it keeps happening, load the 8 files fresh and try "
        "Scenario A with a 60 second limit to check the app itself still works.",
    ),
}

#: substrings of the pipeline's own ValueError text -> catalogue code
_MESSAGE_HINTS: Tuple[Tuple[str, str], ...] = (
    ("horizon_start", "BAD_HORIZON"),
    ("horizon_weeks", "BAD_HORIZON"),
    ("unknown contract", "UNKNOWN_CONTRACT"),
    ("references unknown contract", "UNKNOWN_CONTRACT"),
    ("contract_number", "UNKNOWN_CONTRACT"),
    ("predecessor cycle", "PREDECESSOR_CYCLE"),
    ("cannot depend on itself", "BAD_PREDECESSOR"),
    ("predecessor", "BAD_PREDECESSOR"),
    ("nature_of_activity", "UNKNOWN_NATURE"),
    ("nature_of_works", "UNKNOWN_NATURE"),
    ("buffer", "UNKNOWN_NATURE"),
    ("location_id", "UNKNOWN_LOCATION"),
    ("location kind", "UNKNOWN_LOCATION"),
    ("known sector pair", "UNKNOWN_LOCATION"),
    ("sector locations", "UNKNOWN_LOCATION"),
    ("unknown bound", "UNKNOWN_LOCATION"),
    ("missing activity_id", "BAD_INSTANCE"),
)


def classify(job: Mapping[str, Any]) -> str:
    """Pick the catalogue code that fits a finished ``run_pipeline`` body."""
    status = str(job.get("status") or "error")
    code = str(job.get("error_code") or "")
    message = str(job.get("message") or "").lower()

    if code in CATALOGUE:
        return code
    if status == "infeasible":
        return "INFEASIBLE_B" if str(job.get("scenario", "")).upper() == "B" else "INFEASIBLE"
    if status == "timeout":
        return "TIMEOUT"
    if code == "BAD_INSTANCE":
        for hint, mapped in _MESSAGE_HINTS:
            if hint in message:
                return mapped
        return "BAD_INSTANCE"
    if status == "done":
        return "BAD_RESULT"
    return "UNKNOWN"


def entry(code: str) -> Tuple[str, str, str]:
    """``(title, cause, fix)`` for a code, falling back to the generic one."""
    return CATALOGUE.get(code, CATALOGUE["UNKNOWN"])


def render_failure(job_or_code: Any, extra: Optional[str] = None) -> str:
    """Draw one failure: the headline, why it happened, and what to do.

    Args:
        job_or_code: either a catalogue code, or a finished ``run_pipeline`` body
            (in which case the code is worked out by :func:`classify`).
        extra: anything else worth showing verbatim — the server's own message,
            the tail of the scheduler's log, the name of the offending file.

    Returns:
        The catalogue code that was drawn, so callers can branch on it.
    """
    if isinstance(job_or_code, Mapping):
        job: Mapping[str, Any] = job_or_code
        code = classify(job)
        said = str(job.get("message") or "").strip()
    else:
        job = {}
        code = str(job_or_code)
        said = ""

    title, cause, fix = entry(code)
    icon = ":material/timer_off:" if code in ("TIMEOUT", "WAIT_TIMEOUT") else ":material/error:"
    if code in ("TIMEOUT", "WAIT_TIMEOUT"):
        st.warning(title, icon=icon)
    else:
        st.error(title, icon=icon)

    with st.container(border=True):
        st.markdown("**Why this happened**")
        st.write(cause)
        st.markdown("**What to do about it**")
        st.write(fix)

        detail = extra or said
        if detail:
            st.markdown("**What the scheduler said**")
            st.code(str(detail).strip()[-2000:], language=None)
        if job.get("error_code"):
            st.caption(f"Error code: `{job['error_code']}` · catalogue: `{code}`")
        else:
            st.caption(f"Error code: `{code}`")
    return code


# --------------------------------------------------------------------------- #
# surviving a server restart
# --------------------------------------------------------------------------- #

def _token(scenario: str, time_limit: int) -> str:
    """A marker that carries its own facts, so it still means something after
    the process that created it has been killed."""
    return f"{str(scenario).upper()[:1]}-{int(time_limit)}-{int(time.time())}"


def _read_token(raw: str) -> Dict[str, Any]:
    parts = str(raw).split("-")
    scenario = parts[0][:1].upper() if parts and parts[0] else "?"
    try:
        limit = int(parts[1])
    except (IndexError, ValueError):
        limit = 0
    return {"scenario": scenario if scenario in ("A", "B", "C") else "?", "time_limit": limit}


def mark_run_started(scenario: str, time_limit: int) -> None:
    """Leave a breadcrumb in the URL before a solve begins."""
    try:
        st.query_params[RUN_PARAM] = _token(scenario, time_limit)
    except Exception:  # pragma: no cover - never let bookkeeping break a solve
        pass
    st.session_state["run_in_progress"] = True


def mark_run_finished() -> None:
    """Take the breadcrumb away: this run reached an outcome we can show."""
    st.session_state["run_in_progress"] = False
    try:
        if RUN_PARAM in st.query_params:
            del st.query_params[RUN_PARAM]
    except Exception:  # pragma: no cover
        pass


def render_global_notices() -> None:
    """Show anything the whole app needs to say, whatever screen is up.

    Today that is exactly one thing: a run that started but whose server never
    came back. Called at the top of every rerun; it draws nothing in the normal
    case and never raises.
    """
    try:
        raw = st.query_params.get(RUN_PARAM)
    except Exception:  # pragma: no cover - older/embedded runtimes
        return
    if not raw:
        return

    # a run in this very session, still going or already shown: not a loss
    if st.session_state.get("run_in_progress") or st.session_state.get("job"):
        return

    facts = _read_token(raw if isinstance(raw, str) else str(raw))
    title, _cause, fix = entry("SESSION_LOST")
    scenario = facts["scenario"]
    st.error(
        f"**{title}.** Your previous run (Scenario {scenario}) was lost because the "
        "server restarted mid-solve, most often from running out of memory. Your "
        f"uploaded files are gone too, so load them again. **Fix:** {fix}",
        icon=":material/memory:",
    )
    try:
        del st.query_params[RUN_PARAM]
    except Exception:  # pragma: no cover
        pass
