"""Run with: uv run streamlit run app.py"""
from __future__ import annotations

import html
import os
import uuid
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv
from filelock import Timeout

from planner.domain import KEYS, KINDS, SCHEMAS, ValidationError, availability_message, iso_date, merge_ranges, month_view, working_days
from planner.repository import CsvRepository, csv_text, parse_import
from planner.sql_repository import SqlAlchemyRepository

st.set_page_config(page_title="Team vacation planner", page_icon="📅", layout="wide")
st.markdown("""<style>
.block-container{padding-top:2rem;max-width:1500px}
h1{letter-spacing:-.035em} [data-testid="stMetric"]{background:#f2f5f8;padding:16px;border-radius:10px}
.calendar{overflow-x:auto;border:1px solid #dbe2e9;border-radius:10px;margin:12px 0 8px}
.calendar table{border-collapse:collapse;white-space:nowrap;width:100%;font-size:13px}
.calendar th{background:#274764;color:white;padding:10px 8px;font-weight:600;text-align:center}
.calendar td{padding:11px 9px;text-align:center;border-bottom:1px solid #e7edf2}
.calendar td.name{text-align:left;min-width:180px;background:#f7f9fb}
.calendar .A{background:#cfe7d8;color:#225c39}.calendar .P{background:#dce9fb;color:#23558c}
.calendar .V{background:#e2f2ef;color:#28796d}.calendar .W,.calendar .H{background:#eceff3;color:#6d7889}
</style>""", unsafe_allow_html=True)

root = Path(__file__).parent
load_dotenv()
configured_backend = os.environ.get("DATA_BACKEND", "").strip().lower()
if not configured_backend:
    configured_backend = "supabase" if os.environ.get("DATABASE_URL") or os.environ.get("DB_PASSWORD") or os.environ.get("password") else "csv"
try:
    if configured_backend in {"supabase", "postgres", "postgresql"}:
        repo = SqlAlchemyRepository.from_env()
        backend_label = "Supabase/Postgres"
    else:
        repo = CsvRepository(os.environ.get("VACATION_DATA_DIR", str(root / "data")))
        backend_label = "CSV fallback"
    state = repo.snapshot()
except (ValidationError, OSError, Timeout) as exc:
    st.error(f"Could not read planner data: {exc}")
    st.stop()
except Exception as exc:
    st.error(f"Could not connect to the configured {configured_backend} backend: {exc}")
    st.stop()

if "flash" in st.session_state:
    st.success(st.session_state.pop("flash"))


def save_action(action, message):
    try:
        action()
    except (ValidationError, OSError, Timeout) as exc:
        st.error(str(exc))
        return
    st.session_state["flash"] = message
    for key in list(st.session_state):
        if key.startswith("edit_baseline_"):
            del st.session_state[key]
    st.rerun()


def edit_baseline(table, row):
    """Keep the version the user originally opened until save or explicit refresh."""
    if row is None:
        return None
    record_key = row[KEYS[table]]
    key = f"edit_baseline_{table}_{record_key}"
    if key not in st.session_state:
        st.session_state[key] = dict(row)
    baseline = st.session_state[key]
    if baseline["version"] != row["version"]:
        st.warning("This record changed in another session. Your edit cannot overwrite it. Refresh data and review the latest values.")
    return baseline


def display_rows(rows, columns=None):
    if rows:
        st.dataframe(pd.DataFrame(rows, columns=columns), hide_index=True, width="stretch")
    else:
        st.info("No records yet.")


def calendar_html(view):
    text = '<div class="calendar"><table><thead><tr><th>Employee</th><th>Team</th><th>A</th><th>P</th>'
    text += "".join(f"<th>{d.day}<br>{d:%a}</th>" for d in view["days"])
    text += "</tr></thead><tbody>"
    for employee in view["people"]:
        text += f'<tr><td class="name">{html.escape(employee["name"])}<br><small>{html.escape(employee["employee_id"])}</small></td>'
        text += f'<td>{html.escape(employee["team"])}</td><td>{employee["approved_days"]}</td><td>{employee["planned_days"]}</td>'
        for day in view["days"]:
            code = view["codes"][employee["employee_id"]][day]
            text += f'<td class="{code}">{code or "·"}</td>'
        text += "</tr>"
    return text + "</tbody></table></div>"


st.sidebar.title("Vacation planner")
page = st.sidebar.radio("Workspace", ["Calendar", "Date ranges", "Employees & teams", "Holidays", "Import & export"], key="page")
st.sidebar.caption(f"Backend: {backend_label}. Everyone with access can edit and approve leave.")
if st.sidebar.button("Refresh data", width="stretch"):
    for key in list(st.session_state):
        if key.startswith("edit_baseline_"):
            del st.session_state[key]
    st.rerun()

names = {e["employee_id"]: f"{e['name']} ({e['employee_id']})" for e in state["employees"]}
active_ids = [e["employee_id"] for e in state["employees"] if e["active"] == "true"]
merged = merge_ranges(state["ranges"])

