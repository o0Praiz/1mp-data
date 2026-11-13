#!/usr/bin/env python3
"""Library 1: US bills and resolutions and their official summaries,
from govinfo bulk data.

`engines/library/build.py` imports this module and calls, in order,
`shards`, `sources` and `rows`. This file knows about govinfo's XML
and nothing else: it does no HTTP, writes no file and holds no
credential, so every function here is testable offline on bytes.

Two sources per Congress and measure type:

- `BILLSUM-<congress>-<type>.zip`, one XML file per bill, each with
  one `<item>` and one or more `<summary>`. A row is one summary.
- `BILLS-<congress>-<session>-<type>.zip`, one XML file per bill
  VERSION, named `BILLS-119hr29ih.xml`, where `ih` is the version.

The summary says which action it describes ("Introduced in House"),
and `ACTION_VERSION` maps that to the version code whose text the
summary is about. When the action does not map, or that version is
not in the zip, the row takes the latest version by `STAGE_ORDER`
and says so in `text_match`.

Counts read 2026-10-07: 3,350 summary files and 3,424 summaries in
BILLSUM-119-hr.zip; 8,042 text files in BILLS-119-1-hr.zip. The full
four-Congress build over `hr` and `s` was 43,549 summary files and
49,935 rows.

`O-31` added the six resolution types on 2026-10-09. Checked on the
real zips that day: `SUM_FILE` and `BILL_FILE` match every one of the
8,971 resolution BILLSUM members and every one of the 13,368
resolution BILLS members, and split the type correctly in each, with
zero unmatched on either. The non-greedy `[a-z]+?` type group starts
at one letter and the engine backtracks it outward until the digits
group that follows can match, so `hjres`, `sjres`, `hconres` and
`sconres` come out whole. The XML shape is identical to a bill: one
`<item>` per file with `measure-type`, `measure-number`, `congress`,
`originChamber`, `orig-publish-date` and `update-date`, a `<title>`,
and `<summary>` elements each carrying `summary-id`,
`currentChamber`, `<action-date>`, `<action-desc>` and a CDATA
`<summary-text>`. All 8,971 files, all 11,582 summaries, no
exceptions.

Three version codes appear on resolutions that `STAGE_ORDER` does
not list: `lth` 10 times, `as` once and `hds` once, out of 13,368
members. `_rank` returns -1 for all three, so they are only ever
chosen as the latest text when nothing else is in the zip. Left
alone on purpose: widening `STAGE_ORDER` would change which text
every fallback row in the library picks, and `O-31` changes one
thing.

`O-33` made the action map type-aware on 2026-10-10:
`TYPE_ACTION_VERSION` is read first, and today holds one entry,
`sres` "passed senate" to `ats`. `hr` and `s` have no entry, so their
rows are unchanged by construction.

No row holds model-written text. Every string in a row is either
copied from the source bytes, or computed from them by the code in
this file. `docs/42` section 2.3.
"""
from __future__ import annotations

import html
import re
import zipfile
from io import BytesIO

# govinfo bill version codes, earliest stage first. Used only to pick
# the latest available text when the summary's action does not map.
STAGE_ORDER = (
    "ih", "is", "ipch", "ipcs", "rh", "rs", "rfh", "rfs", "rch", "rcs",
    "rhuc", "rsuc", "pch", "pcs", "cph", "cps", "eh", "es", "eah",
    "eas", "rah", "ras", "rdh", "rds", "ath", "ats", "enr",
)

# What a summary's <action-desc> says, lowercased, to the version code
# of the bill text that action produced. A description not in this map
# falls back to the latest version, and the row records that.
ACTION_VERSION = {
    "introduced in house": "ih",
    "introduced in senate": "is",
    "reported to house": "rh",
    "reported in house": "rh",
    "reported to senate": "rs",
    "reported in senate": "rs",
    "passed house": "eh",
    "passed house amended": "eh",
    "passed senate": "es",
    "passed senate amended": "es",
    "public law": "enr",
    "enrolled": "enr",
    # Added by O-31 from the six resolution types. Counted across all
    # 11,582 resolution summaries of the 116th to 119th Congress on
    # 2026-10-09: this is the only unmapped description that names a
    # bill text version. 116hres575 carries the action and an `rh`
    # file, so the mapping is checked, not guessed. The other three
    # unmapped descriptions name no version and keep falling back:
    # "House agreed to Senate amendment" (1), "Senate agreed to House
    # amendment" (1) and "Conference report filed in House" (1) are
    # actions on an amendment or on a separate CRPT document, not on a
    # bill version.
    "reported to house, part i": "rh",
}

