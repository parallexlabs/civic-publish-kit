"""Static review artifacts for normalized rehearsal bundles.

Public APIs, used by the CLI, the store and tests:
- REPORT_ARTIFACTS: data_dictionary.md, field-definitions.csv, validation.json, errors.txt, report.html and RUNBOOK.md.
- PACKAGE_ARTIFACTS: the seven verified bundle files (the six BUNDLE_FILES plus bundle-manifest.json) and the
  REPORT_ARTIFACTS. Together these are the store's 13 REQUIRED_DATA_ARTIFACTS.
- build_artifacts(source: Path | NormalizedBundle, *, package: bool = True) -> dict[str, bytes]
  Produces deterministic bytes derived only from the bundle: no clock, no randomness, no absolute paths.
  Bundle data files are copied as their original verified bytes. bundle-manifest.json is written as canonical JSON
  plus a newline, exactly as rehearsal.write_bundle writes it.
  With package=True, report.html also links changes.json and CHANGELOG.md, which the store generates.
- validation_json(bundle), errors_text(findings, ok), field_definitions_csv(), data_dictionary_md(bundle),
  report_html(bundle, present, package) and RUNBOOK_MD are pure helpers.
- validate_report(input, out) -> ValidationReport. Writes a fresh atomic directory, never overwritten and with no
  symlink ancestors, containing report-manifest.json and a sha256 for every file.
  A readable bundle produces all PACKAGE_ARTIFACTS; ok may still be False.
  An unreadable or tampered bundle produces only validation.json, errors.txt and report.html, each carrying one
  blocking finding. No table is fabricated in that case.
"""

from __future__ import annotations

import csv
import html
import io
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

from civic_publish_kit.acquire import AcquisitionError
from civic_publish_kit.config import canonical_json, parse_json_strict
from civic_publish_kit.normalize import Finding
from civic_publish_kit.rehearsal import (
    BUNDLE_FILES,
    BUNDLE_MANIFEST,
    FIELD_MAPPING,
    FOOTPRINT_NOTE,
    NormalizedBundle,
    RehearsalError,
    _write_tree,
    load_bundle,
)

JSON = dict[str, Any]
VALIDATION_SCHEMA: Final = "civic-publish-kit/rehearsal-validation/1"
REPORT_SCHEMA: Final = "civic-publish-kit/rehearsal-report/1"
REPORT_MANIFEST: Final = "report-manifest.json"
REPORT_ARTIFACTS: Final = ("data_dictionary.md", "field-definitions.csv", "validation.json", "errors.txt",
                           "report.html", "RUNBOOK.md")
