#!/usr/bin/env python3
"""Library 2: FDA regulatory decisions, from openFDA bulk exports.

`engines/library/build.py` imports this module and calls, in order,
`shards`, `sources`, `rows` and (optionally) `card`. This file knows
about openFDA's bulk JSON and nothing else: it does no HTTP, writes
no file and holds no credential, so every function here is testable
offline on bytes.

One shard is one administrative endpoint. Each of the nine endpoints
in the spec is a single partition, so one shard is one zip plus
`api.fda.gov/download.json`, which is read for that endpoint's
`export_date` and `total_records` rather than trusted from the spec.

**Why this file streams.** `device/510k` is 1,619.5 MB of JSON
uncompressed and `device/recall` is 1,494.0 MB (read 2026-10-12).
`json.load` on either one was killed by the OOM killer in an 8 GB
container. `_records` parses the top-level object key by key and
yields one record at a time: 176,299 records of `device/510k` in
9.5 seconds at a 24 MB peak. It cannot use a scan for `"results"`
either, because openFDA's own `meta` block contains a `results`
object (`skip`, `limit`, `total`) before the top-level `results`
array.

**Schema.** openFDA's records are deeply nested and differ per
endpoint, so this parser guesses no columns. It keeps each
endpoint's identifying fields as typed columns, copied by name, and
the whole record as one compact-JSON string in `record`. `docs/42`
section 3 step 3: code only, no model text. Every string in a row is
copied from the source bytes or computed from them here.

**Two kinds of field never reach a row**, and both are counted:

- `contact` on `device/510k` (151,655 of 176,299 records) and
  `additional_info_contact` on `device/recall` (52,748 of 59,468)
  are the name of a named individual, the second with a direct
  telephone number. Personal data is out on `docs/42` section 5 and
  on rail 1. The decision record stays; the person does not.
- Any field at any depth whose name contains `gmdn`.
  `open.fda.gov/license` (read 2026-10-12) says GMDN content needs a
  licence from The GMDN Agency for commercial use and "for AI
  training purposes". **Measured 2026-10-12: none of the nine
  endpoints has a GMDN field**, so the rule drops nothing today. It
  stays because it is the gate, not an estimate. 42 rows quote a
  GMDN code inside a firm's own free text; see `card()`.

Counts read from `api.fda.gov/download.json` on 2026-10-12, and
every one checked against the zip's own `meta.results.total`:
`drug/drugsfda` 29,383; `drug/orangebook` 48,761;
`drug/enforcement` 18,002; `device/510k` 176,299;
`device/classification` 7,094; `device/pma` 57,238;
`device/recall` 59,468; `device/enforcement` 40,113;
`food/enforcement` 29,471. Two of those moved since the order was
written on 2026-10-07: `drug/drugsfda` was 29,379 and
`device/recall` was 59,344. Both re-exported on 2026-10-09. This is
why the count a shard is validated against is read live, per build,
and never taken from the spec.
"""
from __future__ import annotations

import codecs
import io
import json
import re
import zipfile

GMDN = re.compile(r"gmdn", re.I)
# The metered feed of this data (O-38), public 2026-10-16.
FEED_URL = "https://apify.com/1mp/fda-change-feed"
CHUNK = 1 << 20
_DEC = json.JSONDecoder()


# ------------------------------------------------- streaming JSON read

class _Buf:
    """A refillable text window over a byte stream."""

    def __init__(self, fh, chunk: int = CHUNK):
        self.fh = fh
        self.chunk = chunk
        self.dec = codecs.getincrementaldecoder("utf-8")()
        self.s = ""
        self.i = 0
        self.eof = False

    def more(self) -> bool:
        if self.eof:
            return False
        raw = self.fh.read(self.chunk)
        if not raw:
            self.s += self.dec.decode(b"", True)
            self.eof = True
            return False
        self.s += self.dec.decode(raw)
        return True

    def compact(self) -> None:
        if self.i > CHUNK:
            self.s = self.s[self.i:]
            self.i = 0

    def ws(self) -> None:
        while True:
            while self.i < len(self.s) and self.s[self.i] in " \t\r\n":
                self.i += 1
            if self.i < len(self.s) or not self.more():
                return

    def peek(self) -> str:
        self.ws()
        return self.s[self.i] if self.i < len(self.s) else ""

    def take(self, ch: str) -> None:
        if self.peek() != ch:
            raise ValueError(f"openFDA JSON: expected {ch!r}, saw "
                             f"{self.s[self.i:self.i + 24]!r}")
        self.i += 1

    def value(self):
        """One JSON value at the cursor, refilling until it is whole."""
        self.ws()
        while True:
            try:
                v, end = _DEC.raw_decode(self.s, self.i)
            except ValueError:
                if self.more():
                    continue
                raise
            self.i = end
            self.compact()
            return v