# Consulted before ACTION_VERSION, for one measure type at a time.
# Added by O-33 on 2026-10-10. Lives here, not in library.json,
# because it is knowledge of govinfo's version codes, which is this
# file's job; the spec says what to build, not how to read the XML.
#
# Counted, not guessed: across the 116th to 119th Congress, every
# resolution summary's <action-desc> against the version codes really
# present in that measure's BILLS members. A Senate simple resolution
# that "Passed Senate" is agreed to, not engrossed: of 1,474 such
# summaries, 1,473 measures hold an `ats` file and 2 an `es` file.
# Every other resolution action either already finds the code
# ACTION_VERSION names, or names a code that is not in the zip at all
# and so honestly keeps the latest text. No such count supports a
# second entry: `hres` "Passed House" finds `eh` in 536 of 540, and
# `ath` is present in only 4 of them, all beside `eh`.
TYPE_ACTION_VERSION = {
    "sres": {
        "passed senate": "ats",
    },
}

ITEM = re.compile(r"<item\b(?P<attrs>[^>]*)>(?P<body>.*?)</item>", re.S)
SUMMARY = re.compile(
    r"<summary\b(?P<attrs>[^>]*)>(?P<body>.*?)</summary>", re.S)
ATTR = re.compile(r"([\w:-]+)\s*=\s*\"([^\"]*)\"")
TITLE = re.compile(r"<title>(.*?)</title>", re.S)
ACTION_DATE = re.compile(r"<action-date>(.*?)</action-date>", re.S)
ACTION_DESC = re.compile(r"<action-desc>(.*?)</action-desc>", re.S)
SUM_TEXT = re.compile(r"<summary-text>(.*?)</summary-text>", re.S)
CDATA = re.compile(r"^\s*<!\[CDATA\[(.*?)\]\]>\s*$", re.S)

BILL_FILE = re.compile(
    r"^BILLS-(?P<congress>\d+)(?P<type>[a-z]+?)(?P<number>\d+)"
    r"(?P<version>[a-z][a-z0-9]*)\.xml$")
SUM_FILE = re.compile(
    r"^BILLSUM-(?P<congress>\d+)(?P<type>[a-z]+?)(?P<number>\d+)\.xml$")

BLOCK_END = re.compile(
    r"</(p|li|ul|ol|div|h[1-6]|tr|table|blockquote)>", re.I)
BR = re.compile(r"<br\s*/?>", re.I)
TAG = re.compile(r"<[^>]*>")
# chr(0xa0) is the non-breaking space. Written as a call, not
# as an escape or a literal: the GitHub connector decodes
# \\uXXXX escapes on push (O-16), so an escape here would not
# survive a round trip byte for byte.
SPACES = re.compile("[ \t\r\f\v" + chr(0xa0) + "]+")
BLANKS = re.compile(r"\n{3,}")


def shards(spec: dict) -> list[str]:
    """One shard per Congress, oldest first."""
    return [str(c) for c in spec["source"]["congresses"]]


def sources(spec: dict, shard: str) -> list[dict]:
    """Every file this shard needs, with the role the parser gives it."""
    s = spec["source"]
    out = []
    for t in s["types"]:
        out.append({
            "role": "summary", "type": t, "session": None,
            "url": s["summary_zip"].format(congress=shard, type=t),
        })
        for sess in s["sessions"]:
            out.append({
                "role": "text", "type": t, "session": sess,
                "url": s["text_zip"].format(
                    congress=shard, session=sess, type=t),
                "optional": True,
            })
    return out


