"""Legacy v1 read-only acquisition and shared transport helpers.

Interoperability contract:
- fetch_snapshot(config, out, from_fixtures=None) -> manifest, also <out>/snapshot-manifest.json.
  `out` must not exist; stage in a temp sibling then rename. Live failures never fall back to fixtures.
- load_snapshot(snapshot, config) -> {"manifest": dict, "collections": {source_id: FeatureCollection},
  "provenance": {source_id: dict}}. Verify hashes, exact files, source definitions and count/ID/batch checks.
- ArcGIS features sort by OID; properties = out_fields + provenance["object_id_field"] (completeness only,
  never identity; normalizer drops it). Local GeoJSON filters properties to out_fields; local CSV features
  have null geometry and string properties for out_fields.
- ArcGIS provenance: source_id, kind, role, source_url, where, out_fields, object_id_field, object_id_use,
  title, field_definitions, max_record_count, batch_size, count, source_updated_at (null -> warning),
  retrieved_at, origin ("live"|"fixture"), sample, complete_source_count, snapshot_isolation, requests,
  licence_url, attribution; fixtures add capture_where and synthetic. Local provenance: source_path,
  input_sha256, dropped_fields, count, retrieved_at, origin="local".
- Fixture root: capture-manifest.json {"schema": CAPTURE_SCHEMA, "captured_at", "description",
  "synthetic": bool, "sources": {source_id: {"source_url", "retrieved_at", "capture_where", "sample": bool,
  "sample_count", "complete_source_count", "requests": [...], "files": {name: sha256}}}} plus
  sources/<id>/{metadata,count,ids,final-count,final-ids}.json and batches/batch-NNNN.geojson.
  Preserve genuine response bytes. sample=true means count/ids describe capture_where, not the full source.
- fetch_layer(url, where, fields, allowed_hosts, limits, session=None) -> (collection, provenance).
  Same verified complete read, reused by verify-published read-back.
- Shared transport charges every yielded response chunk immediately to its source byte budget, including
  chunks discarded after an interrupted or timed-out attempt. Byte exhaustion is fatal, not retryable.
"""

from __future__ import annotations

import csv
import hashlib
import io
import shutil
import tempfile
import time
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Final
from urllib.parse import urlsplit

import requests

from civic_publish_kit import __version__
from civic_publish_kit.config import (
    SOURCE_FIELD_PATTERN,
    Config,
    ConfigError,
    Limits,
    SnapshotLayout,
    Source,
    canonical_json,
    parse_json_strict,
    validate_arcgis_layer_url,
    validate_where,
)

JSON = dict[str, Any]
SNAPSHOT_SCHEMA: Final = "civic-publish-kit/snapshot/1"
CAPTURE_SCHEMA: Final = "civic-publish-kit/capture/1"
ISOLATION_NOTE: Final = (
    "ArcGIS REST provides no snapshot isolation; count and object IDs were re-read after all batches and "
    "verified unchanged, which reduces but does not eliminate concurrent-edit risk."
)
OID_NOTE: Final = "Requested only to verify count/ID completeness; never a business identity."
GEOJSON_HELP: Final = (
    "Layer does not support f=geojson. Export it as GeoJSON from the public portal, review it, and configure "
    "it as a local 'geojson' source with an explicit mapping."
)
_REQUIRED: Final = ("metadata.json", "count.json", "ids.json", "final-count.json", "final-ids.json")
_TRANSIENT: Final = frozenset({429, 500, 502, 503, 504})
_HEADERS: Final = {"Accept": "application/json", "User-Agent": f"civic-publish-kit/{__version__} (read-only)"}
_sleep: Callable[[float], None] = time.sleep


class AcquisitionError(RuntimeError):
    pass


class UnsupportedGeoJSONError(AcquisitionError):
    pass


class _TransientError(Exception):
    pass


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse(body: bytes, what: str) -> object:
    try:
        return parse_json_strict(body.decode("utf-8"))
    except (UnicodeDecodeError, ConfigError) as exc:
        raise AcquisitionError(f"{what}: invalid JSON ({exc})") from exc


def _obj(value: object, what: str) -> JSON:
    if not isinstance(value, dict):
        raise AcquisitionError(f"{what}: expected a JSON object")
    return value


