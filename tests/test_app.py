"""Exercise actual Streamlit forms with temporary CSV storage."""
import os
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from streamlit.testing.v1 import AppTest
from planner.repository import CsvRepository


class AppTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"VACATION_DATA_DIR": self.temp.name})
        self.env.start()
        self.repo = CsvRepository(self.temp.name)
        self.repo.save("teams", dict(team="Data", minimum_at_work="1"))
        self.repo.save("employees", dict(employee_id="E1", name="Example employee", team="Data", active="true"))
        self.at = AppTest.from_file(str(Path(__file__).resolve().parents[1] / "app.py"), default_timeout=15)

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def page(self, name):
        self.at.sidebar.radio[0].set_value(name).run()
        self.assertFalse(self.at.exception)

    def test_pages_and_create_edit_range(self):
        self.at.run()
        self.assertFalse(self.at.exception)
        self.page("Date ranges")
        self.at.date_input[0].set_value(date(2026, 10, 12))
        self.at.date_input[1].set_value(date(2026, 10, 14))
        next(b for b in self.at.button if b.label == "Save range").click().run()
        self.assertFalse(self.at.exception)
        saved = self.repo.snapshot()["ranges"]
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved[0]["kind"], "Planned")
        self.at.radio(key="range_mode").set_value("Edit range").run()
        next(s for s in self.at.selectbox if s.label == "Range kind").set_value("Approved")
        next(b for b in self.at.button if b.label == "Save range").click().run()
        self.assertFalse(self.at.exception)
        self.assertEqual(self.repo.snapshot()["ranges"][0]["kind"], "Approved")
        for page in ("Employees & teams", "Holidays", "Import & export", "Calendar"):
            self.page(page)

    def test_invalid_dates_do_not_save(self):
        self.at.run()
        self.page("Date ranges")
        self.at.date_input[0].set_value(date(2026, 10, 14))
        self.at.date_input[1].set_value(date(2026, 10, 12))
        next(b for b in self.at.button if b.label == "Save range").click().run()
        self.assertFalse(self.at.exception)
        self.assertTrue(self.at.error)
        self.assertEqual(self.repo.snapshot()["ranges"], [])

    def test_stale_form_cannot_overwrite_newer_approval(self):
        values = dict(range_id="R1", employee_id="E1", kind="Planned", start_date="2026-10-12", end_date="2026-10-14", note="initial")
        saved = self.repo.save("ranges", values)
        self.at.run()
        self.page("Date ranges")
        self.at.radio(key="range_mode").set_value("Edit range").run()
        self.repo.save("ranges", {**values, "kind": "Approved", "note": "newer edit"}, saved["version"])
        next(t for t in self.at.text_input if t.label == "Note").set_value("stale edit")
        next(b for b in self.at.button if b.label == "Save range").click().run()
        self.assertFalse(self.at.exception)
        self.assertTrue(self.at.error)
        current = self.repo.snapshot()["ranges"][0]
        self.assertEqual((current["kind"], current["note"]), ("Approved", "newer edit"))

    def test_add_and_remove_employee_preserves_record(self):
        self.at.run()
        self.page("Employees & teams")
        next(t for t in self.at.text_input if t.label == "Employee ID").set_value("E2")
        next(t for t in self.at.text_input if t.label == "Employee name").set_value("Second employee")
        next(b for b in self.at.button if b.label == "Add employee").click().run()
        self.assertFalse(self.at.exception)
        saved = next(row for row in self.repo.snapshot()["employees"] if row["employee_id"] == "E2")
        self.assertEqual(saved["active"], "true")

        next(s for s in self.at.selectbox if s.label == "Employee to update").set_value("E2").run()
        next(b for b in self.at.button if b.label == "Remove employee (deactivate)").click().run()
        self.assertFalse(self.at.exception)
        removed = next(row for row in self.repo.snapshot()["employees"] if row["employee_id"] == "E2")
        self.assertEqual(removed["active"], "false")

        next(s for s in self.at.selectbox if s.label == "Employee to update").set_value("E2").run()
        next(b for b in self.at.button if b.label == "Reactivate employee").click().run()
        self.assertFalse(self.at.exception)
        reactivated = next(row for row in self.repo.snapshot()["employees"] if row["employee_id"] == "E2")
        self.assertEqual(reactivated["active"], "true")


if __name__ == "__main__":
    unittest.main()