PACKAGE_ARTIFACTS: Final = (*BUNDLE_FILES, BUNDLE_MANIFEST, *REPORT_ARTIFACTS)
FAILURE_ARTIFACTS: Final = ("validation.json", "errors.txt", "report.html")
PREVIEW_PROJECTS: Final = 25
PREVIEW_FINDINGS: Final = 50
TRUNCATE: Final = 240
_LABELS: Final = {"blocking": "BLOCKING", "warning": "WARNING", "exclusion": "EXCLUDED"}
_DOWNLOADS: Final = (
    ("projects.csv", "Project table (CSV)"),
    ("buildings.csv", "Building table (CSV, supplied rows only)"),
    ("projects.geojson", "Application boundaries in WGS84 (GeoJSON)"),
    ("field-definitions.csv", "Field definitions (CSV)"),
    ("data_dictionary.md", "Data dictionary (Markdown)"),
    ("errors.txt", "All errors and warnings (plain text)"),
    ("validation.json", "Validation result (JSON)"),
    ("RUNBOOK.md", "One-page runbook (Markdown)"),
    ("changes.json", "Field-level changes against the base release (JSON)"),
    ("CHANGELOG.md", "Change log (Markdown)"),
)
RUNBOOK_MD: Final = """# Rehearsal runbook (one page)

Public City of Surrey data only: no credentials or remote writes. City GIS publishes reviewed files through its
own ArcGIS Online. Application boundaries are not building footprints.

1. Extract: `civic-publish extract --config config/rehearsal.json --out work/snapshot`
   Add `--from-fixtures PATH` for genuine frozen replay. A failed live read never falls back to fixtures.
2. Normalize: `civic-publish normalize --config config/rehearsal.json --snapshot work/snapshot --out work/bundle`
   Add `--buildings FILE.csv` only for separately sourced rows with building_id, project_id and public HTTPS
   source_url. Optional columns: building_name, storeys, height_m, building_use. Without rows, buildings.csv has
   headers only and a warning; buildings are never derived from application geometry.
3. Validate: `civic-publish validate --input work/bundle --out work/review`
   Read report.html, errors.txt and both CSV tables. Exit code 2 means blocking issues prevent publication.
   Use HTML for review. Import all CSV columns as text, not automatic formulas/types. CSV retains exact normalized
   source values; no spreadsheet-specific escaping or transformation is applied.
4. Package: `civic-publish package --input work/bundle --store releases`
   This creates an immutable candidate and prints release_id, diff_hash and diff. Current remains unchanged.
5. Review: `civic-publish diff --before CURRENT_ID_OR_empty --after RELEASE_ID --store releases`
   Review every removal. More than 20 percent of projects disappearing is prominently warned.
6. Acknowledge: rerun step 4 with `--ack-diff HASH`, using the exact fresh hash for current. A stale hash fails.
   Successful promotion records local review evidence outside the immutable package.
7. City GIS publication: City GIS staff load reviewed projects.csv, buildings.csv and projects.geojson through
   their own ArcGIS Online procedures. This tool does not publish or modify a hosted service.
8. Rollback: `civic-publish rollback --release PRIOR_ID --store releases`
   Only a previously acknowledged/promoted target with valid original bytes and review proof is eligible.
   Rollback restores only the local current pointer; City GIS handles hosted-layer recovery separately.

## Trusted store and recovery

Give trusted local operators exclusive write access; no untrusted or concurrent external filesystem writers.
SHA256 receipts establish consistency, not signed approval against someone who can rewrite the store. Under
--store PATH, preserve releases/, reviews/ and current.json together; make consistent backups with operations
stopped. Packages alone do not preserve rollback eligibility.

Interrupted acceptance can fail closed after pointer write but before receipt commitment. Preserve original
evidence. Recover only by rollback to a verified prior reviewed target with intact proof and valid pointer
metadata; a damaged current package need not be rewritten. If that is unavailable, stop and retain evidence.
Never fabricate proof or delete/edit current.json to bypass the failure. Remove a stale .lock only after
confirming no operation is running. Atomic rename is not a power-loss-durable multi-file transaction.
"""


@dataclass(frozen=True, slots=True)
class ValidationReport:
    ok: bool
    counts: JSON | None
    out: Path
    report_path: Path
    files: tuple[str, ...]
    errors: tuple[str, ...]


def _flat(value: object) -> str:
    return " ".join(("" if value is None else str(value)).split())


def _e(value: object, limit: int = TRUNCATE) -> str:
    text = _flat(value)
    if len(text) > limit:
        text = text[:limit] + "..."
    return html.escape(text, quote=True)


def _md(value: object) -> str:
    return _flat(value).replace("|", "\\|").replace("`", "'")


def _dict(value: object) -> JSON:
    return value if isinstance(value, dict) else {}


def _findings_array(bundle: NormalizedBundle) -> list[object]:
    data = parse_json_strict(bundle.files["findings.json"].decode("utf-8"))
    if not isinstance(data, list):
        raise RehearsalError("findings.json: expected a list")
    return data


def validation_json(bundle: NormalizedBundle) -> bytes:
    payload = {"schema": VALIDATION_SCHEMA, "ok": bundle.ok, "counts": bundle.manifest["counts"],
               "findings": _findings_array(bundle)}
    return canonical_json(payload) + b"\n"


