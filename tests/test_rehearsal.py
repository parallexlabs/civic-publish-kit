"""Offline rehearsal contracts; genuine capture replay and labelled test-only mutations [18]."""

from __future__ import annotations

import copy
import csv
import hashlib
import io
import math
import shutil
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
import responses
from pyproj import Transformer
from pyproj.enums import TransformDirection

from civic_publish_kit.acquire import AcquisitionError
from civic_publish_kit.config import canonical_json, parse_json_strict
from civic_publish_kit.rehearsal import (
    BUILDING_COLUMNS,
    BUNDLE_FILES,
    BUNDLE_MANIFEST,
    CRS_CHECK_FILE,
    FIELD_MAPPING,
    PROJECT_COLUMNS,
    NormalizedBundle,
    RehearsalConfig,
    RehearsalSnapshot,
    check_crs_pair,
    extract,
    load_bundle,
    load_rehearsal_config,
    load_rehearsal_snapshot,
    make_transformer,
    normalize,
    normalize_snapshot_data,
    validate_bundle_files,
    validate_layer_url,
    write_bundle,
)

Json = dict[str, Any]
ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "rehearsal.json"
CAPTURES = ROOT / "tests" / "fixtures" / "rehearsal"


def _obj(value: object) -> Json:
    assert isinstance(value, dict)
    return cast(Json, value)


def _json(data: bytes) -> Json:
    return _obj(parse_json_strict(data.decode("utf-8")))


def _read(path: Path) -> Json:
    return _json(path.read_bytes())


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _csv(rows: Sequence[Sequence[object]]) -> bytes:
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _rows(data: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(data.decode("utf-8"), newline="")))


def _positions(geom: Json) -> list[tuple[float, float]]:
    polygons = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    return [(float(p[0]), float(p[1])) for polygon in polygons for ring in polygon for p in ring]


def _rehash(files: dict[str, bytes], manifest: Json) -> None:
    manifest["files"] = {name: _sha(body) for name, body in files.items()}


def _rewrite_snapshot(path: Path, name: str, body: bytes) -> None:
    """Refresh file and request hashes to distinguish semantic checks from checksum checks."""
    (path / name).write_bytes(body)
    log = parse_json_strict((path / "requests.json").read_text(encoding="utf-8"))
    assert isinstance(log, list)
    for raw in log:
        entry = _obj(raw)
        if entry["file"] == name:
            entry["sha256"], entry["bytes"] = _sha(body), len(body)
    (path / "requests.json").write_bytes(canonical_json(log) + b"\n")
    manifest = _read(path / "snapshot-manifest.json")
    manifest["files"][name] = _sha(body)
    manifest["files"]["requests.json"] = _sha((path / "requests.json").read_bytes())
    (path / "snapshot-manifest.json").write_bytes(canonical_json(manifest) + b"\n")


@pytest.fixture(scope="session")
def cfg() -> RehearsalConfig:
    return load_rehearsal_config(CONFIG)


@pytest.fixture
def snapshot_data(cfg: RehearsalConfig, tmp_path: Path) -> RehearsalSnapshot:
    path = tmp_path.resolve() / "snapshot"
    extract(cfg, path, from_fixtures=CAPTURES / "a")
    return load_rehearsal_snapshot(path, cfg)


@pytest.fixture
def bundle(cfg: RehearsalConfig, snapshot_data: RehearsalSnapshot, tmp_path: Path) -> NormalizedBundle:
    result = normalize_snapshot_data(cfg, snapshot_data)
    assert result.ok
    path = tmp_path.resolve() / "bundle"
    write_bundle(path, result)
    return load_bundle(path)


