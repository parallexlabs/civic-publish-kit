"""Package review, immutable recovery and validation regressions for the actual store."""

from __future__ import annotations

import copy
import csv
import hashlib
import io
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from civic_publish_kit.config import canonical_json, parse_json_strict
from civic_publish_kit.normalize import Finding
from civic_publish_kit.rehearsal import (
    BUILDING_COLUMNS,
    BUNDLE_FILES,
    BUNDLE_MANIFEST,
    NormalizedBundle,
    NormalizedResult,
    extract,
    load_bundle,
    load_rehearsal_config,
    load_rehearsal_snapshot,
    normalize_snapshot_data,
    write_bundle,
)
from civic_publish_kit.reports import build_artifacts
from civic_publish_kit.store import (
    CHECKSUM_FILE,
    CURRENT_FILE,
    DIFF_COLLECTIONS,
    EMPTY_BASELINE,
    RELEASE_FILE,
    REQUIRED_DATA_ARTIFACTS,
    ReleaseStore,
    StoreError,
    assemble_release,
    build_checksums,
    compute_diff,
    compute_release_id,
    diff_hash,
    parse_checksums,
    semantic_view,
    validate_release_id,
    verify_release_files,
)

Json = dict[str, Any]
ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / "tests" / "fixtures" / "rehearsal"
STAMP = "2026-10-06T23:30:00Z"


def _obj(value: object) -> Json:
    assert isinstance(value, dict)
    return cast(Json, value)


def _json(data: bytes) -> Json:
    return _obj(parse_json_strict(data.decode("utf-8")))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalized(name: str, base: Path) -> NormalizedResult:
    cfg = load_rehearsal_config(ROOT / "config" / "rehearsal.json")
    snapshot = base / f"snapshot-{name}"
    extract(cfg, snapshot, CAPTURES / name)
    return normalize_snapshot_data(cfg, load_rehearsal_snapshot(snapshot, cfg))


def _bundle(result: NormalizedResult, path: Path) -> NormalizedBundle:
    write_bundle(path, result)
    return load_bundle(path)


def _csv(rows: list[list[object]]) -> bytes:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _refresh_bundle_hashes(data: dict[str, bytes]) -> None:
    manifest = _json(data[BUNDLE_MANIFEST])
    manifest["files"] = {name: _sha(data[name]) for name in BUNDLE_FILES}
    data[BUNDLE_MANIFEST] = canonical_json(manifest) + b"\n"


def _forged_package(data: dict[str, bytes], valid: Mapping[str, bytes]) -> tuple[str, dict[str, bytes]]:
    """Test-only attacker recomputes hashes and identity; semantic gates must still reject."""
    files = dict(valid)
    files.update(data)
    release_id = compute_release_id(data, None)
    changes = _json(files["changes.json"])
    changes["candidate"] = release_id
    files["changes.json"] = canonical_json(changes) + b"\n"
    release = _json(files[RELEASE_FILE])
    release["release_id"] = release_id
    release["diff_hash"] = diff_hash(changes)
    release["artifacts"] = {
        name: {"sha256": _sha(body), "bytes": len(body)}
        for name, body in files.items()
        if name not in {RELEASE_FILE, CHECKSUM_FILE}
    }
    files[RELEASE_FILE] = canonical_json(release) + b"\n"
    files[CHECKSUM_FILE] = build_checksums({name: body for name, body in files.items() if name != CHECKSUM_FILE})
    return release_id, files


@pytest.fixture
def result_a(tmp_path: Path) -> NormalizedResult:
    result = _normalized("a", tmp_path.resolve())
    assert result.ok
    return result


@pytest.fixture
def data_a(result_a: NormalizedResult, tmp_path: Path) -> dict[str, bytes]:
    return build_artifacts(_bundle(result_a, tmp_path.resolve() / "bundle-a"))


@pytest.fixture
def data_b(tmp_path: Path) -> dict[str, bytes]:
    result = _normalized("b", tmp_path.resolve())
    assert result.ok
    return build_artifacts(_bundle(result, tmp_path.resolve() / "bundle-b"))


