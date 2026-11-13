#!/usr/bin/env python3
"""The library engine. One spec plus one parser is one library.

    python engines/library/build.py us-bills            # every shard
    python engines/library/build.py us-bills --shard 119
    python engines/library/build.py us-bills --rotate    # one shard/day
    python engines/library/build.py --list

`docs/42` section 3 is the drill, and this file is the whole of it:

    spec -> collect -> normalise -> validate -> stamp -> package
         -> publish

The spec is `libraries/<name>/library.json`. The parser beside it does
the source's XML and nothing else, so it can be tested on bytes with
no network. Everything that touches the network, the disk or Hugging
Face is in this file, behind two injectable callables (`fetch` and
`publish`), so the tests stay offline.

Rules this file keeps:

- **Stamp or refuse.** A row without `source_url`, `retrieved_at` and
  `sha256` fails the build. So does a duplicate key, a missing schema
  field, a row count that disagrees with the source's own file count,
  and an exclusion rule that matched nothing when the spec says it
  should match. `ground/README.md` section 7: when a rule matters,
  give it a gate.
- **No model-written text in a row.** The parser only copies from the
  source bytes. This file never writes a string into a row.
- **No credential anywhere.** `HF_TOKEN` is read from the environment
  and passed to `huggingface_hub`. It is never printed or written.
- **No spend.** govinfo bulk needs no key; Hugging Face dataset
  hosting is free.
- **Gated, so a download is a named lead.** `gate()` adds the access
  form to every card, whichever function rendered it, and
  `hf_publish` turns on automatic approval before it uploads. If it
  cannot, it uploads nothing. `O-39`, `docs/43` section 5.

Parquet needs `pyarrow`. Without it the build writes `.jsonl.gz`
instead and says so in the manifest, so a missing wheel degrades the
package rather than failing the daily job.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import importlib.util
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
LIBRARIES = ROOT / "libraries"
BUILD = HERE / "build"
DATA = HERE / "data"
# The workflow commits engines/apify/data and accounts only (see
# engines/apify/workflow/1mp.yml, the "commit what ci.py wrote" step,
# which agents cannot edit). So the manifest a cycle must be able to
# read goes there too, under a library- prefix.
COMMITTED = ROOT / "engines" / "apify" / "data"
# The paid offer of the direct engine (docs/44 section 6, docs/45).
# The card carries it only when the file says "live": true.
OFFER = ROOT / "engines" / "direct" / "offer.json"
SAMPLE_ROWS = 100
HTTP_TRIES = 3
HTTP_TIMEOUT = 180
# Wall clock the daily job gives all libraries together.
BUDGET_SECS = 20 * 60

# The access form on every dataset card (O-39). Keys read from
# huggingface.co/docs/hub/datasets-gated on 2026-10-16. The card holds
# the form only: Hugging Face has no card key for the approval mode,
# so hf_publish sets it through the settings API. No e-mail field:
# Hugging Face already shares the requester's account and e-mail.
GATE_MODE = "auto"
GATE_USE_CASES = ["research", "commercial", "RAG", "model training",
                  "other"]
GATE_PROMPT = ("This dataset is free. The two questions below tell "
               "the people who build it who uses the data and for "
               "what. Access is approved automatically.")
GATE_LINES = [
    f'extra_gated_prompt: "{GATE_PROMPT}"',
    "extra_gated_fields:",
    "  Company: text",
    "  Use case:",
    "    type: select",
    "    options:",
] + [f"    - {u}" for u in GATE_USE_CASES]


def log(**kw) -> dict:
    print(json.dumps(kw))
    return kw


def load_spec(name: str) -> dict:
    path = LIBRARIES / name / "library.json"
    if not path.is_file():
        raise SystemExit(f"no library spec at {path}")
    spec = json.loads(path.read_text(encoding="utf-8"))
    if spec.get("name") != name:
        raise SystemExit(f"{path}: name is {spec.get('name')!r}, "
                         f"not {name!r}")
    return spec


def load_parser(name: str, spec: dict):
    path = LIBRARIES / name / spec.get("parser", "parse.py")
    sp = importlib.util.spec_from_file_location(
        f"libraries_{name}_parse", path)
    mod = importlib.util.module_from_spec(sp)
    sp.loader.exec_module(mod)
    for fn in ("shards", "sources", "rows"):
        if not hasattr(mod, fn):
            raise SystemExit(f"{path}: no {fn}()")
    return mod


def list_libraries() -> list[str]:
    if not LIBRARIES.exists():
        return []
    return sorted(p.name for p in LIBRARIES.iterdir()
                  if (p / "library.json").is_file())


# ------------------------------------------------------------- collect

def http_fetch(url: str, user_agent: str) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": user_agent,
        "Accept": "application/zip, application/xml, */*",
    })
    last = None
    for attempt in range(HTTP_TRIES):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            # Read the code before writing the verdict: a 404 is a file
            # that is not published, a 403 is a closed door.
            raise RuntimeError(f"HTTP {e.code} {url}") from e
        except Exception as e:                       # noqa: BLE001
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"{type(last).__name__} {url}: {last}")


def collect(spec: dict, srcs: list[dict],
            fetch: Callable[[str, str], bytes]) -> tuple[dict, list[dict]]:
    """url -> {bytes, sha256, retrieved_at, size}, plus one log row each."""
    ua = spec["source"]["user_agent"]
    blobs: dict = {}
    seen: list[dict] = []
    for src in srcs:
        url = src["url"]
        if url in blobs:
            continue
        try:
            raw = fetch(url, ua)
        except Exception as e:                       # noqa: BLE001
            if src.get("optional"):
                seen.append({"url": url, "role": src["role"],
                             "skipped": str(e)})
                continue
            raise
        blobs[url] = {
            "bytes": raw,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "retrieved_at": datetime.now(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"),
            "size": len(raw),
        }
        seen.append({"url": url, "role": src["role"],
                     "size": len(raw), "sha256": blobs[url]["sha256"]})
    return blobs, seen


# ------------------------------------------------------------ validate

STAMPS = ("source_url", "retrieved_at", "sha256")


def validate(spec: dict, shard: str, rows: list[dict],
             stats: dict) -> list[str]:
    """Every reason this shard must not be packaged. Empty is a pass."""
    bad: list[str] = []
    fields = [f["name"] for f in spec["schema"]]
    key = spec["key"]
    if not rows:
        return [f"shard {shard}: no rows"]

    missing_field = set()
    extra_field = set()
    unstamped = 0
    for row in rows:
        missing_field |= {f for f in fields if f not in row}
        extra_field |= {k for k in row if k not in fields}
        if any(not str(row.get(s, "")).strip() for s in STAMPS):
            unstamped += 1
    if missing_field:
        bad.append(f"shard {shard}: rows miss schema fields "
                   f"{sorted(missing_field)}")
    if extra_field:
        bad.append(f"shard {shard}: rows carry fields not in the schema "
                   f"{sorted(extra_field)}")
    if unstamped:
        bad.append(f"shard {shard}: {unstamped} row(s) without "
                   f"{', '.join(STAMPS)}")

    keys = [tuple(str(r.get(k, "")) for k in key) for r in rows]
    if len(set(keys)) != len(keys):
        dupes = len(keys) - len(set(keys))
        bad.append(f"shard {shard}: {dupes} duplicate key(s) on "
                   f"{'+'.join(key)}")

    # The source's own file count must account for every bill: one bill
    # is one BILLSUM file, and a bill is either in the rows or excluded.
    # Both of these run only on a library whose parser counts bills,
    # which is us-bills; a library that counts something else declares
    # the pair to compare in `count_checks`, below.
    files = stats.get("summary_files")
    bills = stats.get("bills")
    if files is not None and bills is not None and files != bills:
        bad.append(f"shard {shard}: {files} summary file(s) in the zips "
                   f"but {bills} bill(s) parsed")
    if bills is not None:
        kept = bills - (stats.get("excluded_bills") or 0)
        in_rows = len({r["bill_id"] for r in rows if "bill_id" in r})
        if kept != in_rows:
            bad.append(f"shard {shard}: {kept} bill(s) expected after "
                       f"exclusions but {in_rows} in the rows")

    # A parser may refuse its own shard: a structural fact only it can
    # see (the manifest and the file disagree, a partition appeared)
    # belongs in its own words, not guessed at from counts.
    for line in stats.get("refusals") or []:
        bad.append(f"shard {shard}: {line}")

    # Gate 4 for any library: the source's own count against the
    # parser's. Each check names two keys of `stats` that must be equal
    # and present.
    for chk in spec.get("count_checks", []):
        want = stats.get(chk["source"])
        got = stats.get(chk["parsed"])
        if want is None or got is None:
            bad.append(f"shard {shard}: count check "
                       f"{chk['source']} vs {chk['parsed']} has no "
                       f"numbers to compare ({want!r}, {got!r})")
        elif want != got:
            bad.append(f"shard {shard}: {want} {chk['source']} from the "
                       f"source but {got} {chk['parsed']}")

    # A field a library drops must never reach a row. The parser
    # removes it; this refuses the shard if one came through anyway.
    for d in spec.get("drop_fields", []):
        col = d.get("in", "record")
        probe = f'"{d["field"]}":'
        hits = sum(1 for r in rows if probe in str(r.get(col, "")))
        if hits:
            bad.append(f"shard {shard}: {hits} row(s) still carry the "
                       f"dropped field {d['field']!r} in {col}")

    for rule in spec.get("exclude", []):
        hits = sum(1 for r in rows
                   if rule["match"].lower() in str(
                       r.get(rule["field"], "")).lower())
        if hits:
            bad.append(f"shard {shard}: {hits} row(s) still match the "
                       f"exclusion {rule['match']!r}")
    return bad


# ------------------------------------------------------------- package

def _parquet(rows: list[dict], spec: dict, path: Path) -> str:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError:
        return ""
    cols = {}
    for f in spec["schema"]:
        n, t = f["name"], f["type"]
        if t == "int32":
            cols[n] = pa.array([int(r.get(n) or 0) for r in rows],
                               type=pa.int32())
        else:
            cols[n] = pa.array([str(r.get(n) or "") for r in rows],
                               type=pa.string())
    table = pa.table(cols)
    pq.write_table(table, path, compression="zstd")
    return "parquet"


def _jsonl_gz(rows: list[dict], path: Path) -> str:
    with gzip.open(path, "wt", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return "jsonl.gz"


def package_shard(spec: dict, shard: str, rows: list[dict],
                  out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=True)
    name = spec["name"]
    stem = f"{name}-{shard}"
    fmt = _parquet(rows, spec, out / f"{stem}.parquet")
    if fmt:
        path = out / f"{stem}.parquet"
    else:
        path = out / f"{stem}.jsonl.gz"
        fmt = _jsonl_gz(rows, path)
    return {"shard": shard, "rows": len(rows), "format": fmt,
            "file": path.name, "bytes": path.stat().st_size,
            "sha256": sha256_file(path)}


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_sample(rows: list[dict], path: Path, limit: int) -> int:
    with path.open("w", encoding="utf-8") as f:
        for r in rows[:limit]:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return min(limit, len(rows))


def write_checksums(out: Path) -> None:
    lines = []
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.name != "checksums.txt":
            lines.append(f"{sha256_file(p)}  "
                         f"{p.relative_to(out).as_posix()}")
    (out / "checksums.txt").write_text("\n".join(lines) + "\n",
                                       encoding="utf-8")


def card(spec: dict, manifest: dict) -> str:
    """The built-in dataset card, which describes us-bills.

    A library whose rows are something else defines `card(spec,
    manifest)` in its own parser and `build` uses that instead.
    """
    lic = spec["licence"]
    src = spec["source"]
    rows = manifest["rows"]
    L = [
        "---",
        "license: other",
        f"license_name: {lic['id']}",
        f"license_link: {lic['proof_url']}",
        "pretty_name: " + spec["title"],
        "language:",
        "- en",
        "task_categories:",
        "- summarization",
        "- text-generation",
        "size_categories:",
        "- " + ("100K<n<1M" if rows >= 100000 else
                "10K<n<100K" if rows >= 10000 else "1K<n<10K"),
        "---",
        "",
        f"# {spec['title']}",
        "",
        f"{rows:,} rows. One row is one official summary of one US "
        "bill, with the",
        "text of the bill version that summary describes. Built by code "
        "from",
        f"{src['host']} bulk data on {manifest['built']}.",
        "",
        "## Why this exists",
        "",
        "`FiscalNote/billsum` is the dataset people use for US bill "
        "summarisation.",
        "It stops at the 115th Congress (2018). This library carries the "
        "116th to",
        "the 119th, is rebuilt from the source daily, and stamps every "
        "row with",
        "the file it came from, the time it was fetched and that file's "
        "SHA-256,",
        "so any row can be checked against the government's own copy.",
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
        "`text`, `summary` and `title` carry the same meaning as in",
        "`FiscalNote/billsum`, so code written for that dataset runs "
        "unchanged.",
        "`summary` is a copy of `summary_text`.",
        "",
        "## Counts",
        "",
        "| Congress | Rows | Bills | Summary files in the zips | "
        "Private bills excluded |",
        "|---|---|---|---|---|",
    ]
    for s in manifest["shards"]:
        st = s["stats"]
        L.append(f"| {s['shard']} | {s['rows']:,} | "
                 f"{st.get('bills', 0):,} | "
                 f"{st.get('summary_files', 0):,} | "
                 f"{st.get('excluded_bills', 0):,} |")
    L += [
        "",
        f"Bill text matched to the summary's own action on "
        f"{manifest['totals']['text_from_action']:,} rows; "
        f"{manifest['totals']['text_from_latest']:,} rows fall back to "
        "the latest",
        f"version available and say so in `text_match`; "
        f"{manifest['totals']['text_missing']:,} rows have no text.",
        "",
        "## Licence and provenance",
        "",
        f"{lic['name']}: {lic['proof']}. Every source file carries this",
        "statement, in its `dc:rights` element:",
        "",
        f"> {lic['source_statement']}",
        "",
        f"Proof: {lic['proof_url']}",
        "",
        "Sources, by file, with the SHA-256 of the bytes this build "
        "read:",
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
        "## What this does not cover",
        "",
    ]
    for item in spec.get("not_covered", []):
        L.append(f"- {item}")
    L += [
        "",
        "## No model-written text",
        "",
        "No row holds text written by a language model. Every string in "
        "every",
        "row is copied from the government source files listed above, or "
        "computed",
        "from them (a length, an id) by the build code. Models wrote the "
        "build",
        "code; they wrote none of the data.",
        "",
        "## How it was built",
        "",
        "`engines/library/build.py` and `libraries/us-bills/parse.py` in "
        "the",
        "project that publishes this dataset. The build refuses to "
        "package a",
        "shard with a duplicate key, a missing stamp, or a row count "
        "that",
        "disagrees with the number of files in the source zips.",
        "",
    ]
    return "\n".join(L)


def gate_lines(feed: str | None = None) -> list[str]:
    """The form. With `feed`, the prompt names the library's paid
    change-feed Actor (O-39 follow-up 4, 2026-10-18)."""
    if not feed:
        return GATE_LINES
    prompt = (f"{GATE_PROMPT} New and changed rows since a date you "
              f"pick are sold as a change feed at {feed}")
    if '"' in prompt or "\\" in prompt:
        raise ValueError(f"feed URL breaks the YAML string: {feed!r}")
    return [f'extra_gated_prompt: "{prompt}"'] + GATE_LINES[1:]


def gate(text: str, feed: str | None = None) -> str:
    """Put the access form into a card's YAML front matter.

    Runs on whatever card a library renders, so no library can ship
    ungated. A card that already holds `extra_gated_fields` is left as
    it is; a card with no front matter gets one. `feed` is the
    library spec's optional `feed_url`.
    """
    lines = gate_lines(feed)
    if text.startswith("---\n"):
        end = text.find("\n---\n", 3)
        if end != -1:
            head = text[4:end + 1]
            if "extra_gated_fields:" in head:
                return text
            return ("---\n" + head + "\n".join(lines) + "\n"
                    + text[end + 1:])
    return "---\n" + "\n".join(lines) + "\n---\n\n" + text


# --------------------------------------------------------------- offer

OFFER_CADENCE = {"weekly": "each Monday", "daily": "each day"}
OFFER_LIBRARIES = ("us-bills", "fda-decisions")


def load_offer(path: Path | None = None) -> dict:
    """The offer file, or {} if it is missing or not valid JSON."""
    try:
        return json.loads((path or OFFER).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def offer_lines(spec: dict, offer: dict) -> list[str]:
    """The card section that sells the change feed by key.

    Empty unless the offer is live and every field is valid. A bad
    field gives no section, never a broken link (docs/45, part 3).
    """
    if not isinstance(offer, dict) or offer.get("live") is not True:
        return []
    lib = spec.get("name")
    if lib not in OFFER_LIBRARIES:
        return []
    base = str(offer.get("base_url") or "").rstrip("/")
    if (not base.startswith("https://")
            or any(c in base for c in " \"<>`'\\\n")):
        return []
    price = offer.get("price_usd_month")
    if type(price) is not int or price <= 0:
        return []
    cadence = OFFER_CADENCE.get(offer.get("load"))
    if not cadence:
        return []
    L = [
        f"## The change feed by key, ${price} a month",
        "",
        "This free copy is the whole library. The paid feed gives only "
        "the rows",
        "that are new or changed since a date you choose.",
        "",
        "- **One key reads both libraries:** `us-bills` and "
        "`fda-decisions`.",
        f"- **Price:** ${price} a month, paid through PayPal. Cancel "
        "in PayPal at any",
        "  time. The key works to the end of the month you paid for.",
        f"- **Buy:** open {base}/subscribe and approve at PayPal.",
        "  The next page shows your key one time. Keep it.",
        "- **Lost key:** load that page again, at most five times a "
        "day. It gives",
        "  a new key and stops the old key.",
        "- **Limits:** 500 requests a day for each key, and 1,000 rows "
        "or fewer",
        "  for each request. Use the `next` value to get the next page.",
        f"- **Updates:** the feed loads new rows {cadence}. Each "
        "answer gives",
        "  `loaded_through`, the last day the feed holds.",
        "- **Older rows:** each answer gives `coverage_from`, the "
        "first day the",
        "  feed holds. For rows before that day, use this free copy.",
        "- **Not included:** interpretation, legal advice and personal "
        "data.",
        "  Each `row` is the change-feed Actor's row, with its source "
        "URL and SHA-256.",
        "  Its key joins a row of this library.",
        "",
        "```",
        "curl -H \"x-api-key: YOUR_KEY\" \\",
        f"  \"{base}/feed?library={lib}&since=7\"",
        "```",
        "",
    ]
    feed = spec.get("feed_url")
    if feed:
        L += [f"To pay for each row and not each month, use the Apify "
              f"Actor: {feed}", ""]
    return L


def add_offer(text: str, spec: dict, offer: dict) -> str:
    """Put the offer before the card's first `## ` section.

    Runs on whatever card a library renders, as `gate()` does. With
    no live offer the card is returned as it was.
    """
    lines = offer_lines(spec, offer)
    if not lines:
        return text
    start = 0
    if text.startswith("---\n"):
        end = text.find("\n---\n", 3)
        start = end + 5 if end != -1 else 0
    at = text.find("\n## ", start)
    block = "\n".join(lines)
    if at == -1:
        return text.rstrip("\n") + "\n\n" + block
    return text[:at + 1] + block + "\n" + text[at + 1:]


# ------------------------------------------------------------- publish

def hf_publish(spec: dict, out: Path, token: str) -> dict:
    """Upload the built folder as a Hugging Face dataset."""
    try:
        from huggingface_hub import HfApi
    except ImportError:
        return {"published": False,
                "note": "huggingface_hub is not installed"}
    api = HfApi(token=token)
    if not callable(getattr(api, "update_repo_settings", None)):
        # Fail closed: an old client cannot set the gate, and an
        # ungated upload gives downloads with no names (O-39).
        return {"published": False, "gated": None,
                "note": "huggingface_hub cannot set the gate: "
                        "not published"}
    who = api.whoami()
    owner = who.get("name") or ""
    repo = f"{owner}/{spec['hf_repo']}"
    api.create_repo(repo_id=repo, repo_type="dataset", exist_ok=True)
    # Gate before the first upload, so the data is never public
    # without the form. Automatic approval, docs/43 section 5.
    api.update_repo_settings(repo_id=repo, repo_type="dataset",
                             gated=GATE_MODE)
    api.upload_folder(folder_path=str(out), repo_id=repo,
                      repo_type="dataset")
    return {"published": True, "repo": repo, "gated": GATE_MODE,
            "url": f"https://huggingface.co/datasets/{repo}"}


# -------------------------------------------------------------- access

# L1 reads gated requests that state commercial or RAG use (docs/43
# section 5). The daily job writes them where a cycle can read them.
# One row a request: the date, the use case and the company, and
# nothing else. The account name and the e-mail stay on Hugging Face.
L1_USES = {"commercial", "RAG"}
ACCESS_COLUMNS = ["date", "library", "requested_on", "use_case",
                  "company"]
L1_COLUMNS = ["date", "library", "repo", "downloads_30d",
              "requests_accepted", "requests_commercial_or_rag"]
COMPANY_MAX = 100


def one_line(value, limit: int = COMPANY_MAX) -> str:
    """A form answer as one short line."""
    return " ".join(str(value or "").split())[:limit]


def access_rows(name: str, requests, today: date) -> list[dict]:
    rows = []
    for r in requests:
        fields = getattr(r, "fields", None) or {}
        when = getattr(r, "timestamp", None)
        rows.append({
            "date": today.isoformat(), "library": name,
            "requested_on": when.date().isoformat() if when else "",
            "use_case": one_line(fields.get("Use case"), 40),
            "company": one_line(fields.get("Company")),
        })
    rows.sort(key=lambda x: (x["requested_on"], x["use_case"]))
    return rows


def write_csv(path: Path, columns: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def read_access(today: date | None = None, token: str | None = None,
                api_cls=None) -> list[dict]:
    """Write every library's accepted access requests and downloads.

    Two files in `COMMITTED`, both whole each day, so a cycle reads
    the newest one only: `library-access-<date>.csv`, one row a
    request, and `library-l1-<date>.csv`, one row a library. Hugging
    Face's download count includes its own bots, so L1 counts the
    requests that state commercial or RAG use as the signal.
    """
    today = today or date.today()
    token = token if token is not None else os.environ.get(
        "HF_TOKEN", "").strip()
    if not token:
        return [{"library": "*access", "status": "SKIPPED",
                 "note": "HF_TOKEN missing: access not read"}]
    if api_cls is None:
        from huggingface_hub import HfApi as api_cls
    api = api_cls(token=token)
    owner = api.whoami().get("name") or ""
    access, l1, out = [], [], []
    for name in list_libraries():
        repo = f"{owner}/{load_spec(name)['hf_repo']}"
        try:
            got = list(api.list_accepted_access_requests(
                repo, repo_type="dataset"))
            info = api.dataset_info(repo)
        except Exception as e:                       # noqa: BLE001
            out.append({"library": name, "status": "ERROR",
                        "error": f"access: {type(e).__name__}: {e}"})
            continue
        rows = access_rows(name, got, today)
        access.extend(rows)
        l1.append({"date": today.isoformat(), "library": name,
                   "repo": repo,
                   "downloads_30d": getattr(info, "downloads", ""),
                   "requests_accepted": len(rows),
                   "requests_commercial_or_rag": sum(
                       r["use_case"] in L1_USES for r in rows)})
        out.append({"library": name, "status": "OK", "access": len(rows)})
    stamp = today.isoformat()
    write_csv(COMMITTED / f"library-access-{stamp}.csv",
              ACCESS_COLUMNS, access)
    write_csv(COMMITTED / f"library-l1-{stamp}.csv", L1_COLUMNS, l1)
    return out


# --------------------------------------------------------------- build

def rotate_shard(shards: list[str], today: date) -> str:
    """One shard a day, in order, so a 30-minute job always finishes."""
    return shards[today.toordinal() % len(shards)]


def build(name: str, shard: str | None = None, rotate: bool = False,
          today: date | None = None,
          fetch: Callable[[str, str], bytes] | None = None,
          publish: Callable[[dict, Path, str], dict] | None = None,
          token: str | None = None,
          out_root: Path | None = None) -> dict:
    today = today or date.today()
    fetch = fetch or http_fetch
    publish = publish or hf_publish
    token = token if token is not None else os.environ.get(
        "HF_TOKEN", "").strip()
    spec = load_spec(name)
    parser = load_parser(name, spec)
    all_shards = parser.shards(spec)
    if shard:
        want = [shard]
    elif rotate:
        want = [rotate_shard(all_shards, today)]
    else:
        want = list(all_shards)

    out = (out_root or BUILD) / name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True, exist_ok=True)
    data_dir = out / "data"

    t0 = time.time()
    manifest = {
        "library": name, "title": spec["title"],
        "built": today.isoformat(),
        "built_at": datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "shards_in_spec": all_shards, "shards_built": want,
        "rotate": bool(rotate and not shard),
        "licence": spec["licence"], "schema": spec["schema"],
        "exclude": spec.get("exclude", []),
        "not_covered": spec.get("not_covered", []),
        "sources": [], "shards": [], "rows": 0,
        # Which stats a library sums over its shards. us-bills declares
        # none, so it keeps the seven it was written with.
        "totals": {k: 0 for k in spec.get("totals", [
            "text_from_action", "text_from_latest", "text_missing",
            "excluded_bills", "excluded_summaries", "summary_files",
            "bills"])},
        "refusals": [],
    }
    sample: list[dict] = []
    for sh in want:
        srcs = parser.sources(spec, sh)
        blobs, seen = collect(spec, srcs, fetch)
        manifest["sources"].extend(seen)
        rows, stats = parser.rows(spec, sh, blobs)
        blobs.clear()
        bad = validate(spec, sh, rows, stats)
        if bad:
            manifest["refusals"].extend(bad)
            for line in bad:
                log(library=name, refused=line)
            continue
        part = package_shard(spec, sh, rows, data_dir)
        part["stats"] = {k: v for k, v in stats.items()
                         if k != "by_type"}
        part["by_type"] = stats.get("by_type", {})
        manifest["shards"].append(part)
        manifest["rows"] += len(rows)
        for k in manifest["totals"]:
            manifest["totals"][k] += stats.get(k, 0) or 0
        if len(sample) < SAMPLE_ROWS:
            sample.extend(rows[:SAMPLE_ROWS - len(sample)])
        rows.clear()
        log(library=name, shard=sh, rows=part["rows"],
            format=part["format"], bytes=part["bytes"])

    manifest["seconds"] = round(time.time() - t0, 1)
    if manifest["refusals"]:
        manifest["status"] = "REFUSED"
        write_manifest(name, manifest, out, today)
        return manifest

    manifest["sample_rows"] = write_sample(
        sample, out / "sample.jsonl", SAMPLE_ROWS)
    # A library whose rows are not bills renders its own card: the
    # built-in one below describes us-bills. `card` is the only
    # optional hook a parser has; the three required ones are
    # checked in load_parser.
    render = getattr(parser, "card", None) or card
    (out / "README.md").write_text(
        gate(add_offer(render(spec, manifest), spec, load_offer()),
             spec.get("feed_url")),
        encoding="utf-8")
    if token:
        manifest["publish"] = publish(spec, out, token)
    else:
        manifest["publish"] = {
            "published": False,
            "note": "HF_TOKEN missing: built, not published"}
        log(library=name, note="HF_TOKEN missing: built, not published")
    manifest["status"] = "OK"
    write_manifest(name, manifest, out, today)
    write_checksums(out)
    return manifest


def write_manifest(name: str, manifest: dict, out: Path,
                   today: date) -> None:
    text = json.dumps(manifest, indent=1, sort_keys=False) + "\n"
    (out / "manifest.json").write_text(text, encoding="utf-8")
    for d in (DATA, COMMITTED):
        d.mkdir(parents=True, exist_ok=True)
    (DATA / f"{name}-manifest-{today.isoformat()}.json").write_text(
        text, encoding="utf-8")
    (COMMITTED / f"library-{name}-manifest-"
                 f"{today.isoformat()}.json").write_text(
        text, encoding="utf-8")


def build_due(today: date | None = None,
              rotate: bool = False,
              budget_secs: int = BUDGET_SECS) -> list[dict]:
    """What the daily job runs: every library, every shard.

    Measured 2026-10-07: a full four-Congress build of us-bills is
    43,549 bills, 49,935 rows and 207 seconds, against the workflow's
    30-minute job, which on the schedule event has no deploys to
    share it with. So the default is a full rebuild, and every day's
    card describes the whole dataset rather than one refreshed shard.

    `budget_secs` is the safeguard, not the plan: once it is spent no
    further library starts, and the skipped ones are recorded. A
    library whose own full build is too slow takes `rotate=True`
    instead and refreshes one shard a day.
    """
    out = []
    t0 = time.time()
    for name in list_libraries():
        spent = time.time() - t0
        if spent > budget_secs:
            out.append({"library": name, "status": "SKIPPED",
                        "note": f"budget spent after {round(spent)}s"})
            continue
        try:
            m = build(name, rotate=rotate, today=today)
            out.append({"library": name, "status": m["status"],
                        "shard": ",".join(m["shards_built"]),
                        "rows": m["rows"], "seconds": m["seconds"],
                        "published": m.get("publish", {}).get(
                            "published", False)})
        except Exception as e:                       # noqa: BLE001
            out.append({"library": name, "status": "ERROR",
                        "error": f"{type(e).__name__}: {e}"})
    # L1's reading, after the builds and outside their budget: two
    # API calls a library.
    try:
        out.extend(read_access(today))
    except Exception as e:                           # noqa: BLE001
        out.append({"library": "*access", "status": "ERROR",
                    "error": f"{type(e).__name__}: {e}"})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("name", nargs="?")
    ap.add_argument("--shard")
    ap.add_argument("--rotate", action="store_true")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    if a.list or not a.name:
        for n in list_libraries():
            print(n)
        return 0
    m = build(a.name, shard=a.shard, rotate=a.rotate)
    print(json.dumps({k: v for k, v in m.items()
                      if k in ("library", "status", "rows",
                               "shards_built", "seconds", "refusals",
                               "publish", "totals")}, indent=1))
    return 1 if m["status"] != "OK" else 0


if __name__ == "__main__":
    sys.exit(main())
