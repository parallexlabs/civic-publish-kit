"""City update rehearsal: layer-167 extraction, EPSG:26910 to WGS84 normalization and normalized bundles.

Contracts used by the CLI, reports and store:
- load_rehearsal_config(path) -> RehearsalConfig. Strict keys. Only the official layer 167, the City host, the fields
  PROJECT_NO/DESCRIPTION/STATUS, EPSG:26910 to EPSG:4326 and the official licence are accepted. Integer caps reject
  fractional values.
- extract(cfg, out, from_fixtures=None, session=None) -> snapshot manifest dict.
  - Live reads happen in this order: metadata, count, all object IDs, ordered pages, then a final count/ID re-read.
  - Pages use orderByFields=<OID> ASC, resultOffset and resultRecordCount, with page size at most
    min(page_size, maxRecordCount, 2000), outSR=26910 and f=geojson.
  - Page 1 is also read with outSR=4326 as a paired CRS check.
  - Each page must equal its expected OID slice, in order. A transfer-limit flag on the final page blocks.
  - A failed live read never falls back to fixtures.
- Paired CRS response:
  - Every non-empty snapshot must contain crs-check/page-0001-epsg4326.geojson.
  - Its object IDs must equal the first native page exactly, in order.
  - A transfer-limit flag on it is accepted only when that page is complete and the source spans more than one page.
  - An empty query must not carry a paired response.
- Snapshot directory: snapshot-manifest.json plus metadata.json, count.json, ids.json, pages/page-NNNN.geojson,
  final-count.json, final-ids.json, requests.json and (for non-empty queries) crs-check/page-0001-epsg4326.geojson.
  - Manifest keys: schema, origin, retrieved_at, query{layer_url, where, configured_where, scope, out_fields, out_sr,
    order_by, page_size}, counts{initial, final, pages}, complete_service_count, object_id_field, object_id_use,
    snapshot_isolation, licence, synthetic, capture, files{name: sha256}.
  - requests.json lists one entry per response file: {file, url, sha256, bytes, retrieved_at or requested_at}.
- Fixture directory: the same genuine response files (no requests.json) plus capture-manifest.json with keys
  schema (CAPTURE_SCHEMA), synthetic, scope, layer_url, where, captured_at, retrieved_at, description,
  complete_service_count (int or null), requests (same entries as requests.json) and files {name: sha256}.
  - scope is "service_sample" or "complete_service". A service_sample must answer its own where exactly and is
    never treated as complete. complete_service requires where to equal the configured where.
- Every request URL, parameter set, hash and byte count is checked against its response file and the reconciled
  query. Raw response bytes per source are capped at 128 MiB in total. Any path with a symlink component is
  rejected, and outputs are never overwritten.
- load_rehearsal_snapshot(path, cfg) -> RehearsalSnapshot. Fully re-verified.
- normalize(cfg, snapshot, out, buildings=None) -> NormalizedResult. normalize_snapshot_data is the pure core.
- write_bundle(out, result) writes projects.csv, buildings.csv, projects.geojson, source-manifest.json,
  findings.json, field-mapping.json and bundle-manifest.json. projects.geojson holds application boundaries,
  never building footprints.
- validate_bundle_files(files, manifest) -> NormalizedBundle. Pure revalidation of:
  - hashes, schemas, types and lengths;
  - CSV/GeoJSON agreement, joins, counts and the ok flag;
  - source counts: initial and final must be equal whole numbers (not booleans) no greater than 20,000, and the page
    count must fit the page size;
  - projects: a blocker-free bundle must hold exactly the source count, and a blocked bundle may hold a subset
    but never more.
  load_bundle(path) reads the tree, then delegates to it. A blocked bundle still loads (so it can be reported) with
  ok=False.
- Both the supplied building CSV and the normalized CSV tables are parsed with strict CSV rules.
- Nullability comes from FIELD_MAPPING.
  - projects.title is nullable. A null, omitted or blank City DESCRIPTION becomes a null title (an empty CSV cell)
    with a missing_description warning. No placeholder is ever invented, and project_id stays the identifier.
  - A DESCRIPTION that is not text, or is longer than the field limit, still blocks.
"""

from __future__ import annotations

import csv
import importlib
import io
import math
import re
import shutil
import tempfile
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal
from urllib.parse import parse_qs, urlsplit

import pyproj
import requests
from pyproj import Transformer
from pyproj.enums import TransformDirection
from pyproj.exceptions import ProjError
from shapely.errors import GEOSException
from shapely.geometry import shape
from shapely.validation import explain_validity

from civic_publish_kit.acquire import (
    ISOLATION_NOTE,
    OID_NOTE,
    AcquisitionError,
    _count,
    _exact,
    _facts,
    _features,
    _ids,
    _now,
    _obj,
    _parse,
    _read_file,
    _Reader,
    _safe_rel,
    _session,
    _sha,
    _tree,
)
from civic_publish_kit.config import (
    MAX_GEOJSON_OUTPUT_BYTES,
    ConfigError,
    Envelope,
    Limits,
    canonical_json,
    parse_json_strict,
    validate_https_url,
    validate_where,
)
from civic_publish_kit.normalize import Finding, Severity, _polygonal, _utc

JSON = dict[str, Any]
Status = Literal["approved", "conditionally_approved", "review_unknown", "unknown"]
STATUSES: Final[tuple[Status, ...]] = ("approved", "conditionally_approved", "review_unknown", "unknown")
SCOPES: Final = ("service_sample", "complete_service")
_HTTPS: Final = "https:" + "//"
OFFICIAL_HOST: Final = "gisservices" + ".surrey.ca"
OFFICIAL_LAYER_URL: Final = _HTTPS + OFFICIAL_HOST + "/arcgis/rest/services/OpenData/MapServer/167"
OFFICIAL_LICENCE_URL: Final = _HTTPS + "opendata-surrey.hub" + ".arcgis.com/pages/55089a19491a4fe59a41e059fd8af708"
OFFICIAL_FIELDS: Final = {"project_id": "PROJECT_NO", "title": "DESCRIPTION", "status": "STATUS"}
NATIVE_CRS: Final = "EPSG:26910"
TARGET_CRS: Final = "EPSG:4326"
CONFIG_SCHEMA: Final = "civic-publish-kit/rehearsal-config/1"
SNAPSHOT_SCHEMA: Final = "civic-publish-kit/rehearsal-snapshot/1"
CAPTURE_SCHEMA: Final = "civic-publish-kit/rehearsal-capture/1"
BUNDLE_SCHEMA: Final = "civic-publish-kit/rehearsal-bundle/1"
SOURCE_MANIFEST_SCHEMA: Final = "civic-publish-kit/rehearsal-source-manifest/1"
SNAPSHOT_MANIFEST: Final = "snapshot-manifest.json"
CAPTURE_MANIFEST: Final = "capture-manifest.json"
BUNDLE_MANIFEST: Final = "bundle-manifest.json"
CRS_CHECK_FILE: Final = "crs-check/page-0001-epsg4326.geojson"
SOURCE_ID: Final = "layer_167"
MAX_PAGE_SIZE: Final = 2000
MAX_RECORDS: Final = 20_000
MAX_RESPONSE_BYTES: Final = 128 * 1024 * 1024
PAIR_TOLERANCE_DEGREES: Final = 0.000001
ROUNDTRIP_TOLERANCE_METRES: Final = 0.001
FOOTPRINT_NOTE: Final = "Layer 167 holds development applications and application boundaries, not building footprints."
_EPSG: Final = re.compile(r"^(?:urn:ogc:def:crs:)?EPSG:{1,2}([0-9]{4,6})$", re.IGNORECASE)
_TIMESTAMP: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_REQUIRED: Final = ("metadata.json", "count.json", "ids.json", "final-count.json", "final-ids.json")
_SNAPSHOT_KEYS: Final = frozenset({"schema", "origin", "retrieved_at", "query", "counts", "complete_service_count",
                                   "object_id_field", "object_id_use", "snapshot_isolation", "licence", "synthetic",
                                   "capture", "files"})
_UNTRIMMED: Final = frozenset({"status_raw"})
_network = importlib.import_module("pyproj.network")


class RehearsalError(AcquisitionError):
    pass


@dataclass(frozen=True, slots=True)
class FieldMap:
    table: Literal["projects", "buildings"]
    name: str
    arcgis_type: str
    length: int | None
    nullable: bool
    description: str

    def to_dict(self) -> JSON:
        return {"table": self.table, "name": self.name, "arcgis_type": self.arcgis_type, "length": self.length,
                "nullable": self.nullable, "description": self.description}


