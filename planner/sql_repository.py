"""PostgreSQL persistence for the Supabase-backed planner.

The SQL repository deliberately exposes the same small interface as the CSV
repository.  The Streamlit domain code can therefore keep its validation,
merging, calendar, and coverage rules while the normalized database tables
remain free to evolve independently.
"""
from __future__ import annotations

import io
import os
import uuid
import zipfile
from datetime import date, datetime, timezone
from typing import Any, Mapping

from dotenv import load_dotenv
from sqlalchemy import URL, create_engine, text
from sqlalchemy.engine import Connection, Engine, URL as SQLAlchemyURL, make_url
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from planner.domain import ConflictError, KEYS, SCHEMAS, ValidationError, validate_state
from planner.repository import csv_text


def _env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is not None and value.strip():
            return value.strip()
    return default


def database_url_from_env() -> SQLAlchemyURL:
    """Build a safe SQLAlchemy URL from environment variables.

    ``URL.create`` handles passwords containing ``@``, ``%``, ``:`` and other
    reserved characters without requiring the user to hand-escape them.
    ``DATABASE_URL`` is also supported for deployments that provide one URL.
    Lower-case names from the original Supabase example are accepted too.
    """
    load_dotenv()
    configured = _env("DATABASE_URL")
    if configured:
        try:
            parsed = make_url(configured)
        except Exception as exc:  # pragma: no cover - SQLAlchemy gives varied parse errors
            raise ValidationError("DATABASE_URL is not a valid PostgreSQL connection URL.") from exc
        if parsed.drivername == "postgresql":
            parsed = parsed.set(drivername="postgresql+psycopg2")
        return parsed

    user = _env("DB_USER", "SUPABASE_DB_USER", "user", default="postgres")
    password = _env("DB_PASSWORD", "SUPABASE_DB_PASSWORD", "password")
    host = _env("DB_HOST", "SUPABASE_DB_HOST", "host")
    port = _env("DB_PORT", "SUPABASE_DB_PORT", "port", default="5432")
    database = _env("DB_NAME", "SUPABASE_DB_NAME", "dbname", default="postgres")
    sslmode = _env("DB_SSLMODE", default="require")
    if not password:
        raise ValidationError("Set DB_PASSWORD (or password) in .env before using Supabase.")
    if not host:
        raise ValidationError("Set DB_HOST (or host) in .env before using Supabase.")
    try:
        port_number = int(port or "5432")
    except ValueError as exc:
        raise ValidationError("DB_PORT must be a number, normally 5432.") from exc
    return URL.create(
        "postgresql+psycopg2",
        username=user,
        password=password,
        host=host,
        port=port_number,
        database=database,
        query={"sslmode": sslmode},
    )


