# civic-publish-kit: City update rehearsal

civic-publish-kit rehearses a reviewable update cycle for the City of Surrey's public Development Applications
layer (MapServer layer 167 on `gisservices.surrey.ca`). It turns a dated, read-only extraction into checked local
tables, application-boundary GeoJSON and review reports, then packages them immutably with a local rollback
pointer.

This is a rehearsal using public data only. It is not City-endorsed software and it does not prove access to any
City system. City GIS publishes reviewed files through its own ArcGIS Online. This tool has no CMS, no admin UI,
uses no credentials and makes no remote writes.

## The six commands

| Command | What it does |
|---|---|
| `extract --config PATH --out PATH [--from-fixtures PATH]` | Reads layer 167 (or replays genuine frozen responses) into an immutable snapshot with exact query URLs, UTC times and SHA256 hashes. A failed live read never falls back to fixtures. |
| `normalize --config PATH --snapshot PATH --out PATH [--buildings CSV]` | Transforms EPSG:26910 to WGS84, then writes projects.csv, buildings.csv, projects.geojson and provenance. |
| `validate --input PATH --out PATH` | Re-verifies the bundle and writes report.html, errors.txt, validation.json and the table downloads. Exits 2 when blocked. |
| `diff --before ID_OR_empty --after ID --store PATH` | Prints `{"diff": ..., "diff_hash": ...}` with added, removed and field-level changes. |
| `package --input PATH --store PATH [--timestamp UTC] [--ack-diff HASH]` | Validates, then creates an immutable package and prints `release_id`, `diff_hash`, `diff` and `created`. Only an exact, fresh `--ack-diff` promotes it; the receipt then includes promotion details. |
| `rollback --release ID --store PATH` | Verifies a previously acknowledged/promoted package and its local review proof, then restores only the local pointer. Never promotes an unreviewed candidate. Prints `release_id`, `previous_release_id` and `action`. |

## Install

```bash
uv sync --all-extras --frozen
uv run civic-publish --help
```

For development without uv:

```bash
pip install -e '.[dev]'
```

## 60-second offline demo

```bash
bash scripts/demo.sh
```

The demo makes no service calls. It replays the genuine bounded fixtures `tests/fixtures/rehearsal/a` (where
`OBJECTID IN (1,2,3)`) and `tests/fixtures/rehearsal/b` (where `OBJECTID IN (1,2)`), and runs all six commands in a
fresh temporary directory:

1. Package A without an acknowledgment, then promote it with the printed hash.
2. Show that B's narrower query appears as an explicit removal with a warning.
3. Show that a stale hash is rejected and the current pointer is unchanged.
4. Promote B with its fresh hash.
5. Roll back to A, then compare the SHA256 of every A package file before and after.

The A-to-B removal comes from the change in query scope; it is not a City deletion. Set `DEMO_KEEP=1` to keep the
temporary outputs for review.

## Typical run

```bash
uv run civic-publish extract --config config/rehearsal.json --out work/snapshot
uv run civic-publish normalize --config config/rehearsal.json --snapshot work/snapshot --out work/bundle
uv run civic-publish validate --input work/bundle --out work/review
uv run civic-publish package --input work/bundle --store releases
uv run civic-publish diff --before empty --after RELEASE_ID --store releases
uv run civic-publish package --input work/bundle --store releases --ack-diff DIFF_HASH
uv run civic-publish rollback --release PRIOR_RELEASE_ID --store releases
```

Use `empty` only for the initial review; thereafter use the actual current package ID. The rollback target must
already have been acknowledged and promoted. See [the runbook](docs/RUNBOOK.md) for trusted-store retention and
failure recovery.

## Observed source state

These are observations, not guarantees. The exact URLs, UTC times and hashes are in `docs/live-run.md`.

- **Extraction:** a full live read at 2026-10-07T00:27:12Z returned 13,880 records with identical initial and final
  unique-ID sets. They arrived in 7 ordered pages (6 × 2,000 + 1,880), reconciled against the 2,000-record server
  limit, so nothing was truncated.
- **Normalization:** it produced 13,854 valid project rows. There are 15 blocking findings: 11 duplicated PROJECT_NO
  values (22 features) and 4 invalid native geometries. 2,709 records have a null DESCRIPTION; they keep a null title
  and a warning.
- **Packaging:** the full City source is correctly refused because of the blocking findings. It is not
  publication-ready.
- **Fixtures:** the committed fixtures are genuine bounded samples of the service, not a full export.

## Data model

`data_dictionary.md` and `field-definitions.csv` are generated from one field registry.

| Table | Field | ArcGIS type | Length | Nullable |
|---|---|---|---|---|
| projects | project_id (trimmed PROJECT_NO) | String | 50 | no |
| projects | title (DESCRIPTION) | String | 4000 | yes, because the City DESCRIPTION may be NULL |
| projects | status | String | 32 | no |
| projects | status_raw | String | 255 | no |
| projects | longitude, latitude | Double | | no |
| projects | location_basis, source_url, retrieved_at, licence | String | 32/255/20/255 | no |
| buildings | building_id | String | 64 | no |
| buildings | project_id | String | 50 | no |
| buildings | source_url | String | 255 | no |
| buildings | building_name | String | 255 | yes |
| buildings | storeys | SmallInteger | | yes |
| buildings | height_m | Double | | yes |
| buildings | building_use | String | 100 | yes |