_S, _D, _I = "esriFieldTypeString", "esriFieldTypeDouble", "esriFieldTypeSmallInteger"
FIELD_MAPPING: Final[tuple[FieldMap, ...]] = (
    FieldMap("projects", "project_id", _S, 50, False, "Trimmed PROJECT_NO; stable identity, never OBJECTID."),
    FieldMap("projects", "title", _S, 4000, True, "Trimmed DESCRIPTION; null when the City value is null or blank."),
    FieldMap("projects", "status", _S, 32, False, "approved, conditionally_approved, review_unknown or unknown."),
    FieldMap("projects", "status_raw", _S, 255, False, "Exact source STATUS value."),
    FieldMap("projects", "longitude", _D, None, False, "WGS84 longitude of the application-boundary interior point."),
    FieldMap("projects", "latitude", _D, None, False, "WGS84 latitude of the application-boundary interior point."),
    FieldMap("projects", "location_basis", _S, 32, False, "application_interior_point (Shapely representative_point)."),
    FieldMap("projects", "source_url", _S, 255, False, "HTTPS URL of public layer 167."),
    FieldMap("projects", "retrieved_at", _S, 20, False, "UTC retrieval time YYYY-MM-DDTHH:MM:SSZ."),
    FieldMap("projects", "licence", _S, 255, False, "Public-data licence attribution."),
    FieldMap("buildings", "building_id", _S, 64, False, "Supplied stable building identifier."),
    FieldMap("buildings", "project_id", _S, 50, False, "Joins projects.project_id (many buildings to one project)."),
    FieldMap("buildings", "source_url", _S, 255, False, "Public HTTPS source of the supplied building row."),
    FieldMap("buildings", "building_name", _S, 255, True, "Supplied name, if any."),
    FieldMap("buildings", "storeys", _I, None, True, "Supplied whole-number storeys; never derived."),
    FieldMap("buildings", "height_m", _D, None, True, "Supplied height in metres; never derived."),
    FieldMap("buildings", "building_use", _S, 100, True, "Supplied use, if any."),
)
PROJECT_COLUMNS: Final = tuple(f.name for f in FIELD_MAPPING if f.table == "projects")
BUILDING_COLUMNS: Final = tuple(f.name for f in FIELD_MAPPING if f.table == "buildings")
BUNDLE_FILES: Final = ("projects.csv", "buildings.csv", "projects.geojson", "source-manifest.json", "findings.json",
                       "field-mapping.json")
_LIMITS: Final = {m.name: m.length for m in FIELD_MAPPING}


@dataclass(frozen=True, slots=True)
class RehearsalConfig:
    name: str
    layer_url: str
    allowed_hosts: tuple[str, ...]
    where: str
    id_field: str
    title_field: str
    status_field: str
    native_crs: str
    target_crs: str
    page_size: int
    timeout_seconds: float
    max_retries: int
    max_records: int
    max_response_bytes: int
    status_map: Mapping[str, Status]
    licence_name: str
    licence_url: str
    attribution: str
    region_envelope: Envelope

    @property
    def out_fields(self) -> tuple[str, ...]:
        return (self.id_field, self.title_field, self.status_field)

    @property
    def native_epsg(self) -> int:
        return _epsg(self.native_crs)

    def limits(self) -> Limits:
        return Limits(timeout_seconds=self.timeout_seconds, max_retries=self.max_retries,
                      max_records=self.max_records, max_response_bytes=self.max_response_bytes,
                      max_batch_size=self.page_size)

    def licence(self) -> JSON:
        return {"name": self.licence_name, "url": self.licence_url, "attribution": self.attribution}


@dataclass(frozen=True, slots=True)
class RehearsalSnapshot:
    manifest: JSON
    manifest_sha256: str
    features: tuple[JSON, ...]
    facts: JSON
    requests: tuple[JSON, ...]
    crs_check: JSON | None


@dataclass(frozen=True, slots=True)
class NormalizedResult:
    projects: tuple[JSON, ...]
    buildings: tuple[JSON, ...]
    features: tuple[JSON, ...]
    provenance: JSON
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not any(f.severity == "blocking" for f in self.findings)

    def files(self) -> dict[str, bytes]:
        return {
            "projects.csv": _csv_bytes(PROJECT_COLUMNS, self.projects),
            "buildings.csv": _csv_bytes(BUILDING_COLUMNS, self.buildings),
            "projects.geojson": canonical_json({"type": "FeatureCollection", "features": list(self.features)}) + b"\n",
            "source-manifest.json": canonical_json(self.provenance) + b"\n",
            "findings.json": canonical_json([f.to_dict() for f in self.findings]) + b"\n",
            "field-mapping.json": _field_mapping_bytes(),
        }


@dataclass(frozen=True, slots=True)
class NormalizedBundle:
    manifest: JSON
    files: Mapping[str, bytes]
    projects: tuple[dict[str, str], ...]
    buildings: tuple[dict[str, str], ...]
    geojson: JSON
    provenance: JSON
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not any(f.severity == "blocking" for f in self.findings)


def _field_mapping_bytes() -> bytes:
    return canonical_json([m.to_dict() for m in FIELD_MAPPING]) + b"\n"


def _epsg(value: str) -> int:
    match = _EPSG.fullmatch(value)
    if match is None:
        raise RehearsalError(f"unrecognized CRS {value!r}")
    return int(match.group(1))


def _finite(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int | float) and math.isfinite(value)


def _text(obj: JSON, key: str, what: str) -> str:
    value = obj.get(key)
    if not isinstance(value, str) or not value or value != value.strip():
        raise RehearsalError(f"{what}.{key}: expected a non-empty trimmed string")
    return value


def _num(obj: JSON, key: str, what: str, low: float, high: float) -> float:
    value = obj.get(key)
    if not _finite(value) or not low <= float(value) <= high:  # type: ignore[arg-type]
        raise RehearsalError(f"{what}.{key}: expected a number in [{low}, {high}]")
    return float(value)  # type: ignore[arg-type]


def _intval(obj: JSON, key: str, what: str, low: int, high: int) -> int:
    value = obj.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise RehearsalError(f"{what}.{key}: expected a whole number in [{low}, {high}]")
    return value


def _keys(obj: JSON, what: str, keys: set[str] | frozenset[str]) -> None:
    if set(obj) != set(keys):
        raise RehearsalError(f"{what}: expected keys {sorted(keys)}, got {sorted(obj)}")


def _safe_path(path: Path, what: str) -> Path:
    full = Path(path).absolute()
    if ".." in full.parts:
        raise RehearsalError(f"{what}: path must not contain '..'")
    for part in (full, *full.parents):
        if part.is_symlink():
            raise RehearsalError(f"{what}: path component {part} is a symbolic link")
    return full


def validate_layer_url(url: str, allowed_hosts: Sequence[str]) -> str:
    if url != OFFICIAL_LAYER_URL or tuple(allowed_hosts) != (OFFICIAL_HOST,):
        raise RehearsalError("only the official City layer 167 on the City host is allowed")
    parts = urlsplit(url)
    if parts.query or parts.fragment or parts.netloc != parts.hostname or parts.scheme != "https":
        raise RehearsalError("layer URL must have no query, fragment, port or credentials")
    return url


