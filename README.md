# 1mp-data

The code that builds two free, public-domain datasets on Hugging Face
and keeps them current.

- **fda-regulatory-decisions**: openFDA bulk exports, CC0 1.0.
  <https://huggingface.co/datasets/o0Praiz/fda-regulatory-decisions>
- **us-bills-summaries**: GovInfo bulk data (BILLSUM and BILLS), 116th
  to 119th Congress. US Government work, 17 U.S.C. §105.
  <https://huggingface.co/datasets/o0Praiz/us-bills-summaries>

## How it works

1. `libraries/<name>/library.json` declares the source, the schema and
   the checks. `libraries/<name>/parse.py` turns source files into rows.
2. `engines/library/build.py` fetches, parses and validates each
   library. If a check fails, the build is `REFUSED` and the dataset on
   Hugging Face stays as it was.
3. A good build is published to Hugging Face. Its manifest (row
   counts, checksums, status) is committed to `engines/apify/data/`.
4. `.github/workflows/data.yml` runs `run.py` each day at 10:23 UTC.

## Run it

```
pip install -r requirements.txt pytest
python -m pytest -q engines/library/tests
HF_TOKEN=... python run.py
```

Without `HF_TOKEN`, the build writes the files and does not publish.

## Contact

Open an issue in this repository, or use the discussion board of the
dataset on Hugging Face.
