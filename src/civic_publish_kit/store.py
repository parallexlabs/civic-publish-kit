"""Rehearsal v2 immutable packages, semantic diff and local review pointers.

The six CLI commands are extract, normalize, validate, diff, package and rollback.
package creates a candidate without changing current.json; package --ack-diff
uses accept() to recompute and acknowledge a fresh diff under the store lock.

Data contains exactly REQUIRED_DATA_ARTIFACTS, including the original normalized
bundle. Shared validate_bundle_files() enforces its schemas and consistency.
The store adds changes.json, CHANGELOG.md, release.json and checksums.sha256.
All generated files are verified before projecting a package's data-only view.

Identity is r- + SHA256(canonical schema/base/data hashes)[:32]. Every original
artifact byte participates. Semantic comparisons exclude retrieval timestamps
and the two derived manifest hashes, but preserve raw response hashes and URLs.
Projects are keyed by project_id; buildings by building_id. Acknowledgments bind
both package identities. Packages are immutable; current.json is outside them.

reviews/<release_id>.json is local acceptance evidence, outside package identity.
It binds the verified package bytes, fresh acknowledged diff/base and acceptance
time. A receipt is committed only after pointer promotion succeeds. Failed
receipt commitment restores the preceding pointer and removes new authorization.
A crash before receipt commitment leaves no rollback authorization: fail closed.
Existing candidates or pointer action strings are never substituted for proof.

Rollback requires valid committed acceptance evidence for its verified target.
It checks current pointer metadata, not a damaged current package, and changes
only the pointer. No package or prior receipt bytes are rewritten by rollback.
All effects are local. No network calls, City writes or subprocesses occur.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import math
import os
import re
import secrets
import shutil
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from civic_publish_kit.config import (
    MAX_GEOJSON_OUTPUT_BYTES,
    REMOVAL_WARNING_FRACTION,
    ConfigError,
    canonical_json,
    parse_json_strict,
)
from civic_publish_kit.rehearsal import (
    BUILDING_COLUMNS,
    BUNDLE_FILES,
    BUNDLE_MANIFEST,
    FIELD_MAPPING,
    PROJECT_COLUMNS,
    STATUSES,
    FieldMap,
    NormalizedBundle,
    RehearsalError,
    validate_bundle_files,
)

JSON = dict[str, Any]
RELEASE_SCHEMA: Final = "civic-publish-kit/release/1"
DIFF_SCHEMA: Final = "civic-publish-kit/diff/1"
CURRENT_SCHEMA: Final = "civic-publish-kit/current/1"
VALIDATION_SCHEMA: Final = "civic-publish-kit/rehearsal-validation/1"
REVIEW_SCHEMA: Final = "civic-publish-kit/review/1"
REVIEW_DIR: Final = "reviews"
EMPTY_BASELINE: Final = "empty"
RELEASE_ID_PATTERN: Final = re.compile(r"^r-[0-9a-f]{32}$")
REQUIRED_DATA_ARTIFACTS: Final = (
    *BUNDLE_FILES,
    BUNDLE_MANIFEST,
    "data_dictionary.md",
    "field-definitions.csv",
    "validation.json",
    "errors.txt",
    "report.html",
    "RUNBOOK.md",
)
OPTIONAL_DATA_ARTIFACTS: Final[tuple[str, ...]] = ()
DATA_ARTIFACTS: Final = REQUIRED_DATA_ARTIFACTS + OPTIONAL_DATA_ARTIFACTS
GENERATED_ARTIFACTS: Final = ("changes.json", "CHANGELOG.md")
RELEASE_FILE: Final = "release.json"
CHECKSUM_FILE: Final = "checksums.sha256"
CURRENT_FILE: Final = "current.json"
LOCK_FILE: Final = ".lock"
STAGING_DIR: Final = ".staging"
GEOJSON_ARTIFACTS: Final = ("projects.geojson",)
DIFF_COLLECTIONS: Final = ("projects", "buildings", "sources")
TIME_KEYS: Final = frozenset({"retrieved_at", "requested_at", "created_at", "captured_at", "updated_at"})
_NAME: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_TIMESTAMP: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_HASH: Final = re.compile(r"^[0-9a-f]{64}$")
_CHECKSUM_LINE: Final = re.compile(r"^([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9_.-]{0,63})$")
_FIELD_COLUMNS: Final = ("table", "name", "arcgis_type", "length", "nullable", "description")
_POINTER_KEYS: Final = frozenset({"schema", "release_id", "previous_release_id", "action", "updated_at"})
_REVIEW_KEYS: Final = frozenset({
    "schema", "release_id", "package_sha256", "base_release_id", "diff", "diff_hash", "accepted_at", "sha256",
})

ChangelogRenderer = Callable[[JSON], bytes]


class StoreError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReleaseRecord:
    release_id: str
    base_release_id: str | None
    created_at: str
    diff_hash: str
    removal_warning: bool
    path: Path
    files: Mapping[str, bytes]


@dataclass(frozen=True, slots=True)
class CandidateResult:
    release_id: str
    created: bool
    diff: JSON
    diff_hash: str


@dataclass(frozen=True, slots=True)
class PointerResult:
    release_id: str
    previous_release_id: str | None
    action: str
    diff_hash: str | None
    removal_warning: bool
    summary: JSON


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(data: bytes, what: str) -> object:
    try:
        return parse_json_strict(data.decode("utf-8"))
    except (UnicodeDecodeError, ConfigError) as exc:
        raise StoreError(f"{what}: invalid JSON ({exc})") from exc


def _obj(value: object, what: str) -> JSON:
    if not isinstance(value, dict):
        raise StoreError(f"{what}: expected a JSON object")
    return value


def _timestamp(value: str, what: str) -> str:
    if not _TIMESTAMP.fullmatch(value):
        raise StoreError(f"{what}: expected UTC timestamp YYYY-MM-DDTHH:MM:SSZ, got {value!r}")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise StoreError(f"{what}: invalid UTC calendar timestamp {value!r}") from exc
    return value


def validate_release_id(value: str) -> str:
    if not RELEASE_ID_PATTERN.fullmatch(value):
        raise StoreError(f"invalid release ID {value!r}")
    return value


def _safe_path(path: Path) -> Path:
    """Inspect lexical absolute components before any operation; never resolve aliases."""
    absolute = path.absolute()
    if ".." in absolute.parts:
        raise StoreError(f"{path}: path traversal is not allowed")
    for component in (*reversed(absolute.parents), absolute):
        if component.is_symlink():
            raise StoreError(f"{component}: symlinks are not allowed in the store")
    return absolute


def _read_bytes(path: Path) -> bytes:
    checked = _safe_path(path)
    if not checked.is_file():
        raise StoreError(f"{path}: expected a regular file")
    if checked.stat().st_size > MAX_GEOJSON_OUTPUT_BYTES:
        raise StoreError(f"{path}: too large")
    return checked.read_bytes()


def _strip_times(value: object) -> object:
    if isinstance(value, dict):
        return {k: _strip_times(v) for k, v in value.items() if k not in TIME_KEYS}
    if isinstance(value, list):
        return [_strip_times(v) for v in value]
    return value


def _strip_source_times(value: object, *, capture: bool = False) -> object:
    """Drop only retrieval times and explicitly identified derived manifest hashes."""
    if isinstance(value, dict):
        return {
            k: _strip_source_times(v, capture=k == "capture")
            for k, v in value.items()
            if k not in TIME_KEYS
            and k != "snapshot_manifest_sha256"
            and not (capture and k == "manifest_sha256")
        }
    if isinstance(value, list):
        return [_strip_source_times(v, capture=capture) for v in value]
    return value


def _keyed_features(data: bytes, name: str, keys: tuple[str, ...]) -> dict[str, JSON]:
    coll = _obj(_parse(data, name), name)
    features = coll.get("features")
    if coll.get("type") != "FeatureCollection" or not isinstance(features, list):
        raise StoreError(f"{name}: expected a GeoJSON FeatureCollection")
    out: dict[str, JSON] = {}
    for index, raw in enumerate(features):
        feature = _obj(raw, f"{name} feature {index}")
        props = _obj(feature.get("properties"), f"{name} feature {index} properties")
        if set(props) != set(PROJECT_COLUMNS) or props.get("status") not in STATUSES:
            raise StoreError(f"{name} feature {index}: invalid project fields or status")
        if any(isinstance(v, dict | list) for v in props.values()):
            raise StoreError(f"{name} feature {index}: nested properties are not allowed")
        parts = [props.get(k) for k in keys]
        if not all(isinstance(p, str) and p and p == p.strip() for p in parts):
            raise StoreError(f"{name} feature {index}: missing or untrimmed key fields {', '.join(keys)}")
        key = ":".join(str(p) for p in parts)
        if key in out:
            raise StoreError(f"{name}: duplicate key {key!r}")
        record = _obj(_strip_times(props), name)
        record["geometry"] = feature.get("geometry")
        out[key] = record
    return out


def _sources_view(data: bytes) -> dict[str, JSON]:
    original = _obj(_parse(data, "source-manifest.json"), "source-manifest.json")
    manifest = _obj(_strip_source_times(original), "source-manifest.json")
    out: dict[str, JSON] = {}
    sources = manifest.get("sources")
    if isinstance(sources, dict):
        for sid, value in sources.items():
            out[str(sid)] = value if isinstance(value, dict) else {"value": value}
    rest = {k: v for k, v in manifest.items() if k != "sources"}
    if rest:
        out["_manifest"] = rest
    return out


def _building_view(bundle: NormalizedBundle) -> dict[str, JSON]:
    out: dict[str, JSON] = {}
    for row in bundle.buildings:
        record: JSON = {column: row[column] for column in BUILDING_COLUMNS}
        try:
            record["storeys"] = int(row["storeys"]) if row["storeys"] else None
            height = float(row["height_m"]) if row["height_m"] else None
        except ValueError as exc:
            raise StoreError(f"buildings.csv: invalid numeric value for {row['building_id']!r}") from exc
        if height is not None and (not math.isfinite(height) or height < 0):
            raise StoreError(f"buildings.csv: invalid height_m for {row['building_id']!r}")
        if record["storeys"] is not None and record["storeys"] < 0:
            raise StoreError(f"buildings.csv: invalid storeys for {row['building_id']!r}")
        record["height_m"] = 0.0 if height == 0 else height
        key = row["building_id"]
        if not key or key in out:
            raise StoreError(f"buildings.csv: missing or duplicate building_id {key!r}")
        out[key] = record
    return out


def _check_field_definitions(data: bytes) -> None:
    try:
        rows = list(csv.reader(io.StringIO(data.decode("utf-8"), newline=""), strict=True))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise StoreError(f"field-definitions.csv: unreadable UTF-8 CSV ({exc})") from exc
    if not rows or rows[0] != list(_FIELD_COLUMNS):
        raise StoreError(f"field-definitions.csv: header must be exactly {', '.join(_FIELD_COLUMNS)}")
    expected: dict[tuple[str, str], FieldMap] = {(field.table, field.name): field for field in FIELD_MAPPING}
    seen: set[tuple[str, str]] = set()
    for line, values in enumerate(rows[1:], start=2):
        if len(values) != len(_FIELD_COLUMNS):
            raise StoreError(f"field-definitions.csv: line {line} has an invalid column count")
        row = dict(zip(_FIELD_COLUMNS, values, strict=True))
        key = (row["table"], row["name"])
        field = expected.get(key)
        if field is None or key in seen:
            raise StoreError(f"field-definitions.csv: unknown or duplicate field {key!r}")
        seen.add(key)
        length = "" if field.length is None else str(field.length)
        nullable = row["nullable"].lower()
        if (
            row["arcgis_type"] != field.arcgis_type
            or row["length"] != length
            or nullable not in {"true", "false"}
            or (nullable == "true") != field.nullable
            or row["description"] != field.description
        ):
            raise StoreError(f"field-definitions.csv: definition differs from FIELD_MAPPING for {key!r}")
    if seen != set(expected):
        raise StoreError("field-definitions.csv: missing FIELD_MAPPING entries")


def _check_data(data: Mapping[str, bytes]) -> None:
    unknown = sorted(set(data) - set(DATA_ARTIFACTS))
    missing = sorted(set(REQUIRED_DATA_ARTIFACTS) - set(data))
    if unknown or missing:
        raise StoreError(f"data artifacts invalid (unknown={unknown}, missing={missing})")
    for name, content in data.items():
        if not isinstance(content, bytes):
            raise StoreError(f"{name}: artifact content must be bytes")
        if name in GEOJSON_ARTIFACTS and len(content) >= MAX_GEOJSON_OUTPUT_BYTES:
            raise StoreError(f"{name}: GeoJSON must be below {MAX_GEOJSON_OUTPUT_BYTES} bytes")


def _validated_bundle(data: Mapping[str, bytes]) -> NormalizedBundle:
    _check_data(data)
    manifest = _obj(_parse(data[BUNDLE_MANIFEST], BUNDLE_MANIFEST), BUNDLE_MANIFEST)
    originals = {name: data[name] for name in BUNDLE_FILES}
    try:
        bundle = validate_bundle_files(originals, manifest)
    except RehearsalError as exc:
        raise StoreError(f"normalized bundle failed validation: {exc}") from exc
    if not bundle.ok or bundle.manifest.get("ok") is not True:
        raise StoreError("normalized bundle has blocking findings; packaging is not permitted")
    validation = _obj(_parse(data["validation.json"], "validation.json"), "validation.json")
    if validation.get("schema") != VALIDATION_SCHEMA or validation.get("ok") is not True:
        raise StoreError("validation.json: unsupported schema or validation is not ok")
    counts = _obj(validation.get("counts"), "validation.json counts")
    original_counts = _obj(bundle.manifest.get("counts"), "bundle-manifest.json counts")
    if canonical_json(counts) != canonical_json(original_counts):
        raise StoreError("validation.json: counts differ from bundle-manifest.json")
    findings = validation.get("findings")
    original_findings = _parse(data["findings.json"], "findings.json")
    if not isinstance(findings, list) or not isinstance(original_findings, list):
        raise StoreError("validation.json and findings.json must contain finding arrays")
    for entry in findings:
        finding = _obj(entry, "validation.json finding")
        if finding.get("severity") not in {"warning", "exclusion"}:
            raise StoreError("validation.json: blocking or malformed finding")
    if canonical_json(findings) != canonical_json(original_findings):
        raise StoreError("validation.json: findings differ from findings.json")
    _check_field_definitions(data["field-definitions.csv"])
    return bundle


def semantic_view(files: Mapping[str, bytes]) -> JSON:
    """Revalidate the exact data-only mapping and exclude retrieval-only semantic changes."""
    bundle = _validated_bundle(files)
    return {
        "projects": _keyed_features(files["projects.geojson"], "projects.geojson", ("project_id",)),
        "buildings": _building_view(bundle),
        "sources": _sources_view(files["source-manifest.json"]),
    }


def _release_view(record: ReleaseRecord) -> JSON:
    """Project data only after verify_release() has checked the complete package."""
    data = {name: record.files[name] for name in DATA_ARTIFACTS}
    return semantic_view(data)


def compute_diff(base_id: str | None, base_view: JSON | None, candidate_id: str, candidate_view: JSON) -> JSON:
    """Field-level canonical diff; None identity and view together mean an empty baseline."""
    if (base_id is None) != (base_view is None):
        raise StoreError("base identity and base view must both be present or both be empty")
    old_view: JSON = base_view if base_view is not None else {name: {} for name in DIFF_COLLECTIONS}
    collections: JSON = {}
    for name in DIFF_COLLECTIONS:
        old: dict[str, JSON] = old_view.get(name, {})
        new: dict[str, JSON] = candidate_view.get(name, {})
        changed: JSON = {}
        for key in sorted(old.keys() & new.keys()):
            fields = {
                field: {"old": old[key].get(field), "new": new[key].get(field)}
                for field in sorted(old[key].keys() | new[key].keys())
                if canonical_json(old[key].get(field)) != canonical_json(new[key].get(field))
            }
            if fields:
                changed[key] = fields
        collections[name] = {
            "added": sorted(new.keys() - old.keys()),
            "removed": sorted(old.keys() - new.keys()),
            "changed": changed,
        }
    projects = collections["projects"]
    buildings = collections["buildings"]
    base_count = len(old_view.get("projects", {}))
    fraction = len(projects["removed"]) / base_count if base_count else 0.0
    summary = {
        "base_projects": base_count,
        "candidate_projects": len(candidate_view.get("projects", {})),
        "added_projects": len(projects["added"]),
        "removed_projects": len(projects["removed"]),
        "changed_projects": len(projects["changed"]),
        "base_buildings": len(old_view.get("buildings", {})),
        "candidate_buildings": len(candidate_view.get("buildings", {})),
        "added_buildings": len(buildings["added"]),
        "removed_buildings": len(buildings["removed"]),
        "changed_buildings": len(buildings["changed"]),
        "removal_fraction": round(fraction, 6),
        "removal_warning": fraction > REMOVAL_WARNING_FRACTION,
        "has_removals": any(collections[name]["removed"] for name in DIFF_COLLECTIONS),
    }
    return {
        "schema": DIFF_SCHEMA,
        "base": base_id,
        "candidate": candidate_id,
        "collections": collections,
        "summary": summary,
    }


def diff_hash(diff: JSON) -> str:
    return _sha(canonical_json(diff))


def compute_release_id(data: Mapping[str, bytes], base_release_id: str | None) -> str:
    """Content identity includes every data artifact byte, including retrieval evidence."""
    _check_data(data)
    if base_release_id is not None:
        validate_release_id(base_release_id)
    payload = {
        "schema": RELEASE_SCHEMA,
        "base_release_id": base_release_id,
        "artifacts": {name: _sha(data[name]) for name in sorted(data)},
    }
    return "r-" + _sha(canonical_json(payload))[:32]


def _md(text: object) -> str:
    return str(text).replace("`", "'").replace("\n", " ")


def render_changelog(diff: JSON) -> bytes:
    summary = diff["summary"]
    lines = [
        f"# Changes for package {_md(diff['candidate'])}",
        "",
        f"Base package: {_md(diff['base'] or 'empty baseline')}",
        "",
        f"Acknowledgment hash: `{diff_hash(diff)}`",
        "",
    ]
    if summary["removal_warning"]:
        lines += [
            f"**Warning: {summary['removed_projects']} of {summary['base_projects']} projects removed "
            f"(more than {int(REMOVAL_WARNING_FRACTION * 100)}%).**",
            "",
        ]
    for name in DIFF_COLLECTIONS:
        part = diff["collections"][name]
        lines += [
            f"## {name}",
            "",
            f"- Added: {len(part['added'])}",
            f"- Removed: {len(part['removed'])}",
            f"- Changed: {len(part['changed'])}",
            "",
        ]
        lines += [f"- added `{_md(key)}`" for key in part["added"]]
        lines += [f"- removed `{_md(key)}`" for key in part["removed"]]
        lines += [
            f"- changed `{_md(key)}`: {', '.join(_md(field) for field in values)}"
            for key, values in part["changed"].items()
        ]
        lines.append("")
    return ("\n".join(lines).rstrip("\n") + "\n").encode("utf-8")


def build_checksums(files: Mapping[str, bytes]) -> bytes:
    return "".join(f"{_sha(files[name])}  {name}\n" for name in sorted(files)).encode("ascii")


def parse_checksums(data: bytes) -> dict[str, str]:
    try:
        text = data.decode("ascii")
    except UnicodeDecodeError as exc:
        raise StoreError("checksums.sha256: not ASCII") from exc
    if not text.endswith("\n"):
        raise StoreError("checksums.sha256: must end with a newline")
    out: dict[str, str] = {}
    for line in text[:-1].split("\n"):
        match = _CHECKSUM_LINE.fullmatch(line)
        if match is None or match.group(2) in out:
            raise StoreError(f"checksums.sha256: malformed or duplicate line {line!r}")
        out[match.group(2)] = match.group(1)
    return out


def assemble_release(
    data: Mapping[str, bytes],
    base_release_id: str | None,
    base_view: JSON | None,
    created_at: str,
    renderer: ChangelogRenderer = render_changelog,
) -> tuple[str, dict[str, bytes], JSON]:
    """Pure validated assembly: package ID, complete file set and diff against the base."""
    _timestamp(created_at, "created_at")
    view = semantic_view(data)
    release_id = compute_release_id(data, base_release_id)
    diff = compute_diff(base_release_id, base_view, release_id, view)
    files: dict[str, bytes] = dict(data)
    files["changes.json"] = canonical_json(diff) + b"\n"
    changelog = renderer(diff)
    if not isinstance(changelog, bytes):
        raise StoreError("CHANGELOG.md: renderer must return bytes")
    files["CHANGELOG.md"] = changelog
    release = {
        "schema": RELEASE_SCHEMA,
        "release_id": release_id,
        "base_release_id": base_release_id,
        "created_at": created_at,
        "identity_artifacts": sorted(data),
        "artifacts": {name: {"sha256": _sha(files[name]), "bytes": len(files[name])} for name in sorted(files)},
        "diff_hash": diff_hash(diff),
        "removal_warning": diff["summary"]["removal_warning"],
    }
    files[RELEASE_FILE] = canonical_json(release) + b"\n"
    files[CHECKSUM_FILE] = build_checksums(files)
    return release_id, files, diff


def verify_release_files(files: Mapping[str, bytes], release_id: str) -> JSON:
    """Pure checksum, identity and shared semantic validation of the exact package file set."""
    validate_release_id(release_id)
    expected_files = {*DATA_ARTIFACTS, *GENERATED_ARTIFACTS, RELEASE_FILE, CHECKSUM_FILE}
    if set(files) != expected_files:
        missing = sorted(expected_files - set(files))
        extra = sorted(set(files) - expected_files)
        raise StoreError(f"{release_id}: package file set invalid (missing={missing}, extra={extra})")
    for name, content in files.items():
        if not _NAME.fullmatch(name) or not isinstance(content, bytes):
            raise StoreError(f"unsafe artifact name or content {name!r}")
    sums = parse_checksums(files[CHECKSUM_FILE])
    if set(sums) != set(files) - {CHECKSUM_FILE}:
        raise StoreError(f"{release_id}: file set differs from checksums.sha256")
    for name, digest in sums.items():
        if not hmac.compare_digest(_sha(files[name]), digest):
            raise StoreError(f"{release_id}: checksum mismatch for {name} (tampered)")
    release = _obj(_parse(files[RELEASE_FILE], RELEASE_FILE), RELEASE_FILE)
    if release.get("schema") != RELEASE_SCHEMA or release.get("release_id") != release_id:
        raise StoreError(f"{release_id}: release.json schema or identity mismatch")
    base = release.get("base_release_id")
    if base is not None and (not isinstance(base, str) or not RELEASE_ID_PATTERN.fullmatch(base)):
        raise StoreError(f"{release_id}: invalid base_release_id")
    _timestamp(str(release.get("created_at")), f"{release_id} created_at")
    artifacts = _obj(release.get("artifacts"), "release.json artifacts")
    if set(artifacts) != set(files) - {RELEASE_FILE, CHECKSUM_FILE}:
        raise StoreError(f"{release_id}: release.json artifact list differs from files")
    for name, meta in artifacts.items():
        entry = _obj(meta, f"release.json artifacts.{name}")
        size = entry.get("bytes")
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or size != len(files[name])
            or entry.get("sha256") != _sha(files[name])
        ):
            raise StoreError(f"{release_id}: release.json entry mismatch for {name}")
    expected = sorted(DATA_ARTIFACTS)
    if release.get("identity_artifacts") != expected:
        raise StoreError(f"{release_id}: identity artifact list mismatch")
    data: dict[str, bytes] = {name: files[name] for name in expected}
    if compute_release_id(data, base) != release_id:
        raise StoreError(f"{release_id}: content address does not match artifacts")
    view = semantic_view(data)
    diff = _obj(_parse(files["changes.json"], "changes.json"), "changes.json")
    if diff.get("schema") != DIFF_SCHEMA or diff.get("candidate") != release_id or diff.get("base") != base:
        raise StoreError(f"{release_id}: changes.json identities mismatch")
    stored_hash = release.get("diff_hash")
    if not isinstance(stored_hash, str) or not _HASH.fullmatch(stored_hash) or diff_hash(diff) != stored_hash:
        raise StoreError(f"{release_id}: changes.json hash mismatch")
    summary = _obj(diff.get("summary"), "changes.json summary")
    warning = release.get("removal_warning")
    if not isinstance(warning, bool) or summary.get("removal_warning") is not warning:
        raise StoreError(f"{release_id}: removal warning differs from changes.json")
    if (
        summary.get("candidate_projects") != len(view["projects"])
        or summary.get("candidate_buildings") != len(view["buildings"])
    ):
        raise StoreError(f"{release_id}: candidate counts differ from validated data")
    if base is None and canonical_json(diff) != canonical_json(compute_diff(None, None, release_id, view)):
        raise StoreError(f"{release_id}: initial changes.json differs from validated data")
    return release


def _write_new(path: Path, data: bytes) -> None:
    checked = _safe_path(path)
    with checked.open("xb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _review_document(record: ReleaseRecord, base: str | None, diff: JSON, at: str) -> JSON:
    payload: JSON = {
        "schema": REVIEW_SCHEMA,
        "release_id": record.release_id,
        "package_sha256": _sha(record.files[CHECKSUM_FILE]),
        "base_release_id": base,
        "diff": diff,
        "diff_hash": diff_hash(diff),
        "accepted_at": _timestamp(at, "review accepted_at"),
    }
    return {**payload, "sha256": _sha(canonical_json(payload))}


def _validate_review(value: object, record: ReleaseRecord) -> JSON:
    proof = _obj(value, "review proof")
    if set(proof) != _REVIEW_KEYS or proof.get("schema") != REVIEW_SCHEMA:
        raise StoreError("review proof: invalid schema or keys")
    release_id = proof.get("release_id")
    if not isinstance(release_id, str) or validate_release_id(release_id) != record.release_id:
        raise StoreError("review proof: package identity mismatch")
    base = proof.get("base_release_id")
    if base is not None:
        if not isinstance(base, str):
            raise StoreError("review proof: invalid base_release_id")
        validate_release_id(base)
        if base == release_id:
            raise StoreError("review proof: base must differ from candidate")
    for key in ("sha256", "package_sha256", "diff_hash"):
        digest = proof.get(key)
        if not isinstance(digest, str) or not _HASH.fullmatch(digest):
            raise StoreError(f"review proof: invalid {key}")
    at = proof.get("accepted_at")
    if not isinstance(at, str):
        raise StoreError("review proof: accepted_at must be a UTC timestamp")
    _timestamp(at, "review accepted_at")
    payload = {key: item for key, item in proof.items() if key != "sha256"}
    if not hmac.compare_digest(_sha(canonical_json(payload)), proof["sha256"]):
        raise StoreError("review proof: checksum mismatch")
    if not hmac.compare_digest(_sha(record.files[CHECKSUM_FILE]), proof["package_sha256"]):
        raise StoreError("review proof: verified package bytes differ from accepted bytes")
    diff = _obj(proof.get("diff"), "review proof diff")
    if (
        set(diff) != {"schema", "base", "candidate", "collections", "summary"}
        or diff.get("schema") != DIFF_SCHEMA
        or diff.get("base") != base
        or diff.get("candidate") != release_id
    ):
        raise StoreError("review proof: diff schema or identities mismatch")
    collections = _obj(diff.get("collections"), "review proof collections")
    if set(collections) != set(DIFF_COLLECTIONS):
        raise StoreError("review proof: invalid diff collections")
    _obj(diff.get("summary"), "review proof summary")
    if not hmac.compare_digest(diff_hash(diff), proof["diff_hash"]):
        raise StoreError("review proof: acknowledged diff hash mismatch")
    return proof


class ReleaseStore:
    def __init__(self, root: Path) -> None:
        self.root = _safe_path(Path(root))
        self.releases = self.root / "releases"

    def _ensure(self) -> None:
        for path in (self.root, self.releases, self.root / STAGING_DIR, self.root / REVIEW_DIR):
            checked = _safe_path(path)
            checked.mkdir(parents=True, exist_ok=True)
            _safe_path(checked)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        path = _safe_path(self.root / LOCK_FILE)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            raise StoreError(f"store is locked by another operation; if none is running remove {path}") from exc
        try:
            os.write(fd, f"{os.getpid()}\n".encode("ascii"))
            yield
        finally:
            os.close(fd)
            _safe_path(path).unlink(missing_ok=True)

    def _read_dir(self, path: Path) -> dict[str, bytes]:
        checked = _safe_path(path)
        if not checked.is_dir():
            raise StoreError(f"{path}: expected a real package directory")
        files: dict[str, bytes] = {}
        for entry in sorted(checked.iterdir()):
            _safe_path(entry)
            if not entry.is_file() or not _NAME.fullmatch(entry.name):
                raise StoreError(f"{path.name}: unexpected entry {entry.name!r} (directory or unsafe name)")
            files[entry.name] = _read_bytes(entry)
        return files

    def list_releases(self) -> list[str]:
        checked = _safe_path(self.releases)
        if not checked.is_dir():
            return []
        names: list[str] = []
        for entry in checked.iterdir():
            _safe_path(entry)
            if RELEASE_ID_PATTERN.fullmatch(entry.name) and entry.is_dir():
                names.append(entry.name)
        return sorted(names)

    def verify_release(self, release_id: str) -> ReleaseRecord:
        path = _safe_path(self.releases / validate_release_id(release_id))
        if not path.is_dir():
            raise StoreError(f"release {release_id} not found")
        files = self._read_dir(path)
        release = verify_release_files(files, release_id)
        return ReleaseRecord(
            release_id,
            release.get("base_release_id"),
            str(release["created_at"]),
            str(release["diff_hash"]),
            bool(release["removal_warning"]),
            path,
            files,
        )

    def _review_path(self, release_id: str) -> Path:
        return _safe_path(self.root / REVIEW_DIR / f"{validate_release_id(release_id)}.json")

    def _read_review(self, record: ReleaseRecord) -> JSON:
        path = self._review_path(record.release_id)
        if not path.exists():
            raise StoreError(
                f"{record.release_id}: no committed review proof; rollback requires a previously "
                "acknowledged and promoted package"
            )
        return _validate_review(_parse(_read_bytes(path), "review proof"), record)

    def _pointer_id(self) -> str | None:
        path = _safe_path(self.root / CURRENT_FILE)
        if not path.exists():
            return None
        pointer = _obj(_parse(_read_bytes(path), CURRENT_FILE), CURRENT_FILE)
        release_id = pointer.get("release_id")
        previous = pointer.get("previous_release_id")
        action = pointer.get("action")
        updated = pointer.get("updated_at")
        if (
            set(pointer) != _POINTER_KEYS
            or pointer.get("schema") != CURRENT_SCHEMA
            or not isinstance(release_id, str)
            or action not in {"accept", "rollback"}
            or not isinstance(updated, str)
        ):
            raise StoreError("current.json: invalid pointer metadata")
        validate_release_id(release_id)
        if previous is not None:
            if not isinstance(previous, str):
                raise StoreError("current.json: invalid previous_release_id")
            validate_release_id(previous)
            if previous == release_id:
                raise StoreError("current.json: previous_release_id must differ from release_id")
        _timestamp(updated, "current.json updated_at")
        return release_id

    def read_current(self) -> str | None:
        """Read-only verified current package ID and acceptance evidence, or None."""
        release_id = self._pointer_id()
        if release_id is not None:
            self._read_review(self.verify_release(release_id))
        return release_id

    def _view(self, release_id: str | None) -> JSON | None:
        return None if release_id is None else _release_view(self.verify_release(release_id))

    def _write_current(self, release_id: str, previous: str | None, action: str, at: str) -> None:
        validate_release_id(release_id)
        if previous is not None:
            validate_release_id(previous)
        if action not in {"accept", "rollback"}:
            raise StoreError("invalid pointer action")
        payload = {
            "schema": CURRENT_SCHEMA,
            "release_id": release_id,
            "previous_release_id": previous,
            "action": action,
            "updated_at": _timestamp(at, "updated_at"),
        }
        if self._pointer_id() != previous:
            raise StoreError("current pointer changed during the operation")
        temp = _safe_path(self.root / f".current-{secrets.token_hex(8)}.tmp")
        try:
            _write_new(temp, canonical_json(payload) + b"\n")
            if self._pointer_id() != previous:
                raise StoreError("current pointer changed during the operation")
            target = _safe_path(self.root / CURRENT_FILE)
            _safe_path(temp).replace(target)
        except BaseException:
            _safe_path(temp).unlink(missing_ok=True)
            raise

    def _restore_current(self, before: bytes | None) -> None:
        """Restore exact preceding pointer bytes after a failed acceptance commit."""
        target = _safe_path(self.root / CURRENT_FILE)
        if before is None:
            target.unlink(missing_ok=True)
            return
        temp = _safe_path(self.root / f".current-restore-{secrets.token_hex(8)}.tmp")
        try:
            _write_new(temp, before)
            _safe_path(temp).replace(_safe_path(target))
        finally:
            _safe_path(temp).unlink(missing_ok=True)

    def _promote_reviewed(self, record: ReleaseRecord, current: str | None, diff: JSON, at: str) -> None:
        """Commit first-acceptance proof only after promotion; keep earlier valid proof immutable."""
        path = self._review_path(record.release_id)
        if path.exists():
            self._read_review(record)
            self._write_current(record.release_id, current, "accept", at)
            return
        document = _review_document(record, current, diff, at)
        _validate_review(document, record)
        pointer_path = _safe_path(self.root / CURRENT_FILE)
        before = _read_bytes(pointer_path) if pointer_path.exists() else None
        temp = _safe_path(path.parent / f".review-{secrets.token_hex(8)}.tmp")
        promoted = False
        committed = False
        try:
            _write_new(temp, canonical_json(document) + b"\n")
            self._write_current(record.release_id, current, "accept", at)
            promoted = True
            _safe_path(path)
            if path.exists():
                raise StoreError("review proof appeared during acceptance; refusing to overwrite")
            _safe_path(temp).rename(path)
            committed = True
            self._read_review(record)
        except BaseException:
            if committed:
                _safe_path(path).unlink(missing_ok=True)
            if promoted:
                self._restore_current(before)
            raise
        finally:
            _safe_path(temp).unlink(missing_ok=True)

    def create_candidate(
        self,
        data: Mapping[str, bytes],
        *,
        created_at: str,
        renderer: ChangelogRenderer = render_changelog,
    ) -> CandidateResult:
        """Stage and verify an immutable candidate; never change current or authorize rollback."""
        _timestamp(created_at, "created_at")
        self._ensure()
        with self._locked():
            base = self.read_current()
            release_id, files, diff = assemble_release(data, base, self._view(base), created_at, renderer)
            final = _safe_path(self.releases / release_id)
            if final.exists():
                record = self.verify_release(release_id)
                existing = _obj(_parse(record.files["changes.json"], "changes.json"), "changes.json")
                return CandidateResult(release_id, False, existing, record.diff_hash)
            staging_root = _safe_path(self.root / STAGING_DIR)
            staging = _safe_path(Path(tempfile.mkdtemp(prefix="build-", dir=staging_root)))
            try:
                for name, content in sorted(files.items()):
                    _write_new(staging / name, content)
                verify_release_files(self._read_dir(staging), release_id)
                _safe_path(final)
                if final.exists():
                    raise StoreError(f"{release_id}: appeared during package creation; refusing to overwrite")
                _safe_path(staging).rename(final)
            except BaseException:
                shutil.rmtree(staging, ignore_errors=True)
                raise
            self.verify_release(release_id)
            return CandidateResult(release_id, True, diff, diff_hash(diff))

    def diff(self, before: str, after: str) -> JSON:
        base = None if before == EMPTY_BASELINE else validate_release_id(before)
        candidate = self.verify_release(after)
        return compute_diff(base, self._view(base), after, _release_view(candidate))

    def accept(self, candidate_id: str, ack_hash: str, *, accepted_at: str | None = None) -> PointerResult:
        """Implement package --ack-diff: verify, acknowledge and commit local evidence under the lock."""
        if not _HASH.fullmatch(ack_hash):
            raise StoreError("acknowledgment must be a 64-character lowercase SHA256 hex digest")
        self._ensure()
        with self._locked():
            record = self.verify_release(candidate_id)
            current = self.read_current()
            if current == candidate_id:
                raise StoreError(f"{candidate_id} is already current")
            diff = compute_diff(current, self._view(current), candidate_id, _release_view(record))
            expected = diff_hash(diff)
            if not hmac.compare_digest(expected, ack_hash):
                before = current or EMPTY_BASELINE
                raise StoreError(
                    f"acknowledgment does not match the diff against current ({before}); rerun "
                    f"diff --before {before} --after {candidate_id}, review it, then package with --ack-diff"
                )
            self._promote_reviewed(record, current, diff, accepted_at or _now())
            summary: JSON = diff["summary"]
            return PointerResult(candidate_id, current, "accept", expected, bool(summary["removal_warning"]), summary)

    def rollback(self, release_id: str, *, at: str | None = None) -> PointerResult:
        """Restore only a verified, previously acknowledged/promoted package; never promote a candidate."""
        self._ensure()
        with self._locked():
            record = self.verify_release(release_id)
            previous = self._pointer_id()
            if previous == release_id:
                raise StoreError(f"{release_id} is already current")
            self._read_review(record)
            self._write_current(release_id, previous, "rollback", at or _now())
            return PointerResult(release_id, previous, "rollback", None, False, {})
