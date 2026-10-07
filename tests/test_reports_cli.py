"""Offline reports and all-six-command acceptance, including the full-release semantic regression [18]."""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import shutil
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from civic_publish_kit.acquire import AcquisitionError
from civic_publish_kit.cli import build_parser, main
from civic_publish_kit.config import canonical_json, parse_json_strict
from civic_publish_kit.normalize import Finding
from civic_publish_kit.rehearsal import (
    BUNDLE_FILES,
    BUNDLE_MANIFEST,
    FIELD_MAPPING,
    NormalizedBundle,
    NormalizedResult,
    extract,
    load_bundle,
    load_rehearsal_config,
    load_rehearsal_snapshot,
    normalize,
    normalize_snapshot_data,
    write_bundle,
)
from civic_publish_kit.reports import (
    FAILURE_ARTIFACTS,
    PACKAGE_ARTIFACTS,
    REPORT_MANIFEST,
    RUNBOOK_MD,
    build_artifacts,
    data_dictionary_md,
    errors_text,
    failure_artifacts,
    field_definitions_csv,
    report_html,
    validate_report,
    validation_json,
)
from civic_publish_kit.store import CURRENT_FILE, REQUIRED_DATA_ARTIFACTS, ReleaseStore, StoreError, diff_hash

Json = dict[str, Any]
ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "rehearsal.json"
CAPTURES = ROOT / "tests" / "fixtures" / "rehearsal"
STAMP = "2026-10-06T23:30:00Z"


def _obj(value: object) -> Json:
    assert isinstance(value, dict)
    return cast(Json, value)


def _json(data: bytes) -> Json:
    return _obj(parse_json_strict(data.decode("utf-8")))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_result(base: Path) -> NormalizedResult:
    cfg = load_rehearsal_config(CONFIG)
    snapshot = base / "snapshot"
    extract(cfg, snapshot, CAPTURES / "a")
    return normalize_snapshot_data(cfg, load_rehearsal_snapshot(snapshot, cfg))


def _call(
    capsys: pytest.CaptureFixture[str],
    args: list[str],
    expected: int = 0,
) -> tuple[Json, str]:
    assert main(args) == expected
    output = capsys.readouterr()
    assert "Traceback" not in output.err
    payload = _obj(parse_json_strict(output.out)) if output.out.strip() else {}
    return payload, output.err


@pytest.fixture
def result(tmp_path: Path) -> NormalizedResult:
    result = _make_result(tmp_path.resolve())
    assert result.ok
    return result


@pytest.fixture
def bundle(result: NormalizedResult, tmp_path: Path) -> NormalizedBundle:
    path = tmp_path.resolve() / "bundle"
    write_bundle(path, result)
    return load_bundle(path)


def test_report_artifact_contract_is_deterministic_and_auditable(bundle: NormalizedBundle, tmp_path: Path) -> None:
    files = build_artifacts(bundle)
    path = tmp_path.resolve() / "bundle"
    assert files == build_artifacts(path)
    assert files == build_artifacts(bundle)
    assert set(files) == set(PACKAGE_ARTIFACTS) == set(REQUIRED_DATA_ARTIFACTS)
    assert len(files) == 13
    for name in BUNDLE_FILES:
        assert files[name] == bundle.files[name]
    assert files[BUNDLE_MANIFEST] == canonical_json(bundle.manifest) + b"\n"
    validation = _json(files["validation.json"])
    assert validation["ok"] is True
    assert validation["counts"] == bundle.manifest["counts"]
    assert validation["findings"] == parse_json_strict(bundle.files["findings.json"].decode("utf-8"))
    assert files["validation.json"] == validation_json(bundle)
    assert b"PASSED" in files["errors.txt"]
    assert b"not building footprints" in files["data_dictionary.md"]
    assert str(tmp_path).encode() not in b"".join(files.values())


