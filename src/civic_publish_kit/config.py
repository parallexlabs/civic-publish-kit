"""Typed configuration, shared domain types and strict validation.

Configuration is loaded from strict JSON (duplicate keys, NaN/Infinity and unknown keys
are rejected). Every source, field, status and pilot-area mapping is explicit; nothing is
guessed. Later modules (acquisition, normalization/validation, store/diff/reports, CLI)
consume the frozen dataclasses defined here and the shared constants and helpers below.

Snapshot / fixture layout (shared by live fetch and frozen fixtures)::

    <root>/snapshot-manifest.json            provenance for the whole snapshot
    <root>/capture-manifest.json             fixtures only: capture record for frozen responses
    <root>/sources/<source_id>/metadata.json layer metadata (title, fields, objectIdField, ...)
    <root>/sources/<source_id>/count.json    returnCountOnly response before reading
    <root>/sources/<source_id>/ids.json      returnIdsOnly response before reading
    <root>/sources/<source_id>/batches/batch-0001.geojson   GeoJSON batch responses (outSR=4326)
    <root>/sources/<source_id>/final-count.json  end-of-read count verification
    <root>/sources/<source_id>/final-ids.json    end-of-read ID verification
    <root>/sources/<source_id>/requests.json     exact requests, UTC times and response hashes
    <root>/sources/<source_id>/input.geojson|input.csv   local (non-ArcGIS) inputs
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import ClassVar, Final, Literal
from urllib.parse import unquote, urlsplit

SCHEMA_VERSION: Final = 1

NormalizedStatus = Literal[
    "approved", "under_construction", "conditionally_approved", "other", "unknown"
]
PilotAreaId = Literal["city_centre", "fleetwood", "campbell_heights"]
PilotAreaValue = Literal["city_centre", "fleetwood", "campbell_heights", "other", "unassigned"]
AreaBasis = Literal[
    "official_plan_polygon", "source_attribute", "reviewed_assignment", "unassigned"
]
LocationBasis = Literal["source_point", "site_interior_point"]
SourceKind = Literal["arcgis", "geojson", "csv"]
SourceRole = Literal["projects", "plan_area"]

NORMALIZED_STATUSES: Final[tuple[NormalizedStatus, ...]] = (
    "approved",
    "under_construction",
    "conditionally_approved",
    "other",
    "unknown",
)
PILOT_AREA_IDS: Final[tuple[PilotAreaId, ...]] = ("city_centre", "fleetwood", "campbell_heights")
PILOT_AREA_VALUES: Final[tuple[PilotAreaValue, ...]] = (
    "city_centre",
    "fleetwood",
    "campbell_heights",
    "other",
    "unassigned",
)
AREA_BASES: Final[tuple[AreaBasis, ...]] = (
    "official_plan_polygon",
    "source_attribute",
    "reviewed_assignment",
    "unassigned",
)
LOCATION_BASES: Final[tuple[LocationBasis, ...]] = ("source_point", "site_interior_point")
SOURCE_KINDS: Final[tuple[SourceKind, ...]] = ("arcgis", "geojson", "csv")
SOURCE_ROLES: Final[tuple[SourceRole, ...]] = ("projects", "plan_area")
REVIEW_LABEL: Final = "review"

MAX_TIMEOUT_SECONDS: Final = 20.0
MAX_RETRIES: Final = 2
MAX_RECORDS: Final = 20_000
MAX_RESPONSE_BYTES: Final = 25_000_000
MAX_BATCH_SIZE: Final = 500
MAX_CONFIG_BYTES: Final = 1_000_000
MAX_GEOJSON_OUTPUT_BYTES: Final = 100_000_000
COORDINATE_TOLERANCE_DEGREES: Final = 0.000001
REMOVAL_WARNING_FRACTION: Final = 0.20
DEFAULT_REGION_ENVELOPE: Final = (-123.1, 48.9, -122.3, 49.4)

OUTPUT_FIELD_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
IDENTIFIER_PATTERN: Final = re.compile(r"^[a-z][a-z0-9_]{0,30}$")
SOURCE_FIELD_PATTERN: Final = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_HOST_PATTERN: Final = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$"
)
_LAYER_PATH_PATTERN: Final = re.compile(
    r"^(?:/[A-Za-z0-9_-]+)*/arcgis/rest/services/"
    r"(?P<services>(?:[A-Za-z0-9_.%-]+/){1,2})"
    r"(?:FeatureServer|MapServer)/(?:0|[1-9][0-9]{0,5})$"
)
_FORBIDDEN_WHERE_TOKENS: Final = (";", "--", "/*", "*/")

ARCGIS_RESERVED_FIELD_NAMES: Final = frozenset(
    {
        "objectid", "fid", "oid", "globalid", "shape", "shape_area", "shape_length",
        "shape__area", "shape__length", "geometry", "geom", "the_geom",
        "add", "all", "alter", "and", "as", "asc", "between", "by", "column", "create",
        "date", "delete", "desc", "drop", "exists", "for", "from", "group", "in", "insert",
        "into", "is", "join", "like", "not", "null", "or", "order", "select", "set",
        "table", "update", "values", "where",
    }
)

PROJECT_FIELDS: Final[tuple[str, ...]] = (
    "project_id",
    "title",
    "status",
    "status_raw",
    "status_evidence_url",
    "pilot_area",
    "area_basis",
    "longitude",
    "latitude",
    "location_basis",
    "source_id",
    "source_record_id",
    "source_url",
    "retrieved_at",
    "source_updated_at",
    "licence",
)

_MISSING: Final = object()


class ConfigError(ValueError):
    """Invalid configuration, with a JSON-path-like location."""

    def __init__(self, location: str, message: str) -> None:
        super().__init__(f"{location}: {message}")
        self.location = location
        self.message = message


def canonical_json(value: object) -> bytes:
    """Deterministic UTF-8 JSON (sorted keys, compact, no NaN) shared by all modules."""
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigError("$", f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(name: str) -> object:
    raise ConfigError("$", f"non-finite JSON number {name} is not allowed")


def parse_json_strict(text: str) -> object:
    """Parse JSON rejecting duplicate keys and NaN/Infinity constants."""
    try:
        return json.loads(
            text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant
        )
    except json.JSONDecodeError as exc:
        raise ConfigError("$", f"malformed JSON at line {exc.lineno} column {exc.colno}") from exc


def _text(value: object, location: str) -> str:
    if not isinstance(value, str):
        raise ConfigError(location, "must be a string")
    if not value or value != value.strip():
        raise ConfigError(location, "must be non-empty without surrounding whitespace")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ConfigError(location, "must not contain control characters")
    return value


def _int_value(value: object, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(location, "must be an integer")
    return value


def _float_value(value: object, location: str) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigError(location, "must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ConfigError(location, "must be finite")
    return result


def _choice[T: str](value: str, allowed: tuple[T, ...], location: str) -> T:
    for option in allowed:
        if value == option:
            return option
    raise ConfigError(location, f"must be one of {', '.join(allowed)}; got {value!r}")


def _identifier(value: str, location: str) -> str:
    if not IDENTIFIER_PATTERN.fullmatch(value):
        raise ConfigError(location, f"must match {IDENTIFIER_PATTERN.pattern}")
    return value


def _source_field(value: str, location: str) -> str:
    if not SOURCE_FIELD_PATTERN.fullmatch(value):
        raise ConfigError(location, f"must be a plain field name matching {SOURCE_FIELD_PATTERN.pattern}")
    return value


def _unique(values: Iterable[str], location: str) -> None:
    seen: set[str] = set()
    for value in values:
        if value in seen:
            raise ConfigError(location, f"duplicate value {value!r}")
        seen.add(value)


class _Obj:
    """Strict reader over one JSON object that tracks consumed keys."""

    __slots__ = ("_data", "_path", "_seen")

    def __init__(self, value: object, path: str) -> None:
        if not isinstance(value, dict):
            raise ConfigError(path, "must be a JSON object")
        self._data: dict[str, object] = value
        self._path = path
        self._seen: set[str] = set()

    def at(self, key: str) -> str:
        return f"{self._path}.{key}"

    def _take(self, key: str) -> object:
        self._seen.add(key)
        return self._data.get(key, _MISSING)

    def _required(self, key: str) -> object:
        value = self._take(key)
        if value is _MISSING:
            raise ConfigError(self.at(key), "is required")
        return value

    def req_str(self, key: str) -> str:
        return _text(self._required(key), self.at(key))

    def opt_str(self, key: str) -> str | None:
        value = self._take(key)
        if value is _MISSING or value is None:
            return None
        return _text(value, self.at(key))

    def req_bool(self, key: str) -> bool:
        value = self._required(key)
        if not isinstance(value, bool):
            raise ConfigError(self.at(key), "must be true or false")
        return value

    def opt_bool(self, key: str, default: bool) -> bool:
        value = self._take(key)
        if value is _MISSING:
            return default
        if not isinstance(value, bool):
            raise ConfigError(self.at(key), "must be true or false")
        return value

    def req_int(self, key: str) -> int:
        return _int_value(self._required(key), self.at(key))

    def opt_int(self, key: str, default: int) -> int:
        value = self._take(key)
        return default if value is _MISSING else _int_value(value, self.at(key))

    def opt_float(self, key: str, default: float) -> float:
        value = self._take(key)
        return default if value is _MISSING else _float_value(value, self.at(key))

    def _list(self, key: str, *, required: bool) -> list[object]:
        value = self._take(key)
        if value is _MISSING:
            if required:
                raise ConfigError(self.at(key), "is required")
            return []
        if not isinstance(value, list):
            raise ConfigError(self.at(key), "must be a JSON array")
        return list(value)

    def str_list(self, key: str, *, required: bool = True) -> tuple[str, ...]:
        items = self._list(key, required=required)
        return tuple(_text(item, f"{self.at(key)}[{i}]") for i, item in enumerate(items))

    def num_list(self, key: str) -> tuple[float, ...]:
        items = self._list(key, required=True)
        return tuple(_float_value(item, f"{self.at(key)}[{i}]") for i, item in enumerate(items))

    def obj(self, key: str) -> _Obj:
        return _Obj(self._required(key), self.at(key))

    def opt_obj(self, key: str) -> _Obj | None:
        value = self._take(key)
        if value is _MISSING or value is None:
            return None
        return _Obj(value, self.at(key))

    def obj_list(self, key: str) -> list[_Obj]:
        items = self._list(key, required=True)
        return [_Obj(item, f"{self.at(key)}[{i}]") for i, item in enumerate(items)]

    def finish(self) -> None:
        unknown = sorted(set(self._data) - self._seen)
        if unknown:
            raise ConfigError(self._path, f"unknown keys: {', '.join(unknown)}")


def validate_host(host: str, location: str = "host") -> str:
    """Validate a lowercase DNS hostname for the allowlist (no wildcards, ports or IPs)."""
    if not _HOST_PATTERN.fullmatch(host):
        raise ConfigError(location, f"invalid allowlisted host {host!r}")
    return host


def validate_https_url(url: str, location: str = "url") -> str:
    """Require an HTTPS URL with a host and no credentials, whitespace or fragment."""
    if any(char.isspace() for char in url):
        raise ConfigError(location, "URL must not contain whitespace")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ConfigError(location, "URL must use https")
    if "@" in parts.netloc or parts.username is not None or parts.password is not None:
        raise ConfigError(location, "URL must not embed credentials")
    if not parts.hostname:
        raise ConfigError(location, "URL must include a host")
    if parts.fragment:
        raise ConfigError(location, "URL must not include a fragment")
    return url


def validate_arcgis_layer_url(
    url: str, allowed_hosts: Iterable[str], location: str = "url"
) -> str:
    """Validate an exact HTTPS ArcGIS FeatureServer/MapServer layer URL on an allowlisted host.

    Rejects embedded credentials, any query string (so no token/API key parameters),
    fragments, explicit ports, path traversal and encoded separators.
    """
    validate_https_url(url, location)
    parts = urlsplit(url)
    if parts.query:
        raise ConfigError(
            location, "layer URL must not include a query string (token/API key parameters are forbidden)"
        )
    host = parts.hostname or ""
    if parts.netloc != host or parts.port is not None:
        raise ConfigError(location, "layer URL host must be lowercase with no port")
    if host not in set(allowed_hosts):
        raise ConfigError(location, f"host {host!r} is not allowlisted")
    match = _LAYER_PATH_PATTERN.fullmatch(parts.path)
    if match is None:
        raise ConfigError(
            location, "must be an exact .../arcgis/rest/services/<service>/FeatureServer|MapServer/<n> layer URL"
        )
    for segment in match.group("services").rstrip("/").split("/"):
        lowered = segment.lower()
        if "%2f" in lowered or "%5c" in lowered or "%00" in lowered:
            raise ConfigError(location, "encoded path separators are not allowed")
        decoded = unquote(segment)
        if decoded in {".", ".."} or any(ord(char) < 32 for char in decoded):
            raise ConfigError(location, "path traversal or control characters are not allowed")
    return url


def validate_where(where: str, location: str = "where") -> str:
    """Basic safety checks on a configured ArcGIS where clause (configured, never user-built)."""
    if len(where) > 2000:
        raise ConfigError(location, "where clause is too long")
    for token in _FORBIDDEN_WHERE_TOKENS:
        if token in where:
            raise ConfigError(location, f"where clause must not contain {token!r}")
    return where


def validate_output_field_names(names: Iterable[str]) -> tuple[str, ...]:
    """Output fields: ^[a-z][a-z0-9_]{0,30}$, unique, and not ArcGIS reserved names."""
    result = tuple(names)
    _unique(result, "output_fields")
    for name in result:
        if not OUTPUT_FIELD_PATTERN.fullmatch(name):
            raise ConfigError("output_fields", f"{name!r} must match {OUTPUT_FIELD_PATTERN.pattern}")
        if name in ARCGIS_RESERVED_FIELD_NAMES:
            raise ConfigError("output_fields", f"{name!r} is an ArcGIS reserved name")
    return result


validate_output_field_names(PROJECT_FIELDS)


def _relative_path(value: str, location: str) -> str:
    if "\\" in value:
        raise ConfigError(location, "use forward slashes in paths")
    pure = PurePosixPath(value)
    if pure.is_absolute() or any(part in {"", ".", ".."} for part in value.split("/")):
        raise ConfigError(location, "path must be relative without traversal")
    return value


@dataclass(frozen=True, slots=True)
class Limits:
    """Per-source transport and size limits; values may only tighten spec maxima."""

    timeout_seconds: float = MAX_TIMEOUT_SECONDS
    max_retries: int = MAX_RETRIES
    max_records: int = MAX_RECORDS
    max_response_bytes: int = MAX_RESPONSE_BYTES
    max_batch_size: int = MAX_BATCH_SIZE

    def batch_size(self, max_record_count: int) -> int:
        """Batch size = min(layer maxRecordCount, configured cap)."""
        if max_record_count < 1:
            raise ValueError("maxRecordCount must be a positive integer")
        return min(max_record_count, self.max_batch_size)

    def to_dict(self) -> dict[str, object]:
        return {
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "max_records": self.max_records,
            "max_response_bytes": self.max_response_bytes,
            "max_batch_size": self.max_batch_size,
        }


@dataclass(frozen=True, slots=True)
class Licence:
    name: str
    url: str
    attribution: str
    reviewed: bool
    review_note: str | None = None

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "name": self.name,
            "url": self.url,
            "attribution": self.attribution,
            "reviewed": self.reviewed,
        }
        if self.review_note is not None:
            result["review_note"] = self.review_note
        return result


@dataclass(frozen=True, slots=True)
class StatusRule:
    """Exact raw status string mapped to a normalized status and an explicit label."""

    raw: str
    status: NormalizedStatus
    label: str

    def to_dict(self) -> dict[str, object]:
        return {"raw": self.raw, "status": self.status, "label": self.label}


@dataclass(frozen=True, slots=True)
class StatusMap:
    rules: tuple[StatusRule, ...]

    def get(self, raw: str) -> StatusRule | None:
        """Exact (case- and whitespace-sensitive) lookup; None means unmapped and blocking."""
        for rule in self.rules:
            if rule.raw == raw:
                return rule
        return None

    @property
    def raw_values(self) -> tuple[str, ...]:
        return tuple(rule.raw for rule in self.rules)

    def to_list(self) -> list[dict[str, object]]:
        return [rule.to_dict() for rule in self.rules]


@dataclass(frozen=True, slots=True)
class FieldMapping:
    """Explicit source-field mapping for project records (no guessed fields)."""

    project_id: str
    source_record_id: str
    title: str
    status: str
    status_evidence_url: str | None = None
    longitude: str | None = None
    latitude: str | None = None

    def source_fields(self) -> tuple[str, ...]:
        ordered = (
            self.project_id,
            self.source_record_id,
            self.title,
            self.status,
            self.status_evidence_url,
            self.longitude,
            self.latitude,
        )
        result: list[str] = []
        for name in ordered:
            if name is not None and name not in result:
                result.append(name)
        return tuple(result)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "project_id": self.project_id,
            "source_record_id": self.source_record_id,
            "title": self.title,
            "status": self.status,
        }
        for key, value in (
            ("status_evidence_url", self.status_evidence_url),
            ("longitude", self.longitude),
            ("latitude", self.latitude),
        ):
            if value is not None:
                result[key] = value
        return result


@dataclass(frozen=True, slots=True)
class Source:
    """One configured input. `out_fields` is the public attribute allowlist.

    For ArcGIS sources acquisition additionally requests the layer's metadata
    objectIdField solely for count/ID completeness checks; it is never a business identity.
    """

    id: str
    kind: SourceKind
    role: SourceRole
    out_fields: tuple[str, ...]
    url: str | None = None
    where: str | None = None
    path: str | None = None
    mapping: FieldMapping | None = None

    @property
    def is_remote(self) -> bool:
        return self.kind == "arcgis"

    def require_url(self) -> str:
        if self.url is None:
            raise ConfigError(f"sources.{self.id}.url", "is required for arcgis sources")
        return self.url

    def require_where(self) -> str:
        if self.where is None:
            raise ConfigError(f"sources.{self.id}.where", "is required for arcgis sources")
        return self.where

    def require_path(self) -> str:
        if self.path is None:
            raise ConfigError(f"sources.{self.id}.path", "is required for local sources")
        return self.path

    def require_mapping(self) -> FieldMapping:
        if self.mapping is None:
            raise ConfigError(f"sources.{self.id}.mapping", "is required for project sources")
        return self.mapping

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {"id": self.id, "kind": self.kind, "role": self.role}
        if self.url is not None:
            result["url"] = self.url
        if self.where is not None:
            result["where"] = self.where
        if self.path is not None:
            result["path"] = self.path
        result["out_fields"] = list(self.out_fields)
        if self.mapping is not None:
            result["mapping"] = self.mapping.to_dict()
        return result


@dataclass(frozen=True, slots=True)
class PilotArea:
    """A pilot area defined by the union of official plan polygons from its sources."""

    id: PilotAreaId
    title: str
    source_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"id": self.id, "title": self.title, "source_ids": list(self.source_ids)}


@dataclass(frozen=True, slots=True)
class Envelope:
    """Regional sanity envelope (warning only; not a plan or jurisdictional boundary)."""

    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float

    def contains(self, longitude: float, latitude: float) -> bool:
        return (
            self.min_lon <= longitude <= self.max_lon
            and self.min_lat <= latitude <= self.max_lat
        )

    def to_list(self) -> list[float]:
        return [self.min_lon, self.min_lat, self.max_lon, self.max_lat]


@dataclass(frozen=True, slots=True)
class Selection:
    """Reviewed project-ID selection. Unselected records are explicitly excluded.

    An empty selection releases nothing: every record is reported as an exclusion.
    """

    reviewed_project_ids: tuple[str, ...]
    note: str

    def is_selected(self, project_id: str) -> bool:
        return project_id in self.reviewed_project_ids

    def to_dict(self) -> dict[str, object]:
        return {"reviewed_project_ids": list(self.reviewed_project_ids), "note": self.note}


@dataclass(frozen=True, slots=True)
class ReleaseOptions:
    include_sites: bool

    def to_dict(self) -> dict[str, object]:
        return {"include_sites": self.include_sites}


@dataclass(frozen=True, slots=True)
class Publishing:
    """Exact item properties printed by `publish --dry-run` for staff."""

    item_title: str
    summary: str
    tags: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {"item_title": self.item_title, "summary": self.summary, "tags": list(self.tags)}


@dataclass(frozen=True, slots=True)
class Config:
    schema_version: int
    name: str
    title: str
    base_dir: Path
    allowed_hosts: tuple[str, ...]
    limits: Limits
    licence: Licence
    status_map: StatusMap
    sources: tuple[Source, ...]
    pilot_areas: tuple[PilotArea, ...]
    area_precedence: tuple[PilotAreaId, ...]
    boundary_disclosure: str
    region_envelope: Envelope
    selection: Selection
    release: ReleaseOptions
    publishing: Publishing

    def source(self, source_id: str) -> Source:
        for source in self.sources:
            if source.id == source_id:
                return source
        raise KeyError(source_id)

    @property
    def project_source(self) -> Source:
        return next(source for source in self.sources if source.role == "projects")

    @property
    def plan_sources(self) -> tuple[Source, ...]:
        return tuple(source for source in self.sources if source.role == "plan_area")

    def pilot_area(self, area_id: str) -> PilotArea:
        for area in self.pilot_areas:
            if area.id == area_id:
                return area
        raise KeyError(area_id)

    def pilot_areas_by_precedence(self) -> tuple[PilotArea, ...]:
        return tuple(self.pilot_area(area_id) for area_id in self.area_precedence)

    def pilot_area_for_source(self, source_id: str) -> PilotArea:
        for area in self.pilot_areas:
            if source_id in area.source_ids:
                return area
        raise KeyError(source_id)

    def resolve_local_path(self, source: Source) -> Path:
        """Resolve a local input path under the config directory, rejecting escapes."""
        base = self.base_dir.resolve()
        candidate = (base / source.require_path()).resolve()
        if not candidate.is_relative_to(base):
            raise ConfigError(f"sources.{source.id}.path", "resolves outside the config directory")
        return candidate

    def release_blockers(self) -> tuple[str, ...]:
        """Reasons public packaging is not permitted (empty tuple means permitted)."""
        reasons: list[str] = []
        if not self.licence.reviewed:
            reasons.append("licence has not been reviewed in configuration")
        if not self.selection.reviewed_project_ids:
            reasons.append("no reviewed project IDs are selected; all records are excluded")
        return tuple(reasons)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "name": self.name,
            "title": self.title,
            "allowed_hosts": list(self.allowed_hosts),
            "limits": self.limits.to_dict(),
            "licence": self.licence.to_dict(),
            "status_map": self.status_map.to_list(),
            "sources": [source.to_dict() for source in self.sources],
            "pilot_areas": [area.to_dict() for area in self.pilot_areas],
            "area_precedence": list(self.area_precedence),
            "boundary_disclosure": self.boundary_disclosure,
            "region_envelope": self.region_envelope.to_list(),
            "selection": self.selection.to_dict(),
            "release": self.release.to_dict(),
            "publishing": self.publishing.to_dict(),
        }

    def digest(self) -> str:
        """SHA256 of canonical configuration (machine-independent; excludes base_dir)."""
        return hashlib.sha256(canonical_json(self.to_dict())).hexdigest()


@dataclass(frozen=True, slots=True)
class SnapshotLayout:
    """Stable on-disk layout shared by live snapshots and frozen fixtures."""

    root: Path

    MANIFEST: ClassVar[str] = "snapshot-manifest.json"
    CAPTURE_MANIFEST: ClassVar[str] = "capture-manifest.json"

    def manifest_path(self) -> Path:
        return self.root / self.MANIFEST

    def capture_manifest_path(self) -> Path:
        return self.root / self.CAPTURE_MANIFEST

    def source_dir(self, source_id: str) -> Path:
        if not IDENTIFIER_PATTERN.fullmatch(source_id):
            raise ValueError(f"invalid source id {source_id!r}")
        return self.root / "sources" / source_id

    def metadata_path(self, source_id: str) -> Path:
        return self.source_dir(source_id) / "metadata.json"

    def count_path(self, source_id: str) -> Path:
        return self.source_dir(source_id) / "count.json"

    def ids_path(self, source_id: str) -> Path:
        return self.source_dir(source_id) / "ids.json"

    def final_count_path(self, source_id: str) -> Path:
        return self.source_dir(source_id) / "final-count.json"

    def final_ids_path(self, source_id: str) -> Path:
        return self.source_dir(source_id) / "final-ids.json"

    def requests_path(self, source_id: str) -> Path:
        return self.source_dir(source_id) / "requests.json"

    def batches_dir(self, source_id: str) -> Path:
        return self.source_dir(source_id) / "batches"

    def batch_path(self, source_id: str, index: int) -> Path:
        if index < 1 or index > 9999:
            raise ValueError("batch index must be between 1 and 9999")
        return self.batches_dir(source_id) / f"batch-{index:04d}.geojson"

    def local_input_path(self, source: Source) -> Path:
        if source.kind == "geojson":
            return self.source_dir(source.id) / "input.geojson"
        if source.kind == "csv":
            return self.source_dir(source.id) / "input.csv"
        raise ValueError(f"source {source.id!r} is not a local input")


def _parse_limits(obj: _Obj | None) -> Limits:
    if obj is None:
        return Limits()
    limits = Limits(
        timeout_seconds=obj.opt_float("timeout_seconds", MAX_TIMEOUT_SECONDS),
        max_retries=obj.opt_int("max_retries", MAX_RETRIES),
        max_records=obj.opt_int("max_records", MAX_RECORDS),
        max_response_bytes=obj.opt_int("max_response_bytes", MAX_RESPONSE_BYTES),
        max_batch_size=obj.opt_int("max_batch_size", MAX_BATCH_SIZE),
    )
    obj.finish()
    if not 0 < limits.timeout_seconds <= MAX_TIMEOUT_SECONDS:
        raise ConfigError(obj.at("timeout_seconds"), f"must be in (0, {MAX_TIMEOUT_SECONDS}]")
    if not 0 <= limits.max_retries <= MAX_RETRIES:
        raise ConfigError(obj.at("max_retries"), f"must be in [0, {MAX_RETRIES}]")
    if not 1 <= limits.max_records <= MAX_RECORDS:
        raise ConfigError(obj.at("max_records"), f"must be in [1, {MAX_RECORDS}]")
    if not 1 <= limits.max_response_bytes <= MAX_RESPONSE_BYTES:
        raise ConfigError(obj.at("max_response_bytes"), f"must be in [1, {MAX_RESPONSE_BYTES}]")
    if not 1 <= limits.max_batch_size <= MAX_BATCH_SIZE:
        raise ConfigError(obj.at("max_batch_size"), f"must be in [1, {MAX_BATCH_SIZE}]")
    return limits


def _parse_licence(obj: _Obj) -> Licence:
    licence = Licence(
        name=obj.req_str("name"),
        url=validate_https_url(obj.req_str("url"), obj.at("url")),
        attribution=obj.req_str("attribution"),
        reviewed=obj.req_bool("reviewed"),
        review_note=obj.opt_str("review_note"),
    )
    obj.finish()
    return licence


def _parse_status_map(items: list[_Obj], location: str) -> StatusMap:
    if not items:
        raise ConfigError(location, "must define at least one explicit status rule")
    rules: list[StatusRule] = []
    for item in items:
        raw = item.req_str("raw")
        status = _choice(item.req_str("status"), NORMALIZED_STATUSES, item.at("status"))
        label = _identifier(item.req_str("label"), item.at("label"))
        item.finish()
        if "review" in raw.casefold() and status != "unknown":
            raise ConfigError(item.at("status"), "review statuses must map to unknown, never approved")
        if label == REVIEW_LABEL and status != "unknown":
            raise ConfigError(item.at("label"), "the review label is only valid for status unknown")
        rules.append(StatusRule(raw=raw, status=status, label=label))
    _unique((rule.raw for rule in rules), location)
    return StatusMap(rules=tuple(rules))


def _parse_mapping(obj: _Obj) -> FieldMapping:
    def field(key: str) -> str:
        return _source_field(obj.req_str(key), obj.at(key))

    def optional(key: str) -> str | None:
        value = obj.opt_str(key)
        return None if value is None else _source_field(value, obj.at(key))

    mapping = FieldMapping(
        project_id=field("project_id"),
        source_record_id=field("source_record_id"),
        title=field("title"),
        status=field("status"),
        status_evidence_url=optional("status_evidence_url"),
        longitude=optional("longitude"),
        latitude=optional("latitude"),
    )
    obj.finish()
    return mapping


def _parse_source(obj: _Obj, allowed_hosts: tuple[str, ...]) -> Source:
    source_id = _identifier(obj.req_str("id"), obj.at("id"))
    kind = _choice(obj.req_str("kind"), SOURCE_KINDS, obj.at("kind"))
    role = _choice(obj.req_str("role"), SOURCE_ROLES, obj.at("role"))
    url = obj.opt_str("url")
    where = obj.opt_str("where")
    path = obj.opt_str("path")
    out_fields = tuple(
        _source_field(name, f"{obj.at('out_fields')}[{i}]")
        for i, name in enumerate(obj.str_list("out_fields"))
    )
    _unique(out_fields, obj.at("out_fields"))
    mapping_obj = obj.opt_obj("mapping")
    mapping = None if mapping_obj is None else _parse_mapping(mapping_obj)
    obj.finish()

    if kind == "arcgis":
        if url is None or where is None:
            raise ConfigError(obj.at("url"), "arcgis sources require url and where")
        validate_arcgis_layer_url(url, allowed_hosts, obj.at("url"))
        validate_where(where, obj.at("where"))
        if path is not None:
            raise ConfigError(obj.at("path"), "arcgis sources must not set a local path")
    else:
        if path is None:
            raise ConfigError(obj.at("path"), f"{kind} sources require a local path")
        _relative_path(path, obj.at("path"))
        if url is not None or where is not None:
            raise ConfigError(obj.at("url"), "local sources must not set url or where")

    if role == "projects":
        if mapping is None:
            raise ConfigError(obj.at("mapping"), "project sources require an explicit field mapping")
        has_lon = mapping.longitude is not None
        has_lat = mapping.latitude is not None
        if kind == "csv" and not (has_lon and has_lat):
            raise ConfigError(obj.at("mapping"), "csv sources require explicit longitude and latitude")
        if kind != "csv" and (has_lon or has_lat):
            raise ConfigError(obj.at("mapping"), "longitude/latitude mappings are only valid for csv")
        missing = [name for name in mapping.source_fields() if name not in out_fields]
        if missing:
            raise ConfigError(obj.at("out_fields"), f"mapped fields not allowlisted: {', '.join(missing)}")
    else:
        if mapping is not None:
            raise ConfigError(obj.at("mapping"), "plan_area sources must not define a project mapping")
        if kind == "csv":
            raise ConfigError(obj.at("kind"), "plan_area sources require polygon geometry (arcgis or geojson)")

    return Source(
        id=source_id,
        kind=kind,
        role=role,
        out_fields=out_fields,
        url=url,
        where=where,
        path=path,
        mapping=mapping,
    )


def _parse_pilot_areas(
    items: list[_Obj], sources: tuple[Source, ...], location: str
) -> tuple[PilotArea, ...]:
    plan_ids = {source.id for source in sources if source.role == "plan_area"}
    areas: list[PilotArea] = []
    used: list[str] = []
    for item in items:
        area_id = _choice(item.req_str("id"), PILOT_AREA_IDS, item.at("id"))
        title = item.req_str("title")
        source_ids = item.str_list("source_ids")
        item.finish()
        if not source_ids:
            raise ConfigError(item.at("source_ids"), "must reference at least one plan source")
        for source_id in source_ids:
            if source_id not in plan_ids:
                raise ConfigError(item.at("source_ids"), f"{source_id!r} is not a plan_area source")
        used.extend(source_ids)
        areas.append(PilotArea(id=area_id, title=title, source_ids=source_ids))
    _unique((area.id for area in areas), location)
    _unique(used, f"{location}.source_ids")
    if {area.id for area in areas} != set(PILOT_AREA_IDS):
        raise ConfigError(location, f"must define exactly: {', '.join(PILOT_AREA_IDS)}")
    unused = sorted(plan_ids - set(used))
    if unused:
        raise ConfigError(location, f"plan sources not assigned to a pilot area: {', '.join(unused)}")
    return tuple(areas)


def _parse_envelope(values: tuple[float, ...], location: str) -> Envelope:
    if len(values) != 4:
        raise ConfigError(location, "must be [min_lon, min_lat, max_lon, max_lat]")
    envelope = Envelope(*values)
    if not (-180 <= envelope.min_lon < envelope.max_lon <= 180):
        raise ConfigError(location, "invalid longitude range")
    if not (-90 <= envelope.min_lat < envelope.max_lat <= 90):
        raise ConfigError(location, "invalid latitude range")
    return envelope


def _parse_selection(obj: _Obj) -> Selection:
    ids = obj.str_list("reviewed_project_ids")
    note = obj.req_str("note")
    obj.finish()
    _unique(ids, obj.at("reviewed_project_ids"))
    return Selection(reviewed_project_ids=tuple(sorted(ids)), note=note)


def _parse_publishing(obj: _Obj) -> Publishing:
    publishing = Publishing(
        item_title=obj.req_str("item_title"),
        summary=obj.req_str("summary"),
        tags=obj.str_list("tags"),
    )
    obj.finish()
    if not publishing.tags:
        raise ConfigError(obj.at("tags"), "must include at least one tag")
    _unique(publishing.tags, obj.at("tags"))
    return publishing


def parse_config(data: object, *, base_dir: Path) -> Config:
    """Validate parsed JSON into a frozen Config. Raises ConfigError on any problem."""
    root = _Obj(data, "$")
    schema_version = root.req_int("schema_version")
    if schema_version != SCHEMA_VERSION:
        raise ConfigError(root.at("schema_version"), f"unsupported version; expected {SCHEMA_VERSION}")
    name = _identifier(root.req_str("name"), root.at("name"))
    title = root.req_str("title")

    allowed_hosts = tuple(
        validate_host(host, f"{root.at('allowed_hosts')}[{i}]")
        for i, host in enumerate(root.str_list("allowed_hosts"))
    )
    if not allowed_hosts:
        raise ConfigError(root.at("allowed_hosts"), "must list at least one host")
    _unique(allowed_hosts, root.at("allowed_hosts"))

    limits = _parse_limits(root.opt_obj("limits"))
    licence = _parse_licence(root.obj("licence"))
    status_map = _parse_status_map(root.obj_list("status_map"), root.at("status_map"))

    sources = tuple(_parse_source(item, allowed_hosts) for item in root.obj_list("sources"))
    _unique((source.id for source in sources), root.at("sources"))
    if sum(1 for source in sources if source.role == "projects") != 1:
        raise ConfigError(root.at("sources"), "exactly one projects source is required")

    pilot_areas = _parse_pilot_areas(root.obj_list("pilot_areas"), sources, root.at("pilot_areas"))
    precedence_loc = root.at("area_precedence")
    area_precedence = tuple(
        _choice(value, PILOT_AREA_IDS, precedence_loc) for value in root.str_list("area_precedence")
    )
    _unique(area_precedence, precedence_loc)
    if set(area_precedence) != set(PILOT_AREA_IDS):
        raise ConfigError(precedence_loc, "must order every pilot area exactly once")

    boundary_disclosure = root.req_str("boundary_disclosure")
    region_envelope = _parse_envelope(root.num_list("region_envelope"), root.at("region_envelope"))
    selection = _parse_selection(root.obj("selection"))
    release_obj = root.obj("release")
    release = ReleaseOptions(include_sites=release_obj.req_bool("include_sites"))
    release_obj.finish()
    publishing = _parse_publishing(root.obj("publishing"))
    root.finish()

    return Config(
        schema_version=schema_version,
        name=name,
        title=title,
        base_dir=base_dir,
        allowed_hosts=allowed_hosts,
        limits=limits,
        licence=licence,
        status_map=status_map,
        sources=sources,
        pilot_areas=pilot_areas,
        area_precedence=area_precedence,
        boundary_disclosure=boundary_disclosure,
        region_envelope=region_envelope,
        selection=selection,
        release=release,
        publishing=publishing,
    )


def load_config(path: str | Path) -> Config:
    """Read, strictly parse and validate a configuration file.

    Local source paths are resolved relative to the configuration file's directory.
    """
    config_path = Path(path)
    location = str(config_path)
    try:
        raw = config_path.read_bytes()
    except OSError as exc:
        raise ConfigError(location, f"cannot read configuration: {exc.strerror or exc}") from exc
    if len(raw) > MAX_CONFIG_BYTES:
        raise ConfigError(location, "configuration file is too large")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(location, "configuration must be UTF-8") from exc
    try:
        data = parse_json_strict(text)
        return parse_config(data, base_dir=config_path.resolve().parent)
    except ConfigError as exc:
        raise ConfigError(f"{location}:{exc.location}", exc.message) from exc