def load_rehearsal_config(path: Path) -> RehearsalConfig:
    raw = _read_file(_safe_path(Path(path), "config"), 1_000_000)
    try:
        root = _obj(parse_json_strict(raw.decode("utf-8")), "rehearsal config")
    except (UnicodeDecodeError, ConfigError) as exc:
        raise RehearsalError(f"rehearsal config: {exc}") from exc
    _keys(root, "config", {"schema", "name", "layer_url", "allowed_hosts", "where", "fields", "native_crs",
                           "target_crs", "page_size", "limits", "status_map", "licence", "region_envelope"})
    if root["schema"] != CONFIG_SCHEMA:
        raise RehearsalError(f"config.schema must be {CONFIG_SCHEMA}")
    hosts = root["allowed_hosts"]
    if hosts != [OFFICIAL_HOST]:
        raise RehearsalError("config.allowed_hosts must list only the official City host")
    fields = _obj(root["fields"], "config.fields")
    if fields != OFFICIAL_FIELDS:
        raise RehearsalError("config.fields must be exactly PROJECT_NO, DESCRIPTION and STATUS")
    limits = _obj(root["limits"], "config.limits")
    _keys(limits, "config.limits", {"timeout_seconds", "max_retries", "max_records", "max_response_bytes"})
    status_map: dict[str, Status] = {}
    items = root["status_map"]
    if not isinstance(items, list) or not items:
        raise RehearsalError("config.status_map must be a non-empty list")
    for item in items:
        rule = _obj(item, "config.status_map[]")
        _keys(rule, "config.status_map[]", {"raw", "status"})
        raw_status, status = _text(rule, "raw", "status_map"), rule["status"]
        mapped = next((s for s in STATUSES[:3] if s == status), None)
        if mapped is None or raw_status in status_map:
            raise RehearsalError(f"status_map: invalid or duplicate rule for {raw_status!r}")
        if "review" in raw_status.casefold() and mapped != "review_unknown":
            raise RehearsalError("status_map: review statuses must map to review_unknown, never approved")
        status_map[raw_status] = mapped
    licence = _obj(root["licence"], "config.licence")
    _keys(licence, "config.licence", {"name", "url", "attribution"})
    if licence["url"] != OFFICIAL_LICENCE_URL:
        raise RehearsalError("config.licence.url must be the official City open-data licence page")
    env = root["region_envelope"]
    if not isinstance(env, list) or len(env) != 4 or not all(_finite(v) for v in env):
        raise RehearsalError("config.region_envelope must be four finite numbers")
    if root["native_crs"] != NATIVE_CRS or root["target_crs"] != TARGET_CRS:
        raise RehearsalError(f"config CRS must be {NATIVE_CRS} to {TARGET_CRS}")
    try:
        where = validate_where(_text(root, "where", "config"))
    except ConfigError as exc:
        raise RehearsalError(str(exc)) from exc
    cfg = RehearsalConfig(
        name=_text(root, "name", "config"), layer_url=_text(root, "layer_url", "config"),
        allowed_hosts=(OFFICIAL_HOST,), where=where, id_field=OFFICIAL_FIELDS["project_id"],
        title_field=OFFICIAL_FIELDS["title"], status_field=OFFICIAL_FIELDS["status"], native_crs=NATIVE_CRS,
        target_crs=TARGET_CRS, page_size=_intval(root, "page_size", "config", 1, MAX_PAGE_SIZE),
        timeout_seconds=_num(limits, "timeout_seconds", "limits", 1, 20),
        max_retries=_intval(limits, "max_retries", "limits", 0, 2),
        max_records=_intval(limits, "max_records", "limits", 1, MAX_RECORDS),
        max_response_bytes=_intval(limits, "max_response_bytes", "limits", 1, MAX_RESPONSE_BYTES),
        status_map=status_map, licence_name=_text(licence, "name", "licence"), licence_url=OFFICIAL_LICENCE_URL,
        attribution=_text(licence, "attribution", "licence"), region_envelope=Envelope(*(float(v) for v in env)),
    )
    validate_layer_url(cfg.layer_url, cfg.allowed_hosts)
    return cfg


def _declared_epsg(coll: JSON, what: str) -> int:
    crs = coll.get("crs")
    props = crs.get("properties") if isinstance(crs, dict) else None
    name = props.get("name") if isinstance(props, dict) else None
    if not isinstance(name, str):
        raise RehearsalError(f"{what}: GeoJSON does not declare its CRS; metre coordinates are never assumed degrees")
    return _epsg(name)


def _check_metadata(meta: object, cfg: RehearsalConfig) -> JSON:
    facts = _facts(meta, cfg.out_fields)
    info = _obj(meta, "metadata.json")
    if info.get("geometryType") != "esriGeometryPolygon":
        raise RehearsalError("metadata.json: layer geometry is not polygon")
    adv = info.get("advancedQueryCapabilities")
    if not (isinstance(adv, dict) and adv.get("supportsPagination") is True and adv.get("supportsOrderBy") is True):
        raise RehearsalError("metadata.json: layer does not support ordered pagination")
    extent = info.get("extent")
    refs = [info.get("sourceSpatialReference"), extent.get("spatialReference") if isinstance(extent, dict) else None]
    wkids = {r.get(k) for r in refs if isinstance(r, dict) for k in ("wkid", "latestWkid")}
    if cfg.native_epsg not in wkids:
        raise RehearsalError(f"metadata.json: layer spatial reference is not {cfg.native_crs}")
    facts["page_size"] = min(cfg.page_size, int(facts["max_record_count"]), MAX_PAGE_SIZE)
    return facts


def _page_params(where: str, oid: str, fields: Sequence[str], offset: int, size: int, out_sr: int) -> dict[str, str]:
    return {"where": where, "outFields": ",".join([oid, *fields]), "orderByFields": f"{oid} ASC",
            "resultOffset": str(offset), "resultRecordCount": str(size), "returnGeometry": "true",
            "outSR": str(out_sr), "f": "geojson"}


def _check_page(data: object, name: str, oid: str, fields: Sequence[str], expected: list[int], last: bool,
                epsg: int) -> list[JSON]:
    coll = _obj(data, name)
    if _declared_epsg(coll, name) != epsg:
        raise RehearsalError(f"{name}: declared CRS is not EPSG:{epsg}")
    got, exceeded = _features(coll, name, oid, {*fields, oid})
    order = [int(f["id"]) for f in got]
    _exact(order, expected, name)
    if order != expected:
        raise RehearsalError(f"{name}: records are not in stable ascending {oid} order")
    if exceeded and last:
        raise RehearsalError(f"{name}: transfer limit flagged on the final page; records may be truncated")
    return got


def _check_pair(data: object, oid: str, fields: Sequence[str], expected: list[int], count: int,
                size: int) -> list[JSON]:
    """Reconcile the paired outSR=4326 page against the first native page; return its features."""
    coll = _obj(data, CRS_CHECK_FILE)
    if "crs" in coll and _declared_epsg(coll, CRS_CHECK_FILE) != 4326:
        raise RehearsalError("the paired response does not declare EPSG:4326")
    got, exceeded = _features(coll, CRS_CHECK_FILE, oid, {*fields, oid})
    order = [int(f["id"]) for f in got]
    _exact(order, expected, CRS_CHECK_FILE)
    if order != expected:
        raise RehearsalError(f"{CRS_CHECK_FILE}: paired records are not in stable ascending {oid} order")
    if exceeded and count <= size:
        raise RehearsalError(f"{CRS_CHECK_FILE}: transfer limit flagged on a single-page paired response; "
                             "records may be truncated")
    return got


def _read_live(cfg: RehearsalConfig, session: requests.Session) -> tuple[dict[str, bytes], list[JSON]]:
    validate_layer_url(cfg.layer_url, cfg.allowed_hosts)
    reader = _Reader(cfg.layer_url, cfg.allowed_hosts, cfg.limits(), session)
    facts = _check_metadata(reader.get("", {"f": "json"}, "metadata.json"), cfg)
    oid, size = str(facts["object_id_field"]), int(facts["page_size"])
    count_q = {"where": cfg.where, "returnCountOnly": "true", "f": "json"}
    ids_q = {"where": cfg.where, "returnIdsOnly": "true", "f": "json"}
    count = _count(reader.get("query", count_q, "count.json"), "count.json", cfg.max_records)
    ids = _ids(reader.get("query", ids_q, "ids.json"), "ids.json", oid, count)
    for index, offset in enumerate(range(0, len(ids), size), start=1):
        name = f"pages/page-{index:04d}.geojson"
        params = _page_params(cfg.where, oid, cfg.out_fields, offset, size, cfg.native_epsg)
        data = reader.get("query", params, name)
        _check_page(data, name, oid, cfg.out_fields, ids[offset : offset + size], offset + size >= len(ids),
                    cfg.native_epsg)
    if ids:
        pair = reader.get("query", _page_params(cfg.where, oid, cfg.out_fields, 0, size, 4326), CRS_CHECK_FILE)
        _check_pair(pair, oid, cfg.out_fields, ids[:size], count, size)
    reader.get("query", count_q, "final-count.json")
    reader.get("query", ids_q, "final-ids.json")
    return reader.files, reader.log


def _source_bytes(files: Mapping[str, bytes]) -> int:
    return sum(len(data) for name, data in files.items() if name != "requests.json")


