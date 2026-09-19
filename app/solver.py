from __future__ import annotations
import argparse
import csv
import datetime as dt
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple
try:
    from ortools.sat.python import cp_model
except ImportError as exc:
    raise ImportError(
        "OR-Tools is required. Install it with: pip install ortools"
    ) from exc
TABLE_ALIASES: Dict[str, Tuple[str, ...]] = {
    "lines": ("lines", "01_LINES.csv", "01_LINES", "LINES"),
    "stations": ("stations", "02_STATIONS.csv", "02_STATIONS", "STATIONS"),
    "sectors": ("sectors", "03_SECTORS.csv", "03_SECTORS", "SECTORS"),
    "location_supply": (
        "location_supply",
        "04_LOCATION_SUPPLY.csv",
        "04_LOCATION_SUPPLY",
        "LOCATION_SUPPLY",
    ),
    "buffer_location": (
        "buffer_location",
        "05_BUFFER_LOCATION.csv",
        "05_BUFFER_LOCATION",
        "BUFFER_LOCATION",
    ),
    "parameters": (
        "parameters",
        "06_PARAMETERS.csv",
        "06_PARAMETERS",
        "PARAMETERS",
    ),
    "project_details": (
        "project_details",
        "07_PROJECT_DETAILS.csv",
        "07_PROJECT_DETAILS",
        "PROJECT_DETAILS",
    ),
    "activity_details": (
        "activity_details",
        "08_ACTIVITY_DETAILS.csv",
        "08_ACTIVITY_DETAILS",
        "ACTIVITY_DETAILS",
    ),
}
REQUIRED_TABLES = (
    "sectors",
    "location_supply",
    "buffer_location",
    "parameters",
    "project_details",
    "activity_details",
)
PRIORITY_BASE_WEIGHT = {1: 100, 2: 10, 3: 1}
ACTIVITY_PRIORITY_TENTHS = {1: 3, 2: 2, 3: 0}
PHYSICAL_NIGHTS_PER_WEEK = 7
def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()
def _as_int(value: Any, field: str) -> int:
    text = _clean(value)
    if text == "":
        raise ValueError(f"Missing integer value for {field}")
    try:
        return int(float(text))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid integer for {field}: {value!r}") from exc
def _parse_date(value: Any, field: str) -> dt.date:
    text = _clean(value)
    if not text:
        raise ValueError(f"Missing date value for {field}")
    try:
        return dt.date.fromisoformat(text[:10])
    except ValueError as exc:
        raise ValueError(f"Invalid ISO date for {field}: {value!r}") from exc
def _records(value: Any) -> List[Dict[str, Any]]:
    """Accept list[dict], tuple[dict], pandas DataFrame, or similar records."""
    if value is None:
        return []
    if isinstance(value, list):
        return [dict(row) for row in value]
    if isinstance(value, tuple):
        return [dict(row) for row in value]
    if hasattr(value, "to_dict"):
        try:
            rows = value.to_dict("records")
            return [dict(row) for row in rows]
        except TypeError:
            pass
    if isinstance(value, Mapping):
        return [dict(value)]
    raise TypeError(f"Unsupported table type: {type(value).__name__}")
def _find_table(data: Mapping[str, Any], canonical: str) -> List[Dict[str, Any]]:
    aliases = TABLE_ALIASES[canonical]
    for alias in aliases:
        if alias in data:
            return _records(data[alias])
    lower_lookup = {str(k).lower(): k for k in data}
    for alias in aliases:
        key = lower_lookup.get(alias.lower())
        if key is not None:
            return _records(data[key])
    return []
def _normalise_instance(data: Mapping[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    tables = {name: _find_table(data, name) for name in TABLE_ALIASES}
    missing = [name for name in REQUIRED_TABLES if not tables[name]]
    if missing:
        raise ValueError(
            "Missing required PS1 tables: " + ", ".join(missing)
        )
    return tables
def _parameters_dict(rows: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    result: Dict[str, str] = {}
    for row in rows:
        key = _clean(row.get("key"))
        if key:
            result[key] = _clean(row.get("value"))
    return result
def _week_of(date_value: dt.date, horizon_start: dt.date) -> int:
    """Week 1 is horizon_start .. horizon_start+6 days."""
    return ((date_value - horizon_start).days // 7) + 1
def _week_end_date(week: int, horizon_start: dt.date) -> dt.date:
    return horizon_start + dt.timedelta(days=week * 7 - 1)
def _date_day_index(date_value: dt.date, horizon_start: dt.date) -> int:
    return (date_value - horizon_start).days
def _read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))
def load_instance(folder: str | Path) -> Dict[str, List[Dict[str, str]]]:
    """
    Load the official eight CSV files from a folder.
    Returns canonical keys such as `project_details` and `activity_details`.
    """
    folder = Path(folder)
    if not folder.exists():
        raise FileNotFoundError(folder)
    result: Dict[str, List[Dict[str, str]]] = {}
    for canonical, aliases in TABLE_ALIASES.items():
        csv_name = next((name for name in aliases if name.endswith(".csv")), None)
        if not csv_name:
            continue
        path = folder / csv_name
        if path.exists():
            result[canonical] = _read_csv(path)
    missing = [name for name in REQUIRED_TABLES if name not in result]
    if missing:
        raise ValueError(
            f"{folder} is missing required files for: {', '.join(missing)}"
        )
    return result
def _write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})
def write_submission(result: Mapping[str, Any], output_dir: str | Path) -> Dict[str, str]:
    """Write the three official submission CSVs for one scenario."""
    if result.get("status") != "success":
        raise ValueError("Cannot write submission because the solver did not succeed")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    access_path = output_dir / "SCHEDULE_ACCESS.csv"
    occupancy_path = output_dir / "SCHEDULE_OCCUPANCY.csv"
    results_path = output_dir / "RESULTS.csv"
    _write_csv(
        access_path,
        ["activity_id", "access_seq", "week", "eclo", "access_night"],
        result["schedule_access"],
    )
    _write_csv(
        occupancy_path,
        ["activity_id", "week", "location_id", "co_share_group"],
        result["schedule_occupancy"],
    )
    _write_csv(
        results_path,
        ["scenario", "contract_number", "simulated_completion_date", "overrun_days"],
        result["results"],
    )
    return {
        "SCHEDULE_ACCESS.csv": str(access_path),
        "SCHEDULE_OCCUPANCY.csv": str(occupancy_path),
        "RESULTS.csv": str(results_path),
    }
