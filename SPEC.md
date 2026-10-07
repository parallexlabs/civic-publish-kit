# civic-publish-kit — frozen rehearsal specification v2

Frozen 2026-10-06 after the lead narrowed the build to the City update rehearsal. This supersedes v1's release-product scope. Preserve and reuse the assembled author-authored modules and tests as shared foundations; retain v1 locally as evidence. Python 3.12 and Apache-2.0. Public data only. This is a rehearsal, not a City production deployment.

## Commands and boundaries

| Command | Required behavior |
|---|---|
| extract --config config/rehearsal.json --out PATH [--from-fixtures PATH] | Capture an explicitly dated, complete query snapshot from layer 167, or replay genuine frozen responses. Record exact prepared query URLs, UTC times and hashes. Never fall back to fixtures after a live failure. |
| normalize --config PATH --snapshot PATH --out PATH [--buildings CSV] | Transform native EPSG:26910 coordinates to WGS84 and emit separate project and building tables, application-boundary GeoJSON and provenance. Stable project identity is PROJECT_NO, never OBJECTID. Building IDs and joins must be explicitly supplied. |
| validate --input PATH --out PATH | Verify normalized files/hashes/schema, report plain-language blocking errors and warnings, and emit a small escaped HTML report with table downloads and errors.txt. Nonzero on blockers. |
| diff --before ID_OR_empty --after ID --store PATH | Show added, removed and field-level changes for projects and buildings, plus an acknowledgment SHA256 binding both package identities. Exclude retrieval timestamps from semantic comparisons. |
| package --input PATH --store PATH [--timestamp UTC] [--ack-diff HASH] | Validate then create an immutable, versioned package without overwriting a prior package. With no acknowledgment, leave current unchanged and print the candidate and review hash. An exact fresh acknowledgment may atomically promote it. Every deletion requires explicit review; stale acknowledgments fail. |
| rollback --release ID --store PATH | Verify the selected prior package and restore the local current pointer. Its package files must be byte-for-byte identical to their original checksummed bytes. No remote effects. |

Only these six commands are exposed by the rehearsal CLI. No CMS, custom admin UI, production credentials, remote writes or access to any City ArcGIS Online tenancy. City GIS publishes the reviewed files through its own ArcGIS Online.

## Verified source and extraction

Layer: https://gisservices.surrey.ca/arcgis/rest/services/OpenData/MapServer/167
Query endpoint: https://gisservices.surrey.ca/arcgis/rest/services/OpenData/MapServer/167/query
Read-only metadata verified 2026-10-06: Development Applications; polygon geometry; EPSG:26910; maxRecordCount 2000; JSON, geoJSON and PBF query formats; ordered pagination supported. where=1=1 count and unique object-ID list both contain 13,880 records at the observed time. Counts are observations, not hard-coded expectations.

Allowlist gisservices.surrey.ca. Reject credentials, token query parameters, redirects and unconfigured hosts. Fixed metadata/query endpoints only. Timeout 20 seconds; at most two retries on transient transport, 429 or 5xx. Bound records to 20,000 and aggregate source response bytes to 128 MiB. Batch/page size never exceeds the server limit of 2000. Use explicit public outFields OBJECTID,PROJECT_NO,DESCRIPTION,STATUS; no applicant/contact fields. OBJECTID is completeness evidence only.

Read metadata, count and all object IDs before data pages. Request f=geojson,outSR=26910, with stable OBJECTID ordering and explicit offsets/page sizes, or use bounded object-ID batches. Reconcile every page against the expected unique IDs and final count/ID re-read. Detect missing, duplicate, unexpected or truncated records; unresolved transfer-limit responses block. The 2000 limit must never silently truncate. Disclose lack of upstream snapshot isolation.

Native GeoJSON from this service explicitly declares crs EPSG:26910. Transform using pyproj with always_xy=True, with source/target CRS recorded. Do not silently interpret metre coordinates as longitude/latitude. Remove native CRS declarations from WGS84 output. Verify against paired genuine native and server-WGS84 responses within 0.000001 degrees and an inverse round-trip tolerance. Invalid or unclosed geometries block; no silent repairs.

Licence: Open Government License – City of Surrey, https://opendata-surrey.hub.arcgis.com/pages/55089a19491a4fe59a41e059fd8af708. Retain source attribution and retrieval evidence.

## Tables, validation and evidence