def _assemble_pages(files: Mapping[str, bytes], cfg: RehearsalConfig) -> tuple[list[JSON], JSON]:
    for name in _REQUIRED:
        if name not in files:
            raise RehearsalError(f"missing stored response {name}")
    if _source_bytes(files) > cfg.max_response_bytes:
        raise RehearsalError(f"raw source responses exceed {cfg.max_response_bytes} bytes in total")
    facts = _check_metadata(_parse(files["metadata.json"], "metadata.json"), cfg)
    oid, size = str(facts["object_id_field"]), int(facts["page_size"])
    count = _count(_parse(files["count.json"], "count.json"), "count.json", cfg.max_records)
    ids = _ids(_parse(files["ids.json"], "ids.json"), "ids.json", oid, count)
    pages = sorted(n for n in files if n.startswith("pages/"))
    if pages != [f"pages/page-{i:04d}.geojson" for i in range(1, math.ceil(count / size) + 1)]:
        raise RehearsalError(f"expected {math.ceil(count / size)} consecutively numbered pages of {size}")
    unknown = sorted(set(files) - {*_REQUIRED, *pages, "requests.json", CRS_CHECK_FILE})
    if unknown:
        raise RehearsalError(f"unexpected snapshot files: {', '.join(unknown)}")
    features: list[JSON] = []
    for i, name in enumerate(pages):
        features += _check_page(_parse(files[name], name), name, oid, cfg.out_fields, ids[i * size : (i + 1) * size],
                                i == len(pages) - 1, cfg.native_epsg)
    _exact([int(f["id"]) for f in features], ids, "pages")
    if count:
        if CRS_CHECK_FILE not in files:
            raise RehearsalError("the paired outSR=4326 response for page 1 is missing; the coordinate "
                                 "transformation cannot be cross-checked")
        _check_pair(_parse(files[CRS_CHECK_FILE], CRS_CHECK_FILE), oid, cfg.out_fields, ids[:size], count, size)
    elif CRS_CHECK_FILE in files:
        raise RehearsalError("a paired response is present for an empty query")
    final_count = _count(_parse(files["final-count.json"], "final-count"), "final-count.json", cfg.max_records)
    final_ids = _ids(_parse(files["final-ids.json"], "final-ids"), "final-ids.json", oid, final_count)
    if final_count != count or final_ids != ids:
        raise RehearsalError("the layer changed during extraction (final count or IDs differ); extract again")
    facts.update(count=count, pages=len(pages))
    return features, facts


def _expected_request(name: str, where: str, oid: str, fields: Sequence[str], size: int,
                      native: int) -> tuple[str, dict[str, str]]:
    query = OFFICIAL_LAYER_URL + "/query"
    if name == "metadata.json":
        return OFFICIAL_LAYER_URL, {"f": "json"}
    if name in ("count.json", "final-count.json"):
        return query, {"where": where, "returnCountOnly": "true", "f": "json"}
    if name in ("ids.json", "final-ids.json"):
        return query, {"where": where, "returnIdsOnly": "true", "f": "json"}
    if name == CRS_CHECK_FILE:
        params = _page_params(where, oid, fields, 0, size, 4326)
    else:
        match = re.fullmatch(r"pages/page-(\d{4})\.geojson", name)
        if match is None:
            raise RehearsalError(f"request log names an unexpected file {name!r}")
        params = _page_params(where, oid, fields, (int(match.group(1)) - 1) * size, size, native)
    params.pop("returnGeometry")
    return query, params


def _check_requests(log: object, files: Mapping[str, bytes], cfg: RehearsalConfig, where: str,
                    facts: JSON) -> list[JSON]:
    if not isinstance(log, list):
        raise RehearsalError("request log must be a list")
    responses = {n for n in files if n != "requests.json"}
    oid, size = str(facts["object_id_field"]), int(facts["page_size"])
    seen: set[str] = set()
    for index, raw in enumerate(log):
        entry = _obj(raw, f"request {index}")
        name, url = entry.get("file"), entry.get("url")
        if not isinstance(name, str) or name not in responses or name in seen or not isinstance(url, str):
            raise RehearsalError(f"request {index}: file must name one response exactly once and include its URL")
        stamp = entry.get("retrieved_at", entry.get("requested_at"))
        if _utc(stamp) is None:
            raise RehearsalError(f"request {index}: missing UTC retrieved_at or requested_at")
        if entry.get("sha256") != _sha(files[name]) or entry.get("bytes") != len(files[name]):
            raise RehearsalError(f"request {index}: hash or byte count does not match {name}")
        base, expected = _expected_request(name, where, oid, cfg.out_fields, size, cfg.native_epsg)
        parts = urlsplit(url)
        if f"{parts.scheme}://{parts.netloc}{parts.path}" != base or parts.fragment:
            raise RehearsalError(f"request {index}: URL is not the expected official endpoint for {name}")
        try:
            params = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True)
        except ValueError as exc:
            raise RehearsalError(f"request {index}: malformed query string") from exc
        if any(len(v) != 1 for v in params.values()):
            raise RehearsalError(f"request {index}: repeated query parameters")
        flat = {k: v[0] for k, v in params.items()}
        if flat.pop("returnGeometry", "true") != "true" or flat != expected:
            raise RehearsalError(f"request {index}: parameters do not match the reconciled query for {name}")
        seen.add(name)
    if seen != responses:
        raise RehearsalError(f"responses without a logged request: {', '.join(sorted(responses - seen))}")
    return list(log)


def _write_tree(out: Path, files: Mapping[str, bytes], manifest_name: str, manifest: JSON) -> JSON:
    out = _safe_path(out, "output")
    if out.exists() or out.is_symlink():
        raise RehearsalError(f"{out}: already exists; outputs are never overwritten")
    out.parent.mkdir(parents=True, exist_ok=True)
    _safe_path(out.parent, "output parent")
    temp = Path(tempfile.mkdtemp(prefix=f".{out.name}.tmp-", dir=out.parent))
    try:
        hashes: dict[str, str] = {}
        for name, data in sorted(files.items()):
            target = temp / _safe_rel(name)
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("xb") as handle:
                handle.write(data)
            hashes[name] = _sha(data)
        full = {**manifest, "files": hashes}
        with (temp / manifest_name).open("xb") as handle:
            handle.write(canonical_json(full) + b"\n")
        if out.exists() or out.is_symlink():
            raise RehearsalError(f"{out}: appeared during write; refusing to overwrite")
        temp.rename(out)
    except BaseException:
        shutil.rmtree(temp, ignore_errors=True)
        raise
    return full


def _read_tree(root: Path, manifest_name: str, schema: str, limit: int) -> tuple[JSON, dict[str, bytes], str]:
    root = _safe_path(root, "input")
    if not root.is_dir():
        raise RehearsalError(f"{root}: must be a real directory")
    raw = _read_file(root / manifest_name, 10_000_000)
    manifest = _obj(_parse(raw, manifest_name), manifest_name)
    listed = _obj(manifest.get("files"), f"{manifest_name} files")
    if manifest.get("schema") != schema or _tree(root) - {manifest_name} != set(listed):
        raise RehearsalError(f"{root}: schema or file set differs from {manifest_name} (missing, extra or tampered)")
    files: dict[str, bytes] = {}
    for name, digest in listed.items():
        files[name] = _read_file(root / _safe_rel(name), limit)
        if _sha(files[name]) != digest:
            raise RehearsalError(f"{name}: checksum mismatch (tampered)")
    return manifest, files, _sha(raw)


def _check_scope(scope: object, where: str, cfg: RehearsalConfig) -> str:
    if scope not in SCOPES:
        raise RehearsalError("scope must be service_sample or complete_service")
    if where != cfg.where and scope != "service_sample":
        raise RehearsalError("a bounded query must be labelled service_sample, never complete_service")
    try:
        validate_where(where)
    except ConfigError as exc:
        raise RehearsalError(str(exc)) from exc
    return str(scope)


def _read_fixture(root: Path, cfg: RehearsalConfig) -> tuple[dict[str, bytes], list[JSON], JSON]:
    root = _safe_path(root, "fixtures")
    raw = _read_file(root / CAPTURE_MANIFEST, 1_000_000)
    cap = _obj(_parse(raw, CAPTURE_MANIFEST), CAPTURE_MANIFEST)
    _keys(cap, CAPTURE_MANIFEST, {"schema", "synthetic", "scope", "layer_url", "where", "captured_at", "retrieved_at",
                                  "description", "complete_service_count", "requests", "files"})
    listed, where, complete = _obj(cap["files"], "capture files"), cap["where"], cap["complete_service_count"]
    if cap["schema"] != CAPTURE_SCHEMA or not isinstance(cap["synthetic"], bool) or cap["layer_url"] != cfg.layer_url:
        raise RehearsalError(f"{CAPTURE_MANIFEST}: requires schema {CAPTURE_SCHEMA}, synthetic and the official layer")
    if not isinstance(where, str) or _utc(cap["retrieved_at"]) is None or "requests.json" in listed:
        raise RehearsalError(f"{CAPTURE_MANIFEST}: needs where, UTC retrieved_at, and requests in the manifest")
    if complete is not None and (isinstance(complete, bool) or not isinstance(complete, int) or complete < 0):
        raise RehearsalError(f"{CAPTURE_MANIFEST}: complete_service_count must be a whole number or null")
    scope = _check_scope(cap["scope"], where, cfg)
    if _tree(root) - {CAPTURE_MANIFEST} != set(listed):
        raise RehearsalError("fixture files do not exactly match the capture manifest")
    files: dict[str, bytes] = {}
    for name, digest in listed.items():
        files[name] = _read_file(root / _safe_rel(name), cfg.max_response_bytes)
        if _sha(files[name]) != digest:
            raise RehearsalError(f"fixture {name}: hash mismatch")
    meta = {k: cap[k] for k in ("captured_at", "retrieved_at", "description", "where", "synthetic",
                                "complete_service_count")}
    return files, cap["requests"], {**meta, "scope": scope, "manifest_sha256": _sha(raw)}


