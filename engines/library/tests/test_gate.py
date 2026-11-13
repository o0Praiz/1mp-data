"""O-39: every library card carries the Hugging Face access form, and
the publish step gates the repo before it uploads. No network, no
token.

    python -m unittest discover -s engines/library/tests

Keys read from huggingface.co/docs/hub/datasets-gated on 2026-10-16:
`extra_gated_prompt`, and `extra_gated_fields` as a map of field name
to a type or to `{type: select, options: [...]}`. The page names no
card key for the approval mode. `HfApi.update_repo_settings(gated=
"auto" | "manual" | False)` sets it (huggingface_hub 2.2.0 source).
"""
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import build  # noqa: E402
import test_build as tb  # noqa: E402
import test_fda as tf  # noqa: E402

try:
    import yaml
except ImportError:  # the string checks below still run
    yaml = None

USE_CASES = ["research", "commercial", "RAG", "model training", "other"]


def front_matter(text: str) -> str:
    assert text.startswith("---\n"), "card has no YAML front matter"
    end = text.find("\n---\n", 3)
    assert end != -1, "card front matter is not closed"
    return text[4:end + 1]


class GateChecks:
    """The checks one built card must pass. Mixed into one test class
    per library, so a missing key fails each library by name."""

    def assert_gated(self, text: str) -> None:
        head = front_matter(text)
        self.assertIn("extra_gated_prompt:", head)
        self.assertIn("extra_gated_fields:", head)
        self.assertIn("  Company: text", head)
        self.assertIn("  Use case:", head)
        self.assertIn("    type: select", head)
        for u in USE_CASES:
            self.assertIn(f"    - {u}\n", head)
        # No field asks for an e-mail or any other personal detail.
        self.assertNotIn("mail", head.lower())
        # The body of the card does not carry the form.
        self.assertNotIn("extra_gated", text[len(head) + 8:])
        if yaml is not None:
            meta = yaml.safe_load(head)
            self.assertTrue(meta["extra_gated_prompt"].strip())
            fields = meta["extra_gated_fields"]
            self.assertEqual(set(fields), {"Company", "Use case"})
            self.assertEqual(fields["Company"], "text")
            self.assertEqual(fields["Use case"]["type"], "select")
            self.assertEqual(fields["Use case"]["options"], USE_CASES)
            # The licence keys the card had before O-39 are still read.
            self.assertIn("license", meta)

    def assert_prompt_is_honest(self, text: str) -> None:
        head = front_matter(text)
        self.assertIn("free", head)
        # The prompt claims no users. Until 2026-10-18 it named no feed
        # either, because O-38 was not public. Both feeds are public
        # now (O-38, O-40), so it may name one, and only by the
        # library's own feed_url.
        for word in ("paid", "users"):
            self.assertNotIn(word, head)
        prompt = [l for l in head.splitlines()
                  if l.startswith("extra_gated_prompt:")][0]
        if "feed" in prompt:
            self.assertIn("https://apify.com/1mp/", prompt)


class TestUsBillsCardIsGated(GateChecks, unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = Path(tempfile.mkdtemp())
        self.keep = (build.DATA, build.COMMITTED)
        build.DATA = self.data / "library-data"
        build.COMMITTED = self.data / "committed"

    def tearDown(self):
        build.DATA, build.COMMITTED = self.keep
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.data, ignore_errors=True)

    def card(self) -> str:
        fetch, _, _ = tb.fake_source()
        m = build.build("us-bills", shard="119", today=tb.TODAY,
                        fetch=fetch, publish=lambda *a: {}, token="",
                        out_root=self.tmp)
        self.assertEqual(m["status"], "OK")
        return (self.tmp / "us-bills" / "README.md").read_text(
            encoding="utf-8")

    def test_the_built_card_carries_the_gate(self):
        self.assert_gated(self.card())

    def test_the_prompt_is_honest(self):
        self.assert_prompt_is_honest(self.card())


