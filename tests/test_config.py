"""Configuration contract tests for the civic_publish_kit.config API.

Contract sources: src/civic_publish_kit/config.py, config/*.json and pyproject.toml.
All mutations and mocked responses below are test-only, not public capture fixtures.
The checked-in Surrey configuration is the baseline; its changing reviewed selection
and configured batch cap are deliberately not pinned.
"""

from __future__ import annotations

import hashlib
import re
import socket
from collections.abc import Callable
from dataclasses import FrozenInstanceError, fields, replace
from pathlib import Path
from typing import cast

import pytest
import requests
import responses

from civic_publish_kit.config import (
    MAX_CONFIG_BYTES,
    NORMALIZED_STATUSES,
    PILOT_AREA_IDS,
    PROJECT_FIELDS,
    Config,
    ConfigError,
    FieldMapping,
    Limits,
    SnapshotLayout,
    Source,
    canonical_json,
    load_config,
    parse_config,
    parse_json_strict,
    validate_arcgis_layer_url,
    validate_host,
    validate_https_url,
    validate_output_field_names,
    validate_where,
)

PathParts = tuple[str | int, ...]

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / "config" / "surrey.json"
LAYER_URL = (
    "https://services5.arcgis.com/YRpe0VKTJytZSSIB/"
    "arcgis/rest/services/Development%20Applications/FeatureServer/0"
)
HOSTS = ("services5.arcgis.com",)


@pytest.fixture(scope="session")
def repo_config() -> Config:
    return load_config(CONFIG_PATH)


@pytest.fixture
def document(repo_config: Config) -> dict[str, object]:
    """Return a fresh serialization of the real repository configuration."""
    return repo_config.to_dict()


def _object(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return cast(dict[str, object], value)


def _array(value: object) -> list[object]:
    assert isinstance(value, list)
    return cast(list[object], value)


def _at(root: object, path: PathParts) -> object:
    value = root
    for part in path:
        value = _object(value)[part] if isinstance(part, str) else _array(value)[part]
    return value


def _set(root: object, path: PathParts, value: object) -> None:
    assert path
    parent = _at(root, path[:-1])
    key = path[-1]
    if isinstance(key, str):
        _object(parent)[key] = value
    else:
        _array(parent)[key] = value


def _delete(root: object, path: PathParts) -> None:
    assert path
    parent = _at(root, path[:-1])
    key = path[-1]
    if isinstance(key, str):
        del _object(parent)[key]
    else:
        del _array(parent)[key]


def _expect_error(
    document: object,
    base_dir: Path,
    location: str,
    message: str,
) -> None:
    with pytest.raises(ConfigError, match=re.escape(message)) as caught:
        parse_config(document, base_dir=base_dir)
    assert caught.value.location == location
    assert message in caught.value.message


def _local_project(document: dict[str, object], kind: str) -> dict[str, object]:
    """Mutate only configuration; do not fabricate any public input records."""
    source = _object(_at(document, ("sources", 0)))
    source["kind"] = kind
    source.pop("url", None)
    source.pop("where", None)
    source["path"] = f"inputs/test-only-projects.{kind}"
    mapping = _object(source["mapping"])
    mapping.pop("longitude", None)
    mapping.pop("latitude", None)
    if kind == "csv":
        mapping["longitude"] = "TEST_ONLY_LONGITUDE"
        mapping["latitude"] = "TEST_ONLY_LATITUDE"
        _array(source["out_fields"]).extend(["TEST_ONLY_LONGITUDE", "TEST_ONLY_LATITUDE"])
    return source


def test_socket_constructor_is_denied() -> None:
    with pytest.raises(AssertionError, match="Real socket access is disabled"):
        socket.socket()


def test_socket_connection_is_denied() -> None:
    with pytest.raises(AssertionError, match="Real socket access is disabled"):
        socket.create_connection(("services5.arcgis.com", 443), timeout=0.01)


def test_dns_is_denied() -> None:
    with pytest.raises(AssertionError, match="Real socket access is disabled"):
        socket.getaddrinfo("services5.arcgis.com", 443)


def test_responses_requests_remain_usable(repo_config: Config) -> None:
    url = repo_config.project_source.require_url()
    with responses.RequestsMock(assert_all_requests_are_fired=True) as mocked:
        mocked.add(responses.GET, url, json={"test_only": True}, status=200)
        with requests.Session() as session:
            response = session.get(url, timeout=20)
        assert response.status_code == 200
        assert response.json() == {"test_only": True}
        assert len(mocked.calls) == 1


def test_repository_config_contract(repo_config: Config) -> None:
    assert repo_config.schema_version == 1
    assert repo_config.name == "surrey"
    assert repo_config.base_dir == CONFIG_PATH.parent.resolve()
    assert repo_config.allowed_hosts == HOSTS
    assert repo_config.project_source.id == "projects"
    assert repo_config.project_source.require_url() == LAYER_URL
    mapping = repo_config.project_source.require_mapping()
    assert mapping.project_id == "PROJECT_NO"
    assert mapping.source_record_id == "PROJECT_NO"
    assert mapping.title == "DESCRIPTION"
    assert mapping.status == "STATUS"
    assert set(mapping.source_fields()) <= set(repo_config.project_source.out_fields)
    assert repo_config.area_precedence == PILOT_AREA_IDS
    assert repo_config.region_envelope.to_list() == [-123.1, 48.9, -122.3, 49.4]
    assert repo_config.licence.reviewed
    assert repo_config.licence.name == "Open Government License \u2013 City of Surrey"
    assert repo_config.boundary_disclosure


def test_official_sources_and_filters(repo_config: Config) -> None:
    expected = {
        "projects": (
            "Development%20Applications",
            "STATUS IN ('Conditional Approval','Final Approval','Under Review','Initial Review')",
        ),
        "city_centre": ("City%20Centre%20Plan", "1=1"),
        "fleetwood": ("Town%20Centre%20Densities", "AREA_NAME='Fleetwood Town Centre'"),
        "campbell_heights": ("Campbell%20Heights%20Local%20Area%20Plan", "1=1"),
        "south_campbell_heights": ("South%20Campbell%20Heights%20Local%20Area%20Plan", "1=1"),
    }
    assert {source.id for source in repo_config.sources} == set(expected)
    for source_id, (service, where) in expected.items():
        source = repo_config.source(source_id)
        assert source.is_remote
        assert source.require_url() == (
            f"https://services5.arcgis.com/YRpe0VKTJytZSSIB/arcgis/rest/services/{service}/FeatureServer/0"
        )
        assert source.require_where() == where
    assert {source.id for source in repo_config.plan_sources} == set(expected) - {"projects"}
    assert repo_config.pilot_area("campbell_heights").source_ids == (
        "campbell_heights",
        "south_campbell_heights",
    )


@pytest.mark.parametrize(
    ("raw", "status", "label"),
    [
        ("Final Approval", "approved", "approved"),
        ("Conditional Approval", "conditionally_approved", "conditionally_approved"),
        ("Under Review", "unknown", "review"),
        ("Initial Review", "unknown", "review"),
    ],
)
def test_frozen_status_meanings(repo_config: Config, raw: str, status: str, label: str) -> None:
    rule = repo_config.status_map.get(raw)
    assert rule is not None
    assert rule.status == status
    assert rule.label == label
    assert rule.to_dict() == {"raw": raw, "status": status, "label": label}


@pytest.mark.parametrize(
    "raw",
    [
        "final approval",
        "FINAL APPROVAL",
        " Final Approval",
        "Final Approval ",
        "Conditional approval",
        "Under review",
        "Approved",
        "",
        "TEST-ONLY-UNMAPPED",
    ],
)
def test_status_lookup_is_exact(repo_config: Config, raw: str) -> None:
    assert repo_config.status_map.get(raw) is None


def test_status_map_helpers(repo_config: Config) -> None:
    rules = repo_config.status_map.rules
    assert repo_config.status_map.raw_values == tuple(rule.raw for rule in rules)
    assert repo_config.status_map.to_list() == [rule.to_dict() for rule in rules]


@pytest.mark.parametrize("status", NORMALIZED_STATUSES)
def test_explicit_nonreview_status_rules(
    document: dict[str, object],
    tmp_path: Path,
    status: str,
) -> None:
    document["status_map"] = [
        {"raw": "TEST-ONLY-EXACT-STATUS", "status": status, "label": "test_only"},
    ]
    config = parse_config(document, base_dir=tmp_path)
    rule = config.status_map.get("TEST-ONLY-EXACT-STATUS")
    assert rule is not None
    assert rule.status == status
    assert config.status_map.get("test-only-exact-status") is None


@pytest.mark.parametrize("raw", ["Under Review", "Initial Review", "UNDER REVIEW", "TEST-ONLY-Review"])
@pytest.mark.parametrize("status", ["approved", "conditionally_approved", "under_construction", "other"])
def test_review_cannot_be_promoted(
    document: dict[str, object],
    tmp_path: Path,
    raw: str,
    status: str,
) -> None:
    document["status_map"] = [{"raw": raw, "status": status, "label": "test_only"}]
    _expect_error(document, tmp_path, "$.status_map[0].status", "review statuses must map to unknown")


@pytest.mark.parametrize("status", ["approved", "conditionally_approved", "under_construction", "other"])
def test_review_label_cannot_claim_another_status(
    document: dict[str, object],
    tmp_path: Path,
    status: str,
) -> None:
    document["status_map"] = [{"raw": "TEST-ONLY-STATUS", "status": status, "label": "review"}]
    _expect_error(document, tmp_path, "$.status_map[0].label", "review label is only valid")


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("", "malformed JSON"),
        ("{", "malformed JSON"),
        ('{"a": 1,}', "malformed JSON"),
        ('{"a": 1} trailing', "malformed JSON"),
        ('{"a": 1, "a": 2}', "duplicate JSON key"),
        ('{"outer": {"a": 1, "a": 2}}', "duplicate JSON key"),
        ('[{"a": 1, "a": 2}]', "duplicate JSON key"),
        ('{"a": 1, "\\u0061": 2}', "duplicate JSON key"),
        ("NaN", "non-finite JSON number"),
        ("Infinity", "non-finite JSON number"),
        ("-Infinity", "non-finite JSON number"),
        ('{"nested": [NaN]}', "non-finite JSON number"),
    ],
)
def test_strict_json_rejections(text: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message) as caught:
        parse_json_strict(text)
    assert caught.value.location == "$"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1, "b": [true, null, "é"]}', {"a": 1, "b": [True, None, "é"]}),
        ('[{"a": 1}, {"a": 2}]', [{"a": 1}, {"a": 2}]),
        ("null", None),
        ("42", 42),
        ('"text"', "text"),
    ],
)
def test_strict_json_valid_values(text: str, expected: object) -> None:
    assert parse_json_strict(text) == expected


