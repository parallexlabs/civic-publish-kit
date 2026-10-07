"""Legacy v1 normalization, blocking validation and release data model.

These pilot-area and local project provenance rules retain v1 behavior. They do not
define v2 rehearsal normalization or its separate building CSV support. Context: [7].

Contract for legacy store/reports/CLI:
- normalize_snapshot(snapshot, config) -> ValidationResult. Data problems never raise. An unreadable or
  tampered snapshot becomes a blocking `snapshot_unreadable` finding. normalize_loaded(loaded, config)
  accepts load_snapshot() output directly.
- ValidationResult.ok is False if any blocking finding exists. `validate` exits nonzero and `build` refuses.
- projects: GeoJSON Point Features sorted by project_id; properties are exactly PROJECT_FIELDS, flat scalars.
- sites: application site polygons, never building footprints, joined by project_id; SITE_DICTIONARY fields.
- pilot_areas: official plan polygons with unmodified source geometry and PILOT_AREA_DICTIONARY fields.
- provenance: {"snapshot", "sources", "boundary_disclosure", "area_precedence", "limitations"}.
- findings: Finding(severity blocking|warning|exclusion, code, message, source_id, record_id).
- FIELD_DICTIONARY, SITE_DICTIONARY and PILOT_AREA_DICTIONARY document all output fields and canonical types.

Legacy v1 rules:
- Missing or duplicate trimmed business IDs are checked for every project.
- Full validation applies to reviewed selected records; unselected records are explicit exclusions.
- Selected IDs absent from the source block.
- Validate every official plan geometry before union; never repair geometry.
- Assign representative points covered by official polygon unions in configured precedence order.
- Local project inputs have no dataset URL; the licence page is not one. Block with `missing_source_url`.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final, Literal, Protocol

from shapely import union_all
from shapely.errors import GEOSException
from shapely.geometry import Point, shape
from shapely.validation import explain_validity

from civic_publish_kit.acquire import AcquisitionError, load_snapshot
from civic_publish_kit.config import (
    AREA_BASES,
    LOCATION_BASES,
    NORMALIZED_STATUSES,
    PILOT_AREA_VALUES,
    PROJECT_FIELDS,
    Config,
    ConfigError,
    FieldMapping,
    Source,
    validate_https_url,
    validate_output_field_names,
)

JSON = dict[str, Any]
Severity = Literal["blocking", "warning", "exclusion"]
SAMPLE_NOTE: Final = (
    "This snapshot holds a captured sample, not the complete source. Counts, exclusions and pilot-area "
    "assignment reflect only the sampled features and are not authoritative membership."
)
LOCAL_URL_NOTE: Final = (
    "Local inputs have no configured HTTPS dataset URL, and the licence page is not a dataset URL, so "
    "records cannot be exported with honest source_url provenance. Configure the public ArcGIS layer "
    "instead; this specification does not support local dataset URLs."
)


class _Geom(Protocol):
    @property
    def is_empty(self) -> bool: ...
    @property
    def is_valid(self) -> bool: ...
    @property
    def x(self) -> float: ...
    @property
    def y(self) -> float: ...
    def representative_point(self) -> _Geom: ...
    def covers(self, other: object) -> bool: ...


@dataclass(frozen=True, slots=True)
class FieldSpec:
    name: str
    type: Literal["string", "number", "enum", "utc_timestamp"]
    required: bool
    description: str
    values: tuple[str, ...] = ()

    def to_dict(self) -> JSON:
        return {"name": self.name, "type": self.type, "required": self.required,
                "values": list(self.values), "description": self.description}


FIELD_DICTIONARY: Final[tuple[FieldSpec, ...]] = (
    FieldSpec("project_id", "string", True, "Business ID from the mapped field (PROJECT_NO), trimmed; never OBJECTID."),
    FieldSpec("title", "string", True, "Mapped title/description text, surrounding whitespace trimmed."),
    FieldSpec("status", "enum", True, "Normalized status from the explicit map; review statuses are unknown, "
              "never approved.", NORMALIZED_STATUSES),
    FieldSpec("status_raw", "string", True, "Exact original source status string."),
    FieldSpec("status_evidence_url", "string", False, "HTTPS URL from an explicitly mapped field, else null."),
    FieldSpec("pilot_area", "enum", True, "Pilot area whose official plan polygon union covers the point, by "
              "configured precedence.", PILOT_AREA_VALUES),
    FieldSpec("area_basis", "enum", True, "Basis for pilot_area.", AREA_BASES),
    FieldSpec("longitude", "number", True, "WGS84 longitude of the project location point."),
    FieldSpec("latitude", "number", True, "WGS84 latitude of the project location point."),
    FieldSpec("location_basis", "enum", True, "source_point, or site_interior_point (Shapely representative_point "
              "of the application site polygon).", LOCATION_BASES),
    FieldSpec("source_id", "string", True, "Configured source identifier linking to source-manifest.json."),
    FieldSpec("source_record_id", "string", True, "Original business identity value as published."),
    FieldSpec("source_url", "string", True, "HTTPS URL of the public source layer."),
    FieldSpec("retrieved_at", "utc_timestamp", True, "UTC time the source was retrieved."),
    FieldSpec("source_updated_at", "utc_timestamp", False, "Layer last-edit UTC time from metadata; null if absent."),
    FieldSpec("licence", "string", True, "Public-data licence attribution."),
)
SITE_DICTIONARY: Final[tuple[FieldSpec, ...]] = (
    FieldSpec("project_id", "string", True, "Joins to projects.project_id."),
    FieldSpec("source_id", "string", True, "Configured source identifier."),
    FieldSpec("source_record_id", "string", True, "Original business identity value as published."),
)
PILOT_AREA_DICTIONARY: Final[tuple[FieldSpec, ...]] = (
    FieldSpec("pilot_area", "enum", True, "Pilot area this official plan polygon belongs to.", PILOT_AREA_VALUES[:3]),
    FieldSpec("source_id", "string", True, "Configured plan source identifier."),
    FieldSpec("source_feature_id", "string", True, "Provider object ID of the plan feature (reference only)."),
    FieldSpec("licence", "string", True, "Public-data licence attribution."),
)
for _spec in (FIELD_DICTIONARY, SITE_DICTIONARY, PILOT_AREA_DICTIONARY):
    validate_output_field_names(f.name for f in _spec)
if tuple(f.name for f in FIELD_DICTIONARY) != PROJECT_FIELDS:
    raise RuntimeError("FIELD_DICTIONARY must match PROJECT_FIELDS")


@dataclass(frozen=True, slots=True)
class Finding:
    severity: Severity
    code: str
    message: str
    source_id: str | None = None
    record_id: str | None = None

    def to_dict(self) -> JSON:
        return {"severity": self.severity, "code": self.code, "message": self.message,
                "source_id": self.source_id, "record_id": self.record_id}


@dataclass(frozen=True, slots=True)
class ValidationResult:
    projects: tuple[JSON, ...]
    sites: tuple[JSON, ...]
    pilot_areas: tuple[JSON, ...]
    provenance: JSON
    findings: tuple[Finding, ...]

    @property
    def ok(self) -> bool:
        return not self.blockers

    @property
    def blockers(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "blocking")

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "warning")

    @property
    def exclusions(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == "exclusion")

    def to_dict(self) -> JSON:
        return {
            "ok": self.ok,
            "counts": {"projects": len(self.projects), "sites": len(self.sites), "pilot_areas": len(self.pilot_areas),
                       "blocking": len(self.blockers), "warnings": len(self.warnings),
                       "exclusions": len(self.exclusions)},
            "findings": [f.to_dict() for f in self.findings],
            "provenance": self.provenance,
        }


def feature_collection(features: tuple[JSON, ...] | list[JSON]) -> JSON:
    return {"type": "FeatureCollection", "features": list(features)}


class _Findings:
    def __init__(self) -> None:
        self.items: list[Finding] = []

    def add(self, severity: Severity, code: str, message: str, source_id: str | None = None,
            record_id: str | None = None) -> None:
        self.items.append(Finding(severity, code, message, source_id, record_id))


def _utc(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() != timedelta(0):
        return None
    return parsed.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            return None
    if not isinstance(value, int | float) or not math.isfinite(value):
        return None
    return float(value)


def _position_ok(pos: object) -> bool:
    if not isinstance(pos, list | tuple) or len(pos) < 2:
        return False
    x, y = _number_strict(pos[0]), _number_strict(pos[1])
    return x is not None and y is not None and -180 <= x <= 180 and -90 <= y <= 90


def _number_strict(value: object) -> float | None:
    return None if isinstance(value, str) else _number(value)


def _ring_error(ring: object) -> str | None:
    if not isinstance(ring, list) or len(ring) < 4:
        return "ring has fewer than 4 positions"
    if not all(_position_ok(p) for p in ring):
        return "non-finite or out-of-range coordinate"
    if list(ring[0][:2]) != list(ring[-1][:2]):
        return "unclosed ring"
    return None


def _polygonal(geom: object) -> tuple[_Geom | None, str | None]:
    if not isinstance(geom, dict):
        return None, "missing geometry"
    kind, coords = geom.get("type"), geom.get("coordinates")
    if kind not in ("Polygon", "MultiPolygon"):
        return None, f"unsupported geometry type {kind!r}"
    polygons = [coords] if kind == "Polygon" else coords
    if not isinstance(polygons, list) or not polygons:
        return None, "empty geometry"
    for polygon in polygons:
        if not isinstance(polygon, list) or not polygon:
            return None, "empty polygon"
        for ring in polygon:
            error = _ring_error(ring)
            if error:
                return None, error
    try:
        shp: _Geom = shape(geom)
    except (ValueError, TypeError, GEOSException) as exc:
        return None, f"unreadable geometry ({exc})"
    if shp.is_empty:
        return None, "empty geometry"
    if not shp.is_valid:
        return None, f"invalid geometry: {explain_validity(shp)}"
    return shp, None


def _source_meta(found: _Findings, source: Source, prov: JSON) -> JSON:
    sid = source.id
    for key in ("licence_url", "attribution"):
        if not isinstance(prov.get(key), str) or not prov[key]:
            found.add("blocking", "missing_provenance", f"Provenance lacks {key}", sid)
    retrieved = _utc(prov.get("retrieved_at"))
    if retrieved is None:
        found.add("blocking", "missing_provenance", "retrieved_at is missing or not a UTC timestamp", sid)
    updated = _utc(prov.get("source_updated_at"))
    if updated is None:
        found.add("warning", "missing_source_updated_at", "Source last-edit time unavailable; source_updated_at "
                  "is null", sid)
    url: str | None = None
    if source.is_remote:
        try:
            url = validate_https_url(str(prov.get("source_url")))
        except ConfigError:
            found.add("blocking", "missing_provenance", "source_url is missing or not HTTPS", sid)
    if prov.get("sample") is True:
        found.add("warning", "sample_snapshot", f"{SAMPLE_NOTE} Captured {prov.get('count')} of "
                  f"{prov.get('complete_source_count')} source records.", sid)
    return {"source_url": url, "retrieved_at": retrieved, "source_updated_at": updated,
            "sample": prov.get("sample") is True}


def _plans(found: _Findings, config: Config, collections: JSON) -> tuple[list[JSON], dict[str, _Geom]]:
    features: list[JSON] = []
    unions: dict[str, _Geom] = {}
    for area in config.pilot_areas_by_precedence():
        geoms: list[_Geom] = []
        clean = True
        for sid in area.source_ids:
            coll = collections.get(sid)
            feats = coll.get("features") if isinstance(coll, dict) else None
            if not isinstance(feats, list) or not feats:
                found.add("blocking", "empty_plan_source", "Official plan source has no features", sid)
                clean = False
                continue
            for feat in feats:
                fid = str(feat.get("id")) if isinstance(feat, dict) else "unknown"
                shp, error = _polygonal(feat.get("geometry") if isinstance(feat, dict) else None)
                if shp is None:
                    found.add("blocking", "invalid_plan_geometry", f"{error}; official geometry is never repaired",
                              sid, fid)
                    clean = False
                    continue
                geoms.append(shp)
                features.append({"type": "Feature", "geometry": feat["geometry"], "properties": {
                    "pilot_area": area.id, "source_id": sid, "source_feature_id": fid,
                    "licence": config.licence.attribution}})
        if clean and geoms:
            union: _Geom = union_all(geoms)
            unions[area.id] = union
    return features, unions


def _coords_ok(found: _Findings, config: Config, lon: float, lat: float, sid: str, pid: str) -> bool:
    env = config.region_envelope
    if env.contains(lat, lon) and not env.contains(lon, lat):
        found.add("blocking", "swapped_coordinates", "Longitude/latitude appear swapped", sid, pid)
        return False
    if not (-180 <= lon <= 180 and -90 <= lat <= 90):
        found.add("blocking", "out_of_range_coordinates", "Coordinates outside WGS84 range", sid, pid)
        return False
    if not env.contains(lon, lat):
        found.add("warning", "outside_region", "Point outside the regional sanity envelope (not a plan boundary)",
                  sid, pid)
    return True


def _location(found: _Findings, config: Config, source: Source, mapping: FieldMapping, feat: JSON,
              pid: str) -> tuple[float, float, str, JSON | None] | None:
    sid, props, geom = source.id, feat["properties"], feat.get("geometry")
    if source.kind == "csv":
        lon = _number(props.get(mapping.longitude or ""))
        lat = _number(props.get(mapping.latitude or ""))
        if lon is None or lat is None:
            found.add("blocking", "invalid_coordinates", "Mapped longitude/latitude missing or non-finite", sid, pid)
            return None
        return (lon, lat, "source_point", None) if _coords_ok(found, config, lon, lat, sid, pid) else None
    if not isinstance(geom, dict):
        found.add("blocking", "missing_geometry", "Record has no geometry", sid, pid)
        return None
    if geom.get("type") == "Point":
        coords = geom.get("coordinates")
        pair = coords if isinstance(coords, list) and len(coords) >= 2 else [None, None]
        lon_p, lat_p = _number_strict(pair[0]), _number_strict(pair[1])
        if lon_p is None or lat_p is None:
            found.add("blocking", "invalid_coordinates", "Point coordinates missing or non-finite", sid, pid)
            return None
        return (lon_p, lat_p, "source_point", None) if _coords_ok(found, config, lon_p, lat_p, sid, pid) else None
    shp, error = _polygonal(geom)
    if shp is None:
        found.add("blocking", "invalid_geometry", f"{error}; geometry is never repaired", sid, pid)
        return None
    point = shp.representative_point()
    lon_r, lat_r = float(point.x), float(point.y)
    return (lon_r, lat_r, "site_interior_point", geom) if _coords_ok(found, config, lon_r, lat_r, sid, pid) else None


def _project(found: _Findings, config: Config, source: Source, meta: JSON, pid: str, feat: JSON,
             unions: dict[str, _Geom]) -> tuple[JSON, JSON | None] | None:
    sid, mapping, props = source.id, source.require_mapping(), feat["properties"]
    nested = sorted(k for k, v in props.items() if isinstance(v, dict | list))
    if nested:
        found.add("blocking", "nested_property", f"Nested properties not allowed: {', '.join(nested)}", sid, pid)
        return None
    ok = True
    title = props.get(mapping.title)
    if not isinstance(title, str) or not title.strip():
        found.add("blocking", "missing_title", "Title is missing or empty", sid, pid)
        ok = False
    raw = props.get(mapping.status)
    rule = config.status_map.get(raw) if isinstance(raw, str) else None
    if rule is None:
        found.add("blocking", "unknown_status", f"Unmapped status {raw!r}; statuses are never guessed", sid, pid)
        ok = False
    record = props.get(mapping.source_record_id)
    if isinstance(record, bool) or not isinstance(record, str | int):
        found.add("blocking", "missing_source_record_id", "Original record identity missing", sid, pid)
        ok = False
    evidence: str | None = None
    if mapping.status_evidence_url is not None:
        value = props.get(mapping.status_evidence_url)
        if value not in (None, ""):
            try:
                evidence = validate_https_url(value if isinstance(value, str) else repr(value))
            except ConfigError:
                found.add("blocking", "invalid_evidence_url", "Evidence URL is not a valid HTTPS URL", sid, pid)
                ok = False
    location = _location(found, config, source, mapping, feat, pid)
    if not ok or location is None or rule is None or meta["source_url"] is None or meta["retrieved_at"] is None:
        return None
    lon, lat, basis, site_geom = location
    area, area_basis = "unassigned", "unassigned"
    point = Point(lon, lat)
    for area_id in config.area_precedence:
        union = unions.get(area_id)
        if union is not None and union.covers(point):
            area, area_basis = area_id, "official_plan_polygon"
            break
    properties = {
        "project_id": pid, "title": str(title).strip(), "status": rule.status, "status_raw": rule.raw,
        "status_evidence_url": evidence, "pilot_area": area, "area_basis": area_basis, "longitude": lon,
        "latitude": lat, "location_basis": basis, "source_id": sid, "source_record_id": str(record),
        "source_url": meta["source_url"], "retrieved_at": meta["retrieved_at"],
        "source_updated_at": meta["source_updated_at"], "licence": config.licence.attribution,
    }
    project = {"type": "Feature", "geometry": {"type": "Point", "coordinates": [lon, lat]}, "properties": properties}
    site = None
    if site_geom is not None and config.release.include_sites:
        site = {"type": "Feature", "geometry": site_geom,
                "properties": {"project_id": pid, "source_id": sid, "source_record_id": str(record)}}
    return project, site


def _projects(found: _Findings, config: Config, collections: JSON, meta: JSON | None,
              unions: dict[str, _Geom]) -> tuple[list[JSON], list[JSON]]:
    source = config.project_source
    mapping, sid = source.require_mapping(), source.id
    coll = collections.get(sid)
    feats = coll.get("features") if isinstance(coll, dict) else None
    if meta is None or not isinstance(feats, list):
        return [], []
    if not source.is_remote:
        found.add("blocking", "missing_source_url", LOCAL_URL_NOTE, sid)
    if mapping.status_evidence_url is None:
        found.add("warning", "status_evidence_url_unmapped", "No evidence URL field is mapped; "
                  "status_evidence_url is null for every record", sid)
    counts: dict[str, int] = {}
    candidates: list[tuple[str, JSON]] = []
    for index, feat in enumerate(feats):
        props = feat.get("properties") if isinstance(feat, dict) else None
        if not isinstance(props, dict):
            found.add("blocking", "malformed_feature", "Feature lacks a properties object", sid, f"#{index}")
            continue
        raw_id = props.get(mapping.project_id)
        if not isinstance(raw_id, str) or not raw_id.strip():
            found.add("blocking", "missing_id", "Business ID missing or not a non-empty string; OBJECTID is never "
                      "used as a fallback", sid, f"#{index}")
            continue
        pid = raw_id.strip()
        counts[pid] = counts.get(pid, 0) + 1
        candidates.append((pid, feat))
    for pid, count in sorted(counts.items()):
        if count > 1:
            found.add("blocking", "duplicate_id", f"Business ID occurs {count} times", sid, pid)
    selected = set(config.selection.reviewed_project_ids)
    projects: list[JSON] = []
    sites: list[JSON] = []
    for pid, feat in candidates:
        if counts[pid] > 1:
            continue
        if pid not in selected:
            found.add("exclusion", "not_selected", "Not in the reviewed selection; explicitly excluded", sid, pid)
            continue
        result = _project(found, config, source, meta, pid, feat, unions)
        if result is not None:
            projects.append(result[0])
            if result[1] is not None:
                sites.append(result[1])
    for pid in sorted(selected - set(counts)):
        found.add("blocking", "selected_missing", "Reviewed selected ID absent from the snapshot; no selected record "
                  "may silently disappear", sid, pid)
    projects.sort(key=lambda f: str(f["properties"]["project_id"]))
    sites.sort(key=lambda f: str(f["properties"]["project_id"]))
    project_ids = {f["properties"]["project_id"] for f in projects}
    for site in sites:
        if site["properties"]["project_id"] not in project_ids:
            found.add("blocking", "orphan_site", "Site polygon has no matching project", sid,
                      str(site["properties"]["project_id"]))
    return projects, sites


def normalize_loaded(loaded: JSON, config: Config) -> ValidationResult:
    found = _Findings()
    for reason in config.release_blockers():
        found.add("blocking", "release_not_permitted", reason)
    manifest_value = loaded.get("manifest")
    collections_value = loaded.get("collections")
    provenance_value = loaded.get("provenance")
    manifest: JSON = manifest_value if isinstance(manifest_value, dict) else {}
    collections: JSON = collections_value if isinstance(collections_value, dict) else {}
    provenance: JSON = provenance_value if isinstance(provenance_value, dict) else {}
    metas: dict[str, JSON] = {}
    for source in config.sources:
        prov = provenance.get(source.id)
        if not isinstance(prov, dict) or not isinstance(collections.get(source.id), dict):
            found.add("blocking", "missing_source", "Source collection or provenance missing from snapshot", source.id)
            continue
        metas[source.id] = _source_meta(found, source, prov)
    pilot_areas, unions = _plans(found, config, collections)
    projects, sites = _projects(found, config, collections, metas.get(config.project_source.id), unions)
    limitations = [config.boundary_disclosure, f"Overlap precedence: {', '.join(config.area_precedence)}."]
    if isinstance(manifest.get("snapshot_isolation"), str):
        limitations.append(manifest["snapshot_isolation"])
    if any(m["sample"] for m in metas.values()):
        limitations.append(SAMPLE_NOTE)
    record = {
        "snapshot": {k: manifest.get(k) for k in ("created_at", "origin", "config_digest", "capture")},
        "sources": {sid: {k: v for k, v in prov.items() if k != "requests"} | {"request_count": len(prov.get(
            "requests") or [])} for sid, prov in provenance.items() if isinstance(prov, dict)},
        "boundary_disclosure": config.boundary_disclosure,
        "area_precedence": list(config.area_precedence),
        "limitations": limitations,
    }
    return ValidationResult(tuple(projects), tuple(sites), tuple(pilot_areas), record, tuple(found.items))


def normalize_snapshot(snapshot: Path, config: Config) -> ValidationResult:
    try:
        loaded = load_snapshot(snapshot, config)
    except AcquisitionError as exc:
        finding = Finding("blocking", "snapshot_unreadable", f"Snapshot failed verification: {exc}")
        return ValidationResult((), (), (), {"snapshot": None, "sources": {}, "limitations": []}, (finding,))
    return normalize_loaded(loaded, config)