@pytest.fixture
def store(tmp_path: Path) -> ReleaseStore:
    return ReleaseStore(tmp_path.resolve() / "store")


def test_exact_whitelist_and_verified_data_only_semantics(data_a: dict[str, bytes]) -> None:
    expected = {
        "projects.csv", "buildings.csv", "projects.geojson", "source-manifest.json", "findings.json",
        "field-mapping.json", "bundle-manifest.json", "data_dictionary.md", "field-definitions.csv",
        "validation.json", "errors.txt", "report.html", "RUNBOOK.md",
    }
    assert set(REQUIRED_DATA_ARTIFACTS) == set(data_a) == expected
    assert DIFF_COLLECTIONS == ("projects", "buildings", "sources")
    release_id, files, changes = assemble_release(data_a, None, None, STAMP)
    assert len(files) == 17
    assert verify_release_files(files, release_id)["diff_hash"] == diff_hash(changes)
    assert len(semantic_view(data_a)["projects"]) == 3
    with pytest.raises(StoreError, match="unknown"):
        semantic_view(files)


def test_full_candidate_diff_accept_read_current_b_removal_and_rollback(
    store: ReleaseStore, data_a: dict[str, bytes], data_b: dict[str, bytes],
) -> None:
    assert store.list_releases() == []
    assert store.read_current() is None
    a = store.create_candidate(data_a, created_at=STAMP)
    original_a = dict(store.verify_release(a.release_id).files)
    assert len(original_a) == 17
    assert store.read_current() is None
    initial = store.diff(EMPTY_BASELINE, a.release_id)
    assert initial == a.diff
    assert initial["summary"]["added_projects"] == 3
    promoted = store.accept(a.release_id, diff_hash(initial), accepted_at=STAMP)
    assert promoted.previous_release_id is None
    assert store.read_current() == a.release_id
    b = store.create_candidate(data_b, created_at=STAMP)
    original_b = dict(store.verify_release(b.release_id).files)
    assert store.read_current() == a.release_id
    changes = store.diff(a.release_id, b.release_id)
    assert changes["summary"]["base_projects"] == 3
    assert changes["summary"]["candidate_projects"] == 2
    assert changes["summary"]["removed_projects"] == 1
    assert changes["summary"]["removal_warning"] is True
    assert changes["summary"]["has_removals"] is True
    assert len(changes["collections"]["projects"]["removed"]) == 1
    pointer = (store.root / CURRENT_FILE).read_bytes()
    with pytest.raises(StoreError, match="does not match"):
        store.accept(b.release_id, a.diff_hash, accepted_at=STAMP)
    assert (store.root / CURRENT_FILE).read_bytes() == pointer
    assert store.read_current() == a.release_id
    reviewed = store.accept(b.release_id, diff_hash(changes), accepted_at=STAMP)
    assert reviewed.previous_release_id == a.release_id
    assert reviewed.removal_warning
    assert store.read_current() == b.release_id
    rollback = store.rollback(a.release_id, at=STAMP)
    assert rollback.action == "rollback"
    assert rollback.previous_release_id == b.release_id
    assert store.read_current() == a.release_id
    assert dict(store.verify_release(a.release_id).files) == original_a
    assert dict(store.verify_release(b.release_id).files) == original_b
    for name, body in original_a.items():
        assert _sha((store.releases / a.release_id / name).read_bytes()) == _sha(body)
    assert store.list_releases() == sorted([a.release_id, b.release_id])


def test_idempotence_preserves_first_package_bytes_and_timestamp(
    store: ReleaseStore, data_a: dict[str, bytes],
) -> None:
    first = store.create_candidate(data_a, created_at=STAMP)
    original = dict(store.verify_release(first.release_id).files)
    repeated = store.create_candidate(data_a, created_at="2026-10-07T00:00:00Z")
    assert not repeated.created
    assert repeated.release_id == first.release_id
    assert repeated.diff_hash == first.diff_hash
    assert dict(store.verify_release(first.release_id).files) == original
    assert store.verify_release(first.release_id).created_at == STAMP
    assert store.read_current() is None