def test_field_definitions_and_nullable_title_match_the_live_registry() -> None:
    rows = list(csv.DictReader(io.StringIO(field_definitions_csv().decode("utf-8"))))
    assert len(rows) == len(FIELD_MAPPING)
    for row, field in zip(rows, FIELD_MAPPING, strict=True):
        assert row == {
            "table": field.table,
            "name": field.name,
            "arcgis_type": field.arcgis_type,
            "length": "" if field.length is None else str(field.length),
            "nullable": "true" if field.nullable else "false",
            "description": field.description,
        }
    title = next(row for row in rows if row["table"] == "projects" and row["name"] == "title")
    assert title["nullable"] == "true"


def test_plain_errors_include_all_severities_without_html_or_line_injection() -> None:
    findings = [
        Finding("blocking", "bad", "TEST-ONLY failure\nsecond line", "layer_167", "P-1"),
        Finding("warning", "warn", "TEST-ONLY warning", None, None),
        Finding("exclusion", "exclude", "TEST-ONLY exclusion", "source", "P-2"),
    ]
    text = errors_text(findings, False).decode("utf-8")
    assert "BLOCKED" in text
    assert "Blocking errors: 1" in text
    assert "Warnings: 1" in text
    assert "BLOCKING: TEST-ONLY failure second line" in text
    assert "WARNING: TEST-ONLY warning" in text
    assert "EXCLUDED: TEST-ONLY exclusion" in text
    assert "record P-1" in text
    assert "source layer_167" in text
    assert "<table" not in text


def test_html_escapes_untrusted_source_title_and_finding(
    result: NormalizedResult, tmp_path: Path,
) -> None:
    attack = '<script>alert("TEST-ONLY")</script> & <img src=x onerror=alert(1)>'
    projects = copy.deepcopy(result.projects)
    features = copy.deepcopy(result.features)
    projects[0]["title"] = attack
    features[0]["properties"]["title"] = attack
    provenance = copy.deepcopy(result.provenance)
    provenance["query"]["where"] = "TEST-ONLY " + attack
    findings = (*result.findings, Finding("warning", "test_only", attack, "layer_167", projects[0]["project_id"]))
    modified = replace(result, projects=projects, features=features, provenance=provenance, findings=findings)
    path = tmp_path.resolve() / "escaped"
    write_bundle(path, modified)
    loaded = load_bundle(path)
    html = build_artifacts(loaded)["report.html"].decode("utf-8")
    assert "<script>" not in html
    assert "<img " not in html
    assert "&lt;script&gt;" in html
    assert "&amp;" in html
    assert "&quot;" in html
    assert '<html lang="en">' in html
    assert "<h1>" in html
    assert "<h2>Findings</h2>" in html
    assert '<th scope="col">' in html
    assert 'href="projects.csv"' in html
    assert 'href="buildings.csv"' in html
    assert 'href="errors.txt"' in html
    assert "href=\"https:" not in html
    assert "src=\"https:" not in html
    assert "WARNING" in html
    assert attack in errors_text(loaded.findings, loaded.ok).decode("utf-8")
    assert "always_xy=True" in data_dictionary_md(loaded).decode("utf-8")


def test_package_and_review_download_links_are_relative(bundle: NormalizedBundle) -> None:
    package = build_artifacts(bundle, package=True)["report.html"]
    review = build_artifacts(bundle, package=False)["report.html"]
    assert b'href="changes.json"' in package
    assert b'href="CHANGELOG.md"' in package
    assert b'href="changes.json"' not in review
    assert b'href="CHANGELOG.md"' not in review
    assert b'href="projects.csv"' in review
    assert b"<script" not in package
    assert b"<iframe" not in package
    assert "City GIS" in RUNBOOK_MD
    assert "own ArcGIS Online" in RUNBOOK_MD
    assert "--ack-diff" in RUNBOOK_MD
    assert "local current pointer" in RUNBOOK_MD


