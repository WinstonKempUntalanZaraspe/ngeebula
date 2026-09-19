from __future__ import annotations

import argparse
import csv
import datetime as dt
from collections import defaultdict
from itertools import combinations
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
CONTRACT_WEIGHT10 = {1: 1000, 2: 100, 3: 10}
ACTIVITY_MULT10 = {1: 13, 2: 12, 3: 10}
SOLVER_VERSION = "hard-rules-v12"


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


def _build_sector_index(sectors: Sequence[Mapping[str, Any]]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
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


def _locations_for_sector_rows(sector_rows: Sequence[Mapping[str, Any]], line: str, bound: str) -> Set[str]:
    locations: Set[str] = set()
    stations: Set[str] = set()
    for row in sector_rows:
        locations.add(f"{_clean(row['sector_id'])}:{bound}")
        stations.add(_clean(row.get("from_station_id")))
        stations.add(_clean(row.get("to_station_id")))
    for station in stations:
        if station:
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


def _get_interchange_stations(stations: Sequence[Mapping[str, Any]]) -> Set[str]:
    truthy = {"1", "true", "yes", "y", "t"}
    flagged = {
        _clean(row.get("station_id"))
        for row in stations
        if _clean(row.get("is_interchange")).lower() in truthy and _clean(row.get("station_id"))
    }
    if flagged:
        return flagged
    lines_by_station: Dict[str, Set[str]] = defaultdict(set)
    for row in stations:
        sid = _clean(row.get("station_id"))
        line = _clean(row.get("line_code"))
        if sid and line:
            lines_by_station[sid].add(line)
    return {sid for sid, lines in lines_by_station.items() if len(lines) > 1}


def _activity_route_and_closure(
    activity: Mapping[str, Any],
    project: Mapping[str, Any],
    sector_by_id: Mapping[str, Mapping[str, Any]],
    sectors_by_line: Mapping[str, Sequence[Mapping[str, Any]]],
    buffer_by_nature: Mapping[str, int],
    all_location_ids: Set[str],
    interchange_stations: Set[str],
) -> Tuple[Set[str], Set[str], Set[str]]:
    start_location = _clean(activity.get("start_location_id"))
    end_location = _clean(activity.get("end_location_id"))
    _, start_line, _, start_bound = _parse_track_location(start_location)
    _, end_line, _, end_bound = _parse_track_location(end_location)
    if start_line != end_line or start_bound != end_bound:
        raise ValueError(f"Activity {activity.get('activity_id')} crosses line/bound in start/end")
    start_base = _sector_base_id(start_location)
    end_base = _sector_base_id(end_location)
    if start_base not in sector_by_id or end_base not in sector_by_id:
        raise ValueError(f"Activity {activity.get('activity_id')} references a sector missing from SECTORS")

    line_rows = list(sectors_by_line[start_line])
    ids = [_clean(row.get("sector_id")) for row in line_rows]
    try:
        i, j = sorted((ids.index(start_base), ids.index(end_base)))
    except ValueError as exc:
        raise ValueError(f"Activity {activity.get('activity_id')} route endpoints are not on line {start_line}") from exc

    route_rows = line_rows[i:j + 1]
    route = _locations_for_sector_rows(route_rows, start_line, start_bound)
    nature, radius = _resolve_nature_buffer(project.get("nature_of_activity"), buffer_by_nature)
    lo = max(0, i - radius)
    hi = min(len(line_rows) - 1, j + radius)
    protected_rows = line_rows[lo:hi + 1]
    full_span = _locations_for_sector_rows(protected_rows, start_line, start_bound)

    if nature == "live":
        closure = set(full_span)
    else:
        closure = set(route)
        closure |= {loc for loc in full_span if loc.startswith("SEC:")}

    if nature == "live":
        closure |= {_swap_bound(loc) for loc in list(closure)}
        touched_hubs = {
            _parse_track_location(loc)[2]
            for loc in closure
            if loc.startswith("PLAT:") and _parse_track_location(loc)[2] in interchange_stations
        }
        if touched_hubs:
            for other_line, rows_raw in sectors_by_line.items():
                if other_line == start_line:
                    continue
                rows = list(rows_raw)
                for idx, row in enumerate(rows):
                    ends = {_clean(row.get("from_station_id")), _clean(row.get("to_station_id"))}
                    if not (ends <= interchange_stations and ends & touched_hubs):
                        continue
                    cross_lo = max(0, idx - radius)
                    cross_hi = min(len(rows) - 1, idx + radius)
                    buffered = rows[cross_lo:cross_hi + 1]
                    closure |= _locations_for_sector_rows(buffered, other_line, "EB")
                    closure |= _locations_for_sector_rows(buffered, other_line, "WB")

    route &= all_location_ids
    closure &= all_location_ids
    if not route:
        raise ValueError(f"Activity {activity.get('activity_id')} expands to no valid occupancy locations")
    affected_lines = {_parse_track_location(loc)[1] for loc in closure}
    return route, closure, affected_lines


def _co_share_pair_allowed(access_type_a: str, access_type_b: str, route_a: Set[str], route_b: Set[str]) -> bool:
    return bool(route_a & route_b) and "PM" not in {access_type_a, access_type_b} and not (access_type_a == "PC" and access_type_b == "PC")


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


def _possession_components(
    groups: Mapping[Tuple[str, int, str], Set[str]],
    by_week: Mapping[int, Set[str]],
) -> Dict[Tuple[int, str], Set[str]]:
    parent: Dict[Tuple[int, str], str] = {(week, aid): aid for week, aids in by_week.items() for aid in aids}

    def root(week: int, aid: str) -> str:
        while parent[week, aid] != aid:
            parent[week, aid] = parent[week, parent[week, aid]]
            aid = parent[week, aid]
        return aid

    for (_, week, _), aids in groups.items():
        present = sorted(a for a in aids if (week, a) in parent)
        if not present:
            continue
        first = root(week, present[0])
        for aid in present[1:]:
            parent[week, root(week, aid)] = first

    components: Dict[Tuple[int, str], Set[str]] = defaultdict(set)
    for week, aid in parent:
        components[week, root(week, aid)].add(aid)
    return dict(components)


def solve_schedule(
    data: Mapping[str, Any],
    scenario: str = "A",
    *,
    time_limit_seconds: float = 60.0,
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
        loc = _clean(row.get("location_id"))
        if loc:
            supply[loc] = _as_int(row.get("supply_capacity"), "supply_capacity")
    all_location_ids = set(supply)

    buffer_by_nature: Dict[str, int] = {}
    for row in tables["buffer_location"]:
        nature = _normalise_nature_label(row.get("nature_of_works"))
        if nature:
            buffer_by_nature[nature] = _as_int(row.get("up_to_buffer_sectors"), "up_to_buffer_sectors")

    interchange_stations = _get_interchange_stations(tables.get("stations", []))
    activity_ids = sorted(activities)
    route: Dict[str, Set[str]] = {}
    closure: Dict[str, Set[str]] = {}
    affected_lines: Dict[str, Set[str]] = {}
    earliest_week: Dict[str, int] = {}
    activity_contract: Dict[str, str] = {}
    activity_access_type: Dict[str, str] = {}
    activity_type: Dict[str, str] = {}

    for aid in activity_ids:
        activity = activities[aid]
        contract = _clean(activity.get("contract_number"))
        project = projects[contract]
        activity_contract[aid] = contract
        activity_access_type[aid] = _clean(project.get("access_type"))
        activity_type[aid] = _clean(activity.get("activity_type"))
        earliest_week[aid] = max(1, _week_of(_parse_date(activity.get("planned_start_date"), f"{aid}.planned_start_date"), horizon_start))
        route[aid], closure[aid], affected_lines[aid] = _activity_route_and_closure(
            activity,
            project,
            sector_by_id,
            sectors_by_line,
            buffer_by_nature,
            all_location_ids,
            interchange_stations,
        )

    max_supply = max(supply.values()) if supply else 1
    slot_count = max(7, max_supply)
    slots = list(range(slot_count))

    model = cp_model.CpModel()
    scheduled: Dict[Tuple[str, int], cp_model.IntVar] = {}
    eclo: Dict[Tuple[str, int], cp_model.IntVar] = {}
    physical: Dict[Tuple[str, int, int], cp_model.IntVar] = {}
    finish: Dict[str, cp_model.IntVar] = {}

    for aid in activity_ids:
        project = projects[activity_contract[aid]]
        planned_completion = _parse_date(project.get("planned_completion_date"), f"{activity_contract[aid]}.planned_completion_date")
        finish[aid] = model.NewIntVar(1, horizon_weeks, f"finish__{aid}")
        end_candidates = []
        for week in weeks:
            s = model.NewBoolVar(f"scheduled__{aid}__w{week}")
            e = model.NewBoolVar(f"eclo__{aid}__w{week}")
            scheduled[aid, week] = s
            eclo[aid, week] = e
            model.Add(e <= s)
            if week < earliest_week[aid]:
                model.Add(s == 0)
            if scenario == "A":
                model.Add(e == 0)
            if scenario == "B" and _week_end_date(week, horizon_start) > planned_completion:
                model.Add(s == 0)
            pvars = []
            for slot in slots:
                z = model.NewBoolVar(f"slot__{aid}__w{week}__s{slot}")
                physical[aid, week, slot] = z
                pvars.append(z)
            model.Add(sum(pvars) == s)
            end_candidates.append(week * s)
        model.AddMaxEquality(finish[aid], end_candidates)
        required = 2 * _as_int(activities[aid].get("total_accesses"), f"{aid}.total_accesses")
        delivered = sum(2 * scheduled[aid, week] + eclo[aid, week] for week in weeks)
        model.Add(delivered >= required)
        model.Add(delivered <= required + 1)

    for successor in activity_ids:
        pred = _clean(activities[successor].get("predecessor_activity_id"))
        if not pred:
            continue
        for week in weeks:
            model.Add(finish[pred] < week).OnlyEnforceIf(scheduled[successor, week])

    activities_at_location: Dict[str, List[str]] = defaultdict(list)
    for aid in activity_ids:
        for loc in route[aid]:
            activities_at_location[loc].append(aid)

    excess_by_location_week: Dict[Tuple[str, int], cp_model.IntVar] = {}
    for loc, nominal_supply in supply.items():
        occupants = activities_at_location.get(loc, [])
        if not occupants:
            continue
        for week in weeks:
            used_vars = []
            for slot in slots:
                terms = [physical[aid, week, slot] for aid in occupants]
                busy = model.NewBoolVar(f"used__{loc}__w{week}__s{slot}")
                model.AddMaxEquality(busy, terms)
                used_vars.append(busy)
                model.Add(sum(terms) <= 4)
                pc_terms = [physical[aid, week, slot] for aid in occupants if activity_access_type[aid] == "PC"]
                if pc_terms:
                    model.Add(sum(pc_terms) <= 1)
                for aid in occupants:
                    if activity_access_type[aid] == "PM":
                        model.Add(sum(terms) <= 1).OnlyEnforceIf(physical[aid, week, slot])
            total_used = sum(used_vars)
            max_excess = max(0, slot_count - nominal_supply)
            excess = model.NewIntVar(0, max_excess, f"excess__{loc}__w{week}")
            model.Add(excess >= total_used - nominal_supply)
            model.Add(excess >= 0)
            model.Add(excess <= total_used)
            excess_by_location_week[loc, week] = excess
            if scenario == "A":
                model.Add(total_used <= nominal_supply)
                model.Add(excess == 0)
            elif scenario == "C":
                model.Add(total_used <= nominal_supply + 1)
                model.Add(excess <= 1)

    pair_conflicts = 0
    pair_coshare_exemptions = 0
    for a, b in combinations(activity_ids, 2):
        sharing = _co_share_pair_allowed(activity_access_type[a], activity_access_type[b], route[a], route[b])
        enters_closure = bool((route[a] & closure[b]) or (route[b] & closure[a]))
        if enters_closure:
            if sharing:
                pair_coshare_exemptions += 1
                for week in weeks:
                    for slot in slots:
                        model.Add(physical[a, week, slot] == physical[b, week, slot]).OnlyEnforceIf(
                            [scheduled[a, week], scheduled[b, week]]
                        )
            else:
                pair_conflicts += 1
                for week in weeks:
                    model.Add(scheduled[a, week] + scheduled[b, week] <= 1)
        if not sharing and (closure[a] & closure[b]):
            for week in weeks:
                for slot in slots:
                    model.Add(physical[a, week, slot] + physical[b, week, slot] <= 1)

    activities_by_contract_type: Dict[Tuple[str, str], List[str]] = defaultdict(list)
    for aid in activity_ids:
        activities_by_contract_type[activity_contract[aid], activity_type[aid]].append(aid)

    for (contract, atype), ids in activities_by_contract_type.items():
        cap = _as_int(projects[contract].get("number_of_maximum_access_per_week"), f"{contract}.number_of_maximum_access_per_week")
        workfronts = _as_int(projects[contract].get("number_of_workfronts"), f"{contract}.number_of_workfronts")
        for week in weeks:
            used = []
            for slot in slots:
                terms = [physical[aid, week, slot] for aid in ids]
                busy = model.NewBoolVar(f"contract__{contract}__{atype}__w{week}__s{slot}")
                model.AddMaxEquality(busy, terms)
                model.Add(sum(terms) <= workfronts)
                used.append(busy)
            model.Add(sum(used) <= cap)

    if scenario == "C":
        all_lines = sorted({line for lines in affected_lines.values() for line in lines})
        window_start = {line: model.NewIntVar(1, horizon_weeks, f"eclo_window__{line}") for line in all_lines}
        for aid in activity_ids:
            for week in weeks:
                for line in affected_lines[aid]:
                    model.Add(window_start[line] <= week).OnlyEnforceIf(eclo[aid, week])
                    model.Add(window_start[line] >= week - 1).OnlyEnforceIf(eclo[aid, week])

    activities_by_contract: Dict[str, List[str]] = defaultdict(list)
    for aid in activity_ids:
        activities_by_contract[activity_contract[aid]].append(aid)

    contract_end_week: Dict[str, cp_model.IntVar] = {}
    contract_overrun_days: Dict[str, cp_model.IntVar] = {}
    horizon_end_day = horizon_weeks * 7 - 1
    primary_terms = []

    for contract, ids in activities_by_contract.items():
        end_var = model.NewIntVar(1, horizon_weeks, f"contract_end__{contract}")
        model.AddMaxEquality(end_var, [finish[aid] for aid in ids])
        contract_end_week[contract] = end_var
        planned_day = _date_day_index(_parse_date(projects[contract].get("planned_completion_date"), f"{contract}.planned_completion_date"), horizon_start)
        max_late = max(0, horizon_end_day - planned_day)
        overrun = model.NewIntVar(0, max_late, f"contract_overrun__{contract}")
        model.AddMaxEquality(overrun, [0, 7 * end_var - 1 - planned_day])
        contract_overrun_days[contract] = overrun
        if scenario == "B":
            model.Add(overrun == 0)
        if scenario in {"A", "C"}:
            tier = _as_int(projects[contract].get("contract_priority"), f"{contract}.contract_priority")
            weight10 = 0
            for aid in ids:
                ap = _as_int(activities[aid].get("activity_priority"), f"{aid}.activity_priority")
                weight10 += (CONTRACT_WEIGHT10.get(tier, 10) * ACTIVITY_MULT10.get(ap, 10)) // 10
            primary_terms.append(weight10 * overrun)

    if scenario in {"B", "C"}:
        primary_terms.extend(70 * var for var in excess_by_location_week.values())
        primary_terms.extend(50 * eclo[aid, week] for aid in activity_ids for week in weeks)

    primary = sum(primary_terms) if primary_terms else 0
    secondary = sum(finish.values())
    scale = len(activity_ids) * horizon_weeks + 1
    model.Minimize(primary * scale + secondary)

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
            "message": "No feasible solution was found within the model/time limit.",
        }

    chosen_slot: Dict[Tuple[str, int], int] = {}
    active_by_week: Dict[int, List[str]] = defaultdict(list)
    for aid in activity_ids:
        for week in weeks:
            if solver.Value(scheduled[aid, week]) != 1:
                continue
            slot = next(s for s in slots if solver.Value(physical[aid, week, s]) == 1)
            chosen_slot[aid, week] = slot
            active_by_week[week].append(aid)

    used_contract_slots: Dict[Tuple[str, str, int], List[int]] = {}
    for (contract, atype), ids in activities_by_contract_type.items():
        for week in weeks:
            values = sorted({chosen_slot[aid, week] for aid in ids if (aid, week) in chosen_slot})
            used_contract_slots[contract, atype, week] = values

    schedule_access: List[Dict[str, Any]] = []
    counters: Dict[str, int] = defaultdict(int)
    for week in weeks:
        for aid in sorted(active_by_week.get(week, [])):
            counters[aid] += 1
            slot = chosen_slot[aid, week]
            key = (activity_contract[aid], activity_type[aid], week)
            access_night = used_contract_slots[key].index(slot) + 1
            schedule_access.append({
                "activity_id": aid,
                "access_seq": counters[aid],
                "week": week,
                "eclo": int(solver.Value(eclo[aid, week])),
                "access_night": access_night,
            })

    schedule_occupancy: List[Dict[str, Any]] = []
    for row in schedule_access:
        aid = str(row["activity_id"])
        week = int(row["week"])
        group = f"p{chosen_slot[aid, week] + 1}"
        for loc in sorted(route[aid]):
            schedule_occupancy.append({
                "activity_id": aid,
                "week": week,
                "location_id": loc,
                "co_share_group": group,
            })

    internal_hard_rule_errors: List[str] = []
    groups: Dict[Tuple[str, int, str], Set[str]] = defaultdict(set)
    by_week: Dict[int, Set[str]] = defaultdict(set)
    for row in schedule_occupancy:
        groups[str(row["location_id"]), int(row["week"]), str(row["co_share_group"])].add(str(row["activity_id"]))
        by_week[int(row["week"])].add(str(row["activity_id"]))

    components = _possession_components(groups, by_week)
    for (week, _root), members in components.items():
        combined_closure = set().union(*(closure[aid] for aid in members))
        for aid in sorted(by_week[week] - members):
            overlap = route[aid] & combined_closure
            if overlap:
                internal_hard_rule_errors.append(
                    f"closure:{week}:{aid}:inside:{','.join(sorted(members))}:{','.join(sorted(overlap)[:4])}"
                )

    results_rows: List[Dict[str, Any]] = []
    for contract in sorted(projects):
        ids = activities_by_contract.get(contract, [])
        if not ids:
            completion_date = horizon_start
        else:
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
    for aid in activity_ids:
        pred = _clean(activities[aid].get("predecessor_activity_id"))
        if pred:
            successor_weeks = [week for week in weeks if solver.Value(scheduled[aid, week]) == 1]
            predecessor_checks.append({
                "predecessor": pred,
                "predecessor_last_week": int(solver.Value(finish[pred])),
                "successor": aid,
                "successor_first_week": min(successor_weeks),
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
            "internal_hard_rule_error_count": len(internal_hard_rule_errors),
            "wall_time_seconds": float(solver.WallTime()),
        },
        "predecessor_checks": predecessor_checks,
        "internal_hard_rule_errors": internal_hard_rule_errors,
        "debug": {
            "solver_version": SOLVER_VERSION,
            "horizon_start": horizon_start.isoformat(),
            "horizon_weeks": horizon_weeks,
            "slot_count": slot_count,
        },
    }


def _main() -> None:
    parser = argparse.ArgumentParser(description="NebulaX PS1 OR-Tools railway access solver")
    parser.add_argument("--data-dir", required=True, help="Folder containing 01_LINES.csv ... 08_ACTIVITY_DETAILS.csv")
    parser.add_argument("--scenario", choices=["A", "B", "C", "a", "b", "c"], default="A")
    parser.add_argument("--output-dir", default="submission", help="Folder to write the three submission CSVs")
    parser.add_argument("--time-limit", type=float, default=60.0)
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
