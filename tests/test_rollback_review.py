"""Genuine A/B regression proof: rollback is recovery, not unacknowledged promotion [14]."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, cast

import pytest

from civic_publish_kit.cli import main
from civic_publish_kit.config import canonical_json, parse_json_strict
from civic_publish_kit.rehearsal import extract, load_bundle, load_rehearsal_config, normalize
from civic_publish_kit.reports import build_artifacts
from civic_publish_kit.store import (
    CHECKSUM_FILE,
    CURRENT_FILE,
    EMPTY_BASELINE,
    REVIEW_DIR,
    REVIEW_SCHEMA,
    CandidateResult,
    ReleaseStore,
    StoreError,
    diff_hash,
)

Json = dict[str, Any]
ROOT = Path(__file__).resolve().parents[1]
CAPTURES = ROOT / "tests" / "fixtures" / "rehearsal"
STAMP = "2026-10-06T23:30:00Z"


def _obj(value: object) -> Json:
    assert isinstance(value, dict)
    return cast(Json, value)


def _read(path: Path) -> Json:
    return _obj(parse_json_strict(path.read_text(encoding="utf-8")))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _proof(store: ReleaseStore, release_id: str) -> Path:
    return store.root / REVIEW_DIR / f"{release_id}.json"


def _accept(store: ReleaseStore, candidate: CandidateResult) -> None:
    base = store.read_current() or EMPTY_BASELINE
    fresh = store.diff(base, candidate.release_id)
    store.accept(candidate.release_id, diff_hash(fresh), accepted_at=STAMP)


def _accepted_pair(
    store: ReleaseStore, artifacts: tuple[dict[str, bytes], dict[str, bytes]],
) -> tuple[CandidateResult, CandidateResult]:
    a = store.create_candidate(artifacts[0], created_at=STAMP)
    _accept(store, a)
    b = store.create_candidate(artifacts[1], created_at=STAMP)
    _accept(store, b)
    return a, b


@pytest.fixture
def artifacts(tmp_path: Path) -> tuple[dict[str, bytes], dict[str, bytes]]:
    cfg = load_rehearsal_config(ROOT / "config" / "rehearsal.json")
    data: list[dict[str, bytes]] = []
    for name in ("a", "b"):
        snapshot = tmp_path.resolve() / f"snapshot-{name}"
        bundle = tmp_path.resolve() / f"bundle-{name}"
        extract(cfg, snapshot, from_fixtures=CAPTURES / name)
        result = normalize(cfg, snapshot, bundle)
        assert result.ok
        data.append(build_artifacts(load_bundle(bundle)))
    return data[0], data[1]


@pytest.fixture
def store(tmp_path: Path) -> ReleaseStore:
    return ReleaseStore(tmp_path.resolve() / "store")


def test_unreviewed_deleting_candidate_cannot_be_promoted_by_cli_rollback(
    store: ReleaseStore,
    artifacts: tuple[dict[str, bytes], dict[str, bytes]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    a = store.create_candidate(artifacts[0], created_at=STAMP)
    _accept(store, a)
    b = store.create_candidate(artifacts[1], created_at=STAMP)
    assert b.diff["summary"]["removed_projects"] == 1
    assert b.diff["summary"]["has_removals"] is True
    assert store.read_current() == a.release_id
    pointer = (store.root / CURRENT_FILE).read_bytes()
    package_a = dict(store.verify_release(a.release_id).files)
    package_b = dict(store.verify_release(b.release_id).files)
    assert not _proof(store, b.release_id).exists()
    assert main(["rollback", "--release", b.release_id, "--store", str(store.root)]) == 2
    output = capsys.readouterr()
    assert "review proof" in output.err
    assert "Traceback" not in output.err
    assert not output.out.strip()
    assert (store.root / CURRENT_FILE).read_bytes() == pointer
    assert store.read_current() == a.release_id
    assert dict(store.verify_release(a.release_id).files) == package_a
    assert dict(store.verify_release(b.release_id).files) == package_b


def test_never_accepted_initial_candidate_cannot_bootstrap_current(
    store: ReleaseStore, artifacts: tuple[dict[str, bytes], dict[str, bytes]],
) -> None:
    candidate = store.create_candidate(artifacts[0], created_at=STAMP)
    assert store.read_current() is None
    assert store.verify_release(candidate.release_id).base_release_id is None
    with pytest.raises(StoreError, match="review proof"):
        store.rollback(candidate.release_id, at=STAMP)
    assert store.read_current() is None
    assert not (store.root / CURRENT_FILE).exists()
    assert not _proof(store, candidate.release_id).exists()


def test_accepted_a_b_rollback_preserves_all_seventeen_files_and_review_receipts(
    store: ReleaseStore, artifacts: tuple[dict[str, bytes], dict[str, bytes]],
) -> None:
    a, b = _accepted_pair(store, artifacts)
    package_a = dict(store.verify_release(a.release_id).files)
    package_b = dict(store.verify_release(b.release_id).files)
    proof_a = _proof(store, a.release_id).read_bytes()
    proof_b = _proof(store, b.release_id).read_bytes()
    assert len(package_a) == 17
    assert len(package_b) == 17
    receipt = _read(_proof(store, b.release_id))
    assert receipt["schema"] == REVIEW_SCHEMA
    assert receipt["release_id"] == b.release_id
    assert receipt["base_release_id"] == a.release_id
    assert receipt["diff"]["base"] == a.release_id
    assert receipt["diff"]["candidate"] == b.release_id
    assert receipt["diff_hash"] == diff_hash(receipt["diff"])
    assert receipt["package_sha256"] == _sha(package_b[CHECKSUM_FILE])
    assert receipt["accepted_at"] == STAMP
    assert set(package_a).isdisjoint({REVIEW_DIR, f"{a.release_id}.json"})
    result = store.rollback(a.release_id, at=STAMP)
    assert result.action == "rollback"
    assert result.previous_release_id == b.release_id
    assert store.read_current() == a.release_id
    assert dict(store.verify_release(a.release_id).files) == package_a
    assert dict(store.verify_release(b.release_id).files) == package_b
    for name, original in package_a.items():
        actual = (store.releases / a.release_id / name).read_bytes()
        assert actual == original
        assert _sha(actual) == _sha(original)
    assert _proof(store, a.release_id).read_bytes() == proof_a
    assert _proof(store, b.release_id).read_bytes() == proof_b


@pytest.mark.parametrize(
    "fault",
    [
        "missing", "tampered", "schema", "release_id", "base", "diff_hash", "package_hash",
        "timestamp", "diff_identity", "file_symlink", "directory_symlink",
    ],
)
def test_absent_corrupt_or_symlink_review_proof_cannot_change_pointer(
    store: ReleaseStore,
    artifacts: tuple[dict[str, bytes], dict[str, bytes]],
    tmp_path: Path,
    fault: str,
) -> None:
    a, b = _accepted_pair(store, artifacts)
    path = _proof(store, a.release_id)
    pointer = (store.root / CURRENT_FILE).read_bytes()
    originals = dict(store.verify_release(a.release_id).files)
    if fault == "missing":
        path.unlink()
    elif fault == "tampered":
        path.write_bytes(b'{"TEST_ONLY":"corrupt receipt"}')
    elif fault == "file_symlink":
        external = tmp_path.resolve() / "receipt-copy"
        external.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(external)
    elif fault == "directory_symlink":
        directory = store.root / REVIEW_DIR
        external = tmp_path.resolve() / "review-copy"
        directory.rename(external)
        directory.symlink_to(external, target_is_directory=True)
    else:
        proof = _read(path)
        if fault == "schema":
            proof["schema"] = "TEST-ONLY invalid"
        elif fault == "release_id":
            proof["release_id"] = b.release_id
        elif fault == "base":
            proof["base_release_id"] = "../escape"
        elif fault == "diff_hash":
            proof["diff_hash"] = "f" * 64
        elif fault == "package_hash":
            proof["package_sha256"] = "f" * 64
        elif fault == "timestamp":
            proof["accepted_at"] = "2026-02-30T00:00:00Z"
        else:
            proof["diff"]["candidate"] = b.release_id
        payload = {key: value for key, value in proof.items() if key != "sha256"}
        proof["sha256"] = _sha(canonical_json(payload))
        path.write_bytes(canonical_json(proof))
    with pytest.raises(StoreError, match="review|release ID|timestamp|symlink"):
        store.rollback(a.release_id, at=STAMP)
    assert (store.root / CURRENT_FILE).read_bytes() == pointer
    assert dict(store.verify_release(a.release_id).files) == originals


def test_failed_ack_does_not_leave_rollback_authorization(
    store: ReleaseStore, artifacts: tuple[dict[str, bytes], dict[str, bytes]],
) -> None:
    a = store.create_candidate(artifacts[0], created_at=STAMP)
    _accept(store, a)
    b = store.create_candidate(artifacts[1], created_at=STAMP)
    pointer = (store.root / CURRENT_FILE).read_bytes()
    with pytest.raises(StoreError, match="acknowledgment"):
        store.accept(b.release_id, "0" * 64, accepted_at=STAMP)
    assert not _proof(store, b.release_id).exists()
    with pytest.raises(StoreError, match="review proof"):
        store.rollback(b.release_id, at=STAMP)
    assert (store.root / CURRENT_FILE).read_bytes() == pointer
    assert store.read_current() == a.release_id
    assert not list((store.root / REVIEW_DIR).glob(".review-*.tmp"))


def test_failed_pointer_promotion_removes_pending_review_proof(
    store: ReleaseStore,
    artifacts: tuple[dict[str, bytes], dict[str, bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = store.create_candidate(artifacts[0], created_at=STAMP)
    _accept(store, a)
    b = store.create_candidate(artifacts[1], created_at=STAMP)
    pointer = (store.root / CURRENT_FILE).read_bytes()
    proof_a = _proof(store, a.release_id).read_bytes()
    original = Path.replace

    def fail(self: Path, target: str | Path) -> Path:
        if self.name.startswith(".current-"):
            raise OSError("TEST-ONLY pointer commit failure")
        return original(self, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail)
        with pytest.raises(OSError, match="TEST-ONLY"):
            store.accept(b.release_id, b.diff_hash, accepted_at=STAMP)
    assert not _proof(store, b.release_id).exists()
    assert _proof(store, a.release_id).read_bytes() == proof_a
    assert (store.root / CURRENT_FILE).read_bytes() == pointer
    assert not list((store.root / REVIEW_DIR).glob(".review-*.tmp"))
    with pytest.raises(StoreError, match="review proof"):
        store.rollback(b.release_id, at=STAMP)
    assert store.read_current() == a.release_id


def test_failed_review_commit_restores_pointer_after_successful_pointer_write(
    store: ReleaseStore,
    artifacts: tuple[dict[str, bytes], dict[str, bytes]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a = store.create_candidate(artifacts[0], created_at=STAMP)
    _accept(store, a)
    b = store.create_candidate(artifacts[1], created_at=STAMP)
    pointer = (store.root / CURRENT_FILE).read_bytes()
    originals_a = dict(store.verify_release(a.release_id).files)
    originals_b = dict(store.verify_release(b.release_id).files)
    original = Path.rename
    observed: list[str] = []

    def fail(self: Path, target: str | Path) -> Path:
        if self.parent.name == REVIEW_DIR and self.name.startswith(".review-"):
            observed.append(_read(store.root / CURRENT_FILE)["release_id"])
            raise OSError("TEST-ONLY review commit failure")
        return original(self, target)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "rename", fail)
        with pytest.raises(OSError, match="TEST-ONLY"):
            store.accept(b.release_id, b.diff_hash, accepted_at=STAMP)
    assert observed == [b.release_id]
    assert (store.root / CURRENT_FILE).read_bytes() == pointer
    assert not _proof(store, b.release_id).exists()
    assert not list((store.root / REVIEW_DIR).glob(".review-*.tmp"))
    assert not (store.root / ".lock").exists()
    assert store.read_current() == a.release_id
    assert dict(store.verify_release(a.release_id).files) == originals_a
    assert dict(store.verify_release(b.release_id).files) == originals_b
    with pytest.raises(StoreError, match="review proof"):
        store.rollback(b.release_id, at=STAMP)


def test_prior_review_allows_recovery_even_when_current_package_is_damaged(
    store: ReleaseStore, artifacts: tuple[dict[str, bytes], dict[str, bytes]],
) -> None:
    a, b = _accepted_pair(store, artifacts)
    original_a = dict(store.verify_release(a.release_id).files)
    damaged = store.releases / b.release_id / "report.html"
    damaged.write_bytes(damaged.read_bytes() + b"TEST-ONLY damaged current evidence")
    evidence = damaged.read_bytes()
    with pytest.raises(StoreError, match="checksum"):
        store.read_current()
    store.rollback(a.release_id, at=STAMP)
    assert store.read_current() == a.release_id
    assert dict(store.verify_release(a.release_id).files) == original_a
    assert damaged.read_bytes() == evidence


def test_pointer_action_and_candidate_ancestry_are_not_review_proof(
    store: ReleaseStore, artifacts: tuple[dict[str, bytes], dict[str, bytes]],
) -> None:
    a = store.create_candidate(artifacts[0], created_at=STAMP)
    _accept(store, a)
    b = store.create_candidate(artifacts[1], created_at=STAMP)
    assert store.verify_release(b.release_id).base_release_id == a.release_id
    pointer = _read(store.root / CURRENT_FILE)
    assert pointer["action"] == "accept"
    before = (store.root / CURRENT_FILE).read_bytes()
    with pytest.raises(StoreError, match="review proof"):
        store.rollback(b.release_id, at=STAMP)
    assert (store.root / CURRENT_FILE).read_bytes() == before
    _proof(store, a.release_id).unlink()
    with pytest.raises(StoreError, match="review proof"):
        store.read_current()