def _date_value(value: Any) -> str:
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _timestamp_value(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def _clean_status(kind: str) -> str:
    return {"Planned": "planned", "Approved": "approved", "Cancelled": "cancelled"}.get(kind, kind.lower())


def _display_status(status: str) -> str:
    return {"planned": "Planned", "approved": "Approved", "cancelled": "Cancelled"}.get(status, status.title())


def _row_dict(row: Mapping[str, Any]) -> dict[str, Any]:
    return dict(row)


class SqlAlchemyRepository:
    """Repository backed by the private ``vacation`` schema in Supabase."""

    def __init__(self, engine: Engine, schema: str = "vacation"):
        if not schema.replace("_", "").isalnum() or not schema[0].isalpha():
            raise ValidationError("DB_SCHEMA must be a simple SQL identifier.")
        self.engine = engine
        self.schema = schema

    @classmethod
    def from_env(cls) -> "SqlAlchemyRepository":
        url = database_url_from_env()
        schema = _env("DB_SCHEMA", "SUPABASE_DB_SCHEMA", default="vacation") or "vacation"
        engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=int(_env("DB_POOL_SIZE", default="5") or "5"),
            max_overflow=int(_env("DB_MAX_OVERFLOW", default="2") or "2"),
            future=True,
        )
        return cls(engine, schema=schema)

    def _table(self, name: str) -> str:
        return f'"{self.schema}"."{name}"'

    def _raise_db(self, exc: Exception) -> None:
        if isinstance(exc, IntegrityError):
            raise ValidationError("The database rejected this change. Check the department, employee, dates, and required fields.") from exc
        if isinstance(exc, SQLAlchemyError):
            raise ValidationError(f"Database operation failed: {exc}") from exc
        raise exc

    def ping(self) -> dict[str, str]:
        try:
            with self.engine.connect() as conn:
                version = conn.execute(text("select current_database() as database, current_user as user, version() as version")).mappings().one()
                return {"database": str(version["database"]), "user": str(version["user"]), "version": str(version["version"])}
        except Exception as exc:
            self._raise_db(exc)
            raise AssertionError("unreachable")

    def _department_rows(self, conn: Connection) -> list[dict[str, str]]:
        rows = conn.execute(text(f"""
            SELECT name, minimum_at_work, version, updated_at
            FROM {self._table('departments')}
            WHERE active = true
            ORDER BY name
        """)).mappings()
        return [{
            "team": str(row["name"]),
            "minimum_at_work": str(row["minimum_at_work"]),
            "version": str(row["version"]),
            "updated_at": _timestamp_value(row["updated_at"]),
        } for row in rows]

    def _employee_rows(self, conn: Connection) -> list[dict[str, str]]:
        rows = conn.execute(text(f"""
            SELECT e.employee_id, e.full_name, d.name AS department,
                   e.active, e.version, e.updated_at
            FROM {self._table('employees')} e
            JOIN {self._table('departments')} d ON d.id = e.department_id
            ORDER BY d.name, e.full_name, e.employee_id
        """)).mappings()
        return [{
            "employee_id": str(row["employee_id"]),
            "name": str(row["full_name"]),
            "team": str(row["department"]),
            "active": str(bool(row["active"])).lower(),
            "version": str(row["version"]),
            "updated_at": _timestamp_value(row["updated_at"]),
        } for row in rows]

    def _range_rows(self, conn: Connection) -> list[dict[str, str]]:
        rows = conn.execute(text(f"""
            SELECT id AS range_id, employee_id, 'Available' AS kind,
                   start_date, end_date, note, version, updated_at
            FROM {self._table('availability_windows')}
            WHERE active = true
            UNION ALL
            SELECT id AS range_id, employee_id,
                   CASE status
                       WHEN 'planned' THEN 'Planned'
                       WHEN 'approved' THEN 'Approved'
                       ELSE 'Cancelled'
                   END AS kind,
                   start_date, end_date, reason AS note, version, updated_at
            FROM {self._table('leave_requests')}
            ORDER BY start_date, end_date, range_id
        """)).mappings()
        return [{
            "range_id": str(row["range_id"]),
            "employee_id": str(row["employee_id"]),
            "kind": str(row["kind"]),
            "start_date": _date_value(row["start_date"]),
            "end_date": _date_value(row["end_date"]),
            "note": str(row["note"] or ""),
            "version": str(row["version"]),
            "updated_at": _timestamp_value(row["updated_at"]),
        } for row in rows]

    def _holiday_rows(self, conn: Connection) -> list[dict[str, str]]:
        rows = conn.execute(text(f"""
            SELECT date, name, version, updated_at
            FROM {self._table('holidays')}
            ORDER BY date
        """)).mappings()
        return [{
            "date": _date_value(row["date"]),
            "name": str(row["name"]),
            "version": str(row["version"]),
            "updated_at": _timestamp_value(row["updated_at"]),
        } for row in rows]

    def snapshot(self) -> dict[str, list[dict[str, str]]]:
        try:
            with self.engine.connect() as conn:
                state = {
                    "teams": self._department_rows(conn),
                    "employees": self._employee_rows(conn),
                    "ranges": self._range_rows(conn),
                    "holidays": self._holiday_rows(conn),
                }
            validate_state(state)
            return state
        except ValidationError:
            raise
        except Exception as exc:
            self._raise_db(exc)
            raise AssertionError("unreachable")

    def _current_department(self, conn: Connection, name: str) -> Mapping[str, Any] | None:
        return conn.execute(text(f"""
            SELECT id, name, minimum_at_work, version, active
            FROM {self._table('departments')}
            WHERE name = :name
            FOR UPDATE
        """), {"name": name}).mappings().first()

    def _current_employee(self, conn: Connection, employee_id: str) -> Mapping[str, Any] | None:
        return conn.execute(text(f"""
            SELECT employee_id, full_name, department_id, active, version, updated_at
            FROM {self._table('employees')}
            WHERE employee_id = :employee_id
            FOR UPDATE
        """), {"employee_id": employee_id}).mappings().first()

    def _current_range(self, conn: Connection, range_id: str) -> Mapping[str, Any] | None:
        return conn.execute(text(f"""
            SELECT id AS range_id, employee_id, 'Available' AS kind,
                   start_date, end_date, note, version, updated_at, 'availability' AS source
            FROM {self._table('availability_windows')}
            WHERE id = :range_id
            UNION ALL
            SELECT id AS range_id, employee_id,
                   CASE status WHEN 'planned' THEN 'Planned' WHEN 'approved' THEN 'Approved' ELSE 'Cancelled' END AS kind,
                   start_date, end_date, reason AS note, version, updated_at, 'leave' AS source
            FROM {self._table('leave_requests')}
            WHERE id = :range_id
        """), {"range_id": range_id}).mappings().first()

    def _insert_membership(self, conn: Connection, employee_id: str, department_id: str) -> None:
        conn.execute(text(f"""
            INSERT INTO {self._table('team_memberships')}
                (employee_id, department_id, valid_from, primary_department)
            VALUES (:employee_id, :department_id, CURRENT_DATE, true)
            ON CONFLICT (employee_id, department_id, valid_from) DO UPDATE
                SET valid_to = NULL, primary_department = true
        """), {"employee_id": employee_id, "department_id": department_id})

    def _save_department(self, conn: Connection, values: dict[str, Any], expected_version: str | None) -> dict[str, str]:
        name = str(values.get("team", "")).strip()
        if not name:
            raise ValidationError("Department name is required.")
        try:
            minimum = int(str(values.get("minimum_at_work", "")).strip())
        except ValueError as exc:
            raise ValidationError("Minimum employees at work must be a non-negative whole number.") from exc
        if minimum < 0:
            raise ValidationError("Minimum employees at work must be a non-negative whole number.")
        current = self._current_department(conn, name)
        if current and expected_version != str(current["version"]):
            raise ConflictError("This department changed in another session. Refresh and try again.")
        if not current and expected_version is not None:
            raise ConflictError("This department no longer exists. Refresh and try again.")
        if current:
            conn.execute(text(f"""
                UPDATE {self._table('departments')}
                SET minimum_at_work = :minimum, active = true
                WHERE id = :id AND version = :version
            """), {"minimum": minimum, "id": current["id"], "version": int(current["version"])})
        else:
            conn.execute(text(f"""
                INSERT INTO {self._table('departments')} (id, name, minimum_at_work, active)
                VALUES (:id, :name, :minimum, true)
            """), {"id": uuid.uuid4().hex, "name": name, "minimum": minimum})
        saved = self._current_department(conn, name)
        return {
            "team": name,
            "minimum_at_work": str(saved["minimum_at_work"]),
            "version": str(saved["version"]),
            "updated_at": _timestamp_value(saved.get("updated_at", datetime.now(timezone.utc))),
        }

    def _save_employee(self, conn: Connection, values: dict[str, Any], expected_version: str | None) -> dict[str, str]:
        employee_id = str(values.get("employee_id", "")).strip()
        name = str(values.get("name", "")).strip()
        department = str(values.get("team", "")).strip()
        active_text = str(values.get("active", "true")).strip().lower()
        if not employee_id or not name or not department:
            raise ValidationError("Employee ID, name, and department are required.")
        if active_text not in ("true", "false"):
            raise ValidationError("Employee active must be true or false.")
        department_row = self._current_department(conn, department)
        if not department_row or not department_row["active"]:
            raise ValidationError("Choose an active department before saving the employee.")
        current = self._current_employee(conn, employee_id)
        if current and expected_version != str(current["version"]):
            raise ConflictError("This employee changed in another session. Refresh and try again.")
        if not current and expected_version is not None:
            raise ConflictError("This employee no longer exists. Refresh and try again.")
        active = active_text == "true"
        if current:
            conn.execute(text(f"""
                UPDATE {self._table('employees')}
                SET full_name = :name,
                    department_id = :department_id,
                    active = :active,
                    termination_date = CASE WHEN :active THEN NULL ELSE COALESCE(termination_date, CURRENT_DATE) END
                WHERE employee_id = :employee_id AND version = :version
            """), {
                "name": name, "department_id": department_row["id"], "active": active,
                "employee_id": employee_id, "version": int(current["version"]),
            })
        else:
            conn.execute(text(f"""
                INSERT INTO {self._table('employees')}
                    (employee_id, employee_code, full_name, department_id, active, termination_date)
                VALUES (:employee_id, :employee_code, :name, :department_id, :active,
                        CASE WHEN :active THEN NULL ELSE CURRENT_DATE END)
            """), {
                "employee_id": employee_id, "employee_code": employee_id, "name": name,
                "department_id": department_row["id"], "active": active,
            })
        self._insert_membership(conn, employee_id, department_row["id"])
        saved = self._current_employee(conn, employee_id)
        return {
            "employee_id": employee_id,
            "name": str(saved["full_name"]),
            "team": department,
            "active": str(bool(saved["active"])).lower(),
            "version": str(saved["version"]),
            "updated_at": _timestamp_value(saved["updated_at"]),
        }

    def _save_range(self, conn: Connection, values: dict[str, Any], expected_version: str | None) -> dict[str, str]:
        range_id = str(values.get("range_id", "")).strip() or uuid.uuid4().hex
        employee_id = str(values.get("employee_id", "")).strip()
        kind = str(values.get("kind", "")).strip()
        start_date = str(values.get("start_date", "")).strip()
        end_date = str(values.get("end_date", "")).strip()
        note = str(values.get("note", "")).strip()
        if kind not in ("Available", "Planned", "Approved", "Cancelled"):
            raise ValidationError("Invalid range kind.")
        try:
            start = date.fromisoformat(start_date)
            end = date.fromisoformat(end_date)
        except ValueError as exc:
            raise ValidationError("Range dates must use YYYY-MM-DD.") from exc
        if end < start:
            raise ValidationError("End date cannot precede start date.")
        employee = self._current_employee(conn, employee_id)
        if not employee:
            raise ValidationError(f"Unknown employee {employee_id!r}.")
        if not employee["active"] and kind != "Cancelled":
            raise ValidationError("Reactivate this employee before recording new leave or availability.")
        current = self._current_range(conn, range_id)
        if current and expected_version != str(current["version"]):
            raise ConflictError("This date range changed in another session. Refresh and try again.")
        if not current and expected_version is not None:
            raise ConflictError("This date range no longer exists. Refresh and try again.")
        source = "availability" if kind == "Available" else "leave"
        if current and current["source"] != source:
            if current["source"] == "availability":
                conn.execute(text(f"DELETE FROM {self._table('availability_windows')} WHERE id = :id"), {"id": range_id})
            else:
                conn.execute(text(f"DELETE FROM {self._table('leave_requests')} WHERE id = :id"), {"id": range_id})
            current = None
        if source == "availability":
            active = kind == "Available"
            if current:
                conn.execute(text(f"""
                    UPDATE {self._table('availability_windows')}
                    SET start_date = :start_date, end_date = :end_date, note = :note, active = :active
                    WHERE id = :id AND version = :version
                """), {"start_date": start, "end_date": end, "note": note, "active": active,
                      "id": range_id, "version": int(current["version"])})
            else:
                conn.execute(text(f"""
                    INSERT INTO {self._table('availability_windows')}
                        (id, employee_id, start_date, end_date, note, active)
                    VALUES (:id, :employee_id, :start_date, :end_date, :note, :active)
                """), {"id": range_id, "employee_id": employee_id, "start_date": start,
                      "end_date": end, "note": note, "active": active})
        else:
            status = _clean_status(kind)
            if current:
                conn.execute(text(f"""
                    UPDATE {self._table('leave_requests')}
                    SET start_date = :start_date, end_date = :end_date,
                        status = :status, reason = :reason
                    WHERE id = :id AND version = :version
                """), {"start_date": start, "end_date": end, "status": status, "reason": note,
                      "id": range_id, "version": int(current["version"])})
            else:
                conn.execute(text(f"""
                    INSERT INTO {self._table('leave_requests')}
                        (id, employee_id, leave_type_id, start_date, end_date, status, reason)
                    VALUES (:id, :employee_id, 'annual', :start_date, :end_date, :status, :reason)
                """), {"id": range_id, "employee_id": employee_id, "start_date": start,
                      "end_date": end, "status": status, "reason": note})
                conn.execute(text(f"""
                    INSERT INTO {self._table('leave_request_events')}
                        (id, leave_request_id, event_type, comment)
                    VALUES (:id, :request_id, :event_type, :comment)
                """), {"id": uuid.uuid4().hex, "request_id": range_id,
                      "event_type": f"{status}_created", "comment": note})
        saved = self._current_range(conn, range_id)
        return {
            "range_id": range_id,
            "employee_id": employee_id,
            "kind": str(saved["kind"]),
            "start_date": _date_value(saved["start_date"]),
            "end_date": _date_value(saved["end_date"]),
            "note": str(saved["note"] or ""),
            "version": str(saved["version"]),
            "updated_at": _timestamp_value(saved["updated_at"]),
        }

    def _save_holiday(self, conn: Connection, values: dict[str, Any], expected_version: str | None) -> dict[str, str]:
        day = str(values.get("date", "")).strip()
        name = str(values.get("name", "")).strip()
        if not day or not name:
            raise ValidationError("Holiday date and name are required.")
        try:
            parsed_day = date.fromisoformat(day)
        except ValueError as exc:
            raise ValidationError("Holiday date must use YYYY-MM-DD.") from exc
        current = conn.execute(text(f"""
            SELECT date, name, version, updated_at FROM {self._table('holidays')}
            WHERE date = :day FOR UPDATE
        """), {"day": parsed_day}).mappings().first()
        if current and expected_version != str(current["version"]):
            raise ConflictError("This holiday changed in another session. Refresh and try again.")
        if not current and expected_version is not None:
            raise ConflictError("This holiday no longer exists. Refresh and try again.")
        if current:
            conn.execute(text(f"""
                UPDATE {self._table('holidays')} SET name = :name
                WHERE date = :day AND version = :version
            """), {"name": name, "day": parsed_day, "version": int(current["version"])})
        else:
            conn.execute(text(f"INSERT INTO {self._table('holidays')} (date, name) VALUES (:day, :name)"),
                         {"day": parsed_day, "name": name})
        saved = conn.execute(text(f"SELECT date, name, version, updated_at FROM {self._table('holidays')} WHERE date = :day"),
                             {"day": parsed_day}).mappings().one()
        return {"date": _date_value(saved["date"]), "name": str(saved["name"]),
                "version": str(saved["version"]), "updated_at": _timestamp_value(saved["updated_at"])}

    def save(self, table: str, values: dict[str, Any], expected_version: str | None = None) -> dict[str, str]:
        if table not in SCHEMAS:
            raise ValidationError("Unknown table.")
        fields = [c for c in SCHEMAS[table] if c not in ("version", "updated_at")]
        if set(values) != set(fields):
            raise ValidationError(f"Expected fields: {', '.join(fields)}")
        try:
            with self.engine.begin() as conn:
                if table == "teams":
                    return self._save_department(conn, values, expected_version)
                if table == "employees":
                    return self._save_employee(conn, values, expected_version)
                if table == "ranges":
                    return self._save_range(conn, values, expected_version)
                return self._save_holiday(conn, values, expected_version)
        except (ValidationError, ConflictError):
            raise
        except Exception as exc:
            self._raise_db(exc)
            raise AssertionError("unreachable")

    def delete_holiday(self, day: str, expected_version: str) -> None:
        try:
            with self.engine.begin() as conn:
                result = conn.execute(text(f"""
                    DELETE FROM {self._table('holidays')}
                    WHERE date = :day AND version = :version
                """), {"day": date.fromisoformat(day), "version": int(expected_version)})
                if result.rowcount != 1:
                    raise ConflictError("Holiday changed in another session. Refresh and try again.")
        except (ValidationError, ConflictError):
            raise
        except Exception as exc:
            self._raise_db(exc)
            raise AssertionError("unreachable")

    def import_ranges(self, incoming: list[dict]) -> tuple[int, int]:
        """Append a batch atomically, skipping exact duplicates."""
        business = ("employee_id", "kind", "start_date", "end_date", "note")
        try:
            with self.engine.begin() as conn:
                existing = self._range_rows(conn)
                by_id = {r["range_id"]: r for r in existing}
                signatures = {tuple(r[k] for k in business) for r in existing}
                added = skipped = 0
                for item in incoming:
                    row = {key: str(item.get(key, "")).strip() for key in business}
                    row["range_id"] = str(item.get("range_id", "")).strip() or uuid.uuid4().hex
                    signature = tuple(row[k] for k in business)
                    old = by_id.get(row["range_id"])
                    if old and tuple(old[k] for k in business) != signature:
                        raise ConflictError(f"Range ID {row['range_id']!r} has different content. Edit that record in the app.")
                    if signature in signatures:
                        skipped += 1
                        continue
                    self._save_range(conn, row, None)
                    signatures.add(signature)
                    added += 1
                return added, skipped
        except (ValidationError, ConflictError):
            raise
        except Exception as exc:
            self._raise_db(exc)
            raise AssertionError("unreachable")

    def backup_zip(self) -> bytes:
        state = self.snapshot()
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
            for table, rows in state.items():
                if table in SCHEMAS:
                    archive.writestr(f"data/{table}.csv", csv_text(rows, SCHEMAS[table]))
        return output.getvalue()

