"""Regressions for review findings M1 (paired CRS response), M2 (strict CSV) and M3 (source-count integrity).

Successful cases use the genuine bounded fixture A snapshot, including its real paired outSR=4326 response.
Every mutation is labelled test-only.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import shutil
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from civic_publish_kit.acquire import AcquisitionError
from civic_publish_kit.config import canonical_json, parse_json_strict
from civic_publish_kit.normalize import Finding
from civic_publish_kit.rehearsal import (
    BUILDING_COLUMNS,
    CRS_CHECK_FILE,
    NormalizedBundle,
    NormalizedResult,
    RehearsalConfig,
    RehearsalSnapshot,
    extract,
    load_bundle,
    load_rehearsal_config,
    load_rehearsal_snapshot,
    normalize_snapshot_data,
    validate_bundle_files,
    write_bundle,
)

Json = dict[str, Any]
ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "rehearsal.json"
CAPTURE_A = ROOT / "tests" / "fixtures" / "rehearsal" / "a"
PUBLIC = "https:" + "//example.org/public-buildings"


def _obj(value: object) -> Json:
    assert isinstance(value, dict)
    return cast(Json, value)


def _csv(rows: Sequence[Sequence[object]]) -> bytes:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _rehash(files: dict[str, bytes], manifest: Json) -> None:
    manifest["files"] = {name: hashlib.sha256(body).hexdigest() for name, body in files.items()}


def _codes(result: NormalizedResult) -> set[tuple[str, str]]:
    return {(f.severity, f.code) for f in result.findings}


@pytest.fixture(scope="module")
def cfg() -> RehearsalConfig:
    return load_rehearsal_config(CONFIG)


@pytest.fixture
def snap(cfg: RehearsalConfig, tmp_path: Path) -> RehearsalSnapshot:
    out = tmp_path.resolve() / "snapshot"
    extract(cfg, out, from_fixtures=CAPTURE_A)
    return load_rehearsal_snapshot(out, cfg)


@pytest.fixture
def bundle(cfg: RehearsalConfig, snap: RehearsalSnapshot, tmp_path: Path) -> NormalizedBundle:
    result = normalize_snapshot_data(cfg, snap)
    assert result.ok
    out = tmp_path.resolve() / "bundle"
    write_bundle(out, result)
    return load_bundle(out)


def test_genuine_complete_pair_reconciles_and_is_ok(cfg: RehearsalConfig, snap: RehearsalSnapshot) -> None:
    assert snap.crs_check is not None
    result = normalize_snapshot_data(cfg, snap)
    assert result.ok
    assert len(result.projects) == len(snap.features) == 3


def test_missing_pair_blocks_non_empty_snapshot(cfg: RehearsalConfig, snap: RehearsalSnapshot) -> None:
    result = normalize_snapshot_data(cfg, replace(snap, crs_check=None))
    assert not result.ok
    assert ("blocking", "crs_pair_missing") in _codes(result)


@pytest.mark.parametrize("fault", ["missing", "duplicate", "extra", "order", "transfer", "properties_transfer"])
def test_pair_must_equal_first_native_page(cfg: RehearsalConfig, snap: RehearsalSnapshot, fault: str) -> None:
    pair = copy.deepcopy(snap.crs_check)
    assert pair is not None
    features = pair["features"]
    if fault == "missing":
        features.pop()
    elif fault == "duplicate":
        features.append(copy.deepcopy(features[0]))
    elif fault == "extra":
        extra = copy.deepcopy(features[0])
        extra["id"] = 999_999
        extra["properties"]["OBJECTID"] = 999_999
        features.append(extra)
    elif fault == "order":
        features.reverse()
    elif fault == "transfer":
        pair["exceededTransferLimit"] = True
    else:
        pair["properties"] = {"exceededTransferLimit": True}
    result = normalize_snapshot_data(cfg, replace(snap, crs_check=pair))
    assert not result.ok
    assert ("blocking", "crs_check_failed") in _codes(result)


def test_complete_first_page_transfer_flag_allowed_only_when_source_exceeds_page(
    cfg: RehearsalConfig, snap: RehearsalSnapshot,
) -> None:
    pair = copy.deepcopy(snap.crs_check)
    assert pair is not None
    pair["features"] = pair["features"][:2]
    pair["exceededTransferLimit"] = True
    paged = replace(snap, crs_check=pair, facts={**snap.facts, "page_size": 2})
    assert normalize_snapshot_data(cfg, paged).ok
    partial = copy.deepcopy(pair)
    partial["features"] = partial["features"][:1]
    assert not normalize_snapshot_data(cfg, replace(paged, crs_check=partial)).ok


def test_fixture_without_pair_cannot_be_extracted(cfg: RehearsalConfig, tmp_path: Path) -> None:
    root = Path(shutil.copytree(CAPTURE_A, tmp_path.resolve() / "test-only-no-pair"))
    (root / CRS_CHECK_FILE).unlink()
    manifest_path = root / "capture-manifest.json"
    capture = _obj(parse_json_strict(manifest_path.read_text(encoding="utf-8")))
    _obj(capture["files"]).pop(CRS_CHECK_FILE)
    capture["requests"] = [r for r in capture["requests"] if _obj(r)["file"] != CRS_CHECK_FILE]
    manifest_path.write_bytes(canonical_json(capture) + b"\n")
    with pytest.raises(AcquisitionError, match="paired"):
        extract(cfg, tmp_path.resolve() / "out", from_fixtures=root)


def test_strict_csv_keeps_quoted_commas_and_newlines(
    cfg: RehearsalConfig, snap: RehearsalSnapshot, tmp_path: Path,
) -> None:
    pid = str(snap.features[0]["properties"]["PROJECT_NO"]).strip()
    name = 'TEST-ONLY, "quoted"\nsecond line'
    raw = _csv([list(BUILDING_COLUMNS), ["TEST-ONLY-A", pid, PUBLIC, name, "3", "9.5", "residential, mixed"]])
    result = normalize_snapshot_data(cfg, snap, raw)
    assert result.ok
    out = tmp_path.resolve() / "with-buildings"
    write_bundle(out, result)
    loaded = load_bundle(out)
    assert loaded.buildings[0]["building_name"] == name
    assert loaded.buildings[0]["building_use"] == "residential, mixed"


@pytest.mark.parametrize("line", [b'"TEST-ONLY"x,P,Q\n', b'"TEST-ONLY,P,Q\n'])
def test_malformed_supplied_csv_blocks(cfg: RehearsalConfig, snap: RehearsalSnapshot, line: bytes) -> None:
    result = normalize_snapshot_data(cfg, snap, b"building_id,project_id,source_url\n" + line)
    assert not result.ok
    assert ("blocking", "unreadable_buildings") in _codes(result)


def test_bundle_reader_rejects_malformed_normalized_csv(bundle: NormalizedBundle) -> None:
    files, manifest = dict(bundle.files), copy.deepcopy(bundle.manifest)
    files["buildings.csv"] += b'"TEST-ONLY"x,P,Q,,,,\n'
    _rehash(files, manifest)
    with pytest.raises(AcquisitionError, match="not readable"):
        validate_bundle_files(files, manifest)


@pytest.mark.parametrize(
    ("section", "change"),
    [
        ("counts", {"initial": True}),
        ("counts", {"final": 4}),
        ("counts", {"pages": 2}),
        ("counts", {"initial": 3.0, "final": 3.0}),
        ("counts", {"initial": 20_001, "final": 20_001, "pages": 11}),
        ("query", {"page_size": 0}),
    ],
)
def test_source_counts_are_whole_equal_feasible_and_bounded(
    bundle: NormalizedBundle, section: str, change: Json,
) -> None:
    files, manifest = dict(bundle.files), copy.deepcopy(bundle.manifest)
    source = _obj(parse_json_strict(files["source-manifest.json"].decode("utf-8")))
    _obj(source[section]).update(change)
    files["source-manifest.json"] = canonical_json(source) + b"\n"
    _rehash(files, manifest)
    with pytest.raises(AcquisitionError, match="source counts"):
        validate_bundle_files(files, manifest)


def test_blocker_free_bundle_cannot_drop_rows_but_blocked_subset_is_reportable(
    cfg: RehearsalConfig, snap: RehearsalSnapshot, tmp_path: Path,
) -> None:
    result = normalize_snapshot_data(cfg, snap)
    dropped = replace(result, projects=result.projects[1:], features=result.features[1:])
    silent = tmp_path.resolve() / "silent-drop"
    write_bundle(silent, dropped)
    with pytest.raises(AcquisitionError, match="source count"):
        load_bundle(silent)
    blocker = Finding("blocking", "test_only_blocker", "TEST-ONLY blocking finding.", "layer_167", "TEST-ONLY")
    blocked = replace(dropped, findings=(*dropped.findings, blocker))
    reported = tmp_path.resolve() / "blocked-subset"
    write_bundle(reported, blocked)
    loaded = load_bundle(reported)
    assert not loaded.ok
    assert len(loaded.projects) == len(snap.features) - 1
