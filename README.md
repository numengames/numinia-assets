# Numinia Assets

The resource depot of the **Summa**, alongside
[numinia-archive](https://github.com/numengames/numinia-archive).
The archive holds canonical knowledge and resource records. This repository
holds the associated files and the notices that must travel with them.
The sites consume both; neither maintains another editable master catalogue.

## Scope

`content/` is the home for admitted images, audio, video, models, avatars and
editable originals. There are **no imported resources in this foundation**.
Only resources with verified CC0 provenance enter the first migration.
CC-BY, unknown and conflicting cases remain at their original source until a
separate decision; none are deleted or relicensed by this repository.

Do not copy the legacy `data/` tree: user records, sessions, favourites,
moderation and billing are not public asset content. Large-object storage,
resource schemas, IDs and versioning remain separate integration cuts.

## Licences

There is **no repository-wide CC0 grant**. [LICENSE](LICENSE) explains the
per-file declarations in [REUSE.toml](REUSE.toml), headers or adjacent
`.license` notices, with complete texts in `LICENSES/`. Third-party rights
are never inferred from a catalogue label. [TRADEMARKS.md](TRADEMARKS.md)
keeps brand permission separate from copyright licensing.

The rules live in the archive's [AGENTS.md](https://github.com/numengames/numinia-archive/blob/main/AGENTS.md),
[licensing canon](https://github.com/numengames/numinia-archive/blob/main/canon/CAN-005-licensing.md)
and [licensing standard](https://github.com/numengames/numinia-archive/blob/main/standards/STD-010-licensing.md).
This repository documents only its local operation.

## Check locally

Use Python 3.11 or later in a virtual environment:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-check.txt
.venv/bin/python -m reuse lint
.venv/bin/python -m unittest discover -s tests -v
```

CI runs both commands. The tests deliberately introduce unlicensed resources
in temporary copies and verify that the gate rejects them. No test writes an
asset into the repository or requires external services.

**A green check verifies licence declarations, not ownership, consent,
embedded media metadata, eligibility for CC0, or byte integrity.** Those need
an evidenced review before a resource is admitted. See [CONTRIBUTING.md](CONTRIBUTING.md).