def _int(value: object, what: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AcquisitionError(f"{what}: expected integer >= {minimum}, got {value!r}")
    return value


def _safe_rel(rel: str) -> str:
    if "\\" in rel or PurePosixPath(rel).is_absolute() or any(p in {"", ".", ".."} for p in rel.split("/")):
        raise AcquisitionError(f"unsafe relative path {rel!r}")
    return rel


def _read_file(path: Path, limit: int) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise AcquisitionError(f"{path}: expected a regular file (symlinks rejected)")
    if path.stat().st_size > limit:
        raise AcquisitionError(f"{path}: exceeds {limit} byte limit")
    return path.read_bytes()


def _tree(root: Path) -> set[str]:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_symlink() or not p.is_dir()}


class _Reader:
    def __init__(self, url: str, hosts: tuple[str, ...], limits: Limits, session: requests.Session) -> None:
        self.url, self.hosts, self.limits, self.session = url, hosts, limits, session
        self.used = 0
        self.log: list[JSON] = []
        self.files: dict[str, bytes] = {}
        self.last = b""

    def get(self, endpoint: str, params: dict[str, str], name: str | None) -> object:
        target = f"{self.url}/{endpoint}" if endpoint else self.url
        prepared = requests.Request("GET", target, params=params, headers=dict(_HEADERS)).prepare()
        exact = prepared.url or ""
        parts = urlsplit(exact)
        if parts.scheme != "https" or parts.hostname not in self.hosts or "@" in parts.netloc:
            raise AcquisitionError(f"refusing non-allowlisted request {exact}")
        requested_at = _now()
        body = self._send(prepared, exact)
        self.last = body
        self.log.append({"url": exact, "requested_at": requested_at, "status": 200, "bytes": len(body),
                         "sha256": _sha(body), "file": name})
        if name is not None:
            self.files[name] = body
        data = _parse(body, exact)
        if isinstance(data, dict) and "error" in data:
            raise AcquisitionError(f"{exact}: ArcGIS error {data['error']!r}; never treated as empty")
        return data

    def keep_last(self, name: str) -> None:
        self.files[name] = self.last
        self.log[-1].update(file=name, accepted=True)

    def _send(self, prepared: requests.PreparedRequest, label: str) -> bytes:
        attempts = self.limits.max_retries + 1
        for attempt in range(attempts):
            if self.used >= self.limits.max_response_bytes:
                raise AcquisitionError(f"{label}: per-source byte limit exhausted")
            try:
                return self._attempt(prepared, label)
            except _TransientError as exc:
                if self.used >= self.limits.max_response_bytes:
                    raise AcquisitionError(f"{label}: per-source byte limit exhausted") from exc
                if attempt + 1 >= attempts:
                    raise AcquisitionError(f"{label}: {exc} after {attempts} attempt(s)") from exc
                _sleep(min(4.0, 0.5 * 2**attempt))
        raise AcquisitionError(f"{label}: no attempts made")

    def _attempt(self, prepared: requests.PreparedRequest, label: str) -> bytes:
        timeout = self.limits.timeout_seconds
        deadline = time.monotonic() + timeout
        try:
            response = self.session.send(prepared, timeout=(timeout, timeout), allow_redirects=False, stream=True)
        except (requests.ConnectionError, requests.Timeout) as exc:
            raise _TransientError(f"transport error {type(exc).__name__}") from exc
        with response:
            status = response.status_code
            if status in _TRANSIENT:
                raise _TransientError(f"HTTP {status}")
            if 300 <= status < 400:
                raise AcquisitionError(f"{label}: redirect HTTP {status} refused")
            if status != 200:
                raise AcquisitionError(f"{label}: HTTP {status}; a failed read is never an empty layer")
            remaining = self.limits.max_response_bytes - self.used
            declared = response.headers.get("Content-Length", "")
            if declared.isdigit() and int(declared) > remaining:
                raise AcquisitionError(f"{label}: response would exceed per-source byte limit")
            chunks: list[bytes] = []
            try:
                for chunk in response.iter_content(65536):
                    self.used += len(chunk)
                    if self.used > self.limits.max_response_bytes:
                        raise AcquisitionError(f"{label}: exceeds {self.limits.max_response_bytes} bytes per source")
                    if time.monotonic() > deadline:
                        raise _TransientError(f"read exceeded {timeout}s timeout")
                    chunks.append(chunk)
            except (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError) as exc:
                raise _TransientError(f"interrupted read {type(exc).__name__}") from exc
        return b"".join(chunks)


