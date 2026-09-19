"""Running the scheduler, and the screen for when it does not finish.

The solve happens in a **separate child process**, not inside the Streamlit
script run. That is deliberate: CP-SAT's memory grows with search time, and on
a small container (Cloud Run's 512 MiB default) a Scenario C solve can push the
whole process over the limit. When that happened inside the Streamlit process
the server itself was killed, the browser reconnected into a fresh empty
session, and the controller was dumped back on step 1 with nothing to read.

With the solve in a child, the kernel kills the child and Streamlit survives,
so we can say what happened and keep the controller where they were.

The 8 files the controller loaded are written to a throwaway folder, the child
runs the pipeline against it, the 3 output CSVs are read back into memory for
the download buttons, and the folder is deleted. Nothing is cached between runs
and nothing is shared between viewers.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict

import streamlit as st

from ui import errors, state
from ui.state import INSTANCE_FILES, REPO_ROOT, SUBMISSION_FILES

#: how long we wait past the solver's own limit before giving up on the child
WAIT_MARGIN_SECONDS = 120

#: the solver may retry with an extended horizon, and each retry gets the full
#: time limit again (CLAUDE.md §9 item 5), so a 60-second run can legitimately
#: take several minutes. Without this the parent would stop a healthy run and
#: report a wait timeout instead of the solver's own, clearer answer.
RETRY_ALLOWANCE = 4

#: exit codes that mean "the operating system killed it", i.e. out of memory.
#: POSIX reports SIGKILL as -9 and shells report it as 137; Windows passes 137
#: straight through when the child exits with it.
_OOM_EXIT_CODES = {-9, 137, 3221225477}


def _solve_in_child(
    instance_dir: str,
    scenario: str,
    time_limit: float,
    result_path: str,
    stderr_path: str,
    repo_root: str,
) -> None:
    """Child-process entry point: solve, then write the job body as JSON.

    Runs in a fresh interpreter (spawn), so it re-establishes its own import
    path and sends nothing back through shared memory — the parent reads the
    JSON file. Anything it prints or raises lands in ``stderr_path``.
    """
    if repo_root not in sys.path:
        sys.path.insert(0, repo_root)

    handle = open(stderr_path, "w", encoding="utf-8", errors="replace")
    sys.stderr = handle
    try:
        # env-gated crash test: pretend the kernel killed us mid-solve
        if os.environ.get("NGEEBULA_TEST_CRASH") == "1":
            handle.write("NGEEBULA_TEST_CRASH=1: simulating an out-of-memory kill\n")
            handle.flush()
            os._exit(137)

        from app.pipeline import run_pipeline

        outcome = run_pipeline(
            instance_dir,
            scenario,
            time_limit_seconds=float(time_limit),
            num_workers=1,  # deterministic output; see CLAUDE.md §12
            out_dir=instance_dir,
            label=f"streamlit scenario {scenario}",
        )
        temporary = result_path + ".part"
        with open(temporary, "w", encoding="utf-8") as out:
            json.dump(outcome, out)
        os.replace(temporary, result_path)  # only now is it readable: no half files
    except BaseException:  # pragma: no cover - the parent reports this
        import traceback

        traceback.print_exc(file=handle)
        handle.flush()
        os._exit(1)
    finally:
        try:
            handle.flush()
            handle.close()
        except Exception:
            pass


def _tail(path: Path, lines: int = 12) -> str:
    """The last few lines the child printed, for the crash screen."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])


def _failure(status: str, message: str, error_code: str, elapsed: float) -> Dict[str, Any]:
    return {
        "status": status,
        "message": message,
        "error_code": error_code,
        "solver_status": None,
        "elapsed_s": elapsed,
    }