def test_ack_is_bound_to_current_not_only_candidate(
    store: ReleaseStore, data_a: dict[str, bytes], data_b: dict[str, bytes],
) -> None:
    a = store.create_candidate(data_a, created_at=STAMP)
    store.accept(a.release_id, a.diff_hash, accepted_at=STAMP)
    b = store.create_candidate(data_b, created_at=STAMP)
    alternative = dict(data_a)
    alternative["RUNBOOK.md"] += b"\nTEST-ONLY alternative reviewed instructions.\n"
    c = store.create_candidate(alternative, created_at=STAMP)
    store.accept(c.release_id, c.diff_hash, accepted_at=STAMP)
    before = (store.root / CURRENT_FILE).read_bytes()
    with pytest.raises(StoreError, match="does not match"):
        store.accept(b.release_id, b.diff_hash, accepted_at=STAMP)
    assert (store.root / CURRENT_FILE).read_bytes() == before
    fresh = store.diff(c.release_id, b.release_id)
    assert diff_hash(fresh) != b.diff_hash
    store.accept(b.release_id, diff_hash(fresh), accepted_at=STAMP)
    assert store.read_current() == b.release_id


@pytest.mark.parametrize("ack", ["", "x" * 64, "A" * 64, "0" * 63, "0" * 64])
def test_bad_ack_never_changes_current(store: ReleaseStore, data_a: dict[str, bytes], ack: str) -> None:
    candidate = store.create_candidate(data_a, created_at=STAMP)
    with pytest.raises(StoreError, match="acknowledgment"):
        store.accept(candidate.release_id, ack, accepted_at=STAMP)
    assert store.read_current() is None
    assert not (store.root / ".lock").exists()


def test_already_current_operations_are_refused(store: ReleaseStore, data_a: dict[str, bytes]) -> None:
    candidate = store.create_candidate(data_a, created_at=STAMP)
    store.accept(candidate.release_id, candidate.diff_hash, accepted_at=STAMP)
    before = (store.root / CURRENT_FILE).read_bytes()
    with pytest.raises(StoreError, match="already current"):
        store.accept(candidate.release_id, candidate.diff_hash, accepted_at=STAMP)
    with pytest.raises(StoreError, match="already current"):
        store.rollback(candidate.release_id, at=STAMP)
    assert (store.root / CURRENT_FILE).read_bytes() == before


@pytest.mark.parametrize("removed", [1, 2])
def test_project_warning_threshold_and_building_source_removals(removed: int) -> None:
    old: Json = {
        "projects": {f"TEST-ONLY-{i}": {"title": "old", "geometry": None} for i in range(5)},
        "buildings": {"TEST-ONLY-B": {"project_id": "TEST-ONLY-0"}},
        "sources": {"TEST-ONLY-S": {"query": "1=1"}},
    }
    new = copy.deepcopy(old)
    for i in range(removed):
        new["projects"].pop(f"TEST-ONLY-{i}")
    new["buildings"] = {}
    new["sources"] = {}
    changed = compute_diff("r-" + "1" * 32, old, "r-" + "2" * 32, new)
    assert changed["summary"]["removal_warning"] is (removed == 2)
    assert changed["summary"]["removed_buildings"] == 1
    assert changed["summary"]["has_removals"]
    assert changed["collections"]["sources"]["removed"] == ["TEST-ONLY-S"]
    assert diff_hash(changed) != diff_hash({**changed, "base": None})