def test_report_preview_is_bounded_but_plain_errors_are_complete(
    result: NormalizedResult, tmp_path: Path,
) -> None:
    """Generate a consistent 30-record TEST-ONLY bundle, not a public service capture."""
    projects: list[Json] = []
    features: list[Json] = []
    for index in range(30):
        row = copy.deepcopy(result.projects[0])
        feature = copy.deepcopy(result.features[0])
        row["project_id"] = f"TEST-ONLY-{index:03d}"
        row["title"] = "TEST-ONLY " + "x" * 1000
        feature["properties"] = row
        projects.append(row)
        features.append(feature)
    findings = tuple(Finding("warning", "test_only", f"TEST-ONLY finding {index} " + "x" * 1000)
                     for index in range(65))
    provenance = copy.deepcopy(result.provenance)
    provenance["synthetic"] = True
    provenance["origin"] = "fixture"
    provenance["counts"] = {"initial": 30, "final": 30, "pages": 1}
    provenance["complete_service_count"] = 30
    provenance["query"]["scope"] = "service_sample"
    provenance["query"]["where"] = "OBJECTID BETWEEN 1 AND 30"
    provenance["query"]["page_size"] = 30
    provenance["capture"] = None
    provenance["requests"] = []
    provenance["notes"].append(
        "TEST-ONLY generated 30-record HTML preview. Counts and query model are synthetic, not a City capture."
    )
    provenance["snapshot_manifest_sha256"] = _sha(canonical_json({
        "test_only": True,
        "query": provenance["query"],
        "counts": provenance["counts"],
    }))
    bounded = replace(
        result,
        projects=tuple(projects),
        features=tuple(features),
        provenance=provenance,
        findings=findings,
    )
    path = tmp_path.resolve() / "bounded"
    write_bundle(path, bounded)
    loaded = load_bundle(path)
    assert loaded.ok
    assert loaded.provenance["synthetic"] is True
    assert loaded.provenance["counts"] == {"initial": 30, "final": 30, "pages": 1}
    assert loaded.provenance["query"]["page_size"] == 30
    assert loaded.manifest["counts"]["projects"] == 30
    assert loaded.manifest["counts"]["warnings"] == 65
    html = report_html(loaded, PACKAGE_ARTIFACTS, package=False).decode("utf-8")
    assert "Showing 25 of 30 projects" in html
    assert "Showing 50 of 65 findings" in html
    assert "TEST-ONLY-029" not in html
    assert "TEST-ONLY finding 64" not in html
    assert len(html) < 100_000
    plain = errors_text(loaded.findings, loaded.ok).decode("utf-8")
    assert "TEST-ONLY finding 64" in plain
    assert "x" * 1000 in plain


def test_successful_validate_report_has_exact_hash_manifest(
    bundle: NormalizedBundle, tmp_path: Path,
) -> None:
    out = tmp_path.resolve() / "review"
    report = validate_report(tmp_path.resolve() / "bundle", out)
    assert report.ok
    assert report.errors == ()
    assert report.counts == bundle.manifest["counts"]
    assert report.report_path == out / "report.html"
    assert set(report.files) == set(PACKAGE_ARTIFACTS)
    manifest = _json((out / REPORT_MANIFEST).read_bytes())
    assert manifest["ok"] is True
    assert set(manifest["files"]) == set(report.files)
    for name, digest in manifest["files"].items():
        assert _sha((out / name).read_bytes()) == digest
    before = (out / REPORT_MANIFEST).read_bytes()
    with pytest.raises(AcquisitionError, match="overwritten"):
        validate_report(tmp_path.resolve() / "bundle", out)
    assert (out / REPORT_MANIFEST).read_bytes() == before


def test_invalid_input_produces_small_readable_failure_report(tmp_path: Path) -> None:
    source = tmp_path.resolve() / "missing"
    out = tmp_path.resolve() / "failure"
    report = validate_report(source, out)
    assert not report.ok
    assert report.counts is None
    assert len(report.errors) == 1
    assert set(report.files) == set(FAILURE_ARTIFACTS)
    assert {path.name for path in out.iterdir()} == {*FAILURE_ARTIFACTS, REPORT_MANIFEST}
    validation = _json((out / "validation.json").read_bytes())
    assert validation["ok"] is False
    assert validation["counts"] is None
    assert validation["findings"][0]["severity"] == "blocking"
    plain = (out / "errors.txt").read_text(encoding="utf-8")
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "BLOCKED" in plain
    assert "could not be verified" in plain
    assert str(source) not in plain
    assert 'href="projects.csv"' not in html
    assert 'href="errors.txt"' in html
    assert "No tables" in html or "no tables" in html


