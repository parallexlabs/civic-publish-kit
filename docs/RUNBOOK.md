# Rehearsal runbook (one page)

Public City of Surrey data only: no credentials or remote writes. City GIS publishes reviewed files through its
own ArcGIS Online. Application boundaries are not building footprints.

1. Extract: `uv run civic-publish extract --config config/rehearsal.json --out work/snapshot`
   Add `--from-fixtures tests/fixtures/rehearsal/a` for genuine frozen replay. Failed live reads never fall back.
2. Normalize: `uv run civic-publish normalize --config config/rehearsal.json --snapshot work/snapshot --out work/bundle`
   Add `--buildings buildings.csv` only for separately sourced rows with `building_id`, `project_id` joining an
   existing PROJECT_NO, and public HTTPS `source_url`. Optional columns: `building_name`, `storeys`, `height_m`,
   `building_use`. Without supplied rows, buildings.csv has headers only and a warning.
3. Validate: `uv run civic-publish validate --input work/bundle --out work/review`
   Read report.html, errors.txt and both CSV tables. Exit code 2 means blocking issues prevent publication.
   Use HTML for review. Import all CSV columns as text, not automatic formulas/types. CSV retains exact normalized
   source values; no spreadsheet-specific escaping or transformation is applied.
4. Package: `uv run civic-publish package --input work/bundle --store releases`
   This creates an immutable candidate and prints `release_id`, `diff_hash`, `diff` and `created`. Current remains
   unchanged; candidate existence alone does not authorize rollback.
5. Review: `uv run civic-publish diff --before CURRENT_ID_OR_empty --after RELEASE_ID --store releases`
   Review every removal. More than 20 percent of projects disappearing is prominently warned.
6. Acknowledge: `uv run civic-publish package --input work/bundle --store releases --ack-diff DIFF_HASH`
   Use the exact fresh hash for current. A stale hash fails. Successful promotion records local review evidence
   outside the immutable package.
7. City GIS publication: staff load reviewed projects.csv, buildings.csv and projects.geojson through their own
   ArcGIS Online procedures. This tool does not publish or modify a hosted service.
8. Rollback: `uv run civic-publish rollback --release PRIOR_ID --store releases`
   Only a previously acknowledged/promoted target with verified original bytes and intact review proof is eligible.
   This restores only the local current pointer. City GIS handles hosted-layer recovery separately.

## Trusted store and recovery

Give trusted operators exclusive local write access; no untrusted or concurrent external filesystem writers.
SHA256 receipts check consistency, not signed approval against someone who can rewrite the store. Under the
chosen `--store PATH`, preserve `releases/`, `reviews/` and `current.json` together. Make consistent backups while
operations are stopped; packages alone do not preserve rollback eligibility.

Interrupted acceptance can fail closed after pointer write but before receipt commitment. Preserve original
pointer, receipts, package bytes and failure evidence. Recover only by rollback to a verified prior reviewed
target with intact proof and valid pointer metadata; do not rewrite a damaged current package. If that recovery
is unavailable, stop and retain evidence. Never fabricate proof or delete/edit `current.json` to bypass failure.
Remove a stale `.lock` only after confirming no operation is running. Atomic rename is not a power-loss-durable
multi-file transaction.