def test_config_error_public_api() -> None:
    error = ConfigError("$.test_only", "bad value")
    assert isinstance(error, ValueError)
    assert error.location == "$.test_only"
    assert error.message == "bad value"
    assert str(error) == "$.test_only: bad value"


@pytest.mark.parametrize("value", [None, [], "test-only", 1, True])
def test_config_requires_object(value: object, tmp_path: Path) -> None:
    _expect_error(value, tmp_path, "$", "must be a JSON object")


@pytest.mark.parametrize(
    "path",
    [
        ("schema_version",),
        ("name",),
        ("title",),
        ("allowed_hosts",),
        ("licence",),
        ("licence", "name"),
        ("licence", "url"),
        ("licence", "attribution"),
        ("licence", "reviewed"),
        ("status_map",),
        ("status_map", 0, "raw"),
        ("status_map", 0, "status"),
        ("status_map", 0, "label"),
        ("sources",),
        ("sources", 0, "id"),
        ("sources", 0, "kind"),
        ("sources", 0, "role"),
        ("sources", 0, "out_fields"),
        ("sources", 0, "mapping", "project_id"),
        ("sources", 0, "mapping", "source_record_id"),
        ("sources", 0, "mapping", "title"),
        ("sources", 0, "mapping", "status"),
        ("pilot_areas",),
        ("pilot_areas", 0, "id"),
        ("pilot_areas", 0, "title"),
        ("pilot_areas", 0, "source_ids"),
        ("area_precedence",),
        ("boundary_disclosure",),
        ("region_envelope",),
        ("selection",),
        ("selection", "reviewed_project_ids"),
        ("selection", "note"),
        ("release",),
        ("release", "include_sites"),
        ("publishing",),
        ("publishing", "item_title"),
        ("publishing", "summary"),
        ("publishing", "tags"),
    ],
)
def test_required_fields(document: dict[str, object], tmp_path: Path, path: PathParts) -> None:
    _delete(document, path)
    location = "$"
    for part in path:
        location += f".{part}" if isinstance(part, str) else f"[{part}]"
    _expect_error(document, tmp_path, location, "is required")


@pytest.mark.parametrize(
    ("path", "location"),
    [
        ((), "$"),
        (("limits",), "$.limits"),
        (("licence",), "$.licence"),
        (("status_map", 0), "$.status_map[0]"),
        (("sources", 0), "$.sources[0]"),
        (("sources", 0, "mapping"), "$.sources[0].mapping"),
        (("pilot_areas", 0), "$.pilot_areas[0]"),
        (("selection",), "$.selection"),
        (("release",), "$.release"),
        (("publishing",), "$.publishing"),
    ],
)
def test_unknown_keys_are_rejected(
    document: dict[str, object],
    tmp_path: Path,
    path: PathParts,
    location: str,
) -> None:
    _object(_at(document, path))["test_only_unknown_key"] = True
    _expect_error(document, tmp_path, location, "unknown keys: test_only_unknown_key")


@pytest.mark.parametrize("value", [None, 1, True, [], {}, "", " padded", "padded ", "a\nb", "a\x7fb"])
def test_required_text_validation(document: dict[str, object], tmp_path: Path, value: object) -> None:
    document["title"] = value
    with pytest.raises(ConfigError, match=r"string|non-empty|control characters") as caught:
        parse_config(document, base_dir=tmp_path)
    assert caught.value.location == "$.title"