def _check_snapshot_manifest(manifest: JSON, cfg: RehearsalConfig, facts: JSON) -> None:
    _keys(manifest, SNAPSHOT_MANIFEST, _SNAPSHOT_KEYS)
    query = _obj(manifest["query"], "snapshot query")
    where, oid = query.get("where"), str(facts["object_id_field"])
    if not isinstance(where, str):
        raise RehearsalError("snapshot query.where missing")
    scope = _check_scope(query.get("scope"), where, cfg)
    expected = {"layer_url": cfg.layer_url, "where": where, "configured_where": cfg.where, "scope": scope,
                "out_fields": [oid, *cfg.out_fields], "out_sr": cfg.native_epsg, "order_by": f"{oid} ASC",
                "page_size": facts["page_size"]}
    counts = {"initial": facts["count"], "final": facts["count"], "pages": facts["pages"]}
    if query != expected or manifest["counts"] != counts or manifest["object_id_field"] != oid:
        raise RehearsalError("snapshot manifest query, counts or object ID field disagree with the responses")
    origin, synthetic, complete = manifest["origin"], manifest["synthetic"], manifest["complete_service_count"]
    if origin not in ("live", "fixture") or not isinstance(synthetic, bool) or _utc(manifest["retrieved_at"]) is None:
        raise RehearsalError("snapshot manifest origin, synthetic flag or retrieved_at invalid")
    if origin == "live" and (synthetic or scope != "complete_service" or manifest["capture"] is not None):
        raise RehearsalError("a live snapshot must be a complete, non-synthetic service read")
    if scope == "complete_service" and complete != facts["count"]:
        raise RehearsalError("complete_service_count must equal the reconciled count")
    if complete is not None and (isinstance(complete, bool) or not isinstance(complete, int) or
                                 complete < facts["count"]):
        raise RehearsalError("complete_service_count must be null or at least the sample count")
    if manifest["licence"] != cfg.licence() or manifest["object_id_use"] != OID_NOTE:
        raise RehearsalError("snapshot licence or object ID note differs from configuration")


def extract(cfg: RehearsalConfig, out: Path, from_fixtures: Path | None = None,
            session: requests.Session | None = None) -> JSON:
    out = _safe_path(Path(out), "output")
    if out.exists() or out.is_symlink():
        raise RehearsalError(f"{out}: already exists; snapshots are never overwritten")
    capture: JSON | None = None
    if from_fixtures is None:
        active = session or _session()
        try:
            retrieved_at = _now()
            files, log = _read_live(cfg, active)
        finally:
            if session is None:
                active.close()
        where, scope, synthetic = cfg.where, "complete_service", False
    else:
        files, log, capture = _read_fixture(Path(from_fixtures), cfg)
        retrieved_at, where, scope = str(capture["retrieved_at"]), str(capture["where"]), str(capture["scope"])
        synthetic = bool(capture["synthetic"])
    _, facts = _assemble_pages(files, cfg)
    log = _check_requests(log, files, cfg, where, facts)
    files["requests.json"] = canonical_json(log) + b"\n"
    oid = str(facts["object_id_field"])
    complete = facts["count"] if scope == "complete_service" else (capture or {}).get("complete_service_count")
    manifest: JSON = {
        "schema": SNAPSHOT_SCHEMA, "origin": "live" if capture is None else "fixture", "retrieved_at": retrieved_at,
        "query": {"layer_url": cfg.layer_url, "where": where, "configured_where": cfg.where, "scope": scope,
                  "out_fields": [oid, *cfg.out_fields], "out_sr": cfg.native_epsg, "order_by": f"{oid} ASC",
                  "page_size": facts["page_size"]},
        "counts": {"initial": facts["count"], "final": facts["count"], "pages": facts["pages"]},
        "complete_service_count": complete, "object_id_field": oid, "object_id_use": OID_NOTE,
        "snapshot_isolation": ISOLATION_NOTE, "licence": cfg.licence(), "synthetic": synthetic, "capture": capture,
    }
    _check_snapshot_manifest({**manifest, "files": {}}, cfg, facts)
    return _write_tree(out, files, SNAPSHOT_MANIFEST, manifest)


def load_rehearsal_snapshot(snapshot: Path, cfg: RehearsalConfig) -> RehearsalSnapshot:
    manifest, files, digest = _read_tree(Path(snapshot), SNAPSHOT_MANIFEST, SNAPSHOT_SCHEMA, cfg.max_response_bytes)
    if "requests.json" not in files:
        raise RehearsalError("snapshot has no requests.json")
    features, facts = _assemble_pages(files, cfg)
    _check_snapshot_manifest(manifest, cfg, facts)
    query = _obj(manifest["query"], "snapshot query")
    log = _check_requests(_parse(files["requests.json"], "requests.json"), files, cfg, str(query["where"]), facts)
    pair = _obj(_parse(files[CRS_CHECK_FILE], CRS_CHECK_FILE), CRS_CHECK_FILE) if CRS_CHECK_FILE in files else None
    return RehearsalSnapshot(manifest, digest, tuple(features), facts, tuple(log), pair)


def make_transformer(cfg: RehearsalConfig) -> Transformer:
    _network.set_network_enabled(active=False)
    if _network.is_network_enabled():
        raise RehearsalError("PROJ network access could not be disabled")
    return Transformer.from_crs(cfg.native_crs, cfg.target_crs, always_xy=True)


def _native_error(geom: object) -> str | None:
    if not isinstance(geom, dict):
        return "the record has no geometry"
    kind, coords = geom.get("type"), geom.get("coordinates")
    if kind not in ("Polygon", "MultiPolygon"):
        return f"the geometry type {kind!r} is not a polygon"
    polygons = [coords] if kind == "Polygon" else coords
    if not isinstance(polygons, list) or not polygons:
        return "the geometry is empty"
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            return "a polygon is empty"
        for ring in polygon:
            if not isinstance(ring, list) or len(ring) < 4:
                return "a ring has fewer than four positions"
            if not all(isinstance(p, list) and len(p) >= 2 and _finite(p[0]) and _finite(p[1]) for p in ring):
                return "a coordinate is missing or not a finite number"
            if ring[0][:2] != ring[-1][:2]:
                return "a ring is not closed"
    try:
        shp = shape(geom)
    except (ValueError, TypeError, GEOSException) as exc:
        return f"the geometry cannot be read ({exc})"
    if shp.is_empty or not shp.is_valid:
        return f"the native geometry is invalid ({explain_validity(shp)}); it is never repaired"
    return None


def _transform(geom: JSON, t: Transformer) -> JSON:
    def ring(points: list[list[float]]) -> list[list[float]]:
        lon, lat = t.transform([p[0] for p in points], [p[1] for p in points], errcheck=True)
        return [[round(float(x), 9), round(float(y), 9)] for x, y in zip(lon, lat, strict=True)]

    polygons = [geom["coordinates"]] if geom["type"] == "Polygon" else geom["coordinates"]
    out = [[ring(r) for r in polygon] for polygon in polygons]
    return {"type": geom["type"], "coordinates": out[0] if geom["type"] == "Polygon" else out}


def _positions(geom: object) -> list[list[float]]:
    if not isinstance(geom, dict):
        return []
    kind, coords = geom.get("type"), geom.get("coordinates")
    if kind not in ("Polygon", "MultiPolygon") or not isinstance(coords, list):
        return []
    out: list[list[float]] = []
    for polygon in [coords] if kind == "Polygon" else coords:
        for ring in polygon if isinstance(polygon, list) else []:
            for p in ring if isinstance(ring, list) else []:
                if not (isinstance(p, list) and len(p) >= 2 and _finite(p[0]) and _finite(p[1])):
                    return []
                out.append([float(p[0]), float(p[1])])
    return out


