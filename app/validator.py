"""Independent PS1 rule checker (our re-implementation of the judges' validator).

Ported from ``solver-review/check_submission.py`` with the same rule semantics,
but as importable functions instead of a CLI, and with the hub/interchange names
derived from the data instead of hard-coded.

Checks the hard rules in PS1 README 2.4 and reports the soft scores of 2.5, in
the report shape of README 2.7.

Closure checking follows the semantics used by this project when the final
solver was debugged: an actual shared ``(location, week, co_share_group)``
creates a co-sharing exemption; Live closures are treated conservatively at
week level, while other closures use the exported group labels as the local
night witness. This remains an independent checker, not the organisers' hidden
validator implementation.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple

Row = Mapping[str, Any]
Rows = Sequence[Row]

CONTRACT_WEIGHT: Dict[str, float] = {"1": 100.0, "2": 10.0, "3": 1.0}
ACTIVITY_NUDGE: Dict[str, float] = {"1": 0.3, "2": 0.2, "3": 0.0}
VALIDATOR_VERSION = "hard-rules-v13-final"

EXPECTED_COLUMNS: Dict[str, List[str]] = {
    "SCHEDULE_ACCESS": ["activity_id", "access_seq", "week", "eclo", "access_night"],
    "SCHEDULE_OCCUPANCY": ["activity_id", "week", "location_id", "co_share_group"],
    "RESULTS": ["scenario", "contract_number", "simulated_completion_date", "overrun_days"],
}

_TABLE_ALIASES: Dict[str, Tuple[str, ...]] = {
    "lines": ("lines", "01_LINES.csv", "01_LINES", "LINES"),
    "stations": ("stations", "02_STATIONS.csv", "02_STATIONS", "STATIONS"),
    "sectors": ("sectors", "03_SECTORS.csv", "03_SECTORS", "SECTORS"),
    "location_supply": ("location_supply", "04_LOCATION_SUPPLY.csv", "LOCATION_SUPPLY"),
    "buffer_location": ("buffer_location", "05_BUFFER_LOCATION.csv", "BUFFER_LOCATION"),
    "parameters": ("parameters", "06_PARAMETERS.csv", "PARAMETERS"),
    "project_details": ("project_details", "07_PROJECT_DETAILS.csv", "PROJECT_DETAILS"),
    "activity_details": ("activity_details", "08_ACTIVITY_DETAILS.csv", "ACTIVITY_DETAILS"),
}


# --------------------------------------------------------------------------- #
# small coercion helpers: CSV rows arrive as strings, JSON rows as int/str mix
# --------------------------------------------------------------------------- #

def _s(value: Any) -> str:
    """Anything -> trimmed string ('' for None/NaN)."""
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
    try:
        return int(float(text)) == 1
    except ValueError:
        return text in {"true", "yes", "y", "t", "eclo"}


def _date(value: Any, field: str) -> dt.date:
    text = _s(value)[:10]
    try:
        return dt.date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} is not an ISO date: {value!r}") from exc


def table(instance: Mapping[str, Any], canonical: str) -> List[Dict[str, Any]]:
    """Fetch one instance table by its canonical name (tolerates file-name keys)."""
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
    """(horizon_start, horizon_weeks) read from 06_PARAMETERS — never guessed."""
    params = _parameters(instance)
    return _date(params.get("horizon_start"), "horizon_start"), _i(
        params.get("horizon_weeks"), "horizon_weeks"
    )


def supply_map(instance: Mapping[str, Any]) -> Dict[str, int]:
    out: Dict[str, int] = {}
    for row in table(instance, "location_supply"):
        loc = _s(row.get("location_id"))
        if loc:
            out[loc] = _i(row.get("supply_capacity"), f"{loc}.supply_capacity", 0)
    return out


# --------------------------------------------------------------------------- #
# network geometry
# --------------------------------------------------------------------------- #

def _sector_middle(sector_id: str) -> str:
    parts = _s(sector_id).split(":")
    return parts[2] if len(parts) >= 3 else ""


def _swap_bound(location_id: str) -> str:
    if location_id.endswith(":EB"):
        return location_id[:-3] + ":WB"
    if location_id.endswith(":WB"):
        return location_id[:-3] + ":EB"
    return location_id


def _hub_geometry(
    stations: Rows, sectors: Rows
) -> Tuple[Set[str], Set[str]]:
    """Interchange stations and the sector 'middles' that join them.

    Derived from ``02_STATIONS.is_interchange`` / ``03_SECTORS.is_shared``; if
    neither flag is set anywhere we fall back to 'the same station id appears on
    more than one line'. Never hard-codes H01/H02.
    """
    station_lines: Dict[str, Set[str]] = defaultdict(set)
    flagged: Set[str] = set()
    for row in stations:
        sid = _s(row.get("station_id"))
        if not sid:
            continue
        station_lines[sid].add(_s(row.get("line_code")))
        if _flag(row.get("is_interchange")):
            flagged.add(sid)
    if not flagged:
        flagged = {sid for sid, lines in station_lines.items() if len(lines) > 1}

    hub_middles: Set[str] = set()
    for row in sectors:
        middle = _sector_middle(row.get("sector_id"))
        if not middle:
            continue
        both_hubs = (
            _s(row.get("from_station_id")) in flagged
            and _s(row.get("to_station_id")) in flagged
        )
        if _flag(row.get("is_shared")) or both_hubs:
            hub_middles.add(middle)
    return flagged, hub_middles


def _bounds(supply: Mapping[str, int]) -> List[str]:
    found = {loc.rsplit(":", 1)[-1] for loc in supply if ":" in loc}
    found = {b for b in found if b}
    return sorted(found) if found else ["EB", "WB"]


class _Geometry:
    """Routes and closures per activity, plus the lookups the checks need."""

    def __init__(self, instance: Mapping[str, Any], problems: List[Tuple[str, str]]):
        self.supply = supply_map(instance)
        self.locations = set(self.supply)
        self.bounds = _bounds(self.supply)
        sectors = table(instance, "sectors")
        stations = table(instance, "stations")
        self.hub_stations, self.hub_middles = _hub_geometry(stations, sectors)

        self.sector_by_id = {_s(r.get("sector_id")): r for r in sectors}
        self.by_line: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for row in sectors:
            self.by_line[_s(row.get("line_code"))].append(row)
        for line in self.by_line:
            self.by_line[line].sort(key=lambda r: _i(r.get("seq"), "sector.seq", 0))

        self.buffers: Dict[str, int] = {}
        for row in table(instance, "buffer_location"):
            nature = _s(row.get("nature_of_works"))
            if nature:
                self.buffers[nature] = _i(
                    row.get("up_to_buffer_sectors"), f"{nature}.up_to_buffer_sectors", 0
                )
        self.mirror_natures = {
            _s(r.get("nature_of_works"))
            for r in table(instance, "buffer_location")
            if _flag(r.get("opposite_bound_required"))
        }

        self.projects = {
            _s(r.get("contract_number")): r for r in table(instance, "project_details")
        }
        self.activities = {
            _s(r.get("activity_id")): r for r in table(instance, "activity_details")
        }

        self.route: Dict[str, Set[str]] = {}
        self.closure: Dict[str, Set[str]] = {}
        self.nature_of: Dict[str, str] = {}
        self.atype_of: Dict[str, str] = {}
        self.contract_of: Dict[str, str] = {}
        for activity_id, activity in self.activities.items():
            contract = _s(activity.get("contract_number"))
            project = self.projects.get(contract)
            if project is None:
                problems.append(
                    ("format", f"{activity_id} refers to unknown contract {contract!r}")
                )
                self.route[activity_id] = set()
                self.closure[activity_id] = set()
                self.nature_of[activity_id] = ""
                self.atype_of[activity_id] = ""
                self.contract_of[activity_id] = contract
                continue
            self.contract_of[activity_id] = contract
            nature = _s(project.get("nature_of_activity"))
            self.nature_of[activity_id] = nature
            self.atype_of[activity_id] = _s(project.get("access_type"))
            if nature not in self.buffers:
                problems.append(
                    (
                        "format",
                        f"{activity_id}: nature_of_activity {nature!r} is not in "
                        f"05_BUFFER_LOCATION; assuming no buffer",
                    )
                )
            try:
                route, closure = self._route_and_closure(activity, nature)
            except ValueError as exc:
                problems.append(("format", f"{activity_id}: {exc}"))
                route, closure = set(), set()
            self.route[activity_id] = route
            self.closure[activity_id] = closure

    # ---- helpers ---------------------------------------------------------- #

    def _locs(self, rows: Rows, line: str, bound: str) -> Set[str]:
        out: Set[str] = set()
        touched: Set[str] = set()
        for row in rows:
            out.add(f"{_s(row.get('sector_id'))}:{bound}")
            touched.add(_s(row.get("from_station_id")))
            touched.add(_s(row.get("to_station_id")))
        for station in touched:
            out.add(f"PLAT:{line}:{station}:{bound}")
        return out & self.locations

    def _route_and_closure(
        self, activity: Row, nature: str
    ) -> Tuple[Set[str], Set[str]]:
        start = _s(activity.get("start_location_id"))
        end = _s(activity.get("end_location_id"))
        parts = start.split(":")
        if len(parts) != 4:
            raise ValueError(f"start_location_id {start!r} is not KIND:LINE:MID:BOUND")
        _, line, middle, bound = parts
        start_base = f"SEC:{line}:{middle}"
        end_base = ":".join(end.split(":")[:3])
        if start_base not in self.sector_by_id or end_base not in self.sector_by_id:
            raise ValueError(f"route {start} -> {end} is not a known sector pair")
        seq_a = _i(self.sector_by_id[start_base].get("seq"), "seq")
        seq_b = _i(self.sector_by_id[end_base].get("seq"), "seq")
        low, high = sorted((seq_a, seq_b))

        line_rows = self.by_line.get(line, [])
        route = self._locs(
            [r for r in line_rows if low <= _i(r.get("seq"), "seq", 0) <= high],
            line,
            bound,
        )
        buffer_size = self.buffers.get(nature, 0)
        closure = self._locs(
            [
                r
                for r in line_rows
                if low - buffer_size <= _i(r.get("seq"), "seq", 0) <= high + buffer_size
            ],
            line,
            bound,
        )
        is_live = nature.lower() == "live" or nature in self.mirror_natures
        if is_live:
            closure |= {_swap_bound(loc) for loc in closure}
            # Cross-line hub closure: the organisers' validator continues the
            # buffer outward from each interchange station on the OTHER line(s)
            # by up_to_buffer_sectors sectors, closing those sectors and the
            # platforms of every station they touch, on both bounds.
            touched_hubs = self._touched_hub_stations(closure)
            if touched_hubs:
                extra = max(buffer_size - 1, 0)
                for other_line, other_rows in self.by_line.items():
                    if other_line == line:
                        continue
                    keep: Set[int] = set()
                    for hub in touched_hubs:
                        adjacent = [
                            _i(r.get("seq"), "seq", 0)
                            for r in other_rows
                            if _s(r.get("from_station_id")) == hub
                            or _s(r.get("to_station_id")) == hub
                        ]
                        if not adjacent:
                            continue
                        low_h, high_h = min(adjacent) - extra, max(adjacent) + extra
                        keep.update(
                            _i(r.get("seq"), "seq", 0)
                            for r in other_rows
                            if low_h <= _i(r.get("seq"), "seq", 0) <= high_h
                        )
                    selected = [
                        r for r in other_rows if _i(r.get("seq"), "seq", 0) in keep
                    ]
                    for bnd in self.bounds:
                        closure |= self._locs(selected, other_line, bnd)
        return route, closure & self.locations

    def _touched_hub_stations(self, closure: Set[str]) -> Set[str]:
        """Interchange stations that a closure set reaches (platform or sector end)."""
        hubs: Set[str] = set()
        for loc in closure:
            parts = loc.split(":")
            if len(parts) != 4:
                continue
            kind, lin, middle, _bnd = parts
            if kind == "PLAT":
                if middle in self.hub_stations:
                    hubs.add(middle)
                continue
            row = self.sector_by_id.get(f"SEC:{lin}:{middle}")
            if row is None:
                continue
            for end in (row.get("from_station_id"), row.get("to_station_id")):
                if _s(end) in self.hub_stations:
                    hubs.add(_s(end))
        return hubs

    def _is_hub_location(self, location_id: str) -> bool:
        parts = location_id.split(":")
        if len(parts) != 4:
            return False
        kind, _line, middle, _bound = parts
        if kind == "SEC":
            return middle in self.hub_middles
        return middle in self.hub_stations


# --------------------------------------------------------------------------- #
# public API
# --------------------------------------------------------------------------- #

def capacity_usage(
    instance: Mapping[str, Any], schedule_occupancy: Rows
) -> List[Dict[str, Any]]:
    """Every location-week that is used at all: distinct possessions vs supply."""
    supply = supply_map(instance)
    used: Dict[Tuple[str, int], Set[str]] = defaultdict(set)
    for row in schedule_occupancy:
        loc = _s(row.get("location_id"))
        if not loc:
            continue
        try:
            week = _i(row.get("week"), "week")
        except ValueError:
            continue
        used[(loc, week)].add(_s(row.get("co_share_group")))
    out = [
        {
            "location_id": loc,
            "week": week,
            "used": len(groups),
            "supply": supply.get(loc, 0),
        }
        for (loc, week), groups in used.items()
        if groups
    ]
    out.sort(key=lambda r: (r["location_id"], r["week"]))
    return out


def validate(
    instance: Mapping[str, Any],
    schedule_access: Rows,
    schedule_occupancy: Rows,
    results: Rows,
    scenario: str,
) -> Dict[str, Any]:
    """Check one submission against one instance. Returns the README 2.7 report."""
    scenario = _s(scenario).upper()
    if scenario not in {"A", "B", "C"}:
        raise ValueError("scenario must be one of: A, B, C")

    viol: List[Tuple[str, str]] = []

    def V(rule: str, message: str) -> None:
        viol.append((rule, message))

    geo = _Geometry(instance, viol)
    supply = geo.supply
    activities = geo.activities
    projects = geo.projects
    horizon_start, _horizon_weeks = horizon(instance)

    def week_of(day: dt.date) -> int:
        return (day - horizon_start).days // 7 + 1

    def week_end(week: int) -> dt.date:
        return horizon_start + dt.timedelta(days=7 * week - 1)

    access = [dict(r) for r in schedule_access]
    occupancy = [dict(r) for r in schedule_occupancy]
    result_rows = [dict(r) for r in results]

    # ---- column checks ---------------------------------------------------- #
    for name, rows in (
        ("SCHEDULE_ACCESS", access),
        ("SCHEDULE_OCCUPANCY", occupancy),
        ("RESULTS", result_rows),
    ):
        if rows and list(rows[0].keys()) != EXPECTED_COLUMNS[name]:
            V("format", f"{name} columns {list(rows[0].keys())} != {EXPECTED_COLUMNS[name]}")

    # ---- rule 1 workload, one row per week, rule 2 start date, ECLO in A --- #
    acc_by_act: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for row in access:
        activity_id = _s(row.get("activity_id"))
        if activity_id not in activities:
            V("format", f"SCHEDULE_ACCESS references unknown activity {activity_id!r}")
            continue
        acc_by_act[activity_id].append(row)

    for activity_id, activity in activities.items():
        rows = acc_by_act.get(activity_id, [])
        if not rows:
            V("workload", f"{activity_id} has no access rows")
            continue
        yielded = sum(1.5 if _is_eclo(r.get("eclo")) else 1.0 for r in rows)
        needed = float(_s(activity.get("total_accesses")) or 0)
        if yielded < needed:
            V("workload", f"{activity_id} yield {yielded} < total_accesses {needed:g}")
        weeks = [_i(r.get("week"), "week", 0) for r in rows]
        if len(weeks) != len(set(weeks)):
            V("one_per_week", f"{activity_id} has more than one access in a week")
        start_week = week_of(_date(activity.get("planned_start_date"), "planned_start_date"))
        if min(weeks) < start_week:
            V(
                "planned_start",
                f"{activity_id} starts week {min(weeks)} < planned start week {start_week}",
            )
        if scenario == "A" and any(_is_eclo(r.get("eclo")) for r in rows):
            V("eclo", f"{activity_id} uses ECLO in scenario A")
        ordered = sorted(rows, key=lambda r: _i(r.get("week"), "week", 0))
        if any(_i(r.get("access_seq"), "access_seq", 0) != i + 1 for i, r in enumerate(ordered)):
            V("format", f"{activity_id} access_seq not 1..n in week order")

    # ---- predecessors ----------------------------------------------------- #
    for activity_id, activity in activities.items():
        pred = _s(activity.get("predecessor_activity_id"))
        if pred and activity_id in acc_by_act and pred in acc_by_act:
            first = min(_i(r.get("week"), "week", 0) for r in acc_by_act[activity_id])
            last = max(_i(r.get("week"), "week", 0) for r in acc_by_act[pred])
            if first <= last:
                V("predecessor", f"{activity_id} starts before predecessor {pred} finishes")

    # ---- occupancy covers the full route ---------------------------------- #
    occ_by_aw: Dict[Tuple[str, int], Dict[str, str]] = defaultdict(dict)
    for row in occupancy:
        activity_id = _s(row.get("activity_id"))
        loc = _s(row.get("location_id"))
        week = _i(row.get("week"), "week", 0)
        if loc and loc not in supply:
            V("format", f"{activity_id} wk{week}: unknown location {loc!r}")
        occ_by_aw[(activity_id, week)][loc] = _s(row.get("co_share_group"))

    for activity_id, rows in acc_by_act.items():
        for row in rows:
            week = _i(row.get("week"), "week", 0)
            got = set(occ_by_aw.get((activity_id, week), {}))
            if got != geo.route.get(activity_id, set()):
                V(
                    "occupancy",
                    f"{activity_id} wk{week} occupancy {sorted(got)} != "
                    f"route {sorted(geo.route.get(activity_id, set()))}",
                )

    # ---- rule 4/8 capacity + legal mix per (location, week) --------------- #
    poss: Dict[Tuple[str, int], Dict[str, Set[str]]] = defaultdict(lambda: defaultdict(set))
    for row in occupancy:
        loc = _s(row.get("location_id"))
        week = _i(row.get("week"), "week", 0)
        poss[(loc, week)][_s(row.get("co_share_group"))].add(_s(row.get("activity_id")))

    excess_total = 0
    hotspots: List[Dict[str, Any]] = []
    for (loc, week), groups in sorted(poss.items()):
        count, cap = len(groups), supply.get(loc, 0)
        if count > cap:
            excess = count - cap
            excess_total += excess
            hotspots.append(
                {"location_id": loc, "week": week, "used": count, "supply": cap}
            )
            if scenario == "A" or (scenario == "C" and excess > 1):
                V("capacity", f"{loc} wk{week}: {count} possessions > supply {cap}")
        for group, members in groups.items():
            types = [geo.atype_of.get(m, "") for m in members]
            pm, pc, co = types.count("PM"), types.count("PC"), types.count("C")
            if pm and len(members) > 1:
                V("mix", f"{loc} wk{week} {group}: PM not alone {sorted(members)}")
            if pc > 1:
                V("mix", f"{loc} wk{week} {group}: two PC {sorted(members)}")
            if pc + co > 4:
                V("mix", f"{loc} wk{week} {group}: more than 4 in possession {sorted(members)}")

    # ---- rule 3/5 closures & buffers (night-level reading, see module doc) - #
    act_week: Dict[int, List[str]] = defaultdict(list)
    for row in access:
        act_week[_i(row.get("week"), "week", 0)].append(_s(row.get("activity_id")))
    for week in sorted(act_week):
        ids = sorted(set(act_week[week]))
        for i, a in enumerate(ids):
            for b in ids[i + 1:]:
                ga = occ_by_aw.get((a, week), {})
                gb = occ_by_aw.get((b, week), {})
                if any(ga.get(loc) == group for loc, group in gb.items()):
                    continue  # partners: they share a possession somewhere -> exempt
                # Live (750 V) closures are week-level in the organisers' validator
                # (confirmed on the public data, A074 vs A039/A065, 19 Sep 2026):
                # anything inside one in the same week is flagged. Other closures
                # are night-level, the night being the co_share_group label.
                nights_a, nights_b = set(ga.values()), set(gb.values())
                live_a = geo.nature_of.get(a, "").lower().startswith("live")
                live_b = geo.nature_of.get(b, "").lower().startswith("live")
                bad_b = [loc for loc, g in gb.items() if loc in geo.closure.get(a, set()) and (live_a or g in nights_a)]
                bad_a = [loc for loc, g in ga.items() if loc in geo.closure.get(b, set()) and (live_b or g in nights_b)]
                if bad_a:
                    V("closure", f"wk{week}: {a} inside closure of ['{b}'] at {sorted(set(bad_a))[:4]}")
                if bad_b:
                    V("closure", f"wk{week}: {b} inside closure of ['{a}'] at {sorted(set(bad_b))[:4]}")

    # ---- rule 6/7 weekly cap + workfronts --------------------------------- #
    by_ctw: Dict[Tuple[str, str, int], Dict[str, Set[str]]] = defaultdict(
        lambda: defaultdict(set)
    )
    for row in access:
        activity_id = _s(row.get("activity_id"))
        activity = activities.get(activity_id)
        if activity is None:
            continue
        key = (
            _s(activity.get("contract_number")),
            _s(activity.get("activity_type")),
            _i(row.get("week"), "week", 0),
        )
        by_ctw[key][_s(row.get("access_night"))].add(activity_id)

    for (contract, atype, week), nights in sorted(by_ctw.items()):
        project = projects.get(contract, {})
        cap = _i(project.get("number_of_maximum_access_per_week"), "max_access", 0)
        workfronts = _i(project.get("number_of_workfronts"), "workfronts", 0)
        if len(nights) > cap:
            V("weekly_cap", f"{contract}/{atype} wk{week}: {len(nights)} access nights > cap {cap}")
        for night, ids in nights.items():
            if len(ids) > workfronts:
                V(
                    "workfronts",
                    f"{contract}/{atype} wk{week} night {night}: {len(ids)} activities "
                    f"> workfronts {workfronts}",
                )
            night_index = _i(night, "access_night", 0)
            if night_index < 1 or night_index > cap:
                V("format", f"{contract} wk{week}: access_night {night} outside 1..{cap}")

    # ---- rule 9 ECLO window (Scenario C) ---------------------------------- #
    if scenario == "C":
        eclo_weeks_by_line: Dict[str, Set[int]] = defaultdict(set)
        for row in access:
            if not _is_eclo(row.get("eclo")):
                continue
            week = _i(row.get("week"), "week", 0)
            for loc in geo.closure.get(_s(row.get("activity_id")), set()):
                parts = loc.split(":")
                if len(parts) == 4:
                    eclo_weeks_by_line[parts[1]].add(week)
        for line, weeks_set in sorted(eclo_weeks_by_line.items()):
            if max(weeks_set) - min(weeks_set) > 1:
                V(
                    "eclo_window",
                    f"line {line}: ECLO weeks {sorted(weeks_set)} span > 2 consecutive weeks",
                )

    # ---- RESULTS + scoring ------------------------------------------------ #
    res_by_contract = {_s(r.get("contract_number")): r for r in result_rows}
    if result_rows and len({_s(r.get("scenario")) for r in result_rows}) != 1:
        V("format", "RESULTS mixes scenarios")

    overrun_total = 0
    earliness_total = 0
    contracts_over = 0
    weighted = 0.0
    prio_over = {"1": 0, "2": 0, "3": 0}

    for contract, project in projects.items():
        ids = [
            a for a, row in activities.items()
            if _s(row.get("contract_number")) == contract and a in acc_by_act
        ]
        if not ids:
            continue
        end_week = max(_i(r.get("week"), "week", 0) for a in ids for r in acc_by_act[a])
        completion = week_end(end_week)
        planned = _date(project.get("planned_completion_date"), "planned_completion_date")
        over = max(0, (completion - planned).days)
        early = max(0, (planned - completion).days)

        row = res_by_contract.get(contract)
        if row is None:
            V("format", f"RESULTS missing {contract}")
        else:
            reported = _s(row.get("simulated_completion_date"))[:10]
            if reported != completion.isoformat():
                V("results", f"{contract} completion {reported} != computed {completion}")
            if _i(row.get("overrun_days"), "overrun_days", -1) != over:
                V(
                    "results",
                    f"{contract} overrun {_s(row.get('overrun_days'))} != computed {over}",
                )
        if scenario == "B" and over > 0:
            V("planned_date", f"{contract} overruns by {over} days in scenario B")

        overrun_total += over
        earliness_total += early
        contracts_over += 1 if over > 0 else 0
        tier = _s(project.get("contract_priority")) or "3"
        prio_over[tier] = prio_over.get(tier, 0) + over
        for a in ids:
            activity_end = max(_i(r.get("week"), "week", 0) for r in acc_by_act[a])
            activity_over = max(0, (week_end(activity_end) - planned).days)
            weight = CONTRACT_WEIGHT.get(tier, 1.0)
            nudge = ACTIVITY_NUDGE.get(_s(activities[a].get("activity_priority")), 0.0)
            weighted += weight * (1 + nudge) * activity_over

    eclo_total = sum(1 for r in access if _is_eclo(r.get("eclo")))
    if scenario == "A":
        objective = weighted
    elif scenario == "B":
        objective = 7 * excess_total + 5 * eclo_total
    else:
        objective = weighted + 7 * excess_total + 5 * eclo_total

    return {
        "scenario": scenario,
        "feasible": not viol,
        "hard_violations": [
            {"rule": rule, "severity": "hard", "detail": detail} for rule, detail in viol
        ],
        "soft_scores": {
            "overrun_days_total": overrun_total,
            "contracts_overrunning": contracts_over,
            "earliness_days_total": earliness_total,
            "excess_access_nights_total": excess_total,
            "eclo_nights_total": eclo_total,
            "priority_overrun": {k: prio_over.get(k, 0) for k in ("1", "2", "3")},
            "priority_weighted_score": round(weighted, 1),
            "objective_score": round(float(objective), 1),
        },
        "detail": {
            "capacity_hotspots": hotspots,
            "nights_scheduled": len(access),
            "eclo_nights": eclo_total,
        },
    }