@pytest.mark.parametrize(
    ("path", "value", "location", "message"),
    [
        (("schema_version",), True, "$.schema_version", "must be an integer"),
        (("schema_version",), 1.0, "$.schema_version", "must be an integer"),
        (("schema_version",), 2, "$.schema_version", "unsupported version"),
        (("name",), "Surrey", "$.name", "must match"),
        (("name",), "a" * 32, "$.name", "must match"),
        (("allowed_hosts",), [], "$.allowed_hosts", "at least one host"),
        (("allowed_hosts",), list(HOSTS) * 2, "$.allowed_hosts", "duplicate value"),
        (("allowed_hosts",), HOSTS[0], "$.allowed_hosts", "must be a JSON array"),
        (("allowed_hosts",), [1], "$.allowed_hosts[0]", "must be a string"),
        (("licence",), [], "$.licence", "must be a JSON object"),
        (("licence", "reviewed"), 1, "$.licence.reviewed", "must be true or false"),
        (("licence", "review_note"), 1, "$.licence.review_note", "must be a string"),
        (("status_map",), [], "$.status_map", "at least one explicit status rule"),
        (("status_map",), {}, "$.status_map", "must be a JSON array"),
        (("status_map",), [None], "$.status_map[0]", "must be a JSON object"),
        (("status_map", 0, "status"), "Approved", "$.status_map[0].status", "must be one of"),
        (("status_map", 0, "label"), "Review Label", "$.status_map[0].label", "must match"),
        (("sources", 0, "id"), "../projects", "$.sources[0].id", "must match"),
        (("sources", 0, "kind"), "http", "$.sources[0].kind", "must be one of"),
        (("sources", 0, "role"), "buildings", "$.sources[0].role", "must be one of"),
        (("sources", 0, "out_fields"), ["*"], "$.sources[0].out_fields[0]", "plain field name"),
        (("sources", 0, "out_fields"), ["STATUS", "STATUS"], "$.sources[0].out_fields", "duplicate"),
        (("sources", 0, "mapping"), None, "$.sources[0].mapping", "explicit field mapping"),
        (("sources", 0, "mapping", "project_id"), "a.b", "$.sources[0].mapping.project_id", "plain field"),
        (("sources", 0, "mapping", "title"), "a" * 65, "$.sources[0].mapping.title", "plain field"),
        (
            ("sources", 0, "mapping", "status_evidence_url"),
            "*",
            "$.sources[0].mapping.status_evidence_url",
            "plain field",
        ),
        (("sources", 0, "out_fields"), [], "$.sources[0].out_fields", "mapped fields not allowlisted"),
        (("pilot_areas", 0, "id"), "other", "$.pilot_areas[0].id", "must be one of"),
        (("pilot_areas", 0, "source_ids"), [], "$.pilot_areas[0].source_ids", "at least one plan source"),
        (("pilot_areas", 0, "source_ids"), ["projects"], "$.pilot_areas[0].source_ids", "not a plan_area source"),
        (("pilot_areas", 0, "source_ids"), ["missing"], "$.pilot_areas[0].source_ids", "not a plan_area source"),
        (("pilot_areas",), [], "$.pilot_areas", "must define exactly"),
        (("area_precedence",), [], "$.area_precedence", "must order every pilot area"),
        (("area_precedence",), ["city_centre"] * 3, "$.area_precedence", "duplicate value"),
        (("area_precedence",), ["other"], "$.area_precedence", "must be one of"),
        (("selection", "reviewed_project_ids"), ["TEST-ONLY-A"] * 2, "$.selection.reviewed_project_ids", "duplicate"),
        (("selection", "reviewed_project_ids"), [1], "$.selection.reviewed_project_ids[0]", "must be a string"),
        (("selection", "reviewed_project_ids"), [" TEST-ONLY-A"], "$.selection.reviewed_project_ids[0]", "whitespace"),
        (("release", "include_sites"), 1, "$.release.include_sites", "must be true or false"),
        (("publishing", "tags"), [], "$.publishing.tags", "at least one tag"),
        (("publishing", "tags"), ["test-only"] * 2, "$.publishing.tags", "duplicate value"),
    ],
)
def test_config_invalid_values(
    document: dict[str, object],
    tmp_path: Path,
    path: PathParts,
    value: object,
    location: str,
    message: str,
) -> None:
    _set(document, path, value)
    _expect_error(document, tmp_path, location, message)


@pytest.mark.parametrize(
    ("key", "maximum"),
    [
        ("timeout_seconds", 20),
        ("max_retries", 2),
        ("max_records", 20_000),
        ("max_response_bytes", 25_000_000),
        ("max_batch_size", 500),
    ],
)
@pytest.mark.parametrize("side", ["below", "above", "bool", "string", "null"])
def test_limit_rejections(
    document: dict[str, object],
    tmp_path: Path,
    key: str,
    maximum: int,
    side: str,
) -> None:
    values: dict[str, object] = {
        "below": -1 if key == "max_retries" else 0,
        "above": maximum + 1,
        "bool": True,
        "string": "1",
        "null": None,
    }
    _object(document["limits"])[key] = values[side]
    with pytest.raises(ConfigError, match="must be") as caught:
        parse_config(document, base_dir=tmp_path)
    assert caught.value.location == f"$.limits.{key}"


@pytest.mark.parametrize("key", ["max_retries", "max_records", "max_response_bytes", "max_batch_size"])
def test_integer_limits_reject_floats(document: dict[str, object], tmp_path: Path, key: str) -> None:
    _object(document["limits"])[key] = 1.5
    _expect_error(document, tmp_path, f"$.limits.{key}", "must be an integer")


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_timeout(document: dict[str, object], tmp_path: Path, value: float) -> None:
    _object(document["limits"])["timeout_seconds"] = value
    _expect_error(document, tmp_path, "$.limits.timeout_seconds", "must be finite")


@pytest.mark.parametrize("mode", ["omitted", "null", "empty"])
def test_limit_defaults(document: dict[str, object], tmp_path: Path, mode: str) -> None:
    if mode == "omitted":
        document.pop("limits")
    else:
        document["limits"] = None if mode == "null" else {}
    assert parse_config(document, base_dir=tmp_path).limits == Limits()


@pytest.mark.parametrize(
    "limits",
    [
        Limits(),
        Limits(timeout_seconds=0.01, max_retries=0, max_records=1, max_response_bytes=1, max_batch_size=1),
        Limits(timeout_seconds=5, max_retries=1, max_records=100, max_response_bytes=1024, max_batch_size=25),
    ],
)
def test_valid_limit_boundaries(
    document: dict[str, object],
    tmp_path: Path,
    limits: Limits,
) -> None:
    document["limits"] = limits.to_dict()
    assert parse_config(document, base_dir=tmp_path).limits == limits


@pytest.mark.parametrize(("maximum", "expected"), [(1, 1), (24, 24), (25, 25), (26, 25), (1203, 25)])
def test_batch_size_caps_layer_limit(maximum: int, expected: int) -> None:
    assert replace(Limits(), max_batch_size=25).batch_size(maximum) == expected


@pytest.mark.parametrize("maximum", [0, -1])
def test_batch_size_rejects_nonpositive_layer_limit(maximum: int) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        Limits().batch_size(maximum)


@pytest.mark.parametrize(
    "host",
    ["services5.arcgis.com", "example.org", "a-b.example.org", "a" * 63 + ".example.org"],
)
def test_allowed_host_validation(host: str) -> None:
    assert validate_host(host) == host


@pytest.mark.parametrize(
    "host",
    [
        "",
        "Services5.arcgis.com",
        "*.arcgis.com",
        "services5.arcgis.com:443",
        "https://services5.arcgis.com",
        "127.0.0.1",
        "[::1]",
        "localhost",
        "example.org.",
        "-bad.example.org",
        "bad-.example.org",
        "bad_name.example.org",
        "a" * 64 + ".example.org",
        " example.org",
    ],
)
def test_invalid_allowed_hosts(host: str) -> None:
    with pytest.raises(ConfigError, match="invalid allowlisted host") as caught:
        validate_host(host, "$.test_only_host")
    assert caught.value.location == "$.test_only_host"