def _records(fh):
    """`(meta, iterator of records)` for an openFDA bulk document.

    Walks the top-level object key by key, so the `results` object
    inside `meta` is never mistaken for the top-level `results`
    array, and no record list is ever held whole in memory.
    """
    b = _Buf(fh)
    b.take("{")
    meta: dict = {}
    while True:
        c = b.peek()
        if c == "}":
            b.i += 1
            return meta, iter(())
        if c == ",":
            b.i += 1
            continue
        key = b.value()
        b.take(":")
        if key == "results":
            b.take("[")
            return meta, _elements(b)
        val = b.value()
        if key == "meta":
            meta = val


def _elements(b: _Buf):
    if b.peek() == "]":
        b.i += 1
        return
    while True:
        yield b.value()
        c = b.peek()
        if c == ",":
            b.i += 1
            continue
        if c == "]":
            b.i += 1
            return
        raise ValueError(f"openFDA JSON: expected ',' or ']', saw {c!r}")


# ----------------------------------------------------- field selection

_IDX = re.compile(r"^(.*)\[(\d+)\]$")


def pluck(rec, path: str) -> str:
    """`a.b[0].c` out of a record, as a string. '' when absent.

    The only path shapes the spec uses: a top-level name, and one
    indexed step into a list of objects. Anything missing gives '',
    never a guess.
    """
    cur = rec
    for part in path.split("."):
        if cur is None:
            return ""
        m = _IDX.match(part)
        idx = None
        if m:
            part, idx = m.group(1), int(m.group(2))
        if not isinstance(cur, dict) or part not in cur:
            return ""
        cur = cur[part]
        if idx is not None:
            if not isinstance(cur, list) or len(cur) <= idx:
                return ""
            cur = cur[idx]
    if cur is None or isinstance(cur, (dict, list)):
        return ""
    if cur is True:
        return "true"
    if cur is False:
        return "false"
    return str(cur)


def strip_fields(rec, drop: set, counts: dict):
    """A copy of `rec` with every dropped field gone, at every depth.

    `drop` is a set of lowercased field names. A field whose name
    contains `gmdn` goes too. Each removal is counted by name, so the
    card can state how many and the gate can prove none came through.
    """
    if isinstance(rec, dict):
        out = {}
        for k, v in rec.items():
            kl = k.lower()
            if kl in drop:
                counts[k] = counts.get(k, 0) + 1
                continue
            if GMDN.search(k):
                counts[f"gmdn:{k}"] = counts.get(f"gmdn:{k}", 0) + 1
                continue
            out[k] = strip_fields(v, drop, counts)
        return out
    if isinstance(rec, list):
        return [strip_fields(v, drop, counts) for v in rec]
    return rec


# --------------------------------------------------- the three hooks

def _endpoints(spec: dict) -> dict:
    return {e["shard"]: e for e in spec["source"]["endpoints"]}


def shards(spec: dict) -> list[str]:
    """One shard per endpoint, in the order the spec lists them."""
    return [e["shard"] for e in spec["source"]["endpoints"]]


def partition_url(spec: dict, ep: dict) -> str:
    cat, name = ep["endpoint"].split("/", 1)
    return spec["source"]["partition"].format(
        category=cat, name=name,
        stem=f"{cat}-{name}", part="0001-of-0001")


def sources(spec: dict, shard: str) -> list[dict]:
    """The manifest and the one partition this endpoint needs."""
    ep = _endpoints(spec)[shard]
    return [
        {"role": "manifest", "url": spec["source"]["manifest"]},
        {"role": "data", "url": partition_url(spec, ep)},
    ]


