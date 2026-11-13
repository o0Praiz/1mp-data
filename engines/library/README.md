# Engine 2: data libraries

Public-domain data, packaged by code, published free on Hugging Face
first. `docs/42` is the decision and the evidence; this file is the
drill. Opened 2026-10-07 by `O-30`.

Engine 1 (`engines/apify/`) sells tools. This engine gives away
libraries, to buy the one thing the project has not yet bought:
someone looking. `docs/42` section 2.2 is the reason it might work.
On Hugging Face, stale public-domain datasets still draw thousands of
downloads a month, and the ones people use are years out of date.

---

## One library is one spec plus one parser

```
libraries/<name>/library.json    the spec: source, licence, schema
libraries/<name>/parse.py        the source's format, and nothing else
```

`engines/library/build.py` is everything else, for every library:

```
spec -> collect -> normalise -> validate -> stamp -> package -> publish
```

To add a library, write those two files. Do not touch `build.py`
unless the engine itself is missing something; if it is, add it for
every library at once.

The parser exposes three required functions and one optional one:

| Function | Returns |
|---|---|
| `shards(spec)` | the partition keys, oldest first |
| `sources(spec, shard)` | one dict per file: `url`, `role`, `optional` |
| `rows(spec, shard, blobs)` | `(rows, stats)` |
| `card(spec, manifest)` | *optional.* The dataset card, if the built-in one does not fit |

`build.py`'s own `card()` describes `us-bills`. A library whose rows
are something else defines `card` in its parser and `build` uses that
instead. `O-32` added the hook on 2026-10-12.

`blobs` is `url -> {bytes, sha256, retrieved_at, size}`. A parser does
no HTTP, writes no file and holds no credential, so every parser is
testable offline on bytes. `libraries/us-bills/parse.py` is the
worked example.

## Run it

```
python engines/library/build.py --list
python engines/library/build.py us-bills             # every shard
python engines/library/build.py us-bills --shard 119 # one
python engines/library/build.py us-bills --rotate    # one a day
python -m unittest discover -s engines/library/tests
```

In the daily job, `engines/apify/ci.py` calls `build_due(today)` on
the `schedule` event, after the Actor work. There is no second
workflow file, and agents could not write one anyway.

## What a build leaves behind

```
engines/library/build/<name>/data/<name>-<shard>.parquet
engines/library/build/<name>/sample.jsonl      100 rows
engines/library/build/<name>/README.md          the dataset card
engines/library/build/<name>/checksums.txt
engines/library/build/<name>/manifest.json
engines/library/data/<name>-manifest-<date>.json
engines/apify/data/library-<name>-manifest-<date>.json
```

The last one is the copy a later cycle can read, because the
workflow's commit step adds `engines/apify/data` and `accounts` and
nothing else. The built data files are never committed to this repo;
Hugging Face holds them.

## The gates

`build.py validate()` refuses a shard, and the manifest says
`REFUSED` and why, on any of these:

1. A row without `source_url`, `retrieved_at` or `sha256`.
2. A duplicate on the spec's `key`.
3. A row missing a schema field, or carrying one the schema has not
   declared.
4. A row count that disagrees with the source's own file count.
5. A row that still matches an exclusion rule.
6. A refusal the parser itself returned, in `stats["refusals"]`: a
   structural fact only the parser can see, such as the source's own
   manifest naming a file this build did not fetch.
7. A pair of counts in the spec's `count_checks` that disagree, or
   that the parser did not report at all. Each check names two keys of
   `stats` that must be equal and present, so a library whose rows are
   not bills still has gate 4.
8. A row that still carries a field the spec's `drop_fields` removes.

Gates 6 to 8 were added by `O-32` on 2026-10-12, when the engine's
gate 4 turned out to be written for bills alone: it read
`stats["bills"]` and `row["bill_id"]`, so a second library crashed
`validate()` rather than being checked by it. The bills-specific half
of gate 4 now runs only when the parser reports `bills`, which is
`us-bills` and nothing else. All 59 tests of `test_build.py` pass
unchanged across the change.

A refused shard is not packaged, not sampled, and not published. The
run writes the manifest anyway, so the next cycle reads the reason
rather than guessing. `ground/README.md` section 7.

## The rails, from `docs/42` section 5

- Public domain, CC0 or CC-BY only, with the licence proof in the
  spec. Never a source whose terms forbid redistribution.
- No personal data. No private bills, no people records.
- **No model-written text in any row.** Claude's terms forbid using
  its output to train models (`docs/42` section 2.3), so every string
  in every row is copied from the source bytes or computed from them
  by code. Models write the build code. They write none of the data.
- Bulk files before page scraping, always, where a bulk file exists.
  Obey `robots.txt` and the source's own rate rules, and record in
  the spec when they were read.
- Honest cards: what the library covers, what it does not, every
  known gap.