def test_failure_artifacts_escape_the_error_without_fabricated_tables() -> None:
    files = failure_artifacts('<script>TEST-ONLY</script> "quoted"')
    assert set(files) == set(FAILURE_ARTIFACTS)
    html = files["report.html"].decode("utf-8")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&quot;" in html
    assert b"<script>TEST-ONLY</script>" in files["errors.txt"]
    assert b"projects.csv" not in files["report.html"]


def test_blocked_bundle_reports_and_package_refusal(result: NormalizedResult, tmp_path: Path) -> None:
    blocked = replace(result, findings=(*result.findings, Finding("blocking", "test_only", "TEST-ONLY blocking error")))
    source = tmp_path.resolve() / "blocked"
    write_bundle(source, blocked)
    loaded = load_bundle(source)
    assert not loaded.ok
    report = validate_report(source, tmp_path.resolve() / "review")
    assert not report.ok
    assert report.errors == ("TEST-ONLY blocking error",)
    assert b"BLOCKED" in (report.out / "report.html").read_bytes()
    store = ReleaseStore(tmp_path.resolve() / "store")
    with pytest.raises(StoreError, match="blocking"):
        store.create_candidate(build_artifacts(loaded), created_at=STAMP)
    assert store.read_current() is None
    assert store.list_releases() == []


def test_report_output_symlink_ancestor_is_rejected(bundle: NormalizedBundle, tmp_path: Path) -> None:
    root = tmp_path.resolve()
    real = root / "real"
    real.mkdir()
    alias = root / "alias"
    alias.symlink_to(real, target_is_directory=True)
    assert bundle.ok
    with pytest.raises(AcquisitionError, match="symbolic link"):
        validate_report(root / "bundle", alias / "review")
    assert not (real / "review").exists()


def test_cli_six_command_fixture_a_b_review_and_exact_rollback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path.resolve()
    store_path = root / "store"
    cfg_args = ["--config", str(CONFIG)]
    snap_a, snap_b = root / "snapshot-a", root / "snapshot-b"
    bundle_a, bundle_b = root / "bundle-a", root / "bundle-b"
    extracted, warning = _call(capsys, ["extract", *cfg_args, "--out", str(snap_a),
                                      "--from-fixtures", str(CAPTURES / "a")])
    assert extracted["counts"]["initial"] == 3
    assert "bounded service sample" in warning
    normalized, _ = _call(capsys, ["normalize", *cfg_args, "--snapshot", str(snap_a), "--out", str(bundle_a)])
    assert normalized["ok"]
    assert normalized["counts"]["projects"] == 3
    validated, _ = _call(capsys, ["validate", "--input", str(bundle_a), "--out", str(root / "review-a")])
    assert validated["ok"]
    a, _ = _call(capsys, ["package", "--input", str(bundle_a), "--store", str(store_path), "--timestamp", STAMP])
    assert a["created"]
    assert not a["promoted"]
    store = ReleaseStore(store_path)
    assert store.read_current() is None
    before = dict(store.verify_release(a["release_id"]).files)
    assert len(before) == 17
    initial, _ = _call(capsys, ["diff", "--before", "empty", "--after", a["release_id"], "--store", str(store_path)])
    assert initial["diff_hash"] == a["diff_hash"]
    promoted, _ = _call(capsys, ["package", "--input", str(bundle_a), "--store", str(store_path),
                               "--timestamp", STAMP, "--ack-diff", initial["diff_hash"]])
    assert promoted["promoted"]
    assert store.read_current() == a["release_id"]
    _call(capsys, ["extract", *cfg_args, "--out", str(snap_b), "--from-fixtures", str(CAPTURES / "b")])
    _call(capsys, ["normalize", *cfg_args, "--snapshot", str(snap_b), "--out", str(bundle_b)])
    _call(capsys, ["validate", "--input", str(bundle_b), "--out", str(root / "review-b")])
    b, warnings = _call(capsys, ["package", "--input", str(bundle_b), "--store", str(store_path), "--timestamp", STAMP])
    assert b["diff"]["summary"]["removed_projects"] == 1
    assert b["review"]["removal_warning"] is True
    assert "review required" in warnings
    assert "20 percent" in warnings
    pointer = (store_path / CURRENT_FILE).read_bytes()
    _, error = _call(capsys, ["package", "--input", str(bundle_b), "--store", str(store_path),
                             "--timestamp", STAMP, "--ack-diff", a["diff_hash"]], expected=2)
    assert "acknowledgment" in error
    assert (store_path / CURRENT_FILE).read_bytes() == pointer
    reviewed, _ = _call(capsys, ["diff", "--before", a["release_id"], "--after", b["release_id"],
                               "--store", str(store_path)])
    assert reviewed["diff_hash"] == diff_hash(reviewed["diff"])
    promoted_b, _ = _call(capsys, ["package", "--input", str(bundle_b), "--store", str(store_path),
                                 "--timestamp", STAMP, "--ack-diff", reviewed["diff_hash"]])
    assert promoted_b["promoted"]
    assert store.read_current() == b["release_id"]
    rollback, _ = _call(capsys, ["rollback", "--release", a["release_id"], "--store", str(store_path)])
    assert rollback["action"] == "rollback"
    assert "No remote service" in rollback["note"]
    assert store.read_current() == a["release_id"]
    after = dict(store.verify_release(a["release_id"]).files)
    assert before == after
    assert {name: _sha(body) for name, body in before.items()} == {name: _sha(body) for name, body in after.items()}