def rows(spec: dict, shard: str, blobs: dict) -> tuple[list[dict], dict]:
    """Rows for one endpoint, and the counts the validator checks."""
    ep = _endpoints(spec)[shard]
    endpoint = ep["endpoint"]
    cat, name = endpoint.split("/", 1)
    murl = spec["source"]["manifest"]
    durl = partition_url(spec, ep)
    drop = {d["field"].lower() for d in spec.get("drop_fields", [])
            if d["endpoint"] == endpoint}
    id_map = ep.get("ids", {})
    key_paths = ep["key"]
    key_fields = "|".join(p.split(".")[-1].split("[")[0]
                          for p in key_paths)

    stats = {
        "endpoint": endpoint, "shard": shard,
        "export_date": "", "source_records": None, "file_records": None,
        "partition_records": None, "records_read": 0, "rows_built": 0,
        "key_from_fallback": 0, "dropped_fields": {},
        "dropped_field_count": 0, "gmdn_fields": 0,
        "refusals": [],
    }

    mblob = blobs.get(murl)
    dblob = blobs.get(durl)
    if mblob is None:
        stats["refusals"].append(f"{murl} was not collected")
        return [], stats
    if dblob is None:
        stats["refusals"].append(f"{durl} was not collected")
        return [], stats

    man = json.loads(mblob["bytes"].decode("utf-8"))
    entry = (man.get("results", {}).get(cat, {}) or {}).get(name)
    if not entry:
        stats["refusals"].append(
            f"{endpoint} is not in {murl}")
        return [], stats
    parts = entry.get("partitions") or []
    stats["export_date"] = entry.get("export_date", "")
    stats["source_records"] = entry.get("total_records")
    if len(parts) != 1:
        stats["refusals"].append(
            f"{endpoint} has {len(parts)} partition(s) in {murl}, and "
            f"this library is built for single-partition endpoints "
            f"only")
        return [], stats
    if parts[0].get("file") != durl:
        stats["refusals"].append(
            f"{endpoint}: {murl} lists {parts[0].get('file')!r} but "
            f"this build fetched {durl!r}")
        return [], stats
    stats["partition_records"] = parts[0].get("records")

    out: list[dict] = []
    dropped: dict = {}
    with zipfile.ZipFile(io.BytesIO(dblob["bytes"])) as z:
        members = z.namelist()
        if len(members) != 1:
            stats["refusals"].append(
                f"{endpoint}: {len(members)} member(s) in the partition "
                f"zip, expected 1")
            return [], stats
        stats["partition_file"] = members[0]
        with z.open(members[0]) as fh:
            meta, it = _records(fh)
            stats["file_records"] = (
                meta.get("results", {}) or {}).get("total")
            stats["file_last_updated"] = meta.get("last_updated", "")
            for rec in it:
                stats["records_read"] += 1
                kv = [pluck(rec, p) for p in key_paths]
                kf = key_fields
                if not any(kv):
                    fb = ep.get("key_fallback") or []
                    kv = [pluck(rec, p) for p in fb]
                    kf = "|".join(p.split(".")[-1].split("[")[0]
                                  for p in fb)
                    kv = [f"{kf}:{'|'.join(kv)}"] if any(kv) else kv
                    stats["key_from_fallback"] += 1
                clean = strip_fields(rec, drop, dropped)
                blob = json.dumps(clean, ensure_ascii=False,
                                  separators=(",", ":"), sort_keys=True)
                row = {
                    "endpoint": endpoint,
                    "record_key": "|".join(kv),
                    "key_fields": kf,
                    "export_date": stats["export_date"],
                    "record": blob,
                    "record_fields": len(clean) if isinstance(
                        clean, dict) else 0,
                    "record_len": len(blob),
                    "dropped_fields": "|".join(
                        sorted(d["field"] for d in
                               spec.get("drop_fields", [])
                               if d["endpoint"] == endpoint
                               and d["field"] in rec)),
                    "partition_file": members[0],
                    "manifest_url": murl,
                    "source_url": durl,
                    "retrieved_at": dblob["retrieved_at"],
                    "sha256": dblob["sha256"],
                }
                for col, path in id_map.items():
                    row[col] = pluck(rec, path)
                for f in spec["schema"]:
                    row.setdefault(f["name"], "" if f["type"] != "int32"
                                   else 0)
                out.append(row)

    stats["rows_built"] = len(out)
    stats["dropped_fields"] = dropped
    stats["dropped_field_count"] = sum(
        v for k, v in dropped.items() if not k.startswith("gmdn:"))
    stats["gmdn_fields"] = sum(
        v for k, v in dropped.items() if k.startswith("gmdn:"))
    return out, stats