def _parse_track_location(location_id: str) -> Tuple[str, str, str, str]:
    """
    Parse e.g. SEC:ALP:S03_S04:EB or PLAT:ALP:S03:EB.
    Returns (kind, line, middle, bound).
    """
    parts = _clean(location_id).split(":")
    if len(parts) != 4:
        raise ValueError(f"Unexpected location_id format: {location_id!r}")
    kind, line, middle, bound = parts
    if kind not in {"SEC", "PLAT"}:
        raise ValueError(f"Unknown location kind in {location_id!r}")
    if bound not in {"EB", "WB"}:
        raise ValueError(f"Unknown bound in {location_id!r}")
    return kind, line, middle, bound
def _sector_base_id(location_id: str) -> str:
    kind, line, middle, _bound = _parse_track_location(location_id)
    if kind != "SEC":
        raise ValueError(f"Activity start/end must be sector locations: {location_id}")
    return f"SEC:{line}:{middle}"
def _swap_bound(location_id: str) -> str:
    kind, line, middle, bound = _parse_track_location(location_id)
    return f"{kind}:{line}:{middle}:{'WB' if bound == 'EB' else 'EB'}"
def _line_of_location(location_id: str) -> str:
    return _parse_track_location(location_id)[1]
def _build_sector_index(
    sectors: Sequence[Mapping[str, Any]],
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    by_line: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for raw in sectors:
        row = dict(raw)
        sector_id = _clean(row.get("sector_id"))
        line = _clean(row.get("line_code"))
        if not sector_id or not line:
            raise ValueError("SECTORS row missing sector_id or line_code")
        row["seq"] = _as_int(row.get("seq"), "SECTORS.seq")
        by_id[sector_id] = row
        by_line[line].append(row)
    for line in by_line:
        by_line[line].sort(key=lambda r: int(r["seq"]))
    return by_id, dict(by_line)
def _locations_for_sector_rows(
    sector_rows: Sequence[Mapping[str, Any]],
    line: str,
    bound: str,
) -> Set[str]:
    locations: Set[str] = set()
    stations: Set[str] = set()
    for row in sector_rows:
        sector_id = _clean(row["sector_id"])
        locations.add(f"{sector_id}:{bound}")
        stations.add(_clean(row["from_station_id"]))
        stations.add(_clean(row["to_station_id"]))
    for station in stations:
        locations.add(f"PLAT:{line}:{station}:{bound}")
    return locations
def _normalise_nature_label(value: Any) -> str:
    """Return a stable, case-insensitive nature-of-works label."""
    text = " ".join(_clean(value).lower().split())
    return text.replace("–", "-").replace("—", "-")
def _resolve_nature_buffer(
    raw_nature: Any, buffer_by_nature: Mapping[str, int]
) -> Tuple[str, int]:
    """
    Match PROJECT_DETAILS.nature_of_activity to BUFFER_LOCATION safely.
    Exact normalized matches win.  For descriptive variants such as
    ``Live (750V)``, use the longest compatible normalized key.  Longest-first
    matching avoids accidentally classifying ``Non-live (Consist)`` as
    ``Live`` merely because the word "live" appears inside "non-live".
    """
    norm_nature = _normalise_nature_label(raw_nature)
    if not norm_nature:
        raise ValueError("Missing nature_of_activity")
    if norm_nature in buffer_by_nature:
        return norm_nature, int(buffer_by_nature[norm_nature])
    for key in sorted(buffer_by_nature, key=len, reverse=True):
        norm_key = _normalise_nature_label(key)
        if not norm_key:
            continue
        if norm_key == "live" and "non-live" in norm_nature:
            continue
        if norm_key in norm_nature or norm_nature in norm_key:
            return norm_key, int(buffer_by_nature[key])
    raise ValueError(
        f"Unrecognised nature_of_activity {raw_nature!r}; "
        f"known buffer types are {sorted(buffer_by_nature)}"
    )
def _get_interchange_hubs(
    stations: Sequence[Mapping[str, Any]],
    sectors: Sequence[Mapping[str, Any]],
) -> Set[str]:
    """
    Derive interchange platform IDs and shared-sector middle IDs from data.
    Examples for the public instance are H01, H02 and H01_H02, but the solver
    does not rely on those literal identifiers.  The fallback preserves
    compatibility with reduced test fixtures that omit 02_STATIONS.csv flags.
    """
    hubs: Set[str] = set()
    interchange_stations: Set[str] = set()
    truthy = {"1", "true", "yes", "y"}
    for station in stations:
        if _clean(station.get("is_interchange")).lower() in truthy:
            station_id = _clean(station.get("station_id"))
            if station_id:
                interchange_stations.add(station_id)
                hubs.add(station_id)
    for sector in sectors:
        is_shared = _clean(sector.get("is_shared")).lower() in truthy
        from_station = _clean(sector.get("from_station_id"))
        to_station = _clean(sector.get("to_station_id"))
        between_interchanges = (
            from_station in interchange_stations
            and to_station in interchange_stations
        )
        if not (is_shared or between_interchanges):
            continue
        sector_id = _clean(sector.get("sector_id"))
        parts = sector_id.split(":")
        if len(parts) >= 3 and parts[0] == "SEC" and parts[2]:
            hubs.add(parts[2])
    return hubs if hubs else {"H01", "H02", "H01_H02"}
def _activity_route_and_closure(
    activity: Mapping[str, Any],
    project: Mapping[str, Any],
    sector_by_id: Mapping[str, Mapping[str, Any]],
    sectors_by_line: Mapping[str, Sequence[Mapping[str, Any]]],
    buffer_by_nature: Mapping[str, int],
    all_location_ids: Set[str],
    interchange_hubs: Set[str],
) -> Tuple[Set[str], Set[str], Set[str]]:
    """
    Return (occupied_locations, closure_locations, affected_lines).
    Closure expansion is deliberately safety-first:
    * route locations are closed;
    * Consist/Live extend by the configured number of adjacent sectors;
    * Live mirrors onto the opposite bound;
    * when a Live closure reaches a data-defined interchange hub/shared sector,
      corresponding interchange locations on the other line(s) close too.
    """
    start_location = _clean(activity.get("start_location_id"))
    end_location = _clean(activity.get("end_location_id"))
    _skind, start_line, _smid, start_bound = _parse_track_location(start_location)
    _ekind, end_line, _emid, end_bound = _parse_track_location(end_location)
    if start_line != end_line or start_bound != end_bound:
        raise ValueError(
            f"Activity {activity.get('activity_id')} crosses line/bound in start/end; "
            "the public format expects one line and one bound per activity"
        )
    start_base = _sector_base_id(start_location)
    end_base = _sector_base_id(end_location)
    if start_base not in sector_by_id or end_base not in sector_by_id:
        raise ValueError(
            f"Activity {activity.get('activity_id')} references a sector missing from SECTORS"
        )
    start_seq = int(sector_by_id[start_base]["seq"])
    end_seq = int(sector_by_id[end_base]["seq"])
    low_seq, high_seq = sorted((start_seq, end_seq))
    line_rows = list(sectors_by_line[start_line])
    route_rows = [r for r in line_rows if low_seq <= int(r["seq"]) <= high_seq]
    route = _locations_for_sector_rows(route_rows, start_line, start_bound)
    matched_nature, buffer_size = _resolve_nature_buffer(
        project.get("nature_of_activity"), buffer_by_nature
    )
    closure_rows = [
        r
        for r in line_rows
        if low_seq - buffer_size <= int(r["seq"]) <= high_seq + buffer_size
    ]
    closure = _locations_for_sector_rows(closure_rows, start_line, start_bound)
    if matched_nature == "live":
        closure |= {_swap_bound(loc) for loc in list(closure)}
        reaches_interchange = any(
            _parse_track_location(loc)[2] in interchange_hubs for loc in closure
        )
        if reaches_interchange:
            for other_line, other_rows_raw in sectors_by_line.items():
                if other_line == start_line:
                    continue
                other_rows = list(other_rows_raw)
                interchange_indices: Set[int] = set()
                for idx, row in enumerate(other_rows):
                    sector_id = _clean(row.get("sector_id"))
                    parts = sector_id.split(":")
                    middle = parts[2] if len(parts) >= 3 else ""
                    from_station = _clean(row.get("from_station_id"))
                    to_station = _clean(row.get("to_station_id"))
                    if middle in interchange_hubs or (
                        from_station in interchange_hubs
                        and to_station in interchange_hubs
                    ):
                        interchange_indices.add(idx)
                cross_rows: List[Mapping[str, Any]] = []
                selected_indices: Set[int] = set()
                for idx in interchange_indices:
                    lo = max(0, idx - buffer_size)
                    hi = min(len(other_rows) - 1, idx + buffer_size)
                    selected_indices.update(range(lo, hi + 1))
                for idx in sorted(selected_indices):
                    cross_rows.append(other_rows[idx])
                if cross_rows:
                    cross_closure = _locations_for_sector_rows(
                        cross_rows, other_line, start_bound
                    )
                    closure |= cross_closure
                    closure |= {_swap_bound(loc) for loc in cross_closure}
    closure &= all_location_ids
    route &= all_location_ids
    affected_lines = {_line_of_location(loc) for loc in closure}
    if not route:
        raise ValueError(
            f"Activity {activity.get('activity_id')} expands to no valid occupancy locations"
        )
    return route, closure, affected_lines
def _co_share_pair_allowed(
    access_type_a: str,
    access_type_b: str,
    route_a: Set[str],
    route_b: Set[str],
    closure_a: Set[str],
    closure_b: Set[str],
) -> bool:
    """
    Two activities may co-share a physical night only when their possession
    types form a legal pair (C+C or PC+C) and their actual worksites overlap.
    Safety-closure overlap is intentionally not used as the co-sharing test:
    buffers are exclusion zones, not proof that the two jobs share a worksite.
    """
    pair = {access_type_a, access_type_b}
    compatible_types = (
        access_type_a == "C" and access_type_b == "C"
    ) or pair == {"PC", "C"}
    if not compatible_types:
        return False
    return bool(route_a & route_b)
def _validate_predecessors(activities: Mapping[str, Mapping[str, Any]]) -> None:
    """Catch unknown predecessor IDs and cycles before CP-SAT is built."""
    graph: Dict[str, Optional[str]] = {}
    for activity_id, row in activities.items():
        predecessor = _clean(row.get("predecessor_activity_id")) or None
        if predecessor and predecessor not in activities:
            raise ValueError(
                f"Activity {activity_id} references unknown predecessor {predecessor}"
            )
        if predecessor == activity_id:
            raise ValueError(f"Activity {activity_id} cannot depend on itself")
        graph[activity_id] = predecessor
    WHITE, GREY, BLACK = 0, 1, 2
    state = {activity_id: WHITE for activity_id in activities}
    def visit(node: str, stack: List[str]) -> None:
        if state[node] == BLACK:
            return
        if state[node] == GREY:
            cycle_start = stack.index(node) if node in stack else 0
            cycle = stack[cycle_start:] + [node]
            raise ValueError("Predecessor cycle detected: " + " -> ".join(cycle))
        state[node] = GREY
        stack.append(node)
        predecessor = graph[node]
        if predecessor:
            visit(predecessor, stack)
        stack.pop()
        state[node] = BLACK
    for activity_id in activities:
        if state[activity_id] == WHITE:
            visit(activity_id, [])
def solve_schedule(
    data: Mapping[str, Any],
    scenario: str = "A",
    *,
    time_limit_seconds: float = 30.0,
    num_workers: Optional[int] = 1,
    random_seed: int = 42,
) -> Dict[str, Any]:
    """
    Solve one NebulaX PS1 scenario.
    Parameters
    ----------
    data:
        Mapping containing the official CSV tables. Values may be list[dict]
        or pandas DataFrames. Exact filenames (e.g. "08_ACTIVITY_DETAILS.csv")
        and canonical keys (e.g. "activity_details") are both accepted.
    scenario:
        "A", "B" or "C".
    Returns
    -------
    dict containing status, official output rows, metrics and debug metadata.
    """
    scenario = _clean(scenario).upper()
    if scenario not in {"A", "B", "C"}:
        raise ValueError("scenario must be one of: A, B, C")
    tables = _normalise_instance(data)
    params = _parameters_dict(tables["parameters"])
    horizon_start = _parse_date(params.get("horizon_start"), "horizon_start")
    horizon_weeks = _as_int(params.get("horizon_weeks"), "horizon_weeks")
    weeks = list(range(1, horizon_weeks + 1))
    physical_nights = list(range(1, PHYSICAL_NIGHTS_PER_WEEK + 1))
    projects: Dict[str, Dict[str, Any]] = {}
    for row in tables["project_details"]:
        contract = _clean(row.get("contract_number"))
        if not contract:
            raise ValueError("PROJECT_DETAILS row missing contract_number")
        projects[contract] = dict(row)
    activities: Dict[str, Dict[str, Any]] = {}
    for row in tables["activity_details"]:
        activity_id = _clean(row.get("activity_id"))
        if not activity_id:
            raise ValueError("ACTIVITY_DETAILS row missing activity_id")
        contract = _clean(row.get("contract_number"))
        if contract not in projects:
            raise ValueError(
                f"Activity {activity_id} references unknown contract {contract!r}"
            )
        activities[activity_id] = dict(row)
    _validate_predecessors(activities)
    sector_by_id, sectors_by_line = _build_sector_index(tables["sectors"])
    supply: Dict[str, int] = {}
    for row in tables["location_supply"]:
        location_id = _clean(row.get("location_id"))
        supply[location_id] = _as_int(row.get("supply_capacity"), "supply_capacity")
    all_location_ids = set(supply)
    buffer_by_nature: Dict[str, int] = {}
    for row in tables["buffer_location"]:
        nature = _normalise_nature_label(row.get("nature_of_works"))
        if not nature:
            raise ValueError("BUFFER_LOCATION row missing nature_of_works")
        buffer_by_nature[nature] = _as_int(
            row.get("up_to_buffer_sectors"), "up_to_buffer_sectors"
        )
    interchange_hubs = _get_interchange_hubs(
        tables.get("stations", []), tables["sectors"]
    )
    activity_ids = list(activities)
    route: Dict[str, Set[str]] = {}
    closure: Dict[str, Set[str]] = {}
    affected_lines: Dict[str, Set[str]] = {}
    earliest_week: Dict[str, int] = {}
    activity_contract: Dict[str, str] = {}
    activity_access_type: Dict[str, str] = {}
    activity_nature: Dict[str, str] = {}
    for activity_id, activity in activities.items():
        contract = _clean(activity.get("contract_number"))
        project = projects[contract]
        activity_contract[activity_id] = contract
        activity_access_type[activity_id] = _clean(project.get("access_type"))
        activity_nature[activity_id] = _clean(project.get("nature_of_activity"))
        start_date = _parse_date(
            activity.get("planned_start_date"),
            f"{activity_id}.planned_start_date",
        )
        earliest_week[activity_id] = max(1, _week_of(start_date, horizon_start))
        r, c, lines = _activity_route_and_closure(
            activity,
            project,
            sector_by_id,
            sectors_by_line,
            buffer_by_nature,
            all_location_ids,
            interchange_hubs,
        )
        route[activity_id] = r
        closure[activity_id] = c
        affected_lines[activity_id] = lines
    activities_at_location: Dict[str, List[str]] = defaultdict(list)
    for activity_id in activity_ids:
        for location_id in route[activity_id]:
            activities_at_location[location_id].append(activity_id)
    model = cp_model.CpModel()
    scheduled: Dict[Tuple[str, int], cp_model.IntVar] = {}
    normal: Dict[Tuple[str, int], cp_model.IntVar] = {}
    eclo: Dict[Tuple[str, int], cp_model.IntVar] = {}
    physical: Dict[Tuple[str, int, int], cp_model.IntVar] = {}
    local_access: Dict[Tuple[str, int, int], cp_model.IntVar] = {}
    for activity_id in activity_ids:
        contract = activity_contract[activity_id]
        cap = _as_int(
            projects[contract].get("number_of_maximum_access_per_week"),
            f"{contract}.number_of_maximum_access_per_week",
        )
        for week in weeks:
            s = model.NewBoolVar(f"scheduled__{activity_id}__w{week}")
            n = model.NewBoolVar(f"normal__{activity_id}__w{week}")
            e = model.NewBoolVar(f"eclo__{activity_id}__w{week}")
            scheduled[activity_id, week] = s
            normal[activity_id, week] = n
            eclo[activity_id, week] = e
            model.Add(n + e == s)
            if week < earliest_week[activity_id]:
                model.Add(s == 0)
            if scenario == "A":
                model.Add(e == 0)
            pvars = []
            for night in physical_nights:
                var = model.NewBoolVar(
                    f"physical__{activity_id}__w{week}__d{night}"
                )
                physical[activity_id, week, night] = var
                pvars.append(var)
            model.Add(sum(pvars) == s)
            avars = []
            for access_night in range(1, cap + 1):
                var = model.NewBoolVar(
                    f"accessnight__{activity_id}__w{week}__n{access_night}"
                )
                local_access[activity_id, week, access_night] = var
                avars.append(var)
            model.Add(sum(avars) == s)
    for activity_id, activity in activities.items():
        required_half_units = 2 * _as_int(
            activity.get("total_accesses"), f"{activity_id}.total_accesses"
        )
        delivered = sum(
            2 * normal[activity_id, week] + 3 * eclo[activity_id, week]
            for week in weeks
        )
        model.Add(delivered >= required_half_units)
        model.Add(delivered <= required_half_units + 1)
    start_week: Dict[str, cp_model.IntVar] = {}
    end_week: Dict[str, cp_model.IntVar] = {}
    sentinel = horizon_weeks + 1
    for activity_id in activity_ids:
        start = model.NewIntVar(1, horizon_weeks, f"startweek__{activity_id}")
        end = model.NewIntVar(1, horizon_weeks, f"endweek__{activity_id}")
        start_week[activity_id] = start
        end_week[activity_id] = end
        start_candidates = []
        end_candidates = []
        for week in weeks:
            sc = model.NewIntVar(1, sentinel, f"startcand__{activity_id}__w{week}")
            ec = model.NewIntVar(0, horizon_weeks, f"endcand__{activity_id}__w{week}")
            model.Add(
                sc
                == week * scheduled[activity_id, week]
                + sentinel * (1 - scheduled[activity_id, week])
            )
            model.Add(ec == week * scheduled[activity_id, week])
            start_candidates.append(sc)
            end_candidates.append(ec)
        model.AddMinEquality(start, start_candidates)
        model.AddMaxEquality(end, end_candidates)
    for successor_id, activity in activities.items():
        predecessor_id = _clean(activity.get("predecessor_activity_id"))
        if predecessor_id:
            model.Add(start_week[successor_id] >= end_week[predecessor_id] + 1)
    activities_by_contract_type: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for activity_id, activity in activities.items():
        key = (
            activity_contract[activity_id],
            _clean(activity.get("activity_type")),
        )
        activities_by_contract_type[key].append(activity_id)
    for (contract, _activity_type), ids in activities_by_contract_type.items():
        project = projects[contract]
        cap = _as_int(
            project.get("number_of_maximum_access_per_week"),
            f"{contract}.number_of_maximum_access_per_week",
        )
        workfronts = _as_int(
            project.get("number_of_workfronts"),
            f"{contract}.number_of_workfronts",
        )
        for week in weeks:
            for access_night in range(1, cap + 1):
                model.Add(
                    sum(
                        local_access[activity_id, week, access_night]
                        for activity_id in ids
                    )
                    <= workfronts
                )
    access_slot_to_physical: Dict[Tuple[str, str, int, int, int], cp_model.IntVar] = {}
    for (contract, activity_type), ids in activities_by_contract_type.items():
        cap = _as_int(
            projects[contract].get("number_of_maximum_access_per_week"),
            f"{contract}.number_of_maximum_access_per_week",
        )
        safe_activity_type = activity_type or "UNKNOWN"
        for week in weeks:
            for access_night in range(1, cap + 1):
                row = []
                for night in physical_nights:
                    link = model.NewBoolVar(
                        f"slotmap__{contract}__{safe_activity_type}__w{week}"
                        f"__n{access_night}__d{night}"
                    )
                    access_slot_to_physical[
                        contract, activity_type, week, access_night, night
                    ] = link
                    row.append(link)
                model.Add(sum(row) <= 1)
            for night in physical_nights:
                model.Add(
                    sum(
                        access_slot_to_physical[
                            contract, activity_type, week, access_night, night
                        ]
                        for access_night in range(1, cap + 1)
                    )
                    <= 1
                )
            for activity_id in ids:
                for access_night in range(1, cap + 1):
                    for night in physical_nights:
                        model.Add(
                            local_access[activity_id, week, access_night]
                            + physical[activity_id, week, night]
                            <= 1
                            + access_slot_to_physical[
                                contract,
                                activity_type,
                                week,
                                access_night,
                                night,
                            ]
                        )
    location_night_used: Dict[Tuple[str, int, int], cp_model.IntVar] = {}
    excess_by_location_week: Dict[Tuple[str, int], cp_model.IntVar] = {}
    for location_id, nominal_supply in supply.items():
        ids = activities_at_location.get(location_id, [])
        if not ids:
            continue
        for week in weeks:
            used_vars = []
            for night in physical_nights:
                relevant = [physical[a, week, night] for a in ids]
                used = model.NewBoolVar(f"used__{location_id}__w{week}__d{night}")
                location_night_used[location_id, week, night] = used
                used_vars.append(used)
                for var in relevant:
                    model.Add(var <= used)
                model.Add(used <= sum(relevant))
                pm = [
                    physical[a, week, night]
                    for a in ids
                    if activity_access_type[a] == "PM"
                ]
                pc = [
                    physical[a, week, night]
                    for a in ids
                    if activity_access_type[a] == "PC"
                ]
                coworker = [
                    physical[a, week, night]
                    for a in ids
                    if activity_access_type[a] == "C"
                ]
                pm_sum = sum(pm) if pm else 0
                pc_sum = sum(pc) if pc else 0
                c_sum = sum(coworker) if coworker else 0
                model.Add(pm_sum <= 1)
                model.Add(pc_sum <= 1)
                model.Add(pm_sum + pc_sum <= 1)
                model.Add(c_sum + pc_sum + 4 * pm_sum <= 4)
            total_used = sum(used_vars)
            max_excess = max(0, PHYSICAL_NIGHTS_PER_WEEK - nominal_supply)
            excess = model.NewIntVar(
                0,
                max_excess,
                f"excess__{location_id}__w{week}",
            )
            model.Add(excess >= total_used - nominal_supply)
            model.Add(excess >= 0)
            model.Add(excess <= total_used)
            excess_by_location_week[location_id, week] = excess
            if scenario == "A":
                model.Add(total_used <= nominal_supply)
                model.Add(excess == 0)
            elif scenario == "C":
                model.Add(total_used <= nominal_supply + 1)
                model.Add(excess <= 1)
    pair_conflicts = 0
    pair_coshare_exemptions = 0
    for i, a in enumerate(activity_ids):
        for b in activity_ids[i + 1 :]:
            shared_worksite = route[a] & route[b]
            a_inside_b_closure = route[a] & closure[b]
            b_inside_a_closure = route[b] & closure[a]
            closure_collision = closure[a] & closure[b]
            if not (a_inside_b_closure or b_inside_a_closure or closure_collision):
                continue
            co_share_allowed = _co_share_pair_allowed(
                activity_access_type[a],
                activity_access_type[b],
                route[a],
                route[b],
                closure[a],
                closure[b],
            )
            outside_shared_worksite = (
                (a_inside_b_closure | b_inside_a_closure) - shared_worksite
            )
            if co_share_allowed and not outside_shared_worksite:
                pair_coshare_exemptions += 1
                continue
            pair_conflicts += 1
            for week in weeks:
                model.Add(scheduled[a, week] + scheduled[b, week] <= 1)
    closure_conflict_pairs: List[Tuple[str, str]] = []
    for i, a in enumerate(activity_ids):
        for b in activity_ids[i + 1 :]:
            shared_worksite = route[a] & route[b]
            a_inside_b_closure = route[a] & closure[b]
            b_inside_a_closure = route[b] & closure[a]
            if not (a_inside_b_closure or b_inside_a_closure):
                continue
            co_share_allowed = _co_share_pair_allowed(
                activity_access_type[a],
                activity_access_type[b],
                route[a],
                route[b],
                closure[a],
                closure[b],
            )
            outside_shared_worksite = (
                (a_inside_b_closure | b_inside_a_closure) - shared_worksite
            )
            if not (co_share_allowed and not outside_shared_worksite):
                closure_conflict_pairs.append((a, b))
    eclo_window_start: Dict[str, cp_model.IntVar] = {}
    if scenario == "C":
        all_lines = sorted({line for lines in affected_lines.values() for line in lines})
        for line in all_lines:
            eclo_window_start[line] = model.NewIntVar(
                1,
                max(1, horizon_weeks),
                f"eclo_window_start__{line}",
            )
        for activity_id in activity_ids:
            for week in weeks:
                e = eclo[activity_id, week]
                for line in affected_lines[activity_id]:
                    window = eclo_window_start[line]
                    model.Add(week >= window).OnlyEnforceIf(e)
                    model.Add(week <= window + 1).OnlyEnforceIf(e)
    contract_end_week: Dict[str, cp_model.IntVar] = {}
    contract_overrun_days: Dict[str, cp_model.IntVar] = {}
    activities_by_contract: Dict[str, List[str]] = defaultdict(list)
    for activity_id in activity_ids:
        activities_by_contract[activity_contract[activity_id]].append(activity_id)
    horizon_end_day = horizon_weeks * 7 - 1
    for contract, ids in activities_by_contract.items():
        end_var = model.NewIntVar(1, horizon_weeks, f"contract_end_week__{contract}")
        model.AddMaxEquality(end_var, [end_week[a] for a in ids])
        contract_end_week[contract] = end_var
        planned_date = _parse_date(
            projects[contract].get("planned_completion_date"),
            f"{contract}.planned_completion_date",
        )
        planned_day = _date_day_index(planned_date, horizon_start)
        overrun = model.NewIntVar(
            0,
            max(0, horizon_end_day - planned_day),
            f"contract_overrun_days__{contract}",
        )
        completion_day_expr = 7 * end_var - 1
        model.Add(overrun >= completion_day_expr - planned_day)
        model.Add(overrun >= 0)
        contract_overrun_days[contract] = overrun
        if scenario == "B":
            model.Add(completion_day_expr <= planned_day)
            model.Add(overrun == 0)
    activity_overrun_days: Dict[str, cp_model.IntVar] = {}
    for activity_id in activity_ids:
        contract = activity_contract[activity_id]
        planned_date = _parse_date(
            projects[contract].get("planned_completion_date"),
            f"{contract}.planned_completion_date",
        )
        planned_day = _date_day_index(planned_date, horizon_start)
        overrun = model.NewIntVar(
            0,
            max(0, horizon_end_day - planned_day),
            f"activity_overrun_days__{activity_id}",
        )
        model.Add(overrun >= 7 * end_week[activity_id] - 1 - planned_day)
        model.Add(overrun >= 0)
        activity_overrun_days[activity_id] = overrun
    objective_terms = []
    if scenario in {"A", "C"}:
        for activity_id, activity in activities.items():
            contract = activity_contract[activity_id]
            contract_priority = _as_int(
                projects[contract].get("contract_priority"),
                f"{contract}.contract_priority",
            )
            activity_priority = _as_int(
                activity.get("activity_priority"),
                f"{activity_id}.activity_priority",
            )
            base = PRIORITY_BASE_WEIGHT.get(contract_priority, 1)
            nudge = ACTIVITY_PRIORITY_TENTHS.get(activity_priority, 0)
            coefficient = base * (10 + nudge)
            objective_terms.append(coefficient * activity_overrun_days[activity_id])
    if scenario in {"B", "C"}:
        objective_terms.extend(70 * var for var in excess_by_location_week.values())
        objective_terms.extend(
            50 * eclo[activity_id, week]
            for activity_id in activity_ids
            for week in weeks
        )
    objective_terms.extend(
        scheduled[activity_id, week]
        for activity_id in activity_ids
        for week in weeks
    )
    model.Minimize(sum(objective_terms))
    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_seconds)
    solver.parameters.num_search_workers = int(
        num_workers if num_workers is not None else 1
    )
    solver.parameters.random_seed = int(random_seed)
    solver.parameters.log_search_progress = False
    status_code = solver.Solve(model)
    status_name = solver.StatusName(status_code)
    if status_code not in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        return {
            "status": "failure",
            "solver_status": status_name,
            "scenario": scenario,
            "schedule_access": [],
            "schedule_occupancy": [],
            "results": [],
            "metrics": {
                "activities": len(activity_ids),
                "pair_conflicts": pair_conflicts,
                "pair_coshare_exemptions": pair_coshare_exemptions,
            },
            "message": (
                "No feasible solution was found within the model/time limit. "
                "Check the hard constraints, horizon, and hidden-instance data."
            ),
        }
    schedule_access: List[Dict[str, Any]] = []
    chosen_physical_night: Dict[Tuple[str, int], int] = {}
    for activity_id in activity_ids:
        contract = activity_contract[activity_id]
        cap = _as_int(
            projects[contract].get("number_of_maximum_access_per_week"),
            f"{contract}.number_of_maximum_access_per_week",
        )
        seq = 0
        for week in weeks:
            if solver.Value(scheduled[activity_id, week]) != 1:
                continue
            seq += 1
            physical_night = next(
                night
                for night in physical_nights
                if solver.Value(physical[activity_id, week, night]) == 1
            )
            chosen_physical_night[activity_id, week] = physical_night
            access_night = next(
                n
                for n in range(1, cap + 1)
                if solver.Value(local_access[activity_id, week, n]) == 1
            )
            schedule_access.append(
                {
                    "activity_id": activity_id,
                    "access_seq": seq,
                    "week": week,
                    "eclo": int(solver.Value(eclo[activity_id, week])),
                    "access_night": access_night,
                }
            )
    schedule_occupancy: List[Dict[str, Any]] = []
    for access in schedule_access:
        activity_id = str(access["activity_id"])
        week = int(access["week"])
        night = chosen_physical_night[activity_id, week]
        for location_id in sorted(route[activity_id]):
            schedule_occupancy.append(
                {
                    "activity_id": activity_id,
                    "week": week,
                    "location_id": location_id,
                    "co_share_group": f"b{night}",
                }
            )
    for a, b in closure_conflict_pairs:
        for week in weeks:
            if (
                solver.Value(scheduled[a, week]) == 1
                and solver.Value(scheduled[b, week]) == 1
            ):
                overlap = sorted(
                    (route[a] & closure[b]) | (route[b] & closure[a])
                )
                raise RuntimeError(
                    f"Internal closure-safety failure in week {week}: "
                    f"{a} conflicts with {b} at {overlap}"
                )
    results_rows: List[Dict[str, Any]] = []
    for contract in sorted(projects):
        if contract not in contract_end_week:
            continue
        completion_week = int(solver.Value(contract_end_week[contract]))
        completion_date = _week_end_date(completion_week, horizon_start)
        planned_date = _parse_date(
            projects[contract].get("planned_completion_date"),
            f"{contract}.planned_completion_date",
        )
        overrun_days = max(0, (completion_date - planned_date).days)
        results_rows.append(
            {
                "scenario": scenario,
                "contract_number": contract,
                "simulated_completion_date": completion_date.isoformat(),
                "overrun_days": overrun_days,
            }
        )
    total_eclo = sum(int(row["eclo"]) for row in schedule_access)
    total_excess = sum(
        int(solver.Value(var)) for var in excess_by_location_week.values()
    )
    predecessor_checks = []
    for activity_id, activity in activities.items():
        predecessor = _clean(activity.get("predecessor_activity_id"))
        if predecessor:
            predecessor_checks.append(
                {
                    "predecessor": predecessor,
                    "predecessor_last_week": int(solver.Value(end_week[predecessor])),
                    "successor": activity_id,
                    "successor_first_week": int(solver.Value(start_week[activity_id])),
                }
            )
    return {
        "status": "success",
        "solver_status": status_name,
        "scenario": scenario,
        "objective_value": float(solver.ObjectiveValue()),
        "schedule_access": schedule_access,
        "schedule_occupancy": schedule_occupancy,
        "results": results_rows,
        "metrics": {
            "activities": len(activity_ids),
            "access_rows": len(schedule_access),
            "occupancy_rows": len(schedule_occupancy),
            "eclo_nights_total": total_eclo,
            "excess_access_nights_total": total_excess,
            "pair_conflicts": pair_conflicts,
            "pair_coshare_exemptions": pair_coshare_exemptions,
            "wall_time_seconds": float(solver.WallTime()),
        },
        "predecessor_checks": predecessor_checks,
        "debug": {
            "horizon_start": horizon_start.isoformat(),
            "horizon_weeks": horizon_weeks,
            "physical_nights_per_week": PHYSICAL_NIGHTS_PER_WEEK,
        },
    }