def _duplicate_capture(source: Path, target: Path) -> Path:
    """Labelled test-only mutation keeps OID/request evidence coherent but duplicates PROJECT_NO."""
    captured = Path(shutil.copytree(source, target))
    page = captured / "pages" / "page-0001.geojson"
    collection = _json(page.read_bytes())
    collection["features"][1]["properties"]["PROJECT_NO"] = collection["features"][0]["properties"]["PROJECT_NO"]
    body = canonical_json(collection)
    page.write_bytes(body)
    manifest_path = captured / "capture-manifest.json"
    manifest = _json(manifest_path.read_bytes())
    manifest["synthetic"] = True
    manifest["description"] = "TEST-ONLY duplicate business identity mutation of a copied public capture."
    manifest["files"]["pages/page-0001.geojson"] = _sha(body)
    for entry in manifest["requests"]:
        if entry["file"] == "pages/page-0001.geojson":
            entry["sha256"], entry["bytes"] = _sha(body), len(body)
    manifest_path.write_bytes(canonical_json(manifest))
    return captured


def test_cli_blocking_normalization_writes_review_and_refuses_package_before_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path.resolve()
    fixture = _duplicate_capture(CAPTURES / "a", root / "test-only-duplicate")
    snapshot, bundle_path = root / "snapshot", root / "bundle"
    _call(capsys, ["extract", "--config", str(CONFIG), "--out", str(snapshot), "--from-fixtures", str(fixture)])
    result, error = _call(capsys, ["normalize", "--config", str(CONFIG), "--snapshot", str(snapshot),
                                 "--out", str(bundle_path)], expected=2)
    assert not result["ok"]
    assert result["counts"]["blocking"] >= 1
    assert "cannot be packaged" in error
    assert any(entry["code"] == "duplicate_project_id" for entry in result["findings"])
    report, _ = _call(capsys, ["validate", "--input", str(bundle_path), "--out", str(root / "report")], expected=2)
    assert not report["ok"]
    assert "duplicate" in (root / "report" / "errors.txt").read_text(encoding="utf-8").lower()
    _, error = _call(capsys, ["package", "--input", str(bundle_path), "--store", str(root / "store"),
                             "--timestamp", STAMP], expected=2)
    assert "blocking" in error
    assert not (root / "store").exists()