def strip_html(raw: str) -> str:
    """HTML to text, paragraphs kept as blank lines. Code only."""
    m = CDATA.match(raw)
    if m:
        raw = m.group(1)
    s = BR.sub("\n", raw)
    s = BLOCK_END.sub("\n\n", s)
    s = TAG.sub("", s)
    s = html.unescape(s)
    s = SPACES.sub(" ", s)
    s = "\n".join(line.strip() for line in s.split("\n"))
    return BLANKS.sub("\n\n", s).strip()


def strip_bill_text(raw: str) -> str:
    """Bill XML to plain text. Tags out, entities resolved, spaces
    collapsed. No reflowing and no rewriting."""
    s = BLOCK_END.sub("\n\n", raw)
    s = re.sub(r"</(section|subsection|paragraph|text|header|"
               r"enum|quote|toc-entry)>", "\n", s, flags=re.I)
    s = TAG.sub(" ", s)
    s = html.unescape(s)
    s = SPACES.sub(" ", s)
    s = "\n".join(line.strip() for line in s.split("\n"))
    return BLANKS.sub("\n\n", s).strip()


def _zip(blob: dict) -> zipfile.ZipFile:
    return zipfile.ZipFile(BytesIO(blob["bytes"]))


def _text_index(blobs: dict, srcs: list[dict]) -> tuple[dict, list]:
    """(type, number, version) -> (url, sha256, retrieved_at, member, zip).

    The open ZipFile is kept in the entry on purpose. Opening one per
    row costs a full read of the archive's central directory each
    time, which on the 116th Congress (8,000 members, 16,596 rows)
    was most of a 17-minute build. Opened once per archive instead.
    The caller closes them.
    """
    index: dict = {}
    opened: list = []
    for src in srcs:
        if src["role"] != "text" or src["url"] not in blobs:
            continue
        blob = blobs[src["url"]]
        z = _zip(blob)
        opened.append(z)
        for member in z.namelist():
            m = BILL_FILE.match(member)
            if not m:
                continue
            key = (m.group("type"), int(m.group("number")),
                   m.group("version"))
            index[key] = (src["url"], blob["sha256"],
                          blob["retrieved_at"], member, z)
    return index, opened


def _rank(version: str) -> int:
    base = version
    while base and base[-1].isdigit():
        base = base[:-1]
    try:
        return STAGE_ORDER.index(base)
    except ValueError:
        return -1


def _pick_text(index: dict, btype: str, number: int,
               action_desc: str) -> tuple:
    """(entry, version, how) for the text this summary describes.

    The type's own map first, then ACTION_VERSION. A code is taken only
    when its file is in the zip; otherwise the next map is tried, and
    then the latest text, which the row records as `latest`.
    """
    desc = action_desc.strip().lower()
    for want in (TYPE_ACTION_VERSION.get(btype, {}).get(desc),
                 ACTION_VERSION.get(desc)):
        if want and (btype, number, want) in index:
            return index[(btype, number, want)], want, "action"
    have = [k[2] for k in index if k[0] == btype and k[1] == number]
    if not have:
        return None, "", "none"
    best = max(have, key=lambda v: (_rank(v), v))
    return index[(btype, number, best)], best, "latest"


def _excluded(spec: dict, title: str) -> bool:
    for rule in spec.get("exclude", []):
        if rule["field"] != "title" or rule["mode"] != "contains_ci":
            continue
        if rule["match"].lower() in (title or "").lower():
            return True
    return False


