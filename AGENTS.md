# Working in Numinia Assets

The organisational source is
[numinia-archive/AGENTS.md](https://github.com/numengames/numinia-archive/blob/main/AGENTS.md),
including the MVP-to-alpha transition regime. This is a resource depot,
not another constitution or canonical catalogue.

- First intake: Avocado and Pot Vapor 02, with primary CC0 evidence and
  unchanged originals; no deployment or rewriting of existing grants.
- First ingestion: verified CC0 only, with evidence reviewed by the operator.
- Keep canonical resource records in the archive. Preserve IDs, original
  bytes, attribution and provenance when the ingestion cut is approved.
- Do not copy the legacy data tree, personal information or operational state.
- Local verification: `python -m reuse lint` and
  `python -m unittest discover -s tests -v`, after installing
  `requirements-check.txt` into a virtual environment (Python 3.11+).
- Do not create a blanket licence for unknown files or describe REUSE success
  as proof of rights, media metadata accuracy or verified CC0 eligibility.
- No merge, deletion, visibility change or legacy retirement without the
  operator's explicit authorisation.