def test_cli_optional_buildings_file_is_used(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path.resolve()
    cfg = load_rehearsal_config(CONFIG)
    snapshot = root / "snapshot"
    extract(cfg, snapshot, CAPTURES / "a")
    snap = load_rehearsal_snapshot(snapshot, cfg)
    pid = snap.features[0]["properties"]["PROJECT_NO"].strip()
    buildings = root / "test-only-buildings.csv"
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows([
        ["building_id", "project_id", "source_url", "storeys", "height_m"],
        ["TEST-ONLY-B", pid, "https://example.org/public-buildings", "3", "12.5"],
    ])
    buildings.write_text(buffer.getvalue(), encoding="utf-8")
    out = root / "bundle"
    payload, _ = _call(capsys, ["normalize", "--config", str(CONFIG), "--snapshot", str(snapshot),
                              "--out", str(out), "--buildings", str(buildings)])
    assert payload["counts"]["buildings"] == 1
    loaded = load_bundle(out)
    assert loaded.buildings[0]["project_id"] == pid
    assert loaded.buildings[0]["height_m"] == "12.5"


@pytest.mark.parametrize("command", ["fetch", "build", "accept", "publish", "verify-published", "cms"])
def test_no_legacy_remote_or_admin_commands(command: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        main([command])
    assert caught.value.code == 2
    output = capsys.readouterr()
    assert "invalid choice" in output.err
    assert "Traceback" not in output.err


def test_parser_help_exposes_exact_six_commands() -> None:
    text = build_parser().format_help()
    assert "{extract,normalize,validate,diff,package,rollback}" in text
    assert "no remote writes" in text


@pytest.mark.parametrize("command", ["extract", "normalize", "diff", "package", "rollback"])
def test_expected_cli_errors_return_two_without_traceback(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], command: str,
) -> None:
    root = tmp_path.resolve()
    if command == "extract":
        args = ["extract", "--config", str(root / "missing-config"), "--out", str(root / "out")]
    elif command == "normalize":
        args = ["normalize", "--config", str(CONFIG), "--snapshot", str(root / "missing"), "--out", str(root / "out")]
    elif command == "diff":
        args = ["diff", "--before", "empty", "--after", "../escape", "--store", str(root / "store")]
    elif command == "package":
        args = ["package", "--input", str(root / "missing"), "--store", str(root / "store")]
    else:
        args = ["rollback", "--release", "r-" + "0" * 32, "--store", str(root / "store")]
    _, error = _call(capsys, args, expected=2)
    assert "error:" in error


@pytest.mark.parametrize("stamp", ["not UTC", "2026-02-30T00:00:00Z"])
def test_cli_invalid_package_timestamp_is_plain_error(
    bundle: NormalizedBundle, tmp_path: Path, capsys: pytest.CaptureFixture[str], stamp: str,
) -> None:
    assert bundle.ok
    _, error = _call(capsys, ["package", "--input", str(tmp_path.resolve() / "bundle"),
                             "--store", str(tmp_path.resolve() / "store"), "--timestamp", stamp], expected=2)
    assert "timestamp" in error
    assert ReleaseStore(tmp_path.resolve() / "store").read_current() is None


def test_cli_validate_unreadable_input_still_writes_failure_report(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path.resolve()
    payload, error = _call(capsys, ["validate", "--input", str(root / "missing"), "--out", str(root / "review")],
                           expected=2)
    assert not payload["ok"]
    assert payload["counts"] is None
    assert set(payload["files"]) == set(FAILURE_ARTIFACTS)
    assert "errors.txt" in error
    assert (root / "review" / "errors.txt").is_file()


def test_cli_default_config_and_sys_argv(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(ROOT)
    snapshot = tmp_path.resolve() / "snapshot"
    _call(capsys, ["extract", "--out", str(snapshot), "--from-fixtures", str(CAPTURES / "a")])
    bundle_path = tmp_path.resolve() / "bundle"
    cfg = load_rehearsal_config(CONFIG)
    assert normalize(cfg, snapshot, bundle_path).ok
    monkeypatch.setattr(sys, "argv", [
        "civic-publish", "validate", "--input", str(bundle_path), "--out", str(tmp_path.resolve() / "review"),
    ])
    assert main() == 0
    output = capsys.readouterr()
    assert _obj(parse_json_strict(output.out))["ok"] is True
    assert "Traceback" not in output.err