class PagedService:
    """Test-only service responses cloned from genuine polygon/metadata examples."""

    def __init__(self, cfg: RehearsalConfig, count: int = 2003, cap: int = 2000) -> None:
        self.cfg, self.count = cfg, count
        self.meta = _read(CAPTURES / "a" / "metadata.json")
        self.meta["maxRecordCount"] = cap
        self.native = _read(CAPTURES / "a" / "pages" / "page-0001.geojson")["features"][0]
        self.paired = _read(CAPTURES / "a" / CRS_CHECK_FILE)["features"][0]
        self.steps: list[str] = []
        self.pages: list[tuple[int, int, int]] = []
        self.count_reads = 0
        self.id_reads = 0
        self.mode = ""
        self.bodies: list[bytes] = []

    def install(self, mocked: responses.RequestsMock) -> None:
        mocked.add_callback(responses.GET, self.cfg.layer_url, callback=self.answer)
        mocked.add_callback(responses.GET, self.cfg.layer_url + "/query", callback=self.answer)

    def feature(self, oid: int, epsg: int) -> Json:
        feature: Json = copy.deepcopy(self.native if epsg == 26910 else self.paired)
        feature["id"] = oid
        feature["properties"] = {
            "OBJECTID": oid,
            "PROJECT_NO": f"TEST-ONLY-{oid:05d}",
            "DESCRIPTION": "TEST-ONLY pagination response",
            "STATUS": "Final Approval",
        }
        return feature

    def answer(self, request: requests.PreparedRequest) -> tuple[int, dict[str, str], str]:
        assert request.method == "GET"
        assert request.url is not None
        parts = urlsplit(request.url)
        params = parse_qs(parts.query)
        payload: Json
        if not parts.path.endswith("/query"):
            self.steps.append("metadata")
            payload = self.meta
        elif params.get("returnCountOnly") == ["true"]:
            self.count_reads += 1
            self.steps.append("count" if self.count_reads == 1 else "final-count")
            count = self.count + 1 if self.mode == "drift" and self.count_reads == 2 else self.count
            payload = {"count": count}
        elif params.get("returnIdsOnly") == ["true"]:
            self.id_reads += 1
            self.steps.append("ids" if self.id_reads == 1 else "final-ids")
            ids = list(range(1, self.count + 1))
            if self.mode == "initial_ids" and self.id_reads == 1:
                ids.pop()
            if self.mode == "duplicate_ids" and self.id_reads == 1:
                ids[-1] = ids[0]
            if self.mode == "drift" and self.id_reads == 2:
                ids.append(self.count + 1)
            if self.mode == "end_ids" and self.id_reads == 2:
                ids[-1] += 1
            payload = {"objectIdFieldName": "OBJECTID", "objectIds": ids}
        else:
            offset = int(params["resultOffset"][0])
            size = int(params["resultRecordCount"][0])
            epsg = int(params["outSR"][0])
            assert params["where"] == [self.cfg.where]
            assert params["orderByFields"] == ["OBJECTID ASC"]
            assert params["outFields"] == ["OBJECTID,PROJECT_NO,DESCRIPTION,STATUS"]
            assert params["f"] == ["geojson"]
            self.pages.append((offset, size, epsg))
            self.steps.append(f"page:{epsg}:{offset}")
            ids = list(range(offset + 1, min(offset + size, self.count) + 1))
            features = [self.feature(oid, epsg) for oid in ids]
            last = offset + size >= self.count
            if epsg == 26910 and last:
                if self.mode in {"missing", "empty"}:
                    features = features[:-1] if self.mode == "missing" else []
                elif self.mode == "duplicate":
                    features.append(copy.deepcopy(features[0]))
                elif self.mode == "extra":
                    features.append(self.feature(self.count + 10, epsg))
                elif self.mode == "order":
                    features.reverse()
            payload = {
                "type": "FeatureCollection",
                "crs": {"type": "name", "properties": {"name": f"EPSG:{epsg}"}},
                "features": features,
            }
            if epsg == 26910:
                if self.mode == "transfer" and last:
                    payload["exceededTransferLimit"] = True
                if self.mode == "transfer_properties" and last:
                    payload["properties"] = {"exceededTransferLimit": True}
                if self.mode == "nonfinal_transfer" and not last:
                    payload["exceededTransferLimit"] = True
                if self.mode == "crs":
                    payload["crs"]["properties"]["name"] = "EPSG:4326"
        body = canonical_json(payload)
        self.bodies.append(body)
        return 200, {"Content-Type": "application/json"}, body.decode("utf-8")


@pytest.mark.parametrize("capture_name", ["a", "b"])
def test_genuine_replay_bytes_hashes_and_determinism(
    cfg: RehearsalConfig, tmp_path: Path, capture_name: str,
) -> None:
    fixture = CAPTURES / capture_name
    capture = _read(fixture / "capture-manifest.json")
    first, second = tmp_path.resolve() / "first", tmp_path.resolve() / "second"
    one = extract(cfg, first, from_fixtures=fixture)
    two = extract(cfg, second, from_fixtures=fixture)
    assert one == two
    assert one["query"]["scope"] == "service_sample"
    assert one["synthetic"] is False
    assert one["query"]["where"] == capture["where"]
    for name, digest in capture["files"].items():
        assert (first / name).read_bytes() == (second / name).read_bytes() == (fixture / name).read_bytes()
        assert _sha((first / name).read_bytes()) == digest
    snap = load_rehearsal_snapshot(first, cfg)
    assert len(snap.features) == (3 if capture_name == "a" else 2)
    left, right = tmp_path.resolve() / "left", tmp_path.resolve() / "right"
    result = normalize(cfg, first, left)
    repeated = normalize(cfg, second, right)
    assert result.ok
    assert repeated.ok
    for path in left.iterdir():
        assert path.read_bytes() == (right / path.name).read_bytes()
    loaded = load_bundle(left)
    assert loaded.ok
    assert loaded.buildings == ()
    assert "crs" not in loaded.geojson
    assert {f.code for f in loaded.findings} >= {"service_sample", "no_buildings_supplied"}
    assert loaded.provenance["crs"]["always_xy"] is True
    assert loaded.provenance["crs"]["network"] is False


