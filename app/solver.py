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
    raise ImportError("OR-Tools is required. Install it with: pip install ortools") from exc

TABLE_ALIASES: Dict[str, Tuple[str, ...]] = {
    "lines": ("lines", "01_LINES.csv", "01_LINES", "LINES"),
    "stations": ("stations", "02_STATIONS.csv", "02_STATIONS", "STATIONS"),
    "sectors": ("sectors", "03_SECTORS.csv", "03_SECTORS", "SECTORS"),
    "location_supply": ("location_supply", "04_LOCATION_SUPPLY.csv", "04_LOCATION_SUPPLY", "LOCATION_SUPPLY"),
    "buffer_location": ("buffer_location", "05_BUFFER_LOCATION.csv", "05_BUFFER_LOCATION", "BUFFER_LOCATION"),
    "parameters": ("parameters", "06_PARAMETERS.csv", "06_PARAMETERS", "PARAMETERS"),
    "project_details": ("project_details", "07_PROJECT_DETAILS.csv", "07_PROJECT_DETAILS", "PROJECT_DETAILS"),
    "activity_details": ("activity_details", "08_ACTIVITY_DETAILS.csv", "08_ACTIVITY_DETAILS", "ACTIVITY_DETAILS"),
}
REQUIRED_TABLES = ("sectors", "location_supply", "buffer_location", "parameters", "project_details", "activity_details")
PRIORITY_BASE_WEIGHT = {1: 100, 2: 10, 3: 1}
ACTIVITY_PRIORITY_TENTHS = {1: 3, 2: 2, 3: 0}
PHYSICAL_NIGHTS_PER_WEEK = 7
SOLVER_VERSION = "buffer-coshare-v5"

def _clean(value: Any) -> str:
    return "" if value is None else str(value).strip()

def _as_int(value: Any, field: str) -> int:
    text = _clean(value)
    if not text:
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
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [dict(row) for row in value]
    if hasattr(value, "to_dict"):
        try:
            return [dict(row) for row in value.to_dict("records")]
        except TypeError:
            pass
    if isinstance(value, Mapping):
        return [dict(value)]
    raise TypeError(f"Unsupported table type: {type(value).__name__}")

def _find_table(data: Mapping[str, Any], canonical: str) -> List[Dict[str, Any]]:
    for alias in TABLE_ALIASES[canonical]:
        if alias in data:
            return _records(data[alias])
    lower = {str(k).lower(): k for k in data}
    for alias in TABLE_ALIASES[canonical]:
        key = lower.get(alias.lower())
        if key is not None:
            return _records(data[key])
    return []

def _normalise_instance(data: Mapping[str, Any]) -> Dict[str, List[Dict[str, Any]]]:
    tables = {name: _find_table(data, name) for name in TABLE_ALIASES}
    missing = [name for name in REQUIRED_TABLES if not tables[name]]
    if missing:
        raise ValueError("Missing required PS1 tables: " + ", ".join(missing))
    return tables

