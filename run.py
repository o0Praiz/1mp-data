"""Build every public data library and publish it to Hugging Face.

The workflow in .github/workflows/data.yml runs this file each day.
It loads engines/library/build.py unchanged and turns off its
read_access step: the access log names the requester's company and
use case, so it stays in the private repo o0Praiz/1mp.

Exit codes: 0 when every library is OK, REFUSED or SKIPPED. A REFUSED
build is a gate that worked, not a crash; the manifest records why.
1 when a library ends in ERROR, so the run shows red.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable

HERE = Path(__file__).resolve().parent
BUILD_PY = HERE / "engines" / "library" / "build.py"


def no_access(today=None, token=None, *args, **kwargs) -> list[dict]:
    """Stands in for build.read_access in the public repo."""
    return [{"library": "*access", "status": "SKIPPED",
             "note": "access log is kept in the private repo"}]


def load_build():
    spec = importlib.util.spec_from_file_location(
        "library_build_engine", BUILD_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.read_access = no_access
    return mod


def run(build_due: Callable[..., list[dict]] | None = None,
        today: date | None = None) -> int:
    if build_due is None:
        build_due = load_build().build_due
    today = today or datetime.now(timezone.utc).date()
    logs = build_due(today)
    for entry in logs:
        print(json.dumps(entry))
    errors = [e for e in logs if e.get("status") == "ERROR"]
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(run())
