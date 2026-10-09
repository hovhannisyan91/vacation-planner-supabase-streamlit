"""CSV persistence. Lock the complete read/validate/write operation."""
from __future__ import annotations

import csv
import io
import os
import shutil
import tempfile
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from filelock import FileLock

from planner.domain import ConflictError, KEYS, SCHEMAS, ValidationError, validate_state


def csv_text(rows: list[dict], columns: tuple | list) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


class CsvRepository:
    def __init__(self, directory: str | Path):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = FileLock(str(self.directory / ".planner.lock"), timeout=10)
        with self.lock:
            for table in SCHEMAS:
                if not (self.directory / f"{table}.csv").exists():
                    self._write(table, [], backup=False)

    def _read(self) -> dict:
        state = {}
        for table, fields in SCHEMAS.items():
            try:
                with (self.directory / f"{table}.csv").open(encoding="utf-8-sig", newline="") as handle:
                    reader = csv.DictReader(handle, strict=True)
                    if reader.fieldnames != list(fields):
                        raise ValidationError(f"{table}.csv must have these columns in order: {', '.join(fields)}")
                    state[table] = list(reader)
            except (csv.Error, UnicodeError) as exc:
                raise ValidationError(f"{table}.csv must contain valid UTF-8 CSV data.") from exc
        validate_state(state)
        return state

    def snapshot(self) -> dict:
        with self.lock:
            return self._read()

    def _write(self, table: str, rows: list[dict], backup: bool = True) -> None:
        path = self.directory / f"{table}.csv"
        if backup and path.exists():
            backups = self.directory / "backups"
            backups.mkdir(exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            shutil.copy2(path, backups / f"{table}-{stamp}-{uuid.uuid4().hex[:8]}.csv")
        fd, name = tempfile.mkstemp(prefix=f".{table}-", suffix=".tmp", dir=self.directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(csv_text(rows, SCHEMAS[table]))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def save(self, table: str, values: dict, expected_version: str | None = None) -> dict:
        if table not in SCHEMAS:
            raise ValidationError("Unknown table.")
        fields = [c for c in SCHEMAS[table] if c not in ("version", "updated_at")]
        if set(values) != set(fields):
            raise ValidationError(f"Expected fields: {', '.join(fields)}")
        row = {key: str(values[key]).strip() for key in fields}
        with self.lock:
            state = self._read()
            key = KEYS[table]
            existing = next((r for r in state[table] if r[key] == row[key]), None)
            if existing and expected_version != existing["version"]:
                raise ConflictError("This record already exists or changed in another session. Refresh and try again.")
            if not existing and expected_version is not None:
                raise ConflictError("This record no longer exists. Refresh and try again.")
            if table == "ranges" and row["kind"] != "Cancelled":
                employee = next((e for e in state["employees"] if e["employee_id"] == row["employee_id"]), None)
                if employee is not None and employee["active"] != "true":
                    raise ValidationError("Reactivate this employee before recording new leave or availability.")
            row.update(version=str(int(existing["version"]) + 1 if existing else 1),
                       updated_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"))
            state[table] = [row if r[key] == row[key] else r for r in state[table]] if existing else state[table] + [row]
            validate_state(state)
            self._write(table, state[table])
            return row

    def delete_holiday(self, day: str, expected_version: str) -> None:
        with self.lock:
            state = self._read()
            row = next((r for r in state["holidays"] if r["date"] == day), None)
            if not row or row["version"] != expected_version:
                raise ConflictError("Holiday changed in another session. Refresh and try again.")
            self._write("holidays", [r for r in state["holidays"] if r["date"] != day])

    def delete_employee_permanently(self, employee_id: str, expected_version: str) -> None:
        """Permanently remove a deactivated employee and all their date ranges."""
        with self.lock:
            state = self._read()
            employee = next((r for r in state["employees"] if r["employee_id"] == employee_id), None)
            if not employee or employee["version"] != expected_version:
                raise ConflictError("Employee changed in another session. Refresh and try again.")
            if employee["active"] == "true":
                raise ValidationError("Deactivate the employee before permanently deleting them.")

            changed = {
                "ranges": [r for r in state["ranges"] if r["employee_id"] != employee_id],
                "employees": [r for r in state["employees"] if r["employee_id"] != employee_id],
            }
            originals = {table: (self.directory / f"{table}.csv").read_bytes() for table in changed}
            replaced = []
            try:
                for table, rows in changed.items():
                    self._write(table, rows)
                    replaced.append(table)
            except Exception:
                for table in reversed(replaced):
                    fd, name = tempfile.mkstemp(prefix=f".{table}-restore-", suffix=".tmp", dir=self.directory)
                    try:
                        with os.fdopen(fd, "wb") as handle:
                            handle.write(originals[table])
                            handle.flush()
                            os.fsync(handle.fileno())
                        os.replace(name, self.directory / f"{table}.csv")
                    finally:
                        if os.path.exists(name):
                            os.unlink(name)
                raise

    def import_ranges(self, incoming: list[dict]) -> tuple[int, int]:
        """Append a whole batch or nothing. Skip exact repeats; reject ID conflicts."""
        with self.lock:
            state = self._read()
            rows = state["ranges"]
            by_id = {r["range_id"]: r for r in rows}
            business = ("employee_id", "kind", "start_date", "end_date", "note")
            signature = lambda row: tuple(row.get(k, "").strip() for k in business)
            seen = {signature(row) for row in rows}
            added = skipped = 0
            for item in incoming:
                row = {key: str(item.get(key, "")).strip() for key in business}
                row["range_id"] = str(item.get("range_id", "")).strip() or uuid.uuid4().hex
                old = by_id.get(row["range_id"])
                if old and signature(old) != signature(row):
                    raise ConflictError(f"Range ID {row['range_id']!r} has different content. Edit that record in the app.")
                if signature(row) in seen:
                    skipped += 1
                    continue
                employee = next((e for e in state["employees"] if e["employee_id"] == row["employee_id"]), None)
                if employee is not None and employee["active"] != "true" and row["kind"] != "Cancelled":
                    raise ValidationError(f"Employee {row['employee_id']} is inactive.")
                row.update(version="1", updated_at=datetime.now(timezone.utc).isoformat(timespec="microseconds"))
                rows.append(row)
                by_id[row["range_id"]] = row
                seen.add(signature(row))
                added += 1
            validate_state(state)
            if added:
                self._write("ranges", rows)
            return added, skipped

    def backup_zip(self) -> bytes:
        with self.lock:
            state = self._read()
            output = io.BytesIO()
            with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
                for table, rows in state.items():
                    archive.writestr(f"data/{table}.csv", csv_text(rows, SCHEMAS[table]))
            return output.getvalue()


def parse_import(content: bytes) -> list[dict]:
    if len(content) > 2_000_000:
        raise ValidationError("Use a CSV smaller than 2 MB.")
    try:
        reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig"), newline=""), strict=True)
        required = {"employee_id", "kind", "start_date", "end_date"}
        allowed = set(SCHEMAS["ranges"])
        fields = reader.fieldnames or []
        if len(fields) != len(set(fields)) or not required.issubset(fields) or not set(fields).issubset(allowed):
            raise ValidationError("Use the range CSV template, including employee_id, kind, start_date and end_date.")
        rows = list(reader)
        if len(rows) > 10_000:
            raise ValidationError("Import at most 10,000 rows per file.")
        if any(None in row or any(v is None for v in row.values()) for row in rows):
            raise ValidationError("A CSV row has a missing or extra field.")
        return rows
    except (UnicodeError, csv.Error) as exc:
        raise ValidationError("Use a valid UTF-8, comma-separated CSV file.") from exc