def check_crs_pair(native: Sequence[JSON], wgs84: Sequence[JSON], t: Transformer) -> list[str]:
    """Plain-language errors comparing pyproj output with the paired genuine server WGS84 response."""
    errors: list[str] = [] if wgs84 else ["the paired WGS84 response contains no features"]
    by_id = {f["id"]: f for f in native}
    for feat in wgs84:
        src = by_id.get(feat["id"])
        a, b = _positions(src["geometry"] if src else None), _positions(feat["geometry"])
        if not a or len(a) != len(b):
            errors.append(f"OBJECTID {feat['id']}: paired geometries cannot be compared vertex by vertex")
            continue
        lon, lat = t.transform([p[0] for p in a], [p[1] for p in a], errcheck=True)
        bx, by = t.transform(lon, lat, direction=TransformDirection.INVERSE, errcheck=True)
        worst = max(max(abs(x - p[0]), abs(y - p[1])) for x, y, p in zip(lon, lat, b, strict=True))
        trip = max(math.hypot(x - p[0], y - p[1]) for x, y, p in zip(bx, by, a, strict=True))
        if worst > PAIR_TOLERANCE_DEGREES:
            errors.append(f"OBJECTID {feat['id']}: transformed coordinates differ from the server by {worst:.9f} deg")
        if trip > ROUNDTRIP_TOLERANCE_METRES:
            errors.append(f"OBJECTID {feat['id']}: inverse round trip drifts {trip:.4f} m "
                          f"(limit {ROUNDTRIP_TOLERANCE_METRES} m)")
    return errors


TransformerFactory = Callable[[RehearsalConfig], Transformer]


class _Found:
    def __init__(self) -> None:
        self.items: list[Finding] = []

    def add(self, severity: Severity, code: str, message: str, record: str | None = None,
            source: str = SOURCE_ID) -> None:
        self.items.append(Finding(severity, code, message, source, record))


def _cell(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, float):
        return repr(value)
    return str(value)


def _csv_bytes(columns: Sequence[str], rows: Sequence[JSON]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_cell(row.get(c)) for c in columns])
    return buffer.getvalue().encode("utf-8")


def _too_long(row: JSON, table: str) -> list[str]:
    return [f"{m.name} is longer than {m.length} characters" for m in FIELD_MAPPING
            if m.table == table and m.length is not None and isinstance(row.get(m.name), str)
            and len(row[m.name]) > m.length]


def _opt_int(value: str, problems: list[str]) -> int | None:
    text = value.strip()
    if not text:
        return None
    if not text.isdigit() or len(text) > 4:
        problems.append("storeys must be a whole number of zero or more")
        return None
    return int(text)


def _opt_float(value: str, problems: list[str]) -> float | None:
    text = value.strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        number = math.nan
    if not math.isfinite(number) or number < 0:
        problems.append("height_m must be a finite number of zero or more")
        return None
    return number


def _buildings(found: _Found, data: bytes | None, project_ids: set[str]) -> list[JSON]:
    src = "buildings_csv"
    if data is None:
        found.add("warning", "no_buildings_supplied", "No separately sourced building rows were supplied, so "
                  "buildings.csv contains headers only. Buildings are never derived from application boundaries.",
                  None, src)
        return []
    try:
        rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"), newline=""), strict=True))
    except (UnicodeDecodeError, csv.Error) as exc:
        found.add("blocking", "unreadable_buildings", f"The building CSV is not readable UTF-8 CSV ({exc}).", None, src)
        return []
    header: list[str] = rows[0] if rows else []
    required = {"building_id", "project_id", "source_url"}
    if len(set(header)) != len(header) or not required <= set(header) or not set(header) <= set(BUILDING_COLUMNS):
        found.add("blocking", "invalid_building_header", "The building CSV header must contain building_id, "
                  f"project_id and source_url, and only these columns: {', '.join(BUILDING_COLUMNS)}.", None, src)
        return []
    seen: set[str] = set()
    out: list[JSON] = []
    for line, raw in enumerate(rows[1:], start=2):
        if len(raw) != len(header):
            found.add("blocking", "invalid_building", f"Line {line} has {len(raw)} values; expected {len(header)}.",
                      f"line {line}", src)
            continue
        rec: dict[str, str] = dict(zip(header, raw, strict=True))
        bid, pid = rec["building_id"].strip(), rec["project_id"].strip()
        problems: list[str] = []
        if not bid:
            problems.append("building_id is empty")
        elif bid in seen:
            problems.append(f"building_id {bid!r} appears more than once")
        if pid not in project_ids:
            problems.append(f"project_id {pid!r} does not match any normalized project")
        try:
            url = validate_https_url(rec["source_url"].strip())
        except ConfigError:
            problems.append("source_url is not a public HTTPS URL")
            url = ""
        row: JSON = {
            "building_id": bid, "project_id": pid, "source_url": url,
            "building_name": rec.get("building_name", "").strip() or None,
            "storeys": _opt_int(rec.get("storeys", ""), problems),
            "height_m": _opt_float(rec.get("height_m", ""), problems),
            "building_use": rec.get("building_use", "").strip() or None,
        }
        problems += _too_long(row, "buildings")
        if bid:
            seen.add(bid)
        if problems:
            found.add("blocking", "invalid_building", f"Line {line}: " + "; ".join(problems) + ".",
                      bid or f"line {line}", src)
            continue
        out.append(row)
    return sorted(out, key=lambda r: str(r["building_id"]))


def _project(found: _Found, cfg: RehearsalConfig, t: Transformer, pid: str, feat: JSON,
             retrieved: str | None) -> tuple[JSON | None, JSON | None]:
    props = feat["properties"]
    nested = sorted(k for k, v in props.items() if isinstance(v, dict | list))
    if nested:
        found.add("blocking", "nested_property", f"Project {pid} has nested attributes: {', '.join(nested)}.", pid)
        return None, None
    title, raw = props.get(cfg.title_field), props.get(cfg.status_field)
    problems: list[str] = []
    title_text: str | None = None
    if isinstance(title, str):
        title_text = title.strip() or None
    elif title is not None:
        problems.append(f"{cfg.title_field} is neither text nor null")
    status_raw = raw if isinstance(raw, str) else ""
    if not status_raw:
        problems.append(f"{cfg.status_field} is empty")
    native = _native_error(feat.get("geometry"))
    if native:
        problems.append(native)
    if problems:
        found.add("blocking", "invalid_project", f"Project {pid}: " + "; ".join(problems) + ".", pid)
        return None, None
    if retrieved is None:
        return None, None
    try:
        wgs = _transform(feat["geometry"], t)
    except ProjError as exc:
        found.add("blocking", "transform_failed", f"Project {pid}: coordinate transformation failed ({exc}).", pid)
        return None, None
    shp, error = _polygonal(wgs)
    if shp is None:
        found.add("blocking", "invalid_project", f"Project {pid}: after transformation to WGS84, {error}.", pid)
        return None, None
    status = cfg.status_map.get(status_raw)
    if status is None:
        found.add("warning", "unrecognized_status", f"Project {pid}: status {status_raw!r} is not recognized, so it "
                  "is recorded as unknown. Approval or construction is never inferred.", pid)
        status = "unknown"
    point = shp.representative_point()
    lon, lat = round(float(point.x), 9), round(float(point.y), 9)
    if not cfg.region_envelope.contains(lon, lat):
        found.add("warning", "outside_region", f"Project {pid}: its interior point lies outside the regional "
                  "sanity envelope (a sanity check, not a boundary).", pid)
    row: JSON = {"project_id": pid, "title": title_text, "status": status, "status_raw": status_raw,
                 "longitude": lon, "latitude": lat, "location_basis": "application_interior_point",
                 "source_url": cfg.layer_url, "retrieved_at": retrieved, "licence": cfg.attribution}
    too_long = _too_long(row, "projects")
    if too_long:
        found.add("blocking", "field_too_long", f"Project {pid}: " + "; ".join(too_long) + ".", pid)
        return None, None
    if title_text is None:
        found.add("warning", "missing_description", f"Project {pid}: the City record has no {cfg.title_field}, so its "
                  "title is left empty (null). No placeholder is invented; project_id remains the identifier.", pid)
    return row, wgs