def test_field_and_geometry_diff_is_not_record_only() -> None:
    before: Json = {
        "projects": {"TEST-ONLY-P": {"title": "before", "geometry": {"type": "Polygon", "coordinates": []}}},
        "buildings": {"TEST-ONLY-B": {"project_id": "TEST-ONLY-P", "height_m": 10.0}},
        "sources": {},
    }
    after = copy.deepcopy(before)
    after["projects"]["TEST-ONLY-P"]["title"] = "after"
    after["projects"]["TEST-ONLY-P"]["geometry"] = {"type": "MultiPolygon", "coordinates": []}
    after["buildings"]["TEST-ONLY-B"]["height_m"] = 12.5
    changes = compute_diff("r-" + "1" * 32, before, "r-" + "2" * 32, after)
    assert changes["collections"]["projects"]["changed"]["TEST-ONLY-P"]["title"] == {
        "old": "before", "new": "after",
    }
    assert "geometry" in changes["collections"]["projects"]["changed"]["TEST-ONLY-P"]
    assert changes["collections"]["buildings"]["changed"]["TEST-ONLY-B"]["height_m"] == {
        "old": 10.0, "new": 12.5,
    }
    assert changes["summary"]["changed_buildings"] == 1
    assert not changes["summary"]["has_removals"]


def test_buildings_are_typed_semantically_and_joined(result_a: NormalizedResult, tmp_path: Path) -> None:
    pid = result_a.projects[0]["project_id"]
    building: Json = {
        "building_id": "TEST-ONLY-B", "project_id": pid, "source_url": "https://example.org/public",
        "building_name": None, "storeys": 3, "height_m": 12.5, "building_use": "residential",
    }
    result = replace(result_a, buildings=(building,))
    bundle = _bundle(result, tmp_path.resolve() / "with-building")
    data = build_artifacts(bundle)
    view = semantic_view(data)
    assert view["buildings"]["TEST-ONLY-B"]["storeys"] == 3
    assert view["buildings"]["TEST-ONLY-B"]["height_m"] == 12.5
    assert view["buildings"]["TEST-ONLY-B"]["building_name"] == ""
    assert view["buildings"]["TEST-ONLY-B"]["project_id"] == pid
    assert set(view["buildings"]["TEST-ONLY-B"]) == set(BUILDING_COLUMNS)


def test_timestamp_only_refresh_changes_identity_not_semantic_records(
    result_a: NormalizedResult, data_a: dict[str, bytes], tmp_path: Path,
) -> None:
    refreshed = "2026-10-07T00:00:00Z"
    projects = copy.deepcopy(result_a.projects)
    features = copy.deepcopy(result_a.features)
    provenance = copy.deepcopy(result_a.provenance)
    for row in projects:
        row["retrieved_at"] = refreshed
    for feature in features:
        feature["properties"]["retrieved_at"] = refreshed
    provenance["retrieved_at"] = refreshed
    provenance["snapshot_manifest_sha256"] = "f" * 64
    if provenance["capture"] is not None:
        provenance["capture"]["retrieved_at"] = refreshed
        provenance["capture"]["captured_at"] = refreshed
        provenance["capture"]["manifest_sha256"] = "e" * 64
    for request in provenance["requests"]:
        for key in ("retrieved_at", "requested_at"):
            if key in request:
                request[key] = refreshed
    updated = replace(result_a, projects=projects, features=features, provenance=provenance)
    data = build_artifacts(_bundle(updated, tmp_path.resolve() / "refreshed"))
    assert semantic_view(data) == semantic_view(data_a)
    assert compute_release_id(data, None) != compute_release_id(data_a, None)
    change = compute_diff("r-" + "1" * 32, semantic_view(data_a), "r-" + "2" * 32, semantic_view(data))
    assert all(not part["added"] and not part["removed"] and not part["changed"]
               for part in change["collections"].values())


@pytest.mark.parametrize("field", ["response_hash", "query", "source_updated_at"])
def test_real_source_evidence_changes_are_not_stripped(
    result_a: NormalizedResult, data_a: dict[str, bytes], tmp_path: Path, field: str,
) -> None:
    provenance = copy.deepcopy(result_a.provenance)
    if field == "response_hash":
        provenance["requests"][0]["sha256"] = "f" * 64
    elif field == "query":
        provenance["query"]["where"] = "OBJECTID IN (1)"
    else:
        provenance["source_updated_at"] = "2026-10-07T00:00:00Z"
    data = build_artifacts(_bundle(replace(result_a, provenance=provenance), tmp_path.resolve() / "changed"))
    assert semantic_view(data)["sources"] != semantic_view(data_a)["sources"]