def test_genuine_crs_pair_and_inverse_millimetre(
    cfg: RehearsalConfig, snapshot_data: RehearsalSnapshot,
) -> None:
    assert snapshot_data.crs_check is not None
    paired = snapshot_data.crs_check["features"]
    transformer = make_transformer(cfg)
    assert check_crs_pair(snapshot_data.features, paired, transformer) == []
    by_id = {feature["id"]: feature for feature in paired}
    for feature in snapshot_data.features:
        native = _positions(feature["geometry"])
        server = _positions(by_id[feature["id"]]["geometry"])
        assert len(native) == len(server)
        for (x, y), (expected_lon, expected_lat) in zip(native, server, strict=True):
            lon, lat = transformer.transform(x, y, errcheck=True)
            assert abs(float(lon) - expected_lon) <= 0.000001
            assert abs(float(lat) - expected_lat) <= 0.000001
            back_x, back_y = transformer.transform(lon, lat, direction=TransformDirection.INVERSE, errcheck=True)
            assert math.hypot(float(back_x) - x, float(back_y) - y) <= 0.001


@pytest.mark.parametrize(("cap", "page_size"), [(2000, 2000), (777, 1024)])
def test_2003_records_complete_ordered_pagination(
    cfg: RehearsalConfig, tmp_path: Path, cap: int, page_size: int,
) -> None:
    configured = replace(cfg, page_size=page_size)
    service = PagedService(configured, cap=cap)
    path = tmp_path.resolve() / "live"
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        service.install(mocked)
        manifest = extract(configured, path)
    loaded = load_rehearsal_snapshot(path, configured)
    assert [feature["id"] for feature in loaded.features] == list(range(1, 2004))
    assert {feature["properties"]["PROJECT_NO"] for feature in loaded.features} == {
        f"TEST-ONLY-{oid:05d}" for oid in range(1, 2004)
    }
    size = min(cap, page_size)
    native = [(offset, limit) for offset, limit, epsg in service.pages if epsg == 26910]
    assert native == [(offset, size) for offset in range(0, 2003, size)]
    assert service.steps[:3] == ["metadata", "count", "ids"]
    assert service.steps[-2:] == ["final-count", "final-ids"]
    assert service.count_reads == service.id_reads == 2
    assert manifest["query"]["scope"] == "complete_service"
    assert manifest["counts"]["initial"] == manifest["counts"]["final"] == 2003
    assert manifest["complete_service_count"] == 2003
    assert manifest["counts"]["pages"] == len(native)


@pytest.mark.parametrize(
    "mode",
    [
        "missing", "empty", "duplicate", "extra", "order", "transfer", "transfer_properties",
        "initial_ids", "duplicate_ids", "drift", "end_ids", "crs",
    ],
)
def test_truncation_reconciliation_and_crs_fail_closed(
    cfg: RehearsalConfig, tmp_path: Path, mode: str,
) -> None:
    service = PagedService(cfg, count=2003)
    service.mode = mode
    out = tmp_path.resolve() / "failed"
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        service.install(mocked)
        with pytest.raises(AcquisitionError, match="exact|order|transfer|count|duplicate|changed|CRS"):
            extract(cfg, out)
    assert not out.exists()
    assert not list(out.parent.glob(".failed.tmp-*"))


def test_nonfinal_transfer_flag_does_not_truncate_exact_pages(cfg: RehearsalConfig, tmp_path: Path) -> None:
    service = PagedService(cfg)
    service.mode = "nonfinal_transfer"
    out = tmp_path.resolve() / "live"
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        service.install(mocked)
        extract(cfg, out)
    assert len(load_rehearsal_snapshot(out, cfg).features) == 2003


def test_empty_complete_query_is_explicit(cfg: RehearsalConfig, tmp_path: Path) -> None:
    service = PagedService(cfg, count=0)
    out = tmp_path.resolve() / "empty"
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        service.install(mocked)
        manifest = extract(cfg, out)
    assert service.pages == []
    assert manifest["counts"] == {"initial": 0, "final": 0, "pages": 0}
    result = normalize_snapshot_data(cfg, load_rehearsal_snapshot(out, cfg))
    assert result.ok
    assert result.projects == result.features == ()
    target = tmp_path.resolve() / "bundle"
    write_bundle(target, result)
    assert load_bundle(target).ok


@pytest.mark.parametrize("limit", ["records", "bytes"])
def test_live_source_hard_limits(cfg: RehearsalConfig, tmp_path: Path, limit: str) -> None:
    configured = replace(cfg, max_records=2) if limit == "records" else replace(cfg, max_response_bytes=100)
    service = PagedService(configured, count=3)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        service.install(mocked)
        with pytest.raises(AcquisitionError, match="record|byte"):
            extract(configured, tmp_path.resolve() / "failed")
    assert not (tmp_path / "failed").exists()


