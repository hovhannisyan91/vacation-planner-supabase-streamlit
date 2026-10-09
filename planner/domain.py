"""Pure planning rules. No Streamlit or storage dependencies."""
from __future__ import annotations

import calendar
from collections import defaultdict
from datetime import date, timedelta
from typing import Iterable

KINDS = ("Available", "Planned", "Approved", "Cancelled")
UNASSIGNED_TEAM = "Unassigned (DMC Department)"
SCHEMAS = {
    "teams": ("team", "minimum_at_work", "version", "updated_at"),
    "employees": ("employee_id", "name", "team", "active", "version", "updated_at"),
    "ranges": ("range_id", "employee_id", "kind", "start_date", "end_date", "note", "version", "updated_at"),
    "holidays": ("date", "name", "version", "updated_at"),
}
KEYS = {table: fields[0] for table, fields in SCHEMAS.items()}


class ValidationError(ValueError):
    pass


class ConflictError(ValidationError):
    pass


def iso_date(value: str) -> date:
    try:
        result = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"Invalid date {value!r}. Use YYYY-MM-DD.") from exc
    if result.isoformat() != value:
        raise ValidationError(f"Invalid date {value!r}. Use YYYY-MM-DD.")
    return result


def validate_state(state: dict[str, list[dict]]) -> None:
    for table, fields in SCHEMAS.items():
        seen = set()
        for row in state[table]:
            if set(row) != set(fields) or any(not isinstance(v, str) for v in row.values()):
                raise ValidationError(f"Invalid columns or values in {table}.csv.")
            key = row[KEYS[table]]
            if not key or key in seen:
                raise ValidationError(f"Missing or duplicate key {key!r} in {table}.csv.")
            seen.add(key)
            if not row["version"].isdigit() or int(row["version"]) < 1:
                raise ValidationError(f"Invalid version for {key!r}.")
    teams = {r["team"] for r in state["teams"]}
    employees = {r["employee_id"] for r in state["employees"]}
    for row in state["teams"]:
        if not row["minimum_at_work"].isdigit():
            raise ValidationError("Minimum at work must be a non-negative whole number.")
    for row in state["employees"]:
        if not row["name"] or (row["team"] not in teams and row["team"] != UNASSIGNED_TEAM):
            raise ValidationError(f"Employee {row['employee_id']} needs a name and an existing team.")
        if row["active"] not in ("true", "false"):
            raise ValidationError("Employee active must be true or false.")
    for row in state["ranges"]:
        if row["employee_id"] not in employees:
            raise ValidationError(f"Unknown employee {row['employee_id']!r}.")
        if row["kind"] not in KINDS:
            raise ValidationError(f"Invalid range kind {row['kind']!r}.")
        if iso_date(row["end_date"]) < iso_date(row["start_date"]):
            raise ValidationError(f"End date precedes start date for {row['range_id']}.")
    for row in state["holidays"]:
        iso_date(row["date"])
        if not row["name"]:
            raise ValidationError("A holiday needs a name.")


def merge_ranges(rows: Iterable[dict]) -> list[dict]:
    """Union intervals within each employee/kind; retain contributing source IDs."""
    grouped = defaultdict(list)
    for row in rows:
        if row["kind"] != "Cancelled":
            grouped[(row["employee_id"], row["kind"])].append(row)
    result = []
    for (employee_id, kind), group in sorted(grouped.items()):
        current = None
        for row in sorted(group, key=lambda r: (r["start_date"], r["end_date"], r["range_id"])):
            start, end = iso_date(row["start_date"]), iso_date(row["end_date"])
            if current is not None and (start - current["end"]).days <= 1:
                current["end"] = max(current["end"], end)
                current["source_ids"].append(row["range_id"])
            else:
                current = {"employee_id": employee_id, "kind": kind, "start": start,
                           "end": end, "source_ids": [row["range_id"]]}
                result.append(current)
    return result


def working_days(start: date, end: date, holidays: set[date]) -> int:
    if end < start:
        return 0
    weeks, remainder = divmod((end - start).days + 1, 7)
    weekdays = weeks * 5 + sum((start.weekday() + n) % 7 < 5 for n in range(remainder))
    return weekdays - sum(start <= h <= end and h.weekday() < 5 for h in holidays)


def availability_message(row: dict, merged: list[dict]) -> str:
    if row["kind"] in ("Available", "Cancelled"):
        return ""
    windows = [r for r in merged if r["employee_id"] == row["employee_id"] and r["kind"] == "Available"]
    if not windows:
        return "No available dates recorded"
    start, end = iso_date(row["start_date"]), iso_date(row["end_date"])
    if any(w["start"] <= start and w["end"] >= end for w in windows):
        return ""
    return "Outside available dates"


def month_view(state: dict, year: int, month: int, include_planned: bool = True) -> dict:
    days = [date(year, month, n) for n in range(1, calendar.monthrange(year, month)[1] + 1)]
    holidays = {iso_date(r["date"]) for r in state["holidays"]}
    merged = merge_ranges(state["ranges"])
    by_employee = defaultdict(list)
    for r in merged:
        by_employee[r["employee_id"]].append(r)
    people, codes, warnings = [], {}, []
    for employee in sorted(state["employees"], key=lambda r: (r["team"], r["name"], r["employee_id"])):
        if employee["active"] != "true":
            continue
        employee_codes = {}
        for day in days:
            kinds = {r["kind"] for r in by_employee[employee["employee_id"]] if r["start"] <= day <= r["end"]}
            code = ("H" if day in holidays else "W" if day.weekday() >= 5 else
                    "A" if "Approved" in kinds else "P" if "Planned" in kinds else
                    "V" if "Available" in kinds else "")
            employee_codes[day] = code
        codes[employee["employee_id"]] = employee_codes
        people.append({**employee, "approved_days": sum(c == "A" for c in employee_codes.values()),
                       "planned_days": sum(c == "P" for c in employee_codes.values())})
    coverage = []
    absent_codes = {"A", "P"} if include_planned else {"A"}
    for team in state["teams"]:
        members = [e for e in people if e["team"] == team["team"]]
        for day in days:
            if day.weekday() >= 5 or day in holidays:
                continue
            absent = [e["name"] for e in members if codes[e["employee_id"]][day] in absent_codes]
            present = len(members) - len(absent)
            coverage.append({"date": day.isoformat(), "team": team["team"], "present": present,
                             "minimum": int(team["minimum_at_work"]), "absent": ", ".join(absent),
                             "below_minimum": present < int(team["minimum_at_work"])})
    active_ids = set(codes)
    for row in state["ranges"]:
        if row["employee_id"] in active_ids and iso_date(row["start_date"]) <= days[-1] and iso_date(row["end_date"]) >= days[0]:
            message = availability_message(row, merged)
            if message:
                warnings.append({"range_id": row["range_id"], "employee_id": row["employee_id"], "warning": message})
    return {"days": days, "people": people, "codes": codes, "coverage": coverage,
            "warnings": warnings, "merged": merged, "holidays": holidays}