@pytest.mark.parametrize(
    "url",
    [
        "https://example.org",
        "https://example.org/licence",
        "https://example.org/licence?version=1",
    ],
)
def test_general_https_urls(url: str) -> None:
    assert validate_https_url(url) == url


@pytest.mark.parametrize(
    ("url", "message"),
    [
        ("http://example.org", "must use https"),
        ("ftp://example.org", "must use https"),
        ("https:///licence", "include a host"),
        ("https://user@example.org/licence", "embed credentials"),
        ("https://user:password@example.org/licence", "embed credentials"),
        ("https://example.org/licence#section", "include a fragment"),
        ("https://example.org/a b", "contain whitespace"),
        ("https://example.org/a\nb", "contain whitespace"),
    ],
)
def test_invalid_general_https_urls(url: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message) as caught:
        validate_https_url(url, "$.test_only_url")
    assert caught.value.location == "$.test_only_url"


@pytest.mark.parametrize(
    "path",
    [
        "/arcgis/rest/services/TestOnly/FeatureServer/0",
        "/arcgis/rest/services/TestOnly/MapServer/1",
        "/arcgis/rest/services/Folder/TestOnly/FeatureServer/999999",
        "/prefix/arcgis/rest/services/Test%20Only/FeatureServer/12",
    ],
)
def test_exact_layer_url_forms(path: str) -> None:
    url = f"https://services5.arcgis.com{path}"
    assert validate_arcgis_layer_url(url, iter(HOSTS)) == url


@pytest.mark.parametrize(
    ("url", "message"),
    [
        (LAYER_URL.replace("https:", "http:"), "must use https"),
        (LAYER_URL.replace("services5.arcgis.com", "other.example.org"), "not allowlisted"),
        (LAYER_URL.replace("services5.arcgis.com", "services5.arcgis.com.evil.org"), "not allowlisted"),
        (LAYER_URL.replace("services5.arcgis.com", "SERVICES5.ARCGIS.COM"), "lowercase with no port"),
        (LAYER_URL.replace("services5.arcgis.com", "services5.arcgis.com:443"), "lowercase with no port"),
        (LAYER_URL.replace("services5.arcgis.com", "user:password@services5.arcgis.com"), "embed credentials"),
        (LAYER_URL + "?token=secret", "query string"),
        (LAYER_URL + "?api_key=secret", "query string"),
        (LAYER_URL + "?f=json", "query string"),
        (LAYER_URL + "#metadata", "fragment"),
        (LAYER_URL + "/", "exact"),
        (LAYER_URL + "/query", "exact"),
        (LAYER_URL.rsplit("/", 1)[0], "exact"),
        (LAYER_URL.rsplit("/", 1)[0] + "/01", "exact"),
        (LAYER_URL.rsplit("/", 1)[0] + "/-1", "exact"),
        (LAYER_URL.rsplit("/", 1)[0] + "/1000000", "exact"),
        (LAYER_URL.replace("FeatureServer", "ImageServer"), "exact"),
        (LAYER_URL.replace("Development%20Applications", ".."), "traversal"),
        (LAYER_URL.replace("Development%20Applications", "%2e%2e"), "traversal"),
        (LAYER_URL.replace("Development%20Applications", "."), "traversal"),
        (LAYER_URL.replace("Development%20Applications", "Test%2fOnly"), "encoded path separators"),
        (LAYER_URL.replace("Development%20Applications", "Test%2FOnly"), "encoded path separators"),
        (LAYER_URL.replace("Development%20Applications", "Test%5cOnly"), "encoded path separators"),
        (LAYER_URL.replace("Development%20Applications", "Test%00Only"), "encoded path separators"),
        (LAYER_URL.replace("Development%20Applications", "Test%01Only"), "control characters"),
        (LAYER_URL.replace("Development%20Applications", "Test Only"), "whitespace"),
        (LAYER_URL.replace("/arcgis/", "/../arcgis/"), "exact"),
    ],
)
def test_unsafe_layer_urls(url: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message) as caught:
        validate_arcgis_layer_url(url, HOSTS, "$.sources[0].url")
    assert caught.value.location == "$.sources[0].url"


@pytest.mark.parametrize("where", ["1=1", "STATUS='Final Approval'", "x" * 2000])
def test_configured_where_is_preserved(where: str) -> None:
    assert validate_where(where) == where


@pytest.mark.parametrize("where", ["1=1; DROP TABLE x", "1=1 -- comment", "/* comment */ 1=1", "1=1 */", "x" * 2001])
def test_unsafe_where_is_rejected(where: str) -> None:
    with pytest.raises(ConfigError, match="where clause") as caught:
        validate_where(where, "$.test_only_where")
    assert caught.value.location == "$.test_only_where"