@pytest.mark.parametrize(
    "name",
    [*REQUIRED_DATA_ARTIFACTS, "changes.json", "CHANGELOG.md", RELEASE_FILE, CHECKSUM_FILE],
)
def test_every_package_file_is_verified(store: ReleaseStore, data_a: dict[str, bytes], name: str) -> None:
    candidate = store.create_candidate(data_a, created_at=STAMP)
    path = store.releases / candidate.release_id / name
    path.write_bytes(path.read_bytes() + b"TEST-ONLY TAMPER")
    with pytest.raises(StoreError, match="checksum|malformed|newline"):
        store.verify_release(candidate.release_id)
    with pytest.raises(StoreError, match="checksum|malformed|newline"):
        store.accept(candidate.release_id, candidate.diff_hash, accepted_at=STAMP)
    assert not (store.root / CURRENT_FILE).exists()


@pytest.mark.parametrize("fault", ["extra", "missing", "directory"])
def test_package_exact_file_set(store: ReleaseStore, data_a: dict[str, bytes], fault: str) -> None:
    candidate = store.create_candidate(data_a, created_at=STAMP)
    folder = store.releases / candidate.release_id
    if fault == "extra":
        (folder / "extra.json").write_bytes(b"{}")
    elif fault == "missing":
        (folder / "report.html").unlink()
    else:
        (folder / "extra-directory").mkdir()
    with pytest.raises(StoreError, match="file set|unexpected"):
        store.verify_release(candidate.release_id)


@pytest.mark.parametrize("fault", ["report_ok", "original_blocking", "geometry", "csv_attribute", "registry"])
def test_recomputed_checksums_and_identity_cannot_bless_invalid_data(
    data_a: dict[str, bytes], fault: str,
) -> None:
    data = dict(data_a)
    _, valid, _ = assemble_release(data_a, None, None, STAMP)
    if fault == "report_ok":
        report = _json(data["validation.json"])
        report["ok"] = False
        data["validation.json"] = canonical_json(report)
    elif fault == "original_blocking":
        findings = parse_json_strict(data["findings.json"].decode("utf-8"))
        assert isinstance(findings, list)
        findings.append(Finding("blocking", "test_only", "TEST-ONLY blocker").to_dict())
        data["findings.json"] = canonical_json(findings)
        manifest = _json(data[BUNDLE_MANIFEST])
        manifest["counts"]["blocking"] += 1
        manifest["ok"] = True
        data[BUNDLE_MANIFEST] = canonical_json(manifest)
        report = _json(data["validation.json"])
        report["findings"], report["counts"], report["ok"] = findings, manifest["counts"], True
        data["validation.json"] = canonical_json(report)
    elif fault == "geometry":
        geo = _json(data["projects.geojson"])
        geo["features"][0]["geometry"]["coordinates"][0].pop()
        data["projects.geojson"] = canonical_json(geo)
    elif fault == "csv_attribute":
        rows = list(csv.reader(io.StringIO(data["projects.csv"].decode("utf-8"))))
        rows[1][1] = "TEST-ONLY forged title"
        data["projects.csv"] = _csv([list(row) for row in rows])
    else:
        data["field-mapping.json"] = b"[]\n"
    _refresh_bundle_hashes(data)
    with pytest.raises(StoreError, match="validation|bundle"):
        assemble_release(data, None, None, STAMP)
    release_id, forged = _forged_package(data, valid)
    assert set(parse_checksums(forged[CHECKSUM_FILE])) == set(forged) - {CHECKSUM_FILE}
    assert all(_sha(forged[name]) == digest for name, digest in parse_checksums(forged[CHECKSUM_FILE]).items())
    with pytest.raises(StoreError, match="validation|bundle"):
        verify_release_files(forged, release_id)