def _facts(metadata: object, fields: Sequence[str]) -> JSON:
    meta = _obj(metadata, "metadata.json")
    defs = meta.get("fields")
    if not isinstance(defs, list) or not all(isinstance(d, dict) and isinstance(d.get("name"), str) for d in defs):
        raise AcquisitionError("metadata.json: missing field definitions")
    oid = meta.get("objectIdField")
    if not isinstance(oid, str) or not oid:
        oids = [d["name"] for d in defs if d.get("type") == "esriFieldTypeOID"]
        if len(oids) != 1:
            raise AcquisitionError("metadata.json: object ID field not declared")
        oid = oids[0]
    missing = [n for n in (*fields, oid) if n not in {d["name"] for d in defs}]
    if missing:
        raise AcquisitionError(f"metadata.json: allowlisted fields absent: {', '.join(missing)}")
    if "geojson" not in {p.strip().lower() for p in str(meta.get("supportedQueryFormats", "")).split(",")}:
        raise UnsupportedGeoJSONError(GEOJSON_HELP)
    edit = meta.get("editingInfo")
    stamp = edit.get("lastEditDate") if isinstance(edit, dict) else None
    updated = None
    if isinstance(stamp, int) and not isinstance(stamp, bool) and stamp >= 0:
        updated = datetime.fromtimestamp(stamp / 1000, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"object_id_field": oid, "title": meta.get("name") if isinstance(meta.get("name"), str) else None,
            "field_definitions": [d for d in defs if d["name"] in {*fields, oid}],
            "max_record_count": _int(meta.get("maxRecordCount"), "maxRecordCount", 1), "source_updated_at": updated}


def _count(data: object, what: str, max_records: int) -> int:
    count = _int(_obj(data, what).get("count"), f"{what} count")
    if count > max_records:
        raise AcquisitionError(f"{what}: {count} records exceeds {max_records} record limit")
    return count


def _ids(data: object, what: str, oid: str, count: int) -> list[int]:
    obj = _obj(data, what)
    if obj.get("objectIdFieldName") not in (None, oid):
        raise AcquisitionError(f"{what}: objectIdFieldName does not match {oid!r}")
    raw = obj.get("objectIds")
    raw = [] if raw is None and count == 0 else raw
    if not isinstance(raw, list):
        raise AcquisitionError(f"{what}: objectIds missing")
    ids = [_int(v, f"{what} objectId") for v in raw]
    if len(set(ids)) != len(ids):
        raise AcquisitionError(f"{what}: duplicate object IDs")
    if len(ids) != count:
        raise AcquisitionError(f"{what}: {len(ids)} IDs but count {count}; not count-complete")
    return sorted(ids)


def _features(data: object, what: str, oid: str, allowed: set[str]) -> tuple[list[JSON], bool]:
    coll = _obj(data, what)
    if coll.get("type") != "FeatureCollection" or not isinstance(coll.get("features"), list):
        raise AcquisitionError(f"{what}: expected a GeoJSON FeatureCollection")
    props = coll.get("properties")
    exceeded = coll.get("exceededTransferLimit") is True or (
        isinstance(props, dict) and props.get("exceededTransferLimit") is True)
    out: list[JSON] = []
    for i, raw in enumerate(coll["features"]):
        feature = _obj(raw, f"{what} feature {i}")
        properties = _obj(feature.get("properties") or {}, f"{what} feature {i} properties")
        if feature.get("type") != "Feature":
            raise AcquisitionError(f"{what} feature {i}: not a Feature")
        extra = sorted(set(properties) - allowed)
        if extra:
            raise AcquisitionError(f"{what}: non-allowlisted fields returned: {', '.join(extra)}")
        fid, pid = feature.get("id"), properties.get(oid)
        if fid is not None and pid is not None and fid != pid:
            raise AcquisitionError(f"{what} feature {i}: id conflicts with {oid}")
        value = _int(pid if pid is not None else fid, f"{what} feature {i} object ID")
        out.append({"type": "Feature", "id": value, "geometry": feature.get("geometry"),
                    "properties": {**properties, oid: value}})
    return out, exceeded


