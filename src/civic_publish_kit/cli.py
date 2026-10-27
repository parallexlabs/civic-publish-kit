"""civic-publish: the City update rehearsal CLI with exactly six commands.

Commands: extract, normalize, validate, diff, package and rollback.
- Public data only. No credentials, no CMS, no admin UI and no writes to any City ArcGIS Online tenancy.
- Dataset and project values are printed as JSON data. They are never passed to a shell or eval.

Exit codes:
- 0: success.
- 2: blocked validation, a refused package, or any expected error. Errors are printed plainly to stderr without a
  traceback.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

from civic_publish_kit.acquire import AcquisitionError
from civic_publish_kit.config import ConfigError
from civic_publish_kit.rehearsal import extract, load_bundle, load_rehearsal_config, normalize
from civic_publish_kit.reports import build_artifacts, validate_report
from civic_publish_kit.store import EMPTY_BASELINE, ReleaseStore, StoreError, diff_hash

BLOCKED: Final = 2
DEFAULT_CONFIG: Final = Path("config/rehearsal.json")
_TIMESTAMP: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _emit(payload: object) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))


def _warn(message: str) -> None:
    print(message, file=sys.stderr)


def _removed(diff: object) -> dict[str, list[str]]:
    collections = diff.get("collections") if isinstance(diff, dict) else None
    out: dict[str, list[str]] = {}
    if isinstance(collections, dict):
        for name, part in collections.items():
            removed = part.get("removed") if isinstance(part, dict) else None
            if isinstance(removed, list) and removed:
                out[str(name)] = [str(r) for r in removed]
    return out


def _removal_warning(diff: object) -> bool:
    summary = diff.get("summary") if isinstance(diff, dict) else None
    return isinstance(summary, dict) and summary.get("removal_warning") is True


def _warn_removals(diff: object) -> None:
    removed = _removed(diff)
    if removed:
        total = sum(len(v) for v in removed.values())
        counts = ", ".join(
            f"{name}={len(removed.get(name, []))}" for name in ("projects", "buildings", "sources")
        )
        _warn(
            f"review required: {total} record(s) would be removed ({counts}); "
            "every removal needs explicit acknowledgment"
        )
    if _removal_warning(diff):
        _warn("WARNING: more than 20 percent of projects would disappear compared with the base release")


def _cmd_extract(args: argparse.Namespace) -> int:
    cfg = load_rehearsal_config(Path(args.config))
    manifest = extract(cfg, Path(args.out), None if args.from_fixtures is None else Path(args.from_fixtures))
    query = manifest.get("query")
    if isinstance(query, dict) and query.get("scope") == "service_sample":
        _warn("note: this snapshot is a bounded service sample, not a complete City export")
    _emit(manifest)
    return 0


def _cmd_normalize(args: argparse.Namespace) -> int:
    cfg = load_rehearsal_config(Path(args.config))
    buildings = None if args.buildings is None else Path(args.buildings)
    result = normalize(cfg, Path(args.snapshot), Path(args.out), buildings)
    tally = Counter((f.severity, f.code) for f in result.findings)
    summary = [{"severity": s, "code": c, "count": n} for (s, c), n in sorted(tally.items())]
    blocking = sum(1 for f in result.findings if f.severity == "blocking")
    counts = {"projects": len(result.projects), "buildings": len(result.buildings), "blocking": blocking,
              "warnings": sum(1 for f in result.findings if f.severity == "warning")}
    _emit({"ok": result.ok, "out": str(Path(args.out)), "counts": counts, "findings": summary})
    if not result.ok:
        _warn(f"blocked: {blocking} blocking error(s); the bundle was written for review but cannot be packaged")
        return BLOCKED
    return 0


def _cmd_validate(args: argparse.Namespace) -> int:
    report = validate_report(Path(args.input), Path(args.out))
    _emit({"ok": report.ok, "counts": report.counts, "report": str(report.report_path), "files": list(report.files)})
    if not report.ok:
        _warn(f"blocked: {len(report.errors)} blocking error(s); see errors.txt and report.html")
        return BLOCKED
    return 0


def _cmd_diff(args: argparse.Namespace) -> int:
    store = ReleaseStore(Path(args.store))
    diff = store.diff(str(args.before), str(args.after))
    _warn_removals(diff)
    _emit({"diff": diff, "diff_hash": diff_hash(diff)})
    return 0


def _cmd_package(args: argparse.Namespace) -> int:
    bundle = load_bundle(Path(args.input))
    if not bundle.ok:
        blocking = [f for f in bundle.findings if f.severity == "blocking"]
        _warn(f"blocked: the bundle has {len(blocking)} blocking error(s) and cannot be packaged; run validate")
        return BLOCKED
    created_at = str(args.timestamp) if args.timestamp else str(bundle.provenance.get("retrieved_at"))
    if not _TIMESTAMP.fullmatch(created_at):
        raise ValueError("--timestamp must be a UTC time like 2026-10-06T23:17:20Z")
    store = ReleaseStore(Path(args.store))
    candidate = store.create_candidate(build_artifacts(bundle), created_at=created_at)
    _warn_removals(candidate.diff)
    payload: dict[str, object] = {"release_id": candidate.release_id, "diff_hash": candidate.diff_hash,
                                  "diff": candidate.diff, "created": candidate.created, "promoted": False}
    if args.ack_diff is None:
        payload["review"] = {
            "message": "Current is unchanged. Review the diff, then rerun with --ack-diff set to diff_hash.",
            "removed": _removed(candidate.diff), "removal_warning": _removal_warning(candidate.diff)}
    else:
        pointer = store.accept(candidate.release_id, str(args.ack_diff))
        payload["promoted"] = True
        payload["promotion"] = {"release_id": pointer.release_id, "previous_release_id": pointer.previous_release_id,
                                "diff_hash": pointer.diff_hash, "removal_warning": pointer.removal_warning}
    _emit(payload)
    return 0


def _cmd_rollback(args: argparse.Namespace) -> int:
    pointer = ReleaseStore(Path(args.store)).rollback(str(args.release))
    _emit({"release_id": pointer.release_id, "previous_release_id": pointer.previous_release_id,
           "action": pointer.action,
           "note": "Only the local current pointer changed. No remote service was modified."})
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="civic-publish",
                                     description="City update rehearsal (public data only; no remote writes).")
    sub = parser.add_subparsers(dest="command", required=True)
    ext = sub.add_parser("extract", help="capture a dated layer-167 snapshot or replay genuine fixtures")
    ext.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    ext.add_argument("--out", type=Path, required=True)
    ext.add_argument("--from-fixtures", type=Path, default=None)
    nor = sub.add_parser("normalize", help="transform to WGS84 and write a normalized bundle")
    nor.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    nor.add_argument("--snapshot", type=Path, required=True)
    nor.add_argument("--out", type=Path, required=True)
    nor.add_argument("--buildings", type=Path, default=None)
    val = sub.add_parser("validate", help="verify a bundle and write report.html and errors.txt")
    val.add_argument("--input", type=Path, required=True)
    val.add_argument("--out", type=Path, required=True)
    dif = sub.add_parser("diff", help="show field-level changes and the acknowledgment hash")
    dif.add_argument("--before", required=True, help=f"release ID or {EMPTY_BASELINE}")
    dif.add_argument("--after", required=True)
    dif.add_argument("--store", type=Path, required=True)
    pkg = sub.add_parser("package", help="create an immutable package; promote only with an exact fresh hash")
    pkg.add_argument("--input", type=Path, required=True)
    pkg.add_argument("--store", type=Path, required=True)
    pkg.add_argument("--timestamp", default=None, help="UTC YYYY-MM-DDTHH:MM:SSZ (default: source retrieval time)")
    pkg.add_argument("--ack-diff", default=None)
    rol = sub.add_parser("rollback", help="verify a prior package and restore the local current pointer")
    rol.add_argument("--release", required=True)
    rol.add_argument("--store", type=Path, required=True)
    return parser


_HANDLERS: Final[dict[str, Callable[[argparse.Namespace], int]]] = {
    "extract": _cmd_extract, "normalize": _cmd_normalize, "validate": _cmd_validate, "diff": _cmd_diff,
    "package": _cmd_package, "rollback": _cmd_rollback,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return _HANDLERS[str(args.command)](args)
    except (AcquisitionError, ConfigError, StoreError, OSError, ValueError) as exc:
        _warn(f"error: {exc}")
        return BLOCKED


if __name__ == "__main__":
    raise SystemExit(main())
