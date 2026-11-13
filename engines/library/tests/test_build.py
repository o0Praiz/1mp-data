"""Offline tests for the library engine. No network, no token.

    python -m unittest discover -s engines/library/tests

Every test builds its own zips in memory, so the suite never touches
govinfo and never needs a key. The one real-source fact these tests
pin is the SHAPE of govinfo's XML, copied from
BILLSUM-119hr29.xml and BILLS-119hr29ih.xml as read on 2026-10-07,
and, for the six resolution types `O-31` added, from the 8,971
resolution BILLSUM files and their BILLS members as read on
2026-10-09. The resolution shape is identical to the bill shape in
all 8,971 files and all 11,582 summaries.
"""
import io
import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent.parent / "libraries" / "us-bills"))
import build  # noqa: E402
import parse  # noqa: E402

TODAY = date(2026, 10, 7)
SPEC = json.loads(
    (HERE.parent.parent.parent / "libraries" / "us-bills"
     / "library.json").read_text(encoding="utf-8"))


def summary_xml(congress, btype, number, title, summaries):
    """One BILLSUM file, in govinfo's shape."""
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?><BillSummaries>',
        f'<item congress="{congress}" measure-type="{btype}" '
        f'measure-number="{number}" measure-id="id{congress}{btype}'
        f'{number}" originChamber="HOUSE" orig-publish-date="2025-01-03" '
        f'update-date="2025-01-13">',
        f'<title>{title}</title>',
    ]
    for i, (desc, text) in enumerate(summaries):
        parts.append(
            f'<summary summary-id="id{congress}{btype}{number}v{i:02d}" '
            f'currentChamber="HOUSE" update-date="2025-01-13">'
            f'<action-date>2025-01-03</action-date>'
            f'<action-desc>{desc}</action-desc>'
            f'<summary-text><![CDATA[{text}]]></summary-text></summary>')
    parts.append('</item><dublinCore xmlns:dc="http://purl.org/dc/'
                 'elements/1.1/"><dc:rights>Pursuant to Title 17 '
                 'Section 105 of the United States Code, this file is '
                 'not subject to copyright protection and is in the '
                 'public domain.</dc:rights></dublinCore>'
                 '</BillSummaries>')
    return "".join(parts)


def bill_xml(congress, btype, number, version, body):
    return (f'<?xml version="1.0"?><bill bill-stage="Introduced-in-House" '
            f'public-private="public"><metadata><dublinCore>'
            f'<dc:title>{congress} {btype.upper()} {number} '
            f'{version.upper()}</dc:title></dublinCore></metadata>'
            f'<legis-body><section><text>{body}</text></section>'
            f'</legis-body></bill>')