def _exact(got: list[int], expected: Iterable[int], what: str) -> None:
    wanted = set(expected)
    dup = sorted(k for k, n in Counter(got).items() if n > 1)
    missing, unexpected = sorted(wanted - set(got)), sorted(set(got) - wanted)
    if dup or missing or unexpected:
        raise AcquisitionError(f"{what}: IDs not exact (missing={missing[:10]}, duplicate={dup[:10]}, "
                               f"unexpected={unexpected[:10]})")


def _assemble(files: dict[str, bytes], fields: Sequence[str], limits: Limits) -> tuple[JSON, JSON]:
    for name in _REQUIRED:
        if name not in files:
            raise AcquisitionError(f"missing stored response {name}")
    facts = _facts(_parse(files["metadata.json"], "metadata.json"), fields)
    oid: str = facts["object_id_field"]
    count = _count(_parse(files["count.json"], "count.json"), "count.json", limits.max_records)
    ids = _ids(_parse(files["ids.json"], "ids.json"), "ids.json", oid, count)
    batches = sorted(n for n in files if n.startswith("batches/"))
    if batches != [f"batches/batch-{i:04d}.geojson" for i in range(1, len(batches) + 1)]:
        raise AcquisitionError("batch files must be numbered consecutively from batch-0001.geojson")
    unknown = sorted(set(files) - {*_REQUIRED, *batches, "requests.json"})
    if unknown:
        raise AcquisitionError(f"unexpected stored files: {', '.join(unknown)}")
    features: list[JSON] = []
    for name in batches:
        got, exceeded = _features(_parse(files[name], name), name, oid, {*fields, oid})
        if exceeded:
            raise AcquisitionError(f"{name}: stored batch is transfer-limited")
        features.extend(got)
    _exact([f["id"] for f in features], ids, "batches")
    final_count = _count(_parse(files["final-count.json"], "final-count.json"), "final-count.json", limits.max_records)
    final_ids = _ids(_parse(files["final-ids.json"], "final-ids.json"), "final-ids.json", oid, final_count)
    if final_count != count or final_ids != ids:
        raise AcquisitionError("source changed during read: end-of-read count/IDs differ; retry later")
    features.sort(key=lambda f: int(f["id"]))
    facts.update(count=count, batch_size=limits.batch_size(facts["max_record_count"]))
    return {"type": "FeatureCollection", "features": features}, facts