@pytest.mark.parametrize("fault", ["schema", "counts", "findings_type", "blocking", "findings_changed"])
def test_report_validation_must_agree_with_original_bundle(data_a: dict[str, bytes], fault: str) -> None:
    data = dict(data_a)
    report = _json(data["validation.json"])
    if fault == "schema":
        report["schema"] = "bad"
    elif fault == "counts":
        report["counts"]["projects"] += 1
    elif fault == "findings_type":
        report["findings"] = {}
    elif fault == "blocking":
        report["findings"].append(Finding("blocking", "test_only", "TEST-ONLY").to_dict())
    else:
        report["findings"][0]["message"] += " TEST-ONLY changed"
    data["validation.json"] = canonical_json(report)
    with pytest.raises(StoreError, match="validation.json"):
        semantic_view(data)


@pytest.mark.parametrize("fault", ["header", "missing", "duplicate", "type", "nullable", "length", "description"])
def test_field_definition_registry_is_enforced(data_a: dict[str, bytes], fault: str) -> None:
    data = dict(data_a)
    rows = list(csv.reader(io.StringIO(data["field-definitions.csv"].decode("utf-8"))))
    if fault == "header":
        rows[0][0] = "bad"
    elif fault == "missing":
        rows.pop()
    elif fault == "duplicate":
        rows.append(rows[1].copy())
    else:
        index = {"type": 2, "length": 3, "nullable": 4, "description": 5}[fault]
        rows[1][index] = "TEST-ONLY wrong"
    data["field-definitions.csv"] = _csv([list(row) for row in rows])
    with pytest.raises(StoreError, match="field-definitions.csv"):
        semantic_view(data)


@pytest.mark.parametrize(
    "fault",
    ["schema", "identity", "base", "timestamp", "artifacts", "size", "identity_artifacts"],
)
def test_rehashed_release_metadata_is_checked(data_a: dict[str, bytes], fault: str) -> None:
    release_id, files, _ = assemble_release(data_a, None, None, STAMP)
    release = _json(files[RELEASE_FILE])
    if fault == "schema":
        release["schema"] = "bad"
    elif fault == "identity":
        release["release_id"] = "r-" + "0" * 32
    elif fault == "base":
        release["base_release_id"] = "../escape"
    elif fault == "timestamp":
        release["created_at"] = "2026-99-99T00:00:00Z"
    elif fault == "artifacts":
        release["artifacts"].pop("report.html")
    elif fault == "size":
        release["artifacts"]["report.html"]["bytes"] = True
    else:
        release["identity_artifacts"] = []
    files[RELEASE_FILE] = canonical_json(release)
    files[CHECKSUM_FILE] = build_checksums({name: body for name, body in files.items() if name != CHECKSUM_FILE})
    with pytest.raises(StoreError, match="schema|identity|base|timestamp|artifact|entry"):
        verify_release_files(files, release_id)


@pytest.mark.parametrize("raw", [b"\xff\n", b"not a checksum\n", b"", b"0" * 64 + b"  ../escape\n"])
def test_checksum_manifest_is_strict(raw: bytes) -> None:
    with pytest.raises(StoreError, match="ASCII|malformed|newline"):
        parse_checksums(raw)


def test_duplicate_checksum_lines_and_sorted_round_trip() -> None:
    files = {"z.txt": b"Z", "a.txt": b"A"}
    checksums = build_checksums(files)
    assert checksums.splitlines()[0].endswith(b"  a.txt")
    assert parse_checksums(checksums) == {name: _sha(body) for name, body in files.items()}
    with pytest.raises(StoreError, match="duplicate"):
        parse_checksums(checksums + checksums.splitlines(keepends=True)[0])


