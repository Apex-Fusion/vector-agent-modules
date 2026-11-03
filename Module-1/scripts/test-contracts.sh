#!/usr/bin/env sh
# Run the Module-1 contract tests: the vendored library check and its
# self-test, then `aiken check` in contracts/. Extra arguments go to
# `aiken check` (for example `-m identity_party`).
set -eu

module_dir=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

"$module_dir/scripts/check-vendored.sh" "$module_dir"
"$module_dir/scripts/check-vendored.sh" --self-test
cd "$module_dir/contracts"
aiken check "$@"