def test_fixture_bytes_are_aggregate_not_just_per_file(cfg: RehearsalConfig, tmp_path: Path) -> None:
    capture = _read(CAPTURES / "a" / "capture-manifest.json")
    total = sum((CAPTURES / "a" / name).stat().st_size for name in capture["files"])
    with pytest.raises(AcquisitionError, match="bytes in total"):
        extract(replace(cfg, max_response_bytes=total - 1), tmp_path.resolve() / "bad", CAPTURES / "a")
    extract(replace(cfg, max_response_bytes=total), tmp_path.resolve() / "exact", CAPTURES / "a")


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("geometryType", "esriGeometryPoint"),
        ("advancedQueryCapabilities", {"supportsPagination": False, "supportsOrderBy": True}),
        ("advancedQueryCapabilities", {"supportsPagination": True, "supportsOrderBy": False}),
        ("extent", {"spatialReference": {"wkid": 4326}}),
        ("supportedQueryFormats", "JSON"),
        ("fields", []),
    ],
)
def test_live_metadata_requirements(cfg: RehearsalConfig, tmp_path: Path, key: str, value: object) -> None:
    service = PagedService(cfg, count=3)
    service.meta[key] = value
    if key == "extent":
        service.meta.pop("sourceSpatialReference", None)
    message = {
        "geometryType": "layer geometry is not polygon",
        "advancedQueryCapabilities": "layer does not support ordered pagination",
        "extent": "layer spatial reference is not",
        "supportedQueryFormats": "does not support f=geojson",
        "fields": "object ID field not declared",
    }[key]
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        service.install(mocked)
        with pytest.raises(AcquisitionError, match=message):
            extract(cfg, tmp_path.resolve() / "bad")
    assert service.steps == ["metadata"]


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema",), "bad"),
        (("allowed_hosts",), ["other.example.org"]),
        (("fields",), {"project_id": "OBJECTID", "title": "DESCRIPTION", "status": "STATUS"}),
        (("native_crs",), "EPSG:4326"),
        (("target_crs",), "EPSG:26910"),
        (("page_size",), 2001),
        (("page_size",), 1.5),
        (("page_size",), True),
        (("limits", "max_retries"), 3),
        (("limits", "max_records"), 20001),
        (("limits", "max_response_bytes"), 134217729),
        (("limits", "timeout_seconds"), 0),
        (("limits", "max_retries"), 1.5),
        (("status_map",), []),
        (("status_map",), [{"raw": "Under Review", "status": "approved"}]),
        (("status_map",), [{"raw": "Final Approval", "status": "under_construction"}]),
        (("status_map",), [{"raw": "X", "status": "approved"}, {"raw": "X", "status": "approved"}]),
        (("licence", "url"), "https://other.example.org/licence"),
        (("region_envelope",), [1, 2, 3]),
        (("where",), "1=1; DELETE"),
        (("name",), " padded"),
        (("unknown",), True),
    ],
)
def test_configuration_contract_rejections(tmp_path: Path, path: tuple[str, ...], value: object) -> None:
    document = _read(CONFIG)
    parent = document
    for key in path[:-1]:
        parent = _obj(parent[key])
    parent[path[-1]] = value
    config_path = tmp_path.resolve() / "config.json"
    config_path.write_bytes(canonical_json(document))
    with pytest.raises(AcquisitionError, match=".+"):
        load_rehearsal_config(config_path)