class TestFdaCardIsGated(GateChecks, unittest.TestCase):

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = Path(tempfile.mkdtemp())
        self.keep = (build.DATA, build.COMMITTED)
        build.DATA = self.data / "library-data"
        build.COMMITTED = self.data / "committed"

    def tearDown(self):
        build.DATA, build.COMMITTED = self.keep
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.data, ignore_errors=True)

    def card(self) -> str:
        recs = [tf.SAMPLES["device/recall"]]

        def fetch(url, ua):
            if url == tf.MANIFEST_URL:
                return tf.manifest_bytes({"device/recall": 1})
            cat, name = url.split("/")[3], url.split("/")[4]
            return tf.bulk_zip(cat, name, recs)

        m = build.build(tf.NAME, shard="device-recall", today=tf.TODAY,
                        fetch=fetch, publish=lambda *a: {}, token="",
                        out_root=self.tmp)
        self.assertEqual(m["status"], "OK")
        return (self.tmp / tf.NAME / "README.md").read_text(
            encoding="utf-8")

    def test_the_built_card_carries_the_gate(self):
        text = self.card()
        self.assert_gated(text)
        self.assertIn("license: cc0-1.0", front_matter(text))

    def test_the_prompt_is_honest(self):
        self.assert_prompt_is_honest(self.card())


class TestGateFunction(unittest.TestCase):

    def test_a_card_with_no_front_matter_gets_one(self):
        out = build.gate("# Title\n")
        self.assertTrue(out.startswith("---\nextra_gated_prompt:"))
        self.assertTrue(out.endswith("---\n\n# Title\n"))

    def test_gate_runs_once(self):
        once = build.gate("---\nlicense: mit\n---\n\n# T\n")
        self.assertEqual(build.gate(once), once)
        self.assertEqual(once.count("extra_gated_fields:"), 1)


class FakeApi:
    calls: list = []

    def __init__(self, token=None):
        pass

    def whoami(self):
        return {"name": "acct"}

    def create_repo(self, **kw):
        FakeApi.calls.append(("create_repo", kw))

    def update_repo_settings(self, **kw):
        FakeApi.calls.append(("update_repo_settings", kw))

    def upload_folder(self, **kw):
        FakeApi.calls.append(("upload_folder", kw))


class OldApi(FakeApi):
    update_repo_settings = None


class TestPublishGatesFirst(unittest.TestCase):

    def run_publish(self, api_cls):
        FakeApi.calls = []
        fake = types.ModuleType("huggingface_hub")
        fake.HfApi = api_cls
        keep = sys.modules.get("huggingface_hub")
        sys.modules["huggingface_hub"] = fake
        try:
            return build.hf_publish({"hf_repo": "lib"}, Path("."),
                                    "not-a-real-token")
        finally:
            if keep is None:
                del sys.modules["huggingface_hub"]
            else:
                sys.modules["huggingface_hub"] = keep

    def test_the_repo_is_gated_auto_before_any_upload(self):
        r = self.run_publish(FakeApi)
        names = [c[0] for c in FakeApi.calls]
        self.assertEqual(names, ["create_repo", "update_repo_settings",
                                 "upload_folder"])
        kw = FakeApi.calls[1][1]
        self.assertEqual(kw["gated"], "auto")
        self.assertEqual(kw["repo_type"], "dataset")
        self.assertEqual(kw["repo_id"], "acct/lib")
        self.assertTrue(r["published"])
        self.assertEqual(r["gated"], "auto")

    def test_a_client_that_cannot_gate_uploads_nothing(self):
        r = self.run_publish(OldApi)
        self.assertFalse(r["published"])
        self.assertEqual(FakeApi.calls, [])


class TestTheFeedIsNamed(unittest.TestCase):
    """O-39 follow-up 4, 2026-10-18: the form names the library's paid
    change feed when the spec has a `feed_url`."""

    def test_the_prompt_names_the_feed(self):
        url = "https://apify.com/1mp/fda-change-feed"
        head = front_matter(build.gate("# T\n", url))
        self.assertIn(url, head)
        self.assertIn("extra_gated_fields:", head)
        if yaml is not None:
            self.assertIn(url, yaml.safe_load(head)["extra_gated_prompt"])

    def test_no_feed_keeps_the_plain_prompt(self):
        self.assertEqual(build.gate("# T\n"), build.gate("# T\n", None))

    def test_a_quote_in_the_url_is_refused(self):
        with self.assertRaises(ValueError):
            build.gate("# T\n", 'https://x/"y')

    def test_both_libraries_name_a_live_feed(self):
        for name in ("us-bills", "fda-decisions"):
            with self.subTest(library=name):
                url = build.load_spec(name).get("feed_url", "")
                self.assertTrue(url.startswith("https://apify.com/1mp/"))


if __name__ == "__main__":
    unittest.main()