def normalize_snapshot_data(cfg: RehearsalConfig, snap: RehearsalSnapshot, buildings_csv: bytes | None = None,
                            buildings_name: str | None = None,
                            transformer: Transformer | None = None) -> NormalizedResult:
    """Pure normalization. Blocking problems become findings, and the affected records are left out of the tables."""
    found = _Found()
    t = transformer or make_transformer(cfg)
    manifest, oid = snap.manifest, str(snap.facts["object_id_field"])
    query = _obj(manifest.get("query"), "snapshot query")
    if query.get("scope") == "service_sample":
        found.add("warning", "service_sample", f"This snapshot is a bounded sample of the service (where "
                  f"{query.get('where')}), not a complete service export. Counts and changes describe the sample only.")
    if manifest.get("synthetic") is True:
        found.add("warning", "synthetic_input", "This snapshot is labelled synthetic test-only data.")
    retrieved = _utc(manifest.get("retrieved_at"))
    if retrieved is None:
        found.add("blocking", "missing_retrieved_at", "The snapshot has no valid UTC retrieval time.")
    if snap.facts.get("source_updated_at") is None:
        found.add("warning", "missing_source_updated_at", "The layer metadata gives no last-edit time.")
    if snap.crs_check is None:
        if snap.features:
            found.add("blocking", "crs_pair_missing", "No paired server WGS84 response is present, so the coordinate "
                      "transformation cannot be cross-checked against the service; the snapshot is blocked.")
    else:
        try:
            size = int(snap.facts["page_size"])
            first = list(snap.features[:size])
            pair = _check_pair(snap.crs_check, oid, cfg.out_fields, [int(f["id"]) for f in first],
                               len(snap.features), size)
            errors = check_crs_pair(first, pair, t)
        except (AcquisitionError, ProjError) as exc:
            errors = [str(exc)]
        for error in errors:
            found.add("blocking", "crs_check_failed", f"Coordinate cross-check failed: {error}.")
    keyed: list[tuple[str, JSON]] = []
    for feat in snap.features:
        locator = f"OBJECTID {feat['id']} (locator only)"
        raw_id = feat["properties"].get(cfg.id_field)
        if not isinstance(raw_id, str) or not raw_id.strip():
            found.add("blocking", "missing_project_id", f"{locator}: {cfg.id_field} is empty. OBJECTID is never "
                      "used as a project ID.", locator)
            continue
        keyed.append((raw_id.strip(), feat))
    counts = Counter(pid for pid, _ in keyed)
    for pid, number in sorted(counts.items()):
        if number > 1:
            found.add("blocking", "duplicate_project_id", f"{cfg.id_field} {pid!r} appears {number} times. Projects "
                      "need a unique stable ID, and OBJECTID is never used instead.", pid)
    rows: list[JSON] = []
    features: list[JSON] = []
    for pid, feat in keyed:
        if counts[pid] > 1:
            continue
        row, geom = _project(found, cfg, t, pid, feat, retrieved)
        if row is not None and geom is not None:
            rows.append(row)
            features.append({"type": "Feature", "geometry": geom, "properties": row})
    rows.sort(key=lambda r: str(r["project_id"]))
    features.sort(key=lambda f: str(f["properties"]["project_id"]))
    buildings = _buildings(found, buildings_csv, {str(r["project_id"]) for r in rows})
    provenance: JSON = {
        "schema": SOURCE_MANIFEST_SCHEMA, "source_id": SOURCE_ID, "layer_url": cfg.layer_url,
        "title": snap.facts.get("title"), "field_definitions": snap.facts.get("field_definitions"),
        "max_record_count": snap.facts.get("max_record_count"),
        "source_updated_at": snap.facts.get("source_updated_at"), "query": query,
        "counts": manifest.get("counts"), "complete_service_count": manifest.get("complete_service_count"),
        "origin": manifest.get("origin"), "synthetic": manifest.get("synthetic"), "capture": manifest.get("capture"),
        "retrieved_at": retrieved, "object_id_field": oid, "object_id_use": OID_NOTE,
        "snapshot_isolation": ISOLATION_NOTE, "snapshot_manifest_sha256": snap.manifest_sha256,
        "requests": list(snap.requests),
        "crs": {"source": cfg.native_crs, "target": cfg.target_crs, "always_xy": True, "network": False,
                "transformer": t.definition, "pyproj": pyproj.__version__, "proj": pyproj.proj_version_str},
        "licence": cfg.licence(),
        "buildings_input": None if buildings_csv is None else {
            "name": Path(buildings_name or "buildings.csv").name, "sha256": _sha(buildings_csv),
            "rows": len(buildings)},
        "notes": [FOOTPRINT_NOTE, "Buildings come only from supplied rows; none are derived from application "
                  "boundaries or descriptions."],
    }
    return NormalizedResult(tuple(rows), tuple(buildings), tuple(features), provenance, tuple(found.items))


def _bundle_counts(findings: Sequence[Finding], projects: int, buildings: int) -> JSON:
    return {"projects": projects, "buildings": buildings,
            "blocking": sum(1 for f in findings if f.severity == "blocking"),
            "warnings": sum(1 for f in findings if f.severity == "warning")}


def write_bundle(out: Path, result: NormalizedResult) -> JSON:
    files = result.files()
    if len(files["projects.geojson"]) >= MAX_GEOJSON_OUTPUT_BYTES:
        raise RehearsalError(f"projects.geojson must be below {MAX_GEOJSON_OUTPUT_BYTES} bytes")
    counts = _bundle_counts(result.findings, len(result.projects), len(result.buildings))
    return _write_tree(Path(out), files, BUNDLE_MANIFEST, {"schema": BUNDLE_SCHEMA, "ok": result.ok, "counts": counts})


def normalize(cfg: RehearsalConfig, snapshot: Path, out: Path, buildings: Path | None = None,
              factory: TransformerFactory = make_transformer) -> NormalizedResult:
    out = _safe_path(Path(out), "output")
    if out.exists() or out.is_symlink():
        raise RehearsalError(f"{out}: already exists; normalized bundles are never overwritten")
    snap = load_rehearsal_snapshot(Path(snapshot), cfg)
    path = None if buildings is None else _safe_path(Path(buildings), "buildings CSV")
    data = None if path is None else _read_file(path, cfg.max_response_bytes)
    result = normalize_snapshot_data(cfg, snap, data, None if path is None else path.name, factory(cfg))
    write_bundle(out, result)
    return result


def _read_csv(data: bytes, columns: Sequence[str], name: str) -> list[dict[str, str]]:
    try:
        rows = list(csv.reader(io.StringIO(data.decode("utf-8"), newline=""), strict=True))
    except (UnicodeDecodeError, csv.Error) as exc:
        raise RehearsalError(f"{name}: not readable UTF-8 CSV ({exc})") from exc
    header: list[str] = rows[0] if rows else []
    if header != list(columns):
        raise RehearsalError(f"{name}: header must be exactly {', '.join(columns)}")
    out: list[dict[str, str]] = []
    for line, row in enumerate(rows[1:], start=2):
        if len(row) != len(columns):
            raise RehearsalError(f"{name}: line {line} has {len(row)} values; expected {len(columns)}")
        out.append(dict(zip(columns, row, strict=True)))
    return out


_SEVERITIES: Final[tuple[Severity, ...]] = ("blocking", "warning", "exclusion")


def _finding(value: object) -> Finding:
    item = _obj(value, "findings.json entry")
    _keys(item, "findings.json entry", {"severity", "code", "message", "source_id", "record_id"})
    severity = next((s for s in _SEVERITIES if s == item["severity"]), None)
    code, message, src, rec = item["code"], item["message"], item["source_id"], item["record_id"]
    if severity is None or not isinstance(code, str) or not isinstance(message, str):
        raise RehearsalError("findings.json: malformed finding")
    if (src is not None and not isinstance(src, str)) or (rec is not None and not isinstance(rec, str)):
        raise RehearsalError("findings.json: source_id and record_id must be strings or null")
    return Finding(severity, code, message, src, rec)


def _check_string(value: object, name: str, what: str, *, trimmed: bool = True) -> str:
    limit = _LIMITS.get(name)
    if not isinstance(value, str) or not value or (trimmed and value != value.strip()):
        raise RehearsalError(f"{what}: {name} must be a non-empty trimmed string")
    if limit is not None and len(value) > limit:
        raise RehearsalError(f"{what}: {name} is longer than {limit} characters")
    return value


