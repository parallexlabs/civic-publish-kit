#!/usr/bin/env bash
# 60-second offline rehearsal of all six civic-publish commands using genuine bounded fixtures A and B.
# No City service calls: extract replays tests/fixtures/rehearsal/{a,b}. Set DEMO_KEEP=1 to keep outputs.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
cd "$ROOT"
CONFIG="config/rehearsal.json"
TS="2026-10-07T00:00:00Z"

if command -v uv >/dev/null 2>&1; then
  CLI=(uv run --no-sync civic-publish)
else
  CLI=(civic-publish)
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/civic-publish-demo.XXXXXX")"
WORK="$(cd "$WORK" && pwd -P)"
cleanup() {
  if [[ "${DEMO_KEEP:-0}" == "1" ]]; then
    echo "kept outputs: $WORK"
  else
    rm -rf -- "$WORK"
  fi
}
trap cleanup EXIT
STORE="$WORK/store"

fail() { echo "demo failed: $*" >&2; exit 1; }

jget() {
  python3 - "$@" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    value = json.load(handle)
for key in sys.argv[2:]:
    value = value[key]
print(value if isinstance(value, str) else json.dumps(value, sort_keys=True))
PY
}

hashdir() {
  python3 - "$1" <<'PY'
import hashlib
import pathlib
import sys

for path in sorted(p for p in pathlib.Path(sys.argv[1]).iterdir() if p.is_file()):
    print(hashlib.sha256(path.read_bytes()).hexdigest(), path.name)
PY
}

run_to() {
  local out="$1"
  shift
  if ! "${CLI[@]}" "$@" >"$out" 2>"$out.err"; then
    cat "$out.err" >&2
    fail "civic-publish $1"
  fi
}

for name in a b; do
  run_to "$WORK/extract-$name.json" extract --config "$CONFIG" --out "$WORK/snapshot-$name" \
    --from-fixtures "tests/fixtures/rehearsal/$name"
  echo "[extract $name] records=$(jget "$WORK/extract-$name.json" counts initial)" \
    "scope=$(jget "$WORK/extract-$name.json" query scope) where=$(jget "$WORK/extract-$name.json" query where)"
  run_to "$WORK/normalize-$name.json" normalize --config "$CONFIG" --snapshot "$WORK/snapshot-$name" \
    --out "$WORK/bundle-$name"
  echo "[normalize $name] ok=$(jget "$WORK/normalize-$name.json" ok)" \
    "projects=$(jget "$WORK/normalize-$name.json" counts projects)" \
    "buildings=$(jget "$WORK/normalize-$name.json" counts buildings)"
  run_to "$WORK/validate-$name.json" validate --input "$WORK/bundle-$name" --out "$WORK/review-$name"
  echo "[validate $name] ok=$(jget "$WORK/validate-$name.json" ok) report=$WORK/review-$name/report.html"
done

run_to "$WORK/package-a.json" package --input "$WORK/bundle-a" --store "$STORE" --timestamp "$TS"
RID_A="$(jget "$WORK/package-a.json" release_id)"
HASH_A="$(jget "$WORK/package-a.json" diff_hash)"
[[ ! -e "$STORE/current.json" ]] || fail "package without acknowledgment changed current"
echo "[package a] candidate=$RID_A review_hash=$HASH_A current=unchanged"

run_to "$WORK/diff-a.json" diff --before empty --after "$RID_A" --store "$STORE"
[[ "$(jget "$WORK/diff-a.json" diff_hash)" == "$HASH_A" ]] || fail "diff hash differs from package review hash"
echo "[diff empty->a] added=$(jget "$WORK/diff-a.json" diff summary added_projects) hash matches"

run_to "$WORK/promote-a.json" package --input "$WORK/bundle-a" --store "$STORE" --timestamp "$TS" \
  --ack-diff "$HASH_A"
[[ "$(jget "$WORK/promote-a.json" promoted)" == "true" ]] || fail "A was not promoted"
[[ "$(jget "$STORE/current.json" release_id)" == "$RID_A" ]] || fail "current is not A"
echo "[package a --ack-diff] promoted=true created=$(jget "$WORK/promote-a.json" created)"
hashdir "$STORE/releases/$RID_A" >"$WORK/a-before.sha256"

run_to "$WORK/package-b.json" package --input "$WORK/bundle-b" --store "$STORE" --timestamp "$TS"
RID_B="$(jget "$WORK/package-b.json" release_id)"
HASH_B="$(jget "$WORK/package-b.json" diff_hash)"
echo "[package b] candidate=$RID_B removed=$(jget "$WORK/package-b.json" review removed)" \
  "removal_warning=$(jget "$WORK/package-b.json" review removal_warning)"
echo "  note: B's query is OBJECTID IN (1,2) versus A's (1,2,3); this removal is a query-scope change," \
  "not a City deletion."

cp "$STORE/current.json" "$WORK/current-before-stale.json"
stale_status=0
if "${CLI[@]}" package --input "$WORK/bundle-b" --store "$STORE" --timestamp "$TS" --ack-diff "$HASH_A" \
  >"$WORK/stale.json" 2>"$WORK/stale.err"; then
  stale_status=0
else
  stale_status=$?
fi
if [[ "$stale_status" -ne 2 ]]; then
  cat "$WORK/stale.err" >&2
  fail "stale acknowledgment returned exit $stale_status; expected exactly 2"
fi
if ! grep -Fq -- "acknowledgment does not match" "$WORK/stale.err"; then
  cat "$WORK/stale.err" >&2
  fail "stale acknowledgment failed for an unrelated reason"
fi
cmp -s "$WORK/current-before-stale.json" "$STORE/current.json" \
  || fail "stale acknowledgment changed current pointer bytes"
[[ "$(jget "$STORE/current.json" release_id)" == "$RID_A" ]] || fail "stale acknowledgment changed current"
echo "[package b stale ack] rejected with exit 2 and expected acknowledgment diagnostic; current=$RID_A unchanged"

run_to "$WORK/diff-b.json" diff --before "$RID_A" --after "$RID_B" --store "$STORE"
[[ "$(jget "$WORK/diff-b.json" diff_hash)" == "$HASH_B" ]] || fail "fresh diff hash differs for B"
run_to "$WORK/promote-b.json" package --input "$WORK/bundle-b" --store "$STORE" --timestamp "$TS" \
  --ack-diff "$HASH_B"
[[ "$(jget "$STORE/current.json" release_id)" == "$RID_B" ]] || fail "current is not B"
echo "[diff a->b + package b --ack-diff] removal reviewed and promoted with fresh hash"

run_to "$WORK/rollback.json" rollback --release "$RID_A" --store "$STORE"
[[ "$(jget "$STORE/current.json" release_id)" == "$RID_A" ]] || fail "rollback did not restore A"
hashdir "$STORE/releases/$RID_A" >"$WORK/a-after.sha256"
cmp -s "$WORK/a-before.sha256" "$WORK/a-after.sha256" || fail "prior package bytes changed"
echo "[rollback a] previous=$(jget "$WORK/rollback.json" previous_release_id)" \
  "files=$(wc -l <"$WORK/a-after.sha256" | tr -d ' ') byte-identical; local pointer only, no remote effect"

echo "review downloads (temporary):"
echo "  $WORK/review-a/report.html  $WORK/review-a/projects.csv  $WORK/review-a/errors.txt"
echo "  package: $STORE/releases/$RID_A"
echo "demo complete"
