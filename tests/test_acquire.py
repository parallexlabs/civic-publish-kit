"""Offline acquisition contracts and saved request-log evidence.

The checked-in Surrey capture is never edited. Temporary mutations, local inputs
and all HTTP responses constructed here are explicitly test-only.
"""

from __future__ import annotations

import hashlib
import io
import shutil
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
import responses
from urllib3.response import HTTPResponse

from civic_publish_kit.acquire import (
    AcquisitionError,
    UnsupportedGeoJSONError,
    fetch_layer,
    fetch_snapshot,
    load_snapshot,
)
from civic_publish_kit.config import Config, ConfigError, Limits, canonical_json, load_config, parse_json_strict

Data = dict[str, object]
ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "surrey"
FIELDS = ("PROJECT_NO", "DESCRIPTION", "STATUS")
OID = "OBJECTID"


def _obj(value: object) -> Data:
    assert isinstance(value, dict)
    return cast(Data, value)


def _list(value: object) -> list[object]:
    assert isinstance(value, list)
    return cast(list[object], value)


def _read(path: Path) -> Data:
    return _obj(parse_json_strict(path.read_text(encoding="utf-8")))


def _write(path: Path, value: object) -> None:
    path.write_bytes(canonical_json(value))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _features(collection: Data) -> list[Data]:
    return [_obj(feature) for feature in _list(collection["features"])]


def _feature(oid: int) -> Data:
    return {
        "type": "Feature",
        "id": oid,
        "geometry": {"type": "Point", "coordinates": [-122.8, 49.1]},
        "properties": {
            OID: oid,
            "PROJECT_NO": f"TEST-ONLY-{oid:04d}",
            "DESCRIPTION": "TEST-ONLY mocked project",
            "STATUS": "Final Approval",
        },
    }


class Service:
    """Test-only ArcGIS response factory, not a public fixture."""

    def __init__(self, url: str, count: int = 3, cap: int = 2) -> None:
        self.url = url
        self.ids = list(range(1, count + 1))
        self.metadata: Data = {
            "name": "TEST-ONLY mocked layer",
            "objectIdField": OID,
            "maxRecordCount": cap,
            "supportedQueryFormats": "JSON, geoJSON",
            "editingInfo": {"lastEditDate": 0},
            "fields": [
                {"name": OID, "type": "esriFieldTypeOID"},
                *({"name": name, "type": "esriFieldTypeString"} for name in FIELDS),
                {"name": "PRIVATE_CONTACT", "type": "esriFieldTypeString"},
            ],
        }
        self.start_count: object = {"count": count}
        self.end_count: object = {"count": count}
        self.start_ids: object = {"objectIdFieldName": OID, "objectIds": self.ids[::-1]}
        self.end_ids: object = {"objectIdFieldName": OID, "objectIds": self.ids}
        self.count_reads = 0
        self.id_reads = 0
        self.batches: list[tuple[int, ...]] = []
        self.bodies: list[bytes] = []
        self.mode = ""
        self.transfer = ""
        self.unresolved = False

    def install(self, mocked: responses.RequestsMock) -> None:
        mocked.add_callback(responses.GET, self.url, callback=self.answer)
        mocked.add_callback(responses.GET, self.url + "/query", callback=self.answer)

    def answer(self, request: requests.PreparedRequest) -> tuple[int, dict[str, str], str]:
        assert request.method == "GET"
        assert request.url is not None
        parts = urlsplit(request.url)
        query = parse_qs(parts.query)
        payload: object
        if parts.path.endswith("/query"):
            assert query["where"] == ["1=1"]
            if query.get("returnCountOnly") == ["true"]:
                self.count_reads += 1
                payload = self.start_count if self.count_reads == 1 else self.end_count
            elif query.get("returnIdsOnly") == ["true"]:
                self.id_reads += 1
                payload = self.start_ids if self.id_reads == 1 else self.end_ids
            else:
                assert query["f"] == ["geojson"]
                assert query["outSR"] == ["4326"]
                assert query["returnGeometry"] == ["true"]
                assert set(query["outFields"][0].split(",")) == {*FIELDS, OID}
                assert len(query["outFields"][0].split(",")) == len(FIELDS) + 1
                ids = tuple(int(value) for value in query["objectIds"][0].split(","))
                self.batches.append(ids)
                payload = self.batch(ids)
        else:
            assert query == {"f": ["json"]}
            payload = self.metadata
        body = canonical_json(payload)
        self.bodies.append(body)
        return 200, {"Content-Type": "application/json"}, body.decode("utf-8")

    def batch(self, ids: tuple[int, ...]) -> Data:
        features = [_feature(oid) for oid in reversed(ids)]
        if self.mode == "missing":
            features.pop()
        elif self.mode == "duplicate":
            features.append(_feature(ids[0]))
        elif self.mode == "unexpected":
            features.append(_feature(99999))
        elif self.mode == "conflict":
            features[0]["id"] = 99999
        elif self.mode == "no_id":
            features[0].pop("id")
            _obj(features[0]["properties"]).pop(OID)
        elif self.mode == "id_only":
            for feature in features:
                _obj(feature["properties"]).pop(OID)
        elif self.mode == "property_only":
            for feature in features:
                feature.pop("id")
        elif self.mode == "private":
            _obj(features[0]["properties"])["PRIVATE_CONTACT"] = "TEST-ONLY excluded"
        elif self.mode == "feature_type":
            features[0]["type"] = "Point"
        elif self.mode == "properties":
            features[0]["properties"] = ["TEST-ONLY invalid"]
        collection: Data = {"type": "FeatureCollection", "features": features}
        if self.mode == "collection_type":
            collection["type"] = "Feature"
        if self.transfer and (len(ids) > 1 or self.unresolved):
            collection["features"] = features[:1]
            if self.transfer == "top":
                collection["exceededTransferLimit"] = True
            else:
                collection["properties"] = {"exceededTransferLimit": True}
        return collection