- No spend. Hugging Face dataset hosting is free; the compute is the
  daily job.

## The libraries

| # | Name | Source | Rows | State |
|---|---|---|---|---|
| 1 | `us-bills` | govinfo bulk XML | 61,549 | built 2026-10-09 |
| 2 | `fda-decisions` | openFDA bulk JSON | 465,829 | built 2026-10-12 |
| 3 | 10-K sections | SEC EDGAR | - | not started |
| 4 | US patents | USPTO bulk | - | not started |

Libraries 1 and 2 publish as soon as `HF_TOKEN` exists. `docs/42`
called EDGAR library 2 and patents library 3; `O-32` built openFDA
first, because EDGAR needs a contact e-mail in the User-Agent (it
answers 403, "Undeclared Automated Tool", without one, `docs/42`
section 2.3) and USPTO's bulk host was unreachable from the
container. The numbers in `docs/42` section 4 keep that document's
order; this table is the built state.

## Measured, 2026-10-12 (`O-32`, library 2)

- A full nine-endpoint build of `fda-decisions` is **465,829 rows in
  109.7 seconds**, 471 MB of zstd Parquet, peak 3.42 GB of memory,
  from 663,467,341 bytes of source zips. With library 1's 251 seconds
  that is 361 seconds for both, well inside the 20-minute budget, so
  `build_due` stays `rotate=False`.
- `download.open.fda.gov` served 663 MB in 11 seconds, about 60 MB a
  second, from the cloud container.
- **The engine cannot `json.load` an openFDA bulk file.**
  `device/510k` is 1,619.5 MB of JSON uncompressed and
  `device/recall` is 1,494.0 MB; both were killed by the OOM killer
  in an 8 GB container. `libraries/fda-decisions/parse.py` streams the
  top-level object key by key instead: 176,299 records of
  `device/510k` in 9.5 seconds at a 24 MB peak. A scan for
  `"results"` would not have worked either, because openFDA's `meta`
  block holds a `results` object of its own before the real one.
- The peak, 3.42 GB, is the one number to watch. It is one shard's
  rows plus the Parquet arrays built from them, and `device/510k` and
  `device/recall` are each about 1.5 GB of records. A third large
  endpoint would not change it; a single endpoint twice that size
  would. `rotate=True` is the lever if it ever matters.

## Publishing

`HF_TOKEN` in the repo's Actions secrets, read from the environment,
never written anywhere. Without it the build logs `HF_TOKEN missing:
built, not published` and keeps the files in the job. That is a
correct outcome, not a failure: the gates still ran and the manifest
still landed.

**Every library is gated** (`O-39`, 2026-10-16). `build.gate()` adds
the Hugging Face access form to every card's front matter: Company
(text) and Use case (research, commercial, RAG, model training,
other). `hf_publish` sets automatic approval with
`update_repo_settings(gated="auto")` before the first upload. If the
client cannot set it, nothing uploads. A new library gets the gate
with no parser change. `docs/drafts/O-39-gate-the-libraries.md` has
the keys and their source.

**The offer** (2026-10-22, `docs/45` §8). `add_offer()` puts the
$29 change-feed offer into each card, before its first section, when
`engines/direct/offer.json` says `"live": true`. It runs on every
card, as `gate()` does. With the offer held, the card does not
change.

## Measured, 2026-10-09 (`O-31`)

`O-31` added the six resolution types to library 1, so the numbers
below are superseded, not wrong: they describe the `hr` and `s`
build. The eight-type build is 52,550 bills, 61,549 rows and 251
seconds, from 96 source files and 726,221,429 bytes, in 229 MB of
zstd Parquet. Still a full rebuild every day, still inside the
20-minute budget, so `build_due` stays `rotate=False`. Resolutions
are 11,582 of the rows, from 8,971 summary files. The split by
Congress is 19,937 / 21,200 / 14,247 / 6,165 for the 116th to the
119th. `orders/done/O-31-library-bills-resolutions.md` has the
action-description counts and the `text_match` split.

## Measured, 2026-10-07 (`O-30`, `hr` and `s` only)

- The four BILLSUM zips plus the sixteen BILLS zips of library 1 are
  57,280,448 and 613,613,942 bytes. govinfo served them at about
  7 MB a second from the cloud container.
- A full four-Congress build: 43,549 bills, 49,935 rows, 207
  seconds, 221 MB of zstd Parquet, peak 1.9 GB of memory. That is
  the whole library every day, well inside the 30-minute job.
- The first version of the parser opened the text archive once per
  row, which re-read its central directory 49,935 times and cost
  1,026 seconds. Opening each archive once took the same build to
  207, with identical counts. If a library ever does get too slow,
  `rotate=True` refreshes one shard a day instead.
- Summary files by Congress: 116th 14,139; 117th 15,045; 118th
  9,434; 119th 4,931. `docs/42` counted the same four numbers from
  the source independently on the same day.