def zip_bytes(members: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, text in members.items():
            z.writestr(name, text)
    return buf.getvalue()


def fake_source(congress="119", bills=None, texts=None):
    """A fetch() over an in-memory govinfo, plus the urls it answers.

    `bills` is (type, number, title, [(action-desc, summary html)]).
    `texts` is (type, number, version code, body text).
    """
    bills = bills if bills is not None else [
        ("hr", 29, "Laken Riley Act",
         [("Introduced in House", "<p>A <strong>bill</strong> to "
                                  "detain.</p><ul><li>one</li></ul>")]),
        ("hr", 30, "For the relief of Jane Roe",
         [("Introduced in House", "<p>Private relief.</p>")]),
        ("s", 7, "A Senate Act",
         [("Introduced in Senate", "<p>Senate one.</p>"),
          ("Passed Senate", "<p>Senate two.</p>")]),
    ]
    texts = texts if texts is not None else [
        ("hr", 29, "ih", "Be it enacted. Section 1."),
        ("hr", 29, "eh", "Be it enacted, as passed."),
        ("hr", 30, "ih", "Private text."),
        ("s", 7, "is", "Senate introduced text."),
        ("s", 7, "es", "Senate engrossed text."),
    ]
    types = list(SPEC["source"]["types"])
    sums = {t: {} for t in types}
    for btype, number, title, ss in bills:
        sums[btype][f"BILLSUM-{congress}{btype}{number}.xml"] = \
            summary_xml(congress, btype, number, title, ss)
    body = {t: {} for t in types}
    for btype, number, version, text in texts:
        body[btype][f"BILLS-{congress}{btype}{number}{version}.xml"] = \
            bill_xml(congress, btype, number, version, text)
    src = SPEC["source"]
    store = {}
    # Every type in the spec gets a zip, empty when the case under
    # test has no measure of that type: `sources()` makes the summary
    # zip of every type a REQUIRED source, so a missing one would stop
    # the build instead of exercising the case.
    for t in types:
        store[src["summary_zip"].format(congress=congress, type=t)] = \
            zip_bytes(sums[t])
        store[src["text_zip"].format(
            congress=congress, session=1, type=t)] = zip_bytes(body[t])
        # Session 2 is not published for this fake Congress.
    calls = []

    def fetch(url, ua):
        calls.append((url, ua))
        if url not in store:
            raise RuntimeError(f"HTTP 404 {url}")
        return store[url]

    return fetch, calls, store


class TestStrip(unittest.TestCase):

    def test_cdata_and_paragraphs(self):
        out = parse.strip_html(
            "<![CDATA[<p><strong>Act</strong></p><p>b&amp;c</p>"
            "<ul><li>x</li></ul>]]>")
        self.assertEqual(out, "Act\n\nb&c\n\nx")

    def test_no_tags_survive(self):
        out = parse.strip_bill_text(
            '<bill><section><text>Be it &amp; so.</text></section></bill>')
        self.assertNotIn("<", out)
        self.assertIn("Be it & so.", out)

    def test_blank_lines_collapse(self):
        self.assertEqual(parse.strip_html("<p>a</p><p></p><p>b</p>"),
                         "a\n\nb")


class TestRows(unittest.TestCase):

    def blobs(self, store):
        return {u: {"bytes": b, "sha256": f"sha-{i}",
                    "retrieved_at": "2026-10-07T11:00:00Z",
                    "size": len(b)}
                for i, (u, b) in enumerate(sorted(store.items()))}

    def rows(self, **kw):
        fetch, calls, store = fake_source(**kw)
        return parse.rows(SPEC, "119", self.blobs(store))

    def test_one_row_per_summary_minus_exclusions(self):
        rows, stats = self.rows()
        # 3 bills, 4 summaries, one bill (2 of 4... ) excluded.
        self.assertEqual(stats["summary_files"], 3)
        self.assertEqual(stats["bills"], 3)
        self.assertEqual(stats["summaries"], 4)
        self.assertEqual(stats["excluded_bills"], 1)
        self.assertEqual(stats["excluded_summaries"], 1)
        self.assertEqual(len(rows), 3)

    def test_private_relief_bill_is_out(self):
        rows, _ = self.rows()
        self.assertNotIn("119-hr-30", {r["bill_id"] for r in rows})
        self.assertFalse([r for r in rows
                          if "for the relief of" in r["title"].lower()])

    def test_key_is_unique(self):
        rows, _ = self.rows()
        keys = [(r["bill_id"], r["version_code"]) for r in rows]
        self.assertEqual(len(keys), len(set(keys)))

    def test_text_follows_the_summary_action(self):
        rows, stats = self.rows()
        by = {(r["bill_id"], r["summary_action_desc"]): r for r in rows}
        intro = by[("119-s-7", "Introduced in Senate")]
        passed = by[("119-s-7", "Passed Senate")]
        self.assertEqual(intro["text_version_code"], "is")
        self.assertEqual(passed["text_version_code"], "es")
        self.assertEqual(intro["text_match"], "action")
        self.assertIn("Senate introduced text.", intro["text"])
        self.assertIn("Senate engrossed text.", passed["text"])
        self.assertEqual(stats["text_from_action"], 3)

    def test_unmapped_action_falls_back_to_latest(self):
        rows, stats = self.rows(bills=[
            ("hr", 29, "A Bill",
             [("House agreed to Senate amendment", "<p>s</p>")])],
            texts=[("hr", 29, "ih", "one"), ("hr", 29, "enr", "final")])
        self.assertEqual(rows[0]["text_match"], "latest")
        self.assertEqual(rows[0]["text_version_code"], "enr")
        self.assertEqual(stats["text_from_latest"], 1)

    def test_missing_text_is_recorded_not_invented(self):
        rows, stats = self.rows(
            bills=[("hr", 99, "A Bill",
                    [("Introduced in House", "<p>s</p>")])],
            texts=[])
        self.assertEqual(rows[0]["text"], "")
        self.assertEqual(rows[0]["text_match"], "none")
        self.assertEqual(rows[0]["source_url_text"], "")
        self.assertEqual(stats["text_missing"], 1)

    def test_every_row_is_stamped(self):
        rows, _ = self.rows()
        for r in rows:
            for field in build.STAMPS:
                self.assertTrue(str(r[field]).strip(), (field, r["bill_id"]))

    def test_billsum_aliases_are_present(self):
        rows, _ = self.rows()
        for r in rows:
            self.assertEqual(r["summary"], r["summary_text"])
            self.assertIn("title", r)
            self.assertIn("text", r)

    def test_lengths_match_the_strings(self):
        rows, _ = self.rows()
        for r in rows:
            self.assertEqual(r["text_len"], len(r["text"]))
            self.assertEqual(r["sum_len"], len(r["summary_text"]))

    def test_schema_and_rows_agree(self):
        rows, _ = self.rows()
        fields = {f["name"] for f in SPEC["schema"]}
        self.assertEqual(set(rows[0]), fields)


RESOLUTION_TYPES = ("hres", "hjres", "sres", "sjres", "hconres",
                    "sconres")
# (type, number, action-desc, the version code that action names)
RESOLUTION_CASES = (
    ("hres", 7, "Introduced in House", "ih"),
    ("hjres", 12, "Introduced in House", "ih"),
    ("hconres", 3, "Introduced in House", "ih"),
    ("sres", 21, "Introduced in Senate", "is"),
    ("sjres", 9, "Introduced in Senate", "is"),
    ("sconres", 2, "Introduced in Senate", "is"),
)
# The four <action-desc> values in the 11,582 resolution summaries of
# the 116th to 119th Congress that `ACTION_VERSION` did not map, read
# 2026-10-09. The first names a bill text version and `O-31` added it.
# The other three name an action on an amendment or on a separate
# conference report, not on a version of the measure, so they keep
# falling back to the latest text and saying so in `text_match`.
RESOLUTION_ACTION_ADDED = (("Reported to House, Part I", "rh"),)
RESOLUTION_ACTIONS_UNMAPPED = (
    "House agreed to Senate amendment",
    "Senate agreed to House amendment",
    "Conference report filed in House",
)


class TestResolutionTypes(unittest.TestCase):
    """The six resolution types, added by `O-31` on 2026-10-09.

    The zips stay out of the suite. What is reproduced here is the
    shape and the file naming read off the real ones that day.
    """

    def blobs(self, store):
        return {u: {"bytes": b, "sha256": f"sha-{i}",
                    "retrieved_at": "2026-10-09T11:00:00Z",
                    "size": len(b)}
                for i, (u, b) in enumerate(sorted(store.items()))}

    def rows(self, bills, texts):
        fetch, calls, store = fake_source(bills=bills, texts=texts)
        return parse.rows(SPEC, "119", self.blobs(store))

    def test_the_spec_covers_the_eight_measure_types(self):
        self.assertEqual(
            SPEC["source"]["types"],
            ["hr", "s", "hres", "hjres", "sres", "sjres", "hconres",
             "sconres"])

    def test_nothing_in_not_covered_claims_resolutions_are_missing(self):
        for item in SPEC["not_covered"]:
            low = item.lower()
            self.assertNotIn("resolutions: hres", low)
            self.assertNotIn("types hr and s only", low)
            self.assertNotIn("are not in this build", low)

    def test_sources_cover_every_type_and_session(self):
        src = SPEC["source"]
        srcs = parse.sources(SPEC, "119")
        self.assertEqual(len(srcs),
                         len(src["types"]) * (1 + len(src["sessions"])))
        for t in src["types"]:
            mine = [x for x in srcs if x["type"] == t]
            self.assertEqual(len(mine), 3, t)
            self.assertEqual(
                [x["role"] for x in mine], ["summary", "text", "text"])
            self.assertIn(f"/BILLSUM/119/{t}/BILLSUM-119-{t}.zip",
                          mine[0]["url"])
            self.assertIn(f"/BILLS/119/1/{t}/BILLS-119-1-{t}.zip",
                          mine[1]["url"])

    def test_the_file_name_regexes_split_every_measure_type(self):
        # The question `O-31` asked: the type group is a non-greedy
        # `[a-z]+?`, so does it stop at one letter on `hjres`? No. It
        # starts at one letter and the engine backtracks it outward
        # until the digits group that follows can match, so the whole
        # type comes out. Checked against all 8,971 real resolution
        # BILLSUM members and all 13,368 resolution BILLS members on
        # 2026-10-09: zero unmatched, zero mis-split.
        cases = RESOLUTION_CASES + (("hr", 29, "", "ih"),
                                    ("s", 7, "", "is"))
        for t, n, _desc, v in cases:
            m = parse.SUM_FILE.match(f"BILLSUM-119{t}{n}.xml")
            self.assertIsNotNone(m, t)
            self.assertEqual(
                (m.group("congress"), m.group("type"), m.group("number")),
                ("119", t, str(n)), t)
            b = parse.BILL_FILE.match(f"BILLS-119{t}{n}{v}.xml")
            self.assertIsNotNone(b, t)
            self.assertEqual(
                (b.group("congress"), b.group("type"), b.group("number"),
                 b.group("version")), ("119", t, str(n), v), t)

    def test_a_long_number_and_a_numbered_version_still_split(self):
        m = parse.SUM_FILE.match("BILLSUM-116sconres152.xml")
        self.assertEqual((m.group("type"), m.group("number")),
                         ("sconres", "152"))
        b = parse.BILL_FILE.match("BILLS-116hconres100rh2.xml")
        self.assertEqual(
            (b.group("type"), b.group("number"), b.group("version")),
            ("hconres", "100", "rh2"))

    def test_each_new_type_makes_a_row_with_its_own_text(self):
        bills = [(t, n, f"A resolution numbered {n}",
                  [(desc, f"<p>Summary of {t} {n}.</p>")])
                 for t, n, desc, _v in RESOLUTION_CASES]
        texts = [(t, n, v, f"Resolved, {t} {n} body.")
                 for t, n, _desc, v in RESOLUTION_CASES]
        rows, stats = self.rows(bills, texts)
        self.assertEqual(len(rows), len(RESOLUTION_CASES))
        self.assertEqual(stats["summary_files"], len(RESOLUTION_CASES))
        self.assertEqual(stats["bills"], len(RESOLUTION_CASES))
        self.assertEqual(stats["text_from_action"], len(RESOLUTION_CASES))
        self.assertEqual(stats["text_from_latest"], 0)
        self.assertEqual(stats["text_missing"], 0)
        by = {r["type"]: r for r in rows}
        self.assertEqual(set(by), set(RESOLUTION_TYPES))
        fields = {f["name"] for f in SPEC["schema"]}
        for t, n, _desc, v in RESOLUTION_CASES:
            r = by[t]
            self.assertEqual(r["bill_id"], f"119-{t}-{n}", t)
            self.assertEqual(r["congress"], 119, t)
            self.assertEqual(r["number"], n, t)
            self.assertEqual(r["version_code"], "v00", t)
            self.assertEqual(r["summary_id"], f"id119{t}{n}v00", t)
            self.assertEqual(r["text_version_code"], v, t)
            self.assertEqual(r["text_match"], "action", t)
            self.assertEqual(r["summary_text"], f"Summary of {t} {n}.", t)
            self.assertEqual(r["summary"], r["summary_text"], t)
            self.assertIn(f"Resolved, {t} {n} body.", r["text"], t)
            self.assertEqual(set(r), fields, t)
            for field in build.STAMPS:
                self.assertTrue(str(r[field]).strip(), (t, field))

    def test_a_shard_of_resolutions_passes_every_gate(self):
        bills = [(t, n, f"A resolution numbered {n}",
                  [(desc, f"<p>Summary of {t} {n}.</p>")])
                 for t, n, desc, _v in RESOLUTION_CASES]
        texts = [(t, n, v, f"Resolved, {t} {n} body.")
                 for t, n, _desc, v in RESOLUTION_CASES]
        rows, stats = self.rows(bills, texts)
        self.assertEqual(build.validate(SPEC, "119", rows, stats), [])
        keys = [(r["bill_id"], r["version_code"]) for r in rows]
        self.assertEqual(len(keys), len(set(keys)))

    def test_the_added_action_descriptions_are_pinned(self):
        for desc, version in RESOLUTION_ACTION_ADDED:
            self.assertEqual(
                parse.ACTION_VERSION.get(desc.strip().lower()), version,
                desc)
            self.assertIn(version, parse.STAGE_ORDER, desc)

    def test_reported_to_house_part_i_takes_the_rh_text(self):
        # 116hres575 carries this action and an `rh` file. Without the
        # mapping the row would fall back to `eh`, the latest version
        # in the zip, so this test fails if the entry is removed.
        rows, stats = self.rows(
            bills=[("hres", 575, "Providing for consideration",
                    [("Reported to House, Part I", "<p>Part one.</p>")])],
            texts=[("hres", 575, "ih", "Resolved, introduced."),
                   ("hres", 575, "rh", "Resolved, reported."),
                   ("hres", 575, "eh", "Resolved, engrossed.")])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["text_match"], "action")
        self.assertEqual(rows[0]["text_version_code"], "rh")
        self.assertIn("Resolved, reported.", rows[0]["text"])
        self.assertEqual(stats["text_from_action"], 1)
        self.assertEqual(stats["text_from_latest"], 0)

    def test_the_actions_that_name_no_version_fall_back_to_latest(self):
        for desc in RESOLUTION_ACTIONS_UNMAPPED:
            self.assertNotIn(desc.strip().lower(), parse.ACTION_VERSION)
        rows, stats = self.rows(
            bills=[("hjres", 28, "A joint resolution",
                    [(d, f"<p>{d}.</p>")
                     for d in RESOLUTION_ACTIONS_UNMAPPED])],
            texts=[("hjres", 28, "ih", "Resolved, introduced."),
                   ("hjres", 28, "enr", "Resolved, enrolled.")])
        self.assertEqual(len(rows), len(RESOLUTION_ACTIONS_UNMAPPED))
        self.assertEqual(stats["text_from_action"], 0)
        self.assertEqual(stats["text_from_latest"],
                         len(RESOLUTION_ACTIONS_UNMAPPED))
        for r in rows:
            self.assertEqual(r["text_match"], "latest")
            self.assertEqual(r["text_version_code"], "enr")
            self.assertIn("Resolved, enrolled.", r["text"])

    def test_the_relief_exclusion_runs_on_resolutions_too(self):
        rows, stats = self.rows(
            bills=[("sres", 4, "A resolution for the relief of Jane Roe",
                    [("Introduced in Senate", "<p>private</p>")]),
                   ("sres", 5, "A resolution designating a day",
                    [("Introduced in Senate", "<p>public</p>")])],
            texts=[("sres", 5, "is", "Resolved.")])
        self.assertEqual([r["bill_id"] for r in rows], ["119-sres-5"])
        self.assertEqual(stats["excluded_bills"], 1)
        self.assertEqual(stats["excluded_summaries"], 1)
        self.assertEqual(build.validate(SPEC, "119", rows, stats), [])

    def test_a_resolution_version_outside_stage_order_ranks_last(self):
        # `lth`, `as` and `hds` appear on real resolutions and are
        # not in STAGE_ORDER, so `_rank` gives them -1. Left that way
        # by `O-31`: widening STAGE_ORDER would change which text
        # every fallback row in the library picks.
        for code in ("as", "hds", "lth"):
            self.assertEqual(parse._rank(code), -1, code)
            self.assertLess(parse._rank(code), parse._rank("ih"), code)

    def test_a_resolution_with_only_an_odd_version_still_gets_it(self):
        rows, stats = self.rows(
            bills=[("hres", 100, "A resolution",
                    [("House agreed to Senate amendment", "<p>s</p>")])],
            texts=[("hres", 100, "lth", "Resolved, laid on the table.")])
        self.assertEqual(rows[0]["text_version_code"], "lth")
        self.assertEqual(rows[0]["text_match"], "latest")
        self.assertEqual(stats["text_missing"], 0)


