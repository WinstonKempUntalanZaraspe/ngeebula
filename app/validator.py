from __future__ import annotations

import datetime as dt
from collections import Counter, defaultdict
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple

Row = Mapping[str, Any]
Rows = Sequence[Row]

_TABLE_ALIASES: Dict[str, Tuple[str, ...]] = {
    "lines": ("lines", "01_LINES.csv", "01_LINES", "LINES"),
    "stations": ("stations", "02_STATIONS.csv", "02_STATIONS", "STATIONS"),
    "sectors": ("sectors", "03_SECTORS.csv", "03_SECTORS", "SECTORS"),
    "location_supply": ("location_supply", "04_LOCATION_SUPPLY.csv", "04_LOCATION_SUPPLY", "LOCATION_SUPPLY"),
    "buffer_location": ("buffer_location", "05_BUFFER_LOCATION.csv", "05_BUFFER_LOCATION", "BUFFER_LOCATION"),
    "parameters": ("parameters", "06_PARAMETERS.csv", "06_PARAMETERS", "PARAMETERS"),
    "project_details": ("project_details", "07_PROJECT_DETAILS.csv", "07_PROJECT_DETAILS", "PROJECT_DETAILS"),
    "activity_details": ("activity_details", "08_ACTIVITY_DETAILS.csv", "08_ACTIVITY_DETAILS", "ACTIVITY_DETAILS"),
}

EXPECTED_COLUMNS = {
    "SCHEDULE_ACCESS": ["activity_id", "access_seq", "week", "eclo", "access_night"],
    "SCHEDULE_OCCUPANCY": ["activity_id", "week", "location_id", "co_share_group"],
    "RESULTS": ["scenario", "contract_number", "simulated_completion_date", "overrun_days"],
}

CONTRACT_WEIGHT = {1: 100.0, 2: 10.0, 3: 1.0}
ACTIVITY_MULTIPLIER = {1: 1.3, 2: 1.2, 3: 1.0}


def _s(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none"} else text


def _i(value: Any, field: str, default: int | None = None) -> int:
    text = _s(value)
    if text == "":
        if default is None:
            raise ValueError(f"{field} is empty")
        return default
    try:
        return int(float(text))
    except ValueError as exc:
        raise ValueError(f"{field} is not a number: {value!r}") from exc


def _flag(value: Any) -> bool:
    return _s(value).lower() in {"1", "true", "yes", "y", "t"}


def _is_eclo(value: Any) -> bool:
    text = _s(value).lower()
    if text in {"", "0", "false", "no", "n", "f"}:
        return False
    if text in {"1", "true", "yes", "y", "t", "eclo"}:
        return True
    try:
        return int(float(text)) == 1
    except ValueError:
        return False


def _date(value: Any, field: str) -> dt.date:
    text = _s(value)[:10]
    try:
        return dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} is not an ISO date: {value!r}") from exc


def table(instance: Mapping[str, Any], canonical: str) -> List[Dict[str, Any]]:
    for key in _TABLE_ALIASES.get(canonical, (canonical,)):
        rows = instance.get(key)
        if rows:
            return [dict(row) for row in rows]
    return []