@pytest.mark.parametrize("raw", [b"{", b"\xff", b'{"schema":NaN}', b'{"name":1,"name":2}'])
def test_strict_config_json(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path.resolve() / "bad.json"
    path.write_bytes(raw)
    with pytest.raises(AcquisitionError, match="config"):
        load_rehearsal_config(path)


@pytest.mark.parametrize(
    "change",
    ["http", "token", "credentials", "offhost", "other_layer", "port"],
)
def test_only_fixed_public_endpoint_is_accepted(cfg: RehearsalConfig, change: str) -> None:
    url = cfg.layer_url
    if change == "http":
        url = url.replace("https:", "http:")
    elif change == "token":
        url += "?token=TEST-ONLY"
    elif change == "credentials":
        url = url.replace("https://", "https://user:password@")
    elif change == "offhost":
        url = url.replace("gisservices.surrey.ca", "other.example.org")
    elif change == "other_layer":
        url = url.rsplit("/", 1)[0] + "/155"
    else:
        url = url.replace("surrey.ca/", "surrey.ca:443/")
    with pytest.raises(AcquisitionError, match="official"):
        validate_layer_url(url, cfg.allowed_hosts)


@pytest.mark.parametrize("fault", ["missing", "extra", "tamper", "file_symlink", "parent_symlink"])
def test_snapshot_tree_integrity(cfg: RehearsalConfig, tmp_path: Path, fault: str) -> None:
    root = tmp_path.resolve()
    out = root / "snapshot"
    extract(cfg, out, CAPTURES / "a")
    path = out / "metadata.json"
    if fault == "missing":
        path.unlink()
    elif fault == "extra":
        (out / "extra.json").write_bytes(b"{}")
    elif fault == "tamper":
        path.write_bytes(path.read_bytes() + b" ")
    elif fault == "file_symlink":
        target = root / "copy.json"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
    else:
        alias = root / "alias"
        alias.symlink_to(out, target_is_directory=True)
        out = alias
    with pytest.raises(AcquisitionError, match="file|tampered|checksum|link"):
        load_rehearsal_snapshot(out, cfg)


@pytest.mark.parametrize("fault", ["ids", "page_order", "final_count", "requests", "query", "scope", "counts"])
def test_rehashed_snapshot_forgery_is_reconciled(cfg: RehearsalConfig, tmp_path: Path, fault: str) -> None:
    out = tmp_path.resolve() / "snapshot"
    extract(cfg, out, CAPTURES / "a")
    if fault in {"query", "scope", "counts"}:
        manifest = _read(out / "snapshot-manifest.json")
        if fault == "query":
            manifest["query"]["out_sr"] = 4326
        elif fault == "scope":
            manifest["query"]["scope"] = "complete_service"
        else:
            manifest["counts"]["initial"] += 1
        (out / "snapshot-manifest.json").write_bytes(canonical_json(manifest))
    else:
        name = {
            "ids": "ids.json", "page_order": "pages/page-0001.geojson",
            "final_count": "final-count.json", "requests": "requests.json",
        }[fault]
        body = parse_json_strict((out / name).read_text(encoding="utf-8"))
        if fault == "ids":
            _obj(body)["objectIds"].pop()
        elif fault == "page_order":
            _obj(body)["features"].reverse()
        elif fault == "final_count":
            _obj(body)["count"] += 1
        else:
            assert isinstance(body, list)
            _obj(body[0])["url"] += "&token=TEST-ONLY"
        if fault == "requests":
            raw = canonical_json(body)
            (out / name).write_bytes(raw)
            manifest = _read(out / "snapshot-manifest.json")
            manifest["files"][name] = _sha(raw)
            (out / "snapshot-manifest.json").write_bytes(canonical_json(manifest))
        else:
            _rewrite_snapshot(out, name, canonical_json(body))
    with pytest.raises(AcquisitionError, match=".+"):
        load_rehearsal_snapshot(out, cfg)


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("schema", "bad"), ("synthetic", "false"), ("scope", "complete_service"),
        ("layer_url", "https://other.example.org/167"), ("retrieved_at", "not UTC"),
        ("complete_service_count", -1), ("complete_service_count", True), ("requests", []),
    ],
)
def test_capture_manifest_failures(cfg: RehearsalConfig, tmp_path: Path, key: str, value: object) -> None:
    captured = Path(shutil.copytree(CAPTURES / "a", tmp_path.resolve() / "test-only-copy"))
    path = captured / "capture-manifest.json"
    manifest = _read(path)
    manifest[key] = value
    path.write_bytes(canonical_json(manifest))
    with pytest.raises(AcquisitionError, match=".+"):
        extract(cfg, tmp_path.resolve() / "failed", captured)
    assert not (tmp_path / "failed").exists()


def test_no_overwrite_and_traversal(cfg: RehearsalConfig, tmp_path: Path) -> None:
    root = tmp_path.resolve()
    out = root / "snapshot"
    manifest = extract(cfg, out, CAPTURES / "a")
    before = (out / "snapshot-manifest.json").read_bytes()
    with pytest.raises(AcquisitionError, match="overwritten"):
        extract(cfg, out, CAPTURES / "a")
    assert _read(out / "snapshot-manifest.json") == manifest
    assert (out / "snapshot-manifest.json").read_bytes() == before
    with pytest.raises(AcquisitionError, match="contain"):
        extract(cfg, root / "unused" / ".." / "escape", CAPTURES / "a")
    bundle_path = root / "bundle"
    normalize(cfg, out, bundle_path)
    with pytest.raises(AcquisitionError, match="overwritten"):
        normalize(cfg, out, bundle_path)


@pytest.mark.parametrize("description", [None, "", "   ", "omit"])
def test_null_description_remains_null_not_placeholder(
    cfg: RehearsalConfig, snapshot_data: RehearsalSnapshot, tmp_path: Path, description: object,
) -> None:
    features = copy.deepcopy(snapshot_data.features)
    pid = features[0]["properties"]["PROJECT_NO"].strip()
    if description == "omit":
        features[0]["properties"].pop("DESCRIPTION", None)
    else:
        features[0]["properties"]["DESCRIPTION"] = description
    changed = replace(snapshot_data, features=features)
    result = normalize_snapshot_data(cfg, changed)
    assert result.ok
    row = next(row for row in result.projects if row["project_id"] == pid)
    assert row["title"] is None
    assert any(f.code == "missing_description" and f.record_id == pid for f in result.findings)
    assert next(m for m in FIELD_MAPPING if m.table == "projects" and m.name == "title").nullable
    out = tmp_path.resolve() / "nullable"
    write_bundle(out, result)
    loaded = load_bundle(out)
    assert next(row for row in loaded.projects if row["project_id"] == pid)["title"] == ""
    feature = next(f for f in loaded.geojson["features"] if f["properties"]["project_id"] == pid)
    assert feature["properties"]["title"] is None


