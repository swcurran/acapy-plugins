#!/usr/bin/env bash
# A script to run a local test of a specific version of acapy-agent against all
# (or selected) plugins' integration tests. The script is designed for testing
# ACA-Py release candidates to verify them before a new release.
#
# The script pins each plugin's acapy-agent dependency to the specified version,
# runs the integration tests, and then reverts any changes made to
# pyproject.toml and poetry.lock files.
#
# Usage: ./test-acapy-version.sh <acapy-agent-version> [plugin ...]
#
# Examples: ./test-acapy-version.sh 1.7.0rc0 ./test-acapy-version.sh 1.7.0rc0
# basicmessage_storage webvh
#
# Requires: poetry, docker (with compose plugin). Run from the repo root.

set -uo pipefail

VERSION="${1:?Usage: $0 <acapy-agent-version> [plugin ...]}"
shift || true

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$REPO_ROOT"

# A plugin is any top-level directory whose pyproject.toml declares an
# acapy-agent dependency. Derived instead of hardcoded so new plugins are
# picked up automatically. plugin_globals is excluded: it holds shared
# pyproject.toml sections for repo_manager.py, not an actual plugin (matches
# pr-integration-tests.yaml, which excludes it the same way).
ALL_PLUGINS=()
while IFS= read -r pj; do
  plugin="$(dirname "$pj")"
  [ "$plugin" == "plugin_globals" ] && continue
  ALL_PLUGINS+=("$plugin")
done < <(grep -l "acapy-agent" */pyproject.toml 2>/dev/null | sort)

# Matches pr-integration-tests.yaml, which skips cheqd's integration tests.
SKIP_TESTS=(cheqd)

if [ "$#" -gt 0 ]; then
  PLUGINS=("$@")
else
  PLUGINS=("${ALL_PLUGINS[@]}")
fi

DIRTY=$(git status --porcelain -- '*/pyproject.toml' '*/poetry.lock' 2>/dev/null)
if [ -n "$DIRTY" ]; then
  echo "error: uncommitted changes already present in pyproject.toml/poetry.lock files. Commit or stash first." >&2
  echo "$DIRTY" >&2
  exit 1
fi

TOUCHED_FILES=()
cleanup() {
  echo
  echo "== Reverting version bump changes =="
  if [ "${#TOUCHED_FILES[@]}" -gt 0 ]; then
    git checkout -- "${TOUCHED_FILES[@]}"
    echo "Reverted: ${TOUCHED_FILES[*]}"
  else
    echo "(nothing to revert)"
  fi
}
trap cleanup EXIT

declare -A RESULT

set_version() {
  local pyproject="$1" version="$2"
  python3 - "$pyproject" "$version" <<'PYEOF'
import re, sys
path, version = sys.argv[1], sys.argv[2]
text = open(path).read()
new_text, n = re.subn(
    r'(acapy-agent\s*=\s*\{\s*version\s*=\s*")[^"]+(")',
    r'\g<1>' + version + r'\g<2>',
    text,
)
if n == 0:
    print(f"warning: no acapy-agent dependency line found in {path}", file=sys.stderr)
    sys.exit(1)
open(path, "w").write(new_text)
PYEOF
}

for plugin in "${PLUGINS[@]}"; do
  echo
  echo "== [$plugin] pinning acapy-agent == $VERSION =="
  pj="$plugin/pyproject.toml"
  lock="$plugin/poetry.lock"

  if [ ! -f "$pj" ]; then
    echo "skip: $pj not found"
    RESULT[$plugin]="no-pyproject"
    continue
  fi

  if ! set_version "$pj" "$VERSION"; then
    RESULT[$plugin]="no-acapy-dep"
    continue
  fi
  TOUCHED_FILES+=("$pj")

  echo "== [$plugin] regenerating poetry.lock =="
  if ! (cd "$plugin" && poetry lock); then
    echo "FAIL: [$plugin] poetry lock failed (version $VERSION likely unresolvable)"
    RESULT[$plugin]="lock-failed"
    [ -f "$lock" ] && TOUCHED_FILES+=("$lock")
    continue
  fi
  TOUCHED_FILES+=("$lock")

  if [[ " ${SKIP_TESTS[*]} " == *" $plugin "* ]]; then
    echo "skip: [$plugin] integration tests skipped (matches CI)"
    RESULT[$plugin]="skipped"
    continue
  fi

  if [ ! -f "$plugin/integration/docker-compose.yml" ]; then
    echo "skip: [$plugin] no integration/docker-compose.yml"
    RESULT[$plugin]="no-integration-tests"
    continue
  fi

  echo "== [$plugin] docker compose build =="
  (
    cd "$plugin/integration"
    if [ -f ./init-network.sh ]; then . ./init-network.sh; fi
    docker compose build
  )
  if [ $? -ne 0 ]; then
    echo "FAIL: [$plugin] docker compose build failed"
    RESULT[$plugin]="build-failed"
    (cd "$plugin/integration" && docker compose down --remove-orphans --rmi local 2>/dev/null)
    continue
  fi

  echo "== [$plugin] running integration tests =="
  (
    cd "$plugin/integration"
    if [ "$plugin" == "cache_redis" ]; then
      docker compose up -d && docker compose run --rm tests
    else
      docker compose up --exit-code-from tests
    fi
  )
  TEST_EXIT=$?

  (cd "$plugin/integration" && docker compose down --remove-orphans --rmi local 2>/dev/null)

  if [ "$TEST_EXIT" -eq 0 ]; then
    RESULT[$plugin]="pass"
  else
    RESULT[$plugin]="fail"
  fi
done

echo
echo "================ Summary (acapy-agent $VERSION) ================"
for plugin in "${PLUGINS[@]}"; do
  printf "%-30s %s\n" "$plugin" "${RESULT[$plugin]:-not-run}"
done
