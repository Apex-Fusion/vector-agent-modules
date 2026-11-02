#!/usr/bin/env sh
# Run the contract tests of the projects that use the shared library: the
# vendored library check and its self-test, then `aiken check` in shared/,
# Module-3/reputation-staking and Module-6/contracts/governance-suggestion.
# Extra arguments go to each `aiken check` (for example `-D`).
set -eu

shared_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
repo_dir=$(dirname -- "$shared_dir")

"$shared_dir/scripts/check-vendored.sh"
"$shared_dir/scripts/check-vendored.sh" --self-test
for project in \
  "$shared_dir" \
  "$repo_dir/Module-3/reputation-staking" \
  "$repo_dir/Module-6/contracts/governance-suggestion"; do
  echo "test-contracts: aiken check in $project"
  (cd "$project" && aiken check "$@")
done
