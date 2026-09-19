# Changes

## Unreleased

- CI takes the family shape (numinia-archive STD-015 § The family pipeline):
  `check.yml` becomes `ci.yml`, its job is literally named `build` (the one
  check the branch ruleset requires in every Numen repository), every step
  is commented, the token is read-only, and a `checklist` job reports the
  files the standards require without blocking while STD-015 is draft.
  Same two checks as before: `reuse lint` over every file and the tests that
  prove a missing declaration is refused.
- `.github/dependabot.yml` (GitHub Actions and pip, daily),
  `dependabot-auto-merge.yml` and `scorecard.yml` copied from the archive;
  `.github/CODEOWNERS` naming the licence map, the licence texts and the
  workflows.
- Import unchanged Avocado and Pot Vapor 02 originals, with primary Polygonal
  Mind CC0 evidence and explicit per-file notices.
- Link the canonical records and the complete review of 32 candidates.
- Add exact inventory, SHA-256, size, GLB container and VRM licence tests.
- No thumbnails, personal state, consumer changes or legacy repository cleanup.

## Foundation

- Establish the resource depot alongside numinia-archive, without importing
  media or duplicating the canonical catalogue.
- Declare licences by file and reserve the first migration for verified CC0.
- Check REUSE compliance in CI and exercise the missing-declaration gate
  against temporary, synthetic files.

Not included: ingestion, storage backends, download delivery, new resource
IDs or schemas, archive cleanup, and retirement of legacy repositories.