@pytest.mark.parametrize("value", [1, True, ["nested"], "x" * 4001])
def test_description_invalid_type_or_length_blocks(
    cfg: RehearsalConfig, snapshot_data: RehearsalSnapshot, value: object,
) -> None:
    features = copy.deepcopy(snapshot_data.features)
    features[0]["properties"]["DESCRIPTION"] = value
    result = normalize_snapshot_data(cfg, replace(snapshot_data, features=features))
    assert not result.ok
    expected_code = (
        "nested_property" if isinstance(value, list)
        else "field_too_long" if isinstance(value, str)
        else "invalid_project"
    )
    assert any(f.code == expected_code and f.severity == "blocking" for f in result.findings)
    assert not any(f.code == "crs_pair_missing" for f in result.findings)


@pytest.mark.parametrize("value", [None, "", " ", 1])
def test_business_id_never_falls_back_to_oid(
    cfg: RehearsalConfig, snapshot_data: RehearsalSnapshot, value: object,
) -> None:
    features = copy.deepcopy(snapshot_data.features)
    oid = features[0]["id"]
    features[0]["properties"]["PROJECT_NO"] = value
    result = normalize_snapshot_data(cfg, replace(snapshot_data, features=features))
    assert not result.ok
    assert any(f.code == "missing_project_id" and f.severity == "blocking" for f in result.findings)
    assert not any(f.code == "crs_pair_missing" for f in result.findings)
    assert all(row["project_id"] != str(oid) for row in result.projects)
    assert len(result.projects) == len(features) - 1


def test_trimmed_duplicate_business_ids_block_both_records(
    cfg: RehearsalConfig, snapshot_data: RehearsalSnapshot,
) -> None:
    features = copy.deepcopy(snapshot_data.features)
    pid = features[0]["properties"]["PROJECT_NO"].strip()
    features[1]["properties"]["PROJECT_NO"] = f" {pid} "
    result = normalize_snapshot_data(cfg, replace(snapshot_data, features=features))
    assert not result.ok
    assert any(f.code == "duplicate_project_id" and f.record_id == pid for f in result.findings)
    assert not any(f.code == "crs_pair_missing" for f in result.findings)
    assert all(row["project_id"] != pid for row in result.projects)


@pytest.mark.parametrize(
    ("raw", "status"),
    [
        ("Final Approval", "approved"), ("Conditional Approval", "conditionally_approved"),
        ("Under Review", "review_unknown"), ("Initial Review", "review_unknown"),
        ("final approval", "unknown"), (" Final Approval ", "unknown"), ("TEST-ONLY NEW", "unknown"),
    ],
)
def test_exact_statuses_and_unrecognized_warning(
    cfg: RehearsalConfig, snapshot_data: RehearsalSnapshot, raw: str, status: str,
) -> None:
    features = copy.deepcopy(snapshot_data.features)
    pid = features[0]["properties"]["PROJECT_NO"].strip()
    features[0]["properties"]["STATUS"] = raw
    result = normalize_snapshot_data(cfg, replace(snapshot_data, features=features))
    assert result.ok
    row = next(row for row in result.projects if row["project_id"] == pid)
    assert row["status_raw"] == raw
    assert row["status"] == status
    warnings = [f for f in result.findings if f.code == "unrecognized_status" and f.record_id == pid]
    assert bool(warnings) is (status == "unknown")


@pytest.mark.parametrize("fault", ["missing", "type", "empty", "short", "open", "bowtie", "nonfinite", "nested"])
def test_invalid_native_geometry_is_never_repaired(
    cfg: RehearsalConfig, snapshot_data: RehearsalSnapshot, fault: str,
) -> None:
    features = copy.deepcopy(snapshot_data.features)
    geom: Json = {"type": "Polygon", "coordinates": [[[500000, 5450000], [500010, 5450000],
                                                     [500010, 5450010], [500000, 5450000]]]}
    if fault == "missing":
        features[0]["geometry"] = None
    elif fault == "type":
        features[0]["geometry"] = {"type": "Point", "coordinates": [500000, 5450000]}
    elif fault == "empty":
        geom["coordinates"] = []
        features[0]["geometry"] = geom
    elif fault == "nested":
        features[0]["properties"]["DESCRIPTION"] = {"test_only": True}
    else:
        ring = geom["coordinates"][0]
        if fault == "short":
            ring.pop()
        elif fault == "open":
            ring[-1] = [500001, 5450000]
        elif fault == "nonfinite":
            ring[1][0] = math.inf
        else:
            geom["coordinates"] = [[[500000, 5450000], [500010, 5450010], [500000, 5450010],
                                    [500010, 5450000], [500000, 5450000]]]
        features[0]["geometry"] = geom
    before = copy.deepcopy(features)
    result = normalize_snapshot_data(cfg, replace(snapshot_data, features=features))
    assert not result.ok
    assert features == before
    expected_code = "nested_property" if fault == "nested" else "invalid_project"
    assert any(f.code == expected_code and f.severity == "blocking" for f in result.findings)
    assert not any(f.code == "crs_pair_missing" for f in result.findings)