if page == "Calendar":
    st.title("Team availability")
    st.caption("Plan vacation dates and check who remains at work.")
    c1, c2, c3 = st.columns([1, 1, 2])
    selected = c1.date_input("Month", value=date.today().replace(day=1), key="month")
    team = c2.selectbox("Team", ["All teams"] + [r["team"] for r in state["teams"]])
    include_planned = c3.checkbox("Include planned leave in staffing warnings", value=True)
    view = month_view(state, selected.year, selected.month, include_planned)
    if team != "All teams":
        view["people"] = [r for r in view["people"] if r["team"] == team]
        view["coverage"] = [r for r in view["coverage"] if r["team"] == team]
        ids = {r["employee_id"] for r in view["people"]}
        view["warnings"] = [r for r in view["warnings"] if r["employee_id"] in ids]
    gaps = [r for r in view["coverage"] if r["below_minimum"]]
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Active employees", len(view["people"]))
    m2.metric("Approved workdays", sum(e["approved_days"] for e in view["people"]))
    m3.metric("Planned workdays", sum(e["planned_days"] for e in view["people"]))
    m4.metric("Team-days below minimum", len(gaps))
    st.subheader(selected.strftime("%B %Y"))
    if view["people"]:
        st.markdown(calendar_html(view), unsafe_allow_html=True)
        st.caption("A approved · P planned · V available for vacation · W weekend · H holiday · dot no range recorded. Approved takes precedence over Planned.")
    else:
        st.info("Add a team and an active employee to start planning.")
    st.subheader("Coverage to review")
    if gaps:
        display_rows(gaps, ["date", "team", "present", "minimum", "absent"])
    else:
        st.success("No staffing gaps for the selected month and teams.")
    if view["warnings"]:
        st.warning("Some leave requests fall outside the employee's available dates, or have no available dates recorded.")
        display_rows(view["warnings"])
    with st.expander("Daily staffing"):
        display_rows(view["coverage"], ["date", "team", "present", "minimum", "absent"])

elif page == "Date ranges":
    st.title("Available dates & leave")
    st.caption("Available means dates when an employee can take vacation. All ranges include both start and end dates.")
    if not names:
        st.info("Add employees and teams first.")
        st.stop()
    mode = st.radio("Action", ["Add range", "Edit range"], horizontal=True, key="range_mode")
    existing = None
    if mode == "Edit range":
        if not state["ranges"]:
            st.info("There are no ranges to edit.")
            st.stop()
        range_records = {r["range_id"]: r for r in state["ranges"]}
        range_labels = {key: f"{names[r['employee_id']]} · {r['kind']} · {r['start_date']} to {r['end_date']} · {key[:8]}" for key, r in range_records.items()}
        chosen = st.selectbox("Range", list(range_records), format_func=range_labels.get, key="range_to_edit")
        existing = edit_baseline("ranges", range_records[chosen])
    eligible = list(names) if existing else active_ids
    if not eligible:
        st.info("Activate an employee before adding a range.")
        st.stop()
    form_key = f"range_{existing['range_id']}_{existing['version']}" if existing else "new_range"
    with st.form(form_key):
        c1, c2 = st.columns(2)
        employee_id = c1.selectbox("Employee", eligible, format_func=names.get, index=eligible.index(existing["employee_id"]) if existing else 0)
        kind = c2.selectbox("Range kind", KINDS, index=KINDS.index(existing["kind"]) if existing else 1)
        c1, c2 = st.columns(2)
        start = c1.date_input("Start date", value=iso_date(existing["start_date"]) if existing else date.today())
        end = c2.date_input("End date", value=iso_date(existing["end_date"]) if existing else date.today())
        note = st.text_input("Note", value=existing["note"] if existing else "", max_chars=1000)
        submitted = st.form_submit_button("Save range", type="primary")
    if submitted:
        values = dict(range_id=existing["range_id"] if existing else uuid.uuid4().hex, employee_id=employee_id,
                      kind=kind, start_date=start.isoformat(), end_date=end.isoformat(), note=note)
        save_action(lambda: repo.save("ranges", values, existing["version"] if existing else None), "Date range saved.")
    st.caption("Set a range to Approved to confirm it, or Cancelled to remove it from planning. Warnings prompt review and do not automatically reject requests.")
    st.subheader("Original ranges")
    records = [{**{k: r[k] for k in ("range_id", "employee_id", "kind", "start_date", "end_date", "note")},
                "availability": availability_message(r, merged) or "—"} for r in state["ranges"]]
    display_rows(records)
    st.subheader("Merged periods")
    st.caption("Overlapping and consecutive calendar dates merge within the same employee and kind. Original rows stay intact.")
    holidays = {iso_date(r["date"]) for r in state["holidays"]}
    display_rows([dict(employee=names[r["employee_id"]], kind=r["kind"], start=r["start"], end=r["end"],
                       workdays=working_days(r["start"], r["end"], holidays), source_rows=len(r["source_ids"])) for r in merged])