def _fetch(
    service: Service,
    limits: Limits | None = None,
    session: requests.Session | None = None,
) -> tuple[Data, Data]:
    collection, provenance = fetch_layer(
        service.url, "1=1", FIELDS, ("services5.arcgis.com",), limits or Limits(), session=session,
    )
    return _obj(collection), _obj(provenance)


def _no_sleep(_delay: float) -> None:
    pass


@pytest.fixture(autouse=True)
def no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Suppress wall-clock backoff only; leave transport and retry decisions intact."""
    monkeypatch.setattr("civic_publish_kit.acquire._sleep", _no_sleep)


@pytest.fixture(scope="session")
def config() -> Config:
    return load_config(ROOT / "config" / "surrey.json")


@pytest.fixture
def captured(tmp_path: Path) -> Path:
    return Path(shutil.copytree(FIXTURES, tmp_path / "test-only-capture-copy"))


@pytest.fixture
def snapshot(config: Config, tmp_path: Path) -> Path:
    out = tmp_path / "snapshot"
    fetch_snapshot(config, out, from_fixtures=FIXTURES)
    return out


def _rehash(snapshot: Path, relative: str, body: bytes) -> None:
    path = snapshot / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    manifest = _read(snapshot / "snapshot-manifest.json")
    _obj(manifest["files"])[relative] = _sha(body)
    _write(snapshot / "snapshot-manifest.json", manifest)


def test_genuine_fixture_replay_and_hash_equivalence(config: Config, tmp_path: Path) -> None:
    out = tmp_path / "snapshot"
    manifest = _obj(fetch_snapshot(config, out, from_fixtures=FIXTURES))
    loaded = _obj(load_snapshot(out, config))
    capture = _read(FIXTURES / "capture-manifest.json")
    assert loaded["manifest"] == manifest == _read(out / "snapshot-manifest.json")
    assert manifest["origin"] == "fixture"
    assert _obj(manifest["capture"])["synthetic"] is False
    assert _obj(manifest["capture"])["manifest_sha256"] == _sha((FIXTURES / "capture-manifest.json").read_bytes())
    assert manifest["licence"] == config.licence.to_dict()
    assert "no snapshot isolation" in str(manifest["snapshot_isolation"])
    collections = _obj(loaded["collections"])
    provenance = _obj(loaded["provenance"])
    assert set(collections) == {source.id for source in config.sources}
    for source in config.sources:
        entry = _obj(_obj(capture["sources"])[source.id])
        prov = _obj(provenance[source.id])
        collection = _obj(collections[source.id])
        features = _features(collection)
        assert len(features) == (5 if source.role == "projects" else 2)
        assert prov["origin"] == "fixture"
        assert prov["source_url"] == source.url
        assert prov["where"] == source.where
        assert prov["capture_where"] == entry["capture_where"]
        assert prov["retrieved_at"] == entry["retrieved_at"]
        assert prov["sample"] == entry["sample"]
        assert prov["complete_source_count"] == entry["complete_source_count"]
        assert prov["requests"] == entry["requests"]
        assert prov["licence_url"] == config.licence.url
        assert prov["attribution"] == config.licence.attribution
        raw_features: list[Data] = []
        for name, digest in _obj(entry["files"]).items():
            relative = f"sources/{source.id}/{name}"
            original = (FIXTURES / relative).read_bytes()
            assert (out / relative).read_bytes() == original
            assert _obj(manifest["files"])[relative] == digest == _sha(original)
            if name.startswith("batches/"):
                raw_features.extend(_features(_read(FIXTURES / relative)))
        oid = str(prov["object_id_field"])
        expected = sorted(raw_features, key=lambda feature: int(str(_obj(feature["properties"])[oid])))
        assert [feature["id"] for feature in features] == [_obj(feature["properties"])[oid] for feature in expected]
        assert [(feature["geometry"], feature["properties"]) for feature in features] == [
            (feature["geometry"], feature["properties"]) for feature in expected
        ]
        assert all(set(_obj(feature["properties"])) <= {*source.out_fields, oid} for feature in features)
        saved_requests = parse_json_strict(
            (out / "sources" / source.id / "requests.json").read_text(encoding="utf-8")
        )
        assert isinstance(saved_requests, list)
        assert saved_requests == prov["requests"]
    projects = _features(_obj(collections["projects"]))
    assert len({_obj(feature["properties"])["PROJECT_NO"] for feature in projects}) == 5


@pytest.mark.parametrize("kind", ["file", "directory", "dangling_symlink"])
def test_snapshot_is_never_overwritten(config: Config, tmp_path: Path, kind: str) -> None:
    out = tmp_path / "snapshot"
    if kind == "file":
        out.write_bytes(b"TEST-ONLY existing")
    elif kind == "directory":
        out.mkdir()
        (out / "sentinel").write_bytes(b"TEST-ONLY existing")
    else:
        out.symlink_to(tmp_path / "missing")
    with pytest.raises(AcquisitionError, match="never overwritten"):
        fetch_snapshot(config, out, from_fixtures=FIXTURES)
    if kind == "file":
        assert out.read_bytes() == b"TEST-ONLY existing"
    elif kind == "directory":
        assert (out / "sentinel").read_bytes() == b"TEST-ONLY existing"
    else:
        assert out.is_symlink()


@pytest.mark.parametrize("fault", ["missing", "extra", "tampered", "file_symlink", "directory_symlink"])
def test_fixture_files_fail_closed(config: Config, captured: Path, tmp_path: Path, fault: str) -> None:
    folder = captured / "sources" / "projects"
    path = folder / "metadata.json"
    if fault == "missing":
        path.unlink()
    elif fault == "extra":
        (folder / "test-only-extra.json").write_bytes(b"{}")
    elif fault == "tampered":
        path.write_bytes(path.read_bytes() + b" ")
    elif fault == "file_symlink":
        target = tmp_path / "test-only-metadata.json"
        target.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(target)
    else:
        target = tmp_path / "test-only-source"
        folder.rename(target)
        folder.symlink_to(target, target_is_directory=True)
    out = tmp_path / "snapshot"
    with pytest.raises(AcquisitionError, match="match|regular file|symlink"):
        fetch_snapshot(config, out, from_fixtures=captured)
    assert not out.exists()
    assert not list(tmp_path.glob(".snapshot.tmp-*"))


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("source_url", "https://other.example.org/layer", "source_url"),
        ("sample", "true", "sample must be boolean"),
        ("sample_count", 4, "sample_count"),
        ("complete_source_count", 4, "sample_count"),
        ("retrieved_at", None, "required"),
        ("capture_where", None, "required"),
        ("requests", {}, "required"),
    ],
)
def test_capture_entry_validation(
    config: Config, captured: Path, tmp_path: Path, key: str, value: object, message: str,
) -> None:
    path = captured / "capture-manifest.json"
    manifest = _read(path)
    _obj(_obj(manifest["sources"])["projects"])[key] = value
    _write(path, manifest)
    out = tmp_path / "snapshot"
    with pytest.raises(AcquisitionError, match=message):
        fetch_snapshot(config, out, from_fixtures=captured)
    assert not out.exists()


@pytest.mark.parametrize("fault", ["schema", "synthetic", "sources", "missing_source", "complete_count"])
def test_capture_manifest_validation(config: Config, captured: Path, tmp_path: Path, fault: str) -> None:
    path = captured / "capture-manifest.json"
    manifest = _read(path)
    if fault == "missing_source":
        _obj(manifest["sources"]).pop("projects")
    elif fault == "complete_count":
        _obj(_obj(manifest["sources"])["fleetwood"])["complete_source_count"] = 3
    else:
        manifest[fault] = None
    _write(path, manifest)
    with pytest.raises(AcquisitionError, match="schema|object|complete_source_count"):
        fetch_snapshot(config, tmp_path / "snapshot", from_fixtures=captured)


@pytest.mark.parametrize("relative", ["../outside.json", "/outside.json", "a//b.json", r"a\b.json"])
def test_fixture_manifest_paths_cannot_escape(
    config: Config, captured: Path, tmp_path: Path, relative: str,
) -> None:
    path = captured / "capture-manifest.json"
    manifest = _read(path)
    listed = _obj(_obj(_obj(manifest["sources"])["projects"])["files"])
    listed[relative] = _sha(b"{}")
    if "\\" in relative:
        (captured / "sources" / "projects" / relative).write_bytes(b"{}")
    _write(path, manifest)
    with pytest.raises(AcquisitionError, match="exactly match|unsafe relative path"):
        fetch_snapshot(config, tmp_path / "snapshot", from_fixtures=captured)


@pytest.mark.parametrize("fault", ["missing", "extra", "tampered", "symlink", "root_symlink", "not_directory"])
def test_snapshot_integrity(config: Config, snapshot: Path, tmp_path: Path, fault: str) -> None:
    path = snapshot / "sources" / "projects" / "metadata.json"
    target = snapshot
    if fault == "missing":
        path.unlink()
    elif fault == "extra":
        (snapshot / "test-only-extra").write_bytes(b"extra")
    elif fault == "tampered":
        path.write_bytes(path.read_bytes() + b" ")
    elif fault == "symlink":
        external = tmp_path / "metadata-copy"
        external.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(external)
    elif fault == "root_symlink":
        target = tmp_path / "snapshot-link"
        target.symlink_to(snapshot, target_is_directory=True)
    else:
        target = tmp_path / "not-a-directory"
        target.write_bytes(b"TEST-ONLY")
    with pytest.raises(AcquisitionError, match="differ|checksum|regular file|real directory"):
        load_snapshot(target, config)


@pytest.mark.parametrize("fault", ["schema", "source_set", "definition", "traversal"])
def test_snapshot_manifest_contract(config: Config, snapshot: Path, fault: str) -> None:
    path = snapshot / "snapshot-manifest.json"
    manifest = _read(path)
    if fault == "schema":
        manifest["schema"] = "TEST-ONLY unsupported"
    elif fault == "source_set":
        _obj(manifest["sources"]).pop("projects")
    elif fault == "definition":
        _obj(_obj(manifest["sources"])["projects"])["where"] = "1=0"
    else:
        _obj(manifest["files"])["../outside.json"] = _sha(b"{}")
    _write(path, manifest)
    with pytest.raises(AcquisitionError, match="schema|sources|definition|files"):
        load_snapshot(snapshot, config)


@pytest.mark.parametrize(
    "fault",
    ["missing_response", "batch_gap", "unknown_response", "outside_source", "transfer", "id"],
)
def test_rehashed_snapshot_still_requires_complete_responses(config: Config, snapshot: Path, fault: str) -> None:
    relative = "sources/projects/batches/batch-0001.geojson"
    manifest_path = snapshot / "snapshot-manifest.json"
    manifest = _read(manifest_path)
    listed = _obj(manifest["files"])
    if fault == "missing_response":
        relative = "sources/projects/count.json"
        (snapshot / relative).unlink()
        listed.pop(relative)
        _write(manifest_path, manifest)
    elif fault == "batch_gap":
        new = "sources/projects/batches/batch-0002.geojson"
        (snapshot / relative).rename(snapshot / new)
        listed[new] = listed.pop(relative)
        _write(manifest_path, manifest)
    elif fault in {"unknown_response", "outside_source"}:
        name = "sources/projects/extra.json" if fault == "unknown_response" else "sources/unused/input.json"
        _rehash(snapshot, name, b"{}")
    else:
        collection = _read(snapshot / relative)
        if fault == "transfer":
            collection["exceededTransferLimit"] = True
        else:
            _list(collection["features"]).pop()
        _rehash(snapshot, relative, canonical_json(collection))
    with pytest.raises(AcquisitionError, match="missing|numbered|unexpected|outside|transfer-limited|IDs not exact"):
        load_snapshot(snapshot, config)


def _local_config(config: Config, base: Path, kind: Literal["geojson", "csv"]) -> Config:
    mapping = replace(config.project_source.require_mapping(), status_evidence_url=None)
    if kind == "csv":
        mapping = replace(mapping, longitude="LONGITUDE", latitude="LATITUDE")
    source = replace(
        config.project_source, kind=kind, url=None, where=None, path=f"input.{kind}",
        mapping=mapping, out_fields=mapping.source_fields(),
    )
    return replace(config, base_dir=base, sources=(source,))


@pytest.mark.parametrize("kind", ["geojson", "csv"])
def test_local_inputs_drop_unknown_raw_fields(config: Config, tmp_path: Path, kind: str) -> None:
    local = _local_config(config, tmp_path, "csv" if kind == "csv" else "geojson")
    path = tmp_path / f"input.{kind}"
    if kind == "geojson":
        feature = _feature(1)
        _obj(feature["properties"])["PRIVATE_CONTACT"] = "TEST-ONLY SECRET"
        _write(path, {"type": "FeatureCollection", "features": [feature]})
    else:
        path.write_text(
            "\ufeffPROJECT_NO,DESCRIPTION,STATUS,LONGITUDE,LATITUDE,PRIVATE_CONTACT\r\n"
            'TEST-ONLY-0001,"TEST-ONLY, quoted",Final Approval,-122.8,49.1,TEST-ONLY SECRET\r\n',
            encoding="utf-8",
        )
    original = path.read_bytes()
    out = tmp_path / "snapshot"
    manifest = _obj(fetch_snapshot(local, out))
    loaded = _obj(load_snapshot(out, local))
    prov = _obj(_obj(loaded["provenance"])["projects"])
    features = _features(_obj(_obj(loaded["collections"])["projects"]))
    assert len(features) == 1
    assert prov["origin"] == "local"
    assert prov["input_sha256"] == _sha(original)
    assert "PRIVATE_CONTACT" in _list(prov["dropped_fields"])
    assert set(_obj(features[0]["properties"])) == set(local.project_source.out_fields)
    stored = (out / "sources" / "projects" / f"input.{kind}").read_bytes()
    assert b"PRIVATE_CONTACT" not in stored
    assert b"TEST-ONLY SECRET" not in stored
    assert path.read_bytes() == original
    assert _obj(manifest["files"])[f"sources/projects/input.{kind}"] == _sha(stored)
    if kind == "csv":
        assert features[0]["geometry"] is None
        assert _obj(features[0]["properties"])["LONGITUDE"] == "-122.8"
        assert not stored.startswith(b"\xef\xbb\xbf")
        assert b"\r\n" not in stored
    else:
        assert features[0]["geometry"] == _feature(1)["geometry"]
        assert OID not in _obj(features[0]["properties"])


@pytest.mark.parametrize(
    "body",
    [
        b"",
        b"PROJECT_NO,PROJECT_NO\nA,A\n",
        b"PROJECT_NO,DESCRIPTION,STATUS,LONGITUDE\nA,B,Final Approval,1\n",
        b"PROJECT_NO,DESCRIPTION,STATUS,LONGITUDE,LATITUDE\nA,B,Final Approval,1\n",
        b"PROJECT_NO,DESCRIPTION,STATUS,LONGITUDE,LATITUDE\nA,B,Final Approval,1,2,3\n",
        b"\xff",
    ],
)
def test_invalid_csv_cannot_create_snapshot(config: Config, tmp_path: Path, body: bytes) -> None:
    local = _local_config(config, tmp_path, "csv")
    (tmp_path / "input.csv").write_bytes(body)
    out = tmp_path / "snapshot"
    with pytest.raises(AcquisitionError, match="CSV|columns|records"):
        fetch_snapshot(local, out)
    assert not out.exists()


@pytest.mark.parametrize(
    "body",
    [
        b"{",
        b"\xff",
        b'{"type":"FeatureCollection","features":[],"features":[]}',
        b'{"type":"FeatureCollection","features":NaN}',
        b"[]",
        b'{"type":"Feature","features":[]}',
        b'{"type":"FeatureCollection","features":[null]}',
        b'{"type":"FeatureCollection","features":[{"type":"Point"}]}',
        b'{"type":"FeatureCollection","features":[{"type":"Feature","properties":["bad"]}]}',
    ],
)
def test_invalid_local_geojson(config: Config, tmp_path: Path, body: bytes) -> None:
    local = _local_config(config, tmp_path, "geojson")
    (tmp_path / "input.geojson").write_bytes(body)
    with pytest.raises(AcquisitionError, match="JSON|Feature|object"):
        fetch_snapshot(local, tmp_path / "snapshot")


@pytest.mark.parametrize("kind", ["geojson", "csv"])
@pytest.mark.parametrize("limit", ["records", "bytes"])
def test_local_hard_limits(config: Config, tmp_path: Path, kind: str, limit: str) -> None:
    local = _local_config(config, tmp_path, "csv" if kind == "csv" else "geojson")
    if kind == "geojson":
        body = canonical_json({"type": "FeatureCollection", "features": [_feature(1), _feature(2)]})
    else:
        body = (
            b"PROJECT_NO,DESCRIPTION,STATUS,LONGITUDE,LATITUDE\n"
            b"TEST-ONLY-A,A,Final Approval,1,2\nTEST-ONLY-B,B,Final Approval,1,2\n"
        )
    (tmp_path / f"input.{kind}").write_bytes(body)
    limits = replace(local.limits, max_records=1) if limit == "records" else replace(
        local.limits, max_response_bytes=len(body) - 1,
    )
    with pytest.raises(AcquisitionError, match="limit|exceeds"):
        fetch_snapshot(replace(local, limits=limits), tmp_path / "snapshot")


@pytest.mark.parametrize("fault", ["direct_symlink", "parent_escape", "traversal", "missing"])
def test_local_path_constraints(config: Config, tmp_path: Path, fault: str) -> None:
    base = tmp_path / "config"
    base.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    (external / "input.geojson").write_bytes(b'{"type":"FeatureCollection","features":[]}')
    local = _local_config(config, base, "geojson")
    if fault == "direct_symlink":
        (base / "input.geojson").symlink_to(external / "input.geojson")
    elif fault == "parent_escape":
        (base / "inputs").symlink_to(external, target_is_directory=True)
        local = replace(local, sources=(replace(local.project_source, path="inputs/input.geojson"),))
    elif fault == "traversal":
        local = replace(local, sources=(replace(local.project_source, path="../external/input.geojson"),))
    with pytest.raises((AcquisitionError, ConfigError), match="symlink|outside|regular file"):
        fetch_snapshot(local, tmp_path / "snapshot")


@pytest.mark.parametrize("kind", ["geojson", "csv"])
def test_rehashed_local_input_must_remain_canonical(config: Config, tmp_path: Path, kind: str) -> None:
    local = _local_config(config, tmp_path, "csv" if kind == "csv" else "geojson")
    body = (
        b"PROJECT_NO,DESCRIPTION,STATUS,LONGITUDE,LATITUDE\nTEST-ONLY-A,A,Final Approval,1,2\n"
        if kind == "csv" else canonical_json({"type": "FeatureCollection", "features": [_feature(1)]})
    )
    (tmp_path / f"input.{kind}").write_bytes(body)
    out = tmp_path / "snapshot"
    fetch_snapshot(local, out)
    relative = f"sources/projects/input.{kind}"
    stored = (out / relative).read_bytes()
    changed = stored.replace(b"\n", b"\r\n") if kind == "csv" else stored + b" "
    _rehash(out, relative, changed)
    with pytest.raises(AcquisitionError, match="filtered canonical form"):
        load_snapshot(out, local)


@pytest.mark.parametrize(("cap", "configured"), [(137, 500), (2000, 500), (137, 100)])
def test_complete_1203_id_read(config: Config, cap: int, configured: int) -> None:
    service = Service(config.project_source.require_url(), count=1203, cap=cap)
    limits = replace(Limits(), max_batch_size=configured)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        service.install(mocked)
        collection, provenance = _fetch(service, limits)
    features = _features(collection)
    assert [feature["id"] for feature in features] == list(range(1, 1204))
    assert {_obj(feature["properties"])["PROJECT_NO"] for feature in features} == {
        f"TEST-ONLY-{oid:04d}" for oid in range(1, 1204)
    }
    size = min(cap, configured)
    assert all(0 < len(batch) <= size for batch in service.batches)
    assert sum(map(len, service.batches)) == 1203
    assert len(service.batches) == (1203 + size - 1) // size
    assert service.count_reads == service.id_reads == 2
    assert provenance["count"] == 1203
    assert provenance["batch_size"] == size
    assert provenance["source_updated_at"] == "1970-01-01T00:00:00Z"
    assert provenance["origin"] == "live"
    assert "no snapshot isolation" in str(provenance["snapshot_isolation"])
    assert "never a business identity" in str(provenance["object_id_use"])
    definitions = [_obj(value) for value in _list(provenance["field_definitions"])]
    assert {definition["name"] for definition in definitions} == {*FIELDS, OID}
    logs = [_obj(value) for value in _list(provenance["requests"])]
    assert len(logs) == len(service.bodies)
    for log, body in zip(logs, service.bodies, strict=True):
        assert log["sha256"] == _sha(body)
        assert log["bytes"] == len(body)
        assert log["status"] == 200
        assert str(log["requested_at"]).endswith("Z")


@pytest.mark.parametrize("location", ["top", "properties"])
def test_transfer_limit_subdivides_without_losing_records(config: Config, location: str) -> None:
    service = Service(config.project_source.require_url(), count=7, cap=5)
    service.transfer = location
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        service.install(mocked)
        collection, provenance = _fetch(service)
    assert [feature["id"] for feature in _features(collection)] == list(range(1, 8))
    assert any(len(batch) > 1 for batch in service.batches)
    assert {batch[0] for batch in service.batches if len(batch) == 1} == set(range(1, 8))
    logs = [_obj(value) for value in _list(provenance["requests"])]
    assert any(log.get("accepted") is False for log in logs)
    assert sum(log.get("accepted") is True for log in logs) == 7


@pytest.mark.parametrize("location", ["top", "properties"])
def test_single_id_transfer_limit_is_fatal(config: Config, location: str) -> None:
    service = Service(config.project_source.require_url(), count=1)
    service.transfer, service.unresolved = location, True
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        service.install(mocked)
        with pytest.raises(AcquisitionError, match="single object ID"):
            _fetch(service)
    assert service.batches == [(1,)]


@pytest.mark.parametrize(
    ("mode", "message"),
    [
        ("missing", "missing="),
        ("duplicate", "duplicate="),
        ("unexpected", "unexpected="),
        ("conflict", "conflicts"),
        ("no_id", "object ID"),
        ("private", "non-allowlisted"),
        ("feature_type", "not a Feature"),
        ("properties", "JSON object"),
        ("collection_type", "FeatureCollection"),
    ],
)
def test_invalid_batches_never_become_complete_reads(config: Config, mode: str, message: str) -> None:
    service = Service(config.project_source.require_url())
    service.mode = mode
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        service.install(mocked)
        with pytest.raises(AcquisitionError, match=message):
            _fetch(service)
    assert service.count_reads == 1


@pytest.mark.parametrize("mode", ["id_only", "property_only"])
def test_oid_can_be_reported_as_feature_id_or_property(config: Config, mode: str) -> None:
    service = Service(config.project_source.require_url())
    service.mode = mode
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        service.install(mocked)
        collection, _ = _fetch(service)
    assert [feature["id"] for feature in _features(collection)] == service.ids
    assert [_obj(feature["properties"])[OID] for feature in _features(collection)] == service.ids


@pytest.mark.parametrize(
    ("stage", "payload", "message"),
    [
        ("start_count", {"count": True}, "integer"),
        ("start_count", {"count": -1}, "integer"),
        ("start_count", {}, "integer"),
        ("start_ids", {}, "objectIds missing"),
        ("start_ids", {"objectIds": [1, 2]}, "count-complete"),
        ("start_ids", {"objectIds": [1, 1, 2]}, "duplicate object IDs"),
        ("start_ids", {"objectIds": [1, 2, "3"]}, "integer"),
        ("start_ids", {"objectIds": [1, 2, True]}, "integer"),
        ("start_ids", {"objectIdFieldName": "WRONG", "objectIds": [1, 2, 3]}, "does not match"),
        ("end_count", {"count": True}, "integer"),
        ("end_ids", {"objectIds": [1, 2]}, "count-complete"),
        ("end_ids", {"objectIds": [1, 1, 2]}, "duplicate object IDs"),
        ("end_ids", {"objectIds": [1, 2, 4]}, "source changed"),
    ],
)
def test_beginning_and_end_completeness(
    config: Config, stage: str, payload: object, message: str,
) -> None:
    service = Service(config.project_source.require_url())
    setattr(service, stage, payload)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        service.install(mocked)
        with pytest.raises(AcquisitionError, match=message):
            _fetch(service)


def test_late_source_count_change(config: Config) -> None:
    service = Service(config.project_source.require_url())
    service.end_count = {"count": 4}
    service.end_ids = {"objectIds": [1, 2, 3, 4]}
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        service.install(mocked)
        with pytest.raises(AcquisitionError, match="source changed during read"):
            _fetch(service)
    assert service.count_reads == service.id_reads == 2


@pytest.mark.parametrize("null_ids", [False, True])
def test_verified_empty_layer(config: Config, null_ids: bool) -> None:
    service = Service(config.project_source.require_url(), count=0)
    if null_ids:
        service.start_ids = service.end_ids = {"objectIds": None}
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        service.install(mocked)
        collection, provenance = _fetch(service)
    assert collection == {"type": "FeatureCollection", "features": []}
    assert provenance["count"] == 0
    assert service.batches == []
    assert service.count_reads == service.id_reads == 2


@pytest.mark.parametrize(
    ("key", "value", "message"),
    [
        ("fields", None, "field definitions"),
        ("fields", [None], "field definitions"),
        ("fields", [{"name": 1}], "field definitions"),
        ("fields", [], "allowlisted fields absent"),
        ("objectIdField", "MISSING", "allowlisted fields absent"),
        ("maxRecordCount", 0, "integer"),
        ("maxRecordCount", True, "integer"),
        ("maxRecordCount", "500", "integer"),
        ("maxRecordCount", None, "integer"),
    ],
)
def test_metadata_requirements(config: Config, key: str, value: object, message: str) -> None:
    service = Service(config.project_source.require_url())
    service.metadata[key] = value
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        service.install(mocked)
        with pytest.raises(AcquisitionError, match=message):
            _fetch(service)
    assert service.count_reads == 0


@pytest.mark.parametrize("ambiguous", [False, True])
def test_metadata_oid_type_fallback(config: Config, ambiguous: bool) -> None:
    service = Service(config.project_source.require_url())
    service.metadata.pop("objectIdField")
    if ambiguous:
        _list(service.metadata["fields"]).append({"name": "SECOND_OID", "type": "esriFieldTypeOID"})
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        service.install(mocked)
        if ambiguous:
            with pytest.raises(AcquisitionError, match="object ID field not declared"):
                _fetch(service)
        else:
            _, provenance = _fetch(service)
            assert provenance["object_id_field"] == OID


@pytest.mark.parametrize("formats", ["JSON", "", None])
def test_unsupported_geojson_has_export_instructions(config: Config, formats: object) -> None:
    service = Service(config.project_source.require_url())
    service.metadata["supportedQueryFormats"] = formats
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        service.install(mocked)
        with pytest.raises(UnsupportedGeoJSONError, match="Export it as GeoJSON") as caught:
            _fetch(service)
    assert "explicit mapping" in str(caught.value)
    assert service.count_reads == 0


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
@pytest.mark.parametrize("recover", [False, True])
def test_http_transient_retry_budget(config: Config, status: int, recover: bool) -> None:
    service = Service(config.project_source.require_url())
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        for _ in range(2 if recover else 3):
            mocked.add(responses.GET, service.url, status=status)
        service.install(mocked)
        if recover:
            collection, _ = _fetch(service)
            assert len(_features(collection)) == 3
            assert sum(call.request.url is not None and "/query?" not in call.request.url for call in mocked.calls) == 3
        else:
            with pytest.raises(AcquisitionError, match="after 3 attempt"):
                _fetch(service)
            assert len(mocked.calls) == 3


@pytest.mark.parametrize("error", [requests.ConnectionError("TEST-ONLY"), requests.Timeout("TEST-ONLY")])
def test_transport_recovers_after_two_failures(config: Config, error: Exception) -> None:
    service = Service(config.project_source.require_url())
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, service.url, body=error)
        mocked.add(responses.GET, service.url, body=error)
        service.install(mocked)
        collection, _ = _fetch(service)
        assert len(_features(collection)) == 3
        assert len(mocked.calls) == len(service.bodies) + 2


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_permanent_http_errors_are_not_retried(config: Config, status: int) -> None:
    service = Service(config.project_source.require_url())
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, service.url, status=status)
        with pytest.raises(AcquisitionError, match=f"HTTP {status}"):
            _fetch(service)
        assert len(mocked.calls) == 1


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_are_refused_without_following(config: Config, status: int) -> None:
    service = Service(config.project_source.require_url())
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, service.url, status=status, headers={"Location": "https://other.example.org/secret"})
        mocked.add(responses.GET, "https://other.example.org/secret", json={})
        with pytest.raises(AcquisitionError, match="redirect"):
            _fetch(service)
        assert len(mocked.calls) == 1


@pytest.mark.parametrize("body", [b"{", b"\xff", b'{"error":{"code":400}}', b'{"a":1,"a":2}', b'{"x":NaN}'])
def test_bad_200_response_is_fatal_not_empty_or_retried(config: Config, body: bytes) -> None:
    service = Service(config.project_source.require_url())
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, service.url, body=body, status=200)
        with pytest.raises(AcquisitionError, match="invalid JSON|ArcGIS error"):
            _fetch(service)
        assert len(mocked.calls) == 1


@pytest.mark.parametrize("fault", ["http", "offhost", "token", "credentials", "traversal", "fields", "where"])
def test_unsafe_requests_are_rejected_before_transport(config: Config, fault: str) -> None:
    url = config.project_source.require_url()
    fields: tuple[str, ...] = FIELDS
    where = "1=1"
    if fault == "http":
        url = url.replace("https:", "http:")
    elif fault == "offhost":
        url = url.replace("services5.arcgis.com", "other.example.org")
    elif fault == "token":
        url += "?token=TEST-ONLY"
    elif fault == "credentials":
        url = url.replace("https://", "https://user:password@")
    elif fault == "traversal":
        url = url.replace("Development%20Applications", "%2e%2e")
    elif fault == "fields":
        fields = ("*",)
    else:
        where = "1=1; DELETE"
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        with pytest.raises(AcquisitionError, match="https|allowlisted|query|credentials|traversal|field|where"):
            fetch_layer(url, where, fields, config.allowed_hosts, Limits())
        assert len(mocked.calls) == 0


def test_timeout_redirect_and_supplied_session_contract(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    service = Service(config.project_source.require_url())
    options: list[dict[str, object]] = []
    closed: list[bool] = []
    with requests.Session() as session:
        original_send = session.send
        original_close = session.close

        def send(request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
            options.append(kwargs)
            return original_send(request, **kwargs)

        def close() -> None:
            closed.append(True)
            original_close()

        monkeypatch.setattr(session, "send", send)
        monkeypatch.setattr(session, "close", close)
        with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
            service.install(mocked)
            _fetch(service, session=session)
        assert closed == []
        assert options
        assert all(option["timeout"] == (20.0, 20.0) for option in options)
        assert all(option["allow_redirects"] is False and option["stream"] is True for option in options)
    assert closed == [True]


def test_interrupted_stream_is_retried(config: Config, monkeypatch: pytest.MonkeyPatch) -> None:
    service = Service(config.project_source.require_url())
    with requests.Session() as session:
        original_send = session.send
        attempted: list[bool] = []
        sent: list[requests.PreparedRequest] = []
        interrupted: list[HTTPResponse] = []

        def chunks(_chunk_size: int = 1, _decode_unicode: bool = False) -> Iterator[bytes]:
            yield b"{"
            raise requests.exceptions.ChunkedEncodingError("TEST-ONLY interrupted stream")

        def send(request: requests.PreparedRequest, **kwargs: Any) -> requests.Response:
            sent.append(request)
            if not attempted:
                attempted.append(True)
                raw = HTTPResponse(body=io.BytesIO(b"{"), preload_content=False)
                interrupted.append(raw)
                response = requests.Response()
                response.status_code = 200
                response.raw = raw
                monkeypatch.setattr(response, "iter_content", chunks)
                return response
            return original_send(request, **kwargs)

        monkeypatch.setattr(session, "send", send)
        with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
            service.install(mocked)
            collection, _ = _fetch(service, session=session)
            assert len(mocked.calls) == len(service.bodies)
    assert attempted == [True]
    assert [raw.closed for raw in interrupted] == [True]
    assert len(sent) == len(service.bodies) + 1
    assert sent[0].url == sent[1].url
    assert len(_features(collection)) == 3
    assert [feature["id"] for feature in _features(collection)] == service.ids


def test_record_limit_is_checked_before_batches(config: Config) -> None:
    service = Service(config.project_source.require_url(), count=4)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        service.install(mocked)
        with pytest.raises(AcquisitionError, match="record limit"):
            _fetch(service, replace(Limits(), max_records=3))
    assert service.id_reads == 0
    assert service.batches == []


@pytest.mark.parametrize("exact", [False, True])
def test_byte_budget_accumulates_across_batches(config: Config, exact: bool) -> None:
    url = config.project_source.require_url()
    baseline = Service(url, count=5, cap=2)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        baseline.install(mocked)
        _fetch(baseline)
    budget = sum(map(len, baseline.bodies)) if exact else sum(map(len, baseline.bodies[:5])) - 1
    service = Service(url, count=5, cap=2)
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        service.install(mocked)
        if exact:
            collection, _ = _fetch(service, replace(Limits(), max_response_bytes=budget))
            assert len(_features(collection)) == 5
        else:
            with pytest.raises(AcquisitionError, match="byte|bytes"):
                _fetch(service, replace(Limits(), max_response_bytes=budget))
            assert len(service.batches) == 2


def test_declared_content_length_cannot_exceed_budget(config: Config) -> None:
    service = Service(config.project_source.require_url())
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        mocked.add(responses.GET, service.url, body=b"{}", headers={"Content-Length": "1000000"})
        with pytest.raises(AcquisitionError, match="byte limit"):
            _fetch(service, replace(Limits(), max_response_bytes=100))
        assert len(mocked.calls) == 1


def test_live_snapshot_round_trip(config: Config, tmp_path: Path) -> None:
    source = replace(config.project_source, where="1=1", out_fields=FIELDS)
    live = replace(config, sources=(source,))
    service = Service(source.require_url())
    out = tmp_path / "snapshot"
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        service.install(mocked)
        manifest = _obj(fetch_snapshot(live, out))
    loaded = _obj(load_snapshot(out, live))
    assert loaded["manifest"] == manifest
    assert manifest["origin"] == "live"
    provenance = _obj(_obj(loaded["provenance"])["projects"])
    assert provenance["sample"] is False
    assert provenance["complete_source_count"] == 3
    assert provenance["licence_url"] == live.licence.url
    assert provenance["attribution"] == live.licence.attribution
    assert [feature["id"] for feature in _features(_obj(_obj(loaded["collections"])["projects"]))] == [1, 2, 3]
    for relative, digest in _obj(manifest["files"]).items():
        assert _sha((out / relative).read_bytes()) == digest


def test_failed_late_fetch_leaves_no_snapshot_or_fixture_fallback(config: Config, tmp_path: Path) -> None:
    project = replace(config.project_source, where="1=1", out_fields=FIELDS)
    live = replace(config, sources=(project, config.plan_sources[0]), limits=replace(config.limits, max_retries=0))
    service = Service(project.require_url())
    out = tmp_path / "snapshot"
    with responses.RequestsMock(assert_all_requests_are_fired=False) as mocked:
        service.install(mocked)
        mocked.add(responses.GET, live.sources[1].require_url(), status=403)
        with pytest.raises(AcquisitionError, match="HTTP 403"):
            fetch_snapshot(live, out)
    assert service.count_reads == service.id_reads == 2
    assert not out.exists()
    assert not list(tmp_path.glob(".snapshot.tmp-*"))
