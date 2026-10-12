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
- `open-worlds/` holds the public fleet's machine recipe and the trial's
  first orders (numinia-archive ADR-069). Each machine now keeps its own
  book (`/srv/fleet/orders/`), changed by requests an Oracle's wallet signs
  and the machine verifies (EIP-712); it adopts the orders here that name
  it once, then never reads this folder again. No IP address, key or
  client name ever goes here. `open-worlds/index.json` lists the orders
  still here and must match the folder (a test enforces it).
  `open-worlds/machine/` is the recipe (AGPL): the bootstrap installs the
  programs, so a merge here changes no machine until its bootstrap is run
  again. The machine holds no GitHub credential and makes each world's
  keys itself; the only secret an Oracle sees is the machine's admin code,
  shown to a signed Oracle and never written here.
- Local verification: `python -m reuse lint` and
  `python -m unittest discover -s tests -v`, after installing
  `requirements-check.txt` into a virtual environment (Python 3.11+;
  `ecdsa` is there for the machine's signature tests).
- Do not create a blanket licence for unknown files or describe REUSE success
  as proof of rights, media metadata accuracy or verified CC0 eligibility.
- No merge, deletion, visibility change or legacy retirement without the
  operator's explicit authorisation.
