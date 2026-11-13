"""Part 3: the $29 offer on the dataset cards (docs/45 section 8).

The card carries the offer only when engines/direct/offer.json says
"live": true and every field is valid. No network, no token.

    python -m unittest discover -s engines/library/tests
"""
import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import build  # noqa: E402
import test_build as tb  # noqa: E402
import test_fda as tf  # noqa: E402

BASE = "https://abc.supabase.co/functions/v1"
LIVE = {"live": True, "base_url": BASE, "price_usd_month": 29,
        "load": "weekly"}
CARD = "---\nlicense: cc0-1.0\n---\n\n# T\n\nIntro.\n\n## Columns\n\nx\n"


def spec(name="us-bills", feed="https://apify.com/1mp/us-bills-change-feed"):
    return {"name": name, "feed_url": feed}


class TestOfferLines(unittest.TestCase):

    def test_not_live_gives_nothing(self):
        for off in ({}, {**LIVE, "live": False}, {**LIVE, "live": "yes"},
                    None, []):
            self.assertEqual(build.offer_lines(spec(), off), [], off)

    def test_a_bad_field_gives_nothing(self):
        bad = [{**LIVE, "base_url": ""},
               {**LIVE, "base_url": "http://abc.supabase.co/functions/v1"},
               {**LIVE, "base_url": BASE + '"x'},
               {**LIVE, "base_url": BASE + " x"},
               {**LIVE, "price_usd_month": 0},
               {**LIVE, "price_usd_month": "29"},
               {**LIVE, "price_usd_month": True},
               {**LIVE, "load": "hourly"}]
        for off in bad:
            self.assertEqual(build.offer_lines(spec(), off), [], off)

    def test_an_unknown_library_gives_nothing(self):
        self.assertEqual(build.offer_lines(spec("edgar"), LIVE), [])

    def test_the_live_text(self):
        text = "\n".join(build.offer_lines(spec(), LIVE))
        self.assertIn("## The change feed by key, $29 a month", text)
        self.assertIn(f"{BASE}/subscribe", text)
        self.assertIn(f'"{BASE}/feed?library=us-bills&since=7"', text)
        self.assertIn("each Monday", text)
        self.assertIn("PayPal", text)
        self.assertIn("https://apify.com/1mp/us-bills-change-feed", text)
        # Honest text: no claims we cannot prove.
        for word in ("guarantee", "real-time", "best", "official"):
            self.assertNotIn(word, text.lower())

    def test_daily_load_and_trailing_slash(self):
        off = {**LIVE, "load": "daily", "base_url": BASE + "/"}
        text = "\n".join(build.offer_lines(spec("fda-decisions"), off))
        self.assertIn("each day", text)
        self.assertIn(f"{BASE}/feed?library=fda-decisions", text)
        self.assertNotIn("v1//", text)

    def test_no_feed_url_gives_no_apify_line(self):
        text = "\n".join(build.offer_lines(spec(feed=None), LIVE))
        self.assertNotIn("apify.com", text)


class TestAddOffer(unittest.TestCase):

    def test_the_offer_goes_before_the_first_section(self):
        out = build.add_offer(CARD, spec(), LIVE)
        self.assertTrue(out.startswith("---\nlicense: cc0-1.0\n---\n"))
        self.assertLess(out.index("Intro."), out.index("## The change"))
        self.assertLess(out.index("## The change"), out.index("## Columns"))

    def test_no_offer_leaves_the_card_as_it_was(self):
        self.assertEqual(build.add_offer(CARD, spec(), {}), CARD)

    def test_a_card_with_no_section_gets_the_offer_at_the_end(self):
        out = build.add_offer("# T\n\nIntro.\n", spec(), LIVE)
        self.assertTrue(out.startswith("# T\n\nIntro.\n\n## The change"))

    def test_a_section_mark_in_the_front_matter_is_skipped(self):
        card = "---\nx: \"a\n## b\"\n---\n\n# T\n\n## C\n"
        out = build.add_offer(card, spec(), LIVE)
        self.assertLess(out.index("---\n\n# T"), out.index("## The change"))

    def test_the_gate_still_works_on_an_offer_card(self):
        out = build.gate(build.add_offer(CARD, spec(), LIVE))
        self.assertIn("extra_gated_fields:", out.split("\n---\n", 1)[0])
        self.assertEqual(out.count("## The change feed by key"), 1)


class TestTheFileInTheRepo(unittest.TestCase):

    def test_the_repo_offer_is_valid_and_held(self):
        off = build.load_offer()
        self.assertIn(off.get("live"), (True, False))
        self.assertEqual(off.get("price_usd_month"), 29)
        self.assertIn(off.get("load"), build.OFFER_CADENCE)
        if off["live"]:
            # Live means the section renders. A live file that renders
            # nothing is a silent failure.
            self.assertTrue(build.offer_lines(spec(), off))

    def test_a_missing_or_broken_file_is_no_offer(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            self.assertEqual(build.load_offer(tmp / "none.json"), {})
            (tmp / "bad.json").write_text("{", encoding="utf-8")
            self.assertEqual(build.load_offer(tmp / "bad.json"), {})
        finally:
            shutil.rmtree(tmp)


class TestBuiltCards(unittest.TestCase):
    """Both real cards, built end to end, with the offer live."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.data = Path(tempfile.mkdtemp())
        self.keep = (build.DATA, build.COMMITTED, build.OFFER)
        build.DATA = self.data / "library-data"
        build.COMMITTED = self.data / "committed"
        build.OFFER = self.data / "offer.json"
        build.OFFER.write_text(json.dumps(LIVE), encoding="utf-8")

    def tearDown(self):
        build.DATA, build.COMMITTED, build.OFFER = self.keep
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.data, ignore_errors=True)

    def check(self, text: str, lib: str) -> None:
        self.assertEqual(text.count("## The change feed by key"), 1)
        self.assertIn(f"{BASE}/feed?library={lib}&since=7", text)
        head = text.split("\n---\n", 1)[0]
        self.assertIn("extra_gated_fields:", head)
        self.assertNotIn("## The change", head)

    def test_us_bills(self):
        fetch, _, _ = tb.fake_source()
        m = build.build("us-bills", shard="119", today=tb.TODAY,
                        fetch=fetch, publish=lambda *a: {}, token="",
                        out_root=self.tmp)
        self.assertEqual(m["status"], "OK")
        self.check((self.tmp / "us-bills" / "README.md").read_text(
            encoding="utf-8"), "us-bills")

    def test_fda_decisions(self):
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
        self.check((self.tmp / tf.NAME / "README.md").read_text(
            encoding="utf-8"), tf.NAME)

    def test_held_offer_leaves_the_card_without_it(self):
        build.OFFER.write_text(json.dumps({**LIVE, "live": False}),
                               encoding="utf-8")
        fetch, _, _ = tb.fake_source()
        build.build("us-bills", shard="119", today=tb.TODAY, fetch=fetch,
                    publish=lambda *a: {}, token="", out_root=self.tmp)
        text = (self.tmp / "us-bills" / "README.md").read_text(
            encoding="utf-8")
        self.assertNotIn("## The change feed by key", text)


if __name__ == "__main__":
    unittest.main()