def test_output_field_contract() -> None:
    expected = (
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
    assert expected == PROJECT_FIELDS
    assert validate_output_field_names(iter(expected)) == expected
    assert validate_output_field_names(["a", "a" * 31, "test_only_1"]) == ("a", "a" * 31, "test_only_1")


@pytest.mark.parametrize(
    ("names", "message"),
    [
        (["title", "title"], "duplicate value"),
        ([""], "must match"),
        (["Title"], "must match"),
        (["_title"], "must match"),
        (["1title"], "must match"),
        (["a" * 32], "must match"),
        (["title-name"], "must match"),
        (["objectid"], "reserved name"),
        (["shape__area"], "reserved name"),
        (["globalid"], "reserved name"),
        (["select"], "reserved name"),
        (["geometry"], "reserved name"),
    ],
)
def test_invalid_output_fields(names: list[str], message: str) -> None:
    with pytest.raises(ConfigError, match=message) as caught:
        validate_output_field_names(names)
    assert caught.value.location == "output_fields"


@pytest.mark.parametrize("key", ["url", "where"])
@pytest.mark.parametrize("mode", ["missing", "null"])
def test_arcgis_requires_url_and_where(
    document: dict[str, object],
    tmp_path: Path,
    key: str,
    mode: str,
) -> None:
    source = _object(_at(document, ("sources", 0)))
    if mode == "missing":
        source.pop(key)
    else:
        source[key] = None
    _expect_error(document, tmp_path, "$.sources[0].url", "arcgis sources require url and where")


def test_arcgis_disallows_local_path(document: dict[str, object], tmp_path: Path) -> None:
    _set(document, ("sources", 0, "path"), "inputs/test-only.geojson")
    _expect_error(document, tmp_path, "$.sources[0].path", "must not set a local path")


@pytest.mark.parametrize("kind", ["csv", "geojson"])
def test_local_project_configuration(document: dict[str, object], tmp_path: Path, kind: str) -> None:
    _local_project(document, kind)
    config = parse_config(document, base_dir=tmp_path)
    source = config.project_source
    assert not source.is_remote
    assert source.require_path() == f"inputs/test-only-projects.{kind}"
    assert source.url is None
    assert source.where is None
    assert config.resolve_local_path(source) == tmp_path / source.require_path()
    if kind == "csv":
        assert source.require_mapping().longitude == "TEST_ONLY_LONGITUDE"
        assert source.require_mapping().latitude == "TEST_ONLY_LATITUDE"


@pytest.mark.parametrize("kind", ["csv", "geojson"])
def test_local_source_requires_path(document: dict[str, object], tmp_path: Path, kind: str) -> None:
    source = _local_project(document, kind)
    source.pop("path")
    _expect_error(document, tmp_path, "$.sources[0].path", "require a local path")


@pytest.mark.parametrize("kind", ["csv", "geojson"])
@pytest.mark.parametrize("key", ["url", "where"])
def test_local_source_disallows_remote_options(
    document: dict[str, object],
    tmp_path: Path,
    kind: str,
    key: str,
) -> None:
    source = _local_project(document, kind)
    source[key] = LAYER_URL if key == "url" else "1=1"
    _expect_error(document, tmp_path, "$.sources[0].url", "local sources must not set url or where")


@pytest.mark.parametrize(
    "path",
    ["/absolute.csv", "../escape.csv", "a/../../escape.csv", "./input.csv", "a//b.csv", "a/", r"a\b.csv"],
)
def test_local_config_path_safety(document: dict[str, object], tmp_path: Path, path: str) -> None:
    source = _local_project(document, "csv")
    source["path"] = path
    with pytest.raises(ConfigError, match=r"paths|relative without traversal") as caught:
        parse_config(document, base_dir=tmp_path)
    assert caught.value.location == "$.sources[0].path"


@pytest.mark.parametrize("removed", [("longitude",), ("latitude",), ("longitude", "latitude")])
def test_csv_requires_both_coordinates(
    document: dict[str, object],
    tmp_path: Path,
    removed: tuple[str, ...],
) -> None:
    source = _local_project(document, "csv")
    mapping = _object(source["mapping"])
    for key in removed:
        mapping.pop(key)
    _expect_error(document, tmp_path, "$.sources[0].mapping", "explicit longitude and latitude")


@pytest.mark.parametrize("kind", ["arcgis", "geojson"])
@pytest.mark.parametrize("coordinate", ["longitude", "latitude"])
def test_non_csv_disallows_coordinate_mapping(
    document: dict[str, object],
    tmp_path: Path,
    kind: str,
    coordinate: str,
) -> None:
    source = (
        _local_project(document, kind)
        if kind == "geojson"
        else _object(_at(document, ("sources", 0)))
    )
    _object(source["mapping"])[coordinate] = "TEST_ONLY_COORDINATE"
    _expect_error(document, tmp_path, "$.sources[0].mapping", "only valid for csv")


@pytest.mark.parametrize("mode", ["omitted", "null"])
def test_optional_evidence_mapping(
    document: dict[str, object],
    tmp_path: Path,
    mode: str,
) -> None:
    mapping = _object(_at(document, ("sources", 0, "mapping")))
    if mode == "omitted":
        mapping.pop("status_evidence_url", None)
    else:
        mapping["status_evidence_url"] = None
    config = parse_config(document, base_dir=tmp_path)
    result = config.project_source.require_mapping()
    assert result.status_evidence_url is None
    assert "status_evidence_url" not in result.to_dict()


def test_optional_mapping_must_be_allowlisted(document: dict[str, object], tmp_path: Path) -> None:
    _set(document, ("sources", 0, "mapping", "status_evidence_url"), "TEST_ONLY_EVIDENCE")
    _expect_error(document, tmp_path, "$.sources[0].out_fields", "mapped fields not allowlisted")


def test_field_mapping_deduplicates_in_order(repo_config: Config) -> None:
    original = repo_config.project_source.require_mapping()
    minimal = replace(original, status_evidence_url=None, longitude=None, latitude=None)
    assert minimal.source_fields() == ("PROJECT_NO", "DESCRIPTION", "STATUS")
    assert minimal.to_dict() == {
        "project_id": "PROJECT_NO",
        "source_record_id": "PROJECT_NO",
        "title": "DESCRIPTION",
        "status": "STATUS",
    }
    complete = replace(
        minimal,
        status_evidence_url="TEST_ONLY_EVIDENCE",
        longitude="TEST_ONLY_LONGITUDE",
        latitude="TEST_ONLY_LATITUDE",
    )
    assert complete.source_fields() == (
        "PROJECT_NO",
        "DESCRIPTION",
        "STATUS",
        "TEST_ONLY_EVIDENCE",
        "TEST_ONLY_LONGITUDE",
        "TEST_ONLY_LATITUDE",
    )
    assert complete.to_dict() == {
        **minimal.to_dict(),
        "status_evidence_url": "TEST_ONLY_EVIDENCE",
        "longitude": "TEST_ONLY_LONGITUDE",
        "latitude": "TEST_ONLY_LATITUDE",
    }


@pytest.mark.parametrize("kind", ["arcgis", "geojson"])
def test_plan_sources_disallow_project_mapping(
    document: dict[str, object],
    tmp_path: Path,
    kind: str,
) -> None:
    source = _object(_at(document, ("sources", 1)))
    if kind == "geojson":
        source["kind"] = kind
        source.pop("url")
        source.pop("where")
        source["path"] = "inputs/test-only-plan.geojson"
    source["mapping"] = _object(_at(document, ("sources", 0, "mapping"))).copy()
    _expect_error(document, tmp_path, "$.sources[1].mapping", "must not define a project mapping")


def test_plan_sources_disallow_csv(document: dict[str, object], tmp_path: Path) -> None:
    source = _object(_at(document, ("sources", 1)))
    source["kind"] = "csv"
    source.pop("url")
    source.pop("where")
    source["path"] = "inputs/test-only-plan.csv"
    _expect_error(document, tmp_path, "$.sources[1].kind", "require polygon geometry")


def test_local_geojson_plan_is_valid(document: dict[str, object], tmp_path: Path) -> None:
    source = _object(_at(document, ("sources", 1)))
    source["kind"] = "geojson"
    source.pop("url")
    source.pop("where")
    source["path"] = "inputs/test-only-plan.geojson"
    result = parse_config(document, base_dir=tmp_path).source("city_centre")
    assert result.role == "plan_area"
    assert result.mapping is None
    assert result.require_path() == "inputs/test-only-plan.geojson"
    assert not result.is_remote


@pytest.mark.parametrize("mode", ["none", "two", "duplicate_id"])
def test_source_cardinality(document: dict[str, object], tmp_path: Path, mode: str) -> None:
    sources = _array(document["sources"])
    if mode == "none":
        sources.pop(0)
    else:
        duplicate = _object(sources[0]).copy()
        if mode == "two":
            duplicate["id"] = "test_only_second_project"
        sources.append(duplicate)
    message = "duplicate value" if mode == "duplicate_id" else "exactly one projects source"
    _expect_error(document, tmp_path, "$.sources", message)


def test_duplicate_raw_status_is_rejected(document: dict[str, object], tmp_path: Path) -> None:
    rules = _array(document["status_map"])
    rules.append(_object(rules[0]).copy())
    _expect_error(document, tmp_path, "$.status_map", "duplicate value")


@pytest.mark.parametrize("mode", ["duplicate_area", "missing_area", "shared_source", "unused_source"])
def test_area_relationship_errors(document: dict[str, object], tmp_path: Path, mode: str) -> None:
    areas = _array(document["pilot_areas"])
    location = "$.pilot_areas"
    if mode == "duplicate_area":
        areas.append(_object(areas[0]).copy())
        message = "duplicate value"
    elif mode == "missing_area":
        areas.pop(0)
        message = "must define exactly"
    elif mode == "shared_source":
        _array(_object(areas[1])["source_ids"]).append("city_centre")
        location += ".source_ids"
        message = "duplicate value"
    else:
        _set(document, ("pilot_areas", 2, "source_ids"), ["campbell_heights"])
        message = "plan sources not assigned"
    _expect_error(document, tmp_path, location, message)


def test_duplicate_source_within_area(document: dict[str, object], tmp_path: Path) -> None:
    _set(document, ("pilot_areas", 0, "source_ids"), ["city_centre", "city_centre"])
    _expect_error(document, tmp_path, "$.pilot_areas.source_ids", "duplicate value")


def test_area_helpers_and_configured_precedence(document: dict[str, object], tmp_path: Path) -> None:
    document["area_precedence"] = ["campbell_heights", "fleetwood", "city_centre"]
    config = parse_config(document, base_dir=tmp_path)
    assert tuple(area.id for area in config.pilot_areas_by_precedence()) == (
        "campbell_heights",
        "fleetwood",
        "city_centre",
    )
    for area in config.pilot_areas:
        assert config.pilot_area(area.id) is area
        assert area.to_dict() == {
            "id": area.id,
            "title": area.title,
            "source_ids": list(area.source_ids),
        }
        for source_id in area.source_ids:
            assert config.pilot_area_for_source(source_id) is area
            assert config.source(source_id).role == "plan_area"


@pytest.mark.parametrize("helper", ["source", "pilot_area", "pilot_area_for_source"])
def test_lookup_helpers_reject_unknown_ids(repo_config: Config, helper: str) -> None:
    lookups: dict[str, Callable[[str], object]] = {
        "source": repo_config.source,
        "pilot_area": repo_config.pilot_area,
        "pilot_area_for_source": repo_config.pilot_area_for_source,
    }
    lookup = lookups[helper]
    with pytest.raises(KeyError, match="test_only_missing"):
        lookup("test_only_missing")


def test_project_source_is_not_an_area_source(repo_config: Config) -> None:
    with pytest.raises(KeyError, match="projects"):
        repo_config.pilot_area_for_source(repo_config.project_source.id)


@pytest.mark.parametrize(
    ("values", "message"),
    [
        ([], "must be"),
        ([-123.1, 48.9, -122.3], "must be"),
        ([-123.1, 48.9, -122.3, 49.4, 0], "must be"),
        ([-181, 48.9, -122.3, 49.4], "longitude"),
        ([-123.1, 48.9, 181, 49.4], "longitude"),
        ([-122.3, 48.9, -123.1, 49.4], "longitude"),
        ([-123.1, 48.9, -123.1, 49.4], "longitude"),
        ([-123.1, -91, -122.3, 49.4], "latitude"),
        ([-123.1, 48.9, -122.3, 91], "latitude"),
        ([-123.1, 49.4, -122.3, 48.9], "latitude"),
        ([-123.1, 49.4, -122.3, 49.4], "latitude"),
    ],
)
def test_envelope_ranges(
    document: dict[str, object],
    tmp_path: Path,
    values: list[float],
    message: str,
) -> None:
    document["region_envelope"] = values
    _expect_error(document, tmp_path, "$.region_envelope", message)


@pytest.mark.parametrize("value", [True, "49", None, float("nan"), float("inf"), float("-inf")])
def test_envelope_requires_finite_numbers(
    document: dict[str, object],
    tmp_path: Path,
    value: object,
) -> None:
    _set(document, ("region_envelope", 0), value)
    with pytest.raises(ConfigError, match=r"must be a number|must be finite") as caught:
        parse_config(document, base_dir=tmp_path)
    assert caught.value.location == "$.region_envelope[0]"


def test_envelope_helpers(repo_config: Config) -> None:
    envelope = repo_config.region_envelope
    assert envelope.contains(envelope.min_lon, envelope.min_lat)
    assert envelope.contains(envelope.max_lon, envelope.max_lat)
    assert envelope.contains(
        (envelope.min_lon + envelope.max_lon) / 2,
        (envelope.min_lat + envelope.max_lat) / 2,
    )
    assert not envelope.contains(envelope.min_lon - 0.01, envelope.min_lat)
    assert not envelope.contains(envelope.max_lon + 0.01, envelope.max_lat)
    assert not envelope.contains(envelope.min_lon, envelope.min_lat - 0.01)
    assert not envelope.contains(envelope.max_lon, envelope.max_lat + 0.01)
    assert not envelope.contains(float("nan"), envelope.min_lat)
    assert envelope.to_list() == [
        envelope.min_lon,
        envelope.min_lat,
        envelope.max_lon,
        envelope.max_lat,
    ]


def test_global_envelope_boundaries(document: dict[str, object], tmp_path: Path) -> None:
    document["region_envelope"] = [-180, -90, 180, 90]
    envelope = parse_config(document, base_dir=tmp_path).region_envelope
    assert envelope.contains(-180, -90)
    assert envelope.contains(180, 90)


def test_explicit_selection_is_sorted_and_exact(document: dict[str, object], tmp_path: Path) -> None:
    _set(document, ("selection", "reviewed_project_ids"), ["TEST-ONLY-B", "TEST-ONLY-A"])
    config = parse_config(document, base_dir=tmp_path)
    selection = config.selection
    assert selection.reviewed_project_ids == ("TEST-ONLY-A", "TEST-ONLY-B")
    assert selection.is_selected("TEST-ONLY-A")
    assert not selection.is_selected("test-only-a")
    assert not selection.is_selected(" TEST-ONLY-A")
    assert not selection.is_selected("TEST-ONLY-C")
    assert selection.to_dict() == {
        "reviewed_project_ids": ["TEST-ONLY-A", "TEST-ONLY-B"],
        "note": selection.note,
    }


@pytest.mark.parametrize("reviewed", [False, True])
@pytest.mark.parametrize("selected", [False, True])
def test_independent_public_release_blockers(
    repo_config: Config,
    reviewed: bool,
    selected: bool,
) -> None:
    selection = replace(
        repo_config.selection,
        reviewed_project_ids=("TEST-ONLY-A",) if selected else (),
    )
    config = replace(
        repo_config,
        licence=replace(repo_config.licence, reviewed=reviewed),
        selection=selection,
    )
    expected: list[str] = []
    if not reviewed:
        expected.append("licence has not been reviewed in configuration")
    if not selected:
        expected.append("no reviewed project IDs are selected; all records are excluded")
    assert config.release_blockers() == tuple(expected)
    assert selection.is_selected("TEST-ONLY-A") is selected
    assert not selection.is_selected("TEST-ONLY-UNSELECTED")


def test_unreviewed_licence_loads_but_blocks(document: dict[str, object], tmp_path: Path) -> None:
    _set(document, ("licence", "reviewed"), False)
    _set(document, ("selection", "reviewed_project_ids"), ["TEST-ONLY-A"])
    config = parse_config(document, base_dir=tmp_path)
    assert config.release_blockers() == ("licence has not been reviewed in configuration",)


@pytest.mark.parametrize("mode", ["omitted", "null"])
def test_optional_licence_review_note(document: dict[str, object], tmp_path: Path, mode: str) -> None:
    licence = _object(document["licence"])
    if mode == "omitted":
        licence.pop("review_note", None)
    else:
        licence["review_note"] = None
    result = parse_config(document, base_dir=tmp_path).licence
    assert result.review_note is None
    assert "review_note" not in result.to_dict()


def test_licence_release_and_publishing_serialization(repo_config: Config) -> None:
    licence = replace(repo_config.licence, review_note="TEST-ONLY review note")
    assert licence.to_dict() == {
        "name": licence.name,
        "url": licence.url,
        "attribution": licence.attribution,
        "reviewed": licence.reviewed,
        "review_note": "TEST-ONLY review note",
    }
    for include_sites in (False, True):
        assert replace(repo_config.release, include_sites=include_sites).to_dict() == {
            "include_sites": include_sites,
        }
    publishing = repo_config.publishing
    assert publishing.to_dict() == {
        "item_title": publishing.item_title,
        "summary": publishing.summary,
        "tags": list(publishing.tags),
    }


def test_frozen_dataclasses(repo_config: Config, tmp_path: Path) -> None:
    instances = (
        repo_config,
        repo_config.limits,
        repo_config.licence,
        repo_config.status_map,
        repo_config.status_map.rules[0],
        repo_config.project_source.require_mapping(),
        repo_config.project_source,
        repo_config.pilot_areas[0],
        repo_config.region_envelope,
        repo_config.selection,
        repo_config.release,
        repo_config.publishing,
        SnapshotLayout(tmp_path),
    )
    for instance in instances:
        field = fields(instance)[0]
        original = getattr(instance, field.name)
        with pytest.raises(FrozenInstanceError, match="cannot assign"):
            setattr(instance, field.name, original)


@pytest.mark.parametrize("helper", ["require_url", "require_where", "require_path", "require_mapping"])
def test_source_require_helpers_report_missing_values(repo_config: Config, helper: str) -> None:
    source = replace(repo_config.project_source, url=None, where=None, path=None, mapping=None)
    key = helper.removeprefix("require_")
    requirements: dict[str, Callable[[], object]] = {
        "require_url": source.require_url,
        "require_where": source.require_where,
        "require_path": source.require_path,
        "require_mapping": source.require_mapping,
    }
    requirement = requirements[helper]
    with pytest.raises(ConfigError, match="is required") as caught:
        requirement()
    assert caught.value.location == f"sources.projects.{key}"


def test_source_serialization_variants(repo_config: Config) -> None:
    original = repo_config.project_source
    remote = original.to_dict()
    assert remote["id"] == original.id
    assert remote["url"] == original.require_url()
    assert remote["where"] == original.require_where()
    assert remote["out_fields"] == list(original.out_fields)
    assert remote["mapping"] == original.require_mapping().to_dict()
    assert "path" not in remote

    local = replace(original, kind="geojson", url=None, where=None, path="inputs/test-only.geojson")
    assert local.require_path() == "inputs/test-only.geojson"
    assert not local.is_remote
    assert local.to_dict() == {
        "id": original.id,
        "kind": "geojson",
        "role": original.role,
        "path": "inputs/test-only.geojson",
        "out_fields": list(original.out_fields),
        "mapping": original.require_mapping().to_dict(),
    }
    plan = repo_config.plan_sources[0]
    assert "mapping" not in plan.to_dict()


def test_config_round_trip(repo_config: Config, tmp_path: Path) -> None:
    document = repo_config.to_dict()
    rebuilt = parse_config(parse_json_strict(canonical_json(document).decode("utf-8")), base_dir=tmp_path)
    assert rebuilt == replace(repo_config, base_dir=tmp_path)
    assert rebuilt.to_dict() == document
    assert "base_dir" not in document
    assert rebuilt.digest() == repo_config.digest()


def test_serialization_does_not_expose_mutable_config_state(repo_config: Config) -> None:
    original_digest = repo_config.digest()
    exported = repo_config.to_dict()
    _array(exported["allowed_hosts"]).clear()
    _array(_object(exported["selection"])["reviewed_project_ids"]).append("TEST-ONLY-MUTATION")
    _object(exported["licence"])["reviewed"] = not repo_config.licence.reviewed
    assert repo_config.digest() == original_digest


def test_canonical_json_is_compact_sorted_utf8() -> None:
    value = {"z": [True, None], "a": "é"}
    assert canonical_json(value) == '{"a":"é","z":[true,null]}'.encode()
    assert canonical_json({"z": 1, "a": 2}) == canonical_json({"a": 2, "z": 1})


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_canonical_json_rejects_nonfinite_numbers(value: float) -> None:
    with pytest.raises(ValueError, match="Out of range float"):
        canonical_json({"test_only": [value]})


def test_digest_is_sha256_and_machine_independent(repo_config: Config, tmp_path: Path) -> None:
    digest = repo_config.digest()
    assert re.fullmatch(r"[0-9a-f]{64}", digest)
    assert digest == hashlib.sha256(canonical_json(repo_config.to_dict())).hexdigest()
    assert replace(repo_config, base_dir=tmp_path / "another-machine").digest() == digest
    assert replace(repo_config, title=repo_config.title + " TEST-ONLY").digest() != digest


def test_digest_ignores_json_object_order(document: dict[str, object], tmp_path: Path) -> None:
    reordered = dict(reversed(list(document.items())))
    original = parse_config(document, base_dir=tmp_path)
    other = parse_config(reordered, base_dir=tmp_path / "other")
    assert original.digest() == other.digest()


def test_selection_order_is_canonical(document: dict[str, object], tmp_path: Path) -> None:
    _set(document, ("selection", "reviewed_project_ids"), ["TEST-ONLY-B", "TEST-ONLY-A"])
    first = parse_config(document, base_dir=tmp_path)
    _set(document, ("selection", "reviewed_project_ids"), ["TEST-ONLY-A", "TEST-ONLY-B"])
    second = parse_config(document, base_dir=tmp_path)
    assert first.digest() == second.digest()


def test_resolve_local_path_does_not_depend_on_cwd(
    repo_config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = tmp_path / "configuration"
    base.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    config = replace(repo_config, base_dir=base)
    source = replace(
        repo_config.project_source,
        kind="geojson",
        url=None,
        where=None,
        path="inputs/test-only.geojson",
    )
    monkeypatch.chdir(elsewhere)
    assert config.resolve_local_path(source) == base / "inputs" / "test-only.geojson"


@pytest.mark.parametrize("path", ["../outside.geojson", "/outside.geojson"])
def test_resolve_local_path_rechecks_containment(repo_config: Config, tmp_path: Path, path: str) -> None:
    config = replace(repo_config, base_dir=tmp_path / "configuration")
    source = replace(repo_config.project_source, kind="geojson", path=path)
    with pytest.raises(ConfigError, match="resolves outside") as caught:
        config.resolve_local_path(source)
    assert caught.value.location == "sources.projects.path"


def test_resolve_local_path_rejects_symlink_escape(repo_config: Config, tmp_path: Path) -> None:
    base = tmp_path / "configuration"
    outside = tmp_path / "outside"
    base.mkdir()
    outside.mkdir()
    (base / "inputs").symlink_to(outside, target_is_directory=True)
    config = replace(repo_config, base_dir=base)
    source = replace(repo_config.project_source, kind="geojson", path="inputs/test-only.geojson")
    with pytest.raises(ConfigError, match="resolves outside") as caught:
        config.resolve_local_path(source)
    assert caught.value.location == "sources.projects.path"


def test_resolve_local_path_allows_contained_symlink(repo_config: Config, tmp_path: Path) -> None:
    base = tmp_path / "configuration"
    actual = base / "actual"
    actual.mkdir(parents=True)
    (base / "inputs").symlink_to(actual, target_is_directory=True)
    config = replace(repo_config, base_dir=base)
    source = replace(repo_config.project_source, kind="geojson", path="inputs/test-only.geojson")
    assert config.resolve_local_path(source) == actual / "test-only.geojson"


def test_load_config_uses_repository_path_after_chdir(
    repo_config: Config,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    assert load_config(CONFIG_PATH) == repo_config


@pytest.mark.parametrize("as_string", [False, True])
def test_load_config_path_types(
    document: dict[str, object],
    tmp_path: Path,
    as_string: bool,
) -> None:
    path = tmp_path / "test-only-config.json"
    path.write_bytes(canonical_json(document))
    argument: str | Path = str(path) if as_string else path
    loaded = load_config(argument)
    assert loaded.to_dict() == document
    assert loaded.base_dir == tmp_path.resolve()


@pytest.mark.parametrize("kind", ["missing", "directory"])
def test_load_config_read_errors(tmp_path: Path, kind: str) -> None:
    path = tmp_path / "test-only-config.json"
    if kind == "directory":
        path.mkdir()
    with pytest.raises(ConfigError, match="cannot read configuration") as caught:
        load_config(path)
    assert caught.value.location == str(path)
    assert isinstance(caught.value.__cause__, OSError)


def test_load_config_rejects_non_utf8(tmp_path: Path) -> None:
    path = tmp_path / "test-only-config.json"
    path.write_bytes(b"\xff")
    with pytest.raises(ConfigError, match="configuration must be UTF-8") as caught:
        load_config(path)
    assert caught.value.location == str(path)
    assert isinstance(caught.value.__cause__, UnicodeDecodeError)


def test_load_config_size_boundary(document: dict[str, object], tmp_path: Path) -> None:
    path = tmp_path / "test-only-config.json"
    encoded = canonical_json(document)
    assert len(encoded) < MAX_CONFIG_BYTES
    path.write_bytes(encoded + b" " * (MAX_CONFIG_BYTES - len(encoded)))
    assert load_config(path).to_dict() == document
    path.write_bytes(encoded + b" " * (MAX_CONFIG_BYTES + 1 - len(encoded)))
    with pytest.raises(ConfigError, match="configuration file is too large") as caught:
        load_config(path)
    assert caught.value.location == str(path)


@pytest.mark.parametrize(
    ("raw", "location", "message"),
    [
        (b"{", "$", "malformed JSON"),
        (b'{"a": 1, "a": 2}', "$", "duplicate JSON key"),
        (b'{"a": NaN}', "$", "non-finite JSON number"),
        (b"[]", "$", "must be a JSON object"),
        (b"{}", "$.schema_version", "is required"),
    ],
)
def test_load_config_wraps_validation_locations(
    tmp_path: Path,
    raw: bytes,
    location: str,
    message: str,
) -> None:
    path = tmp_path / "test-only-config.json"
    path.write_bytes(raw)
    with pytest.raises(ConfigError, match=message) as caught:
        load_config(path)
    assert caught.value.location == f"{path}:{location}"
    assert isinstance(caught.value.__cause__, ConfigError)


@pytest.mark.parametrize("token", ["1e9999", "-1e9999"])
def test_overflowing_json_numbers_cannot_enter_config(
    document: dict[str, object],
    tmp_path: Path,
    token: str,
) -> None:
    encoded = canonical_json(document).decode("utf-8")
    encoded = re.sub(r'"timeout_seconds":[^,}]+', f'"timeout_seconds":{token}', encoded, count=1)
    path = tmp_path / "test-only-config.json"
    path.write_text(encoded, encoding="utf-8")
    with pytest.raises(ConfigError, match="must be finite") as caught:
        load_config(path)
    assert caught.value.location == f"{path}:$.limits.timeout_seconds"


def test_snapshot_layout_paths(tmp_path: Path) -> None:
    layout = SnapshotLayout(tmp_path)
    source_id = "south_campbell_heights"
    source_dir = tmp_path / "sources" / source_id
    assert layout.manifest_path() == tmp_path / "snapshot-manifest.json"
    assert layout.capture_manifest_path() == tmp_path / "capture-manifest.json"
    assert layout.source_dir(source_id) == source_dir
    assert layout.metadata_path(source_id) == source_dir / "metadata.json"
    assert layout.count_path(source_id) == source_dir / "count.json"
    assert layout.ids_path(source_id) == source_dir / "ids.json"
    assert layout.final_count_path(source_id) == source_dir / "final-count.json"
    assert layout.final_ids_path(source_id) == source_dir / "final-ids.json"
    assert layout.requests_path(source_id) == source_dir / "requests.json"
    assert layout.batches_dir(source_id) == source_dir / "batches"


@pytest.mark.parametrize(
    ("index", "filename"),
    [(1, "batch-0001.geojson"), (9, "batch-0009.geojson"), (100, "batch-0100.geojson"), (9999, "batch-9999.geojson")],
)
def test_snapshot_batch_filenames(tmp_path: Path, index: int, filename: str) -> None:
    layout = SnapshotLayout(tmp_path)
    assert layout.batch_path("projects", index) == tmp_path / "sources" / "projects" / "batches" / filename


@pytest.mark.parametrize("index", [-1, 0, 10000])
def test_snapshot_batch_index_bounds(tmp_path: Path, index: int) -> None:
    with pytest.raises(ValueError, match="between 1 and 9999"):
        SnapshotLayout(tmp_path).batch_path("projects", index)


@pytest.mark.parametrize("source_id", ["", "../projects", "/projects", "a/b", r"a\b", ".", "..", "Projects", "a" * 32])
def test_snapshot_layout_rejects_unsafe_source_ids(tmp_path: Path, source_id: str) -> None:
    layout = SnapshotLayout(tmp_path)
    with pytest.raises(ValueError, match="invalid source id"):
        layout.source_dir(source_id)
    with pytest.raises(ValueError, match="invalid source id"):
        layout.metadata_path(source_id)
    with pytest.raises(ValueError, match="invalid source id"):
        layout.batch_path(source_id, 1)


@pytest.mark.parametrize("kind", ["geojson", "csv"])
def test_snapshot_local_input_paths(repo_config: Config, tmp_path: Path, kind: str) -> None:
    layout = SnapshotLayout(tmp_path)
    original = repo_config.project_source
    source: Source
    if kind == "geojson":
        source = replace(original, kind="geojson", url=None, where=None, path="inputs/test-only.geojson")
    else:
        source = replace(original, kind="csv", url=None, where=None, path="inputs/test-only.csv")
    assert layout.local_input_path(source) == tmp_path / "sources" / source.id / f"input.{kind}"


def test_snapshot_local_input_rejects_remote_source(repo_config: Config, tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not a local input"):
        SnapshotLayout(tmp_path).local_input_path(repo_config.project_source)


def test_field_mapping_public_type(repo_config: Config) -> None:
    mapping = repo_config.project_source.require_mapping()
    assert isinstance(mapping, FieldMapping)
    assert replace(mapping, source_record_id=mapping.project_id).source_fields() == mapping.source_fields()
