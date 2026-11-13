"""O-39 follow-up 2, 2026-10-18: the daily job writes each library's
accepted access requests, so a cycle can read L1. No network, no
token.

    python -m unittest discover -s engines/library/tests

`HfApi.list_accepted_access_requests(repo_id, repo_type=...)` gives
`AccessRequest` items with `username`, `fullname`, `email`,
`timestamp`, `status` and `fields` (huggingface_hub 2.2.0). The files
keep the date, the use case and the company only.
"""
import csv
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import build  # noqa: E402

TODAY = date(2026, 10, 18)


def request(company, use, day=17):
    return SimpleNamespace(
        username="someone", fullname="Some One",
        email="someone@example.com", status="accepted",
        timestamp=datetime(2026, 10, day, 9, tzinfo=timezone.utc),
        fields={"Company": company, "Use case": use})


class FakeApi:
    requests = {}
    fail = set()

    def __init__(self, token=None):
        self.token = token

    def whoami(self):
        return {"name": "acct"}

    def list_accepted_access_requests(self, repo, repo_type=None):
        assert repo_type == "dataset"
        if repo in FakeApi.fail:
            raise RuntimeError("403")
        return iter(FakeApi.requests.get(repo, []))

    def dataset_info(self, repo):
        return SimpleNamespace(downloads=21)


class TestReadAccess(unittest.TestCase):

    def setUp(self):
        self.keep = build.COMMITTED
        self.dir = Path(tempfile.mkdtemp())
        build.COMMITTED = self.dir
        FakeApi.fail = set()
        FakeApi.requests = {
            "acct/us-bills-summaries": [
                request("Acme  Health\nInc", "RAG"),
                request("", "research", day=16)],
            "acct/fda-regulatory-decisions": [
                request("Beta", "commercial")],
        }

    def tearDown(self):
        build.COMMITTED = self.keep
        shutil.rmtree(self.dir, ignore_errors=True)

    def read(self, name):
        with (self.dir / name).open(encoding="utf-8") as f:
            return list(csv.DictReader(f))

    def test_no_token_writes_nothing(self):
        out = build.read_access(TODAY, token="", api_cls=FakeApi)
        self.assertEqual(out[0]["status"], "SKIPPED")
        self.assertEqual(list(self.dir.iterdir()), [])

    def test_one_row_a_request_and_no_personal_fields(self):
        build.read_access(TODAY, token="t", api_cls=FakeApi)
        rows = self.read("library-access-2026-10-18.csv")
        self.assertEqual(len(rows), 3)
        self.assertEqual(list(rows[0]), build.ACCESS_COLUMNS)
        text = (self.dir / "library-access-2026-10-18.csv").read_text()
        for word in ("someone", "Some One", "example.com"):
            self.assertNotIn(word, text)

    def test_the_company_is_one_line(self):
        build.read_access(TODAY, token="t", api_cls=FakeApi)
        rows = self.read("library-access-2026-10-18.csv")
        self.assertIn("Acme Health Inc", [r["company"] for r in rows])

    def test_l1_counts_commercial_and_rag_only(self):
        build.read_access(TODAY, token="t", api_cls=FakeApi)
        l1 = {r["library"]: r for r in
              self.read("library-l1-2026-10-18.csv")}
        self.assertEqual(l1["us-bills"]["requests_accepted"], "2")
        self.assertEqual(l1["us-bills"]["requests_commercial_or_rag"],
                         "1")
        self.assertEqual(
            l1["fda-decisions"]["requests_commercial_or_rag"], "1")
        self.assertEqual(l1["us-bills"]["downloads_30d"], "21")

    def test_one_library_failing_is_recorded_and_the_other_kept(self):
        FakeApi.fail = {"acct/fda-regulatory-decisions"}
        out = build.read_access(TODAY, token="t", api_cls=FakeApi)
        status = {o["library"]: o["status"] for o in out}
        self.assertEqual(status["fda-decisions"], "ERROR")
        self.assertEqual(status["us-bills"], "OK")
        l1 = self.read("library-l1-2026-10-18.csv")
        self.assertEqual([r["library"] for r in l1], ["us-bills"])

    def test_no_requests_still_writes_both_files(self):
        FakeApi.requests = {}
        build.read_access(TODAY, token="t", api_cls=FakeApi)
        self.assertEqual(self.read("library-access-2026-10-18.csv"), [])
        self.assertEqual(len(self.read("library-l1-2026-10-18.csv")), 2)


if __name__ == "__main__":
    unittest.main()