class TestTypeAwareActions(unittest.TestCase):
    """The per-type action map, added by `O-33` on 2026-10-10.

    Counted on the real zips that day, 116th to 119th Congress: of
    1,474 `sres` summaries whose action is "Passed Senate", 1,473
    measures hold an `ats` file and 2 an `es` file. The general map
    names `es`, so 1,470 of them fell back to `latest`. Every other
    resolution action either finds the code the general map names or
    names a code that is not in the zip, and keeps `latest`.
    """

    def blobs(self, store):
        return {u: {"bytes": b, "sha256": f"sha-{i}",
                    "retrieved_at": "2026-10-10T08:00:00Z",
                    "size": len(b)}
                for i, (u, b) in enumerate(sorted(store.items()))}

    def rows(self, bills, texts):
        fetch, calls, store = fake_source(bills=bills, texts=texts)
        return parse.rows(SPEC, "119", self.blobs(store))

    def one(self, btype, desc, versions):
        rows, stats = self.rows(
            bills=[(btype, 9, "A measure", [(desc, "<p>s</p>")])],
            texts=[(btype, 9, v, f"Text {v}.") for v in versions])
        self.assertEqual(len(rows), 1)
        return rows[0], stats

    def test_the_type_map_holds_only_the_counted_entry(self):
        self.assertEqual(parse.TYPE_ACTION_VERSION,
                         {"sres": {"passed senate": "ats"}})
        for btype, table in parse.TYPE_ACTION_VERSION.items():
            self.assertIn(btype, SPEC["source"]["types"])
            for desc, code in table.items():
                self.assertEqual(desc, desc.strip().lower())
                self.assertIn(code, parse.STAGE_ORDER, desc)

    def test_sres_passed_senate_takes_the_ats_text(self):
        # The 1,470 rows. Without the entry the general map asks for
        # `es`, finds none, and the row falls back to `latest`.
        r, stats = self.one("sres", "Passed Senate", ["is", "ats"])
        self.assertEqual(r["text_match"], "action")
        self.assertEqual(r["text_version_code"], "ats")
        self.assertIn("Text ats.", r["text"])
        self.assertEqual(stats["text_from_action"], 1)
        self.assertEqual(stats["text_from_latest"], 0)

    def test_sres_passed_senate_prefers_ats_to_es(self):
        # The 2 rows whose zip holds both. Before O-33 they took `es`.
        r, _ = self.one("sres", "Passed Senate", ["is", "es", "ats"])
        self.assertEqual(r["text_match"], "action")
        self.assertEqual(r["text_version_code"], "ats")

    def test_a_type_code_not_in_the_zip_falls_through(self):
        # No `ats` but an `es`: the general map still answers, so no
        # row loses an `action` match it had before O-33.
        r, _ = self.one("sres", "Passed Senate", ["is", "es"])
        self.assertEqual(r["text_match"], "action")
        self.assertEqual(r["text_version_code"], "es")

    def test_sres_introduced_with_only_ats_records_latest(self):
        # 1,190 real rows: the zip holds no `is`. The text the action
        # names does not exist, so the row says `latest`, not `action`.
        r, stats = self.one("sres", "Introduced in Senate", ["ats"])
        self.assertEqual(r["text_match"], "latest")
        self.assertEqual(r["text_version_code"], "ats")
        self.assertEqual(stats["text_from_action"], 0)
        self.assertEqual(stats["text_from_latest"], 1)

    def test_hres_passed_house_still_takes_eh(self):
        # `ath` is in 4 of the 540 real cases, always beside `eh`, so
        # no entry was added for `hres`.
        r, _ = self.one("hres", "Passed House", ["ih", "eh", "ath"])
        self.assertEqual(r["text_match"], "action")
        self.assertEqual(r["text_version_code"], "eh")

    def test_hr_and_s_rows_are_unchanged(self):
        self.assertNotIn("hr", parse.TYPE_ACTION_VERSION)
        self.assertNotIn("s", parse.TYPE_ACTION_VERSION)
        rows, stats = parse.rows(
            SPEC, "119", self.blobs(fake_source()[2]))
        got = {(r["bill_id"], r["summary_action_desc"]):
               (r["text_match"], r["text_version_code"]) for r in rows}
        self.assertEqual(got, {
            ("119-hr-29", "Introduced in House"): ("action", "ih"),
            ("119-s-7", "Introduced in Senate"): ("action", "is"),
            ("119-s-7", "Passed Senate"): ("action", "es"),
        })
        # And every general action picks for hr and s exactly what the
        # general map alone would pick, with `ats` also in the zip.
        for btype in ("hr", "s"):
            for desc, code in parse.ACTION_VERSION.items():
                index = {(btype, 1, v): (v,) for v in (code, "ats")}
                _e, v, how = parse._pick_text(index, btype, 1, desc)
                self.assertEqual((v, how), (code, "action"),
                                 (btype, desc))


