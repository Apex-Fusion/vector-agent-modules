#!/usr/bin/env sh
# Check the vendored identity library against the checksums in VENDORED.md.
#
# Usage:
#   scripts/check-vendored.sh [DIR...]
#       Check each DIR. The default is shared/ and the projects that link the
#       library: Module-3/reputation-staking and
#       Module-6/contracts/governance-suggestion. The checksums always come
#       from shared/VENDORED.md; the listed paths are relative to each DIR.
#   scripts/check-vendored.sh --self-test
#       Copy the vendored files to a temporary directory and show that the
#       check passes on the copy and fails when a file changes, disappears or
#       when an unlisted file appears in a vendored directory.
#
# VENDORED.md holds a fenced block with the info string `sha256`. Each line is
# in `sha256sum` format: "<sha256>  <path relative to DIR>". A vendored
# directory is a listed directory whose name is `vector_identity`; it must hold
# only listed files. Symbolic links to the files and directories count as the
# files and directories.
#
# Exit status: 0 when the check (or the self-test) passes, 1 otherwise.
set -eu

script_dir=$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)
shared_dir=$(dirname -- "$script_dir")
repo_dir=$(dirname -- "$shared_dir")

# Print the checksum lines of the `sha256` block of the VENDORED.md in $1.
checksums() {
  awk '
    /^```sha256[[:space:]]*$/ { inside = 1; next }
    /^```/ { inside = 0 }
    inside && NF { print }
  ' "$1/VENDORED.md"
}

# Print the listed paths of the VENDORED.md in $1.
listed_paths() {
  checksums "$1" | awk '{ print $2 }'
}

# Print the vendored directories (listed directories named vector_identity).
vendored_dirs() {
  listed_paths "$1" | while IFS= read -r path; do
    parent=$(dirname -- "$path")
    case "$parent" in
      vector_identity | */vector_identity) printf '%s\n' "$parent" ;;
    esac
  done | sort -u
}

# Check the files in $2 against the VENDORED.md in $1.
check() {
  md_dir=$1
  dir=$2
  if [ ! -f "$md_dir/VENDORED.md" ]; then
    echo "check-vendored: $md_dir/VENDORED.md is missing" >&2
    return 1
  fi
  lines=$(checksums "$md_dir")
  if [ -z "$lines" ]; then
    echo "check-vendored: $md_dir/VENDORED.md has no sha256 block" >&2
    return 1
  fi
  status=0
  if ! (cd "$dir" && printf '%s\n' "$lines" | sha256sum --check --strict --quiet -); then
    echo "check-vendored: checksum mismatch in $dir" >&2
    status=1
  fi
  paths=$(listed_paths "$md_dir")
  for vdir in $(vendored_dirs "$md_dir"); do
    for file in $(cd "$dir" && find -H "$vdir" -type f | sort); do
      if ! printf '%s\n' "$paths" | grep -qx -- "$file"; then
        echo "check-vendored: $dir/$file is not listed in VENDORED.md" >&2
        status=1
      fi
    done
  done
  return "$status"
}

copy_files() {
  from=$1
  to=$2
  cp "$from/VENDORED.md" "$to/VENDORED.md"
  for vdir in $(vendored_dirs "$from"); do
    mkdir -p "$to/$vdir"
    cp -R "$from/$vdir/." "$to/$vdir/"
  done
  for path in $(listed_paths "$from"); do
    mkdir -p "$to/$(dirname -- "$path")"
    cp "$from/$path" "$to/$path"
  done
}

self_test() {
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT INT TERM
  failures=0

  expect() {
    want=$1
    label=$2
    if check "$tmp" "$tmp" >/dev/null 2>&1; then got=pass; else got=fail; fi
    if [ "$got" = "$want" ]; then
      echo "self-test: $label: $got (expected)"
    else
      echo "self-test: $label: $got, expected $want" >&2
      failures=$((failures + 1))
    fi
  }

  copy_files "$shared_dir" "$tmp"
  expect pass "unchanged copy"

  first=$(listed_paths "$shared_dir" | head -n 1)
  printf ' ' >>"$tmp/$first"
  expect fail "modified $first"
  cp "$shared_dir/$first" "$tmp/$first"
  expect pass "restored $first"

  rm "$tmp/$first"
  expect fail "missing $first"
  cp "$shared_dir/$first" "$tmp/$first"

  vdir=$(vendored_dirs "$shared_dir" | head -n 1)
  if [ -n "$vdir" ]; then
    printf 'pub const extra: Int = 1\n' >"$tmp/$vdir/extra.ak"
    expect fail "unlisted file $vdir/extra.ak"
    rm "$tmp/$vdir/extra.ak"
  fi
  expect pass "final copy"

  [ "$failures" -eq 0 ]
}

case "${1:-}" in
  --self-test)
    self_test
    ;;
  -h | --help)
    sed -n '2,21p' "$0"
    ;;
  *)
    if [ "$#" -eq 0 ]; then
      set -- "$shared_dir" \
        "$repo_dir/Module-3/reputation-staking" \
        "$repo_dir/Module-6/contracts/governance-suggestion"
    fi
    result=0
    for dir in "$@"; do
      if check "$shared_dir" "$dir"; then
        echo "check-vendored: $dir matches VENDORED.md"
      else
        echo "check-vendored: $dir does not match VENDORED.md" >&2
        result=1
      fi
    done
    exit "$result"
    ;;
esac
