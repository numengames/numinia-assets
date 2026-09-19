# Contributing

Organisational rules live in
[numinia-archive](https://github.com/numengames/numinia-archive/blob/main/AGENTS.md),
including its transition regime. Changes here enter through reviewed PRs.

## Before importing a resource

The initial intake is limited to verified CC0 works. The accepted originals
and pending candidates are recorded in the archive. For each further intake:

- Link the canonical resource record in numinia-archive; do not add another
  editable catalogue here. Preserve existing resource identifiers.
- Provide provenance supporting the rightsholder's CC0 grant. Compare it
  with the catalogue declaration and embedded metadata. A conflict or unknown
  licence is a review question, not permission to change a label.
- Include an explicit declaration for the exact file. For binary content,
  an adjacent `.license` notice avoids editing the original bytes. It names
  the actual rightsholder and SPDX licence, not automatically Numen Games.
- Include the relevant full text in `LICENSES/`; keep required attribution
  with the resource. Review embedded licence metadata separately.
- Run the two checks in README.md. These do not prove the legal claim true.
- Do not migrate accounts, personal state, invoices, secrets or logs. Leave
  original repositories and excluded resources untouched.

Intake tests pin the accepted bytes and reject unexpected media. A general
resource schema and storage integration remain separate cuts.
No download service is deployed.

## Changes to the scaffolding

Update the local instructions when commands change. Add a regression test if
the licence gate changes. Do not introduce `content/**` or `**` default
licensing: an unannotated future file must fail, not silently become CC0.
Future licence families require an explicit decision, not a folder move.