elif page == "Employees & teams":
    st.title("Employees & teams")
    team_tab, people_tab = st.tabs(["Departments", "Employees"])
    with team_tab:
        team_names = [r["team"] for r in state["teams"]]
        selected = st.selectbox("Department to update", ["Add a department"] + team_names)
        current = edit_baseline("teams", next((r for r in state["teams"] if r["team"] == selected), None))
        with st.form(f"team_{selected}_{current['version'] if current else 'new'}"):
            team_name = st.text_input("Department name", value=current["team"] if current else "", disabled=bool(current), max_chars=80)
            minimum = st.number_input("Minimum employees at work", min_value=0, value=int(current["minimum_at_work"]) if current else 1, step=1)
            submitted = st.form_submit_button("Save department", type="primary")
        if submitted:
            save_action(lambda: repo.save("teams", dict(team=team_name, minimum_at_work=str(minimum)), current["version"] if current else None), "Department saved.")
        st.caption("The initial departments are DMC, Data Analytics and Engineering, Data Governance, and ML. You can add another department here.")
        display_rows(state["teams"], ["department", "minimum_at_work"])
    with people_tab:
        if not team_names:
            st.info("Create a department first.")
        else:
            selected_employee = st.selectbox("Employee to update", ["Add an employee"] + list(names), format_func=lambda value: names.get(value, value))
            current = edit_baseline("employees", next((r for r in state["employees"] if r["employee_id"] == selected_employee), None))
            with st.form(f"employee_{selected_employee}_{current['version'] if current else 'new'}"):
                c1, c2 = st.columns(2)
                employee_id = c1.text_input("Employee ID", value=current["employee_id"] if current else "", disabled=bool(current), max_chars=40)
                name = c2.text_input("Employee name", value=current["name"] if current else "", max_chars=100)
                team_name = st.selectbox("Employee department", team_names, index=team_names.index(current["team"]) if current else 0)
                active = st.checkbox("Active employee", value=current["active"] == "true" if current else True)
                submitted = st.form_submit_button("Add employee" if current is None else "Save employee", type="primary")
            if submitted:
                save_action(lambda: repo.save("employees", dict(employee_id=employee_id, name=name, team=team_name, active=str(active).lower()), current["version"] if current else None), "Employee saved.")
            if current:
                if current["active"] == "true":
                    if st.button("Remove employee (deactivate)", key=f"remove_employee_{current['employee_id']}"):
                        values = {"employee_id": current["employee_id"], "name": current["name"], "team": current["team"], "active": "false"}
                        save_action(lambda: repo.save("employees", values, current["version"]), "Employee removed from the active roster. History was preserved.")
                else:
                    if st.button("Reactivate employee", key=f"reactivate_employee_{current['employee_id']}"):
                        values = {"employee_id": current["employee_id"], "name": current["name"], "team": current["team"], "active": "true"}
                        save_action(lambda: repo.save("employees", values, current["version"]), "Employee reactivated.")
                st.caption("Remove deactivates the employee instead of deleting leave history. Reactivate them from this same control.")
            display_rows(state["employees"], ["employee_id", "name", "team", "active"])

elif page == "Holidays":
    st.title("Holidays")
    st.caption("Workdays are Monday–Friday, excluding the holidays below. No official holiday calendar is preloaded.")
    with st.form("holiday"):
        c1, c2 = st.columns(2)
        day = c1.date_input("Holiday date")
        name = c2.text_input("Holiday name", max_chars=100)
        submitted = st.form_submit_button("Add holiday", type="primary")
    if submitted:
        save_action(lambda: repo.save("holidays", dict(date=day.isoformat(), name=name)), "Holiday added.")
    display_rows(state["holidays"], ["date", "name"])
    if state["holidays"]:
        selected = st.selectbox("Holiday to remove", [r["date"] for r in state["holidays"]])
        current = next(r for r in state["holidays"] if r["date"] == selected)
        if st.button("Remove selected holiday"):
            save_action(lambda: repo.delete_holiday(selected, current["version"]), "Holiday removed.")

elif page == "Import & export":
    st.title("Import & export")
    st.subheader("Merge ranges from a CSV")
    st.caption("Create employees first. Imports append new rows, skip exact duplicates and reject conflicting range IDs. The entire file is validated before saving.")
    template = "employee_id,kind,start_date,end_date,note\n"
    st.download_button("Download range template", template, "range_template.csv", "text/csv")
    uploaded = st.file_uploader("Range CSV", type=["csv"])
    if uploaded is not None:
        try:
            rows = parse_import(uploaded.getvalue())
            display_rows(rows)
            if any(r["kind"] == "Approved" for r in rows):
                st.info("This file contains Approved leave. Importing it records those approvals.")
            if st.button("Merge imported ranges", type="primary"):
                added, skipped = repo.import_ranges(rows)
                st.session_state["flash"] = f"Imported {added} ranges. Skipped {skipped} exact duplicates."
                st.rerun()
        except (ValidationError, OSError, Timeout) as exc:
            st.error(str(exc))
    st.subheader("Download current data")
    st.download_button("Download all CSV files", repo.backup_zip(), "vacation_data.zip", "application/zip")
    for table, fields in SCHEMAS.items():
        st.download_button(f"Download {table}.csv", csv_text(state[table], fields), f"{table}.csv", "text/csv", key=f"export_{table}")
    st.caption("CSV files are the saved data. Keep a backup before editing files outside the app, and stop the app while making those edits.")