class TestStage(unittest.TestCase):

    def test_rank_ignores_trailing_digits(self):
        self.assertEqual(parse._rank("eh1s"), parse._rank("eh1s"))
        self.assertGreater(parse._rank("enr"), parse._rank("ih"))
        self.assertEqual(parse._rank("zz"), -1)

    def test_every_action_maps_to_a_known_stage(self):
        for code in parse.ACTION_VERSION.values():
            self.assertIn(code, parse.STAGE_ORDER)


class TestValidate(unittest.TestCase):

    def good(self):
        rows = [{f["name"]: ("1" if f["type"] != "int32" else 1)
                 for f in SPEC["schema"]}]
        rows[0]["bill_id"] = "119-hr-1"
        rows[0]["version_code"] = "v00"
        rows[0]["title"] = "A Bill"
        stats = {"summary_files": 1, "bills": 1, "excluded_bills": 0}
        return rows, stats

    def test_a_clean_shard_passes(self):
        rows, stats = self.good()
        self.assertEqual(build.validate(SPEC, "119", rows, stats), [])

    def test_no_rows_is_refused(self):
        self.assertTrue(build.validate(SPEC, "119", [], {}))

    def test_missing_stamp_is_refused(self):
        rows, stats = self.good()
        rows[0]["sha256"] = ""
        self.assertIn("without", " ".join(
            build.validate(SPEC, "119", rows, stats)))

    def test_duplicate_key_is_refused(self):
        rows, stats = self.good()
        rows.append(dict(rows[0]))
        stats["summary_files"] = stats["bills"] = 1
        self.assertIn("duplicate", " ".join(
            build.validate(SPEC, "119", rows, stats)))

    def test_count_mismatch_with_the_source_is_refused(self):
        rows, stats = self.good()
        stats["summary_files"] = 9
        self.assertIn("summary file", " ".join(
            build.validate(SPEC, "119", rows, stats)))

    def test_a_row_that_still_matches_the_exclusion_is_refused(self):
        rows, stats = self.good()
        rows[0]["title"] = "For the relief of Jane Roe"
        self.assertIn("exclusion", " ".join(
            build.validate(SPEC, "119", rows, stats)))

    def test_a_field_outside_the_schema_is_refused(self):
        rows, stats = self.good()
        rows[0]["extra"] = "x"
        self.assertIn("not in the schema", " ".join(
            build.validate(SPEC, "119", rows, stats)))