def _main() -> None:
    parser = argparse.ArgumentParser(
        description="NebulaX PS1 OR-Tools railway access solver"
    )
    parser.add_argument(
        "--data-dir",
        required=True,
        help="Folder containing 01_LINES.csv ... 08_ACTIVITY_DETAILS.csv",
    )
    parser.add_argument(
        "--scenario",
        choices=["A", "B", "C", "a", "b", "c"],
        default="A",
    )
    parser.add_argument(
        "--output-dir",
        default="submission",
        help="Folder to write the three submission CSVs",
    )
    parser.add_argument("--time-limit", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    data = load_instance(args.data_dir)
    result = solve_schedule(
        data,
        args.scenario,
        time_limit_seconds=args.time_limit,
        num_workers=args.workers,
    )
    print(f"status        : {result['status']}")
    print(f"solver_status : {result['solver_status']}")
    print(f"scenario      : {result['scenario']}")
    if result["status"] != "success":
        print(result.get("message", "Solver failed"))
        raise SystemExit(2)
    files = write_submission(result, args.output_dir)
    print(f"objective     : {result['objective_value']}")
    print(f"metrics       : {result['metrics']}")
    print("predecessors  :")
    for check in result["predecessor_checks"]:
        print(
            "  "
            f"{check['predecessor']} ends w{check['predecessor_last_week']} -> "
            f"{check['successor']} starts w{check['successor_first_week']}"
        )
    print("files         :")
    for name, path in files.items():
        print(f"  {name}: {path}")
if __name__ == "__main__":
    _main()