def errors_text(findings: Sequence[Finding], ok: bool) -> bytes:
    blocking = sum(1 for f in findings if f.severity == "blocking")
    warnings = sum(1 for f in findings if f.severity == "warning")
    head = "PASSED: no blocking errors." if ok else (
        "BLOCKED: publication is not allowed until every blocking error below is fixed.")
    lines = [f"Validation result: {head}", f"Blocking errors: {blocking}", f"Warnings: {warnings}", ""]
    for f in findings:
        where = ", ".join(x for x in (f"code {f.code}", f"record {f.record_id}" if f.record_id else "",
                                      f"source {f.source_id}" if f.source_id else "") if x)
        lines.append(f"{_LABELS.get(f.severity, f.severity.upper())}: {_flat(f.message)} [{_flat(where)}]")
    return ("\n".join(lines).rstrip("\n") + "\n").encode("utf-8")


def field_definitions_csv() -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(["table", "name", "arcgis_type", "length", "nullable", "description"])
    for m in FIELD_MAPPING:
        writer.writerow([m.table, m.name, m.arcgis_type, "" if m.length is None else str(m.length),
                         "true" if m.nullable else "false", m.description])
    return buffer.getvalue().encode("utf-8")


def data_dictionary_md(bundle: NormalizedBundle) -> bytes:
    prov = bundle.provenance
    query, crs, licence = _dict(prov.get("query")), _dict(prov.get("crs")), _dict(prov.get("licence"))
    counts = _dict(bundle.manifest.get("counts"))
    lines = [
        "# Data dictionary", "",
        "## What the tables describe", "",
        "- projects.csv and projects.geojson: one row per development application from City layer 167. "
        f"{FOOTPRINT_NOTE}",
        "- projects.geojson geometry is the application boundary polygon in WGS84. longitude and latitude are the "
        "Shapely representative interior point of that boundary, not a building location.",
        "- project_id is the trimmed PROJECT_NO, the stable project identity. OBJECTID is completeness evidence only "
        "and is never used as an identity.",
        "- buildings.csv contains only separately supplied rows. Buildings, heights and counts are never derived from "
        "application boundaries or descriptions. If no rows were supplied, the file contains headers only.",
        "- Relationship: one project to many buildings. buildings.project_id must match projects.project_id; orphan "
        "rows are rejected.", "",
        "## Coordinates", "",
        f"- Source CRS {_md(crs.get('source'))}, target CRS {_md(crs.get('target'))}, pyproj always_xy="
        f"{_md(crs.get('always_xy'))}, network access {_md(crs.get('network'))}.",
        f"- Transformation: {_md(crs.get('transformer'))} (pyproj {_md(crs.get('pyproj'))}, "
        f"PROJ {_md(crs.get('proj'))}).", "",
        "## Fields", "",
        "| table | name | ArcGIS type | length | nullable | description |",
        "|---|---|---|---|---|---|",
    ]
    for m in FIELD_MAPPING:
        length = "" if m.length is None else str(m.length)
        lines.append(f"| {m.table} | {m.name} | {m.arcgis_type} | {length} | {'yes' if m.nullable else 'no'} | "
                     f"{_md(m.description)} |")
    lines += [
        "", "## CSV safety", "",
        "Use report.html for review. When importing CSV into a spreadsheet, explicitly import every column as text, "
        "not automatic formulas or types. CSV retains exact normalized source values; no spreadsheet-specific "
        "escaping or transformation is applied.",
        "", "## Source and licence", "",
        f"- Layer: {_md(prov.get('layer_url'))}",
        f"- Query where: {_md(query.get('where'))} (scope: {_md(query.get('scope'))})",
        f"- Retrieved (UTC): {_md(prov.get('retrieved_at'))}",
        f"- Projects: {_md(counts.get('projects'))}; buildings: {_md(counts.get('buildings'))}",
        f"- Complete service count observed at capture: {_md(prov.get('complete_service_count'))} "
        "(an observation, not a guarantee)",
        f"- Licence: {_md(licence.get('name'))}, {_md(licence.get('url'))}. {_md(licence.get('attribution'))}",
        f"- Snapshot isolation: {_md(prov.get('snapshot_isolation'))}", "",
        "## Exact query URLs", "",
    ]
    for entry in prov.get("requests") or []:
        item = _dict(entry)
        stamp = item.get("retrieved_at", item.get("requested_at"))
        lines.append(f"- {_md(item.get('file'))}: {_md(item.get('url'))} (UTC {_md(stamp)})")
    lines += ["", "## Publication", "",
              "City GIS publishes the reviewed files through its own ArcGIS Online. This tool makes no remote changes."]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _table(caption: str, headers: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    out = ["<table>", f"<caption>{_e(caption)}</caption>", "<thead><tr>"]
    out += [f'<th scope="col">{_e(h)}</th>' for h in headers]
    out.append("</tr></thead><tbody>")
    for row in rows:
        out.append("<tr>" + "".join(f"<td>{_e(c)}</td>" for c in row) + "</tr>")
    out.append("</tbody></table>")
    return out


def _page(title: str, body: list[str]) -> bytes:
    head = ['<!DOCTYPE html>', '<html lang="en">', '<head><meta charset="utf-8">', f"<title>{_e(title)}</title>",
            "</head>", "<body>", "<main>", f"<h1>{_e(title)}</h1>"]
    tail = ["<p>Generated from the bundle only. No scripts, maps, remote assets or admin controls.</p>",
            "</main>", "</body>", "</html>"]
    return ("\n".join(head + body + tail) + "\n").encode("utf-8")


def _downloads(present: Sequence[str]) -> list[str]:
    items = [f'<li><a href="{_e(name)}">{_e(name)}</a>: {_e(label)}</li>' for name, label in _DOWNLOADS
             if name in present]
    return ["<h2>Downloads</h2>", "<ul>", *items, "</ul>"] if items else []


def report_html(bundle: NormalizedBundle, present: Sequence[str], package: bool) -> bytes:
    prov = bundle.provenance
    query, licence, counts = _dict(prov.get("query")), _dict(prov.get("licence")), _dict(bundle.manifest["counts"])
    findings = bundle.findings
    blocking = [f for f in findings if f.severity == "blocking"]
    links = [n for n in present if package or n not in ("changes.json", "CHANGELOG.md")]
    body: list[str] = []
    if bundle.ok:
        body.append("<p><strong>Result: PASSED.</strong> No blocking errors were found. The files may be packaged for "
                    "review; City GIS publishes them through its own ArcGIS Online.</p>")
    else:
        body.append(f"<p><strong>Result: BLOCKED.</strong> Publication is blocked. {len(blocking)} blocking error(s) "
                    "must be fixed first; every error is listed in errors.txt.</p>")
    sample = query.get("scope") == "service_sample"
    scope = ("Bounded sample of the City service, not a complete export. Counts describe the sample only."
             if sample else "Complete read of the configured query.")
    body += ["<h2>Scope and source</h2>", *_table("Source details", ("Item", "Value"), [
        ("Source layer", prov.get("layer_url")), ("Query where", query.get("where")), ("Scope", scope),
        ("Retrieved (UTC)", prov.get("retrieved_at")),
        ("Complete service count observed", f"{_flat(prov.get('complete_service_count'))} "
                                            "(an observation at capture time, not a guarantee)"),
        ("Projects in this bundle", counts.get("projects")), ("Buildings in this bundle", counts.get("buildings")),
        ("Blocking errors", counts.get("blocking")), ("Warnings", counts.get("warnings")),
        ("Licence", f"{_flat(licence.get('name'))}. {_flat(licence.get('attribution'))}"),
        ("Geometry", FOOTPRINT_NOTE),
    ])]
    notices: list[str] = []
    if sample:
        notices.append("This is a bounded service sample, not a complete City export.")
    if counts.get("buildings") == 0:
        notices.append("No building rows were supplied, so buildings.csv contains headers only.")
    unknown = sum(1 for f in findings if f.code == "unrecognized_status")
    if unknown:
        notices.append(f"{unknown} project(s) have an unrecognized status and are recorded as unknown.")
    if blocking:
        notices.append("Source blocking issues (for example duplicate PROJECT_NO values or invalid geometry) "
                       "prevent packaging and publication.")
    if notices:
        body += ["<h2>Notices</h2>", "<ul>", *[f"<li>{_e(n)}</li>" for n in notices], "</ul>"]
    body += _downloads(links)
    shown = findings[:PREVIEW_FINDINGS]
    body += ["<h2>Findings</h2>", *_table("Errors and warnings", ("Severity", "Code", "Record", "Message"),
                                          [(_LABELS.get(f.severity, f.severity), f.code, f.record_id or "", f.message)
                                           for f in shown])]
    if len(findings) > len(shown):
        body.append(f"<p>Showing {len(shown)} of {len(findings)} findings. The full list is in errors.txt.</p>")
    rows = bundle.projects[:PREVIEW_PROJECTS]
    cols = ("project_id", "title", "status", "status_raw", "longitude", "latitude")
    body += ["<h2>Project preview</h2>", *_table("Projects", cols, [[r[c] for c in cols] for r in rows])]
    if len(bundle.projects) > len(rows):
        body.append(f"<p>Showing {len(rows)} of {len(bundle.projects)} projects. Download projects.csv for all.</p>")
    return _page("Validation report: City of Surrey development applications (rehearsal)", body)


def build_artifacts(source: Path | NormalizedBundle, *, package: bool = True) -> dict[str, bytes]:
    bundle = source if isinstance(source, NormalizedBundle) else load_bundle(Path(source))
    files: dict[str, bytes] = {name: bytes(bundle.files[name]) for name in BUNDLE_FILES}
    files[BUNDLE_MANIFEST] = canonical_json(bundle.manifest) + b"\n"
    files["data_dictionary.md"] = data_dictionary_md(bundle)
    files["field-definitions.csv"] = field_definitions_csv()
    files["validation.json"] = validation_json(bundle)
    files["errors.txt"] = errors_text(bundle.findings, bundle.ok)
    files["RUNBOOK.md"] = RUNBOOK_MD.encode("utf-8")
    generated: tuple[str, ...] = ("changes.json", "CHANGELOG.md") if package else ()
    present = [*PACKAGE_ARTIFACTS, *generated]
    files["report.html"] = report_html(bundle, present, package)
    return files


def failure_artifacts(message: str) -> dict[str, bytes]:
    finding = Finding("blocking", "bundle_unreadable", f"The normalized bundle could not be verified: {message}")
    validation = {"schema": VALIDATION_SCHEMA, "ok": False, "counts": None, "findings": [finding.to_dict()]}
    body = ["<p><strong>Result: BLOCKED.</strong> The input could not be verified, so no tables are shown and "
            "publication is blocked.</p>", "<h2>Error</h2>", f"<p>{_e(finding.message, 2000)}</p>",
            *_downloads(FAILURE_ARTIFACTS)]
    return {"validation.json": canonical_json(validation) + b"\n", "errors.txt": errors_text([finding], False),
            "report.html": _page("Validation report: input could not be verified", body)}


def _sanitize(message: str, path: Path) -> str:
    for text in sorted({str(path.absolute()), str(path)}, key=len, reverse=True):
        if text and text != ".":
            message = message.replace(text, "<input>")
    return message


def validate_report(input_path: Path, out: Path) -> ValidationReport:
    source = Path(input_path)
    counts: JSON | None
    errors: tuple[str, ...]
    try:
        bundle = load_bundle(source)
    except (AcquisitionError, ValueError, OSError) as exc:
        message = _sanitize(str(exc), source)
        files, ok, counts, errors = failure_artifacts(message), False, None, (message,)
    else:
        files, ok, counts = build_artifacts(bundle, package=False), bundle.ok, _dict(bundle.manifest["counts"])
        errors = tuple(f.message for f in bundle.findings if f.severity == "blocking")
    _write_tree(Path(out), files, REPORT_MANIFEST, {"schema": REPORT_SCHEMA, "ok": ok})
    target = Path(out).absolute()
    return ValidationReport(ok, counts, target, target / "report.html", tuple(sorted(files)), errors)