How the tables relate:

- **Projects:** PROJECT_NO is the only project identity. OBJECTID is completeness evidence and is never used as an
  ID.
- **Statuses:** Final Approval, Conditional Approval, Under Review and Initial Review are mapped explicitly. Any
  other status stays `unknown` with a warning.
- **Geometry:** projects.geojson contains application boundaries, never building footprints. Each interior point is
  a Shapely representative point.
- **Buildings:** buildings.csv holds only separately supplied rows, passed with `--buildings` and requiring
  `building_id`, `project_id` and a public `source_url`. Without it the file has headers only, plus a warning.
  Buildings, heights and counts are never invented. Many buildings join to one project, and orphan rows are rejected.

## Safety and provenance

- **Allowed source:** only the official layer, host and fields are accepted. Credentials, token parameters and
  redirects are rejected. Requests time out after 20 seconds with at most two retries. Streamed response chunks
  count toward the 128 MiB aggregate source cap even when an interrupted attempt is discarded. Exhausting that
  budget stops the read rather than retrying.
- **CRS:** EPSG:26910 to EPSG:4326 is done with pyproj `always_xy=True` and PROJ network access disabled. The
  transform is checked against the server's own WGS84 response within 0.000001 degrees, plus an inverse round trip.
- **No repairs:** invalid or unclosed geometry blocks and is never repaired.
- **Recorded files:** every snapshot, bundle and package records an exact file manifest with SHA256 checksums.
  Symlinks, traversal, inconsistent files and overwrites are rejected within the trusted-store boundary below.
- **Reproducibility:** the same bundle, artifact templates, dependency versions, base package and fixed
  `--timestamp` produce identical bytes and the same package identity. Packaging defaults to source retrieval time.
  Changes to generated documentation, including RUNBOOK.md, change future artifact bytes and package identities;
  existing immutable packages are not rewritten.

## Package contents

Each package keeps all seven original normalized bundle files as audit metadata: projects.csv, buildings.csv,
projects.geojson, source-manifest.json, findings.json, field-mapping.json and bundle-manifest.json.

It also contains review files: data_dictionary.md, field-definitions.csv, validation.json, errors.txt,
report.html and RUNBOOK.md.

The store adds changes.json, CHANGELOG.md, release.json and checksums.sha256.

Review and promotion rules:

- Every removal must be reviewed. If more than 20% of projects disappear, a prominent warning is shown.
- A stale acknowledgment fails.
- Candidate creation is not promotion and does not establish rollback eligibility.
- Successful acknowledged promotion records local review evidence outside the immutable package.
- Rollback requires a previously acknowledged/promoted target with valid package bytes and intact review proof.
  It changes only the local `current.json` and has no remote effect.

## Trusted local store

Give trusted local operators exclusive write access to the store. Do not expose it to untrusted writers or
concurrent external filesystem changes. The lock serializes cooperating tool operations; it is not an
authorization boundary against a filesystem writer.

Review receipts use SHA256 consistency checks. They are not signed approvals and cannot authenticate approval
against someone able to rewrite the store, its receipts and its checksums.

Under the path supplied to `--store`, retain `releases/`, `reviews/` and `current.json` together. For example,
`--store releases` puts these entries inside that directory. Make consistent backups while no operation is
running. Copying only the 17-file package directories does not preserve their rollback eligibility.

## Interrupted operation recovery

Acceptance writes the pointer and commits review evidence in separate filesystem operations. An interruption
after pointer write but before receipt commitment can leave the store failing closed, even if the candidate's
package checksums are valid. Atomic rename does not provide a power-loss-durable multi-file transaction.

Preserve the original pointer, receipts, package bytes and failure evidence before attempting recovery. Use
`rollback --release PRIOR_ID` only for a verified prior reviewed package with intact proof and valid pointer
metadata. This can restore the prior package without modifying evidence in a damaged current package.

If no valid prior reviewed target is available, or pointer metadata cannot be verified, stop and retain the
evidence for investigation. Do not fabricate review proof or delete/edit `current.json` to bypass the failure.
Remove a stale `.lock` only after confirming that no operation is running.

## Open by design

- **Portable code:** plain Python 3.12 under Apache-2.0, with pinned dependencies and no proprietary runtime.
- **Open tables:** standard CSV and GeoJSON, readable without a map or any vendor tool.
- **Checkable provenance:** exact query URLs, UTC times and response hashes let anyone recheck a result against
  the public service.
- **Source licence:** the data is used under the Open Government License - City of Surrey, and the attribution
  travels with every row.

## Limitations

- There is no snapshot isolation upstream. Counts and IDs are re-read after paging to detect concurrent edits.
- Layer 167 describes applications, not buildings, and layer 155 is not read in this build.
- No ArcGIS Online tenancy testing or remote publication is performed or claimed.
- Local SHA256 review evidence assumes trusted store writers; it is not a signed approval system.
- File staging and pointer replacement do not make acceptance a power-loss-durable multi-file transaction.

## Development checks

```bash
uv run ruff check .
uv run mypy
uv run pytest
uv run python -m build
bash scripts/demo.sh
```

CI (`.github/workflows/ci.yml`) runs these on Python 3.12 with read-only permissions. It downloads packages to
install dependencies but makes no City service calls.

## Licence

Apache-2.0. Copyright 2026 ParalleX Labs Inc. See `LICENSE` and `CITATION.cff`.