def _parameters_dict(rows: Sequence[Mapping[str, Any]]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for row in rows:
        key = _clean(row.get("key"))
        if key:
            out[key] = _clean(row.get("value"))
    return out

def _week_of(date_value: dt.date, horizon_start: dt.date) -> int:
    return ((date_value - horizon_start).days // 7) + 1

def _week_end_date(week: int, horizon_start: dt.date) -> dt.date:
    return horizon_start + dt.timedelta(days=week * 7 - 1)

def _date_day_index(date_value: dt.date, horizon_start: dt.date) -> int:
    return (date_value - horizon_start).days

def _read_csv(path: Path) -> List[Dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))

def load_instance(folder: str | Path) -> Dict[str, List[Dict[str, str]]]:
    folder = Path(folder)
    if not folder.exists():
        raise FileNotFoundError(folder)
    result: Dict[str, List[Dict[str, str]]] = {}
    for canonical, aliases in TABLE_ALIASES.items():
        csv_name = next((x for x in aliases if x.endswith(".csv")), None)
        if csv_name and (folder / csv_name).exists():
            result[canonical] = _read_csv(folder / csv_name)
    missing = [name for name in REQUIRED_TABLES if name not in result]
    if missing:
        raise ValueError(f"{folder} is missing required files for: {', '.join(missing)}")
    return result

def _write_csv(path: Path, fieldnames: Sequence[str], rows: Iterable[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fieldnames})

def write_submission(result: Mapping[str, Any], output_dir: str | Path) -> Dict[str, str]:
    if result.get("status") != "success":
        raise ValueError("Cannot write submission because the solver did not succeed")
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    access = output_dir / "SCHEDULE_ACCESS.csv"
    occupancy = output_dir / "SCHEDULE_OCCUPANCY.csv"
    results = output_dir / "RESULTS.csv"
    _write_csv(access, ["activity_id", "access_seq", "week", "eclo", "access_night"], result["schedule_access"])
    _write_csv(occupancy, ["activity_id", "week", "location_id", "co_share_group"], result["schedule_occupancy"])
    _write_csv(results, ["scenario", "contract_number", "simulated_completion_date", "overrun_days"], result["results"])
    return {"SCHEDULE_ACCESS.csv": str(access), "SCHEDULE_OCCUPANCY.csv": str(occupancy), "RESULTS.csv": str(results)}

def _parse_track_location(location_id: str) -> Tuple[str, str, str, str]:
    parts = _clean(location_id).split(":")
    if len(parts) != 4:
        raise ValueError(f"Unexpected location_id format: {location_id!r}")
    kind, line, middle, bound = parts
    if kind not in {"SEC", "PLAT"} or bound not in {"EB", "WB"}:
        raise ValueError(f"Invalid location_id: {location_id!r}")
    return kind, line, middle, bound

def _sector_base_id(location_id: str) -> str:
    kind, line, middle, _ = _parse_track_location(location_id)
    if kind != "SEC":
        raise ValueError(f"Activity start/end must be sector locations: {location_id}")
    return f"SEC:{line}:{middle}"

def _swap_bound(location_id: str) -> str:
    kind, line, middle, bound = _parse_track_location(location_id)
    return f"{kind}:{line}:{middle}:{'WB' if bound == 'EB' else 'EB'}"

def _line_of_location(location_id: str) -> str:
    return _parse_track_location(location_id)[1]

def _build_sector_index(sectors: Sequence[Mapping[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
    by_id: Dict[str, Dict[str, Any]] = {}
    by_line: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for raw in sectors:
        row = dict(raw)
        sector_id, line = _clean(row.get("sector_id")), _clean(row.get("line_code"))
        if not sector_id or not line:
            raise ValueError("SECTORS row missing sector_id or line_code")
        row["seq"] = _as_int(row.get("seq"), "SECTORS.seq")
        by_id[sector_id] = row
        by_line[line].append(row)
    for line in by_line:
        by_line[line].sort(key=lambda r: int(r["seq"]))
    return by_id, dict(by_line)

def _locations_for_sector_rows(sector_rows: Sequence[Mapping[str, Any]], line: str, bound: str) -> Set[str]:
    locations, stations = set(), set()
    for row in sector_rows:
        locations.add(f"{_clean(row['sector_id'])}:{bound}")
        stations.add(_clean(row["from_station_id"]))
        stations.add(_clean(row["to_station_id"]))
    for station in stations:
        locations.add(f"PLAT:{line}:{station}:{bound}")
    return locations

def _normalise_nature_label(value: Any) -> str:
    return " ".join(_clean(value).lower().split()).replace("–", "-").replace("—", "-")

def _resolve_nature_buffer(raw_nature: Any, buffer_by_nature: Mapping[str, int]) -> Tuple[str, int]:
    nature = _normalise_nature_label(raw_nature)
    if not nature:
        raise ValueError("Missing nature_of_activity")
    if nature in buffer_by_nature:
        return nature, int(buffer_by_nature[nature])
    for key in sorted(buffer_by_nature, key=len, reverse=True):
        norm = _normalise_nature_label(key)
        if not norm or (norm == "live" and "non-live" in nature):
            continue
        if norm in nature or nature in norm:
            return norm, int(buffer_by_nature[key])
    raise ValueError(f"Unrecognised nature_of_activity {raw_nature!r}; known buffer types are {sorted(buffer_by_nature)}")

def _get_interchange_hubs(stations: Sequence[Mapping[str, Any]], sectors: Sequence[Mapping[str, Any]]) -> Set[str]:
    hubs, interchange = set(), set()
    truthy = {"1", "true", "yes", "y"}
    for station in stations:
        if _clean(station.get("is_interchange")).lower() in truthy:
            station_id = _clean(station.get("station_id"))
            if station_id:
                interchange.add(station_id)
                hubs.add(station_id)
    for sector in sectors:
        shared = _clean(sector.get("is_shared")).lower() in truthy
        start, end = _clean(sector.get("from_station_id")), _clean(sector.get("to_station_id"))
        if not (shared or (start in interchange and end in interchange)):
            continue
        parts = _clean(sector.get("sector_id")).split(":")
        if len(parts) >= 3 and parts[0] == "SEC" and parts[2]:
            hubs.add(parts[2])
    return hubs or {"H01", "H02", "H01_H02"}

def _activity_route_and_closure(
    activity: Mapping[str, Any],
    project: Mapping[str, Any],
    sector_by_id: Mapping[str, Mapping[str, Any]],
    sectors_by_line: Mapping[str, Sequence[Mapping[str, Any]]],
    buffer_by_nature: Mapping[str, int],
    all_location_ids: Set[str],
    interchange_hubs: Set[str],
) -> Tuple[Set[str], Set[str], Set[str]]:
    start_location, end_location = _clean(activity.get("start_location_id")), _clean(activity.get("end_location_id"))
    _, start_line, _, start_bound = _parse_track_location(start_location)
    _, end_line, _, end_bound = _parse_track_location(end_location)
    if start_line != end_line or start_bound != end_bound:
        raise ValueError(f"Activity {activity.get('activity_id')} crosses line/bound in start/end")
    start_base, end_base = _sector_base_id(start_location), _sector_base_id(end_location)
    if start_base not in sector_by_id or end_base not in sector_by_id:
        raise ValueError(f"Activity {activity.get('activity_id')} references a sector missing from SECTORS")
    low, high = sorted((int(sector_by_id[start_base]["seq"]), int(sector_by_id[end_base]["seq"])))
    line_rows = list(sectors_by_line[start_line])
    route_rows = [r for r in line_rows if low <= int(r["seq"]) <= high]
    route = _locations_for_sector_rows(route_rows, start_line, start_bound)
    nature, buffer_size = _resolve_nature_buffer(project.get("nature_of_activity"), buffer_by_nature)
    closure_rows = [r for r in line_rows if low - buffer_size <= int(r["seq"]) <= high + buffer_size]
    closure = _locations_for_sector_rows(closure_rows, start_line, start_bound)
    if nature == "live":
        closure |= {_swap_bound(loc) for loc in list(closure)}
        if any(_parse_track_location(loc)[2] in interchange_hubs for loc in closure):
            for location_id in all_location_ids:
                _, line, middle, _ = _parse_track_location(location_id)
                if line != start_line and middle in interchange_hubs:
                    closure.add(location_id)
    closure &= all_location_ids
    route &= all_location_ids
    if not route:
        raise ValueError(f"Activity {activity.get('activity_id')} expands to no valid occupancy locations")
    return route, closure, {_line_of_location(loc) for loc in closure}

def _co_share_pair_allowed(
    access_type_a: str,
    access_type_b: str,
    route_a: Set[str],
    route_b: Set[str],
) -> bool:
    pair = {access_type_a, access_type_b}
    legal = (access_type_a == "C" and access_type_b == "C") or pair == {"PC", "C"}
    return legal and bool(route_a & route_b)

def _validate_predecessors(activities: Mapping[str, Mapping[str, Any]]) -> None:
    graph: Dict[str, Optional[str]] = {}
    for activity_id, row in activities.items():
        predecessor = _clean(row.get("predecessor_activity_id")) or None
        if predecessor and predecessor not in activities:
            raise ValueError(f"Activity {activity_id} references unknown predecessor {predecessor}")
        if predecessor == activity_id:
            raise ValueError(f"Activity {activity_id} cannot depend on itself")
        graph[activity_id] = predecessor
    state = {activity_id: 0 for activity_id in activities}
    def visit(node: str, stack: List[str]) -> None:
        if state[node] == 2:
            return
        if state[node] == 1:
            i = stack.index(node) if node in stack else 0
            raise ValueError("Predecessor cycle detected: " + " -> ".join(stack[i:] + [node]))
        state[node] = 1
        stack.append(node)
        if graph[node]:
            visit(graph[node], stack)
        stack.pop()
        state[node] = 2
    for activity_id in activities:
        if state[activity_id] == 0:
            visit(activity_id, [])

def solve_schedule(
    data: Mapping[str, Any],
    scenario: str = "A",
    *,
    time_limit_seconds: float = 30.0,
    num_workers: Optional[int] = 1,
    random_seed: int = 42,
) -> Dict[str, Any]:
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
            raise ValueError(f"Activity {activity_id} references unknown contract {contract!r}")
        activities[activity_id] = dict(row)

    _validate_predecessors(activities)
    sector_by_id, sectors_by_line = _build_sector_index(tables["sectors"])

    supply: Dict[str, int] = {}
    for row in tables["location_supply"]:
        supply[_clean(row.get("location_id"))] = _as_int(row.get("supply_capacity"), "supply_capacity")
    all_location_ids = set(supply)

    buffer_by_nature: Dict[str, int] = {}
    for row in tables["buffer_location"]:
        nature = _normalise_nature_label(row.get("nature_of_works"))
        if not nature:
            raise ValueError("BUFFER_LOCATION row missing nature_of_works")
        buffer_by_nature[nature] = _as_int(row.get("up_to_buffer_sectors"), "up_to_buffer_sectors")

    interchange_hubs = _get_interchange_hubs(tables.get("stations", []), tables["sectors"])
    activity_ids = list(activities)
    route: Dict[str, Set[str]] = {}
    closure: Dict[str, Set[str]] = {}
    affected_lines: Dict[str, Set[str]] = {}
    earliest_week: Dict[str, int] = {}
    activity_contract: Dict[str, str] = {}
    activity_access_type: Dict[str, str] = {}

    for activity_id, activity in activities.items():
        contract = _clean(activity.get("contract_number"))
        project = projects[contract]
        activity_contract[activity_id] = contract
        activity_access_type[activity_id] = _clean(project.get("access_type"))
        earliest_week[activity_id] = max(1, _week_of(_parse_date(activity.get("planned_start_date"), f"{activity_id}.planned_start_date"), horizon_start))
        route[activity_id], closure[activity_id], affected_lines[activity_id] = _activity_route_and_closure(
            activity, project, sector_by_id, sectors_by_line, buffer_by_nature, all_location_ids, interchange_hubs
        )

    activities_at_location: Dict[str, List[str]] = defaultdict(list)
    for activity_id in activity_ids:
        for location_id in route[activity_id]:
            activities_at_location[location_id].append(activity_id)

    model = cp_model.CpModel()
    scheduled: Dict[Tuple[str, int], cp_model.IntVar] = {}
    normal: Dict[Tuple[str, int], cp_model.IntVar] = {}
    eclo: Dict[Tuple[str, int], cp_model.IntVar] = {}
    local_access: Dict[Tuple[str, int, int], cp_model.IntVar] = {}

    for activity_id in activity_ids:
        contract = activity_contract[activity_id]
        cap = _as_int(projects[contract].get("number_of_maximum_access_per_week"), f"{contract}.number_of_maximum_access_per_week")
        for week in weeks:
            s = model.NewBoolVar(f"scheduled__{activity_id}__w{week}")
            n = model.NewBoolVar(f"normal__{activity_id}__w{week}")
            e = model.NewBoolVar(f"eclo__{activity_id}__w{week}")
            scheduled[activity_id, week], normal[activity_id, week], eclo[activity_id, week] = s, n, e
            model.Add(n + e == s)
            if week < earliest_week[activity_id]:
                model.Add(s == 0)
            if scenario == "A":
                model.Add(e == 0)
            avars = []
            for access_night in range(1, cap + 1):
                var = model.NewBoolVar(f"accessnight__{activity_id}__w{week}__n{access_night}")
                local_access[activity_id, week, access_night] = var
                avars.append(var)
            model.Add(sum(avars) == s)

    for activity_id, activity in activities.items():
        required = 2 * _as_int(activity.get("total_accesses"), f"{activity_id}.total_accesses")
        delivered = sum(2 * normal[activity_id, week] + 3 * eclo[activity_id, week] for week in weeks)
        model.Add(delivered >= required)
        model.Add(delivered <= required + 1)

    start_week: Dict[str, cp_model.IntVar] = {}
    end_week: Dict[str, cp_model.IntVar] = {}
    sentinel = horizon_weeks + 1
    for activity_id in activity_ids:
        start = model.NewIntVar(1, horizon_weeks, f"startweek__{activity_id}")
        end = model.NewIntVar(1, horizon_weeks, f"endweek__{activity_id}")
        start_week[activity_id], end_week[activity_id] = start, end
        start_candidates, end_candidates = [], []
        for week in weeks:
            sc = model.NewIntVar(1, sentinel, f"startcand__{activity_id}__w{week}")
            ec = model.NewIntVar(0, horizon_weeks, f"endcand__{activity_id}__w{week}")
            model.Add(sc == week * scheduled[activity_id, week] + sentinel * (1 - scheduled[activity_id, week]))
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
        activities_by_contract_type[(activity_contract[activity_id], _clean(activity.get("activity_type")))].append(activity_id)

    for (contract, _), ids in activities_by_contract_type.items():
        project = projects[contract]
        cap = _as_int(project.get("number_of_maximum_access_per_week"), f"{contract}.number_of_maximum_access_per_week")
        workfronts = _as_int(project.get("number_of_workfronts"), f"{contract}.number_of_workfronts")
        for week in weeks:
            for access_night in range(1, cap + 1):
                model.Add(sum(local_access[a, week, access_night] for a in ids) <= workfronts)

    max_slots = PHYSICAL_NIGHTS_PER_WEEK
    occupancy_slot: Dict[Tuple[str, int, str, int], cp_model.IntVar] = {}
    group_used: Dict[Tuple[str, int, int], cp_model.IntVar] = {}
    excess_by_location_week: Dict[Tuple[str, int], cp_model.IntVar] = {}

    for location_id, nominal_supply in supply.items():
        ids = activities_at_location.get(location_id, [])
        if not ids:
            continue
        for week in weeks:
            used_vars = []
            for slot in range(1, max_slots + 1):
                used = model.NewBoolVar(f"groupused__{location_id}__w{week}__g{slot}")
                group_used[location_id, week, slot] = used
                used_vars.append(used)
                pm, pc, coworker, members = [], [], [], []
                for a in ids:
                    var = model.NewBoolVar(f"occslot__{a}__w{week}__{location_id}__g{slot}")
                    occupancy_slot[a, week, location_id, slot] = var
                    members.append(var)
                    model.Add(var <= scheduled[a, week])
                    if activity_access_type[a] == "PM":
                        pm.append(var)
                    elif activity_access_type[a] == "PC":
                        pc.append(var)
                    elif activity_access_type[a] == "C":
                        coworker.append(var)
                for var in members:
                    model.Add(var <= used)
                model.Add(used <= sum(members))
                pm_sum = sum(pm) if pm else 0
                pc_sum = sum(pc) if pc else 0
                c_sum = sum(coworker) if coworker else 0
                model.Add(pm_sum <= 1)
                model.Add(pc_sum <= 1)
                model.Add(pm_sum + pc_sum <= 1)
                model.Add(c_sum + pc_sum + 4 * pm_sum <= 4)

            for a in ids:
                model.Add(sum(occupancy_slot[a, week, location_id, slot] for slot in range(1, max_slots + 1)) == scheduled[a, week])

            total_used = sum(used_vars)
            max_excess = max(0, max_slots - nominal_supply)
            excess = model.NewIntVar(0, max_excess, f"excess__{location_id}__w{week}")
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
        for b in activity_ids[i + 1:]:
            if not (closure[a] & closure[b]):
                continue
            shared = route[a] & route[b]
            if _co_share_pair_allowed(
                activity_access_type[a],
                activity_access_type[b],
                route[a],
                route[b],
            ):
                pair_coshare_exemptions += 1
                for week in weeks:
                    both = model.NewBoolVar(f"coshare__{a}__{b}__w{week}")
                    model.Add(both <= scheduled[a, week])
                    model.Add(both <= scheduled[b, week])
                    model.Add(both >= scheduled[a, week] + scheduled[b, week] - 1)
                    for location_id in shared:
                        for slot in range(1, max_slots + 1):
                            model.Add(
                                occupancy_slot[a, week, location_id, slot]
                                == occupancy_slot[b, week, location_id, slot]
                            ).OnlyEnforceIf(both)
                continue
            pair_conflicts += 1
            for week in weeks:
                model.Add(scheduled[a, week] + scheduled[b, week] <= 1)

    eclo_window_start: Dict[str, cp_model.IntVar] = {}
    if scenario == "C":
        all_lines = sorted({line for lines in affected_lines.values() for line in lines})
        for line in all_lines:
            eclo_window_start[line] = model.NewIntVar(1, max(1, horizon_weeks), f"eclo_window_start__{line}")
        for activity_id in activity_ids:
            for week in weeks:
                for line in affected_lines[activity_id]:
                    window = eclo_window_start[line]
                    model.Add(week >= window).OnlyEnforceIf(eclo[activity_id, week])
                    model.Add(week <= window + 1).OnlyEnforceIf(eclo[activity_id, week])

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
        planned_day = _date_day_index(_parse_date(projects[contract].get("planned_completion_date"), f"{contract}.planned_completion_date"), horizon_start)
        overrun = model.NewIntVar(0, max(0, horizon_end_day - planned_day), f"contract_overrun_days__{contract}")
        completion_day = 7 * end_var - 1
        model.Add(overrun >= completion_day - planned_day)
        model.Add(overrun >= 0)
        contract_overrun_days[contract] = overrun
        if scenario == "B":
            model.Add(completion_day <= planned_day)
            model.Add(overrun == 0)

    activity_overrun_days: Dict[str, cp_model.IntVar] = {}
    for activity_id in activity_ids:
        contract = activity_contract[activity_id]
        planned_day = _date_day_index(_parse_date(projects[contract].get("planned_completion_date"), f"{contract}.planned_completion_date"), horizon_start)
        overrun = model.NewIntVar(0, max(0, horizon_end_day - planned_day), f"activity_overrun_days__{activity_id}")
        model.Add(overrun >= 7 * end_week[activity_id] - 1 - planned_day)
        model.Add(overrun >= 0)
        activity_overrun_days[activity_id] = overrun

    objective_terms = []
    if scenario in {"A", "C"}:
        for activity_id, activity in activities.items():
            contract = activity_contract[activity_id]
            contract_priority = _as_int(projects[contract].get("contract_priority"), f"{contract}.contract_priority")
            activity_priority = _as_int(activity.get("activity_priority"), f"{activity_id}.activity_priority")
            coefficient = PRIORITY_BASE_WEIGHT.get(contract_priority, 1) * (10 + ACTIVITY_PRIORITY_TENTHS.get(activity_priority, 0))
            objective_terms.append(coefficient * activity_overrun_days[activity_id])
    if scenario in {"B", "C"}:
        objective_terms.extend(70 * var for var in excess_by_location_week.values())
        objective_terms.extend(50 * eclo[a, w] for a in activity_ids for w in weeks)
    objective_terms.extend(scheduled[a, w] for a in activity_ids for w in weeks)
    model.Minimize(sum(objective_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit_seconds)
    solver.parameters.num_search_workers = int(num_workers if num_workers is not None else 1)
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
            "message": "No feasible solution was found within the model/time limit. Check the hard constraints, horizon, and hidden-instance data.",
        }

    schedule_access: List[Dict[str, Any]] = []
    for activity_id in activity_ids:
        contract = activity_contract[activity_id]
        cap = _as_int(projects[contract].get("number_of_maximum_access_per_week"), f"{contract}.number_of_maximum_access_per_week")
        seq = 0
        for week in weeks:
            if solver.Value(scheduled[activity_id, week]) != 1:
                continue
            seq += 1
            access_night = next(n for n in range(1, cap + 1) if solver.Value(local_access[activity_id, week, n]) == 1)
            schedule_access.append({
                "activity_id": activity_id,
                "access_seq": seq,
                "week": week,
                "eclo": int(solver.Value(eclo[activity_id, week])),
                "access_night": access_night,
            })

    schedule_occupancy: List[Dict[str, Any]] = []
    for access in schedule_access:
        activity_id, week = str(access["activity_id"]), int(access["week"])
        for location_id in sorted(route[activity_id]):
            slot = next(
                g for g in range(1, max_slots + 1)
                if solver.Value(occupancy_slot[activity_id, week, location_id, g]) == 1
            )
            schedule_occupancy.append({
                "activity_id": activity_id,
                "week": week,
                "location_id": location_id,
                "co_share_group": f"b{slot}",
            })

    results_rows: List[Dict[str, Any]] = []
    for contract in sorted(projects):
        if contract not in contract_end_week:
            continue
        completion_week = int(solver.Value(contract_end_week[contract]))
        completion_date = _week_end_date(completion_week, horizon_start)
        planned_date = _parse_date(projects[contract].get("planned_completion_date"), f"{contract}.planned_completion_date")
        results_rows.append({
            "scenario": scenario,
            "contract_number": contract,
            "simulated_completion_date": completion_date.isoformat(),
            "overrun_days": max(0, (completion_date - planned_date).days),
        })

    predecessor_checks = []
    for activity_id, activity in activities.items():
        predecessor = _clean(activity.get("predecessor_activity_id"))
        if predecessor:
            predecessor_checks.append({
                "predecessor": predecessor,
                "predecessor_last_week": int(solver.Value(end_week[predecessor])),
                "successor": activity_id,
                "successor_first_week": int(solver.Value(start_week[activity_id])),
            })

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
            "eclo_nights_total": sum(int(row["eclo"]) for row in schedule_access),
            "excess_access_nights_total": sum(int(solver.Value(var)) for var in excess_by_location_week.values()),
            "pair_conflicts": pair_conflicts,
            "pair_coshare_exemptions": pair_coshare_exemptions,
            "wall_time_seconds": float(solver.WallTime()),
        },
        "predecessor_checks": predecessor_checks,
        "debug": {
            "horizon_start": horizon_start.isoformat(),
            "horizon_weeks": horizon_weeks,
            "max_co_share_groups_per_location_week": max_slots,
        },
    }

def _main() -> None:
    parser = argparse.ArgumentParser(description="NebulaX PS1 OR-Tools railway access solver")
    parser.add_argument("--data-dir", required=True, help="Folder containing 01_LINES.csv ... 08_ACTIVITY_DETAILS.csv")
    parser.add_argument("--scenario", choices=["A", "B", "C", "a", "b", "c"], default="A")
    parser.add_argument("--output-dir", default="submission", help="Folder to write the three submission CSVs")
    parser.add_argument("--time-limit", type=float, default=30.0)
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    data = load_instance(args.data_dir)
    result = solve_schedule(data, args.scenario, time_limit_seconds=args.time_limit, num_workers=args.workers)
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
        print(f"  {check['predecessor']} ends w{check['predecessor_last_week']} -> {check['successor']} starts w{check['successor_first_week']}")
    print("files         :")
    for name, path in files.items():
        print(f"  {name}: {path}")

if __name__ == "__main__":
    _main()
