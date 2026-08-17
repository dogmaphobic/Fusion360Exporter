import os
import tempfile
import unittest
from pathlib import Path

from resilience import (
    GIB,
    ExportJournal,
    MemoryMonitor,
    MemorySnapshot,
    output_is_fresh,
    partial_output_path,
)


class FreshnessTests(unittest.TestCase):
    def test_output_must_be_nonempty_and_at_least_as_new_as_source(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model.step"
            self.assertFalse(output_is_fresh(path, 100))

            path.write_bytes(b"")
            os.utime(path, (200, 200))
            self.assertFalse(output_is_fresh(path, 100))

            path.write_bytes(b"STEP")
            os.utime(path, (99, 99))
            self.assertFalse(output_is_fresh(path, 100))

            os.utime(path, (100, 100))
            self.assertTrue(output_is_fresh(path, 100))

    def test_partial_path_keeps_format_extension_last(self):
        path = Path("/archive/model.f3d")
        partial = partial_output_path(path)
        self.assertEqual(".f3d", partial.suffix)
        self.assertEqual(".model.f3d.partial.f3d", partial.name)


class JournalTests(unittest.TestCase):
    def test_interrupted_record_is_quarantined_on_next_load(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            record = {"key": "version-45", "name": "Ancient model", "version": 45}
            ExportJournal(output).begin(record)

            recovered = ExportJournal(output)
            self.assertEqual("version-45", recovered.recovered_record["key"])
            self.assertTrue(recovered.is_quarantined("version-45"))
            self.assertIsNone(recovered.state["in_progress"])

            recovered.retry("version-45")
            self.assertFalse(recovered.is_quarantined("version-45"))

    def test_clean_finish_does_not_quarantine(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            journal = ExportJournal(output)
            journal.begin({"key": "version-1", "name": "Model", "version": 1})
            journal.finish()

            reloaded = ExportJournal(output)
            self.assertIsNone(reloaded.recovered_record)
            self.assertFalse(reloaded.is_quarantined("version-1"))

    def test_first_journal_recovers_model_from_interrupted_legacy_log(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            log = output / "2026_08_14_23_00.txt"
            log.write_text(
                "Visiting file Trooper Payload Tray v45.f3d\n"
                "Opening `Trooper Payload Tray` v45\n",
                encoding="utf-8",
            )

            journal = ExportJournal(output)
            record = {
                "key": "actual-version-id",
                "name": "Trooper Payload Tray",
                "version": 45,
            }
            self.assertEqual("Trooper Payload Tray", journal.recovered_record["name"])
            self.assertTrue(journal.is_quarantined("actual-version-id", record))

            journal.retry("actual-version-id", record)
            self.assertFalse(journal.is_quarantined("actual-version-id", record))

    def test_completed_legacy_log_does_not_create_quarantine(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory)
            (output / "2026_08_14_23_01.txt").write_text(
                "Opening `Model` v1\nClosing `Model` v1\nSaved 2 files\n",
                encoding="utf-8",
            )
            journal = ExportJournal(output)
            self.assertIsNone(journal.recovered_record)


class MemoryPolicyTests(unittest.TestCase):
    def test_floor_is_greater_of_absolute_and_fractional_limits(self):
        monitor = MemoryMonitor(minimum_free_gib=4, minimum_free_fraction=0.10)
        small = MemorySnapshot(1 * GIB, 5 * GIB, 16 * GIB)
        large = MemorySnapshot(1 * GIB, 7 * GIB, 128 * GIB)

        self.assertEqual(4 * GIB, monitor.safety_floor(small))
        self.assertFalse(monitor.is_low(small))
        self.assertEqual(int(12.8 * GIB), monitor.safety_floor(large))
        self.assertTrue(monitor.is_low(large))


if __name__ == "__main__":
    unittest.main()