def test_wrong_transformer_and_damaged_pair_block(
    cfg: RehearsalConfig, snapshot_data: RehearsalSnapshot,
) -> None:
    wrong = Transformer.from_pipeline("+proj=affine +s11=1 +s22=1")
    result = normalize_snapshot_data(cfg, snapshot_data, transformer=wrong)
    assert not result.ok
    assert any(f.code == "crs_check_failed" for f in result.findings)
    pair = copy.deepcopy(snapshot_data.crs_check)
    assert pair is not None
    pair["features"][0]["geometry"]["coordinates"][0][0][0] += 0.01
    result = normalize_snapshot_data(cfg, replace(snapshot_data, crs_check=pair))
    assert not result.ok
    assert any(f.code == "crs_check_failed" for f in result.findings)


def test_provenance_timestamp_and_sample_notices(cfg: RehearsalConfig, snapshot_data: RehearsalSnapshot) -> None:
    manifest = copy.deepcopy(snapshot_data.manifest)
    manifest["retrieved_at"] = "not UTC"
    manifest["synthetic"] = True
    result = normalize_snapshot_data(cfg, replace(snapshot_data, manifest=manifest, crs_check=None))
    assert not result.ok
    codes = {f.code for f in result.findings}
    assert {"missing_retrieved_at", "synthetic_input", "service_sample", "crs_pair_missing"} <= codes


def test_typed_buildings_one_to_many_and_public_provenance(
    cfg: RehearsalConfig, snapshot_data: RehearsalSnapshot, tmp_path: Path,
) -> None:
    pid = snapshot_data.features[0]["properties"]["PROJECT_NO"].strip()
    rows = [
        list(BUILDING_COLUMNS),
        ["TEST-ONLY-B", pid, "https://example.org/public-buildings", "", "", "", ""],
        ["TEST-ONLY-A", pid, "https://example.org/public-buildings", "TEST-ONLY, named", "4", "12.5", "residential"],
    ]
    raw = _csv(rows)
    result = normalize_snapshot_data(cfg, snapshot_data, raw, "/ignored/private/path/buildings.csv")
    assert result.ok
    assert [row["building_id"] for row in result.buildings] == ["TEST-ONLY-A", "TEST-ONLY-B"]
    assert result.buildings[0]["storeys"] == 4
    assert result.buildings[0]["height_m"] == 12.5
    assert result.buildings[1]["storeys"] is None
    assert result.buildings[1]["height_m"] is None
    assert result.provenance["buildings_input"] == {"name": "buildings.csv", "sha256": _sha(raw), "rows": 2}
    out = tmp_path.resolve() / "with-buildings"
    write_bundle(out, result)
    loaded = load_bundle(out)
    assert loaded.buildings[0]["storeys"] == "4"
    assert loaded.buildings[0]["height_m"] == "12.5"
    assert loaded.buildings[1]["height_m"] == ""


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("building_id", ""), ("project_id", "TEST-ONLY-ORPHAN"),
        ("source_url", "http://example.org"), ("source_url", "https://user:password@example.org"),
        ("storeys", "-1"), ("storeys", "1.5"), ("storeys", "10000"),
        ("height_m", "-1"), ("height_m", "NaN"), ("height_m", "Infinity"),
        ("height_m", "not numeric"), ("building_name", "x" * 256), ("building_use", "x" * 101),
    ],
)
def test_invalid_building_values_block(
    cfg: RehearsalConfig, snapshot_data: RehearsalSnapshot, column: str, value: str,
) -> None:
    pid = snapshot_data.features[0]["properties"]["PROJECT_NO"].strip()
    row = ["TEST-ONLY-A", pid, "https://example.org/public", "", "", "", ""]
    row[BUILDING_COLUMNS.index(column)] = value
    result = normalize_snapshot_data(cfg, snapshot_data, _csv([list(BUILDING_COLUMNS), row]))
    assert not result.ok
    assert result.buildings == ()
    assert any(f.code == "invalid_building" for f in result.findings)


@pytest.mark.parametrize(
    "fault",
    ["duplicate", "duplicate_header", "missing_header", "unknown_header", "short_row", "utf8"],
)
def test_building_csv_structure_blocks(cfg: RehearsalConfig, snapshot_data: RehearsalSnapshot, fault: str) -> None:
    pid = snapshot_data.features[0]["properties"]["PROJECT_NO"].strip()
    rows = [list(BUILDING_COLUMNS), ["TEST-ONLY-A", pid, "https://example.org/public", "", "", "", ""]]
    if fault == "duplicate":
        rows.append(rows[1].copy())
    elif fault == "duplicate_header":
        rows[0][-1] = rows[0][0]
    elif fault == "missing_header":
        rows = []
    elif fault == "unknown_header":
        rows[0][-1] = "PRIVATE_CONTACT"
    elif fault == "short_row":
        rows[1].pop()
    raw = b"\xff" if fault == "utf8" else _csv(rows)
    result = normalize_snapshot_data(cfg, snapshot_data, raw)
    assert not result.ok


