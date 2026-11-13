"""Offline tests for library 2, `fda-decisions`. No network, no token.

    python -m unittest discover -s engines/library/tests

Every test builds its own zips in memory, so the suite never touches
openFDA and never needs a key. The real-source facts these tests pin,
all read 2026-10-12 from the nine bulk exports the spec names:

- A bulk file is `{"meta": {...}, "results": [...]}`, and `meta`
  carries a `results` OBJECT of its own (`skip`, `limit`, `total`)
  BEFORE the top-level `results` array. `test_meta_results_is_not_the
  _record_array` is that trap.
- `drug/orangebook` records carry no top-level application number:
  all 48,761 have exactly one element in `products`, and the key is
  `products[0].application_type` + `products[0].application_number`
  + `product_number`.
- `device/510k` carries `contact`, the name of a named individual, on
  all 176,299 records (151,655 of them non-empty). `device/recall`
  carries `additional_info_contact`, a name and a direct telephone
  number, on 52,748 of 59,468.
- Not one of the nine endpoints has a GMDN field. The rule is still
  gated, and `test_a_gmdn_field_never_reaches_a_row` proves the gate
  works on a record that has one.
- One record in each of the three enforcement endpoints has an empty
  `recall_number` ("Not Yet Classified"), which is why those three
  endpoints declare a `key_fallback` of `event_id`.
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
import build  # noqa: E402

NAME = "fda-decisions"
SPEC = build.load_spec(NAME)
PARSE = build.load_parser(NAME, SPEC)
TODAY = date(2026, 10, 12)
MANIFEST_URL = SPEC["source"]["manifest"]

# Every endpoint this library must never fetch, from the order's own
# refusals and the spec's not_covered block.
REFUSED = [
    ("drug", "event"), ("device", "event"), ("food", "event"),
    ("animalandveterinary", "event"), ("cosmetic", "event"),
    ("device", "udi"),
]

# One record per endpoint, in the shape openFDA publishes, trimmed to
# the fields these tests exercise.
SAMPLES = {
    "drug/drugsfda": {
        "application_number": "ANDA076191",
        "sponsor_name": "BARR",
        "products": [{"product_number": "002", "brand_name": "X"}],
    },
    "drug/orangebook": {
        "product_number": "001",
        "approval_date": "19861024",
        "products": [{"application_type": "A",
                      "application_number": "018659",
                      "brand_name": "ALLOPURINOL"}],
    },
    "drug/enforcement": {
        "recall_number": "D-0862-2026", "event_id": "99712",
        "recalling_firm": "Sterling Pharmaceutical Services LLC",
        "reason_for_recall": "Lack of Assurance of Sterility",
    },
    "device/510k": {
        "k_number": "K162491", "product_code": "LZA",
        "applicant": "Acme Medical Sdn Bhd",
        "contact": "KUAN KAM HON",
        "decision_date": "2016-12-01",
    },
    "device/classification": {
        "product_code": "JBQ", "device_class": "2",
        "device_name": "Antithrombin Iii Quantitation",
        "regulation_number": "864.7060",
    },
    "device/pma": {
        "pma_number": "P960040", "supplement_number": "S368",
        "product_code": "LWS", "applicant": "Boston Scientific",
    },
    "device/recall": {
        "product_res_number": "Z-0108-2027", "res_event_number": "99817",
        "product_code": "OWB",
        "recalling_firm": "Canon Medical System, USA, INC.",
        "additional_info_contact": "Orlando Tadeo\n714-730-5000",
    },
    "device/enforcement": {
        "recall_number": "Z-3229-2026", "event_id": "99729",
        "recalling_firm": "Medline Industries, LP",
    },
    "food/enforcement": {
        "recall_number": "H-1393-2026", "event_id": "99859",
        "recalling_firm": "Kwok Shing, Inc.",
    },
}


def endpoint_of(shard):
    for e in SPEC["source"]["endpoints"]:
        if e["shard"] == shard:
            return e
    raise KeyError(shard)


def bulk_zip(category, name, records):
    """One openFDA partition zip, built in memory.

    `meta` carries its own `results` object first, exactly as the real
    exports do, so a parser that scans for `"results"` fails here.
    """
    member = f"{category}-{name}-0001-of-0001.json"
    doc = {
        "meta": {
            "disclaimer": "Do not rely on openFDA to make decisions "
                          "regarding medical care.",
            "terms": "https://open.fda.gov/terms/",
            "license": "https://open.fda.gov/license/",
            "last_updated": "2026-10-12",
            "results": {"skip": 0, "limit": len(records),
                        "total": len(records)},
        },
        "results": records,
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(member, json.dumps(doc, indent=2))
    return buf.getvalue()


def manifest_bytes(counts, extra_partitions=None, wrong_file=None,
                   include_refused=True):
    """A download.json in openFDA's shape, for the endpoints given.

    `include_refused` adds the adverse-event endpoints and
    `device/udi`, so a test can prove the build ignores an endpoint
    the source offers.
    """
    res = {}
    for ep, n in counts.items():
        cat, name = ep.split("/", 1)
        url = (f"https://download.open.fda.gov/{cat}/{name}/"
               f"{cat}-{name}-0001-of-0001.json.zip")
        parts = [{"display_name": f"/{ep} data",
                  "file": wrong_file or url,
                  "size_mb": "1.00", "records": n}]
        for i in range(extra_partitions or 0):
            parts.append({"display_name": f"/{ep} data",
                          "file": url.replace("0001-of-0001",
                                              f"000{i + 2}-of-0002"),
                          "size_mb": "1.00", "records": 1})
        res.setdefault(cat, {})[name] = {
            "export_date": "2026-10-12",
            "total_records": n + (extra_partitions or 0),
            "partitions": parts,
        }
    if include_refused:
        for cat, name in REFUSED:
            res.setdefault(cat, {})[name] = {
                "export_date": "2026-10-12",
                "total_records": 26136889,
                "partitions": [{
                    "display_name": f"/{cat}/{name} data",
                    "file": f"https://download.open.fda.gov/{cat}/"
                            f"{name}/{cat}-{name}-0001-of-0371.json.zip",
                    "size_mb": "1.00", "records": 26136889}],
            }
    doc = {"meta": {"last_updated": "2026-10-12",
                    "license": "https://open.fda.gov/license/"},
           "results": res}
    return json.dumps(doc, indent=1).encode("utf-8")


def blob(raw, url, when="2026-10-12T00:00:00Z"):
    import hashlib
    return {"bytes": raw, "sha256": hashlib.sha256(raw).hexdigest(),
            "retrieved_at": when, "size": len(raw)}


def blobs_for(shard, records=None, **kw):
    """The `blobs` dict `rows()` expects, for one shard."""
    ep = endpoint_of(shard)
    endpoint = ep["endpoint"]
    cat, name = endpoint.split("/", 1)
    recs = records if records is not None else [SAMPLES[endpoint]]
    durl = PARSE.partition_url(SPEC, ep)
    data = bulk_zip(cat, name, recs)
    man = manifest_bytes({endpoint: len(recs)}, **kw)
    return {MANIFEST_URL: blob(man, MANIFEST_URL),
            durl: blob(data, durl)}, durl


# A test build must never overwrite a committed manifest, so every
# build in this module writes its manifest to a temporary folder.
_KEEP = {}


def setUpModule():
    _KEEP["committed"] = build.COMMITTED
    build.COMMITTED = Path(tempfile.mkdtemp())


def tearDownModule():
    shutil.rmtree(build.COMMITTED, ignore_errors=True)
    build.COMMITTED = _KEEP["committed"]


class TestStreamingReader(unittest.TestCase):
    """The reader, on the shapes the real files have."""

    def test_meta_results_is_not_the_record_array(self):
        raw = bulk_zip("device", "classification",
                       [SAMPLES["device/classification"]])
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            with z.open(z.namelist()[0]) as fh:
                meta, it = PARSE._records(fh)
                got = list(it)
        # meta's own results object is read as meta, not as records.
        self.assertEqual(meta["results"]["total"], 1)
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["product_code"], "JBQ")

    def test_every_record_is_yielded_across_a_chunk_boundary(self):
        recs = [{"product_code": f"P{i:04d}", "pad": "x" * 500}
                for i in range(400)]
        raw = bulk_zip("device", "classification", recs)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            with z.open(z.namelist()[0]) as fh:
                # A 64-byte window forces hundreds of refills.
                b = PARSE._Buf(fh, chunk=64)
                b.take("{")
                meta = {}
                while True:
                    c = b.peek()
                    if c == ",":
                        b.i += 1
                        continue
                    k = b.value()
                    b.take(":")
                    if k == "results":
                        b.take("[")
                        got = list(PARSE._elements(b))
                        break
                    meta[k] = b.value()
        self.assertEqual(len(got), 400)
        self.assertEqual(got[0]["product_code"], "P0000")
        self.assertEqual(got[-1]["product_code"], "P0399")

    def test_a_multibyte_character_survives_a_split(self):
        recs = [{"product_code": "A", "device_name": "café "
                 + "é" * 300}]
        raw = bulk_zip("device", "classification", recs)
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            with z.open(z.namelist()[0]) as fh:
                b = PARSE._Buf(fh, chunk=7)
                b.take("{")
                while True:
                    k = b.value()
                    b.take(":")
                    if k == "results":
                        b.take("[")
                        got = list(PARSE._elements(b))
                        break
                    b.value()
                    if b.peek() == ",":
                        b.i += 1
        self.assertEqual(got[0]["device_name"],
                         "café " + "é" * 300)

    def test_an_empty_results_array_yields_nothing(self):
        raw = bulk_zip("device", "classification", [])
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            with z.open(z.namelist()[0]) as fh:
                meta, it = PARSE._records(fh)
                self.assertEqual(list(it), [])


class TestPluck(unittest.TestCase):

    def test_a_top_level_name(self):
        self.assertEqual(PARSE.pluck({"a": "1"}, "a"), "1")

    def test_one_indexed_step(self):
        rec = {"products": [{"application_number": "018659"}]}
        self.assertEqual(
            PARSE.pluck(rec, "products[0].application_number"), "018659")

    def test_a_missing_path_is_empty_never_a_guess(self):
        for path in ("b", "products[0].x", "products[3].x", "a.b.c"):
            self.assertEqual(PARSE.pluck({"a": "1"}, path), "")

    def test_a_number_and_a_bool_become_their_text(self):
        self.assertEqual(PARSE.pluck({"n": 7}, "n"), "7")
        self.assertEqual(PARSE.pluck({"b": True}, "b"), "true")
        self.assertEqual(PARSE.pluck({"b": False}, "b"), "false")

    def test_a_container_is_not_squashed_into_a_column(self):
        self.assertEqual(PARSE.pluck({"a": {"b": 1}}, "a"), "")
        self.assertEqual(PARSE.pluck({"a": [1, 2]}, "a"), "")


class TestRowsPerEndpoint(unittest.TestCase):
    """One row per endpoint, through the real spec."""

    def test_the_spec_covers_the_nine_endpoints_the_order_named(self):
        want = {"drug/drugsfda", "drug/orangebook", "drug/enforcement",
                "device/510k", "device/classification", "device/pma",
                "device/recall", "device/enforcement",
                "food/enforcement"}
        got = {e["endpoint"] for e in SPEC["source"]["endpoints"]}
        self.assertEqual(got, want)

    def test_one_row_per_endpoint_through_rows(self):
        for shard in PARSE.shards(SPEC):
            with self.subTest(shard=shard):
                bl, durl = blobs_for(shard)
                rows, stats = PARSE.rows(SPEC, shard, bl)
                self.assertEqual(stats["refusals"], [])
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["endpoint"],
                                 endpoint_of(shard)["endpoint"])
                self.assertEqual(stats["rows_built"], 1)
                self.assertEqual(stats["records_read"], 1)
                self.assertEqual(stats["source_records"], 1)
                self.assertEqual(stats["file_records"], 1)
                self.assertEqual(stats["partition_records"], 1)

    def test_every_endpoints_key_is_the_source_field_not_a_guess(self):
        want = {
            "drug/drugsfda": ("ANDA076191", "application_number"),
            "drug/orangebook": ("A|018659|001",
                                "application_type|application_number"
                                "|product_number"),
            "drug/enforcement": ("D-0862-2026", "recall_number"),
            "device/510k": ("K162491", "k_number"),
            "device/classification": ("JBQ", "product_code"),
            "device/pma": ("P960040|S368",
                           "pma_number|supplement_number"),
            "device/recall": ("Z-0108-2027", "product_res_number"),
            "device/enforcement": ("Z-3229-2026", "recall_number"),
            "food/enforcement": ("H-1393-2026", "recall_number"),
        }
        for shard in PARSE.shards(SPEC):
            ep = endpoint_of(shard)["endpoint"]
            bl, _ = blobs_for(shard)
            rows, _ = PARSE.rows(SPEC, shard, bl)
            with self.subTest(endpoint=ep):
                self.assertEqual(
                    (rows[0]["record_key"], rows[0]["key_fields"]),
                    want[ep])

    def test_every_row_is_stamped(self):
        for shard in PARSE.shards(SPEC):
            bl, durl = blobs_for(shard)
            rows, _ = PARSE.rows(SPEC, shard, bl)
            for r in rows:
                with self.subTest(shard=shard):
                    for s in build.STAMPS:
                        self.assertTrue(str(r.get(s, "")).strip(), s)
                    self.assertEqual(r["source_url"], durl)
                    self.assertEqual(r["sha256"],
                                     bl[durl]["sha256"])
                    self.assertEqual(r["retrieved_at"],
                                     "2026-10-12T00:00:00Z")

    def test_a_row_carries_exactly_the_schema_fields(self):
        fields = {f["name"] for f in SPEC["schema"]}
        for shard in PARSE.shards(SPEC):
            bl, _ = blobs_for(shard)
            rows, _ = PARSE.rows(SPEC, shard, bl)
            with self.subTest(shard=shard):
                self.assertEqual(set(rows[0]), fields)

    def test_the_whole_record_is_kept_as_json_not_flattened(self):
        bl, _ = blobs_for("drug-drugsfda")
        rows, _ = PARSE.rows(SPEC, "drug-drugsfda", bl)
        rec = json.loads(rows[0]["record"])
        self.assertEqual(rec, SAMPLES["drug/drugsfda"])
        self.assertEqual(rows[0]["record_fields"], len(rec))
        self.assertEqual(rows[0]["record_len"], len(rows[0]["record"]))

    def test_the_export_date_comes_from_the_manifest_not_the_spec(self):
        bl, _ = blobs_for("device-classification")
        rows, stats = PARSE.rows(SPEC, "device-classification", bl)
        self.assertEqual(rows[0]["export_date"], "2026-10-12")
        self.assertEqual(stats["export_date"], "2026-10-12")

    def test_the_key_is_unique_over_many_records(self):
        recs = [dict(SAMPLES["device/510k"], k_number=f"K{i:06d}")
                for i in range(500)]
        bl, _ = blobs_for("device-510k", records=recs)
        rows, stats = PARSE.rows(SPEC, "device-510k", bl)
        keys = [tuple(r[k] for k in SPEC["key"]) for r in rows]
        self.assertEqual(len(set(keys)), 500)
        self.assertEqual(build.validate(SPEC, "device-510k", rows,
                                        stats), [])

    def test_a_duplicate_key_is_refused(self):
        recs = [SAMPLES["device/510k"], dict(SAMPLES["device/510k"])]
        bl, _ = blobs_for("device-510k", records=recs)
        rows, stats = PARSE.rows(SPEC, "device-510k", bl)
        bad = build.validate(SPEC, "device-510k", rows, stats)
        self.assertTrue(any("duplicate key" in b for b in bad), bad)

    def test_an_empty_recall_number_falls_back_to_the_event_id(self):
        rec = {"recall_number": "", "event_id": "99892",
               "classification": "Not Yet Classified"}
        for shard in ("drug-enforcement", "device-enforcement",
                      "food-enforcement"):
            bl, _ = blobs_for(shard, records=[rec])
            rows, stats = PARSE.rows(SPEC, shard, bl)
            with self.subTest(shard=shard):
                self.assertEqual(rows[0]["record_key"],
                                 "event_id:99892")
                self.assertEqual(rows[0]["key_fields"], "event_id")
                self.assertEqual(stats["key_from_fallback"], 1)
                self.assertEqual(
                    build.validate(SPEC, shard, rows, stats), [])


class TestPersonalDataIsDropped(unittest.TestCase):
    """The two contact fields never reach a row, and are counted."""

    def test_the_510k_contact_name_is_dropped_and_counted(self):
        bl, _ = blobs_for("device-510k")
        rows, stats = PARSE.rows(SPEC, "device-510k", bl)
        self.assertNotIn("contact", json.loads(rows[0]["record"]))
        self.assertNotIn("KUAN KAM HON", rows[0]["record"])
        self.assertEqual(stats["dropped_fields"], {"contact": 1})
        self.assertEqual(stats["dropped_field_count"], 1)
        self.assertEqual(rows[0]["dropped_fields"], "contact")

    def test_the_recall_contact_name_and_phone_are_dropped(self):
        bl, _ = blobs_for("device-recall")
        rows, stats = PARSE.rows(SPEC, "device-recall", bl)
        rec = json.loads(rows[0]["record"])
        self.assertNotIn("additional_info_contact", rec)
        self.assertNotIn("Orlando Tadeo", rows[0]["record"])
        self.assertNotIn("714-730-5000", rows[0]["record"])
        self.assertEqual(stats["dropped_field_count"], 1)
        self.assertEqual(rows[0]["dropped_fields"],
                         "additional_info_contact")

    def test_the_rest_of_the_record_survives_the_drop(self):
        bl, _ = blobs_for("device-510k")
        rows, _ = PARSE.rows(SPEC, "device-510k", bl)
        rec = json.loads(rows[0]["record"])
        self.assertEqual(rec["k_number"], "K162491")
        self.assertEqual(rec["applicant"], "Acme Medical Sdn Bhd")
        self.assertEqual(rec["decision_date"], "2016-12-01")

    def test_a_dropped_field_is_removed_at_any_depth(self):
        rec = {"k_number": "K1", "contact": "A PERSON",
               "nested": {"contact": "ANOTHER PERSON", "keep": "yes"},
               "list": [{"contact": "A THIRD"}]}
        bl, _ = blobs_for("device-510k", records=[rec])
        rows, stats = PARSE.rows(SPEC, "device-510k", bl)
        self.assertNotIn("PERSON", rows[0]["record"])
        self.assertNotIn("A THIRD", rows[0]["record"])
        self.assertEqual(stats["dropped_fields"], {"contact": 3})
        self.assertIn("yes", rows[0]["record"])

    def test_a_contact_that_came_through_is_refused(self):
        bl, _ = blobs_for("device-510k")
        rows, stats = PARSE.rows(SPEC, "device-510k", bl)
        rec = json.loads(rows[0]["record"])
        rec["contact"] = "KUAN KAM HON"
        rows[0]["record"] = json.dumps(rec, separators=(",", ":"))
        bad = build.validate(SPEC, "device-510k", rows, stats)
        self.assertTrue(
            any("dropped field 'contact'" in b for b in bad), bad)

    def test_the_drop_is_scoped_to_the_endpoint_that_declares_it(self):
        # `contact` is declared for device/510k only. On another
        # endpoint no drop rule applies, so nothing is counted.
        rec = dict(SAMPLES["device/pma"])
        bl, _ = blobs_for("device-pma", records=[rec])
        rows, stats = PARSE.rows(SPEC, "device-pma", bl)
        self.assertEqual(stats["dropped_fields"], {})
        self.assertEqual(rows[0]["dropped_fields"], "")

    def test_every_drop_rule_names_an_endpoint_in_the_spec(self):
        eps = {e["endpoint"] for e in SPEC["source"]["endpoints"]}
        for d in SPEC["drop_fields"]:
            self.assertIn(d["endpoint"], eps)
            self.assertTrue(d["why"].strip())


class TestGmdn(unittest.TestCase):
    """GMDN is licensed content. The rule is gated, not assumed."""

    def test_a_gmdn_field_never_reaches_a_row(self):
        rec = {"k_number": "K1", "gmdn_terms": [
            {"code": "13906", "name": "Pouch", "definition": "A bag."}]}
        bl, _ = blobs_for("device-510k", records=[rec])
        rows, stats = PARSE.rows(SPEC, "device-510k", bl)
        self.assertNotIn("gmdn", rows[0]["record"].lower())
        self.assertNotIn("13906", rows[0]["record"])
        self.assertEqual(stats["gmdn_fields"], 1)

    def test_a_gmdn_field_is_caught_whatever_its_case_or_depth(self):
        rec = {"k_number": "K1",
               "GMDN_Code": "1", "openfda": {"gmdnTerms": ["x"]},
               "l": [{"device_gmdn": "y"}]}
        bl, _ = blobs_for("device-510k", records=[rec])
        rows, stats = PARSE.rows(SPEC, "device-510k", bl)
        self.assertNotIn("gmdn", rows[0]["record"].lower())
        self.assertEqual(stats["gmdn_fields"], 3)

    def test_the_rule_runs_on_every_endpoint_not_just_devices(self):
        for shard in PARSE.shards(SPEC):
            ep = endpoint_of(shard)
            rec = dict(SAMPLES[ep["endpoint"]], gmdn_code="13906")
            bl, _ = blobs_for(shard, records=[rec])
            rows, stats = PARSE.rows(SPEC, shard, bl)
            with self.subTest(shard=shard):
                self.assertEqual(stats["gmdn_fields"], 1)
                self.assertNotIn("gmdn", rows[0]["record"].lower())

    def test_a_gmdn_code_in_the_agencys_free_text_is_left_alone(self):
        # 42 real rows quote a GMDN code inside a firm's own
        # description, 21 in device/recall and 21 in
        # device/enforcement. Those are the agency's record and are
        # not a GMDN term name or definition, so they stay, and the
        # card says so. Dropping a field is not editing a value.
        rec = {"product_res_number": "Z-1",
               "code_info": "Product Code: X843H-56; GMDN: 13906;"}
        bl, _ = blobs_for("device-recall", records=[rec])
        rows, stats = PARSE.rows(SPEC, "device-recall", bl)
        self.assertIn("GMDN: 13906", rows[0]["record"])
        self.assertEqual(stats["gmdn_fields"], 0)

    def test_the_licence_block_records_the_gmdn_carve_out(self):
        lic = SPEC["licence"]
        self.assertEqual(lic["id"], "CC0-1.0")
        self.assertEqual(lic["proof_url"],
                         "https://open.fda.gov/license/")
        self.assertIn("Creative Commons CC0 1.0 Universal",
                      lic["source_statement"])
        self.assertIn("GMDN", lic["gmdn_carve_out"])


class TestRefusedEndpoints(unittest.TestCase):
    """Nothing this library fetches is a refused endpoint."""

    def test_no_refused_endpoint_is_in_the_spec(self):
        got = {e["endpoint"] for e in SPEC["source"]["endpoints"]}
        for cat, name in REFUSED:
            self.assertNotIn(f"{cat}/{name}", got)

    def test_no_shard_asks_for_a_refused_url(self):
        urls = []
        for shard in PARSE.shards(SPEC):
            urls += [s["url"] for s in PARSE.sources(SPEC, shard)]
        for cat, name in REFUSED:
            for u in urls:
                self.assertNotIn(f"/{cat}/{name}/", u)

    def test_a_refused_endpoint_offered_by_the_manifest_is_ignored(self):
        # The manifest these tests build carries drug/event,
        # device/udi and the rest. Not one byte of them is requested.
        asked = []

        def fetch(url, ua):
            asked.append(url)
            if url == MANIFEST_URL:
                return manifest_bytes(
                    {"device/classification": 1})
            return bulk_zip("device", "classification",
                            [SAMPLES["device/classification"]])

        out = Path(tempfile.mkdtemp())
        try:
            m = build.build(NAME, shard="device-classification",
                            today=TODAY, fetch=fetch,
                            publish=lambda *a: {}, token="",
                            out_root=out)
        finally:
            shutil.rmtree(out, ignore_errors=True)
        self.assertEqual(m["status"], "OK")
        for cat, name in REFUSED:
            for u in asked:
                self.assertNotIn(f"/{cat}/{name}/", u)

    def test_a_record_from_a_refused_endpoint_makes_no_row(self):
        # There is no shard for one, so there is no way to ask for
        # rows from one: the endpoint is not in the spec at all.
        for cat, name in REFUSED:
            with self.subTest(endpoint=f"{cat}/{name}"):
                with self.assertRaises(KeyError):
                    PARSE.rows(SPEC, f"{cat}-{name}", {})

    def test_the_card_names_both_exclusions(self):
        nc = " ".join(SPEC["not_covered"]).lower()
        for ep in ("drug/event", "device/event", "food/event",
                   "animalandveterinary/event", "cosmetic/event",
                   "device/udi"):
            self.assertIn(ep, nc)
        self.assertIn("gmdn", nc)
        self.assertIn("personal data", nc)


class TestManifestGates(unittest.TestCase):
    """The parser refuses what only it can see."""

    def test_a_second_partition_is_refused(self):
        bl, _ = blobs_for("device-classification", extra_partitions=1)
        rows, stats = PARSE.rows(SPEC, "device-classification", bl)
        self.assertEqual(rows, [])
        bad = build.validate(SPEC, "device-classification",
                             [{"x": 1}], stats)
        self.assertTrue(any("partition(s)" in b for b in bad), bad)

    def test_a_manifest_naming_another_file_is_refused(self):
        bl, _ = blobs_for(
            "device-classification",
            wrong_file="https://download.open.fda.gov/device/"
                       "classification/device-classification-"
                       "0001-of-0002.json.zip")
        rows, stats = PARSE.rows(SPEC, "device-classification", bl)
        self.assertEqual(rows, [])
        self.assertTrue(any("lists" in r for r in stats["refusals"]),
                        stats["refusals"])

    def test_an_endpoint_missing_from_the_manifest_is_refused(self):
        ep = endpoint_of("device-classification")
        durl = PARSE.partition_url(SPEC, ep)
        bl = {MANIFEST_URL: blob(manifest_bytes({}), MANIFEST_URL),
              durl: blob(bulk_zip("device", "classification", []),
                         durl)}
        rows, stats = PARSE.rows(SPEC, "device-classification", bl)
        self.assertEqual(rows, [])
        self.assertTrue(any("is not in" in r
                            for r in stats["refusals"]))

    def test_a_count_that_disagrees_with_the_source_is_refused(self):
        # openFDA says 2 records; the file holds 1.
        ep = endpoint_of("device-classification")
        durl = PARSE.partition_url(SPEC, ep)
        bl = {
            MANIFEST_URL: blob(
                manifest_bytes({"device/classification": 2}),
                MANIFEST_URL),
            durl: blob(bulk_zip("device", "classification",
                                [SAMPLES["device/classification"]]),
                       durl),
        }
        rows, stats = PARSE.rows(SPEC, "device-classification", bl)
        bad = build.validate(SPEC, "device-classification", rows, stats)
        self.assertTrue(
            any("source_records" in b for b in bad), bad)

    def test_the_spec_checks_all_four_counts(self):
        got = {(c["source"], c["parsed"])
               for c in SPEC["count_checks"]}
        self.assertEqual(got, {
            ("source_records", "rows_built"),
            ("partition_records", "rows_built"),
            ("file_records", "rows_built"),
            ("records_read", "rows_built")})

    def test_a_missing_count_is_refused_not_passed_over(self):
        bl, _ = blobs_for("device-classification")
        rows, stats = PARSE.rows(SPEC, "device-classification", bl)
        stats["file_records"] = None
        bad = build.validate(SPEC, "device-classification", rows, stats)
        self.assertTrue(
            any("no numbers to compare" in b for b in bad), bad)

    def test_a_zip_with_two_members_is_refused(self):
        ep = endpoint_of("device-classification")
        durl = PARSE.partition_url(SPEC, ep)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("a.json", '{"meta":{},"results":[]}')
            z.writestr("b.json", '{"meta":{},"results":[]}')
        bl = {MANIFEST_URL: blob(
                  manifest_bytes({"device/classification": 0}),
                  MANIFEST_URL),
              durl: blob(buf.getvalue(), durl)}
        rows, stats = PARSE.rows(SPEC, "device-classification", bl)
        self.assertEqual(rows, [])
        self.assertTrue(any("member(s)" in r
                            for r in stats["refusals"]))


class TestNoNetworkNoCredential(unittest.TestCase):

    def test_the_parser_imports_nothing_that_does_http_or_io(self):
        import re
        src = (build.LIBRARIES / NAME / "parse.py").read_text(
            encoding="utf-8")
        imported = set()
        for line in src.splitlines():
            m = re.match(r"\s*(?:import|from)\s+([\w.]+)", line)
            if m:
                imported.add(m.group(1).split(".")[0])
        # zipfile reads the bytes build.py already fetched; io is the
        # BytesIO around them. Neither opens a socket or a file.
        self.assertEqual(imported,
                         {"__future__", "codecs", "io", "json", "re",
                          "zipfile"})
        # and the builtin open() is never called (z.open() on a member
        # of an in-memory zip is not file I/O, so match only a bare
        # open).
        self.assertEqual(re.findall(r"(?<![.\w])open\s*\(", src), [])
        for bad in ("urlopen", "requests", "socket", "subprocess",
                    "os.environ", "getenv", "TOKEN"):
            self.assertNotIn(bad, src, bad)

    def test_the_spec_says_no_key_is_needed(self):
        self.assertFalse(SPEC["source"]["needs_key"])

    def test_robots_was_read_for_every_host_touched(self):
        read = {r["url"]: r for r in SPEC["source"]["robots"]}
        self.assertIn("https://download.open.fda.gov/robots.txt", read)
        self.assertIn("https://api.fda.gov/robots.txt", read)
        for r in SPEC["source"]["robots"]:
            self.assertEqual(r["read"], "2026-10-12")
            self.assertIn(r["http"], (200, 403, 404))
            self.assertTrue(r["says"].strip())

    def test_every_url_the_build_asks_for_is_an_openfda_host(self):
        ok = ("https://download.open.fda.gov/", "https://api.fda.gov/")
        for shard in PARSE.shards(SPEC):
            for s in PARSE.sources(SPEC, shard):
                self.assertTrue(s["url"].startswith(ok), s["url"])


class TestEndToEnd(unittest.TestCase):
    """A whole shard, through build, offline."""

    def _fetch(self, counts, recs):
        def fetch(url, ua):
            if url == MANIFEST_URL:
                return manifest_bytes(counts)
            cat, name = url.split("/")[3], url.split("/")[4]
            return bulk_zip(cat, name, recs)
        return fetch

    def test_a_shard_builds_passes_every_gate_and_is_packaged(self):
        recs = [dict(SAMPLES["device/510k"], k_number=f"K{i:06d}")
                for i in range(120)]
        out = Path(tempfile.mkdtemp())
        try:
            m = build.build(
                NAME, shard="device-510k", today=TODAY,
                fetch=self._fetch({"device/510k": 120}, recs),
                publish=lambda *a: {"published": True}, token="",
                out_root=out)
            self.assertEqual(m["refusals"], [])
            self.assertEqual(m["status"], "OK")
            self.assertEqual(m["rows"], 120)
            self.assertEqual(m["totals"]["dropped_field_count"], 120)
            self.assertEqual(m["totals"]["gmdn_fields"], 0)
            self.assertEqual(m["sample_rows"], 100)
            built = out / NAME
            self.assertTrue((built / "README.md").is_file())
            self.assertTrue((built / "checksums.txt").is_file())
            self.assertTrue((built / "manifest.json").is_file())
            data = list((built / "data").iterdir())
            self.assertEqual(len(data), 1)
            self.assertIn("fda-decisions-device-510k", data[0].name)
            # No contact name anywhere in anything the build wrote.
            for p in built.rglob("*"):
                if p.is_file() and p.suffix in (".md", ".jsonl",
                                                ".json", ".txt"):
                    text = p.read_text(encoding="utf-8",
                                       errors="replace")
                    self.assertNotIn("KUAN KAM HON", text, p.name)
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_a_shard_name_never_puts_a_slash_in_a_file_name(self):
        for shard in PARSE.shards(SPEC):
            self.assertNotIn("/", shard)
            self.assertEqual(shard,
                             endpoint_of(shard)["endpoint"].replace(
                                 "/", "-"))

    def test_the_card_states_cc0_links_the_licence_and_names_the_gaps(self):
        recs = [SAMPLES["device/recall"]]
        out = Path(tempfile.mkdtemp())
        try:
            m = build.build(
                NAME, shard="device-recall", today=TODAY,
                fetch=self._fetch({"device/recall": 1}, recs),
                publish=lambda *a: {}, token="", out_root=out)
            text = (out / NAME / "README.md").read_text(
                encoding="utf-8")
        finally:
            shutil.rmtree(out, ignore_errors=True)
        self.assertEqual(m["status"], "OK")
        self.assertIn("license: cc0-1.0", text)
        self.assertIn("https://open.fda.gov/license/", text)
        self.assertIn("Creative Commons CC0 1.0 Universal", text)
        self.assertIn("device/udi", text)
        self.assertIn("drug/event", text)
        self.assertIn("GMDN", text)
        self.assertIn("additional_info_contact", text)
        self.assertIn("No model-written text", text)
        # The card names the metered feed of this data (O-38).
        self.assertIn("https://apify.com/1mp/fda-change-feed", text)
        # The us-bills card must not be what an FDA build renders.
        self.assertNotIn("billsum", text.lower())
        self.assertNotIn("Congress", text)

    def test_the_manifest_lands_where_the_workflow_commits(self):
        recs = [SAMPLES["device/classification"]]
        out = Path(tempfile.mkdtemp())
        try:
            build.build(
                NAME, shard="device-classification", today=TODAY,
                fetch=self._fetch({"device/classification": 1}, recs),
                publish=lambda *a: {}, token="", out_root=out)
            p = (build.COMMITTED
                 / f"library-{NAME}-manifest-{TODAY.isoformat()}.json")
            self.assertTrue(p.is_file())
            self.assertEqual(
                json.loads(p.read_text(encoding="utf-8"))["status"],
                "OK")
        finally:
            shutil.rmtree(out, ignore_errors=True)

    def test_no_token_is_written_into_any_output(self):
        recs = [SAMPLES["device/classification"]]
        out = Path(tempfile.mkdtemp())
        secret = "hf_notarealtokenjustforthistest"
        try:
            build.build(
                NAME, shard="device-classification", today=TODAY,
                fetch=self._fetch({"device/classification": 1}, recs),
                publish=lambda s, o, t: {"published": True},
                token=secret, out_root=out)
            for p in (out / NAME).rglob("*"):
                if p.is_file():
                    self.assertNotIn(
                        secret, p.read_text(encoding="utf-8",
                                            errors="replace"), p.name)
        finally:
            shutil.rmtree(out, ignore_errors=True)


class TestUsBillsIsUnchanged(unittest.TestCase):
    """The engine changes O-32 made must not reach library 1."""

    def test_us_bills_declares_none_of_the_new_spec_keys(self):
        bills = build.load_spec("us-bills")
        for k in ("count_checks", "drop_fields", "totals"):
            self.assertNotIn(k, bills)

    def test_the_default_totals_are_still_the_seven_us_bills_had(self):
        bills = build.load_spec("us-bills")
        self.assertEqual(
            set(bills.get("totals", [
                "text_from_action", "text_from_latest", "text_missing",
                "excluded_bills", "excluded_summaries",
                "summary_files", "bills"])),
            {"text_from_action", "text_from_latest", "text_missing",
             "excluded_bills", "excluded_summaries", "summary_files",
             "bills"})

    def test_the_bill_gate_is_inert_without_a_bills_count(self):
        # fda rows have no bill_id. Before O-32 this raised KeyError.
        bl, _ = blobs_for("device-classification")
        rows, stats = PARSE.rows(SPEC, "device-classification", bl)
        self.assertEqual(build.validate(SPEC, "device-classification",
                                        rows, stats), [])

    def test_the_fda_parser_supplies_its_own_card(self):
        self.assertTrue(callable(getattr(PARSE, "card", None)))
        bills = build.load_parser("us-bills",
                                  build.load_spec("us-bills"))
        self.assertIsNone(getattr(bills, "card", None))


if __name__ == "__main__":
    unittest.main()
