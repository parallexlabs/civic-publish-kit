"""civic-publish-kit: safe, reproducible releases of public civic GeoJSON/CSV data.

This is a public-data reference implementation. It is not City-endorsed software and
does not perform remote writes, credentialed access or deployment.
"""

from civic_publish_kit.config import (
    Config,
    ConfigError,
    FieldMapping,
    Licence,
    Limits,
    PilotArea,
    SnapshotLayout,
    Source,
    StatusMap,
    StatusRule,
    canonical_json,
    load_config,
    parse_config,
)

__version__ = "0.1.0"

__all__ = [
    "Config",
    "ConfigError",
    "FieldMapping",
    "Licence",
    "Limits",
    "PilotArea",
    "SnapshotLayout",
    "Source",
    "StatusMap",
    "StatusRule",
    "__version__",
    "canonical_json",
    "load_config",
    "parse_config",
]