class TestCollect(unittest.TestCase):

    def test_an_optional_source_that_404s_is_skipped(self):
        fetch, calls, store = fake_source()
        srcs = parse.sources(SPEC, "119")
        blobs, seen = build.collect(SPEC, srcs, fetch)
        skipped = [s for s in seen if "skipped" in s]
        ntypes = len(SPEC["source"]["types"])
        # Session 2 is unpublished for every type; the summary zip and
        # session 1 answer for all of them.
        self.assertEqual(len(skipped), ntypes)
        self.assertIn("404", skipped[0]["skipped"])
        self.assertEqual(len(blobs), 2 * ntypes)

    def test_a_required_source_that_fails_stops_the_build(self):
        def fetch(url, ua):
            raise RuntimeError("HTTP 403 closed door")
        with self.assertRaises(RuntimeError):
            build.collect(SPEC, parse.sources(SPEC, "119"), fetch)

    def test_the_declared_user_agent_is_sent(self):
        fetch, calls, store = fake_source()
        build.collect(SPEC, parse.sources(SPEC, "119"), fetch)
        self.assertTrue(calls)
        for _, ua in calls:
            self.assertEqual(ua, SPEC["source"]["user_agent"])
            self.assertIn("github.com", ua)


class TestBuild(unittest.TestCase):

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

    def run_build(self, token="", **kw):
        fetch, calls, store = fake_source()
        published = []

        def publish(spec, out, tok):
            published.append((spec["name"], out, tok))
            return {"published": True, "repo": "acct/us-bills-summaries"}

        m = build.build("us-bills", shard="119", today=TODAY,
                        fetch=fetch, publish=publish, token=token,
                        out_root=self.tmp, **kw)
        return m, published

    def test_end_to_end_without_a_token(self):
        m, published = self.run_build()
        self.assertEqual(m["status"], "OK")
        self.assertEqual(m["rows"], 3)
        self.assertEqual(m["totals"]["excluded_bills"], 1)
        self.assertFalse(published)
        self.assertEqual(m["publish"]["note"],
                         "HF_TOKEN missing: built, not published")
        out = self.tmp / "us-bills"
        self.assertTrue((out / "manifest.json").is_file())
        self.assertTrue((out / "README.md").is_file())
        self.assertTrue((out / "sample.jsonl").is_file())
        self.assertTrue((out / "checksums.txt").is_file())
        files = list((out / "data").iterdir())
        self.assertEqual(len(files), 1)
        self.assertTrue(files[0].name.startswith("us-bills-119."))

    def test_the_manifest_lands_where_the_workflow_commits(self):
        self.run_build()
        self.assertTrue((build.COMMITTED /
                         "library-us-bills-manifest-2026-10-07.json"
                         ).is_file())
        self.assertTrue((build.DATA /
                         "us-bills-manifest-2026-10-07.json").is_file())

    def test_a_token_publishes_once(self):
        m, published = self.run_build(token="not-a-real-token")
        self.assertTrue(m["publish"]["published"])
        self.assertEqual(len(published), 1)
        self.assertEqual(published[0][0], "us-bills")

    def test_no_token_is_written_into_any_output(self):
        secret = "hf_averyobvioussecret"
        self.run_build(token=secret)
        out = self.tmp / "us-bills"
        for p in list(out.rglob("*")) + list(build.COMMITTED.iterdir()):
            if p.is_file():
                self.assertNotIn(secret,
                                 p.read_bytes().decode("utf-8", "replace"),
                                 p.name)

    def test_the_sample_is_capped_and_is_jsonl(self):
        self.run_build()
        lines = (self.tmp / "us-bills" / "sample.jsonl"
                 ).read_text(encoding="utf-8").strip().split("\n")
        self.assertEqual(len(lines), 3)
        self.assertLessEqual(len(lines), build.SAMPLE_ROWS)
        json.loads(lines[0])

    def test_checksums_cover_every_file(self):
        self.run_build()
        out = self.tmp / "us-bills"
        listed = {l.split("  ", 1)[1] for l in
                  (out / "checksums.txt").read_text(
                      encoding="utf-8").strip().split("\n")}
        on_disk = {p.relative_to(out).as_posix()
                   for p in out.rglob("*")
                   if p.is_file() and p.name != "checksums.txt"}
        self.assertEqual(listed, on_disk)

    def test_the_card_states_the_licence_and_the_no_model_text_rule(self):
        self.run_build()
        text = (self.tmp / "us-bills" / "README.md").read_text(
            encoding="utf-8")
        self.assertIn("17 U.S.C. " + chr(0xa7) + "105", text)
        self.assertIn(SPEC["licence"]["proof_url"], text)
        self.assertIn("No model-written text", text)
        self.assertIn("FiscalNote/billsum", text)
        for item in SPEC["not_covered"]:
            self.assertIn(item.split(".")[0], text)

    def test_a_refusal_stops_the_package_and_is_recorded(self):
        # Two summaries of one bill sharing a summary-id: a duplicate
        # key, which the gate must refuse before anything is packaged.
        doc = summary_xml("119", "hr", 29, "A Bill",
                          [("Introduced in House", "<p>one</p>")])
        doc = doc.replace(
            "</item>",
            '<summary summary-id="id119hr29v00" currentChamber="HOUSE">'
            "<action-date>2025-01-03</action-date>"
            "<action-desc>Introduced in House</action-desc>"
            "<summary-text><![CDATA[<p>two</p>]]></summary-text>"
            "</summary></item>")
        src = SPEC["source"]
        store = {
            src["summary_zip"].format(congress="119", type=t):
                zip_bytes({"BILLSUM-119hr29.xml": doc} if t == "hr"
                          else {})
            for t in SPEC["source"]["types"]
        }

        def fetch(url, ua):
            if url not in store:
                raise RuntimeError(f"HTTP 404 {url}")
            return store[url]

        m = build.build("us-bills", shard="119", today=TODAY,
                        fetch=fetch, publish=lambda *a: {},
                        token="", out_root=self.tmp)
        self.assertEqual(m["status"], "REFUSED")
        self.assertIn("duplicate", " ".join(m["refusals"]))
        self.assertFalse((self.tmp / "us-bills" / "data").exists())
        self.assertFalse((self.tmp / "us-bills" / "README.md").exists())
        self.assertFalse((self.tmp / "us-bills" / "checksums.txt"
                          ).exists())
        # The refused build still leaves a manifest, so the next cycle
        # can read why it refused.
        self.assertTrue((build.COMMITTED /
                         "library-us-bills-manifest-2026-10-07.json"
                         ).is_file())

    def test_a_shard_of_resolutions_builds_and_packages(self):
        bills = [(t, n, f"A resolution numbered {n}",
                  [(desc, f"<p>Summary of {t} {n}.</p>")])
                 for t, n, desc, _v in RESOLUTION_CASES]
        texts = [(t, n, v, f"Resolved, {t} {n} body.")
                 for t, n, _desc, v in RESOLUTION_CASES]
        fetch, calls, store = fake_source(bills=bills, texts=texts)
        m = build.build("us-bills", shard="119", today=TODAY,
                        fetch=fetch, publish=lambda *a: {}, token="",
                        out_root=self.tmp)
        self.assertEqual(m["refusals"], [])
        self.assertEqual(m["status"], "OK")
        self.assertEqual(m["rows"], len(RESOLUTION_CASES))
        self.assertEqual(m["shards"][0]["stats"]["summary_files"],
                         m["shards"][0]["stats"]["bills"])
        self.assertEqual(set(m["shards"][0]["by_type"]),
                         set(SPEC["source"]["types"]))
        card = (self.tmp / "us-bills" / "README.md").read_text(
            encoding="utf-8")
        self.assertNotIn("are not in this build", card)
        for t in RESOLUTION_TYPES:
            self.assertIn(t, card, t)

    def test_rotate_builds_one_shard_a_day_and_covers_them_all(self):
        shards = ["116", "117", "118", "119"]
        picked = {build.rotate_shard(shards, date.fromordinal(o))
                  for o in range(TODAY.toordinal(), TODAY.toordinal() + 4)}
        self.assertEqual(picked, set(shards))
        self.assertEqual(build.rotate_shard(shards, TODAY),
                         build.rotate_shard(shards, TODAY))


class TestSpec(unittest.TestCase):

    def test_the_spec_and_the_parser_agree_on_shards(self):
        self.assertEqual(parse.shards(SPEC), ["116", "117", "118", "119"])

    def test_every_source_url_is_govinfo_over_https(self):
        for shard in parse.shards(SPEC):
            for src in parse.sources(SPEC, shard):
                self.assertTrue(src["url"].startswith(
                    "https://www.govinfo.gov/bulkdata/"), src["url"])

    def test_the_key_fields_are_in_the_schema(self):
        fields = {f["name"] for f in SPEC["schema"]}
        for k in SPEC["key"]:
            self.assertIn(k, fields)

    def test_a_name_mismatch_is_refused(self):
        with self.assertRaises(SystemExit):
            build.load_spec("not-a-library")


if __name__ == "__main__":
    unittest.main()