@pytest.mark.parametrize("value", ["../escape", "r-" + "A" * 32, "r-" + "0" * 31, ""])
def test_release_identifiers_reject_unsafe_values(value: str) -> None:
    with pytest.raises(StoreError, match="invalid release ID"):
        validate_release_id(value)


@pytest.mark.parametrize("stamp", ["2026-10-06", "2026-10-06T23:30:00+00:00", "2026-02-30T00:00:00Z"])
def test_invalid_timestamp_does_not_create_candidate(
    store: ReleaseStore, data_a: dict[str, bytes], stamp: str,
) -> None:
    with pytest.raises(StoreError, match="timestamp"):
        store.create_candidate(data_a, created_at=stamp)
    assert store.list_releases() == []


def test_data_whitelist_rejects_legacy_extra_and_missing(data_a: dict[str, bytes]) -> None:
    extra = dict(data_a)
    extra["sites.geojson"] = b"{}"
    with pytest.raises(StoreError, match="unknown"):
        compute_release_id(extra, None)
    missing = dict(data_a)
    missing.pop("findings.json")
    with pytest.raises(StoreError, match="missing"):
        compute_release_id(missing, None)
    with pytest.raises(StoreError, match="both"):
        compute_diff("r-" + "1" * 32, None, "r-" + "2" * 32, {})


def test_lock_serializes_and_preserves_another_operation(store: ReleaseStore, data_a: dict[str, bytes]) -> None:
    candidate = store.create_candidate(data_a, created_at=STAMP)
    lock = store.root / ".lock"
    lock.write_bytes(b"TEST-ONLY other operation")
    with pytest.raises(StoreError, match="locked"):
        store.accept(candidate.release_id, candidate.diff_hash)
    with pytest.raises(StoreError, match="locked"):
        store.create_candidate(data_a, created_at=STAMP)
    assert lock.read_bytes() == b"TEST-ONLY other operation"
    assert store.read_current() is None