Projects retain stable project_id from trimmed PROJECT_NO, title from DESCRIPTION, exact status_raw, an explicit normalized status, source URL/identity, retrieval time and licence, and WGS84 coordinates or application geometry. Explicit familiar statuses may map to approved, conditionally_approved or review/unknown; unrecognized raw statuses remain unknown with a clear warning, never inferred construction or approval. Missing or duplicate stable IDs block, never fall back to OBJECTID. Flat scalar properties and finite valid coordinates only.

Layer 167 describes applications and application boundaries, not individual building footprints. Emit buildings.csv separately. Without separately sourced building rows it is an empty table with headers and an explicit warning. Optionally accept a supplied building CSV with stable building_id, project_id join and public source_url, plus documented optional scalar fields. Reject duplicate/missing IDs, orphan joins and malformed values. Do not fabricate buildings, heights, footprints or counts from application polygons or description text; do not query layer 155 in this narrowed build.

Write deterministic CSV tables and WGS84 GeoJSON, a documented ArcGIS field mapping, validation.json, plain errors.txt, and a small report.html. HTML escapes every untrusted value, uses semantic headings/tables with headers, plain-text severity, keyboard-operable links and relative table-download links. No scripts, remote assets or admin UI. Report tables remain usable without a map.

Every snapshot/normalized bundle/package has a schema, exact file manifest and SHA256 checksums. Preserve genuine original fixture response bytes, dated queries and response hashes; label bounded fixture queries as samples of the service, not complete service exports. Synthetic test inputs are labelled test-only.

## Packages and review

Reuse the existing content-addressed ReleaseStore and diff/acknowledgment/atomic-pointer logic, adapting its artifact whitelist and semantic view to project/building tables. No irrelevant pilot-area dependencies are required. Packages include projects.csv, buildings.csv, projects.geojson (application geometry, never building geometry), field mapping/data dictionary, validation.json, errors.txt, report.html, source-manifest.json, RUNBOOK.md, changes.json, CHANGELOG.md, release.json and checksums.sha256. Each GeoJSON is below 100 MB. Deterministic fixed timestamps and identical inputs give identical file hashes and identity. Creation and pointer changes are staged/verified/atomic; reject traversal, symlinks, tampering and overwrites. Blocking validation cannot be packaged or promoted. Report all removals, and prominently warn if more than 20 percent of projects disappear.

The one-page runbook explains extract → normalize → validate → diff/review → package/acknowledge → City GIS publication → rollback. It states City GIS uses its own ArcGIS Online and that local rollback restores the reviewed files, not a remote service. Document exact field names, ArcGIS types, lengths, nullability and project/building relationships. No tenancy testing or remote publication is claimed.

## Gates and delivery

The primary author and independent test author write every application module, test, fixture loader, CI file, README and runbook. Assemble returned code verbatim; send exact failures back. Never locally author implementation. Reuse existing foundations and tests; resolve their outstanding lint/type failures.

Required gates: offline pytest with >=80% total application coverage, Ruff, strict mypy Python 3.12, wheel/sdist build, all-six-command CLI smoke, live read-only extraction/validation evidence with UTCs and counts, paired genuine CRS checks, 2001+ record/truncation/missing-ID tests, deterministic hashes, deletion-review/stale-ack failures, rollback byte identity, HTML escaping/downloads and plain errors. README, Apache-2.0 LICENSE (ParalleX Labs Inc.), CITATION.cff, CI Python 3.12 contents:read, and a short reproducible demo/runbook complete the repository.

Retry failed, empty, cut or capacity-refused requests as NEW distinct requests after 3–5 minutes; poll capacity and never stop because of these. Request only the remainder of a cut answer; split after two failures. Never duplicate an accepted pending job. Keep at most two task requests in flight. Cancel superseded task jobs, not other workers' work.

After assembly and passing gates, pack every tracked file with the supplied full_repo_pack.py, check the local confidentiality guard before staging externally, then two independent full-repository review requests for HIGH/MEDIUM/LOW findings and patches. Apply corrections through the author repair loop, rerun all gates, commit as the configured Git user with a plain message. No push/repository creation. Untracked STATUS.md records scope, results, the private request ledger, review findings/outcomes and any unfinished items. BUILD_LOG.md records provenance and fixes. Restricted campaign/bid files are never sent to the authors; only this bounded public-data spec and code/data are sent.