def _run_solve(folder: Path, scenario: str, time_limit: int) -> Dict[str, Any]:
    """Start the child, watch it, and come back with a job body either way."""
    result_path = folder / "_job.json"
    stderr_path = folder / "_child.log"
    started = time.time()

    context = mp.get_context("spawn")
    child = context.Process(
        target=_solve_in_child,
        args=(
            str(folder),
            scenario,
            float(time_limit),
            str(result_path),
            str(stderr_path),
            str(REPO_ROOT),
        ),
        daemon=False,
    )

    deadline = time_limit * RETRY_ALLOWANCE + WAIT_MARGIN_SECONDS
    with st.status(
        f"Building the schedule for Scenario {scenario}…", expanded=True
    ) as status:
        st.write("Placing every job across the whole planning period…")
        progress = st.empty()
        child.start()

        while child.is_alive():
            elapsed = time.time() - started
            if elapsed > deadline:
                child.terminate()
                child.join(10)
                if child.is_alive():  # pragma: no cover - last resort
                    child.kill()
                    child.join(5)
                status.update(
                    label=f"Scenario {scenario} was stopped after {elapsed:.0f} s",
                    state="error",
                    expanded=False,
                )
                return _failure(
                    "error",
                    f"The scheduler was still running {elapsed:.0f} seconds in, well past "
                    f"the {time_limit} second limit (which it may retry up to "
                    f"{RETRY_ALLOWANCE} times) plus a {WAIT_MARGIN_SECONDS} second grace "
                    "period, so it was stopped.",
                    "WAIT_TIMEOUT",
                    elapsed,
                )
            progress.write(f"Working… {elapsed:.0f} s of up to {deadline:.0f} s.")
            time.sleep(1.0)

        child.join()
        elapsed = time.time() - started
        exitcode = child.exitcode

        if result_path.exists():
            try:
                outcome = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                outcome = _failure(
                    "error",
                    f"The scheduler's answer could not be read back: {exc}",
                    "BAD_RESULT",
                    elapsed,
                )
            outcome.setdefault("elapsed_s", elapsed)
            st.write("Checking every safety rule, buffer and weekly limit…")
            st.write("Scoring the result against the scenario's price list…")
            st.write(f"Finished in {float(outcome.get('elapsed_s') or elapsed):.1f} seconds.")
            status.update(
                label=(
                    f"Scenario {scenario} finished in "
                    f"{float(outcome.get('elapsed_s') or elapsed):.1f} s"
                ),
                state="complete" if outcome.get("status") == "done" else "error",
                expanded=False,
            )
            return outcome

        # no answer file: the child died before it could write one
        tail = _tail(stderr_path)
        if exitcode is not None and (exitcode < 0 or exitcode in _OOM_EXIT_CODES):
            outcome = _failure(
                "error",
                "OUT OF MEMORY: the scheduler was stopped by the server because it ran "
                "out of memory. Scenario C uses more memory than A or B. Fix: give the "
                "server at least 2 GiB (Cloud Run: Edit & deploy new revision → Memory → "
                "2 GiB), or lower the time limit."
                + (f"\n\n{tail}" if tail else ""),
                "OUT_OF_MEMORY",
                elapsed,
            )
        else:
            outcome = _failure(
                "error",
                f"The scheduler crashed (exit code {exitcode})."
                + (f"\n\n{tail}" if tail else ""),
                "CRASH",
                elapsed,
            )
        status.update(
            label=f"Scenario {scenario} stopped after {elapsed:.0f} s",
            state="error",
            expanded=False,
        )
        return outcome


def render_running() -> None:
    """Do the solve, keep the result, then move to the screen that fits it."""
    scenario = st.session_state["scenario"]
    time_limit = int(st.session_state.get("time_limit", state.TIME_LIMIT_DEFAULT))
    files = st.session_state["files"]

    if len(files) != len(INSTANCE_FILES):
        st.session_state["problem"] = (
            "Some of the 8 planning files went missing between choosing the rules and "
            "starting the scheduler. Load all 8 again."
        )
        state.go("upload")
        return

    errors.mark_run_started(scenario, time_limit)
    folder = Path(tempfile.mkdtemp(prefix="trackaccess_"))
    try:
        for name, blob in files.items():
            (folder / name).write_bytes(blob)

        outcome = _run_solve(folder, scenario, time_limit)
        outcome["scenario"] = scenario

        st.session_state["job"] = outcome
        st.session_state["selected_activity"] = None
        st.session_state["csvs"] = (
            {
                name: (folder / name).read_bytes()
                for name in SUBMISSION_FILES
                if (folder / name).exists()
            }
            if outcome.get("status") == "done"
            else {}
        )
    finally:
        shutil.rmtree(folder, ignore_errors=True)
        # whatever happened, we have something to show: the session stays put
        errors.mark_run_finished()

    if outcome.get("status") == "done" and not st.session_state["csvs"]:
        st.session_state["job"]["download_warning"] = "NO_DOWNLOAD"

    state.go("result" if outcome.get("status") == "done" else "failed")


def render_failed() -> None:
    """One screen, one catalogue entry, always with a next step to take."""
    job: Dict[str, Any] = dict(st.session_state.get("job") or {})
    job.setdefault("scenario", st.session_state.get("scenario"))
    if not job:
        job = {"status": "error", "message": "", "error_code": "UNKNOWN"}

    code = errors.render_failure(job)

    if code in ("OUT_OF_MEMORY", "WAIT_TIMEOUT"):
        st.info(
            "Nothing you loaded was lost. The 8 files are still here — go back, "
            "lower the time limit or pick another scenario, and run it again.",
            icon=":material/info:",
        )

    st.space("medium")
    with st.container(horizontal=True, horizontal_alignment="distribute"):
        if st.button(
            "Back: change rules and try again",
            type="primary",
            icon=":material/arrow_back:",
        ):
            state.go("rules")
        if st.button("Start over with new files", icon=":material/restart_alt:"):
            state.reset()
            state.go("upload")