def _fetch_files(url: str, where: str, fields: tuple[str, ...], hosts: tuple[str, ...], limits: Limits,
                 session: requests.Session) -> tuple[dict[str, bytes], list[JSON]]:
    try:
        validate_arcgis_layer_url(url, hosts)
        validate_where(where)
    except ConfigError as exc:
        raise AcquisitionError(str(exc)) from exc
    if any(not SOURCE_FIELD_PATTERN.fullmatch(f) for f in fields):
        raise AcquisitionError("invalid field names")
    reader = _Reader(url, hosts, limits, session)
    facts = _facts(reader.get("", {"f": "json"}, "metadata.json"), fields)
    oid: str = facts["object_id_field"]
    count_q = {"where": where, "returnCountOnly": "true", "f": "json"}
    ids_q = {"where": where, "returnIdsOnly": "true", "f": "json"}
    count = _count(reader.get("query", count_q, "count.json"), "count.json", limits.max_records)
    ids = _ids(reader.get("query", ids_q, "ids.json"), "ids.json", oid, count)
    out_fields = ",".join(dict.fromkeys([*fields, oid]))
    accepted = [0]

    def read(chunk: list[int]) -> None:
        params = {"where": where, "objectIds": ",".join(map(str, chunk)), "outFields": out_fields,
                  "returnGeometry": "true", "outSR": "4326", "f": "geojson"}
        got, exceeded = _features(reader.get("query", params, None), "batch", oid, {*fields, oid})
        if exceeded:
            reader.log[-1]["accepted"] = False
            if len(chunk) == 1:
                raise AcquisitionError(f"transfer limit unresolved for single object ID {chunk[0]}")
            read(chunk[: len(chunk) // 2])
            read(chunk[len(chunk) // 2 :])
            return
        _exact([f["id"] for f in got], chunk, "batch")
        accepted[0] += 1
        if accepted[0] > 9999:
            raise AcquisitionError("too many batches")
        reader.keep_last(f"batches/batch-{accepted[0]:04d}.geojson")

    size = limits.batch_size(facts["max_record_count"])
    for start in range(0, len(ids), size):
        read(ids[start : start + size])
    reader.get("query", count_q, "final-count.json")
    reader.get("query", ids_q, "final-ids.json")
    return reader.files, reader.log


def _provenance(url: str, where: str, fields: Sequence[str], facts: JSON, at: str, origin: str, log: list[Any]) -> JSON:
    return {"source_url": url, "where": where, "out_fields": list(fields), **facts, "object_id_use": OID_NOTE,
            "retrieved_at": at, "origin": origin, "snapshot_isolation": ISOLATION_NOTE, "requests": log}


def _session() -> requests.Session:
    session = requests.Session()
    session.trust_env = False
    return session


def fetch_layer(url: str, where: str, fields: Sequence[str], allowed_hosts: Iterable[str], limits: Limits,
                session: requests.Session | None = None) -> tuple[JSON, JSON]:
    active = session or _session()
    try:
        started = _now()
        files, log = _fetch_files(url, where, tuple(fields), tuple(allowed_hosts), limits, active)
        collection, facts = _assemble(files, tuple(fields), limits)
        return collection, _provenance(url, where, fields, facts, started, "live", log)
    finally:
        if session is None:
            active.close()


def _local_geojson(data: bytes, fields: Sequence[str], max_records: int, what: str) -> tuple[JSON, list[str]]:
    coll = _obj(_parse(data, what), what)
    if coll.get("type") != "FeatureCollection" or not isinstance(coll.get("features"), list):
        raise AcquisitionError(f"{what}: expected a GeoJSON FeatureCollection")
    if len(coll["features"]) > max_records:
        raise AcquisitionError(f"{what}: exceeds {max_records} record limit")
    dropped: set[str] = set()
    features: list[JSON] = []
    for i, raw in enumerate(coll["features"]):
        feature = _obj(raw, f"{what} feature {i}")
        if feature.get("type") != "Feature":
            raise AcquisitionError(f"{what} feature {i}: not a Feature")
        props = _obj(feature.get("properties") or {}, f"{what} feature {i} properties")
        dropped |= set(props) - set(fields)
        features.append({"type": "Feature", "geometry": feature.get("geometry"),
                         "properties": {k: v for k, v in props.items() if k in fields}})
    return {"type": "FeatureCollection", "features": features}, sorted(dropped)


def _local_csv(data: bytes, fields: Sequence[str], max_records: int, what: str) -> tuple[JSON, bytes, list[str]]:
    try:
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"), newline="")))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise AcquisitionError(f"{what}: unreadable UTF-8 CSV ({exc})") from exc
    if not rows or len(set(rows[0])) != len(rows[0]):
        raise AcquisitionError(f"{what}: CSV header missing or has duplicate columns")
    header, body = rows[0], rows[1:]
    missing = [f for f in fields if f not in header]
    if missing or len(body) > max_records:
        raise AcquisitionError(f"{what}: missing allowlisted columns {missing} or exceeds {max_records} records")
    index = [header.index(f) for f in fields]
    kept: list[list[str]] = []
    for line, row in enumerate(body, start=2):
        if len(row) != len(header):
            raise AcquisitionError(f"{what}: line {line} has {len(row)} columns, expected {len(header)}")
        kept.append([row[i] for i in index])
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(fields)
    writer.writerows(kept)
    features = [{"type": "Feature", "geometry": None, "properties": dict(zip(fields, r, strict=True))} for r in kept]
    return ({"type": "FeatureCollection", "features": features}, buffer.getvalue().encode("utf-8"),
            sorted(set(header) - set(fields)))


def _local(config: Config, source: Source, data: bytes) -> tuple[JSON, dict[str, bytes], list[str]]:
    limit = config.limits.max_records
    if source.kind == "geojson":
        collection, dropped = _local_geojson(data, source.out_fields, limit, source.id)
        return collection, {"input.geojson": canonical_json(collection)}, dropped
    collection, filtered, dropped = _local_csv(data, source.out_fields, limit, source.id)
    return collection, {"input.csv": filtered}, dropped


def _read_local(config: Config, source: Source) -> tuple[dict[str, bytes], JSON]:
    if (config.base_dir / source.require_path()).is_symlink():
        raise AcquisitionError(f"{source.id}: symlinked local inputs are rejected")
    data = _read_file(config.resolve_local_path(source), config.limits.max_response_bytes)
    collection, files, dropped = _local(config, source, data)
    return files, {"source_path": source.path, "input_sha256": _sha(data), "out_fields": list(source.out_fields),
                   "dropped_fields": dropped, "count": len(collection["features"]), "retrieved_at": _now(),
                   "origin": "local", "source_updated_at": None}


def _load_capture(root: Path) -> JSON:
    data = _read_file(SnapshotLayout(root).capture_manifest_path(), 1_000_000)
    capture = _obj(_parse(data, "capture-manifest.json"), "capture-manifest.json")
    if capture.get("schema") != CAPTURE_SCHEMA or not isinstance(capture.get("synthetic"), bool):
        raise AcquisitionError(f"capture-manifest.json: requires schema {CAPTURE_SCHEMA} and boolean synthetic")
    _obj(capture.get("sources"), "capture-manifest.json sources")
    return {**capture, "manifest_sha256": _sha(data)}


def _fixture_source(capture: JSON, root: Path, source: Source, limits: Limits) -> tuple[dict[str, bytes], JSON]:
    what = f"capture-manifest sources.{source.id}"
    entry = _obj(capture["sources"].get(source.id), what)
    if entry.get("source_url") != source.url:
        raise AcquisitionError(f"{what}: source_url does not match configuration")
    listed = _obj(entry.get("files"), f"{what} files")
    folder = SnapshotLayout(root).source_dir(source.id)
    if folder.is_symlink() or not folder.is_dir() or _tree(folder) != set(listed) or "requests.json" in listed:
        raise AcquisitionError(f"{what}: fixture files do not exactly match the capture manifest")
    files: dict[str, bytes] = {}
    for name, digest in listed.items():
        files[name] = _read_file(folder / _safe_rel(name), limits.max_response_bytes)
        if _sha(files[name]) != digest:
            raise AcquisitionError(f"{what}: hash mismatch for {name}")
    _, facts = _assemble(files, source.out_fields, limits)
    sample, complete = entry.get("sample"), _int(entry.get("complete_source_count"), f"{what} complete_source_count")
    if not isinstance(sample, bool):
        raise AcquisitionError(f"{what}: sample must be boolean")
    if sample:
        if _int(entry.get("sample_count"), f"{what} sample_count") != facts["count"] or complete < facts["count"]:
            raise AcquisitionError(f"{what}: sample_count must equal captured count and not exceed complete count")
    elif complete != facts["count"]:
        raise AcquisitionError(f"{what}: complete_source_count differs from captured count")
    at, capture_where, log = entry.get("retrieved_at"), entry.get("capture_where"), entry.get("requests")
    if not isinstance(at, str) or not isinstance(capture_where, str) or not isinstance(log, list):
        raise AcquisitionError(f"{what}: retrieved_at, capture_where and requests are required")
    files["requests.json"] = canonical_json(log)
    prov = _provenance(source.require_url(), source.require_where(), source.out_fields, facts, at, "fixture", log)
    prov.update(sample=sample, complete_source_count=complete, capture_where=capture_where,
                synthetic=capture["synthetic"])
    return files, prov


def fetch_snapshot(config: Config, out: Path, from_fixtures: Path | None = None) -> JSON:
    out = Path(out)
    if out.exists() or out.is_symlink():
        raise AcquisitionError(f"{out}: already exists; snapshots are never overwritten")
    capture = _load_capture(from_fixtures) if from_fixtures is not None else None
    created_at = _now()
    collected: dict[str, tuple[dict[str, bytes], JSON]] = {}
    session = _session()
    try:
        for source in config.sources:
            if source.kind != "arcgis":
                files, prov = _read_local(config, source)
            elif capture is not None and from_fixtures is not None:
                files, prov = _fixture_source(capture, from_fixtures, source, config.limits)
            else:
                started, url, where = _now(), source.require_url(), source.require_where()
                files, log = _fetch_files(url, where, source.out_fields, config.allowed_hosts, config.limits, session)
                _, facts = _assemble(files, source.out_fields, config.limits)
                prov = _provenance(url, where, source.out_fields, facts, started, "live", log)
                prov.update(sample=False, complete_source_count=facts["count"])
                files["requests.json"] = canonical_json(log)
            prov.update(source_id=source.id, kind=source.kind, role=source.role,
                        licence_url=config.licence.url, attribution=config.licence.attribution)
            collected[source.id] = (files, prov)
    finally:
        session.close()
    manifest: JSON = {
        "schema": SNAPSHOT_SCHEMA, "created_at": created_at, "tool_version": __version__,
        "config_name": config.name, "config_digest": config.digest(),
        "origin": "live" if capture is None else "fixture", "snapshot_isolation": ISOLATION_NOTE,
        "licence": config.licence.to_dict(), "files": {}, "sources": {k: v[1] for k, v in collected.items()},
    }
    if capture is not None:
        manifest["capture"] = {
            k: capture.get(k)
            for k in ("captured_at", "description", "synthetic", "manifest_sha256")
        }
    out.parent.mkdir(parents=True, exist_ok=True)
    temp = Path(tempfile.mkdtemp(prefix=f".{out.name}.tmp-", dir=out.parent))
    try:
        for sid, (files, _) in sorted(collected.items()):
            for name, data in sorted(files.items()):
                rel = _safe_rel(f"sources/{sid}/{name}")
                (temp / rel).parent.mkdir(parents=True, exist_ok=True)
                with (temp / rel).open("xb") as handle:
                    handle.write(data)
                manifest["files"][rel] = _sha(data)
        with (temp / SnapshotLayout.MANIFEST).open("xb") as handle:
            handle.write(canonical_json(manifest))
        if out.exists() or out.is_symlink():
            raise AcquisitionError(f"{out}: appeared during fetch; refusing to overwrite")
        temp.rename(out)
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return manifest


def load_snapshot(snapshot: Path, config: Config) -> JSON:
    root = Path(snapshot)
    if root.is_symlink() or not root.is_dir():
        raise AcquisitionError(f"{root}: snapshot must be a real directory")
    manifest = _obj(_parse(_read_file(root / SnapshotLayout.MANIFEST, 10_000_000), "manifest"), "manifest")
    if manifest.get("schema") != SNAPSHOT_SCHEMA:
        raise AcquisitionError(f"unsupported snapshot schema {manifest.get('schema')!r}")
    listed = _obj(manifest.get("files"), "manifest files")
    if _tree(root) - {SnapshotLayout.MANIFEST} != set(listed):
        raise AcquisitionError("snapshot files differ from manifest (missing, extra or tampered)")
    blobs: dict[str, bytes] = {}
    for rel, digest in listed.items():
        blobs[rel] = _read_file(root / _safe_rel(rel), config.limits.max_response_bytes)
        if _sha(blobs[rel]) != digest:
            raise AcquisitionError(f"{rel}: checksum mismatch")
    sources = _obj(manifest.get("sources"), "manifest sources")
    if set(sources) != {s.id for s in config.sources}:
        raise AcquisitionError("snapshot sources do not match configured sources")
    collections: dict[str, JSON] = {}
    provenance: dict[str, JSON] = {}
    used = 0
    for source in config.sources:
        prov = _obj(sources[source.id], f"manifest sources.{source.id}")
        want: JSON = {"kind": source.kind, "role": source.role, "out_fields": list(source.out_fields)}
        if source.is_remote:
            want.update(source_url=source.url, where=source.where)
        else:
            want.update(source_path=source.path)
        if any(prov.get(k) != v for k, v in want.items()):
            raise AcquisitionError(f"{source.id}: snapshot source definition differs from configuration")
        prefix = f"sources/{source.id}/"
        files = {rel[len(prefix) :]: data for rel, data in blobs.items() if rel.startswith(prefix)}
        used += len(files)
        if source.is_remote:
            collections[source.id], _ = _assemble(files, source.out_fields, config.limits)
        else:
            name = "input.geojson" if source.kind == "geojson" else "input.csv"
            if set(files) != {name}:
                raise AcquisitionError(f"{source.id}: expected only {name}")
            collections[source.id], stored, _ = _local(config, source, files[name])
            if stored[name] != files[name]:
                raise AcquisitionError(f"{source.id}: stored local input is not in filtered canonical form")
        provenance[source.id] = prov
    if used != len(blobs):
        raise AcquisitionError("snapshot contains files outside configured sources")
    return {"manifest": manifest, "collections": collections, "provenance": provenance}