def rows(spec: dict, shard: str, blobs: dict) -> tuple[list[dict], dict]:
    """Rows for one Congress, and the counts a validator checks."""
    srcs = sources(spec, shard)
    index, opened = _text_index(blobs, srcs)
    stats = {
        "congress": int(shard), "summary_files": 0, "items": 0,
        "summaries": 0, "rows": 0, "excluded_summaries": 0,
        "excluded_bills": 0, "text_files": len(index),
        "text_from_action": 0, "text_from_latest": 0, "text_missing": 0,
        "by_type": {},
    }
    out: list[dict] = []
    excluded_ids: set = set()
    seen_bills: set = set()

    for src in srcs:
        if src["role"] != "summary" or src["url"] not in blobs:
            continue
        blob = blobs[src["url"]]
        per = {"summary_files": 0, "summaries": 0, "rows": 0,
               "excluded_bills": 0}
        with _zip(blob) as z:
            members = [m for m in z.namelist() if SUM_FILE.match(m)]
            per["summary_files"] = len(members)
            for member in members:
                doc = z.read(member).decode("utf-8", "replace")
                for item in ITEM.finditer(doc):
                    attrs = dict(ATTR.findall(item.group("attrs")))
                    body = item.group("body")
                    btype = attrs.get("measure-type", src["type"])
                    number = int(attrs.get("measure-number", "0"))
                    congress = int(attrs.get("congress", shard))
                    title_m = TITLE.search(body)
                    title = strip_html(title_m.group(1)) if title_m else ""
                    bill_id = f"{congress}-{btype}-{number}"
                    stats["items"] += 1
                    seen_bills.add(bill_id)
                    sums = list(SUMMARY.finditer(body))
                    stats["summaries"] += len(sums)
                    per["summaries"] += len(sums)
                    if _excluded(spec, title):
                        stats["excluded_summaries"] += len(sums)
                        if bill_id not in excluded_ids:
                            excluded_ids.add(bill_id)
                            per["excluded_bills"] += 1
                        continue
                    for sm in sums:
                        sattrs = dict(ATTR.findall(sm.group("attrs")))
                        sbody = sm.group("body")
                        sid = sattrs.get("summary-id", "")
                        version = sid.rsplit("v", 1)[-1]
                        version = f"v{version}" if version else ""
                        dm = ACTION_DATE.search(sbody)
                        gm = ACTION_DESC.search(sbody)
                        tm = SUM_TEXT.search(sbody)
                        desc = strip_html(gm.group(1)) if gm else ""
                        summary_text = strip_html(tm.group(1)) if tm else ""
                        entry, tv, how = _pick_text(
                            index, btype, number, desc)
                        if how == "action":
                            stats["text_from_action"] += 1
                        elif how == "latest":
                            stats["text_from_latest"] += 1
                        else:
                            stats["text_missing"] += 1
                        if entry:
                            turl, tsha, tret, tmember, tz = entry
                            text = strip_bill_text(
                                tz.read(tmember).decode(
                                    "utf-8", "replace"))
                        else:
                            turl = tsha = tret = ""
                            text = ""
                        out.append({
                            "bill_id": bill_id,
                            "congress": congress,
                            "type": btype,
                            "number": number,
                            "summary_id": sid,
                            "version_code": version,
                            "title": title,
                            "summary_text": summary_text,
                            "summary": summary_text,
                            "summary_action_date": (
                                dm.group(1).strip() if dm else ""),
                            "summary_action_desc": desc,
                            "summary_chamber": sattrs.get(
                                "currentChamber", ""),
                            "origin_chamber": attrs.get(
                                "originChamber", ""),
                            "orig_publish_date": attrs.get(
                                "orig-publish-date", ""),
                            "update_date": attrs.get("update-date", ""),
                            "text": text,
                            "text_version_code": tv,
                            "text_match": how,
                            "text_len": len(text),
                            "sum_len": len(summary_text),
                            "source_url": f"{src['url']} {turl}".strip(),
                            "source_url_summary": src["url"],
                            "source_url_text": turl,
                            "retrieved_at": blob["retrieved_at"],
                            "sha256": f"{blob['sha256']} {tsha}".strip(),
                            "sha256_summary": blob["sha256"],
                            "sha256_text": tsha,
                        })
                        per["rows"] += 1
        stats["by_type"][src["type"]] = per
        stats["summary_files"] += per["summary_files"]

    for z in opened:
        z.close()
    stats["rows"] = len(out)
    stats["excluded_bills"] = len(excluded_ids)
    stats["bills"] = len(seen_bills)
    return out, stats