# ---------------------------------------------------------- the card

def card(spec: dict, manifest: dict) -> str:
    """The dataset card. Every number here comes from the manifest."""
    lic = spec["licence"]
    src = spec["source"]
    rows_n = manifest["rows"]
    eps = {e["endpoint"]: e for e in src["endpoints"]}
    L = [
        "---",
        "license: cc0-1.0",
        f"license_link: {lic['proof_url']}",
        f"pretty_name: {spec['title']}",
        "language:",
        "- en",
        "task_categories:",
        "- text-classification",
        "- text-retrieval",
        "size_categories:",
        "- " + ("100K<n<1M" if rows_n >= 100000 else
                "10K<n<100K" if rows_n >= 10000 else "1K<n<10K"),
        "---",
        "",
        f"# {spec['title']}",
        "",
        f"{rows_n:,} rows. One row is one administrative record of one "
        "FDA regulatory",
        "decision or enforcement action, exactly as openFDA exports it. "
        "Built by",
        f"code from {src['host']} bulk exports on {manifest['built']}.",
        "",
        "## Why this exists",
        "",
        "`hazyresearch/based-fda` is the FDA dataset on the Hub with the "
        "most",
        "downloads. It has not changed since 2024-05-19 and states no "
        "licence.",
        "This library is rebuilt from openFDA's own daily bulk exports, "
        "states",
        "CC0, and stamps every row with the file it came from, the time "
        "those",
        "bytes were fetched and that file's SHA-256, so any row can be "
        "checked",
        "against the agency's own copy.",
        "",
        "## Only the changes, metered",
        "",
        "This library is the full copy, rebuilt each day. To get only "
        "the records",
        "that are new or changed since a date, on eight of these "
        "endpoints, with a",
        "`firm_key` that joins one firm across them, use the FDA Change "
        "Feed on the",
        f"Apify Store: {FEED_URL}. It is priced per 1,000 rows "
        "and comes from the",
        "same project.",
        "",
        "## What is in it",
        "",
        "| Endpoint | Rows | openFDA `total_records` | In the file | "
        "Export date | Key |",
        "|---|---|---|---|---|---|",
    ]
    for s in manifest["shards"]:
        st = s["stats"]
        e = eps.get(st.get("endpoint"), {})
        L.append(
            f"| `{st.get('endpoint')}` | {s['rows']:,} | "
            f"{(st.get('source_records') or 0):,} | "
            f"{(st.get('file_records') or 0):,} | "
            f"{st.get('export_date', '')} | "
            f"`{'` + `'.join(e.get('key', []))}` |")
    L += [
        "",
        f"Every endpoint above is a single partition, so the whole "
        f"library is one",
        f"zip per endpoint and is rebuilt in full every day.",
        "",
        "## Columns",
        "",
        "| Column | Type | What it is |",
        "|---|---|---|",
    ]
    for f in spec["schema"]:
        L.append(f"| `{f['name']}` | {f['type']} | "
                 f"{f.get('note', '')} |")
    L += [
        "",
        "openFDA's records are deeply nested and the shape differs per "
        "endpoint,",
        "so this library does not flatten them into guessed columns. "
        "The whole",
        "record is in `record`, as compact JSON with its keys sorted; "
        "the typed",
        "columns beside it are copies of fields of that same name in "
        "the source,",
        "so a filter on `k_number` or `recall_number` needs no JSON "
        "parsing.",
        "A typed column is `\"\"` on an endpoint that has no such "
        "field.",
        "",
        "`record_key` is unique within an endpoint, and `endpoint` "
        "plus `record_key`",
        "is unique across the library. `key_fields` names the source "
        "field or fields",
        "it was taken from.",
        "",
        "## Licence",
        "",
        f"**{lic['name']}.** {lic['proof']}",
        "",
        f"> {lic['source_statement']}",
        "",
        f"Read at {lic['proof_url']} on {lic['proof_read']}.",
        "",
        "## What this does NOT cover, and why",
        "",
    ]
    for item in spec.get("not_covered", []):
        L.append(f"- {item}")
    drops = {}
    for s in manifest["shards"]:
        for k, v in (s["stats"].get("dropped_fields") or {}).items():
            drops[f"{s['stats'].get('endpoint')} {k}"] = v
    L += [
        "",
        "## Fields dropped from the records that are here",
        "",
        "Two kinds of field never reach a row. Both are counted by the "
        "build, and",
        "the build refuses the shard if one came through anyway.",
        "",
    ]
    for d in spec.get("drop_fields", []):
        n = drops.get(f"{d['endpoint']} {d['field']}", 0)
        L.append(f"- `{d['endpoint']}` `{d['field']}`: {d['why']} "
                 f"Dropped from {n:,} records in this build.")
    gm = sum(v for k, v in drops.items() if " gmdn:" in k)
    L += [
        f"- Any field at any depth whose name contains `gmdn`. "
        f"`open.fda.gov/license`",
        f"  says GMDN content needs a licence from The GMDN Agency for "
        f"commercial use",
        f"  and for AI training. {gm:,} such field(s) were found in "
        f"this build.",
        "",
        "A caution the build cannot fix: 42 rows quote a GMDN code "
        "number inside a",
        "firm's own free text, 21 in `device/recall` and 21 in "
        "`device/enforcement`,",
        "in `code_info` (20 of them), `product_description` (18) and "
        "`reason_for_recall`",
        "(4), as read on 2026-10-12. Those strings are the agency's "
        "record of what",
        "the firm",
        "reported, not a GMDN term name or definition, and they are "
        "left exactly",
        "as the agency published them. If you need the dataset free of "
        "any GMDN",
        "mention, filter `record` on `GMDN`.",
        "",
        "## Provenance",
        "",
        f"Each endpoint's `export_date` and `total_records` are read "
        f"from",
        f"`{src['manifest']}` at build time, never from a stored "
        f"number, and the",
        "row count of every shard is checked against both that number "
        "and the",
        "count in the exported file's own `meta.results.total`. A shard "
        "whose",
        "counts disagree is refused and not published.",
        "",
        "| File | Bytes | SHA-256 |",
        "|---|---|---|",
    ]
    for f in manifest["sources"]:
        if "sha256" in f:
            L.append(f"| {f['url'].rsplit('/', 1)[-1]} | "
                     f"{f['size']:,} | `{f['sha256'][:16]}...` |")
    L += [
        "",
        "## No model-written text",
        "",
        "No row holds text written by a language model. Every string in "
        "every row",
        "is copied from the openFDA export listed above, or computed "
        "from it (a",
        "length, a field count, a joined key) by the build code. Models "
        "wrote the",
        "build code; they wrote none of the data.",
        "",
        "## Disclaimer, from openFDA",
        "",
        "> Do not rely on openFDA to make decisions regarding medical "
        "care. While",
        "> we make every effort to ensure that data is accurate, you "
        "should assume",
        "> all results are unvalidated.",
        "",
        "These rows are the agency's administrative records, unchanged. "
        "They are",
        "not advice, and nothing here is a reading of what any label, "
        "approval or",
        "recall means.",
        "",
        "## How it was built",
        "",
        "`engines/library/build.py` and "
        "`libraries/fda-decisions/parse.py` in the",
        "project that publishes this dataset. The build refuses to "
        "package a shard",
        "with a duplicate key, a missing stamp, a field outside the "
        "schema, a",
        "dropped field that came through, or a row count that disagrees "
        "with",
        "openFDA's own.",
        "",
    ]
    return "\n".join(L)
