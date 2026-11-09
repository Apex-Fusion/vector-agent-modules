#!/usr/bin/env sh
# Run the jury contract tests: the vendored library check and its self-test,
# then `aiken check` in contracts/. Extra arguments go to `aiken check` (for
# example `-m "jury_tests/case_tests.{..}"`).
set -eu

jury_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

"$jury_dir/scripts/check-vendored.sh"
"$jury_dir/scripts/check-vendored.sh" --self-test
cd "$jury_dir/contracts"
aiken check "$@"