def test_failed_package_rename_preserves_pointer_and_prior_bytes(
    store: ReleaseStore, data_a: dict[str, bytes], data_b: dict[str, bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = store.create_candidate(data_a, created_at=STAMP)
    store.accept(a.release_id, a.diff_hash, accepted_at=STAMP)
    original = dict(store.verify_release(a.release_id).files)
    pointer = (store.root / CURRENT_FILE).read_bytes()
    rename = Path.rename

    def fail(self: Path, target: str | Path) -> Path:
        if self.parent.name == ".staging":
            raise OSError("TEST-ONLY atomic rename failure")
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", fail)
    with pytest.raises(OSError, match="TEST-ONLY"):
        store.create_candidate(data_b, created_at=STAMP)
    assert store.read_current() == a.release_id
    assert (store.root / CURRENT_FILE).read_bytes() == pointer
    assert dict(store.verify_release(a.release_id).files) == original
    assert store.list_releases() == [a.release_id]
    assert not list((store.root / ".staging").iterdir())
    assert not (store.root / ".lock").exists()


def test_failed_pointer_replace_is_atomic(
    store: ReleaseStore, data_a: dict[str, bytes], data_b: dict[str, bytes], monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = store.create_candidate(data_a, created_at=STAMP)
    store.accept(a.release_id, a.diff_hash, accepted_at=STAMP)
    b = store.create_candidate(data_b, created_at=STAMP)
    pointer = (store.root / CURRENT_FILE).read_bytes()
    original = Path.replace

    def fail(self: Path, target: str | Path) -> Path:
        if self.name.startswith(".current-"):
            raise OSError("TEST-ONLY pointer replacement failure")
        return original(self, target)

    monkeypatch.setattr(Path, "replace", fail)
    with pytest.raises(OSError, match="TEST-ONLY"):
        store.accept(b.release_id, b.diff_hash, accepted_at=STAMP)
    assert (store.root / CURRENT_FILE).read_bytes() == pointer
    assert store.read_current() == a.release_id
    assert not list(store.root.glob(".current-*.tmp"))
    assert not (store.root / ".lock").exists()
    assert store.verify_release(b.release_id).release_id == b.release_id


@pytest.mark.parametrize("component", ["root", "ancestor", "releases", "staging", "release", "file", "pointer", "lock"])
def test_symlinks_at_every_store_component_are_rejected(
    store: ReleaseStore, data_a: dict[str, bytes], tmp_path: Path, component: str,
) -> None:
    a = store.create_candidate(data_a, created_at=STAMP)
    store.accept(a.release_id, a.diff_hash, accepted_at=STAMP)
    root = tmp_path.resolve()
    if component in {"root", "ancestor"}:
        alias = root / "alias"
        alias.symlink_to(store.root, target_is_directory=True)
        target = alias if component == "root" else alias / "nested"
        with pytest.raises(StoreError, match="symlink"):
            ReleaseStore(target)
        return
    if component in {"releases", "staging", "release"}:
        target = {
            "releases": store.releases,
            "staging": store.root / ".staging",
            "release": store.releases / a.release_id,
        }[component]
        moved = root / "moved"
        target.rename(moved)
        target.symlink_to(moved, target_is_directory=True)
    elif component == "file":
        target = store.releases / a.release_id / "projects.csv"
        external = root / "external.csv"
        external.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(external)
    elif component == "pointer":
        target = store.root / CURRENT_FILE
        external = root / "external-pointer"
        external.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(external)
    else:
        external = root / "external-lock"
        external.write_bytes(b"TEST-ONLY")
        (store.root / ".lock").symlink_to(external)
    with pytest.raises(StoreError, match="symlink"):
        if component in {"staging", "lock"}:
            store.create_candidate(data_a, created_at=STAMP)
        elif component == "pointer":
            store.read_current()
        else:
            store.verify_release(a.release_id)


def test_store_root_traversal_is_not_resolved_away(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="traversal"):
        ReleaseStore(tmp_path.resolve() / "unused" / ".." / "store")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema", "bad"), ("release_id", "../escape"), ("previous_release_id", 1),
        ("action", "publish"), ("updated_at", "yesterday"),
    ],
)
def test_pointer_metadata_blocks_read_and_rollback(
    store: ReleaseStore, data_a: dict[str, bytes], data_b: dict[str, bytes], key: str, value: object,
) -> None:
    a = store.create_candidate(data_a, created_at=STAMP)
    store.accept(a.release_id, a.diff_hash, accepted_at=STAMP)
    b = store.create_candidate(data_b, created_at=STAMP)
    store.accept(b.release_id, b.diff_hash, accepted_at=STAMP)
    path = store.root / CURRENT_FILE
    pointer = _json(path.read_bytes())
    pointer[key] = value
    path.write_bytes(canonical_json(pointer))
    damaged = path.read_bytes()
    with pytest.raises(StoreError, match="pointer|release ID|previous|timestamp"):
        store.read_current()
    with pytest.raises(StoreError, match="pointer|release ID|previous|timestamp"):
        store.rollback(a.release_id, at=STAMP)
    assert path.read_bytes() == damaged


def test_rollback_preserves_damaged_current_package_evidence(
    store: ReleaseStore, data_a: dict[str, bytes], data_b: dict[str, bytes],
) -> None:
    a = store.create_candidate(data_a, created_at=STAMP)
    store.accept(a.release_id, a.diff_hash, accepted_at=STAMP)
    original_a = dict(store.verify_release(a.release_id).files)
    b = store.create_candidate(data_b, created_at=STAMP)
    store.accept(b.release_id, b.diff_hash, accepted_at=STAMP)
    damaged = store.releases / b.release_id / "report.html"
    damaged.write_bytes(damaged.read_bytes() + b"TEST-ONLY damaged current evidence")
    evidence = damaged.read_bytes()
    with pytest.raises(StoreError, match="checksum"):
        store.read_current()
    store.rollback(a.release_id, at=STAMP)
    assert store.read_current() == a.release_id
    assert dict(store.verify_release(a.release_id).files) == original_a
    assert damaged.read_bytes() == evidence