def _check_project_props(props: JSON, attribution: str, retrieved: str, what: str) -> tuple[float, float]:
    _keys(props, what, set(PROJECT_COLUMNS))
    for m in FIELD_MAPPING:
        if m.table != "projects":
            continue
        value = props[m.name]
        if value is None:
            if not m.nullable:
                raise RehearsalError(f"{what}: {m.name} must not be null")
            continue
        if m.arcgis_type == _S:
            _check_string(value, m.name, what, trimmed=m.name not in _UNTRIMMED)
        elif not isinstance(value, float) or not _finite(value):
            raise RehearsalError(f"{what}: {m.name} must be a finite number")
    lon, lat = float(props["longitude"]), float(props["latitude"])
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        raise RehearsalError(f"{what}: coordinates are outside the WGS84 range")
    if props["status"] not in STATUSES or props["location_basis"] != "application_interior_point":
        raise RehearsalError(f"{what}: status or location_basis is not an allowed value")
    if props["source_url"] != OFFICIAL_LAYER_URL or not _TIMESTAMP.fullmatch(props["retrieved_at"]):
        raise RehearsalError(f"{what}: source_url must be layer 167 and retrieved_at a UTC timestamp")
    if props["licence"] != attribution or props["retrieved_at"] != retrieved:
        raise RehearsalError(f"{what}: licence or retrieved_at disagrees with source-manifest.json")
    return lon, lat


def _check_building(row: dict[str, str], known: set[str], what: str) -> None:
    for name in ("building_id", "project_id", "source_url"):
        _check_string(row[name], name, what)
    if row["project_id"] not in known:
        raise RehearsalError(f"{what}: project_id has no matching project")
    try:
        validate_https_url(row["source_url"])
    except ConfigError as exc:
        raise RehearsalError(f"{what}: source_url is not HTTPS") from exc
    for name in ("building_name", "building_use"):
        if row[name]:
            _check_string(row[name], name, what)
    storeys, height = row["storeys"], row["height_m"]
    if storeys and (not storeys.isdigit() or len(storeys) > 4 or str(int(storeys)) != storeys):
        raise RehearsalError(f"{what}: storeys must be blank or a whole number")
    if height:
        try:
            number = float(height)
        except ValueError as exc:
            raise RehearsalError(f"{what}: height_m must be blank or a number") from exc
        if not math.isfinite(number) or number < 0 or repr(number) != height:
            raise RehearsalError(f"{what}: height_m must be blank or a canonical non-negative number")


def _whole_count(value: object, what: str) -> int:
    """Narrow one recorded count to a non-negative int, rejecting booleans, floats and other types."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RehearsalError(f"source-manifest.json: {what} is not a whole number; source counts must be equal "
                             "whole numbers with a feasible page count")
    return value


def _source_count(provenance: JSON) -> int:
    """Return the reconciled source record count after checking whole, equal, bounded counts and feasible pages."""
    counts, query = provenance.get("counts"), provenance.get("query")
    if not isinstance(counts, dict) or not isinstance(query, dict):
        raise RehearsalError("source-manifest.json: source counts and query are required")
    count = _whole_count(counts.get("initial"), "counts.initial")
    final = _whole_count(counts.get("final"), "counts.final")
    pages = _whole_count(counts.get("pages"), "counts.pages")
    size = _whole_count(query.get("page_size"), "query.page_size")
    if count > MAX_RECORDS:
        raise RehearsalError(f"source-manifest.json: source counts exceed the {MAX_RECORDS} record limit")
    if count != final or not 1 <= size <= MAX_PAGE_SIZE or pages != math.ceil(count / size):
        raise RehearsalError("source-manifest.json: source counts must be equal whole numbers with a feasible page "
                             "count")
    return count


def validate_bundle_files(files: Mapping[str, bytes], manifest: JSON) -> NormalizedBundle:
    """Pure revalidation of one normalized bundle: hashes, schemas, types, agreement, joins, counts and ok flag."""
    _keys(manifest, BUNDLE_MANIFEST, {"schema", "ok", "counts", "files"})
    listed = _obj(manifest["files"], "bundle files")
    if manifest["schema"] != BUNDLE_SCHEMA or set(files) != set(BUNDLE_FILES) or set(listed) != set(BUNDLE_FILES):
        raise RehearsalError(f"bundle must contain exactly: {', '.join(BUNDLE_FILES)}")
    if any(listed[name] != _sha(files[name]) for name in BUNDLE_FILES):
        raise RehearsalError("bundle file hashes do not match bundle-manifest.json")
    if files["field-mapping.json"] != _field_mapping_bytes():
        raise RehearsalError("field-mapping.json differs from the field mapping registry")
    provenance = _obj(_parse(files["source-manifest.json"], "source-manifest.json"), "source-manifest.json")
    licence, crs = provenance.get("licence"), provenance.get("crs")
    if provenance.get("schema") != SOURCE_MANIFEST_SCHEMA or provenance.get("layer_url") != OFFICIAL_LAYER_URL:
        raise RehearsalError("source-manifest.json: unsupported schema or layer")
    if not isinstance(licence, dict) or licence.get("url") != OFFICIAL_LICENCE_URL or not isinstance(crs, dict):
        raise RehearsalError("source-manifest.json: licence or CRS record missing")
    if crs.get("source") != NATIVE_CRS or crs.get("target") != TARGET_CRS or crs.get("always_xy") is not True:
        raise RehearsalError("source-manifest.json: CRS record must be EPSG:26910 to EPSG:4326 with always_xy")
    source_count = _source_count(provenance)
    attribution, retrieved = licence.get("attribution"), provenance.get("retrieved_at")
    raw_findings = _parse(files["findings.json"], "findings.json")
    if not isinstance(raw_findings, list):
        raise RehearsalError("findings.json: expected a list")
    findings = tuple(_finding(x) for x in raw_findings)
    blocked = any(f.severity == "blocking" for f in findings)
    projects = _read_csv(files["projects.csv"], PROJECT_COLUMNS, "projects.csv")
    buildings = _read_csv(files["buildings.csv"], BUILDING_COLUMNS, "buildings.csv")
    if len(projects) > source_count or (not blocked and len(projects) != source_count):
        raise RehearsalError("projects.csv: a blocker-free bundle must contain exactly the source count of projects "
                             "(records are never silently dropped), and no bundle may exceed it")
    if projects and (not isinstance(attribution, str) or not isinstance(retrieved, str)):
        raise RehearsalError("source-manifest.json: attribution and retrieved_at are required for project rows")
    ids = [r["project_id"] for r in projects]
    if ids != sorted(set(ids)):
        raise RehearsalError("projects.csv: project_id values must be unique and sorted")
    geo = _obj(_parse(files["projects.geojson"], "projects.geojson"), "projects.geojson")
    _keys(geo, "projects.geojson", {"type", "features"})
    feats = geo["features"]
    if geo["type"] != "FeatureCollection" or not isinstance(feats, list) or len(feats) != len(projects):
        raise RehearsalError("projects.geojson must be a WGS84 FeatureCollection matching projects.csv")
    for row, raw in zip(projects, feats, strict=True):
        what = f"project {row['project_id']!r}"
        feat = _obj(raw, what)
        _keys(feat, what, {"type", "geometry", "properties"})
        props = _obj(feat["properties"], what)
        lon, lat = _check_project_props(props, str(attribution), str(retrieved), what)
        shp, error = _polygonal(feat["geometry"])
        if feat["type"] != "Feature" or shp is None:
            raise RehearsalError(f"{what}: geometry is not a valid WGS84 polygon ({error})")
        point = shp.representative_point()
        if (round(float(point.x), 9), round(float(point.y), 9)) != (lon, lat):
            raise RehearsalError(f"{what}: longitude/latitude are not the geometry's interior point")
        if [_cell(props[c]) for c in PROJECT_COLUMNS] != [row[c] for c in PROJECT_COLUMNS]:
            raise RehearsalError(f"{what}: projects.csv and projects.geojson attributes differ")
    bids = [b["building_id"] for b in buildings]
    if bids != sorted(set(bids)):
        raise RehearsalError("buildings.csv: building_id values must be unique and sorted")
    known = set(ids)
    for b in buildings:
        _check_building(b, known, f"building {b['building_id']!r}")
    if manifest["counts"] != _bundle_counts(findings, len(projects), len(buildings)):
        raise RehearsalError("bundle-manifest.json counts disagree with the bundle contents")
    if manifest["ok"] is not (not blocked):
        raise RehearsalError("bundle-manifest.json ok flag disagrees with findings.json")
    return NormalizedBundle(manifest, dict(files), tuple(projects), tuple(buildings), geo, provenance, findings)


def load_bundle(path: Path) -> NormalizedBundle:
    """Read a normalized bundle tree (no symlinks, exact file set, hashes) and revalidate it."""
    manifest, files, _ = _read_tree(Path(path), BUNDLE_MANIFEST, BUNDLE_SCHEMA, MAX_GEOJSON_OUTPUT_BYTES)
    return validate_bundle_files(files, manifest)
