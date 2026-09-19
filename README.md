# Numinia Assets

The resource depot of the **Summa**, alongside
[numinia-archive](https://github.com/numengames/numinia-archive).
The archive holds canonical knowledge and resource records. This repository
holds the associated files and the notices that must travel with them.
The sites consume both; neither maintains another editable master catalogue.

## Scope

`content/` is the home for admitted images, audio, video, models, avatars and
editable originals. The first intake includes **Avocado and Pot Vapor 02**.
Their canonical records and the review of all 32 candidates live in the
[resource register](https://github.com/numengames/numinia-archive/blob/main/system/SYS-005-digital-resource-register.md).
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
asset into the repository or requires external services. Intake tests also
pin the admitted files by SHA-256 and size, reject unexpected media, and
check that the model containers need no external buffer or texture files.

**A green check verifies licence declarations, not ownership, consent,
eligibility for CC0, or all embedded media metadata.** Intake tests verify
byte integrity and the admitted VRM licence, not legal entitlement. Rights need
an evidenced review before a resource is admitted. See [CONTRIBUTING.md](CONTRIBUTING.md).
