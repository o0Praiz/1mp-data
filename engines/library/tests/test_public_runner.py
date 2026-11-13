"""The public runner, run.py, for the 1mp-data repo.

Added 2026-11-10 for the split in docs/48 section 6.1. build.py stays
byte-identical to the private repo; run.py turns off its access log.
"""
import importlib.util
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent.parent.parent
spec = importlib.util.spec_from_file_location("public_runner",
                                              ROOT / "run.py")
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
DAY = date(2026, 11, 10)


class TestNoAccessLog(unittest.TestCase):
    def test_the_loaded_build_never_reads_access(self):
        mod = runner.load_build()
        self.assertIs(mod.read_access, runner.no_access)
        with mock.patch.object(mod, "list_libraries", return_value=[]):
            out = mod.build_due(DAY)
        self.assertEqual([e["status"] for e in out], ["SKIPPED"])
        self.assertEqual(out[0]["library"], "*access")

    def test_no_access_writes_no_file(self):
        mod = runner.load_build()
        with mock.patch.object(mod, "write_csv") as w, \
             mock.patch.object(mod, "list_libraries", return_value=[]):
            mod.build_due(DAY)
        w.assert_not_called()


class TestExitCode(unittest.TestCase):
    def test_refused_is_not_a_crash(self):
        logs = [{"library": "a", "status": "OK"},
                {"library": "b", "status": "REFUSED"},
                {"library": "c", "status": "SKIPPED"}]
        self.assertEqual(runner.run(lambda t: logs, DAY), 0)

    def test_an_error_turns_the_run_red(self):
        logs = [{"library": "a", "status": "OK"},
                {"library": "b", "status": "ERROR", "error": "x"}]
        self.assertEqual(runner.run(lambda t, **k: logs, DAY), 1)


if __name__ == "__main__":
    unittest.main()