def _parameters(instance: Mapping[str, Any]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for row in table(instance, "parameters"):
        key = _s(row.get("key") or row.get("parameter") or row.get("name"))
        if key:
            out[key] = _s(row.get("value"))
    return out


def horizon(instance: Mapping[str, Any]) -> Tuple[dt.date, int]:
    params = _parameters(instance)
    return _date(params.get("horizon_start"), "horizon_start"), _i(params.get("horizon_weeks"), "horizon_weeks")


def supply_map(instance: Mapping[str, Any]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in table(instance, "location_supply"):
        loc = _s(row.get("location_id"))
        if loc:
            out[loc] = _i(row.get("supply_capacity"), f"{loc}.supply_capacity", 0)
    return out


def _swap_bound(location_id: str) -> str:
    if location_id.endswith(":EB"):
        return location_id[:-3] + ":WB"
    if location_id.endswith(":WB"):
        return location_id[:-3] + ":EB"
    return location_id


class _Geometry:
    def __init__(self, instance: Mapping[str, Any], problems: List[Tuple[str, str]]):
        self.supply = supply_map(instance)
        self.locations = set(self.supply)
        self.projects = {_s(r.get("contract_number")): r for r in table(instance, "project_details")}
        self.activities = {_s(r.get("activity_id")): r for r in table(instance, "activity_details")}
        self.sector_by_id: Dict[str, Dict[str, Any]] = {}
        self.by_line: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for raw in table(instance, "sectors"):
            row = dict(raw)
            sid = _s(row.get("sector_id"))
            line = _s(row.get("line_code"))
            row["seq"] = _i(row.get("seq"), "sector.seq", 0)
            self.sector_by_id[sid] = row
            self.by_line[line].append(row)
        for line in self.by_line:
            self.by_line[line].sort(key=lambda r: int(r["seq"]))

        stations = table(instance, "stations")
        self.interchanges = {
            _s(r.get("station_id"))
            for r in stations
            if _flag(r.get("is_interchange")) and _s(r.get("station_id"))
        }
        if not self.interchanges:
            lines_by_station: Dict[str, Set[str]] = defaultdict(set)
            for row in stations:
                sid, line = _s(row.get("station_id")), _s(row.get("line_code"))
                if sid and line:
                    lines_by_station[sid].add(line)
            self.interchanges = {sid for sid, lines in lines_by_station.items() if len(lines) > 1}

        self.buffers: Dict[str, int] = {}
        self.mirror_natures: Set[str] = set()
        for row in table(instance, "buffer_location"):
            nature = _s(row.get("nature_of_works"))
            if nature:
                self.buffers[nature] = _i(row.get("up_to_buffer_sectors"), f"{nature}.buffer", 0)
                if _flag(row.get("opposite_bound_required")):
                    self.mirror_natures.add(nature)

        self.route: Dict[str, Set[str]] = {}
        self.closure: Dict[str, Set[str]] = {}
        self.affected_lines: Dict[str, Set[str]] = {}
        self.atype_of: Dict[str, str] = {}
        self.nature_of: Dict[str, str] = {}

        for aid, activity in self.activities.items():
            contract = _s(activity.get("contract_number"))
            project = self.projects.get(contract)
            if project is None:
                problems.append(("format", f"{aid} refers to unknown contract {contract!r}"))
                self.route[aid], self.closure[aid], self.affected_lines[aid] = set(), set(), set()
                self.atype_of[aid], self.nature_of[aid] = "", ""
                continue
            self.atype_of[aid] = _s(project.get("access_type"))
            self.nature_of[aid] = _s(project.get("nature_of_activity"))
            try:
                route, closure = self._expand(activity, project)
            except Exception as exc:
                problems.append(("format", f"{aid}: {exc}"))
                route, closure = set(), set()
            self.route[aid] = route
            self.closure[aid] = closure
            self.affected_lines[aid] = {loc.split(":")[1] for loc in closure if len(loc.split(":")) == 4}

    def _locs(self, rows: Rows, line: str, bound: str) -> Set[str]:
        out: Set[str] = set()
        touched: Set[str] = set()
        for row in rows:
            out.add(f"{_s(row.get('sector_id'))}:{bound}")
            touched.add(_s(row.get("from_station_id")))
            touched.add(_s(row.get("to_station_id")))
        for station in touched:
            if station:
                out.add(f"PLAT:{line}:{station}:{bound}")
        return out & self.locations

    def _expand(self, activity: Row, project: Row) -> Tuple[Set[str], Set[str]]:
        start = _s(activity.get("start_location_id"))
        end = _s(activity.get("end_location_id"))
        a = start.split(":")
        b = end.split(":")
        if len(a) != 4 or len(b) != 4 or a[0] != "SEC" or b[0] != "SEC" or a[1] != b[1] or a[3] != b[3]:
            raise ValueError("route endpoints must be sectors on one line and bound")
        line, bound = a[1], a[3]
        rows = self.by_line[line]
        ids = [_s(r.get("sector_id")) for r in rows]
        i, j = sorted((ids.index(":".join(a[:3])), ids.index(":".join(b[:3]))))
        route = self._locs(rows[i:j + 1], line, bound)
        nature = _s(project.get("nature_of_activity"))
        radius = self.buffers.get(nature, 0)
        lo, hi = max(0, i - radius), min(len(rows) - 1, j + radius)
        full = self._locs(rows[lo:hi + 1], line, bound)
        live = nature.lower() == "live" or nature in self.mirror_natures
        if live:
            closure = set(full)
        else:
            closure = set(route) | {loc for loc in full if loc.startswith("SEC:")}
        if live:
            closure |= {_swap_bound(loc) for loc in list(closure)}
            touched = {
                loc.split(":")[2]
                for loc in closure
                if loc.startswith("PLAT:") and loc.split(":")[2] in self.interchanges
            }
            if touched:
                for other, other_rows in self.by_line.items():
                    if other == line:
                        continue
                    for idx, row in enumerate(other_rows):
                        ends = {_s(row.get("from_station_id")), _s(row.get("to_station_id"))}
                        if ends <= self.interchanges and ends & touched:
                            buffered = other_rows[max(0, idx - radius):min(len(other_rows), idx + radius + 1)]
                            closure |= self._locs(buffered, other, "EB")
                            closure |= self._locs(buffered, other, "WB")
        return route, closure & self.locations


def capacity_usage(instance: Mapping[str, Any], schedule_occupancy: Rows) -> List[Dict[str, Any]]:
    supply = supply_map(instance)
    used: Dict[Tuple[str, int], Set[str]] = defaultdict(set)
    for row in schedule_occupancy:
        loc = _s(row.get("location_id"))
        if not loc:
            continue
        used[loc, _i(row.get("week"), "week", 0)].add(_s(row.get("co_share_group")))
    out = []
    for (loc, week), groups in sorted(used.items()):
        out.append({"location_id": loc, "week": week, "used": len(groups), "supply": supply.get(loc, 0)})
    return out


def _possession_components(groups: Mapping[Tuple[str, int, str], Set[str]], by_week: Mapping[int, Set[str]]) -> Dict[Tuple[int, str], Set[str]]:
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


def validate(
    instance: Mapping[str, Any],
    schedule_access: Rows,
    schedule_occupancy: Rows,
    results: Rows,
    scenario: str,
) -> Dict[str, Any]:
    scenario = _s(scenario).upper()
    if scenario not in {"A", "B", "C"}:
        raise ValueError("scenario must be one of: A, B, C")

    violations: List[Tuple[str, str]] = []

    def V(rule: str, detail: str) -> None:
        violations.append((rule, detail))

    geo = _Geometry(instance, violations)
    projects = geo.projects
    activities = geo.activities
    supply = geo.supply
    horizon_start, horizon_weeks = horizon(instance)

    def week_of(day: dt.date) -> int:
        return (day - horizon_start).days // 7 + 1

    def week_end(week: int) -> dt.date:
        return horizon_start + dt.timedelta(days=7 * week - 1)

    access = [dict(r) for r in schedule_access]
    occupancy = [dict(r) for r in schedule_occupancy]
    result_rows = [dict(r) for r in results]

    for name, rows in (("SCHEDULE_ACCESS", access), ("SCHEDULE_OCCUPANCY", occupancy), ("RESULTS", result_rows)):
        if rows and list(rows[0].keys()) != EXPECTED_COLUMNS[name]:
            V("format", f"{name} columns {list(rows[0].keys())} != {EXPECTED_COLUMNS[name]}")

    access_by_activity: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_week: Dict[int, Set[str]] = defaultdict(set)
    for row in access:
        aid = _s(row.get("activity_id"))
        if aid not in activities:
            V("reference", f"Unknown activity {aid}")
            continue
        week = _i(row.get("week"), "week", 0)
        access_by_activity[aid].append(row)
        by_week[week].add(aid)

    finish: Dict[str, int] = {}
    for aid, activity in activities.items():
        rows = access_by_activity.get(aid, [])
        if not rows:
            V("workload", f"{aid} has no access rows")
            finish[aid] = 0
            continue
        units = sum(3 if _is_eclo(r.get("eclo")) else 2 for r in rows)
        need = 2 * _i(activity.get("total_accesses"), f"{aid}.total_accesses", 0)
        if units < need:
            V("workload", f"{aid}: delivered {units / 2:g} < {need / 2:g}")
        weeks = [_i(r.get("week"), "week", 0) for r in rows]
        if len(weeks) != len(set(weeks)):
            V("access_week", f"{aid}: more than one access in same week")
        earliest = max(1, week_of(_date(activity.get("planned_start_date"), "planned_start_date")))
        if min(weeks) < earliest:
            V("planned_start", f"{aid}: week {min(weeks)} < {earliest}")
        ordered = sorted(rows, key=lambda r: _i(r.get("week"), "week", 0))
        if [ _i(r.get("access_seq"), "access_seq", 0) for r in ordered ] != list(range(1, len(rows) + 1)):
            V("sequence", f"{aid}: access_seq must be contiguous from 1")
        if scenario == "A" and any(_is_eclo(r.get("eclo")) for r in rows):
            V("eclo", f"{aid}: Scenario A forbids ECLO")
        finish[aid] = max(weeks)

    for aid, activity in activities.items():
        pred = _s(activity.get("predecessor_activity_id"))
        if pred and access_by_activity.get(aid):
            first = min(_i(r.get("week"), "week", 0) for r in access_by_activity[aid])
            if not access_by_activity.get(pred) or first <= finish.get(pred, 0):
                V("predecessor", f"{aid}: starts before predecessor {pred} finishes")

    expected = {
        (aid, _i(row.get("week"), "week", 0), loc)
        for aid, rows in access_by_activity.items()
        for row in rows
        for loc in geo.route.get(aid, set())
    }
    actual = Counter((_s(r.get("activity_id")), _i(r.get("week"), "week", 0), _s(r.get("location_id"))) for r in occupancy)
    for key in sorted(expected - actual.keys()):
        V("route", f"Missing occupancy {key}")
    for key, count in actual.items():
        if key not in expected or count != 1:
            V("route", f"Unexpected or duplicate occupancy {key}")

    groups: Dict[Tuple[str, int, str], Set[str]] = defaultdict(set)
    location_week_groups: Dict[Tuple[str, int], Set[str]] = defaultdict(set)
    for row in occupancy:
        aid = _s(row.get("activity_id"))
        loc = _s(row.get("location_id"))
        week = _i(row.get("week"), "week", 0)
        group = _s(row.get("co_share_group"))
        if aid not in activities or loc not in supply:
            V("reference", f"Unknown occupancy {aid}/{loc}")
            continue
        groups[loc, week, group].add(aid)
        location_week_groups[loc, week].add(group)

    excess_total = 0
    hotspots: List[Dict[str, Any]] = []
    for (loc, week), group_names in sorted(location_week_groups.items()):
        cap = supply.get(loc, 0)
        over = max(0, len(group_names) - cap)
        excess_total += over
        if scenario == "A" and over > 0:
            V("capacity", f"{loc}, week {week}: {len(group_names)} possessions against capacity {cap}")
        if scenario == "C" and over > 1:
            V("capacity", f"{loc}, week {week}: {len(group_names)} possessions against capacity {cap}")
        hotspots.append({"location_id": loc, "week": week, "used": len(group_names), "capacity": cap, "excess": over})

    for (loc, week, group), aids in groups.items():
        kinds = Counter(geo.atype_of.get(aid, "") for aid in aids)
        if len(aids) > 4 or kinds["PC"] > 1 or (kinds["PM"] and len(aids) > 1):
            V("mix", f"{loc}, week {week}, {group}: illegal possession mix")

    components = _possession_components(groups, by_week)
    for (week, _root), members in sorted(components.items()):
        closure = set().union(*(geo.closure[aid] for aid in members))
        for aid in sorted(by_week[week] - members):
            overlap = geo.route[aid] & closure
            if overlap:
                V("closure", f"wk{week}: {aid} inside closure of {sorted(members)[:3]} at {sorted(overlap)[:4]}")

    accounts: Dict[Tuple[str, str, int, int], Set[str]] = defaultdict(set)
    for row in access:
        aid = _s(row.get("activity_id"))
        if aid not in activities:
            continue
        activity = activities[aid]
        contract = _s(activity.get("contract_number"))
        atype = _s(activity.get("activity_type"))
        week = _i(row.get("week"), "week", 0)
        night = _i(row.get("access_night"), "access_night", 0)
        accounts[contract, atype, week, night].add(aid)

    by_ctw: Dict[Tuple[str, str, int], Set[int]] = defaultdict(set)
    for (contract, atype, week, night), aids in accounts.items():
        by_ctw[contract, atype, week].add(night)
        project = projects[contract]
        cap = _i(project.get("number_of_maximum_access_per_week"), "max_access", 0)
        workfronts = _i(project.get("number_of_workfronts"), "workfronts", 0)
        if not 1 <= night <= cap:
            V("allocation", f"{contract}/{atype} wk{week}: access_night {night} outside 1..{cap}")
        if len(aids) > workfronts:
            V("workfront", f"{contract}/{atype} wk{week} access {night}: {len(aids)} > {workfronts}")
    for (contract, atype, week), nights in by_ctw.items():
        cap = _i(projects[contract].get("number_of_maximum_access_per_week"), "max_access", 0)
        if len(nights) > cap:
            V("allocation", f"{contract}/{atype} wk{week}: {len(nights)} nights > {cap}")

    if scenario == "C":
        eclo_by_line: Dict[str, List[int]] = defaultdict(list)
        for row in access:
            if not _is_eclo(row.get("eclo")):
                continue
            aid = _s(row.get("activity_id"))
            week = _i(row.get("week"), "week", 0)
            for line in geo.affected_lines.get(aid, set()):
                eclo_by_line[line].append(week)
        for line, ws in eclo_by_line.items():
            if ws and max(ws) - min(ws) > 1:
                V("eclo_window", f"{line}: ECLO spans more than two consecutive weeks")

    results_by_contract = {_s(r.get("contract_number")): r for r in result_rows}
    if len(results_by_contract) != len(result_rows) or set(results_by_contract) != set(projects):
        V("results", "RESULTS must contain exactly one row per contract")

    overrun_total = 0
    earliness_total = 0
    tiers = {"1": 0, "2": 0, "3": 0}
    weighted = 0.0
    contracts_overrunning = 0
    contract_details = []

    for contract, project in projects.items():
        members = [aid for aid, a in activities.items() if _s(a.get("contract_number")) == contract]
        end_week = max((finish.get(aid, 0) for aid in members), default=0)
        completion = week_end(end_week) if end_week else horizon_start
        planned = _date(project.get("planned_completion_date"), f"{contract}.planned_completion_date")
        late = max(0, (completion - planned).days)
        early = max(0, (planned - completion).days)
        if scenario == "B" and late:
            V("planned_date", f"{contract}: completion misses {planned} by {late} days")
        row = results_by_contract.get(contract)
        if row is not None:
            if _s(row.get("scenario")).upper() != scenario:
                V("results", f"{contract}: wrong scenario")
            if _s(row.get("simulated_completion_date"))[:10] != completion.isoformat():
                V("results", f"{contract}: completion summary mismatch")
            if _i(row.get("overrun_days"), "overrun_days", -1) != late:
                V("results", f"{contract}: overrun summary mismatch")
        overrun_total += late
        earliness_total += early
        contracts_overrunning += int(late > 0)
        tier = _i(project.get("contract_priority"), "contract_priority", 3)
        tiers[str(tier)] = tiers.get(str(tier), 0) + late
        weight = CONTRACT_WEIGHT.get(tier, 1.0) * sum(
            ACTIVITY_MULTIPLIER.get(_i(activities[aid].get("activity_priority"), "activity_priority", 3), 1.0)
            for aid in members
        )
        weighted += late * weight
        contract_details.append({
            "contract_number": contract,
            "completion_week": end_week,
            "simulated_completion_date": completion.isoformat(),
            "planned_completion_date": planned.isoformat(),
            "overrun_days": late,
            "priority": tier,
            "score_weight": weight,
            "weighted_overrun_score": round(late * weight, 1),
        })

    eclo_total = sum(int(_is_eclo(r.get("eclo"))) for r in access)
    objective = (0.0 if scenario == "B" else weighted) + (0 if scenario == "A" else 7 * excess_total + 5 * eclo_total)

    return {
        "scenario": scenario,
        "feasible": not violations,
        "hard_violations": [{"rule": rule, "severity": "hard", "detail": detail} for rule, detail in violations],
        "soft_scores": {
            "scenario": scenario,
            "overrun_days_total": overrun_total,
            "contracts_overrunning": contracts_overrunning,
            "earliness_days_total": earliness_total,
            "excess_access_nights_total": excess_total,
            "eclo_nights_total": eclo_total,
            "priority_overrun": {k: tiers.get(k, 0) for k in ("1", "2", "3")},
            "priority_weighted_score": round(weighted, 1),
            "objective_score": round(objective, 1),
        },
        "detail": {
            "capacity_hotspots": [h for h in hotspots if h["used"] >= h["capacity"]],
            "nights_scheduled": len(access),
            "eclo_nights": eclo_total,
        },
        "contracts": contract_details,
    }