@pytest.mark.parametrize(
    "fault",
    [
        "hash", "registry", "source_layer", "source_licence", "source_crs", "source_retrieved",
        "csv_header", "csv_duplicate", "csv_attribute", "geo_crs", "geo_geometry", "geo_point",
        "geo_nested", "geo_status", "geo_integer_coordinate", "finding", "counts", "ok", "extra_file",
    ],
)
def test_rehashed_bundle_forgery_cannot_bypass_validation(bundle: NormalizedBundle, fault: str) -> None:
    files = dict(bundle.files)
    manifest = copy.deepcopy(bundle.manifest)
    if fault == "hash":
        files["projects.csv"] += b" "
    elif fault == "extra_file":
        files["extra.json"] = b"{}"
    elif fault == "registry":
        files["field-mapping.json"] = b"[]\n"
    elif fault.startswith("source_"):
        source = _json(files["source-manifest.json"])
        if fault == "source_layer":
            source["layer_url"] = "https://example.org/167"
        elif fault == "source_licence":
            source["licence"]["url"] = "https://example.org/licence"
        elif fault == "source_crs":
            source["crs"]["always_xy"] = False
        else:
            source["retrieved_at"] = "2026-10-07T00:00:00Z"
        files["source-manifest.json"] = canonical_json(source)
    elif fault.startswith("csv_"):
        rows = _rows(files["projects.csv"])
        if fault == "csv_header":
            rows[0][0] = "OBJECTID"
        elif fault == "csv_duplicate":
            rows[2][0] = rows[1][0]
        else:
            rows[1][PROJECT_COLUMNS.index("title")] = "TEST-ONLY different title"
        files["projects.csv"] = _csv(rows)
    elif fault.startswith("geo_"):
        geo = _json(files["projects.geojson"])
        props = geo["features"][0]["properties"]
        if fault == "geo_crs":
            geo["crs"] = {"type": "name", "properties": {"name": "EPSG:26910"}}
        elif fault == "geo_geometry":
            geo["features"][0]["geometry"]["coordinates"][0].pop()
        elif fault == "geo_point":
            props["longitude"] += 0.1
        elif fault == "geo_nested":
            props["title"] = {"test_only": True}
        elif fault == "geo_status":
            props["status"] = "under_construction"
        else:
            props["longitude"] = int(props["longitude"])
        files["projects.geojson"] = canonical_json(geo)
    elif fault == "finding":
        files["findings.json"] = canonical_json([{"severity": "warning", "code": "broken"}])
    elif fault == "counts":
        manifest["counts"]["projects"] += 1
    else:
        manifest["ok"] = False
    if fault != "hash":
        _rehash(files, manifest)
    with pytest.raises(AcquisitionError, match=".+"):
        validate_bundle_files(files, manifest)


def test_blocked_bundle_loads_for_review_but_ok_cannot_be_forged(
    cfg: RehearsalConfig, snapshot_data: RehearsalSnapshot, tmp_path: Path,
) -> None:
    features = copy.deepcopy(snapshot_data.features)
    features[0]["properties"]["PROJECT_NO"] = None
    result = normalize_snapshot_data(cfg, replace(snapshot_data, features=features))
    assert not result.ok
    assert any(f.code == "missing_project_id" and f.severity == "blocking" for f in result.findings)
    assert not any(f.code == "crs_pair_missing" for f in result.findings)
    out = tmp_path.resolve() / "blocked"
    write_bundle(out, result)
    loaded = load_bundle(out)
    assert not loaded.ok
    manifest = copy.deepcopy(loaded.manifest)
    manifest["ok"] = True
    with pytest.raises(AcquisitionError, match="ok flag"):
        validate_bundle_files(loaded.files, manifest)


@pytest.mark.parametrize("fault", ["missing", "extra", "tamper", "symlink"])
def test_bundle_tree_gate(bundle: NormalizedBundle, tmp_path: Path, fault: str) -> None:
    path = tmp_path.resolve() / "bundle"
    target = path / BUNDLE_FILES[0]
    if fault == "missing":
        target.unlink()
    elif fault == "extra":
        (path / "extra").write_bytes(b"TEST-ONLY")
    elif fault == "tamper":
        target.write_bytes(target.read_bytes() + b" ")
    else:
        external = tmp_path.resolve() / "copy"
        external.write_bytes(target.read_bytes())
        target.unlink()
        target.symlink_to(external)
    assert bundle.ok
    with pytest.raises(AcquisitionError, match="file|checksum|tampered|symlink"):
        load_bundle(path)


def test_pure_and_disk_bundle_validation_agree(bundle: NormalizedBundle) -> None:
    checked = validate_bundle_files(bundle.files, bundle.manifest)
    assert checked.projects == bundle.projects
    assert checked.buildings == bundle.buildings
    assert checked.geojson == bundle.geojson
    assert checked.findings == bundle.findings
    assert set(checked.files) == set(BUNDLE_FILES)
    assert BUNDLE_MANIFEST not in checked.files
