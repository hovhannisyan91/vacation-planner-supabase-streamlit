import io
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from unittest.mock import patch

from planner.domain import ConflictError, ValidationError, availability_message, merge_ranges, month_view
from planner.repository import CsvRepository, parse_import


def period(key, employee="E1", kind="Approved", start="2026-10-12", end="2026-10-14", note=""):
    return dict(range_id=key, employee_id=employee, kind=kind, start_date=start, end_date=end, note=note)


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = CsvRepository(self.temp.name)
        self.repo.save("teams", dict(team="Data", minimum_at_work="1"))
        for key in ("E1", "E2"):
            self.repo.save("employees", dict(employee_id=key, name=key, team="Data", active="true"))

    def tearDown(self):
        self.temp.cleanup()

    def test_union_is_transitive_sorted_and_status_specific(self):
        rows = [period("3", start="2026-10-17", end="2026-10-19"), period("1"),
                period("2", start="2026-10-14", end="2026-10-16"), period("4"),
                period("5", kind="Planned"), period("6", employee="E2"), period("7", kind="Cancelled")]
        result = merge_ranges(rows)
        approved = next(r for r in result if r["employee_id"] == "E1" and r["kind"] == "Approved")
        self.assertEqual((approved["start"], approved["end"]), (date(2026, 10, 12), date(2026, 10, 19)))
        self.assertEqual(len(approved["source_ids"]), 4)
        self.assertEqual(len(result), 3)

    def test_available_union_covers_request(self):
        merged = merge_ranges([period("a", kind="Available", start="2026-10-01", end="2026-10-15"),
                               period("b", kind="Available", start="2026-10-16", end="2026-10-31")])
        self.assertEqual(availability_message(period("c", start="2026-10-12", end="2026-10-20"), merged), "")
        self.assertEqual(availability_message(period("d", employee="E2"), merged), "No available dates recorded")
        self.assertEqual(availability_message(period("e", end="2026-11-01"), merged), "Outside available dates")

    def test_calendar_counts_people_once_and_excludes_holidays(self):
        self.repo.import_ranges([period("1"), period("2", note="overlap"),
                                 period("3", kind="Planned", end="2026-10-16"),
                                 period("4", employee="E2", kind="Planned", start="2026-10-14", end="2026-10-14")])
        state = self.repo.snapshot()
        view = month_view(state, 2026, 10)
        e1 = next(e for e in view["people"] if e["employee_id"] == "E1")
        self.assertEqual((e1["approved_days"], e1["planned_days"]), (3, 2))
        self.assertEqual(len([r for r in view["coverage"] if r["below_minimum"]]), 1)
        self.assertFalse(any(r["below_minimum"] for r in month_view(state, 2026, 10, False)["coverage"]))
        self.repo.save("holidays", dict(date="2026-10-14", name="Example holiday"))
        view = month_view(self.repo.snapshot(), 2026, 10)
        self.assertEqual(view["people"][0]["approved_days"], 2)
        self.assertFalse(any(r["below_minimum"] for r in view["coverage"]))
        self.assertEqual(len(month_view(state, 2028, 2)["days"]), 29)

    def test_invalid_batch_has_no_partial_write(self):
        before = (Path(self.temp.name) / "ranges.csv").read_bytes()
        with self.assertRaises(ValidationError):
            self.repo.import_ranges([period("good"), period("bad", employee="unknown")])
        self.assertEqual((Path(self.temp.name) / "ranges.csv").read_bytes(), before)
        with self.assertRaises(ValidationError):
            self.repo.save("ranges", period("bad", end="2026-10-01"))

    def test_csv_import_is_repeatable_and_id_conflicts_reject(self):
        row = period("same")
        self.assertEqual(self.repo.import_ranges([row, row]), (1, 1))
        self.assertEqual(self.repo.import_ranges([row]), (0, 1))
        with self.assertRaises(ConflictError):
            self.repo.import_ranges([period("another"), period("same", end="2026-10-16")])
        self.assertEqual(len(self.repo.snapshot()["ranges"]), 1)

    def test_stale_edits_and_simultaneous_creates(self):
        row = self.repo.save("ranges", period("original"))
        self.repo.save("ranges", period("original", end="2026-10-16"), row["version"])
        with self.assertRaises(ConflictError):
            self.repo.save("ranges", period("original", end="2026-10-17"), row["version"])
        def create(index):
            return CsvRepository(self.temp.name).save("ranges", period(f"concurrent-{index}"))
        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(create, range(12)))
        self.assertEqual(len(self.repo.snapshot()["ranges"]), 13)

    def test_atomic_failure_preserves_previous_file_and_backups_exist(self):
        before = (Path(self.temp.name) / "ranges.csv").read_bytes()
        with patch("planner.repository.os.replace", side_effect=OSError("simulated failure")):
            with self.assertRaises(OSError):
                self.repo.save("ranges", period("failed"))
        self.assertEqual((Path(self.temp.name) / "ranges.csv").read_bytes(), before)
        self.assertTrue(list((Path(self.temp.name) / "backups").glob("ranges-*.csv")))
        self.assertFalse(list(Path(self.temp.name).glob("*.tmp")))

    def test_utf8_csv_notes_and_backup_roundtrip(self):
        data = 'employee_id,kind,start_date,end_date,note\nE1,Planned,2026-10-12,2026-10-14,"Արձակուրդ, with comma"\n'.encode("utf-8-sig")
        rows = parse_import(data)
        self.repo.import_ranges(rows)
        self.assertEqual(self.repo.snapshot()["ranges"][0]["note"], "Արձակուրդ, with comma")
        with zipfile.ZipFile(io.BytesIO(self.repo.backup_zip())) as archive:
            self.assertEqual(len(archive.namelist()), 4)
        with self.assertRaises(ValidationError):
            parse_import(b"employee_id,kind,start_date,end_date\nE1,Planned,2026-10-01\n")


if __name__ == "__main__":
    unittest.main()
